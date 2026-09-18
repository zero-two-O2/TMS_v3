"""roi.context -- immutable position-bound active ROI context."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RoiActiveContext:
    """Authoritative snapshot of the active ROI evaluation context.

    Published atomically after the PTZ reaches the target and the ROI
    set validates. Consumers stamp every result with
    (camera/session/position/context generations) and discard anything
    that does not match the currently published snapshot.
    """

    camera_id: str
    ptz_id: str
    position_id: str
    position_generation: int
    session_generation: int
    context_generation: int
    state: str  # "active" | "waiting" | "inactive"
    operation_id: str = ""
    roi_ids: tuple = ()
    image_width: int = 640
    image_height: int = 480
    activated_at: float = 0.0

    def matches(self, *, camera_id: str, session_generation: int,
                position_id: str, context_generation: int) -> bool:
        return (
            self.state == "active"
            and self.camera_id == camera_id
            and self.session_generation == session_generation
            and self.position_id == position_id
            and self.context_generation == context_generation
        )


def inactive_context(camera_id: str = "", session_generation: int = 0) -> RoiActiveContext:
    return RoiActiveContext(
        camera_id=camera_id, ptz_id="", position_id="",
        position_generation=0, session_generation=session_generation,
        context_generation=0, state="inactive",
        activated_at=time.time())


__all__ = ["RoiActiveContext", "inactive_context"]
