"""ptz.roi_activation -- safe Go To + ROI-set activation workflow (Phase 7).

Moves the PTZ to a saved position through ``PtzService`` and, only after
authoritative position-reached, validates and records the position's
ROI set as the camera's active context.

Explicit boundary: the running observer/processing pipeline pins its
``AnalysisConfig`` at start and acquisition never stamps
``position_id`` on frames, so no safe runtime retarget API exists yet.
This workflow therefore does NOT touch the observer. It validates the
ROI reference, resolves the applicable ROI set via the existing
``resolve_rois`` path, and records the outcome in
``ActivePositionRegistry`` -- the explicit seam Phase 8 will use to
retarget processing. The previously active context is kept on any
failure (rollback = change nothing).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Sequence

from thermal_monitor.core.models import ROIConfig
from thermal_monitor.core.roi_resolver import resolve_rois
from thermal_monitor.ptz.controller import PtzCommandError, PtzOperation, PtzOperationState
from thermal_monitor.ptz.errors import (
    PtzError,
    PtzErrorCategory,
    PtzStateError,
    PtzValidationError,
)
from thermal_monitor.ptz.positions import PtzPosition, check_position_binding

logger = logging.getLogger(__name__)


class RoiActivationState(str, Enum):
    """Explicit workflow states."""

    VALIDATING = "validating"
    MOVING = "moving"
    WAITING_FOR_REACHED = "waiting_for_reached"
    ACTIVATING_ROI = "activating_roi"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ActivePositionContext:
    """Recorded active context for one camera (registry seam for Phase 8)."""

    camera_id: str
    ptz_id: str
    position_id: str
    position_name: str
    roi_set_ref: str
    roi_ids: tuple = ()
    activated_monotonic: float = 0.0
    # Phase 8 contract fields (defaulted so older constructions keep working).
    context_generation: int = 0
    session_generation: int = 0
    activation_state: str = "active"
    operation_id: str = ""


@dataclass(frozen=True, slots=True)
class RoiActivationResult:
    """Final combined workflow outcome for the caller/UI."""

    state: RoiActivationState
    camera_id: str
    ptz_id: str
    position_id: str
    operation: Optional[PtzOperation] = None
    roi_ids: tuple = ()
    error: Optional[PtzError] = None
    elapsed_s: float = 0.0
    # True once the new context was read back from the processing path
    # (Phase 8 observer publish). False when no observer was attached.
    first_result_confirmed: bool = False


class ActivePositionRegistry:
    """Thread-safe camera -> active-position-context store.

    Updated only on fully validated COMPLETED workflows; failures and
    cancellations leave the previous context untouched. Readers always
    receive the stored immutable snapshot (never a live mutable view).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._contexts: dict[str, ActivePositionContext] = {}
        self._shutdown = False

    def set(
        self,
        context: ActivePositionContext,
        *,
        session_generation: int | None = None,
        current_session_generation: int | None = None,
    ) -> None:
        """Store a context. When both generations are given, a mismatch
        rejects the update instead of publishing stale state."""
        with self._lock:
            if self._shutdown:
                raise PtzStateError("ActivePositionRegistry is shut down")
            if (
                session_generation is not None
                and current_session_generation is not None
                and session_generation != current_session_generation
            ):
                raise PtzStateError(
                    f"Stale session generation {session_generation} != "
                    f"{current_session_generation}; context rejected"
                )
            self._contexts[context.camera_id] = context

    def get(self, camera_id: str) -> Optional[ActivePositionContext]:
        with self._lock:
            return self._contexts.get(camera_id)

    def clear(self, camera_id: str) -> None:
        with self._lock:
            self._contexts.pop(camera_id, None)

    def clear_all(self) -> None:
        with self._lock:
            self._contexts.clear()

    def shutdown(self) -> None:
        """Idempotent shutdown: drop all contexts, reject later writes."""
        with self._lock:
            self._shutdown = True
            self._contexts.clear()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._shutdown


