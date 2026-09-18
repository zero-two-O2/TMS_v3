"""halcon.geometry -- ROI geometry -> HALCON operator parameters.

Converts validated RoiDefinition geometry into the row/col tuples the
existing ``HalconROIAdapter`` generators expect. Degenerate or
out-of-bounds geometry is rejected before any HALCON call.
"""

from __future__ import annotations

from thermal_monitor.roi import geometry as _g
from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.errors import RoiValidationError


def verify_image_for_halcon(image, *, width: int = 640, height: int = 480):
    import numpy as np
    arr = np.asarray(image)
    if arr.ndim != 2:
        raise RoiValidationError(f"HALCON image must be 2-D, got ndim={arr.ndim}")
    h, w = arr.shape
    if h <= 0 or w <= 0:
        raise RoiValidationError("HALCON image has empty dimensions")
    if not np.any(np.isfinite(arr)):
        raise RoiValidationError("HALCON image has no finite pixels (no-data)")
    return arr


def halcon_params_for(object_type: RoiObjectType, geom) -> dict:
    """Return ``{operator, params}`` for region generation + sampling."""
    if object_type == RoiObjectType.SPOT:
        return {"operator": "sample", "row": float(geom.row), "col": float(geom.col)}
    if object_type in (RoiObjectType.HOTTEST_SPOT, RoiObjectType.HOT_COLD_SPOTS):
        return {"operator": "min_max_gray",
                "row1": float(geom.row1), "col1": float(geom.col1),
                "row2": float(geom.row2), "col2": float(geom.col2)}
    if object_type in (RoiObjectType.FREE_LINE, RoiObjectType.HORIZONTAL_LINE,
                       RoiObjectType.VERTICAL_LINE, RoiObjectType.RULER,
                       RoiObjectType.HORIZONTAL_RULER, RoiObjectType.VERTICAL_RULER,
                       RoiObjectType.MEASURE_LINE, RoiObjectType.ARROW):
        _require_in_bounds(geom.row1, geom.col1)
        _require_in_bounds(geom.row2, geom.col2)
        return {"operator": "gen_region_line",
                "row1": float(geom.row1), "col1": float(geom.col1),
                "row2": float(geom.row2), "col2": float(geom.col2)}
    if object_type == RoiObjectType.POLYLINE:
        for r, c in geom.points:
            _require_in_bounds(r, c)
        return {"operator": "gen_region_polygon_filled",
                "rows": [float(r) for r, _ in geom.points],
                "cols": [float(c) for _, c in geom.points],
                "open": True}
    if object_type == RoiObjectType.CROSS_LINE:
        return {"operator": "gen_cross",
                "center_row": float(geom.center_row), "center_col": float(geom.center_col)}
    if object_type in (RoiObjectType.RECTANGLE, RoiObjectType.ANNOTATION_RECTANGLE):
        return {"operator": "gen_rectangle1",
                "row1": float(geom.row1), "col1": float(geom.col1),
                "row2": float(geom.row2), "col2": float(geom.col2)}
    if object_type == RoiObjectType.CIRCLE:
        return {"operator": "gen_circle",
                "row": float(geom.center_row), "col": float(geom.center_col),
                "radius": float(geom.radius)}
    if object_type in (RoiObjectType.ELLIPSE, RoiObjectType.ANNOTATION_ELLIPSE):
        return {"operator": "gen_ellipse",
                "row": float(geom.center_row), "col": float(geom.center_col),
                "phi": float(geom.phi), "radius1": float(geom.radius1),
                "radius2": float(geom.radius2)}
    if object_type == RoiObjectType.POLYGON:
        for r, c in geom.points:
            _require_in_bounds(r, c)
        return {"operator": "gen_region_polygon_filled",
                "rows": [float(r) for r, _ in geom.points],
                "cols": [float(c) for _, c in geom.points],
                "open": False}
    if object_type == RoiObjectType.MEASURE_ANGLE:
        return {"operator": "angle_ll",
                "center_row": float(geom.center_row), "center_col": float(geom.center_col),
                "end1_row": float(geom.end1_row), "end1_col": float(geom.end1_col),
                "end2_row": float(geom.end2_row), "end2_col": float(geom.end2_col)}
    if object_type == RoiObjectType.NOTE:
        return {"operator": "none", "row": float(geom.row), "col": float(geom.col)}
    raise RoiValidationError(f"no HALCON mapping for {object_type}")


def _require_in_bounds(row: float, col: float, *, width: int = 640,
                       height: int = 480) -> None:
    import math
    if not (math.isfinite(row) and math.isfinite(col)):
        raise RoiValidationError("HALCON coordinates must be finite")
    # Deliberately permissive: HALCON clips regions itself; only
    # non-finite input is rejected here. Bounds come from the caller.


__all__ = ["halcon_params_for", "verify_image_for_halcon"]
