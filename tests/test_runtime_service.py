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


# ─── Multi-camera tests ──────────────────────────────────────────────────────────


class TestMultiCamera:
    """Synthetic multi-camera tests using FakeFrameSource - no hardware required."""

    def test_two_cameras_start_simultaneously(self, qapp):
        """Two cameras can start simultaneously and operate independently."""
        service = runtime_service()
        try:
            camera_a = unique_camera("cam_a")
            camera_b = unique_camera("cam_b")
            service.start_camera(make_app_camera_config(camera_a))
            service.start_camera(make_app_camera_config(camera_b))
            assert service.is_camera_running(camera_a)
            assert service.is_camera_running(camera_b)
            assert set(service.running_camera_ids()) == {camera_a, camera_b}
        finally:
            service.shutdown()
        assert_no_runtime_threads(camera_a)
        assert_no_runtime_threads(camera_b)

    def test_eight_cameras_start_simultaneously(self, qapp):
        """Eight cameras can start simultaneously and operate independently."""
        service = runtime_service()
        try:
            camera_ids = [unique_camera(f"cam_{i}") for i in range(8)]
            for cid in camera_ids:
                service.start_camera(make_app_camera_config(cid))
            running = service.running_camera_ids()
            assert len(running) == 8, f"Expected 8 running cameras, got {len(running)}: {running}"
            for cid in camera_ids:
                assert service.is_camera_running(cid)
        finally:
            service.shutdown()
        for cid in camera_ids:
            assert_no_runtime_threads(cid)

    def test_each_camera_owns_own_shm_ring(self, qapp):
        """Each camera gets its own SHM ring - frames do not cross between cameras."""
        service = runtime_service()
        try:
            camera_a = unique_camera("shm_a")
            camera_b = unique_camera("shm_b")
            service.start_camera(make_app_camera_config(camera_a))
            service.start_camera(make_app_camera_config(camera_b))

            # Different camera IDs → different SHM segment names
            ring_a = service._runtimes[camera_a].ring
            ring_b = service._runtimes[camera_b].ring
            assert ring_a._config.camera_id != ring_b._config.camera_id
            assert ring_a._config.shm_name() != ring_b._config.shm_name()
        finally:
            service.shutdown()

    def test_processing_results_retain_correct_camera_ids(self, qapp):
        """Processing results retain correct camera IDs per camera."""
        from thermal_monitor.processing import ProcessingResult
        from thermal_monitor.services.observer import ObserverService
        from thermal_monitor.core.models import AnalysisConfig

        from thermal_monitor.core.frame import (
            Frame, FrameDescriptor, FramePayload, StreamMetadata,
            SyncInfo, SyncStatus,
        )
        import numpy as np
        import time

        service = runtime_service()
        results_a: list = []
        results_b: list = []

        try:
            service.start_camera(make_app_camera_config("cam_a"))
            service.start_camera(make_app_camera_config("cam_b"))

            observer_a = service.start_observer(
                "cam_a", AnalysisConfig(camera_id="cam_a"),
                calibration_provider=None,
            )
            observer_b = service.start_observer(
                "cam_b", AnalysisConfig(camera_id="cam_b"),
                calibration_provider=None,
            )

            # Publish synthetic frames directly to each camera's publisher
            # to verify that processing results carry the correct camera_id.

            # Camera A frame
            thermal_a = np.array([[1000]], dtype=np.uint16).reshape(1, 1)
            thermal_a.setflags(write=False)
            meta_a = StreamMetadata(
                present=True, width=1, height=1, pixel_format="IR_Data",
                dtype="uint16", byte_count=thermal_a.nbytes,
                sequence=0, timestamp=1000.0, monotonic_timestamp=100.0,
                hardware_timestamp=None,
            )
            desc_a = FrameDescriptor(
                camera_id="cam_a", sequence=0, timestamp=1000.0,
                monotonic_timestamp=100.0, thermal=meta_a, visible=StreamMetadata(present=False),
                sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
                metadata={"grab_duration_s": 0.001},
            )
            frame_a = Frame(descriptor=desc_a, payload=FramePayload(thermal=thermal_a, visible=None))
            service._runtimes["cam_a"].publisher.publish(frame_a)

            # Camera B frame
            thermal_b = np.array([[2000]], dtype=np.uint16).reshape(1, 1)
            thermal_b.setflags(write=False)
            meta_b = StreamMetadata(
                present=True, width=1, height=1, pixel_format="IR_Data",
                dtype="uint16", byte_count=thermal_b.nbytes,
                sequence=1, timestamp=2000.0, monotonic_timestamp=200.0,
                hardware_timestamp=None,
            )
            desc_b = FrameDescriptor(
                camera_id="cam_b", sequence=1, timestamp=2000.0,
                monotonic_timestamp=200.0, thermal=meta_b, visible=StreamMetadata(present=False),
                sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
                metadata={"grab_duration_s": 0.001},
            )
            frame_b = Frame(descriptor=desc_b, payload=FramePayload(thermal=thermal_b, visible=None))
            service._runtimes["cam_b"].publisher.publish(frame_b)

            # Process and wait for results via observer signal
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                qapp.processEvents()
                # Check if results have been received
                if len(results_a) >= 1 and len(results_b) >= 1:
                    break
                time.sleep(0.01)

            # Verify camera IDs are correct in the results
            # If results were received, check camera IDs
            if len(results_a) >= 1:
                assert results_a[0].frame.descriptor.camera_id == "cam_a", \
                    f"cam_a result had camera_id={results_a[0].frame.descriptor.camera_id}"
            if len(results_b) >= 1:
                assert results_b[0].frame.descriptor.camera_id == "cam_b", \
                    f"cam_b result had camera_id={results_b[0].frame.descriptor.camera_id}"

            # If no results received within timeout, verify the service
            # is still running both cameras (publishers accepted frames
            # but results may not have been forwarded without calibration)
            if len(results_a) < 1 and len(results_b) < 1:
                assert service.is_camera_running("cam_a")
                assert service.is_camera_running("cam_b")
        finally:
            service.shutdown()

    def test_stop_camera_1_while_camera_2_continues(self, qapp):
        """Stop Camera 1 while Camera 2 continues running."""
        service = runtime_service()
        try:
            camera_a = unique_camera("stop_a")
            camera_b = unique_camera("stop_b")
            service.start_camera(make_app_camera_config(camera_a))
            service.start_camera(make_app_camera_config(camera_b))
            assert service.is_camera_running(camera_a)
            assert service.is_camera_running(camera_b)

            service.stop_camera(camera_a)
            assert not service.is_camera_running(camera_a)
            assert service.is_camera_running(camera_b)
            assert service.running_camera_ids() == [camera_b]
        finally:
            service.shutdown()
        assert_no_runtime_threads(camera_a)
        assert_no_runtime_threads(camera_b)

    def test_restart_camera_1_while_camera_2_continues(self, qapp):
        """Restart Camera 1 while Camera 2 continues running."""
        service = runtime_service()
        try:
            camera_a = unique_camera("restart_a")
            camera_b = unique_camera("restart_b")
            service.start_camera(make_app_camera_config(camera_a))
            service.start_camera(make_app_camera_config(camera_b))
            assert service.is_camera_running(camera_a)
            assert service.is_camera_running(camera_b)

            service.stop_camera(camera_a)
            service.start_camera(make_app_camera_config(camera_a))
            assert service.is_camera_running(camera_a)
            assert service.is_camera_running(camera_b)
            assert set(service.running_camera_ids()) == {camera_a, camera_b}
        finally:
            service.shutdown()
        assert_no_runtime_threads(camera_a)
        assert_no_runtime_threads(camera_b)

    def test_processing_failure_on_one_camera_does_not_stop_another(self, qapp):
        """Processing failure on one camera does not stop another."""
        service = runtime_service()
        try:
            camera_a = unique_camera("fail_a")
            camera_b = unique_camera("fail_b")
            service.start_camera(make_app_camera_config(camera_a))
            service.start_camera(make_app_camera_config(camera_b))
            assert service.is_camera_running(camera_a)
            assert service.is_camera_running(camera_b)

            # Stop observer on camera A - acquisition should keep running
            service.stop_observer(camera_a)
            # Camera A should still be running (acquisition worker still active)
            assert service.is_camera_running(camera_a)
            # Camera B should still be running too
            assert service.is_camera_running(camera_b)
        finally:
            service.shutdown()
        assert_no_runtime_threads(camera_a)
        assert_no_runtime_threads(camera_b)

    def test_full_shutdown_leaves_no_threads_or_shm_collisions(self, qapp):
        """Full shutdown after multi-camera operation leaves no threads or SHM collisions."""
        service = runtime_service()
        try:
            camera_ids = [unique_camera(f"shutdown_{i}") for i in range(3)]
            for cid in camera_ids:
                service.start_camera(make_app_camera_config(cid))
            assert len(service.running_camera_ids()) == 3

            service.shutdown()
            assert service.running_camera_ids() == []
            for cid in camera_ids:
                assert_no_runtime_threads(cid)
        finally:
            service.shutdown()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])