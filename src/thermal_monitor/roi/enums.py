"""roi.enums -- explicit object-type taxonomy for Phase 10.

Every drawn object is one of these types; the category separates
thermal-analysis ROIs from pure measurement objects and annotations
so the UI, evaluator, and alarm boundary can treat them differently.
"""

from __future__ import annotations

from enum import Enum


class RoiCategory(str, Enum):
    ANALYSIS = "analysis"        # thermal statistics, alarm-capable
    MEASUREMENT = "measurement"  # geometric measurement, no thermal stats
    ANNOTATION = "annotation"    # visual note only


class RoiObjectType(str, Enum):
    # Spots
    SPOT = "spot"
    HOTTEST_SPOT = "hottest_spot"
    HOT_COLD_SPOTS = "hot_cold_spots"
    # Lines
    FREE_LINE = "free_line"
    HORIZONTAL_LINE = "horizontal_line"
    VERTICAL_LINE = "vertical_line"
    POLYLINE = "polyline"
    CROSS_LINE = "cross_line"
    # Areas
    RECTANGLE = "rectangle"
    ELLIPSE = "ellipse"
    CIRCLE = "circle"
    POLYGON = "polygon"
    # Measurements (rulers)
    RULER = "ruler"
    HORIZONTAL_RULER = "horizontal_ruler"
    VERTICAL_RULER = "vertical_ruler"
    MEASURE_LINE = "measure_line"
    MEASURE_ANGLE = "measure_angle"
    # Annotations
    NOTE = "note"
    ARROW = "arrow"
    ANNOTATION_RECTANGLE = "annotation_rectangle"
    ANNOTATION_ELLIPSE = "annotation_ellipse"


CATEGORY_BY_TYPE: dict[RoiObjectType, RoiCategory] = {
    RoiObjectType.SPOT: RoiCategory.ANALYSIS,
    RoiObjectType.HOTTEST_SPOT: RoiCategory.ANALYSIS,
    RoiObjectType.HOT_COLD_SPOTS: RoiCategory.ANALYSIS,
    RoiObjectType.FREE_LINE: RoiCategory.ANALYSIS,
    RoiObjectType.HORIZONTAL_LINE: RoiCategory.ANALYSIS,
    RoiObjectType.VERTICAL_LINE: RoiCategory.ANALYSIS,
    RoiObjectType.POLYLINE: RoiCategory.ANALYSIS,
    RoiObjectType.CROSS_LINE: RoiCategory.ANALYSIS,
    RoiObjectType.RECTANGLE: RoiCategory.ANALYSIS,
    RoiObjectType.ELLIPSE: RoiCategory.ANALYSIS,
    RoiObjectType.CIRCLE: RoiCategory.ANALYSIS,
    RoiObjectType.POLYGON: RoiCategory.ANALYSIS,
    RoiObjectType.RULER: RoiCategory.MEASUREMENT,
    RoiObjectType.HORIZONTAL_RULER: RoiCategory.MEASUREMENT,
    RoiObjectType.VERTICAL_RULER: RoiCategory.MEASUREMENT,
    RoiObjectType.MEASURE_LINE: RoiCategory.MEASUREMENT,
    RoiObjectType.MEASURE_ANGLE: RoiCategory.MEASUREMENT,
    RoiObjectType.NOTE: RoiCategory.ANNOTATION,
    RoiObjectType.ARROW: RoiCategory.ANNOTATION,
    RoiObjectType.ANNOTATION_RECTANGLE: RoiCategory.ANNOTATION,
    RoiObjectType.ANNOTATION_ELLIPSE: RoiCategory.ANNOTATION,
}


def category_of(object_type: RoiObjectType) -> RoiCategory:
    return CATEGORY_BY_TYPE[object_type]


__all__ = ["RoiCategory", "RoiObjectType", "CATEGORY_BY_TYPE", "category_of"]
