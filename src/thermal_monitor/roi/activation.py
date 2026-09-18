"""roi.activation -- position-bound activation publishing to RoiContextRegistry.

Wraps the proven ``ObserverRetargetCoordinator`` sequence (validate ->
move -> wait-for-reached -> resolve ROIs) and, only after success,
validates the loaded ROI set and atomically publishes a
``RoiActiveContext``. Failures never publish partial state; a second
retarget supersedes the first via operation guards.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Callable

from thermal_monitor.roi.context import RoiActiveContext
from thermal_monitor.roi.errors import RoiBindingError
from thermal_monitor.roi.models import RoiDefinition
from thermal_monitor.roi.registry import RoiContextRegistry
from thermal_monitor.roi.validation import check_position_belongs_to_camera

logger = logging.getLogger(__name__)


def new_operation_id() -> str:
    return f"roiop_{time.time_ns():d}_{uuid.uuid4().hex[:8]}"


class RoiActivationPublisher:
    """Coordinates PTZ movement + ROI context publication."""

    def __init__(self, coordinator, registry: RoiContextRegistry | None = None) -> None:
        self._coordinator = coordinator
        self._registry = registry or RoiContextRegistry()

    @property
    def registry(self) -> RoiContextRegistry:
        return self._registry

    def activate(self, camera_id: str, position,
                 roi_loader: Callable[[str, str], list[RoiDefinition]],
                 analysis_config_provider, observer_provider,
                 session_generation_provider: Callable[[], int], *,
                 velocity=None, timeout_s: float = 60.0,
                 cancel_event: threading.Event | None = None,
                 operation_id: str = ""):
        """Full sequence; returns the coordinator result + published context."""
        operation_id = operation_id or new_operation_id()
        self._registry.claim_operation(camera_id, operation_id)
        try:
            binding = self._coordinator._service.binding_for_camera(camera_id)
            check_position_belongs_to_camera(position, camera_id, binding.ptz_id)
        except RoiBindingError as exc:
            from thermal_monitor.ptz.errors import PtzError, PtzErrorCategory
            from thermal_monitor.ptz.roi_activation import RoiActivationResult, RoiActivationState
            return RoiActivationResult(
                state=RoiActivationState.FAILED, camera_id=camera_id,
                ptz_id=getattr(position, "ptz_id", ""),
                position_id=getattr(position, "position_id", ""),
                error=PtzError(code=f"{camera_id}:roi-binding:mismatch",
                               message=str(exc),
                               category=PtzErrorCategory.COMMAND_REJECTED),
                elapsed_s=0.0), None
        result = self._coordinator.retarget(
            camera_id, position, analysis_config_provider, observer_provider,
            session_generation_provider, velocity=velocity, timeout_s=timeout_s,
            cancel_event=cancel_event)
        from thermal_monitor.ptz.roi_activation import RoiActivationState
        if result.state != RoiActivationState.COMPLETED:
            return result, None
        session_generation = session_generation_provider()
        try:
            rois = roi_loader(camera_id, position.position_id)
        except Exception as exc:
            logger.warning("ROI load failed cam=%s pos=%s: %s",
                           camera_id, position.position_id, exc)
            rois = []
        for roi in rois:
            if (roi.camera_id != camera_id or roi.ptz_id != result.ptz_id
                    or roi.position_id != position.position_id):
                logger.error("ROI binding mismatch on load: %s", roi.roi_id)
                from thermal_monitor.ptz.errors import PtzError, PtzErrorCategory
                failed = type(result)(state=RoiActivationState.FAILED,
                                      camera_id=result.camera_id, ptz_id=result.ptz_id,
                                      position_id=result.position_id,
                                      operation=result.operation, roi_ids=(),
                                      error=PtzError(
                                          code=f"{camera_id}:roi-binding:mismatch",
                                          message=f"ROI {roi.roi_id!r} binding mismatch",
                                          category=PtzErrorCategory.COMMAND_REJECTED),
                                      elapsed_s=result.elapsed_s)
                return failed, None
        base = self._registry.get(camera_id)
        position_generation = (base.position_generation + 1) if base else 1
        # Reuse the generation the coordinator published to the observer
        # so the processing path and this registry stay aligned.
        context_generation = _generation_from_registry(self._coordinator, camera_id)
        context = RoiActiveContext(
            camera_id=camera_id, ptz_id=result.ptz_id,
            position_id=position.position_id,
            position_generation=position_generation,
            session_generation=session_generation,
            context_generation=context_generation,
            state="active", operation_id=operation_id,
            roi_ids=tuple(r.roi_id for r in rois),
            activated_at=time.time())
        try:
            self._registry.publish(
                context, current_session_generation=session_generation_provider())
        except Exception as exc:
            logger.warning("ROI context publish rejected: %s", exc)
            from thermal_monitor.ptz.errors import PtzError, PtzErrorCategory
            failed = type(result)(state=RoiActivationState.FAILED,
                                  camera_id=result.camera_id, ptz_id=result.ptz_id,
                                  position_id=result.position_id,
                                  operation=result.operation, roi_ids=result.roi_ids,
                                  error=PtzError(
                                      code=f"{camera_id}:roi-context:stale",
                                      message=str(exc),
                                      category=PtzErrorCategory.UNKNOWN),
                                  elapsed_s=result.elapsed_s)
            return failed, None
        logger.info("[ROI] context published cam=%s pos=%s rois=%d op=%s",
                    camera_id, position.position_id, len(rois), operation_id)
        return result, context


def _generation_from_registry(coordinator, camera_id: str) -> int:
    with coordinator._lock:
        return coordinator._generations.get(camera_id, 0)


__all__ = ["RoiActivationPublisher", "new_operation_id"]
