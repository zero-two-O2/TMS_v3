"""roi.loading -- authoritative position-bound ROI loading (§9).

Single entry point used by the Configuration Go To worker (and any
future caller) to build an immutable session snapshot AFTER the PTZ
reaches the target:

1. Validate camera and position ids.
2. Resolve the saved position and confirm it belongs to the camera
   (identity is the stored record, never pan/tilt or display name).
3. Reject stale session generations before touching the repository.
4. Query ONLY (camera_id, position_id) with explicit column lists.
5. Deserialize + validate every ROI (binding, geometry, schema).
6. Return an immutable (RoiActiveContext, roi tuple) pair the caller
   publishes atomically. Failures raise; the current session is never
   mutated here.

Replaces the Phase 10 RoiContextRegistry/RoiActivationPublisher pair:
the existing ActivePositionRegistry remains the single registry; this
module is the single loader.
"""

from __future__ import annotations

import time
import uuid
from typing import Callable


def new_operation_id() -> str:
    return f"roiop_{time.time_ns():d}_{uuid.uuid4().hex[:8]}"


def load_rois_for_position(
    camera_id: str,
    position_id: str,
    camera_session_generation: int,
    position_generation: int,
    *,
    position_provider: Callable[[str], object | None],
    roi_repository,
    current_session_generation: int | None = None,
    context_generation: int = 0,
    operation_id: str = "",
    image_width: int = 640,
    image_height: int = 480,
    source: str = "goto",
):
    """Load and validate the ROI session for a reached position.

    Returns ``(RoiActiveContext, tuple[RoiDefinition, ...])``.
    """
    from thermal_monitor.roi.context import RoiActiveContext
    from thermal_monitor.roi.errors import RoiBindingError, RoiStaleContextError

    if not isinstance(camera_id, str) or not camera_id.strip():
        raise RoiBindingError("load_rois_for_position: camera_id is required")
    if not isinstance(position_id, str) or not position_id.strip():
        raise RoiBindingError("load_rois_for_position: position_id is required")
    if (current_session_generation is not None
            and camera_session_generation != current_session_generation):
        raise RoiStaleContextError(
            f"session moved on ({camera_session_generation} != "
            f"{current_session_generation}); refusing load")
    try:
        position = position_provider(position_id)
    except Exception as exc:
        raise RoiBindingError(f"position {position_id!r} unavailable: {exc}") from exc
    if position is None:
        raise RoiBindingError(f"position {position_id!r} does not exist")
    if getattr(position, "position_id", position_id) != position_id:
        raise RoiBindingError("position provider returned the wrong record")
    # Camera ownership is enforced here from the stored record (never the
    # display name or pan/tilt). The live PTZ binding was already
    # enforced by the movement workflow before the load.
    if getattr(position, "camera_id", None) != camera_id:
        raise RoiBindingError(
            f"position {position_id!r} belongs to camera "
            f"{getattr(position, 'camera_id', None)!r}, not {camera_id!r}")
    try:
        rois = list(roi_repository.list_for_position(camera_id, position_id))
    except Exception as exc:
        raise RoiBindingError(
            f"ROI query for {camera_id}/{position_id} failed: {exc}") from exc
    for roi in rois:
        if (roi.camera_id != camera_id or roi.position_id != position_id
                or roi.ptz_id != position.ptz_id):
            raise RoiBindingError(
                f"ROI {roi.roi_id!r} does not belong to "
                f"{camera_id}/{position.ptz_id}/{position_id}")
    context = RoiActiveContext(
        camera_id=camera_id, ptz_id=position.ptz_id, position_id=position_id,
        position_generation=int(position_generation),
        session_generation=int(camera_session_generation),
        context_generation=int(context_generation), state="active",
        operation_id=operation_id or new_operation_id(),
        roi_ids=tuple(r.roi_id for r in rois),
        image_width=int(image_width), image_height=int(image_height),
        activated_at=time.time())
    _ = source  # recorded by the caller (notice/log text), not the snapshot
    return context, tuple(rois)


__all__ = ["load_rois_for_position", "new_operation_id"]
