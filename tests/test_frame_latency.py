"""Tests for acquisition-to-display latency triage + raw IR capture.

Covers, without hardware:

* ``core.frame_latency`` tracker math (segments, percentiles, display age,
  behind-frames, stale-display regression flag, env gating).
* ``core.raw_ir_diag`` capture (files, cadence, disabled path, sanitation).
* Production wiring: env flags drive worker/consumer marks; INFO summaries.
* Focus/NUC control-plane must not hold ``CameraRuntimeService._lock``
  across blocking GVCP IO (GUI stats stay responsive during a settle).
* Render throttle floor (20 fps default) and trace-field compatibility.
* Widget display-age plumbing (seq/meta map bounded + pop on render).
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication

from thermal_monitor.camera.acquisition import AcquisitionWorker
from thermal_monitor.camera.model import CameraIdentity as DriverCameraIdentity
from thermal_monitor.camera.model import CameraConfig as DriverCameraConfig
from thermal_monitor.camera.model import CameraValidationResult, GrabResult
from thermal_monitor.camera.tv46_custom import CustomTV46LDriver
from thermal_monitor.camera.shm import create_ring_buffer_and_publisher
from thermal_monitor.config import CamerasConfig, RecordingConfig, StorageConfig, SystemConfig
from thermal_monitor.core.frame_latency import (
    FrameLatencyTracker,
    format_summary_brief,
    get_default_tracker,
    latency_enabled,
    latency_sample_every,
)
from thermal_monitor.core.models import AnalysisConfig
from thermal_monitor.core.models import CameraConfig as AppCameraConfig
from thermal_monitor.core.models import CameraIdentity as AppCameraIdentity
from thermal_monitor.core.raw_ir_diag import (
    maybe_dump_raw,
    raw_diag_enabled,
    safe_camera_name,
)
from thermal_monitor.processing.alarms import NullAlarmEvaluator
from thermal_monitor.processing.consumer import ProcessingConsumer
from thermal_monitor.processing.pipeline import SimpleProcessingPipeline
from thermal_monitor.services.runtime import CameraRuntimeService
from thermal_monitor.ui.modes.observer_image import LiveThermalWidget
from thermal_monitor.ui.modes.thermal_render_worker import ThermalRenderWorker
from thermal_monitor.ui.modes.vl_render_worker import VlRenderRequest, VlRenderWorker
from thermal_monitor.ui.modes.thermal_render_worker import RenderRequest
from tests.conftest import FakeFrameSource, make_camera_config


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def unique_camera(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def make_grab(sequence: int, width: int = 16, height: int = 16) -> GrabResult:
    array = (
        (np.arange(width * height, dtype=np.uint32).reshape(height, width) + sequence * 131)
        % 65536
    ).astype(np.uint16)
    array.setflags(write=False)
    return GrabResult(
        thermal=array,
        thermal_format="IR_Data",
        frame_id=2000 + sequence,
        packet_stats={"packets_seen": 0, "packets_lost": 0, "blocks_incomplete": 0, "blocks_discarded": 0},
        grab_started=time.perf_counter(),
        grab_completed=time.perf_counter(),
        converted_at=time.perf_counter(),
    )


# ─── Env gating ───────────────────────────────────────────────────────────────

def test_latency_env_gating(monkeypatch):
    monkeypatch.delenv("TMS_FRAME_LATENCY_DIAG", raising=False)
    assert latency_enabled() is False
    assert latency_enabled(True) is True
    monkeypatch.setenv("TMS_FRAME_LATENCY_DIAG", "1")
    assert latency_enabled() is True
    assert latency_enabled(False) is False
    monkeypatch.delenv("TMS_FRAME_LATENCY_EVERY", raising=False)
    assert latency_sample_every() == 10
    monkeypatch.setenv("TMS_FRAME_LATENCY_EVERY", "5")
    assert latency_sample_every() == 5


def test_raw_diag_env_gating(monkeypatch):
    monkeypatch.delenv("TMS_RAW_IR_DIAG", raising=False)
    assert raw_diag_enabled() is False
    monkeypatch.setenv("TMS_RAW_IR_DIAG", "yes")
    assert raw_diag_enabled() is True
    assert safe_camera_name("cam/../x") == "cam_.._x"


# ─── Tracker math ─────────────────────────────────────────────────────────────

def _drive_tracker(tracker: FrameLatencyTracker, stream: str = "cam", n: int = 5):
    # Offsets (ms from grab T0) model a 9 fps feed: ~110 ms blocking grab
    # wait, then a ~25 ms pipeline to display.
    base = 1_000_000_000
    for seq in range(n):
        t0 = base + seq * 111_000_000
        complete = t0 + 110_000_000
        tracker.note_published(stream, seq, 100 + seq, t0, complete, complete + 1_000_000, sample_every=1)
        tracker.note_stage(stream, seq, "consumed", complete + 2_000_000)
        tracker.note_stage(stream, seq, "process_start", complete + 3_000_000)
        tracker.note_stage(stream, seq, "process_end", complete + 5_000_000)
        tracker.note_stage(stream, seq, "emitted", complete + 6_000_000)
        tracker.note_stage(stream, seq, "ui_received", complete + 8_000_000)
        tracker.note_stage(stream, seq, "render_start", complete + 10_000_000)
        tracker.note_stage(stream, seq, "render_done", complete + 20_000_000)
        tracker.note_displayed(stream, seq, 100 + seq, complete, complete + 25_000_000)
    return base


def test_tracker_segments_and_ages():
    tracker = FrameLatencyTracker()
    _drive_tracker(tracker)
    summary = tracker.summary("cam")
    assert summary["sampled_frames"] == 5
    assert summary["published_total"] == 5
    assert summary["regressions"] == 0
    seg = summary["segments_ms"]
    assert seg["H_grab_wait"]["median_ms"] == pytest.approx(110.0)
    assert seg["A_acq_to_shm"]["median_ms"] == pytest.approx(1.0)
    assert seg["C_processing"]["median_ms"] == pytest.approx(2.0)
    assert seg["F_render"]["median_ms"] == pytest.approx(10.0)
    assert seg["G_acq_to_display"]["median_ms"] == pytest.approx(25.0)
    assert seg["G_acq_to_display"]["n"] == 5
    age = summary["display_age_ms"]
    assert age["n"] == 5 and age["max_ms"] == pytest.approx(25.0)
    behind = summary["behind_frames"]
    assert behind["n"] == 5 and behind["max_ms"] == pytest.approx(0.0)
    hw_delta = summary["hw_frame_delta"]
    assert hw_delta["n"] == 5 and hw_delta["max_ms"] == pytest.approx(0.0)
    assert summary["last_displayed_hw"] == 104
    assert "G_acq_to_display" in format_summary_brief(summary)
    assert "H_grab_wait" in format_summary_brief(summary)
    assert "hw_frame_delta" in format_summary_brief(summary)


def test_tracker_behind_frames_counts_queue():
    tracker = FrameLatencyTracker()
    base = 9_000_000_000
    for seq in range(5):
        tracker.note_published("cam", seq, seq, base, base, base, sample_every=1)
    # Display the oldest while 4 newer frames are already published.
    tracker.note_displayed("cam", 0, 0, base - 500_000_000, base)
    summary = tracker.summary("cam")
    assert summary["behind_frames"]["max_ms"] == pytest.approx(4.0)
    assert summary["display_age_ms"]["max_ms"] == pytest.approx(500.0)
    assert summary["hw_frame_delta"]["max_ms"] == pytest.approx(4.0)
    assert summary["latest_hw"] == 4
    assert summary["last_displayed_hw"] == 0


def test_tracker_regression_logs_error(caplog):
    tracker = FrameLatencyTracker()
    with caplog.at_level(logging.ERROR):
        tracker.note_displayed("cam", 10, 10, None, 1_000)
        tracker.note_displayed("cam", 9, 9, None, 2_000)
    assert tracker.summary("cam")["regressions"] == 1
    assert any("FRAME LATENCY REGRESSION" in r.getMessage() for r in caplog.records)


def test_tracker_unsampled_and_reset():
    tracker = FrameLatencyTracker()
    tracker.note_stage("cam", 999, "consumed", 5)  # no entry: no-op, no raise
    assert tracker.summary("cam")["sampled_frames"] == 0
    assert tracker.latest_published("ghost") is None
    _drive_tracker(tracker, n=2)
    tracker.reset(stream="cam")
    assert tracker.summary("cam")["sampled_frames"] == 0
    assert tracker.latest_published("cam") is None


# ─── Raw IR capture ───────────────────────────────────────────────────────────

def test_raw_dump_writes_ir_and_temp(tmp_path):
    thermal = np.arange(64, dtype=np.uint16).reshape(8, 8)
    thermal.setflags(write=False)
    temp = np.arange(64, dtype=np.float32).reshape(8, 8)
    path = maybe_dump_raw("cam/15", 7, 7007, thermal, temp, enabled=True, every=1, directory=tmp_path)
    assert path is not None and path.exists()
    assert "cam_15" in path.name and "seq000007" in path.name and "hw7007" in path.name
    np.testing.assert_array_equal(np.load(str(path)), thermal)
    np.testing.assert_array_equal(np.load(str(path.with_name(path.name.replace("_ir.npy", "_temp.npy")))), temp)


def test_raw_dump_cadence_disabled_and_none(tmp_path):
    thermal = np.zeros((4, 4), dtype=np.uint16)
    assert maybe_dump_raw("c", 3, 3, thermal, enabled=True, every=2, directory=tmp_path) is None
    assert maybe_dump_raw("c", 4, 4, thermal, enabled=True, every=2, directory=tmp_path) is not None
    assert maybe_dump_raw("c", 4, 4, thermal, enabled=False, every=1, directory=tmp_path) is None
    assert maybe_dump_raw("c", 4, 4, None, enabled=True, every=1, directory=tmp_path) is None
    # Never raises, even on garbage (returns a path or None, never throws).
    maybe_dump_raw("c", 0, 0, object(), enabled=True, every=1, directory=tmp_path)


# ─── Production wiring (env-driven) ───────────────────────────────────────────

def test_latency_marks_flow_worker_to_consumer(monkeypatch, caplog):
    monkeypatch.setenv("TMS_FRAME_LATENCY_DIAG", "1")
    monkeypatch.setenv("TMS_FRAME_LATENCY_EVERY", "1")
    caplog.set_level(logging.INFO)
    camera_id = unique_camera("cam_lat")
    tracker = get_default_tracker()
    tracker.reset(stream=camera_id)
    ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=8)
    grabs = [make_grab(i) for i in range(30)]
    source = FakeFrameSource(grab_script=list(grabs))
    _orig_grab = source.grab

    def _throttled_grab(timeout_ms):
        # Throttle the synthetic source so the consumer keeps up and every
        # sampled published entry is still retained (no eviction race).
        time.sleep(0.02)
        return _orig_grab(timeout_ms)

    source.grab = _throttled_grab
    worker = AcquisitionWorker(
        camera_id, source, publisher, make_camera_config()
    )
    assert worker._latency_diag is True
    pipeline = SimpleProcessingPipeline(config=AnalysisConfig(camera_id=camera_id))
    consumer = ProcessingConsumer(
        camera_id=camera_id, ring_buffer=ring,
        consumer_name=f"processing_{camera_id}", pipeline=pipeline,
        alarm_evaluator=NullAlarmEvaluator(), result_callback=lambda r: None,
    )
    try:
        worker.start()
        consumer.start()
        assert consumer.wait_for_frames(10, timeout=15.0)
        worker.stop()
        consumer.stop()
        summary = tracker.summary(camera_id)
        assert summary["published_total"] > 0
        assert summary["sampled_frames"] > 0
        assert summary["segments_ms"]["B_shm_to_consumer"]["n"] > 0
        assert summary["segments_ms"]["C_processing"]["n"] > 0
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
        assert any(m.startswith("FRAME LATENCY DIAG") and camera_id in m for m in messages)
        assert any(m.startswith("FRAME LATENCY SUMMARY") for m in messages)
    finally:
        try:
            worker.stop(timeout=2.0)
        except Exception:
            pass
        try:
            consumer.close()
        except Exception:
            pass
        ring.close()
        tracker.reset(stream=camera_id)


# ─── Focus must not hold the runtime lock across GVCP IO ──────────────────────

class SlowFocusDriver(CustomTV46LDriver):
    """Production driver type whose focus write blocks like a 5 s settle."""

    def __init__(self, config: DriverCameraConfig) -> None:
        super().__init__(config, camera_ip=config.ip_address or "127.0.0.1")
        self.settle_s = 2.0
        self._grab_seq = 0

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def is_connected(self) -> bool:
        return True

    def reopen(self) -> None:
        return None

    def validate_registers(self, expected_scda_ip: str = "", expected_fusion_value: int = 3):
        return CameraValidationResult(scda_ok=True, scp_ok=True, fusion_ok=True, checks=())

    def grab(self, timeout_ms: int) -> GrabResult:
        self._grab_seq += 1
        array = np.zeros((640, 480), dtype=np.uint16)
        array.setflags(write=False)
        now = time.perf_counter()
        return GrabResult(
            thermal=array,
            thermal_format="IR_Data",
            frame_id=self._grab_seq,
            packet_stats={"packets_seen": 0, "packets_lost": 0, "blocks_incomplete": 0, "blocks_discarded": 0},
            grab_started=now,
            grab_completed=now,
            converted_at=now,
        )

    def get_focus_limits(self, op_id=None):
        return (100, 9000)

    def get_focus_mm(self, op_id=None):
        return 4800

    def set_focus_mm(self, value_mm, settle_timeout=0.0, op_id=None):
        time.sleep(self.settle_s)  # blocking GVCP + motor settle
        return int(value_mm)


def _lock_test_service(holder: dict):
    def factory(cfg):
        src = SlowFocusDriver(cfg)
        holder["source"] = src
        return src

    return CameraRuntimeService(
        cameras_config=CamerasConfig(),
        system_config=SystemConfig(),
        recording_config=RecordingConfig(),
        storage_config=StorageConfig(),
        source_factory=factory,
    )


def _lock_test_camera(camera_id: str) -> AppCameraConfig:
    return AppCameraConfig(
        identity=AppCameraIdentity(camera_id=camera_id, serial_number="SN_LOCK"),
        name="127.0.0.1",
        metadata={"ip_address": "127.0.0.1", "grab_timeout_ms": 500},
    )


def test_focus_settle_does_not_block_runtime_stats():
    camera_id = unique_camera("cam_lock")
    holder: dict = {}
    service = _lock_test_service(holder)
    service.start_camera(_lock_test_camera(camera_id))
    try:
        assert service.is_camera_running(camera_id)
        errors: list = []
        done = threading.Event()

        def do_focus():
            try:
                service.set_focus_mm(camera_id, 4800)
            except Exception as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=do_focus, daemon=True)
        started = time.monotonic()
        thread.start()
        time.sleep(0.3)  # let the focus op enter its blocking settle
        stats_started = time.monotonic()
        stats = service.camera_stats(camera_id)
        stats_elapsed = time.monotonic() - stats_started
        assert stats is not None
        # Old code held runtime._lock for the whole ~2 s settle; stats must
        # stay responsive (generous bound: well under the settle duration).
        assert stats_elapsed < 1.0, f"camera_stats blocked {stats_elapsed:.2f}s on focus op"
        assert done.wait(timeout=10.0)
        assert not errors
        assert time.monotonic() - started >= 1.5  # the settle really blocked the op
    finally:
        service.shutdown()


# ─── Render throttle + trace fields ───────────────────────────────────────────

def test_render_throttle_floor_inside_latency_budget():
    assert ThermalRenderWorker()._interval == pytest.approx(0.05)
    assert VlRenderWorker()._interval == pytest.approx(0.05)


def test_render_request_trace_fields_default_none():
    req = RenderRequest(np.zeros((2, 2), dtype=np.float32), 3)
    assert req.camera_id is None and req.acq_mono_ns is None and req.hw_sequence is None
    vreq = VlRenderRequest(yuyv=np.zeros((2, 4), dtype=np.uint8), sequence=3)
    assert vreq.camera_id is None and vreq.acq_mono_ns is None


# ─── Widget display-age plumbing ──────────────────────────────────────────────

class _FakeThermalMeta:
    sequence = 4242


class _FakeDescriptor:
    sequence = 17
    camera_id = "cam_widget"
    thermal = _FakeThermalMeta()
    monotonic_timestamp = 0.0


class _FakePayload:
    def __init__(self, arr):
        self.thermal = arr


class _FakeFrame:
    def __init__(self, arr):
        self.descriptor = _FakeDescriptor()
        self.payload = _FakePayload(arr)


def test_widget_tracks_meta_and_reports_display_age(qapp, monkeypatch):
    monkeypatch.setenv("TMS_FRAME_LATENCY_DIAG", "1")
    tracker = get_default_tracker()
    tracker.reset(stream="cam_widget")
    widget = LiveThermalWidget()
    try:
        mono_s = time.perf_counter()
        mono_ns = int(mono_s * 1e9)
        _FakeDescriptor.monotonic_timestamp = mono_s
        tracker.note_published("cam_widget", 17, 4242, mono_ns, mono_ns, mono_ns, sample_every=1)
        arr = np.full((4, 4), 25.0, dtype=np.float32)
        widget.set_frame(arr, _FakeFrame(arr))
        assert widget._last_submitted_sequence == 17
        assert widget._last_camera_id == "cam_widget"
        assert widget._pending_meta[17][0] == "cam_widget"
        assert widget._pending_meta[17][1] == 4242
        image = QImage(4, 4, QImage.Format.Format_RGB888)
        image.fill(0)
        rgb = np.zeros((4, 4, 3), dtype=np.uint8)
        widget._on_rendered(image, arr, 20.0, 30.0, 17, image, rgb)
        assert 17 not in widget._pending_meta  # popped, map stays bounded
        summary = tracker.summary("cam_widget")
        assert summary["last_displayed_seq"] == 17
        assert summary["display_age_ms"]["n"] == 1
        assert summary["behind_frames"]["n"] == 1
    finally:
        widget.close()
        tracker.reset(stream="cam_widget")
