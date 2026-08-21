"""
services.runtime -- application-level lifecycle controller for camera runtimes.

Stage 7F: this is the single app-level owner of the Observer-mode producer
path.  For each running camera it owns, in order:

    TV46LDriver (FrameSource) -> AcquisitionWorker
        -> SharedMemoryRingBuffer + SharedMemoryPublisher
        -> optional RecordingConsumer (services.recording_consumer)
        -> optional ObserverService (ProcessingConsumer bridge to the GUI)

The GUI never touches the camera driver, HALCON, the acquisition loop, the
shared-memory ring or the recording writer directly.  It requests lifecycle
operations here (start/stop camera, observer, recording) and reads back
state via the accessor methods.  This module therefore has no PyQt6
dependency.

Deterministic ordering is guaranteed:

* startup:  create ring + publisher -> create source -> create worker ->
  start worker -> verify ACQUIRING -> (optionally) recording -> observer.
* shutdown: stop observer -> stop recording -> stop worker -> close ring.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from thermal_monitor.camera.acquisition import AcquisitionWorker, FramePublisher
from thermal_monitor.camera.driver import FrameSource, TV46LDriver
from thermal_monitor.camera.model import (
    AcquisitionState,
    AcquisitionStats,
    CameraConfig as DriverCameraConfig,
    CameraIdentity as DriverCameraIdentity,
)
from thermal_monitor.camera.shm import create_frame_publisher_for_camera
from thermal_monitor.core.models import AnalysisConfig, CameraConfig
from thermal_monitor.core.shm import SharedMemoryRingBuffer
from thermal_monitor.processing import CalibrationProvider
from thermal_monitor.processing.temperature import CachingCalibrationProvider
from thermal_monitor.services.observer import ObserverService
from thermal_monitor.services.recording_consumer import (
    RecordingConsumer,
    RecordingConsumerStats,
    create_recording_consumer,
)
from thermal_monitor.storage.recording import RecordingWriteMetadata

logger = logging.getLogger(__name__)


class CameraRuntimeError(RuntimeError):
    """Raised when a camera runtime lifecycle operation fails."""


# TV46L is a fixed 640x480 Mono16 camera (V2-validated).
_THERMAL_WIDTH = 640
_THERMAL_HEIGHT = 480
_RECORDING_CHUNK_BYTES = 64 * 1024 * 1024


def _int_meta(metadata: dict, key: str, default: int) -> int:
    value = metadata.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CameraRuntimeError(f"metadata[{key!r}] must be an integer, got {value!r}") from exc


def _float_meta(metadata: dict, key: str, default: float) -> float:
    value = metadata.get(key)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise CameraRuntimeError(f"metadata[{key!r}] must be a number, got {value!r}") from exc


def build_driver_config(camera_config: CameraConfig) -> DriverCameraConfig:
    """Map an application-level ``core.models.CameraConfig`` to a driver config.

    The application config deliberately does not carry HALCON/GVSP tuning
    (that lives in ``camera.model.CameraConfig``).  The mapping fills in the
    V2-validated defaults and lets the config's ``metadata`` mapping override
    any driver-level field.  The camera IP is stored in ``name`` by the UI;
    the HALCON device identifier defaults to ``"default"`` (auto-discovery)
    unless metadata explicitly provides it.
    """
    identity = camera_config.identity
    metadata = dict(camera_config.metadata or {})

    return DriverCameraConfig(
        identity=DriverCameraIdentity(
            camera_id=identity.camera_id,
            serial_number=identity.serial_number,
            model=identity.model,
            vendor=identity.vendor,
            firmware=identity.firmware,
            user_name=identity.user_name,
        ),
        device_identifier=str(metadata.get("device_identifier") or "default"),
        ip_address=str(metadata.get("ip_address") or camera_config.name or ""),
        frame_rate=_int_meta(metadata, "frame_rate", 9),
        grab_timeout_ms=_int_meta(metadata, "grab_timeout_ms", 500),
        socket_buffer_size=_int_meta(metadata, "socket_buffer_size", 1048576),
        num_buffers=_int_meta(metadata, "num_buffers", 8),
        stream_source_thermal=str(metadata.get("stream_source_thermal") or "IR_Data"),
        thermal_bits_per_channel=_int_meta(metadata, "thermal_bits_per_channel", 16),
        stream_source_visible=metadata.get("stream_source_visible"),
        visible_bits_per_channel=_int_meta(metadata, "visible_bits_per_channel", -1),
        consecutive_fail_limit=_int_meta(metadata, "consecutive_fail_limit", 3),
        reconnect_interval_s=_float_meta(metadata, "reconnect_interval_s", 3.0),
        reconnect_backoff_factor=_float_meta(metadata, "reconnect_backoff_factor", 2.0),
        max_reconnect_attempts=_int_meta(metadata, "max_reconnect_attempts", 10),
    )


@dataclass(slots=True)
class CameraRuntime:
    """Per-camera runtime objects owned by :class:`CameraRuntimeService`.

    A runtime exists for exactly one ``camera_id``.  The producer parts
    (ring, publisher, worker) are created together in ``start_camera`` and
    torn down together; consumers attach to the producer-owned ring and are
    stopped before the ring is closed.
    """

    camera_id: str
    driver_config: DriverCameraConfig
    ring: SharedMemoryRingBuffer
    publisher: FramePublisher
    worker: AcquisitionWorker
    observer: ObserverService | None = None
    recording: RecordingConsumer | None = None
    recording_ring: SharedMemoryRingBuffer | None = None
    recording_metadata: RecordingWriteMetadata | None = None

    def is_alive(self) -> bool:
        """True while the acquisition worker is not stopped or failed."""
        return self.worker.state not in (AcquisitionState.STOPPED, AcquisitionState.ERROR)


class CameraRuntimeService:
    """Application-level lifecycle controller for camera runtimes.

    The constructor is dependency-light: the default frame source is the real
    ``TV46LDriver`` (HALCON is only imported lazily inside its methods), and
    the default calibration provider is :class:`CachingCalibrationProvider`.
    Tests inject a synthetic ``source_factory`` and a fixed calibration
    provider; the controller itself never knows the difference.
    """

    def __init__(
        self,
        *,
        source_factory: Callable[[DriverCameraConfig], FrameSource] | None = None,
        calibration_provider: CalibrationProvider | None = None,
        ring_depth: int = 32,
        acquire_timeout_s: float = 10.0,
    ) -> None:
        self._source_factory = source_factory or (lambda cfg: TV46LDriver(cfg))
        self._calibration_provider = calibration_provider
        self._ring_depth = ring_depth
        self._acquire_timeout_s = acquire_timeout_s
        self._runtimes: dict[str, CameraRuntime] = {}
        self._lock = threading.RLock()

    @property
    def calibration_provider(self) -> CalibrationProvider:
        """The default calibration provider (created lazily on first use)."""
        if self._calibration_provider is None:
            self._calibration_provider = CachingCalibrationProvider()
        return self._calibration_provider

    # ─── Camera producer lifecycle ───────────────────────────────────────────

    def start_camera(
        self,
        camera_config: CameraConfig,
        *,
        timeout: float | None = None,
    ) -> str:
        """Start the full producer path for one camera and verify ACQUIRING.

        Idempotent: if the camera is already running, returns immediately.
        On any failure the partially-created resources are torn down and
        :class:`CameraRuntimeError` is raised, so no orphan threads or SHM
        segments are left behind.

        Raises:
            CameraRuntimeError: if the ring cannot be created, the source
                cannot be built, or the worker does not reach ACQUIRING.
        """
        timeout = self._acquire_timeout_s if timeout is None else timeout
        camera_id = camera_config.identity.camera_id
        if not camera_id:
            raise CameraRuntimeError("camera_config.identity.camera_id is required")
        if not camera_config.enabled:
            raise CameraRuntimeError(f"Camera {camera_id} is disabled")

        with self._lock:
            existing = self._runtimes.get(camera_id)
            if existing is not None:
                if existing.is_alive():
                    logger.info("Camera %s: already running", camera_id)
                    return camera_id
                self._stop_runtime_locked(existing, timeout=timeout)

            driver_config = build_driver_config(camera_config)

            try:
                ring, publisher = create_frame_publisher_for_camera(
                    driver_config, ring_depth=self._ring_depth
                )
            except Exception as exc:
                raise CameraRuntimeError(
                    f"SHM ring creation failed for camera {camera_id}: {exc}"
                ) from exc

            try:
                source = self._source_factory(driver_config)
            except Exception as exc:
                ring.close()
                raise CameraRuntimeError(
                    f"Failed to create frame source for camera {camera_id}: {exc}"
                ) from exc

            try:
                worker = AcquisitionWorker(camera_id, source, publisher, driver_config)
            except Exception as exc:
                ring.close()
                raise CameraRuntimeError(
                    f"Failed to build acquisition worker for camera {camera_id}: {exc}"
                ) from exc

            runtime = CameraRuntime(
                camera_id=camera_id,
                driver_config=driver_config,
                ring=ring,
                publisher=publisher,
                worker=worker,
            )
            self._runtimes[camera_id] = runtime

            try:
                worker.start()
            except Exception as exc:
                self._stop_runtime_locked(runtime, timeout=timeout)
                self._runtimes.pop(camera_id, None)
                raise CameraRuntimeError(
                    f"Failed to start acquisition for camera {camera_id}: {exc}"
                ) from exc

            if not worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=timeout):
                message = self._failure_message(runtime)
                self._stop_runtime_locked(runtime, timeout=timeout)
                self._runtimes.pop(camera_id, None)
                raise CameraRuntimeError(message)

            logger.info("Camera %s: runtime started", camera_id)
            return camera_id

    def stop_camera(self, camera_id: str, *, timeout: float = 5.0) -> None:
        """Stop the observer, recording, acquisition worker and ring for one camera.

        Deterministic teardown order: observer -> recording -> worker -> ring.
        Safe to call for an unknown or already-stopped camera.
        """
        with self._lock:
            runtime = self._runtimes.get(camera_id)
            if runtime is None:
                return
            self._stop_runtime_locked(runtime, timeout=timeout)
            self._runtimes.pop(camera_id, None)

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Stop every running camera runtime."""
        with self._lock:
            for camera_id in list(self._runtimes.keys()):
                self._stop_runtime_locked(self._runtimes[camera_id], timeout=timeout)
            self._runtimes.clear()

    def is_camera_running(self, camera_id: str) -> bool:
        with self._lock:
            runtime = self._runtimes.get(camera_id)
            return runtime is not None and runtime.is_alive()

    def running_camera_ids(self) -> list[str]:
        with self._lock:
            return [cid for cid, rt in self._runtimes.items() if rt.is_alive()]

    def camera_stats(self, camera_id: str) -> AcquisitionStats | None:
        """Latest AcquisitionStats for a camera, or None when not running."""
        with self._lock:
            runtime = self._runtimes.get(camera_id)
            if runtime is None:
                return None
            return runtime.worker.stats()

    # ─── Observer (consumer bridge to the GUI) ───────────────────────────────

    def start_observer(
        self,
        camera_id: str,
        analysis_config: AnalysisConfig | None,
        *,
        calibration_provider: CalibrationProvider | None = None,
    ) -> ObserverService:
        """Attach an ObserverService to a running camera's ring.

        The camera must already be running (``start_camera`` first).  The
        observer only consumes frames; stopping it does not stop acquisition.
        """
        if analysis_config is None:
            raise ValueError("analysis_config is required to start observer monitoring")

        with self._lock:
            runtime = self._require_running(camera_id)
            if runtime.observer is not None and runtime.observer.is_running:
                logger.warning("Camera %s: observer already running", camera_id)
                return runtime.observer

            service = ObserverService()
            try:
                service.start(
                    camera_id,
                    analysis_config=analysis_config,
                    calibration_provider=calibration_provider or self.calibration_provider,
                    ring_depth=self._ring_depth,
                    thermal_width=_THERMAL_WIDTH,
                    thermal_height=_THERMAL_HEIGHT,
                )
            except Exception as exc:
                raise CameraRuntimeError(
                    f"Failed to start observer for camera {camera_id}: {exc}"
                ) from exc

            runtime.observer = service
            return service

    def stop_observer(self, camera_id: str) -> None:
        """Stop the observer for a camera; acquisition keeps running."""
        with self._lock:
            runtime = self._runtimes.get(camera_id)
            if runtime is not None:
                self._stop_observer_locked(runtime)

    def is_observer_running(self, camera_id: str) -> bool:
        with self._lock:
            runtime = self._runtimes.get(camera_id)
            return bool(
                runtime is not None
                and runtime.observer is not None
                and runtime.observer.is_running
            )

    def observer_service(self, camera_id: str) -> ObserverService | None:
        """The active ObserverService for a camera, or None."""
        with self._lock:
            runtime = self._runtimes.get(camera_id)
            return runtime.observer if runtime is not None else None

    # ─── Optional recording ──────────────────────────────────────────────────

    def start_recording(
        self,
        camera_id: str,
        output_dir: str | Path | None = None,
        *,
        recording_id: str | None = None,
        trigger: str = "manual",
    ) -> RecordingConsumer:
        """Start a RecordingConsumer over the running camera's ring.

        The camera must already be running.  Recording is optional and
        independent: a recording failure never stops acquisition or the
        observer.
        """
        with self._lock:
            runtime = self._require_running(camera_id)
            if runtime.recording is not None:
                logger.warning("Camera %s: recording already active", camera_id)
                return runtime.recording

            output_dir = Path(output_dir) if output_dir is not None else Path("recordings")
            recording_id = recording_id or f"rec_{camera_id}_{int(time.time() * 1000)}"
            metadata = RecordingWriteMetadata(
                recording_id=recording_id,
                cameras=[camera_id],
                streams={camera_id: ["IR"]},
                trigger=trigger,
                camera_snapshots=(self._runtime_snapshot(runtime),),
            )

            try:
                ring, consumer = create_recording_consumer(
                    camera_id=camera_id,
                    output_dir=output_dir,
                    recording_metadata=metadata,
                    ring_depth=self._ring_depth,
                    chunk_target_bytes=_RECORDING_CHUNK_BYTES,
                )
            except Exception as exc:
                raise CameraRuntimeError(
                    f"Failed to attach recording consumer for camera {camera_id}: {exc}"
                ) from exc

            try:
                consumer.start()
            except Exception as exc:
                try:
                    ring.close()
                except Exception:
                    pass
                raise CameraRuntimeError(
                    f"Failed to start recording for camera {camera_id}: {exc}"
                ) from exc

            runtime.recording = consumer
            runtime.recording_ring = ring
            runtime.recording_metadata = metadata
            return consumer

    def stop_recording(self, camera_id: str) -> RecordingConsumerStats | None:
        """Stop and finalize the recording for a camera.

        Returns the final consumer stats, or None if no recording was active.
        """
        with self._lock:
            runtime = self._runtimes.get(camera_id)
            if runtime is None:
                return None
            return self._stop_recording_locked(runtime)

    def is_recording(self, camera_id: str) -> bool:
        with self._lock:
            runtime = self._runtimes.get(camera_id)
            return runtime is not None and runtime.recording is not None

    def recording_stats(self, camera_id: str) -> RecordingConsumerStats | None:
        with self._lock:
            runtime = self._runtimes.get(camera_id)
            if runtime is None or runtime.recording is None:
                return None
            return runtime.recording.stats()

    # ─── Internal helpers ────────────────────────────────────────────────────

    def _require_running(self, camera_id: str) -> CameraRuntime:
        runtime = self._runtimes.get(camera_id)
        if runtime is None:
            raise CameraRuntimeError(
                f"No running camera {camera_id}; call start_camera first"
            )
        if not runtime.is_alive():
            raise CameraRuntimeError(
                f"Camera {camera_id} is not running (state={runtime.worker.state.value})"
            )
        return runtime

    def _failure_message(self, runtime: CameraRuntime) -> str:
        stats = runtime.worker.stats()
        detail = stats.last_error or (
            f"did not reach ACQUIRING within {self._acquire_timeout_s:.1f}s"
        )
        return f"Acquisition failed for camera {runtime.camera_id}: {detail}"

    def _stop_runtime_locked(self, runtime: CameraRuntime, *, timeout: float) -> None:
        """Deterministic teardown for one runtime (caller holds the lock)."""
        self._stop_observer_locked(runtime)
        self._stop_recording_locked(runtime)
        try:
            runtime.worker.stop(timeout=timeout)
        except Exception:
            logger.exception("Camera %s: error stopping acquisition worker", runtime.camera_id)
        try:
            runtime.ring.close()
        except Exception:
            logger.exception("Camera %s: error closing ring", runtime.camera_id)

    def _stop_observer_locked(self, runtime: CameraRuntime) -> None:
        service = runtime.observer
        runtime.observer = None
        if service is not None:
            try:
                service.stop(timeout=5.0)
            except Exception:
                logger.exception("Camera %s: error stopping observer", runtime.camera_id)

    def _stop_recording_locked(
        self, runtime: CameraRuntime
    ) -> RecordingConsumerStats | None:
        consumer = runtime.recording
        ring = runtime.recording_ring
        runtime.recording = None
        runtime.recording_ring = None
        runtime.recording_metadata = None
        if consumer is None:
            return None
        stats = consumer.stats()
        try:
            consumer.stop(timeout=5.0)
        except Exception:
            logger.exception("Camera %s: error stopping recording consumer", runtime.camera_id)
        try:
            consumer.close()
        except Exception:
            pass
        if ring is not None:
            try:
                ring.close()
            except Exception:
                pass
        return stats

    def _runtime_snapshot(self, runtime: CameraRuntime) -> dict[str, object]:
        identity = runtime.driver_config.identity
        return {
            "camera_id": runtime.camera_id,
            "serial_number": identity.serial_number,
            "model": identity.model,
            "vendor": identity.vendor,
            "device_identifier": runtime.driver_config.device_identifier,
            "ip_address": runtime.driver_config.ip_address,
            "frame_rate": runtime.driver_config.frame_rate,
            "stream_source_thermal": runtime.driver_config.stream_source_thermal,
            "thermal_bits_per_channel": runtime.driver_config.thermal_bits_per_channel,
        }


__all__ = [
    "CameraRuntime",
    "CameraRuntimeError",
    "CameraRuntimeService",
    "build_driver_config",
]
