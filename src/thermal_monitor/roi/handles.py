"""roi.handles -- pure geometry manipulation for drag/resize/edit.

All functions are pure (no Qt, no I/O): they take validated geometry and
return new validated geometry in image coordinates. The interaction
layer and editor own mouse state; this module owns the math.

Handle ids are stable strings:
- rect/box: "nw","ne","sw","se","n","s","e","w"
- circle/ellipse: "e","s" (radius), "center" (move handled separately)
- line/arrow/ruler: "start","end"
- angle: "center","end1","end2"
- spot/note: "center"
- cross: "center"
- polygon/polyline: "v<i>" per vertex
"""

from __future__ import annotations

import math

from thermal_monitor.roi import geometry as _g
from thermal_monitor.roi.errors import RoiValidationError

MIN_SIDE_PX = 1.0


def handles_for(geom) -> list[tuple[str, float, float]]:
    """Return [(handle_id, col, row)] in image coordinates."""
    if isinstance(geom, (_g.SpotGeometry, _g.NoteGeometry)):
        row = geom.row if isinstance(geom, _g.SpotGeometry) else geom.row
        return [("center", float(geom.col), float(row))]
    if isinstance(geom, _g.RectangleGeometry):
        r1, c1, r2, c2 = geom.row1, geom.col1, geom.row2, geom.col2
        return [("nw", c1, r1), ("ne", c2, r1), ("sw", c1, r2), ("se", c2, r2),
                ("n", (c1 + c2) / 2, r1), ("s", (c1 + c2) / 2, r2),
                ("w", c1, (r1 + r2) / 2), ("e", c2, (r1 + r2) / 2)]
    if isinstance(geom, _g.CircleGeometry):
        return [("center", geom.center_col, geom.center_row),
                ("e", geom.center_col + geom.radius, geom.center_row),
                ("s", geom.center_col, geom.center_row + geom.radius)]
    if isinstance(geom, _g.EllipseGeometry):
        return [("center", geom.center_col, geom.center_row),
                ("e", geom.center_col + geom.radius2, geom.center_row),
                ("s", geom.center_col, geom.center_row + geom.radius1)]
    if isinstance(geom, (_g.LineGeometry, _g.ArrowGeometry)):
        return [("start", geom.col1, geom.row1), ("end", geom.col2, geom.row2)]
    if isinstance(geom, _g.AngleGeometry):
        return [("center", geom.center_col, geom.center_row),
                ("end1", geom.end1_col, geom.end1_row),
                ("end2", geom.end2_col, geom.end2_row)]
    if isinstance(geom, _g.CrossLineGeometry):
        return [("center", geom.center_col, geom.center_row)]
    if isinstance(geom, (_g.HottestSpotGeometry, _g.HotColdSpotsGeometry)):
        return [("nw", geom.col1, geom.row1), ("se", geom.col2, geom.row2)]
    if isinstance(geom, (_g.PolygonGeometry, _g.PolylineGeometry)):
        return [(f"v{i}", c, r) for i, (r, c) in enumerate(geom.points)]
    raise RoiValidationError(f"no handles for {type(geom).__name__}")


def move_geometry(geom, dcol: float, drow: float):
    """Translate by (dcol, drow); returns a new geometry object."""
    if isinstance(geom, _g.SpotGeometry):
        return _g.SpotGeometry(row=geom.row + drow, col=geom.col + dcol)
    if isinstance(geom, _g.NoteGeometry):
        return _g.NoteGeometry(row=geom.row + drow, col=geom.col + dcol,
                               text=geom.text)
    if isinstance(geom, _g.RectangleGeometry):
        return _g.RectangleGeometry(row1=geom.row1 + drow, col1=geom.col1 + dcol,
                                    row2=geom.row2 + drow, col2=geom.col2 + dcol)
    if isinstance(geom, _g.CircleGeometry):
        return _g.CircleGeometry(center_row=geom.center_row + drow,
                                 center_col=geom.center_col + dcol,
                                 radius=geom.radius)
    if isinstance(geom, _g.EllipseGeometry):
        return _g.EllipseGeometry(center_row=geom.center_row + drow,
                                  center_col=geom.center_col + dcol,
                                  radius1=geom.radius1, radius2=geom.radius2,
                                  phi=geom.phi)
    if isinstance(geom, _g.LineGeometry):
        return _g.LineGeometry(row1=geom.row1 + drow, col1=geom.col1 + dcol,
                               row2=geom.row2 + drow, col2=geom.col2 + dcol)
    if isinstance(geom, _g.ArrowGeometry):
        return _g.ArrowGeometry(row1=geom.row1 + drow, col1=geom.col1 + dcol,
                                row2=geom.row2 + drow, col2=geom.col2 + dcol)
    if isinstance(geom, _g.AngleGeometry):
        return _g.AngleGeometry(
            center_row=geom.center_row + drow, center_col=geom.center_col + dcol,
            end1_row=geom.end1_row + drow, end1_col=geom.end1_col + dcol,
            end2_row=geom.end2_row + drow, end2_col=geom.end2_col + dcol)
    if isinstance(geom, _g.CrossLineGeometry):
        return _g.CrossLineGeometry(center_row=geom.center_row + drow,
                                    center_col=geom.center_col + dcol,
                                    half_length_row=geom.half_length_row,
                                    half_length_col=geom.half_length_col)
    if isinstance(geom, _g.HottestSpotGeometry):
        return _g.HottestSpotGeometry(
            row1=geom.row1 + drow, col1=geom.col1 + dcol,
            row2=geom.row2 + drow, col2=geom.col2 + dcol)
    if isinstance(geom, _g.HotColdSpotsGeometry):
        return _g.HotColdSpotsGeometry(
            row1=geom.row1 + drow, col1=geom.col1 + dcol,
            row2=geom.row2 + drow, col2=geom.col2 + dcol,
            min_separation=geom.min_separation)
    if isinstance(geom, _g.PolygonGeometry):
        return _g.PolygonGeometry(
            points=tuple((r + drow, c + dcol) for r, c in geom.points))
    if isinstance(geom, _g.PolylineGeometry):
        return _g.PolylineGeometry(
            points=tuple((r + drow, c + dcol) for r, c in geom.points))
    raise RoiValidationError(f"cannot move {type(geom).__name__}")


