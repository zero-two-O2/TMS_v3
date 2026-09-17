"""ptz.service -- application-facing PTZ service (Phase 5).

Owns camera->PTZ bindings, per-PTZ controllers over one shared
``OpcUaSession``, and a single background status monitor. Public API is
blocking-but-bounded and thread-safe; UI layers must invoke it from a
worker thread (a future QThread host), never the GUI thread. No Qt, no
camera, no database imports.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field as dc_field
from typing import Callable, Optional

from thermal_monitor.ptz.client import OpcUaSession
from thermal_monitor.ptz.controller import (
    PtzCommandError,
    PtzController,
    PtzOperation,
)
from thermal_monitor.ptz.errors import (
    PtzError,
    PtzErrorCategory,
    PtzValidationError,
)
from thermal_monitor.ptz.mapping import PtzMapping
from thermal_monitor.ptz.models import (
    PtzLimits,
    PtzStationBinding,
    PtzTolerance,
    VelocityMode,
)
from thermal_monitor.ptz.state import PlcConnectionState, PtzStatus

logger = logging.getLogger(__name__)

StatusListener = Callable[[str, PtzStatus], None]
"""``(ptz_id, status)`` snapshot delivery; exceptions are contained."""


@dataclass(frozen=True, slots=True)
class PtzServiceConfig:
    """Service-wide policy. Timeouts are bounded and deployment-tunable."""

    limits: Optional[PtzLimits] = None
    tolerance: PtzTolerance = dc_field(
        default_factory=lambda: PtzTolerance(pan=0.1, tilt=0.1)
    )
    poll_interval_s: float = 0.05
    move_timeout_s: float = 30.0
    calibration_timeout_s: float = 60.0
    monitor_interval_s: float = 0.5
    stale_threshold_s: float = 2.0

    def __post_init__(self) -> None:
        for name in (
            "poll_interval_s",
            "move_timeout_s",
            "calibration_timeout_s",
            "monitor_interval_s",
            "stale_threshold_s",
        ):
            value = float(getattr(self, name))
            if not value > 0:
                raise PtzValidationError(f"{name} must be > 0")
            object.__setattr__(self, name, value)


class PtzService:
    """Camera-associated PTZ access over a shared OPC UA session."""

    def __init__(
        self,
        session: OpcUaSession,
        mapping: PtzMapping,
        config: Optional[PtzServiceConfig] = None,
    ) -> None:
        self._session = session
        self._mapping = mapping
        self._config = config or PtzServiceConfig()
        self._lock = threading.RLock()
        self._bindings: dict[str, PtzStationBinding] = {}
        self._controllers: dict[str, PtzController] = {}
        self._listeners: list[StatusListener] = []
        self._cached: dict[str, tuple[float, PtzStatus]] = {}
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()
        self._generation = 0
        self._shutdown = False

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        """Establish the OPC UA session (blocking, bounded). Call from a
        worker thread, never the GUI thread."""
        self._ensure_usable()
        self._session.connect()

    def disconnect(self) -> None:
        """Release the session; monitor keeps serving degraded cache."""
        self._session.disconnect()

    def shutdown(self, timeout_s: float = 5.0) -> None:
        """Idempotent deterministic shutdown: stop the monitor (bounded
        join, no callbacks afterwards), cancel tracked operations, and
        disconnect the session. The transport itself stays owned by its
        creator."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            self._generation += 1
        self._stop_monitor(timeout_s=timeout_s)
        with self._lock:
            controllers = list(self._controllers.values())
        for controller in controllers:
            try:
                controller._cancel_active("service shutdown")
            except Exception:
                pass
        try:
            self._session.disconnect()
        except Exception:
            pass

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._shutdown

    def _ensure_usable(self) -> None:
        with self._lock:
            if self._shutdown:
                raise PtzCommandError(
                    PtzError(
                        code="service:shut-down",
                        message="PTZ service is shut down",
                        category=PtzErrorCategory.UNKNOWN,
                    )
                )

    # -- bindings ------------------------------------------------------------

    def register_binding(self, binding: PtzStationBinding) -> None:
        """Register camera->PTZ. A conflicting re-registration fails;
        an identical one is idempotent."""
        self._ensure_usable()
        with self._lock:
            existing = self._bindings.get(binding.camera_id)
            if existing is not None:
                if existing.ptz_id != binding.ptz_id:
                    raise PtzCommandError(
                        PtzError(
                            code=f"service:binding:conflict:{binding.camera_id}",
                            message=(
                                f"Camera {binding.camera_id} already bound to "
                                f"{existing.ptz_id}; cannot rebind to "
                                f"{binding.ptz_id}"
                            ),
                            category=PtzErrorCategory.COMMAND_REJECTED,
                        )
                    )
                return
            self._bindings[binding.camera_id] = binding
            if binding.ptz_id not in self._controllers:
                self._controllers[binding.ptz_id] = PtzController(
                    binding.ptz_id,
                    self._session,
                    self._mapping,
                    limits=self._config.limits,
                    tolerance=self._config.tolerance,
                    poll_interval_s=self._config.poll_interval_s,
                    move_timeout_s=self._config.move_timeout_s,
                    calibration_timeout_s=self._config.calibration_timeout_s,
                )

    def binding_for_camera(self, camera_id: str) -> PtzStationBinding:
        with self._lock:
            try:
                return self._bindings[camera_id]
            except KeyError as exc:
                raise PtzCommandError(
                    PtzError(
                        code=f"service:binding:unknown:{camera_id}",
                        message=f"No PTZ binding for camera {camera_id!r}; "
                        "missing bindings never default to another PTZ",
                        category=PtzErrorCategory.COMMAND_REJECTED,
                    )
                ) from exc

    def controller_for_camera(self, camera_id: str) -> PtzController:
        binding = self.binding_for_camera(camera_id)
        with self._lock:
            return self._controllers[binding.ptz_id]

    # -- public operations (camera-addressed) ----------------------------------

    def get_status(self, camera_id: str) -> PtzStatus:
        self._ensure_usable()
        return self.controller_for_camera(camera_id).read_status()

    def is_ready(self, camera_id: str) -> bool:
        try:
            return self.get_status(camera_id).accepts_commands
        except PtzCommandError:
            return False

    def move_absolute(
        self,
        camera_id: str,
        pan: float,
        tilt: float,
        *,
        velocity: Optional[float] = None,
        pan_velocity: Optional[float] = None,
        tilt_velocity: Optional[float] = None,
        velocity_mode: VelocityMode = VelocityMode.SINGLE,
        request_id: str = "",
        wait: bool = True,
        timeout_s: Optional[float] = None,
    ) -> PtzOperation:
        self._ensure_usable()
        return self.controller_for_camera(camera_id).move_absolute(
            pan,
            tilt,
            velocity=velocity,
            pan_velocity=pan_velocity,
            tilt_velocity=tilt_velocity,
            velocity_mode=velocity_mode,
            request_id=request_id,
            wait=wait,
            timeout_s=timeout_s,
        )

    def move_relative(
        self,
        camera_id: str,
        delta_pan: float,
        delta_tilt: float,
        *,
        velocity: Optional[float] = None,
        pan_velocity: Optional[float] = None,
        tilt_velocity: Optional[float] = None,
        velocity_mode: VelocityMode = VelocityMode.SINGLE,
        request_id: str = "",
        wait: bool = True,
        timeout_s: Optional[float] = None,
    ) -> PtzOperation:
        self._ensure_usable()
        return self.controller_for_camera(camera_id).move_relative(
            delta_pan,
            delta_tilt,
            velocity=velocity,
            pan_velocity=pan_velocity,
            tilt_velocity=tilt_velocity,
            velocity_mode=velocity_mode,
            request_id=request_id,
            wait=wait,
            timeout_s=timeout_s,
        )

    def stop(self, camera_id: str, *, timeout_s: float = 5.0) -> PtzOperation:
        self._ensure_usable()
        return self.controller_for_camera(camera_id).stop(timeout_s=timeout_s)

    def clear_error(self, camera_id: str, *, timeout_s: float = 5.0) -> PtzStatus:
        self._ensure_usable()
        return self.controller_for_camera(camera_id).clear_error(
            timeout_s=timeout_s
        )

    def request_calibration(
        self,
        camera_id: str,
        *,
        wait: bool = True,
        timeout_s: Optional[float] = None,
    ) -> PtzStatus:
        self._ensure_usable()
        return self.controller_for_camera(camera_id).request_calibration(
            wait=wait, timeout_s=timeout_s
        )

    def wait_until_reached(
        self,
        camera_id: str,
        operation_id: str,
        timeout_s: float,
    ) -> PtzOperation:
        self._ensure_usable()
        return self.controller_for_camera(camera_id).wait_for_operation(
            operation_id, timeout_s
        )

    # -- listeners + monitor -----------------------------------------------------

    def add_status_listener(self, listener: StatusListener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def remove_status_listener(self, listener: StatusListener) -> None:
        with self._lock:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

    def start_monitoring(self) -> None:
        """Start the single status monitor (idempotent). Exactly one
        monitoring loop exists; dispatch paths read live and never
        depend on it."""
        self._ensure_usable()
        with self._lock:
            existing = self._monitor_thread
            if existing is not None and existing.is_alive():
                return
            self._monitor_stop.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="PtzStatusMonitor",
                daemon=True,
            )
        self._monitor_thread.start()

    def stop_monitoring(self, timeout_s: float = 5.0) -> None:
        self._stop_monitor(timeout_s=timeout_s)

    def _stop_monitor(self, timeout_s: float = 5.0) -> None:
        with self._lock:
            thread = self._monitor_thread
        if thread is None:
            return
        self._monitor_stop.set()
        if threading.get_ident() == thread.ident:
            return
        thread.join(timeout=max(0.0, timeout_s))
        if thread.is_alive():
            logger.warning("[PTZ-SVC] monitor did not stop in time")

    def cached_status(self, camera_id: str) -> Optional[PtzStatus]:
        """Last monitor snapshot (may be stale); live reads stay with
        the controller paths."""
        binding = self.binding_for_camera(camera_id)
        with self._lock:
            entry = self._cached.get(binding.ptz_id)
        return entry[1] if entry is not None else None

    def _monitor_loop(self) -> None:
        with self._lock:
            generation = self._generation
        while not self._monitor_stop.is_set():
            with self._lock:
                controllers = list(self._controllers.values())
            for controller in controllers:
                if self._monitor_stop.is_set():
                    return
                try:
                    status = controller.read_status()
                except Exception:
                    continue
                fresh = status.communication_ok
                if not fresh:
                    with self._lock:
                        previous = self._cached.get(controller.ptz_id)
                    if previous is not None and (
                        time.monotonic() - previous[0]
                        > self._config.stale_threshold_s
                    ):
                        status = PtzStatus(
                            plc_state=self._session.state,
                            ptz_available=False,
                            communication_ok=False,
                            ready=False,
                            movement=previous[1].movement,
                            actual_pan=previous[1].actual_pan,
                            actual_tilt=previous[1].actual_tilt,
                            calibration=previous[1].calibration,
                            error=previous[1].error,
                        )
                    else:
                        continue
                with self._lock:
                    if self._shutdown or generation != self._generation:
                        return  # no callbacks after shutdown
                    self._cached[controller.ptz_id] = (
                        time.monotonic(),
                        status,
                    )
                    listeners = list(self._listeners)
                for listener in listeners:
                    try:
                        listener(controller.ptz_id, status)
                    except Exception:
                        logger.exception(
                            "[PTZ-SVC] status listener failed"
                        )
            self._monitor_stop.wait(self._config.monitor_interval_s)


__all__ = ["PtzService", "PtzServiceConfig", "StatusListener"]
