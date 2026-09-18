"""ui.widgets.roi_overlay -- position-scoped overlay model for the Camera Region.

Overlays are derived from validated RoiDefinitions in image coordinates
and repainted on resize without touching the stored geometry. Only the
active context's ROIs are ever shown; stale contexts are dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from thermal_monitor.roi.enums import RoiObjectType


@dataclass(slots=True)
class RoiOverlayItem:
    roi_id: str
    object_type: RoiObjectType
    geometry: dict
    name: str = ""
    selected: bool = False
    alarm_active: bool = False
    color: str = "#FFFF00"
    value_text: str = ""
    valid: bool = True


@dataclass(slots=True)
class RoiOverlaySet:
    """One paintable snapshot bound to a single active context."""

    camera_id: str
    ptz_id: str
    position_id: str
    context_generation: int
    items: list[RoiOverlayItem] = field(default_factory=list)

    def for_paint(self) -> list[RoiOverlayItem]:
        return list(self.items)


def overlay_color(object_type: RoiObjectType, *, selected: bool = False,
                 alarm: bool = False) -> str:
    if alarm:
        return "#FF0000"
    if selected:
        return "#00FF00"
    from thermal_monitor.roi.enums import RoiCategory, category_of
    category = category_of(object_type)
    if category == RoiCategory.MEASUREMENT:
        return "#00CCFF"
    if category == RoiCategory.ANNOTATION:
        return "#FFAA00"
    return "#FFFF00"


__all__ = ["RoiOverlayItem", "RoiOverlaySet", "overlay_color"]
