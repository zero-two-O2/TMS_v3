"""roi.geometry -- validated, serializable geometry for every object type.

Canonical persistent representation: image pixel coordinates in the
HALCON row/col convention (row = y down, col = x right), tied to the
source image dimensions (e.g. 640x480), never widget coordinates.
All types are frozen dataclasses with ``to_dict`` / ``from_dict``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.errors import RoiValidationError


def _finite(name: str, value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RoiValidationError(f"{name} must be numeric, got {value!r}") from exc
    if not math.isfinite(number):
        raise RoiValidationError(f"{name} must be finite, got {value!r}")
    return number


def _positive(name: str, value: object) -> float:
    number = _finite(name, value)
    if number <= 0:
        raise RoiValidationError(f"{name} must be > 0, got {number}")
    return number


def _non_negative(name: str, value: object) -> float:
    number = _finite(name, value)
    if number < 0:
        raise RoiValidationError(f"{name} must be >= 0, got {number}")
    return number


@dataclass(frozen=True, slots=True)
class SpotGeometry:
    row: float
    col: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "row", _finite("row", self.row))
        object.__setattr__(self, "col", _finite("col", self.col))

    def to_dict(self) -> dict:
        return {"row": self.row, "col": self.col}

    @staticmethod
    def from_dict(data: dict) -> "SpotGeometry":
        return SpotGeometry(row=data["row"], col=data["col"])


@dataclass(frozen=True, slots=True)
class HottestSpotGeometry:
    row1: float
    col1: float
    row2: float
    col2: float

    def __post_init__(self) -> None:
        r1 = _finite("row1", self.row1)
        c1 = _finite("col1", self.col1)
        r2 = _finite("row2", self.row2)
        c2 = _finite("col2", self.col2)
        if r1 > r2 or c1 > c2:
            raise RoiValidationError("hottest-spot search region requires row1<=row2, col1<=col2")
        if r2 - r1 < 1.0 or c2 - c1 < 1.0:
            raise RoiValidationError("hottest-spot search region is degenerate (<1px)")
        object.__setattr__(self, "row1", r1)
        object.__setattr__(self, "col1", c1)
        object.__setattr__(self, "row2", r2)
        object.__setattr__(self, "col2", c2)

    def to_dict(self) -> dict:
        return {"row1": self.row1, "col1": self.col1, "row2": self.row2, "col2": self.col2}

    @staticmethod
    def from_dict(data: dict) -> "HottestSpotGeometry":
        return HottestSpotGeometry(row1=data["row1"], col1=data["col1"], row2=data["row2"], col2=data["col2"])


@dataclass(frozen=True, slots=True)
class HotColdSpotsGeometry:
    row1: float
    col1: float
    row2: float
    col2: float
    min_separation: float = 5.0

    def __post_init__(self) -> None:
        r1 = _finite("row1", self.row1)
        c1 = _finite("col1", self.col1)
        r2 = _finite("row2", self.row2)
        c2 = _finite("col2", self.col2)
        if r1 > r2 or c1 > c2:
            raise RoiValidationError("hot/cold-spot search region requires row1<=row2, col1<=col2")
        if r2 - r1 < 1.0 or c2 - c1 < 1.0:
            raise RoiValidationError("hot/cold-spot search region is degenerate (<1px)")
        sep = _non_negative("min_separation", self.min_separation)
        object.__setattr__(self, "row1", r1)
        object.__setattr__(self, "col1", c1)
        object.__setattr__(self, "row2", r2)
        object.__setattr__(self, "col2", c2)
        object.__setattr__(self, "min_separation", sep)

    def to_dict(self) -> dict:
        return {"row1": self.row1, "col1": self.col1, "row2": self.row2,
                "col2": self.col2, "min_separation": self.min_separation}

    @staticmethod
    def from_dict(data: dict) -> "HotColdSpotsGeometry":
        return HotColdSpotsGeometry(row1=data["row1"], col1=data["col1"], row2=data["row2"],
                                    col2=data["col2"], min_separation=data.get("min_separation", 5.0))


@dataclass(frozen=True, slots=True)
class LineGeometry:
    row1: float
    col1: float
    row2: float
    col2: float

    def __post_init__(self) -> None:
        r1 = _finite("row1", self.row1)
        c1 = _finite("col1", self.col1)
        r2 = _finite("row2", self.row2)
        c2 = _finite("col2", self.col2)
        if math.hypot(r2 - r1, c2 - c1) < 1.0:
            raise RoiValidationError("line geometry is degenerate (<1px length)")
        object.__setattr__(self, "row1", r1)
        object.__setattr__(self, "col1", c1)
        object.__setattr__(self, "row2", r2)
        object.__setattr__(self, "col2", c2)

    def to_dict(self) -> dict:
        return {"row1": self.row1, "col1": self.col1, "row2": self.row2, "col2": self.col2}

    @staticmethod
    def from_dict(data: dict) -> "LineGeometry":
        return LineGeometry(row1=data["row1"], col1=data["col1"], row2=data["row2"], col2=data["col2"])


@dataclass(frozen=True, slots=True)
class PolylineGeometry:
    points: tuple[tuple[float, float], ...]  # ((row, col), ...)

    def __post_init__(self) -> None:
        pts = tuple((float(_finite(f"points[{i}].row", p[0])), float(_finite(f"points[{i}].col", p[1])))
                    for i, p in enumerate(self.points))
        if len(pts) < 2:
            raise RoiValidationError("polyline requires at least 2 vertices")
        total = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))
        if total < 1.0:
            raise RoiValidationError("polyline is degenerate (<1px total length)")
        object.__setattr__(self, "points", pts)

    def to_dict(self) -> dict:
        return {"points": [[r, c] for r, c in self.points]}

    @staticmethod
    def from_dict(data: dict) -> "PolylineGeometry":
        return PolylineGeometry(points=tuple((p[0], p[1]) for p in data["points"]))


@dataclass(frozen=True, slots=True)
class CrossLineGeometry:
    center_row: float
    center_col: float
    half_length_row: float = 50.0
    half_length_col: float = 50.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "center_row", _finite("center_row", self.center_row))
        object.__setattr__(self, "center_col", _finite("center_col", self.center_col))
        object.__setattr__(self, "half_length_row", _positive("half_length_row", self.half_length_row))
        object.__setattr__(self, "half_length_col", _positive("half_length_col", self.half_length_col))

    def to_dict(self) -> dict:
        return {"center_row": self.center_row, "center_col": self.center_col,
                "half_length_row": self.half_length_row, "half_length_col": self.half_length_col}

    @staticmethod
    def from_dict(data: dict) -> "CrossLineGeometry":
        return CrossLineGeometry(center_row=data["center_row"], center_col=data["center_col"],
                                 half_length_row=data.get("half_length_row", 50.0),
                                 half_length_col=data.get("half_length_col", 50.0))


@dataclass(frozen=True, slots=True)
class RectangleGeometry:
    row1: float
    col1: float
    row2: float
    col2: float

    def __post_init__(self) -> None:
        r1 = _finite("row1", self.row1)
        c1 = _finite("col1", self.col1)
        r2 = _finite("row2", self.row2)
        c2 = _finite("col2", self.col2)
        if r1 > r2 or c1 > c2:
            raise RoiValidationError("rectangle requires row1<=row2, col1<=col2")
        if r2 - r1 < 1.0 or c2 - c1 < 1.0:
            raise RoiValidationError("rectangle is degenerate (<1px side)")
        object.__setattr__(self, "row1", r1)
        object.__setattr__(self, "col1", c1)
        object.__setattr__(self, "row2", r2)
        object.__setattr__(self, "col2", c2)

    def to_dict(self) -> dict:
        return {"row1": self.row1, "col1": self.col1, "row2": self.row2, "col2": self.col2}

    @staticmethod
    def from_dict(data: dict) -> "RectangleGeometry":
        return RectangleGeometry(row1=data["row1"], col1=data["col1"], row2=data["row2"], col2=data["col2"])


@dataclass(frozen=True, slots=True)
class EllipseGeometry:
    center_row: float
    center_col: float
    radius1: float
    radius2: float
    phi: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "center_row", _finite("center_row", self.center_row))
        object.__setattr__(self, "center_col", _finite("center_col", self.center_col))
        object.__setattr__(self, "radius1", _positive("radius1", self.radius1))
        object.__setattr__(self, "radius2", _positive("radius2", self.radius2))
        object.__setattr__(self, "phi", _finite("phi", self.phi))

    def to_dict(self) -> dict:
        return {"center_row": self.center_row, "center_col": self.center_col,
                "radius1": self.radius1, "radius2": self.radius2, "phi": self.phi}

    @staticmethod
    def from_dict(data: dict) -> "EllipseGeometry":
        return EllipseGeometry(center_row=data["center_row"], center_col=data["center_col"],
                               radius1=data["radius1"], radius2=data["radius2"],
                               phi=data.get("phi", 0.0))


@dataclass(frozen=True, slots=True)
class CircleGeometry:
    center_row: float
    center_col: float
    radius: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "center_row", _finite("center_row", self.center_row))
        object.__setattr__(self, "center_col", _finite("center_col", self.center_col))
        object.__setattr__(self, "radius", _positive("radius", self.radius))

    def to_dict(self) -> dict:
        return {"center_row": self.center_row, "center_col": self.center_col, "radius": self.radius}

    @staticmethod
    def from_dict(data: dict) -> "CircleGeometry":
        return CircleGeometry(center_row=data["center_row"], center_col=data["center_col"],
                              radius=data["radius"])


@dataclass(frozen=True, slots=True)
class PolygonGeometry:
    points: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        pts = tuple((float(_finite(f"points[{i}].row", p[0])), float(_finite(f"points[{i}].col", p[1])))
                    for i, p in enumerate(self.points))
        if len(pts) < 3:
            raise RoiValidationError(f"polygon requires at least 3 vertices, got {len(pts)}")
        area2 = abs(sum(a[1] * b[0] - b[1] * a[0] for a, b in zip(pts, pts[1:] + pts[:1])))
        if area2 < 1.0:
            raise RoiValidationError("polygon is degenerate (near-zero area)")
        object.__setattr__(self, "points", pts)

    def to_dict(self) -> dict:
        return {"points": [[r, c] for r, c in self.points]}

    @staticmethod
    def from_dict(data: dict) -> "PolygonGeometry":
        return PolygonGeometry(points=tuple((p[0], p[1]) for p in data["points"]))


@dataclass(frozen=True, slots=True)
class AngleGeometry:
    center_row: float
    center_col: float
    end1_row: float
    end1_col: float
    end2_row: float
    end2_col: float

    def __post_init__(self) -> None:
        cr = _finite("center_row", self.center_row)
        cc = _finite("center_col", self.center_col)
        a1r = _finite("end1_row", self.end1_row)
        a1c = _finite("end1_col", self.end1_col)
        a2r = _finite("end2_row", self.end2_row)
        a2c = _finite("end2_col", self.end2_col)
        if math.hypot(a1r - cr, a1c - cc) < 1.0 or math.hypot(a2r - cr, a2c - cc) < 1.0:
            raise RoiValidationError("angle segments must each be >= 1px (zero-length rejected)")
        object.__setattr__(self, "center_row", cr)
        object.__setattr__(self, "center_col", cc)
        object.__setattr__(self, "end1_row", a1r)
        object.__setattr__(self, "end1_col", a1c)
        object.__setattr__(self, "end2_row", a2r)
        object.__setattr__(self, "end2_col", a2c)

    def to_dict(self) -> dict:
        return {"center_row": self.center_row, "center_col": self.center_col,
                "end1_row": self.end1_row, "end1_col": self.end1_col,
                "end2_row": self.end2_row, "end2_col": self.end2_col}

    @staticmethod
    def from_dict(data: dict) -> "AngleGeometry":
        return AngleGeometry(center_row=data["center_row"], center_col=data["center_col"],
                             end1_row=data["end1_row"], end1_col=data["end1_col"],
                             end2_row=data["end2_row"], end2_col=data["end2_col"])


@dataclass(frozen=True, slots=True)
class NoteGeometry:
    row: float
    col: float
    text: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "row", _finite("row", self.row))
        object.__setattr__(self, "col", _finite("col", self.col))
        if not isinstance(self.text, str):
            raise RoiValidationError("note text must be a string")
        if len(self.text) > 500:
            raise RoiValidationError("note text exceeds 500 characters")

    def to_dict(self) -> dict:
        return {"row": self.row, "col": self.col, "text": self.text}

    @staticmethod
    def from_dict(data: dict) -> "NoteGeometry":
        return NoteGeometry(row=data["row"], col=data["col"], text=data.get("text", ""))


@dataclass(frozen=True, slots=True)
class ArrowGeometry:
    row1: float
    col1: float
    row2: float
    col2: float

    def __post_init__(self) -> None:
        r1 = _finite("row1", self.row1)
        c1 = _finite("col1", self.col1)
        r2 = _finite("row2", self.row2)
        c2 = _finite("col2", self.col2)
        if math.hypot(r2 - r1, c2 - c1) < 1.0:
            raise RoiValidationError("arrow is degenerate (<1px length)")
        object.__setattr__(self, "row1", r1)
        object.__setattr__(self, "col1", c1)
        object.__setattr__(self, "row2", r2)
        object.__setattr__(self, "col2", c2)

    def to_dict(self) -> dict:
        return {"row1": self.row1, "col1": self.col1, "row2": self.row2, "col2": self.col2}

    @staticmethod
    def from_dict(data: dict) -> "ArrowGeometry":
        return ArrowGeometry(row1=data["row1"], col1=data["col1"], row2=data["row2"], col2=data["col2"])


GEOMETRY_BY_TYPE = {
    RoiObjectType.SPOT: SpotGeometry,
    RoiObjectType.HOTTEST_SPOT: HottestSpotGeometry,
    RoiObjectType.COLDEST_SPOT: HottestSpotGeometry,  # shared search-region shape
    RoiObjectType.HOT_COLD_SPOTS: HotColdSpotsGeometry,
    RoiObjectType.FREE_LINE: LineGeometry,
    RoiObjectType.HORIZONTAL_LINE: LineGeometry,
    RoiObjectType.VERTICAL_LINE: LineGeometry,
    RoiObjectType.POLYLINE: PolylineGeometry,
    RoiObjectType.CROSS_LINE: CrossLineGeometry,
    RoiObjectType.RECTANGLE: RectangleGeometry,
    RoiObjectType.ELLIPSE: EllipseGeometry,
    RoiObjectType.CIRCLE: CircleGeometry,
    RoiObjectType.POLYGON: PolygonGeometry,
    RoiObjectType.RULER: LineGeometry,
    RoiObjectType.HORIZONTAL_RULER: LineGeometry,
    RoiObjectType.VERTICAL_RULER: LineGeometry,
    RoiObjectType.MEASURE_LINE: LineGeometry,
    RoiObjectType.MEASURE_ANGLE: AngleGeometry,
    RoiObjectType.NOTE: NoteGeometry,
    RoiObjectType.ARROW: ArrowGeometry,
    RoiObjectType.ANNOTATION_RECTANGLE: RectangleGeometry,
    RoiObjectType.ANNOTATION_ELLIPSE: EllipseGeometry,
}


def geometry_from_dict(object_type: RoiObjectType, data: dict):  # noqa: ANN201
    try:
        cls = GEOMETRY_BY_TYPE[object_type]
    except KeyError as exc:
        raise RoiValidationError(f"unsupported object type {object_type}") from exc
    if not isinstance(data, dict):
        raise RoiValidationError("geometry must be a mapping")
    try:
        return cls.from_dict(data)
    except RoiValidationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise RoiValidationError(
            f"{object_type.value} geometry is missing or invalid: {exc}") from exc


__all__ = [
    "SpotGeometry", "HottestSpotGeometry", "HotColdSpotsGeometry",
    "LineGeometry", "PolylineGeometry", "CrossLineGeometry",
    "RectangleGeometry", "EllipseGeometry", "CircleGeometry",
    "PolygonGeometry", "AngleGeometry", "NoteGeometry", "ArrowGeometry",
    "GEOMETRY_BY_TYPE", "geometry_from_dict",
]
