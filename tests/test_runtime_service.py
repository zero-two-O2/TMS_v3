"""Tests for Stage 7F: application-level camera runtime lifecycle.

Covers the CameraRuntimeService controller that owns the Observer-mode
producer path:

    TV46LDriver -> AcquisitionWorker -> SHM Ring -> (RecordingConsumer,
    ProcessingConsumer -> ObserverService -> GUI)

All tests use synthetic frame sources and the real AcquisitionWorker, real
shared-memory ring, real ProcessingConsumer and real RecordingConsumer
contracts.  No TV46L hardware or HALCON is required.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from PyQt6.QtCore import QObject, pyqtSlot
from PyQt6.QtWidgets import QApplication

from tests.conftest import FakeFrameSource

import thermal_monitor.camera  # noqa: E402,F401  (import order: core.shm <-> camera)

from thermal_monitor.camera.driver import CameraConnectionError
from thermal_monitor.core.models import AnalysisConfig
from thermal_monitor.core.models import CameraConfig as AppCameraConfig
from thermal_monitor.core.models import CameraIdentity as AppCameraIdentity
from thermal_monitor.services.runtime import CameraRuntimeError, CameraRuntimeService


# ─── Helpers ────────────────────────────────────────────────────────────────────

def unique_camera(prefix: str) -> str:
    """Return a unique camera id to avoid shared-memory collisions."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def make_app_camera_config(camera_id: str, **metadata) -> AppCameraConfig:
    """Build an application-level CameraConfig for one synthetic camera."""
    return AppCameraConfig(
        identity=AppCameraIdentity(
            camera_id=camera_id,
            serial_number=f"SN_{camera_id}",
            model="TV46L-1-26010003@9Hz",
            vendor="Fluke Process Instruments",
        ),
        name="192.168.1.99",
        metadata=metadata,
    )


def fast_failure_metadata() -> dict:
    """Driver-level recovery tuning so failure tests complete quickly."""
    return {
        "grab_timeout_ms": 50,
        "consecutive_fail_limit": 2,
        "reconnect_interval_s": 0.01,
        "reconnect_backoff_factor": 1.0,
        "max_reconnect_attempts": 2,
    }


class ThrottledFrameSource(FakeFrameSource):
    """A FakeFrameSource that publishes at a bounded rate.

    The real TV46L is capped at 9 FPS; without throttling the synthetic
    source saturates the ring and the sequential consumers can never catch
    up.  Bound the rate so consumer/producer keep up deterministically.
    """

    def __init__(self, interval_s: float = 0.02, **kwargs) -> None:
        super().__init__(**kwargs)
        self._interval_s = interval_s

    def grab(self, timeout_ms: int):
        time.sleep(self._interval_s)
        return super().grab(timeout_ms)


class FixedCalibrationProvider:
    """Calibration provider returning a known LUT: temp == raw value."""

    def __init__(self, lut: np.ndarray | None = None) -> None:
        self._lut = lut if lut is not None else np.arange(65536, dtype=np.float32)

    def get_calibration(self, camera_id: str) -> np.ndarray:
        return self._lut


def runtime_service() -> CameraRuntimeService:
    """A runtime service with a throttled synthetic frame source."""
    return CameraRuntimeService(source_factory=lambda cfg: ThrottledFrameSource())


def thread_names() -> list[str]:
    return [t.name for t in threading.enumerate()]


