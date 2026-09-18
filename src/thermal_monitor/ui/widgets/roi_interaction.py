"""ui.widgets.roi_interaction -- selection/drawing state machine.

Operates purely in image coordinates via ViewportMapping. Owns:
- active tool, in-progress vertices, Escape cancellation
- hit-testing for selection (tolerance in image px)
- an explicit undo/redo command stack scoped to one position session
  (cleared on every position change so old commands can never leak
  into a new context).

No Qt painting here; the Camera Region renders RoiOverlaySet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from thermal_monitor.roi.coordinate_system import ViewportMapping
from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.errors import RoiValidationError
from thermal_monitor.roi.geometry import (
    ArrowGeometry, CircleGeometry, CrossLineGeometry, EllipseGeometry,
    HottestSpotGeometry, HotColdSpotsGeometry, LineGeometry, NoteGeometry,
    PolygonGeometry, PolylineGeometry, RectangleGeometry, SpotGeometry,
)


@dataclass(slots=True)
class EditCommand:
    kind: str  # create | delete | move | geometry
    roi_id: str
    before: dict | None = None
    after: dict | None = None


class RoiInteractionState:
    """Tool + selection + undo state for one editing session."""

    SELECT_TOLERANCE_PX = 6.0

    def __init__(self) -> None:
        self.active_tool: RoiObjectType | None = None
        self.selected_id: str | None = None
        self.in_progress: list[tuple[float, float]] = []  # image (col,row)
        self.drag_start: tuple[float, float] | None = None
        self._undo: list[EditCommand] = []
        self._redo: list[EditCommand] = []

    # -- session scope -------------------------------------------------
    def clear_session(self) -> None:
        self.active_tool = None
        self.selected_id = None
        self.in_progress.clear()
        self.drag_start = None
        self._undo.clear()
        self._redo.clear()

    def push_undo(self, command: EditCommand) -> None:
        self._undo.append(command)
        self._redo.clear()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def pop_undo(self) -> EditCommand | None:
        if not self._undo:
            return None
        command = self._undo.pop()
        self._redo.append(command)
        return command

    def pop_redo(self) -> EditCommand | None:
        if not self._redo:
            return None
        command = self._redo.pop()
        self._undo.append(command)
        return command

    # -- drawing --------------------------------------------------------
    def cancel_drawing(self) -> bool:
        """Escape: drop the in-progress shape. Returns True if cancelled."""
        if self.in_progress or self.drag_start is not None:
            self.in_progress.clear()
            self.drag_start = None
            return True
        return False

    def click(self, col: float, row: float, *, finish: bool = False):
        """Feed one image-coordinate click; returns a geometry or None."""
        tool = self.active_tool
        if tool is None:
            return None
        if tool in (RoiObjectType.SPOT, RoiObjectType.NOTE):
            self.in_progress.clear()
            return self._finish_single(tool, col, row)
        if tool in (RoiObjectType.FREE_LINE, RoiObjectType.HORIZONTAL_LINE,
                    RoiObjectType.VERTICAL_LINE, RoiObjectType.RULER,
                    RoiObjectType.HORIZONTAL_RULER, RoiObjectType.VERTICAL_RULER,
                    RoiObjectType.MEASURE_LINE, RoiObjectType.ARROW):
            self.in_progress.append((col, row))
            if len(self.in_progress) >= 2:
                pts = self.in_progress[:2]
                self.in_progress.clear()
                return self._finish_two_point(tool, pts)
            return None
        if tool in (RoiObjectType.POLYLINE, RoiObjectType.POLYGON):
            self.in_progress.append((col, row))
            if finish and len(self.in_progress) >= 2:
                pts = list(self.in_progress)
                self.in_progress.clear()
                return self._finish_multi(tool, pts)
            return None
        if tool in (RoiObjectType.HOTTEST_SPOT, RoiObjectType.COLDEST_SPOT,
                    RoiObjectType.HOT_COLD_SPOTS,
                    RoiObjectType.RECTANGLE, RoiObjectType.ANNOTATION_RECTANGLE):
            self.in_progress.append((col, row))
            if len(self.in_progress) >= 2:
                pts = self.in_progress[:2]
                self.in_progress.clear()
                return self._finish_box(tool, pts)
            return None
        if tool in (RoiObjectType.ELLIPSE, RoiObjectType.CIRCLE,
                    RoiObjectType.ANNOTATION_ELLIPSE, RoiObjectType.CROSS_LINE):
            self.in_progress.append((col, row))
            if len(self.in_progress) >= 2:
                pts = self.in_progress[:2]
                self.in_progress.clear()
                return self._finish_center_span(tool, pts)
            return None
        if tool == RoiObjectType.MEASURE_ANGLE:
            self.in_progress.append((col, row))
            if len(self.in_progress) >= 3:
                pts = self.in_progress[:3]
                self.in_progress.clear()
                from thermal_monitor.roi.geometry import AngleGeometry
                return AngleGeometry(center_row=pts[1][1], center_col=pts[1][0],
                                     end1_row=pts[0][1], end1_col=pts[0][0],
                                     end2_row=pts[2][1], end2_col=pts[2][0])
            return None
        return None

    def _finish_single(self, tool, col, row):
        if tool == RoiObjectType.SPOT:
            return SpotGeometry(row=row, col=col)
        return NoteGeometry(row=row, col=col, text="")

    def _finish_two_point(self, tool, pts):
        (c1, r1), (c2, r2) = pts
        if tool == RoiObjectType.HORIZONTAL_LINE:
            r2 = r1
        if tool == RoiObjectType.VERTICAL_LINE:
            c2 = c1
        if tool == RoiObjectType.HORIZONTAL_RULER:
            r2 = r1
        if tool == RoiObjectType.VERTICAL_RULER:
            c2 = c1
        if tool == RoiObjectType.ARROW:
            return ArrowGeometry(row1=r1, col1=c1, row2=r2, col2=c2)
        return LineGeometry(row1=r1, col1=c1, row2=r2, col2=c2)

    def _finish_multi(self, tool, pts):
        rows_cols = [(r, c) for c, r in pts]
        if tool == RoiObjectType.POLYLINE:
            return PolylineGeometry(points=tuple(rows_cols))
        if len(rows_cols) < 3:
            raise RoiValidationError("polygon requires at least 3 vertices")
        return PolygonGeometry(points=tuple(rows_cols))

    def _finish_box(self, tool, pts):
        (c1, r1), (c2, r2) = pts
        r_lo, r_hi = (r1, r2) if r1 <= r2 else (r2, r1)
        c_lo, c_hi = (c1, c2) if c1 <= c2 else (c2, c1)
        if tool in (RoiObjectType.HOTTEST_SPOT, RoiObjectType.COLDEST_SPOT):
            return HottestSpotGeometry(row1=r_lo, col1=c_lo, row2=r_hi, col2=c_hi)
        if tool == RoiObjectType.HOT_COLD_SPOTS:
            return HotColdSpotsGeometry(row1=r_lo, col1=c_lo, row2=r_hi, col2=c_hi)
        return RectangleGeometry(row1=r_lo, col1=c_lo, row2=r_hi, col2=c_hi)

    def _finish_center_span(self, tool, pts):
        (c1, r1), (c2, r2) = pts
        if tool == RoiObjectType.CROSS_LINE:
            return CrossLineGeometry(center_row=r1, center_col=c1,
                                     half_length_row=max(1.0, abs(r2 - r1)),
                                     half_length_col=max(1.0, abs(c2 - c1)))
        if tool == RoiObjectType.CIRCLE:
            radius = max(1.0, math.hypot(r2 - r1, c2 - c1))
            return CircleGeometry(center_row=r1, center_col=c1, radius=radius)
        return EllipseGeometry(center_row=r1, center_col=c1,
                               radius1=max(1.0, abs(r2 - r1)),
                               radius2=max(1.0, abs(c2 - c1)))

    # -- transient preview ------------------------------------------------
    def preview_geometry(self, tool, points):
        """Pure preview construction from image-coordinate points.

        Same finishers as click(), but stateless: never touches
        ``in_progress``. Returns a geometry or None when the points do
        not define one yet. May raise RoiValidationError for degenerate
        input (callers hide the preview until the input is valid).
        ``points`` are (col, row) tuples.
        """
        if tool is None or not points:
            return None
        if tool in (RoiObjectType.SPOT, RoiObjectType.NOTE):
            return self._finish_single(tool, *points[0])
        if tool in (RoiObjectType.FREE_LINE, RoiObjectType.HORIZONTAL_LINE,
                    RoiObjectType.VERTICAL_LINE, RoiObjectType.RULER,
                    RoiObjectType.HORIZONTAL_RULER, RoiObjectType.VERTICAL_RULER,
                    RoiObjectType.MEASURE_LINE, RoiObjectType.ARROW):
            if len(points) < 2:
                return None
            return self._finish_two_point(tool, points[:2])
        if tool in (RoiObjectType.POLYLINE, RoiObjectType.POLYGON):
            if len(points) < 2:
                return None
            if tool == RoiObjectType.POLYLINE:
                return self._finish_multi(tool, points)
            if len(points) < 3:
                # Rubber-band edge only; completion needs 3+ vertices.
                return None
            return self._finish_multi(tool, points)
        if tool in (RoiObjectType.HOTTEST_SPOT, RoiObjectType.COLDEST_SPOT,
                    RoiObjectType.HOT_COLD_SPOTS,
                    RoiObjectType.RECTANGLE, RoiObjectType.ANNOTATION_RECTANGLE):
            if len(points) < 2:
                return None
            return self._finish_box(tool, points[:2])
        if tool in (RoiObjectType.ELLIPSE, RoiObjectType.CIRCLE,
                    RoiObjectType.ANNOTATION_ELLIPSE, RoiObjectType.CROSS_LINE):
            if len(points) < 2:
                return None
            return self._finish_center_span(tool, points[:2])
        if tool == RoiObjectType.MEASURE_ANGLE:
            if len(points) < 3:
                return None
            from thermal_monitor.roi.geometry import AngleGeometry
            pts = points[:3]
            return AngleGeometry(center_row=pts[1][1], center_col=pts[1][0],
                                 end1_row=pts[0][1], end1_col=pts[0][0],
                                 end2_row=pts[2][1], end2_col=pts[2][0])
        return None

    # -- selection ------------------------------------------------------
    def hit_test(self, col: float, row: float, items: list) -> str | None:
        best: str | None = None
        best_dist = self.SELECT_TOLERANCE_PX
        for item in items:
            dist = _distance_to_item(col, row, item)
            if dist is not None and dist <= best_dist:
                best_dist = dist
                best = item.roi_id
        self.selected_id = best
        return best


def _distance_to_item(col: float, row: float, item) -> float | None:
    g = item.geometry
    if isinstance(g, dict):
        # overlay dict form: approximate by stored center keys
        r = g.get("row", g.get("center_row", g.get("row1")))
        c = g.get("col", g.get("center_col", g.get("col1")))
        if r is None or c is None:
            return None
        return math.hypot(row - float(r), col - float(c))
    t = type(g).__name__
    if t in ("SpotGeometry", "NoteGeometry"):
        return math.hypot(row - g.row, col - g.col)
    if t == "LineGeometry":
        return _point_segment_distance(col, row, g.col1, g.row1, g.col2, g.row2)
    if t == "RectangleGeometry":
        if g.col1 <= col <= g.col2 and g.row1 <= row <= g.row2:
            return 0.0
        dx = min(abs(col - g.col1), abs(col - g.col2))
        dy = min(abs(row - g.row1), abs(row - g.row2))
        return math.hypot(dx, dy)
    if t == "CircleGeometry":
        dist = math.hypot(row - g.center_row, col - g.center_col)
        if dist <= g.radius:
            return 0.0
        return dist - g.radius
    if t == "EllipseGeometry":
        # Rotate into the ellipse frame, then normalize by radii.
        phi = float(getattr(g, "phi", 0.0) or 0.0)
        dx = col - g.center_col
        dy = row - g.center_row
        cos_p, sin_p = math.cos(phi), math.sin(phi)
        lx = (dx * cos_p + dy * sin_p) / max(g.radius2, 1e-9)
        ly = (-dx * sin_p + dy * cos_p) / max(g.radius1, 1e-9)
        norm = math.hypot(lx, ly)
        if norm <= 1.0:
            return 0.0
        return (norm - 1.0) * min(g.radius1, g.radius2)
    if t == "PolygonGeometry":
        pts = [(float(r), float(c)) for r, c in g.points]
        if _point_in_polygon(col, row, pts):
            return 0.0
        return min(
            _point_segment_distance(col, row, c1, r1, c2, r2)
            for (r1, c1), (r2, c2) in zip(pts, pts[1:] + pts[:1]))
    if t == "CrossLineGeometry":
        return math.hypot(row - g.center_row, col - g.center_col)
    return math.hypot(row - getattr(g, "center_row", row), col - getattr(g, "center_col", col))


def _point_in_polygon(col: float, row: float, pts: list) -> bool:
    """Ray-casting point-in-polygon (image coords: col=x, row=y)."""
    inside = False
    n = len(pts)
    for i in range(n):
        r1, c1 = pts[i]
        r2, c2 = pts[(i + 1) % n]
        if ((r1 > row) != (r2 > row)) and (
                col < (c2 - c1) * (row - r1) / (r2 - r1) + c1):
            inside = not inside
    return inside


def _point_segment_distance(px, py, x1, y1, x2, y2) -> float:
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


__all__ = ["EditCommand", "RoiInteractionState"]
