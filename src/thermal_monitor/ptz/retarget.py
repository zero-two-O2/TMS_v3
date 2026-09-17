"""ptz.retarget -- observer retarget coordination (Phase 8).

Serializes retarget requests per camera (latest wins), runs the
existing ``RoiActivationWorkflow`` without committing its registry
write, then publishes the validated context to the processing path and
commits the registry exactly once. Failures, cancellation, generation
mismatch, or publish errors leave the previous context untouched.

The coordinator never touches acquisition, SHM, or the observer thread
directly: publishing goes through the observer's thread-safe
``set_active_position`` pass-through, and completion is confirmed by
reading the published tuple back.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from typing import Callable, Optional

from thermal_monitor.ptz.errors import PtzError, PtzErrorCategory
from thermal_monitor.ptz.positions import PtzPosition
from thermal_monitor.ptz.roi_activation import (
    ActivePositionContext,
    ActivePositionRegistry,
    RoiActivationResult,
    RoiActivationState,
    RoiActivationWorkflow,
)

logger = logging.getLogger(__name__)


class ObserverRetargetCoordinator:
    """Per-camera serialized Go To + observer publish + registry commit.

    Blocking and bounded; runs on the calling (worker) thread. At most
    one active request per camera: a newer request cancels the older
    one, which stops its motion and reports CANCELLED.
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
        self._lock = threading.RLock()
        self._active: dict[str, threading.Event] = {}
        self._generations: dict[str, int] = {}

    @property
    def registry(self) -> ActivePositionRegistry:
        return self._registry

    def cancel(self, camera_id: str) -> None:
        """Cancel the in-flight retarget for a camera, if any."""
        with self._lock:
            event = self._active.get(camera_id)
        if event is not None:
            event.set()

    def retarget(
        self,
        camera_id: str,
        position: PtzPosition,
        analysis_config_provider: Callable[[str], object | None],
        observer_provider: Callable[[], object | None],
        session_generation_provider: Callable[[], int],
        *,
        velocity: Optional[float] = None,
        timeout_s: float = 60.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> RoiActivationResult:
        """Move, validate, publish to the observer, commit the registry."""
        t0 = time.monotonic()
        mine = threading.Event()
        with self._lock:
            previous = self._active.get(camera_id)
            if previous is not None:
                previous.set()
            self._active[camera_id] = mine
        try:
            return self._run_guarded(
                t0, mine, cancel_event, camera_id, position,
                analysis_config_provider, observer_provider,
                session_generation_provider, velocity, timeout_s,
            )
        finally:
            with self._lock:
                if self._active.get(camera_id) is mine:
                    del self._active[camera_id]

    # -- internals ---------------------------------------------------------

    def _cancelled(
        self,
        mine: threading.Event,
        external: Optional[threading.Event],
        is_current: Callable[[], bool],
    ) -> bool:
        if mine.is_set():
            return True
        if external is not None and external.is_set():
            return True
        try:
            return not is_current()
        except Exception:
            return True

    def _run_guarded(
        self,
        t0: float,
        mine: threading.Event,
        external: Optional[threading.Event],
        camera_id: str,
        position: PtzPosition,
        analysis_config_provider: Callable[[str], object | None],
        observer_provider: Callable[[], object | None],
        session_generation_provider: Callable[[], int],
        velocity: Optional[float],
        timeout_s: float,
    ) -> RoiActivationResult:
        try:
            session_generation = session_generation_provider()
        except Exception:
            session_generation = -1

        def _is_current() -> bool:
            if mine.is_set():
                return False
            if external is not None and external.is_set():
                return False
            try:
                return session_generation_provider() == session_generation
            except Exception:
                return False

        workflow = RoiActivationWorkflow(
            self._service, self._registry,
            poll_interval_s=self._poll_interval_s,
        )
        result = workflow.run(
            camera_id,
            position,
            analysis_config_provider,
            velocity=velocity,
            timeout_s=timeout_s,
            is_current=_is_current,
            cancel_event=mine,
            commit_context=False,
        )
        if result.state != RoiActivationState.COMPLETED:
            return result
        if self._cancelled(mine, external, _is_current):
            return self._cancelled_result(
                t0, camera_id, position, result.operation
            )
        # -- publish to the processing path ------------------------------
        roi_ref = position.roi_set_ref or ""
        override_id = roi_ref or "default"
        with self._lock:
            context_generation = self._generations.get(camera_id, 0) + 1
        observer = observer_provider()
        if observer is None:
            # No observer attached (PTZ-only operation): registry commit
            # still records the validated context; nothing confirms frames.
            self._commit(
                camera_id, position, result, context_generation,
                session_generation,
            )
            with self._lock:
                self._generations[camera_id] = context_generation
            return result
        try:
            if getattr(observer, "camera_id", camera_id) != camera_id:
                raise RuntimeError("observer is attached to another camera")
            published = observer.set_active_position(
                override_id, context_generation
            )
            read_back = observer.active_position
        except Exception as exc:
            return RoiActivationResult(
                state=RoiActivationState.FAILED,
                camera_id=camera_id,
                ptz_id=result.ptz_id,
                position_id=position.position_id,
                operation=result.operation,
                roi_ids=result.roi_ids,
                error=PtzError(
                    code=f"{camera_id}:retarget:publish",
                    message=f"Observer publish failed: {exc}",
                    category=PtzErrorCategory.UNKNOWN,
                ),
                elapsed_s=time.monotonic() - t0,
            )
        if published != context_generation or read_back != (
            override_id, context_generation
        ):
            return RoiActivationResult(
                state=RoiActivationState.FAILED,
                camera_id=camera_id,
                ptz_id=result.ptz_id,
                position_id=position.position_id,
                operation=result.operation,
                roi_ids=result.roi_ids,
                error=PtzError(
                    code=f"{camera_id}:retarget:confirm",
                    message="Published context did not read back",
                    category=PtzErrorCategory.UNKNOWN,
                ),
                elapsed_s=time.monotonic() - t0,
            )
        if self._cancelled(mine, external, _is_current):
            return self._cancelled_result(
                t0, camera_id, position, result.operation
            )
        # -- commit (single atomic registry write, generation-checked) ---
        try:
            self._registry.set(
                ActivePositionContext(
                    camera_id=camera_id,
                    ptz_id=result.ptz_id,
                    position_id=position.position_id,
                    position_name=position.name,
                    roi_set_ref=roi_ref,
                    roi_ids=result.roi_ids,
                    activated_monotonic=time.monotonic(),
                    context_generation=context_generation,
                    session_generation=session_generation,
                    activation_state="active",
                    operation_id=(
                        result.operation.operation_id
                        if result.operation is not None
                        else ""
                    ),
                ),
                session_generation=session_generation,
                current_session_generation=session_generation_provider(),
            )
        except Exception as exc:
            return RoiActivationResult(
                state=RoiActivationState.FAILED,
                camera_id=camera_id,
                ptz_id=result.ptz_id,
                position_id=position.position_id,
                operation=result.operation,
                roi_ids=result.roi_ids,
                error=PtzError(
                    code=f"{camera_id}:retarget:commit",
                    message=f"Registry commit rejected: {exc}",
                    category=PtzErrorCategory.UNKNOWN,
                ),
                elapsed_s=time.monotonic() - t0,
            )
        with self._lock:
            self._generations[camera_id] = context_generation
        logger.info(
            "[PTZ-RETARGET] committed cam=%s pos=%s gen=%s",
            camera_id, position.position_id, context_generation,
        )
        return dataclasses.replace(
            result, first_result_confirmed=True,
            elapsed_s=time.monotonic() - t0,
        )

    def _commit(self, camera_id, position, result, context_generation,
                session_generation) -> None:
        self._registry.set(
            ActivePositionContext(
                camera_id=camera_id,
                ptz_id=result.ptz_id,
                position_id=position.position_id,
                position_name=position.name,
                roi_set_ref=position.roi_set_ref or "",
                roi_ids=result.roi_ids,
                activated_monotonic=time.monotonic(),
                context_generation=context_generation,
                session_generation=session_generation,
                activation_state="active",
                operation_id=(
                    result.operation.operation_id
                    if result.operation is not None
                    else ""
                ),
            )
        )

    def _cancelled_result(self, t0, camera_id, position, operation):
        return RoiActivationResult(
            state=RoiActivationState.CANCELLED,
            camera_id=camera_id,
            ptz_id=position.ptz_id,
            position_id=position.position_id,
            operation=operation,
            error=PtzError(
                code=f"{camera_id}:retarget:cancelled",
                message="Retarget cancelled",
                category=PtzErrorCategory.UNKNOWN,
            ),
            elapsed_s=time.monotonic() - t0,
        )


__all__ = ["ObserverRetargetCoordinator"]
