"""roi.validation -- camera+PTZ+position binding validation."""

from __future__ import annotations

from thermal_monitor.roi.errors import RoiBindingError
from thermal_monitor.roi.models import RoiDefinition


def check_roi_binding(roi: RoiDefinition, *, camera_id: str, ptz_id: str,
                      position_id: str, known_positions: set[str] | None = None) -> None:
    """Reject saves/loads that would place an ROI under the wrong owner."""
    if roi.camera_id != camera_id:
        raise RoiBindingError(
            f"ROI {roi.roi_id!r} belongs to camera {roi.camera_id!r}, "
            f"not {camera_id!r}")
    if roi.ptz_id != ptz_id:
        raise RoiBindingError(
            f"ROI {roi.roi_id!r} belongs to PTZ {roi.ptz_id!r}, not {ptz_id!r}")
    if roi.position_id != position_id:
        raise RoiBindingError(
            f"ROI {roi.roi_id!r} belongs to position {roi.position_id!r}, "
            f"not {position_id!r}")
    if known_positions is not None and position_id not in known_positions:
        raise RoiBindingError(f"position {position_id!r} is not a known saved position")


def check_position_belongs_to_camera(position, camera_id: str, active_ptz_id: str) -> None:
    """A position from camera A must never be saved under camera B."""
    if position.camera_id != camera_id:
        raise RoiBindingError(
            f"position {position.position_id!r} belongs to camera "
            f"{position.camera_id!r}, not {camera_id!r}")
    if position.ptz_id != active_ptz_id:
        raise RoiBindingError(
            f"position {position.position_id!r} belongs to PTZ {position.ptz_id!r}, "
            f"active PTZ is {active_ptz_id!r}")


__all__ = ["check_roi_binding", "check_position_belongs_to_camera"]