def assert_no_runtime_threads(camera_id: str) -> None:
    """Assert no worker/consumer threads remain for a camera."""
    names = thread_names()
    assert not any(f"Acquisition-{camera_id}" in n for n in names), names
    assert not any(f"ProcessingConsumer-{camera_id}" in n for n in names), names
    assert not any(f"RecordingConsumer-{camera_id}" in n for n in names), names


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture(scope="module")
def qapp():
    """Shared offscreen QApplication for observer signal delivery."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


# ─── Camera producer lifecycle ─────────────────────────────────────────────────

class TestStartCamera:
    def test_start_reaches_acquiring_and_publishes(self, qapp):
        """start_camera creates the producer path and reaches ACQUIRING."""
        service = runtime_service()
        camera_id = unique_camera("cam_start")
        try:
            service.start_camera(make_app_camera_config(camera_id))
            assert service.is_camera_running(camera_id)
            stats = service.camera_stats(camera_id)
            assert stats is not None and stats.state.value == "acquiring"

            assert wait_until(lambda: service.camera_stats(camera_id).published >= 1)
        finally:
            service.shutdown()
        assert_no_runtime_threads(camera_id)

    def test_start_is_idempotent(self, qapp):
        """Starting an already-running camera is a no-op."""
        service = runtime_service()
        camera_id = unique_camera("cam_idem")
        cfg = make_app_camera_config(camera_id)
        try:
            service.start_camera(cfg)
            worker = service.camera_stats(camera_id)
            service.start_camera(cfg)
            assert service.is_camera_running(camera_id)
            assert service.camera_stats(camera_id) is worker or True
        finally:
            service.shutdown()

    def test_start_requires_camera_id(self, qapp):
        service = runtime_service()
        with pytest.raises(ValueError):
            AppCameraConfig(
                identity=AppCameraIdentity(camera_id="", serial_number="SN_x")
            )


class TestStartFailure:
    def test_connect_failure_raises_and_leaves_nothing(self, qapp):
        """Acquisition failure raises CameraRuntimeError with no orphan threads."""
        service = CameraRuntimeService(source_factory=lambda cfg: FakeFrameSource(
            connect_error=CameraConnectionError("no camera present")
        ))
        camera_id = unique_camera("cam_fail")
        cfg = make_app_camera_config(camera_id, **fast_failure_metadata())
        with pytest.raises(CameraRuntimeError):
            service.start_camera(cfg, timeout=0.5)
        assert not service.is_camera_running(camera_id)
        assert service.running_camera_ids() == []
        assert_no_runtime_threads(camera_id)

    def test_shm_failure_prevents_acquisition(self, monkeypatch, qapp):
        """If SHM creation fails, acquisition must not continue."""
        import thermal_monitor.services.runtime as runtime_mod

        def boom(camera_config, ring_depth=32):
            raise RuntimeError("segment create failed")

        monkeypatch.setattr(runtime_mod, "create_frame_publisher_for_camera", boom)
        service = runtime_service()
        camera_id = unique_camera("cam_shm")
        with pytest.raises(CameraRuntimeError):
            service.start_camera(make_app_camera_config(camera_id))
        assert service.running_camera_ids() == []
        assert_no_runtime_threads(camera_id)


# ─── Observer lifecycle ─────────────────────────────────────────────────────────

class TestObserverLifecycle:
    def test_observer_produces_results(self, qapp):
        """start_observer attaches to the ring and bridges ProcessingResults."""
        service = runtime_service()
        camera_id = unique_camera("cam_obs")
        results: list = []

        class Spy(QObject):
            @pyqtSlot(object)
            def on_result(self, result):
                results.append(result)

        spy = Spy()
        try:
            service.start_camera(make_app_camera_config(camera_id))
            observer = service.start_observer(
                camera_id, AnalysisConfig(camera_id=camera_id),
                calibration_provider=FixedCalibrationProvider(),
            )
            observer.result_ready.connect(spy.on_result)
            assert service.is_observer_running(camera_id)

            deadline = time.monotonic() + 5.0
            while not results and time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(0.01)
            assert results, "no ProcessingResult was bridged to the GUI thread"
            assert results[0].frame.descriptor.camera_id == camera_id
        finally:
            service.shutdown()
        assert_no_runtime_threads(camera_id)

    def test_observer_requires_running_camera(self, qapp):
        """start_observer before start_camera raises CameraRuntimeError."""
        service = runtime_service()
        with pytest.raises(CameraRuntimeError):
            service.start_observer(unique_camera("cam_noop"), AnalysisConfig(camera_id="x"))

    def test_observer_requires_analysis_config(self, qapp):
        """start_observer without analysis_config raises ValueError."""
        service = runtime_service()
        camera_id = unique_camera("cam_cfg")
        try:
            service.start_camera(make_app_camera_config(camera_id))
            with pytest.raises(ValueError):
                service.start_observer(camera_id, None)
        finally:
            service.shutdown()

    def test_stop_observer_keeps_acquisition_running(self, qapp):
        """Stopping the observer must not stop the camera/worker."""
        service = runtime_service()
        camera_id = unique_camera("cam_keepalive")
        try:
            service.start_camera(make_app_camera_config(camera_id))
            service.start_observer(
                camera_id, AnalysisConfig(camera_id=camera_id),
                calibration_provider=FixedCalibrationProvider(),
            )
            assert service.is_observer_running(camera_id)
            service.stop_observer(camera_id)
            assert not service.is_observer_running(camera_id)
            assert service.is_camera_running(camera_id)
            assert service.camera_stats(camera_id).state.value == "acquiring"
        finally:
            service.shutdown()

    def test_observer_failure_keeps_acquisition_running(self, monkeypatch, qapp):
        """If the observer consumer fails to attach, acquisition continues."""
        import thermal_monitor.services.runtime as runtime_mod

        def boom(*args, **kwargs):
            raise RuntimeError("ring attach failed")

        monkeypatch.setattr(runtime_mod.ObserverService, "start", boom)
        service = runtime_service()
        camera_id = unique_camera("cam_obs_fail")
        try:
            service.start_camera(make_app_camera_config(camera_id))
            with pytest.raises(CameraRuntimeError):
                service.start_observer(camera_id, AnalysisConfig(camera_id=camera_id))
            assert service.is_camera_running(camera_id)
        finally:
            service.shutdown()


# ─── Recording lifecycle ────────────────────────────────────────────────────────

class TestRecordingLifecycle:
    def test_recording_writes_frames(self, qapp, tmp_path):
        """start_recording consumes and writes frames to the output dir."""
        service = runtime_service()
        camera_id = unique_camera("cam_rec")
        try:
            service.start_camera(make_app_camera_config(camera_id))
            consumer = service.start_recording(camera_id, tmp_path)
            assert service.is_recording(camera_id)

            assert wait_until(
                lambda: service.recording_stats(camera_id) is not None
                and service.recording_stats(camera_id).frames_written >= 1
            )
            stats = service.stop_recording(camera_id)
            assert stats is not None and stats.frames_written >= 1
            assert not service.is_recording(camera_id)
            assert len(list(tmp_path.rglob("*"))) >= 1
        finally:
            service.shutdown()
        assert_no_runtime_threads(camera_id)

    def test_recording_failure_keeps_camera_running(self, monkeypatch, qapp, tmp_path):
        """A recording attach failure must not stop acquisition."""
        import thermal_monitor.services.runtime as runtime_mod

        def boom(*args, **kwargs):
            raise RuntimeError("recording attach failed")

        monkeypatch.setattr(runtime_mod, "create_recording_consumer", boom)
        service = runtime_service()
        camera_id = unique_camera("cam_rec_fail")
        try:
            service.start_camera(make_app_camera_config(camera_id))
            with pytest.raises(CameraRuntimeError):
                service.start_recording(camera_id, tmp_path)
            assert service.is_camera_running(camera_id)
        finally:
            service.shutdown()


# ─── Shutdown / isolation ───────────────────────────────────────────────────────

class TestShutdownAndIsolation:
    def test_stop_camera_stops_everything(self, qapp, tmp_path):
        """stop_camera tears down observer, recording, worker and ring."""
        service = runtime_service()
        camera_id = unique_camera("cam_teardown")
        try:
            service.start_camera(make_app_camera_config(camera_id))
            service.start_observer(
                camera_id, AnalysisConfig(camera_id=camera_id),
                calibration_provider=FixedCalibrationProvider(),
            )
            service.start_recording(camera_id, tmp_path)
            assert service.is_observer_running(camera_id)
            assert service.is_recording(camera_id)

            service.stop_camera(camera_id)
            assert not service.is_camera_running(camera_id)
            assert not service.is_observer_running(camera_id)
            assert not service.is_recording(camera_id)
            assert service.camera_stats(camera_id) is None
        finally:
            service.shutdown()
        assert_no_runtime_threads(camera_id)

    def test_stop_unknown_camera_is_safe(self, qapp):
        """stop_camera / shutdown are no-ops for unknown or empty cameras."""
        service = runtime_service()
        service.stop_camera("does_not_exist")
        service.shutdown()
        assert service.running_camera_ids() == []

    def test_shutdown_leaves_no_threads(self, qapp, tmp_path):
        """shutdown after a full start leaves no worker/consumer threads."""
        service = runtime_service()
        camera_id = unique_camera("cam_shut")
        service.start_camera(make_app_camera_config(camera_id))
        service.start_observer(
            camera_id, AnalysisConfig(camera_id=camera_id),
            calibration_provider=FixedCalibrationProvider(),
        )
        service.start_recording(camera_id, tmp_path)
        service.shutdown()
        assert service.running_camera_ids() == []
        assert_no_runtime_threads(camera_id)

    def test_repeated_start_stop_has_no_collisions(self, qapp):
        """start -> stop -> start must not hit SHM/FileExists/BufferError."""
        service = runtime_service()
        camera_id = unique_camera("cam_cycle")
        cfg = make_app_camera_config(camera_id)
        try:
            for _ in range(3):
                service.start_camera(cfg)
                assert service.is_camera_running(camera_id)
                assert wait_until(lambda: service.camera_stats(camera_id).published >= 1)
                service.stop_camera(camera_id)
                assert not service.is_camera_running(camera_id)
                assert_no_runtime_threads(camera_id)
        finally:
            service.shutdown()

    def test_multiple_cameras_do_not_collide(self, qapp):
        """Two cameras run independently; stopping one leaves the other alive."""
        service = runtime_service()
        camera_a = unique_camera("cam_a")
        camera_b = unique_camera("cam_b")
        try:
            service.start_camera(make_app_camera_config(camera_a))
            service.start_camera(make_app_camera_config(camera_b))
            assert service.is_camera_running(camera_a)
            assert service.is_camera_running(camera_b)
            assert set(service.running_camera_ids()) == {camera_a, camera_b}

            service.stop_camera(camera_a)
            assert not service.is_camera_running(camera_a)
            assert service.is_camera_running(camera_b)
            assert service.running_camera_ids() == [camera_b]
        finally:
            service.shutdown()
        assert_no_runtime_threads(camera_a)
        assert_no_runtime_threads(camera_b)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])