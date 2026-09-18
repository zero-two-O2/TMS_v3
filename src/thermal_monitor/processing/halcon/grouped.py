"""
processing.halcon.grouped -- transient grouped ROI processing views.

Converts persistent ROI definitions (ROIConfig, or any object with
``roi_id``/``name``/``enabled``/``geometry``) into immutable per-shape
groups whose parallel tuples align exactly with ``roi_ids``. The groups
are a transient processing optimization: they never touch persistence,
never mutate inputs, and carry no HALCON handles.

Ordering is deterministic: shape groups follow SHAPE_ORDER, ROIs keep
their input order within a group. Result mapping must use positional
``zip`` against ``roi_ids`` -- never ``list.index``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from thermal_monitor.core.models import ROIShape

# Deterministic processing order for shape groups. HALCON call order
# follows this sequence, so batched results stay reproducible.
SHAPE_ORDER: tuple[ROIShape, ...] = (
    ROIShape.RECTANGLE1,
    ROIShape.RECTANGLE2,
    ROIShape.CIRCLE,
    ROIShape.ELLIPSE,
    ROIShape.POLYGON,
)


@dataclass(frozen=True, slots=True)
class ShapeGroup:
    """One shape's aligned processing view.

    ``params`` holds one entry per ROI in ``roi_ids`` order:
    - RECTANGLE1: (row1, col1, row2, col2) ints
    - RECTANGLE2: (row, col, phi, length1, length2) floats
    - CIRCLE: (row, col, radius) floats
    - ELLIPSE: (row, col, phi, radius1, radius2) floats
    - POLYGON: ((rows...), (cols...)) vertex tuples per ROI
    """

    shape: ROIShape
    roi_ids: tuple[str, ...]
    roi_names: tuple[str, ...]
    params: tuple[Any, ...]
    fingerprint: tuple[Any, ...]
    camera_id: str
    position_id: str
    context_generation: int

    def __len__(self) -> int:
        return len(self.roi_ids)


@dataclass(frozen=True, slots=True)
class InvalidRoi:
    """An ROI rejected before any HALCON call."""

    roi_id: str
    roi_name: str
    shape: ROIShape | None
    error: str


def _finite(value: object, label: str) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric, got {value!r}") from exc
    import math

    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite, got {value!r}")
    return number


def _extract_params(shape: ROIShape, parameters: Any) -> Any:
    """Extract and validate one ROI's HALCON parameters.

    Raises ValueError for invalid/degenerate geometry. HALCON row/col
    convention (y/x) is preserved end to end.
    """
    get = parameters.get if hasattr(parameters, "get") else None
    if get is None:
        raise ValueError("geometry parameters must be a mapping")

    if shape == ROIShape.RECTANGLE1:
        row1 = _finite(get("y1"), "y1")
        col1 = _finite(get("x1"), "x1")
        row2 = _finite(get("y2"), "y2")
        col2 = _finite(get("x2"), "x2")
        # HALCON gen_rectangle1 requires row2 >= row1, col2 >= col1
        # (strict inequality raises #1303/#1304); equality is a 1px line.
        if row2 < row1 or col2 < col1:
            raise ValueError(f"degenerate rectangle1 ({row1},{col1})-({row2},{col2})")
        return (int(round(row1)), int(round(col1)), int(round(row2)), int(round(col2)))

    if shape == ROIShape.RECTANGLE2:
        row = _finite(get("center_y"), "center_y")
        col = _finite(get("center_x"), "center_x")
        phi = _finite(get("phi"), "phi")
        length1 = _finite(get("length1"), "length1")
        length2 = _finite(get("length2"), "length2")
        if length1 <= 0 or length2 <= 0:
            raise ValueError(f"degenerate rectangle2 lengths ({length1},{length2})")
        return (row, col, phi, length1, length2)

    if shape == ROIShape.CIRCLE:
        row = _finite(get("center_y"), "center_y")
        col = _finite(get("center_x"), "center_x")
        radius = _finite(get("radius"), "radius")
        if radius <= 0:
            raise ValueError(f"degenerate circle radius ({radius})")
        return (row, col, radius)

    if shape == ROIShape.ELLIPSE:
        row = _finite(get("center_y"), "center_y")
        col = _finite(get("center_x"), "center_x")
        phi = _finite(get("phi"), "phi")
        radius1 = _finite(get("radius1"), "radius1")
        radius2 = _finite(get("radius2"), "radius2")
        if radius1 <= 0 or radius2 <= 0:
            raise ValueError(f"degenerate ellipse radii ({radius1},{radius2})")
        return (row, col, phi, radius1, radius2)

    if shape == ROIShape.POLYGON:
        points = get("points")
        if not isinstance(points, (list, tuple)) or len(points) < 3:
            raise ValueError(f"polygon requires >= 3 points, got {points!r}")
        rows: list[float] = []
        cols: list[float] = []
        for i, pt in enumerate(points):
            if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                raise ValueError(f"polygon point {i} must be a (row, col) pair")
            rows.append(_finite(pt[0], f"polygon point {i} row"))
            cols.append(_finite(pt[1], f"polygon point {i} col"))
        return (tuple(rows), tuple(cols))

    raise ValueError(f"unsupported ROI shape: {shape}")


def build_shape_groups(
    rois: Sequence[Any],
    *,
    camera_id: str,
    position_id: str,
    context_generation: int,
) -> tuple[tuple[ShapeGroup, ...], tuple[InvalidRoi, ...]]:
    """Group enabled ROIs by shape with aligned parameter tuples.

    Disabled ROIs are skipped (not invalid). Unknown shapes and invalid
    geometry are collected as InvalidRoi -- callers map those to
    explicitly invalid statistics without touching HALCON.
    """
    buckets: dict[ROIShape, list[Any]] = {}
    order: list[ROIShape] = []
    invalid: list[InvalidRoi] = []

    for roi in rois:
        if not getattr(roi, "enabled", True):
            continue
        shape = getattr(roi, "geometry", None)
        shape = getattr(shape, "shape", None)
        if not isinstance(shape, ROIShape):
            invalid.append(InvalidRoi(
                roi_id=str(getattr(roi, "roi_id", "")),
                roi_name=str(getattr(roi, "name", "")),
                shape=None,
                error=f"unsupported ROI shape: {shape!r}",
            ))
            continue
        if shape not in buckets:
            buckets[shape] = []
            order.append(shape)
        buckets[shape].append(roi)

    groups: list[ShapeGroup] = []
    # Emit in SHAPE_ORDER, then any future shapes in first-seen order.
    ordered_shapes = [s for s in SHAPE_ORDER if s in buckets]
    ordered_shapes.extend(s for s in order if s not in SHAPE_ORDER)

    for shape in ordered_shapes:
        ids: list[str] = []
        names: list[str] = []
        params: list[Any] = []
        for roi in buckets[shape]:
            try:
                extracted = _extract_params(shape, roi.geometry.parameters)
            except ValueError as exc:
                invalid.append(InvalidRoi(
                    roi_id=str(roi.roi_id),
                    roi_name=str(roi.name),
                    shape=shape,
                    error=str(exc),
                ))
                continue
            ids.append(str(roi.roi_id))
            names.append(str(roi.name))
            params.append(extracted)
        if not ids:
            continue
        groups.append(ShapeGroup(
            shape=shape,
            roi_ids=tuple(ids),
            roi_names=tuple(names),
            params=tuple(params),
            fingerprint=tuple((rid, p) for rid, p in zip(ids, params)),
            camera_id=camera_id,
            position_id=position_id,
            context_generation=int(context_generation),
        ))

    return tuple(groups), tuple(invalid)


__all__ = [
    "SHAPE_ORDER",
    "ShapeGroup",
    "InvalidRoi",
    "build_shape_groups",
]