def resize_with_handle(geom, handle_id: str, col: float, row: float):
    """Apply one handle drag; minimum-size rule enforced (no negative geometry)."""
    if handle_id == "center":
        for cls in (_g.SpotGeometry,):
            if isinstance(geom, cls):
                return cls(row=row, col=col)
        if isinstance(geom, _g.NoteGeometry):
            return _g.NoteGeometry(row=row, col=col, text=geom.text)
        if isinstance(geom, (_g.CircleGeometry, _g.EllipseGeometry,
                             _g.CrossLineGeometry)):
            return move_geometry(
                geom, col - _center_of(geom)[0], row - _center_of(geom)[1])
        if isinstance(geom, _g.AngleGeometry):
            dcol, drow = col - geom.center_col, row - geom.center_row
            return _g.AngleGeometry(
                center_row=geom.center_row + drow, center_col=geom.center_col + dcol,
                end1_row=geom.end1_row + drow, end1_col=geom.end1_col + dcol,
                end2_row=geom.end2_row + drow, end2_col=geom.end2_col + dcol)
        raise RoiValidationError(f"center handle unsupported for {type(geom).__name__}")
    if isinstance(geom, _g.RectangleGeometry):
        r1, c1, r2, c2 = geom.row1, geom.col1, geom.row2, geom.col2
        if "n" in handle_id:
            r1 = min(row, r2 - MIN_SIDE_PX)
        if "s" in handle_id:
            r2 = max(row, r1 + MIN_SIDE_PX)
        if "w" in handle_id:
            c1 = min(col, c2 - MIN_SIDE_PX)
        if "e" in handle_id:
            c2 = max(col, c1 + MIN_SIDE_PX)
        return _g.RectangleGeometry(row1=r1, col1=c1, row2=r2, col2=c2)
    if isinstance(geom, (_g.HottestSpotGeometry, _g.HotColdSpotsGeometry)):
        r1, c1, r2, c2 = geom.row1, geom.col1, geom.row2, geom.col2
        if handle_id == "nw":
            r1, c1 = min(row, r2 - MIN_SIDE_PX), min(col, c2 - MIN_SIDE_PX)
        else:
            r2, c2 = max(row, r1 + MIN_SIDE_PX), max(col, c1 + MIN_SIDE_PX)
        if isinstance(geom, _g.HottestSpotGeometry):
            return _g.HottestSpotGeometry(row1=r1, col1=c1, row2=r2, col2=c2)
        return _g.HotColdSpotsGeometry(row1=r1, col1=c1, row2=r2, col2=c2,
                                       min_separation=geom.min_separation)
    if isinstance(geom, _g.CircleGeometry):
        radius = max(MIN_SIDE_PX, math.hypot(row - geom.center_row,
                                             col - geom.center_col))
        return _g.CircleGeometry(center_row=geom.center_row,
                                 center_col=geom.center_col, radius=radius)
    if isinstance(geom, _g.EllipseGeometry):
        if handle_id == "e":
            radius2 = max(MIN_SIDE_PX, abs(col - geom.center_col))
            return _g.EllipseGeometry(center_row=geom.center_row,
                                      center_col=geom.center_col,
                                      radius1=geom.radius1, radius2=radius2,
                                      phi=geom.phi)
        radius1 = max(MIN_SIDE_PX, abs(row - geom.center_row))
        return _g.EllipseGeometry(center_row=geom.center_row,
                                  center_col=geom.center_col,
                                  radius1=radius1, radius2=geom.radius2,
                                  phi=geom.phi)
    if isinstance(geom, _g.LineGeometry):
        if handle_id == "start":
            return _g.LineGeometry(row1=row, col1=col, row2=geom.row2,
                                   col2=geom.col2)
        return _g.LineGeometry(row1=geom.row1, col1=geom.col1, row2=row,
                               col2=col)
    if isinstance(geom, _g.ArrowGeometry):
        if handle_id == "start":
            return _g.ArrowGeometry(row1=row, col1=col, row2=geom.row2,
                                    col2=geom.col2)
        return _g.ArrowGeometry(row1=geom.row1, col1=geom.col1, row2=row,
                                col2=col)
    if isinstance(geom, _g.AngleGeometry):
        if handle_id == "end1":
            return _g.AngleGeometry(center_row=geom.center_row,
                                    center_col=geom.center_col,
                                    end1_row=row, end1_col=col,
                                    end2_row=geom.end2_row,
                                    end2_col=geom.end2_col)
        if handle_id == "end2":
            return _g.AngleGeometry(center_row=geom.center_row,
                                    center_col=geom.center_col,
                                    end1_row=geom.end1_row,
                                    end1_col=geom.end1_col,
                                    end2_row=row, end2_col=col)
        raise RoiValidationError(f"unknown angle handle {handle_id!r}")
    if isinstance(geom, (_g.PolygonGeometry, _g.PolylineGeometry)):
        if not handle_id.startswith("v"):
            raise RoiValidationError(f"unknown vertex handle {handle_id!r}")
        try:
            index = int(handle_id[1:])
        except ValueError as exc:
            raise RoiValidationError(f"bad vertex handle {handle_id!r}") from exc
        pts = list(geom.points)
        if not 0 <= index < len(pts):
            raise RoiValidationError(f"vertex {index} out of range")
        pts[index] = (row, col)
        if isinstance(geom, _g.PolygonGeometry):
            return _g.PolygonGeometry(points=tuple(pts))
        return _g.PolylineGeometry(points=tuple(pts))
    raise RoiValidationError(
        f"cannot resize {type(geom).__name__} with {handle_id!r}")