def _fail(
    camera_id: str,
    ptz_id: str,
    position_id: str,
    error: PtzError,
    operation: Optional[PtzOperation],
    t0: float,
) -> RoiActivationResult:
    return RoiActivationResult(
        state=RoiActivationState.FAILED,
        camera_id=camera_id,
        ptz_id=ptz_id,
        position_id=position_id,
        operation=operation,
        error=error,
        elapsed_s=time.monotonic() - t0,
    )


class RoiActivationWorkflow:
    """Blocking, bounded Go To + ROI activation for one position.

    Runs on the calling (worker) thread; the UI wraps it in a daemon
    thread. Polls the service; never touches the observer.
    """

    def __init__(
        self,
        service,  # PtzService (structural use only; no import cycle)
        registry: Optional[ActivePositionRegistry] = None,
        *,
        poll_interval_s: float = 0.05,
    ) -> None:
        self._service = service
        self._registry = registry or ActivePositionRegistry()
        self._poll_interval_s = poll_interval_s

    @property
    def registry(self) -> ActivePositionRegistry:
        return self._registry

    def run(
        self,
        camera_id: str,
        position: PtzPosition,
        analysis_config_provider: Callable[[str], object | None],
        *,
        velocity: Optional[float] = None,
        timeout_s: float = 60.0,
        is_current: Optional[Callable[[], bool]] = None,
        cancel_event: Optional[threading.Event] = None,
        commit_context: bool = True,
    ) -> RoiActivationResult:
        """Execute VALIDATING -> MOVING -> WAITING -> ACTIVATING -> DONE.

        With ``commit_context=False`` the registry write is skipped and
        the caller (retarget coordinator) commits the full context after
        publishing it to the processing path.
        """
        t0 = time.monotonic()

        def _cancelled() -> bool:
            if cancel_event is not None and cancel_event.is_set():
                return True
            try:
                if is_current is not None and not is_current():
                    return True
            except Exception:
                return True
            return False

        def _cancelled_result(op: Optional[PtzOperation]) -> RoiActivationResult:
            return RoiActivationResult(
                state=RoiActivationState.CANCELLED,
                camera_id=camera_id,
                ptz_id=position.ptz_id,
                position_id=position.position_id,
                operation=op,
                error=PtzError(
                    code=f"{camera_id}:roi-activation:cancelled",
                    message="ROI activation cancelled",
                    category=PtzErrorCategory.UNKNOWN,
                ),
                elapsed_s=time.monotonic() - t0,
            )

        # -- VALIDATING ------------------------------------------------
        try:
            binding = self._service.binding_for_camera(camera_id)
        except PtzCommandError as exc:
            return _fail(
                camera_id, position.ptz_id, position.position_id,
                exc.error, None, t0,
            )
        try:
            check_position_binding(position, binding.ptz_id)
        except PtzValidationError as exc:
            return _fail(
                camera_id, binding.ptz_id, position.position_id,
                PtzError(
                    code=f"{camera_id}:roi-activation:binding-mismatch",
                    message=str(exc),
                    category=PtzErrorCategory.COMMAND_REJECTED,
                ),
                None, t0,
            )
        if _cancelled():
            return _cancelled_result(None)

        # -- MOVING (dispatch, non-blocking) ---------------------------
        try:
            if (
                position.pan_velocity is not None
                and position.tilt_velocity is not None
            ):
                operation = self._service.move_absolute(
                    camera_id, position.pan, position.tilt,
                    velocity_mode="per_axis",
                    pan_velocity=position.pan_velocity,
                    tilt_velocity=position.tilt_velocity,
                    wait=False,
                )
            else:
                operation = self._service.move_absolute(
                    camera_id, position.pan, position.tilt,
                    velocity=position.velocity
                    if position.velocity is not None
                    else velocity,
                    wait=False,
                )
        except PtzCommandError as exc:
            return _fail(
                camera_id, binding.ptz_id, position.position_id,
                exc.error, exc.operation, t0,
            )
        if _cancelled():
            try:
                self._service.stop(camera_id)
            except Exception:
                pass
            return _cancelled_result(operation)

        # -- WAITING_FOR_REACHED ----------------------------------------
        deadline = time.monotonic() + timeout_s
        final = operation
        while True:
            if _cancelled():
                try:
                    self._service.stop(camera_id)
                except Exception:
                    pass
                return _cancelled_result(final)
            try:
                final = self._service.wait_until_reached(
                    camera_id, operation.operation_id,
                    timeout_s=max(0.05, deadline - time.monotonic()),
                )
            except PtzCommandError as exc:
                if (
                    exc.error.category == PtzErrorCategory.MOVEMENT_TIMEOUT
                    and time.monotonic() < deadline
                ):
                    time.sleep(self._poll_interval_s)
                    continue
                return _fail(
                    camera_id, binding.ptz_id, position.position_id,
                    exc.error, exc.operation or final, t0,
                )
            break
        if final.state != PtzOperationState.REACHED:
            return _fail(
                camera_id, binding.ptz_id, position.position_id,
                final.error
                or PtzError(
                    code=f"{camera_id}:roi-activation:not-reached",
                    message=f"Movement ended in {final.state.value}",
                    category=PtzErrorCategory.MOVEMENT_TIMEOUT,
                ),
                final, t0,
            )
        if _cancelled():
            return _cancelled_result(final)

        # -- ACTIVATING_ROI (validate + resolve + record only) ---------
        try:
            analysis_config = analysis_config_provider(camera_id)
        except Exception as exc:
            return _fail(
                camera_id, binding.ptz_id, position.position_id,
                PtzError(
                    code=f"{camera_id}:roi-activation:config",
                    message=f"ROI config unavailable: {exc}",
                    category=PtzErrorCategory.UNKNOWN,
                ),
                final, t0,
            )
        roi_ref = position.roi_set_ref or ""
        if not roi_ref:
            roi_ids: tuple = ()
        else:
            if analysis_config is None:
                return _fail(
                    camera_id, binding.ptz_id, position.position_id,
                    PtzError(
                        code=f"{camera_id}:roi-activation:roi-ref",
                        message=(
                            f"ROI-set reference {roi_ref!r} cannot be "
                            "resolved: no analysis config"
                        ),
                        category=PtzErrorCategory.COMMAND_REJECTED,
                    ),
                    final, t0,
                )
            associations = getattr(
                analysis_config, "position_associations", {}
            ) or {}
            if roi_ref not in associations:
                return _fail(
                    camera_id, binding.ptz_id, position.position_id,
                    PtzError(
                        code=f"{camera_id}:roi-activation:roi-ref",
                        message=(
                            f"ROI-set reference {roi_ref!r} is not a known "
                            "position association"
                        ),
                        category=PtzErrorCategory.COMMAND_REJECTED,
                    ),
                    final, t0,
                )
            try:
                resolved: Sequence[ROIConfig] = resolve_rois(
                    analysis_config, camera_id, roi_ref
                )
            except Exception as exc:
                return _fail(
                    camera_id, binding.ptz_id, position.position_id,
                    PtzError(
                        code=f"{camera_id}:roi-activation:resolve",
                        message=f"ROI resolution failed: {exc}",
                        category=PtzErrorCategory.UNKNOWN,
                    ),
                    final, t0,
                )
            roi_ids = tuple(r.roi_id for r in resolved)
        # Registry update is the activation commit point: previous context
        # survives every failure above unchanged. The retarget coordinator
        # defers this commit until after the observer publish.
        if commit_context:
            self._registry.set(
                ActivePositionContext(
                    camera_id=camera_id,
                    ptz_id=binding.ptz_id,
                    position_id=position.position_id,
                    position_name=position.name,
                    roi_set_ref=roi_ref,
                    roi_ids=roi_ids,
                    activated_monotonic=time.monotonic(),
                )
            )
        logger.info(
            "[PTZ-ROI] activated cam=%s ptz=%s pos=%s rois=%s",
            camera_id, binding.ptz_id, position.position_id, list(roi_ids),
        )
        return RoiActivationResult(
            state=RoiActivationState.COMPLETED,
            camera_id=camera_id,
            ptz_id=binding.ptz_id,
            position_id=position.position_id,
            operation=final,
            roi_ids=roi_ids,
            elapsed_s=time.monotonic() - t0,
        )


__all__ = [
    "ActivePositionContext",
    "ActivePositionRegistry",
    "RoiActivationResult",
    "RoiActivationState",
    "RoiActivationWorkflow",
]
