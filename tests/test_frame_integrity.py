"""Tests for intermittent-distortion triage instrumentation.

Covers the diagnostic-only frame-integrity path
(acquisition CRC -> registry -> consumer-snapshot CRC) plus two minimal
production fixes:

* ``SharedMemoryPublisher.latest()`` must serve repeated calls (persistent
  pinned consumer instead of one colliding temp consumer per call).
* ``ProcessingConsumer`` must re-anchor to the newest frame after its
  expected sequence was overwritten (no livelock on a stale sequence).

All tests are synthetic; no TV46L hardware is required.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

import numpy as np
import pytest

from thermal_monitor.camera.acquisition import AcquisitionWorker
from thermal_monitor.camera.model import GrabResult
from thermal_monitor.camera.shm import create_ring_buffer_and_publisher
from thermal_monitor.core.frame import (
    Frame,
    FrameDescriptor,
    FramePayload,
    StreamMetadata,
    SyncInfo,
    SyncStatus,
)
from thermal_monitor.core.frame_integrity import (
    FrameIntegrityRegistry,
    compute_thermal_crc,
    get_default_registry,
    integrity_diag_enabled,
    integrity_sample_every,
)
from thermal_monitor.core.models import AnalysisConfig
from thermal_monitor.core.shm import SharedMemoryPublisher, create_ring_buffer
from thermal_monitor.processing.alarms import NullAlarmEvaluator
from thermal_monitor.processing.consumer import ProcessingConsumer
from thermal_monitor.processing.pipeline import SimpleProcessingPipeline
from thermal_monitor.ui.modes.thermal_render_worker import (
    RenderRequest,
    ThermalRenderWorker,
)
from tests.conftest import FakeFrameSource, make_camera_config


# ─── Helpers ──────────────────────────────────────────────────────────────────

def unique_camera(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def make_grab(sequence: int, width: int = 16, height: int = 16) -> GrabResult:
    array = ((np.arange(width * height, dtype=np.uint32).reshape(height, width) + sequence * 7919) % 65536).astype(np.uint16)
    array.setflags(write=False)
    return GrabResult(
        thermal=array,
        thermal_format="IR_Data",
        frame_id=1000 + sequence,
        packet_stats={"packets_seen": sequence * 10, "packets_lost": 0, "blocks_incomplete": 0, "blocks_discarded": 0},
        grab_started=time.perf_counter(),
        grab_completed=time.perf_counter(),
        converted_at=time.perf_counter(),
    )


def make_frame(camera_id: str, sequence: int, width: int = 16, height: int = 16) -> Frame:
    thermal = (np.arange(width * height, dtype=np.uint16).reshape(height, width) + sequence).astype(np.uint16)
    thermal.setflags(write=False)
    descriptor = FrameDescriptor(
        camera_id=camera_id,
        sequence=sequence,
        timestamp=1000.0 + sequence,
        monotonic_timestamp=100.0 + sequence,
        thermal=StreamMetadata(
            present=True, width=width, height=height, pixel_format="IR_Data",
            dtype="uint16", byte_count=thermal.nbytes, sequence=5000 + sequence,
            timestamp=1000.0 + sequence, monotonic_timestamp=100.0 + sequence,
        ),
        visible=StreamMetadata(present=False),
        sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
        metadata={"packet_stats": {"packets_seen": 0, "packets_lost": 0, "blocks_incomplete": 0, "blocks_discarded": 0}},
    )
    return Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal, visible=None))


class BlockingPipeline(SimpleProcessingPipeline):
    """Pipeline that blocks inside process_frame until released (test seam)."""

    def __init__(self, config: AnalysisConfig) -> None:
        super().__init__(config)
        self.entered = threading.Event()
        self.release = threading.Event()

    def process_frame(self, frame: Frame):
        self.entered.set()
        assert self.release.wait(timeout=15.0), "pipeline gate was never released"
        return super().process_frame(frame)


# ─── CRC helper ───────────────────────────────────────────────────────────────

def test_crc_deterministic_and_none():
    a = np.arange(256, dtype=np.uint16).reshape(16, 16)
    a.setflags(write=False)
    b = a.copy()
    b.setflags(write=False)
    assert compute_thermal_crc(a) == compute_thermal_crc(b)
    c = (a + 1).astype(np.uint16)
    c.setflags(write=False)
    assert compute_thermal_crc(c) != compute_thermal_crc(a)
    assert compute_thermal_crc(None) is None


def test_crc_read_only_input_not_mutated():
    a = np.arange(64, dtype=np.uint16).reshape(8, 8)
    a.setflags(write=False)
    before = compute_thermal_crc(a)
    compute_thermal_crc(a)
    assert not a.flags.writeable
    assert compute_thermal_crc(a) == before


# ─── Registry ─────────────────────────────────────────────────────────────────

def test_registry_match():
    reg = FrameIntegrityRegistry()
    assert reg.record_acquisition("cam", 7, 7007, 0x12345678, 1.0, 2.0) is None
    assert reg.record_snapshot("cam", 7, 7007, 0x12345678, 1.5, 2.5) is True
    stats = reg.stats(camera_id="cam")
    assert (stats.acquisitions_sampled, stats.snapshots_checked, stats.snapshots_matched) == (1, 1, 1)
    assert stats.snapshots_mismatched == 0
    assert stats.last_mismatch is None


def test_registry_mismatch_records_details():
    reg = FrameIntegrityRegistry()
    reg.record_acquisition("cam", 3, 3003, 0xAAAAAAAA, 1.0, 2.0)
    assert reg.record_snapshot("cam", 3, 3003, 0xBBBBBBBB, 1.5, 2.5) is False
    stats = reg.stats(camera_id="cam")
    assert stats.snapshots_mismatched == 1
    assert stats.last_mismatch is not None
    assert stats.last_mismatch.sequence == 3
    assert stats.last_mismatch.acquisition_crc32 == 0xAAAAAAAA
    assert stats.last_mismatch.snapshot_crc32 == 0xBBBBBBBB


def test_registry_unsampled_and_filter_and_reset():
    reg = FrameIntegrityRegistry()
    assert reg.record_snapshot("cam_a", 99, None, 0x1, 0.0, 0.0) is None
    reg.record_acquisition("cam_b", 1, 11, 0x2, 0.0, 0.0)
    assert reg.stats(camera_id="cam_a").snapshots_unsampled == 1
    assert reg.stats(camera_id="cam_b").acquisitions_sampled == 1
    assert reg.stats().snapshots_checked == 1
    reg.reset(camera_id="cam_a")
    assert reg.stats(camera_id="cam_a").snapshots_checked == 0
    assert reg.stats(camera_id="cam_b").acquisitions_sampled == 1
    reg.reset()
    assert reg.stats().acquisitions_sampled == 0


def test_env_gating(monkeypatch):
    monkeypatch.delenv("TMS_FRAME_INTEGRITY_DIAG", raising=False)
    assert integrity_diag_enabled() is False
    assert integrity_diag_enabled(True) is True
    monkeypatch.setenv("TMS_FRAME_INTEGRITY_DIAG", "1")
    assert integrity_diag_enabled() is True
    assert integrity_diag_enabled(False) is False
    monkeypatch.delenv("TMS_FRAME_INTEGRITY_EVERY", raising=False)
    assert integrity_sample_every() == 30
    monkeypatch.setenv("TMS_FRAME_INTEGRITY_EVERY", "7")
    assert integrity_sample_every() == 7
    assert integrity_sample_every(1) == 1


# ─── SharedMemoryPublisher.latest() regression ────────────────────────────────

def test_publisher_latest_repeated_calls():
    ring = create_ring_buffer(
        camera_id=unique_camera("cam_latest"),
        thermal_width=8, thermal_height=8,
        thermal_dtype=np.dtype(np.uint16), depth=4,
    )
    try:
        publisher = SharedMemoryPublisher(ring)
        publisher.publish(make_frame(ring._config.camera_id, 0, 8, 8))
        first = publisher.latest()
        second = publisher.latest()  # previously raised ValueError (name collision)
        assert first is not None and second is not None
        assert first.descriptor.sequence == 0
        assert second.descriptor.sequence == 0
        np.testing.assert_array_equal(first.payload.thermal, second.payload.thermal)
        # Returned frames are durable copies, not SHM views.
        assert first.payload.thermal.base is None or not isinstance(first.payload.thermal.base, memoryview)
        publisher.close()
        assert publisher.latest() is None
    finally:
        ring.close()


# ─── End-to-end: worker CRC == consumer-snapshot CRC ──────────────────────────

def test_worker_and_consumer_checksums_match():
    camera_id = unique_camera("cam_crc")
    ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=8)
    registry = FrameIntegrityRegistry()
    grabs = [make_grab(i) for i in range(60)]
    worker = AcquisitionWorker(
        camera_id, FakeFrameSource(grab_script=list(grabs)), publisher,
        make_camera_config(), integrity_diag=True, integrity_sample_every=1,
        integrity_registry=registry,
    )
    results: list = []
    pipeline = SimpleProcessingPipeline(config=AnalysisConfig(camera_id=camera_id))
    consumer = ProcessingConsumer(
        camera_id=camera_id, ring_buffer=ring,
        consumer_name=f"processing_{camera_id}", pipeline=pipeline,
        alarm_evaluator=NullAlarmEvaluator(), result_callback=results.append,
        integrity_diag=True, integrity_sample_every=1, integrity_registry=registry,
    )
    try:
        worker.start()
        consumer.start()
        assert consumer.wait_for_frames(20, timeout=15.0)
        worker.stop()
        consumer.stop()
        stats = registry.stats(camera_id=camera_id)
        assert stats.acquisitions_sampled > 0
        assert stats.snapshots_matched > 0
        assert stats.snapshots_mismatched == 0
        assert worker.integrity_stats().snapshots_mismatched == 0
        assert consumer.integrity_stats().snapshots_matched > 0
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


# ─── ProcessingConsumer overwrite recovery ────────────────────────────────────

def test_processing_consumer_recovers_after_overwrite_burst():
    camera_id = unique_camera("cam_burst")
    ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=2)
    pipeline = BlockingPipeline(config=AnalysisConfig(camera_id=camera_id))
    consumer = ProcessingConsumer(
        camera_id=camera_id, ring_buffer=ring,
        consumer_name=f"processing_{camera_id}", pipeline=pipeline,
        alarm_evaluator=NullAlarmEvaluator(),
    )
    try:
        assert publisher.publish(make_frame(camera_id, 0)).accepted
        consumer.start()
        assert pipeline.entered.wait(timeout=10.0), "consumer never picked up seq 0"
        # Overwrite everything while the consumer is blocked on seq 0.
        for seq in range(1, 10):
            assert publisher.publish(make_frame(camera_id, seq)).accepted
        pipeline.release.set()
        assert consumer.wait_for_frames(2, timeout=10.0), "consumer stuck on overwritten sequence"
        consumer.stop()
        stats = consumer.stats()
        assert stats.last_sequence == 9
        assert stats.frames_processed >= 2
    finally:
        pipeline.release.set()
        try:
            consumer.close()
        except Exception:
            pass
        ring.close()


# ─── Render-path trace fields ─────────────────────────────────────────────────

def test_render_request_carries_hw_sequence_and_counters():
    worker = ThermalRenderWorker(max_fps=1000.0)
    try:
        assert worker.submitted_frames == 0
        assert worker.rendered_frames == 0
        worker.submit(RenderRequest(np.zeros((2, 2), dtype=np.float32), 1, hw_sequence=101))
        assert worker.submitted_frames == 1
        assert worker._pending is not None and worker._pending.hw_sequence == 101
        # Same sequence replaces pending work (latest-wins): counted as
        # submitted, and the replaced request counts as dropped.
        worker.submit(RenderRequest(np.zeros((2, 2), dtype=np.float32), 1, hw_sequence=101))
        assert worker.submitted_frames == 2
        assert worker.dropped_frames == 1
    finally:
        try:
            worker.stop()
        except RuntimeError:
            pass


# ─── Production env-var configuration path ────────────────────────────────────

def test_env_vars_drive_production_path_with_visible_info_output(monkeypatch, caplog):
    """TMS_FRAME_INTEGRITY_DIAG/EVERY must enable sampling through the exact
    production configuration path (no explicit flags) and the run must emit
    INFO startup lines plus INFO shutdown summaries even with zero mismatches.
    """
    monkeypatch.setenv("TMS_FRAME_INTEGRITY_DIAG", "1")
    monkeypatch.setenv("TMS_FRAME_INTEGRITY_EVERY", "1")
    caplog.set_level(logging.INFO)

    camera_id = unique_camera("cam_env")
    get_default_registry().reset(camera_id=camera_id)
    ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=8)
    grabs = [make_grab(i) for i in range(30)]
    # Production construction: no integrity flags, env decides.
    worker = AcquisitionWorker(
        camera_id, FakeFrameSource(grab_script=list(grabs)), publisher,
        make_camera_config(),
    )
    assert worker._integrity_diag is True
    assert worker._integrity_every == 1
    results: list = []
    pipeline = SimpleProcessingPipeline(config=AnalysisConfig(camera_id=camera_id))
    consumer = ProcessingConsumer(
        camera_id=camera_id, ring_buffer=ring,
        consumer_name=f"processing_{camera_id}", pipeline=pipeline,
        alarm_evaluator=NullAlarmEvaluator(), result_callback=results.append,
    )
    assert consumer._integrity_diag is True
    assert consumer._integrity_every == 1
    # Both sides share the process-global registry (object identity).
    assert worker._integrity is get_default_registry()
    assert consumer._integrity is get_default_registry()
    try:
        worker.start()
        consumer.start()
        assert consumer.wait_for_frames(10, timeout=15.0)
        worker.stop()
        consumer.stop()

        stats = get_default_registry().stats(camera_id=camera_id)
        assert stats.acquisitions_sampled > 0
        assert stats.snapshots_checked > 0
        assert stats.snapshots_mismatched == 0

        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
        assert any(
            m.startswith("FRAME INTEGRITY DIAG") and camera_id in m for m in messages
        ), "missing INFO startup line"
        summaries = [m for m in messages if m.startswith("FRAME INTEGRITY SUMMARY")]
        assert any(camera_id in m for m in summaries), "missing INFO shutdown summary"
        worker_summary = next(m for m in summaries if "acquired_frames=" in m and camera_id in m)
        assert "mismatched=0" in worker_summary
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
        get_default_registry().reset(camera_id=camera_id)