def clamp_to_image(geom, width: int, height: int):
    """Keep geometry within [0,width-1]x[0,height-1]; never silently shrink below minimum."""
    def _clamp_col(c: float) -> float:
        return min(max(float(c), 0.0), float(width - 1))

    def _clamp_row(r: float) -> float:
        return min(max(float(r), 0.0), float(height - 1))

    if isinstance(geom, _g.SpotGeometry):
        return _g.SpotGeometry(row=_clamp_row(geom.row), col=_clamp_col(geom.col))
    if isinstance(geom, _g.NoteGeometry):
        return _g.NoteGeometry(row=_clamp_row(geom.row), col=_clamp_col(geom.col),
                               text=geom.text)
    if isinstance(geom, _g.RectangleGeometry):
        r1, r2 = sorted((_clamp_row(geom.row1), _clamp_row(geom.row2)))
        c1, c2 = sorted((_clamp_col(geom.col1), _clamp_col(geom.col2)))
        if r2 - r1 < MIN_SIDE_PX or c2 - c1 < MIN_SIDE_PX:
            return geom  # preserve rather than corrupt
        return _g.RectangleGeometry(row1=r1, col1=c1, row2=r2, col2=c2)
    # Center-based shapes: clamp the center, keep radii.
    if isinstance(geom, _g.CircleGeometry):
        return _g.CircleGeometry(center_row=_clamp_row(geom.center_row),
                                 center_col=_clamp_col(geom.center_col),
                                 radius=geom.radius)
    if isinstance(geom, _g.EllipseGeometry):
        return _g.EllipseGeometry(center_row=_clamp_row(geom.center_row),
                                  center_col=_clamp_col(geom.center_col),
                                  radius1=geom.radius1, radius2=geom.radius2,
                                  phi=geom.phi)
    if isinstance(geom, (_g.LineGeometry, _g.ArrowGeometry)):
        cls = type(geom)
        return cls(row1=_clamp_row(geom.row1), col1=_clamp_col(geom.col1),
                   row2=_clamp_row(geom.row2), col2=_clamp_col(geom.col2))
    return geom


def _center_of(geom) -> tuple[float, float]:
    if isinstance(geom, _g.CircleGeometry):
        return geom.center_col, geom.center_row
    if isinstance(geom, _g.EllipseGeometry):
        return geom.center_col, geom.center_row
    if isinstance(geom, _g.CrossLineGeometry):
        return geom.center_col, geom.center_row
    raise RoiValidationError(f"no center for {type(geom).__name__}")


__all__ = ["MIN_SIDE_PX", "handles_for", "move_geometry",
           "resize_with_handle", "clamp_to_image"]
