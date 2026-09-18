"""ui.widgets.roi_icons -- programmatic toolbar icons for ROI tools.

No icon library is vendored (the project has none); glyphs are drawn
with QPainter on 16x16 pixmaps, cached per key. Each icon has a tooltip
and accessible name supplied by the toolbar.
"""

from __future__ import annotations

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
    _HAS_PYQT6 = True
except ImportError:
    _HAS_PYQT6 = False

from thermal_monitor.roi.enums import RoiObjectType

_CACHE: dict[str, object] = {}

_INK = (35, 39, 44)
_ACCENT = (84, 110, 122)
_WARM = (200, 90, 40)
_COOL = (40, 120, 200)


def icon_for(key: str):  # noqa: ANN201
    """QIcon for a tool key (object-type value or action name). Cached."""
    if not _HAS_PYQT6:
        return None
    if key in _CACHE:
        return _CACHE[key]
    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _draw(painter, key)
    finally:
        painter.end()
    icon = QIcon(pixmap)
    _CACHE[key] = icon
    return icon


def _pen(color, width: float = 1.6, dashed: bool = False) -> "QPen":
    pen = QPen(QColor(*color), width)
    if dashed:
        pen.setStyle(Qt.PenStyle.DashLine)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    return pen


def _draw(p: "QPainter", key: str) -> None:
    ink = _pen(_INK)
    p.setPen(ink)
    p.setBrush(Qt.BrushStyle.NoBrush)

    if key == "undo":
        p.drawArc(3, 4, 10, 9, 40 * 16, 220 * 16)
        p.drawLine(3, 4, 3, 8)
        p.drawLine(3, 4, 7, 4)
    elif key == "redo":
        p.drawArc(3, 4, 10, 9, -40 * 16, -220 * 16)
        p.drawLine(13, 4, 13, 8)
        p.drawLine(13, 4, 9, 4)
    elif key == "select":
        p.setBrush(QColor(*_INK))
        from PyQt6.QtGui import QPolygon
        from PyQt6.QtCore import QPoint
        p.drawPolygon(QPolygon([QPoint(4, 2), QPoint(4, 13), QPoint(7, 10),
                                QPoint(9, 14), QPoint(11, 13), QPoint(9, 9),
                                QPoint(12, 9)]))
    elif key == "delete":
        p.setPen(_pen((180, 60, 50), 2.0))
        p.drawLine(4, 4, 12, 12)
        p.drawLine(12, 4, 4, 12)
    elif key == "hide":
        p.drawEllipse(2, 5, 12, 6)
        p.setBrush(QColor(*_INK))
        p.drawEllipse(6, 6, 4, 4)
    elif key in (RoiObjectType.SPOT.value, "spot"):
        p.setPen(_pen(_WARM, 1.6))
        p.drawLine(8, 2, 8, 14)
        p.drawLine(2, 8, 14, 8)
        p.setBrush(QColor(*_WARM))
        p.drawEllipse(6, 6, 4, 4)
    elif key in (RoiObjectType.HOTTEST_SPOT.value,):
        p.setPen(_pen(_WARM, 1.8))
        p.drawLine(4, 11, 8, 5)
        p.drawLine(8, 5, 8, 9)
        p.drawLine(8, 5, 12, 5)
        p.drawLine(8, 11, 12, 11)
        p.drawLine(8, 11, 8, 13)
    elif key in (RoiObjectType.COLDEST_SPOT.value,):
        p.setPen(_pen(_COOL, 1.8))
        p.drawLine(4, 5, 8, 11)
        p.drawLine(8, 5, 12, 5)
        p.drawLine(8, 5, 8, 3)
        p.drawLine(8, 11, 12, 11)
        p.drawLine(8, 11, 8, 7)
    elif key in (RoiObjectType.HOT_COLD_SPOTS.value,):
        p.setBrush(QColor(*_WARM))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(3, 3, 4, 4)
        p.setBrush(QColor(*_COOL))
        p.drawEllipse(9, 9, 4, 4)
    elif key in (RoiObjectType.FREE_LINE.value, RoiObjectType.MEASURE_LINE.value,
                 RoiObjectType.RULER.value, "profile"):
        p.drawLine(3, 13, 13, 3)
        if key == RoiObjectType.RULER.value:
            p.drawLine(3, 13, 5, 11)
            p.drawLine(13, 3, 11, 5)
        if key == "profile":
            p.setPen(_pen(_ACCENT, 1.2))
            p.drawLine(3, 13, 6, 9)
            p.drawLine(6, 9, 9, 11)
            p.drawLine(9, 11, 13, 6)
    elif key in (RoiObjectType.HORIZONTAL_LINE.value,
                 RoiObjectType.HORIZONTAL_RULER.value):
        p.drawLine(2, 8, 14, 8)
        p.drawLine(2, 8, 4, 6)
        p.drawLine(2, 8, 4, 10)
        p.drawLine(14, 8, 12, 6)
        p.drawLine(14, 8, 12, 10)
    elif key in (RoiObjectType.VERTICAL_LINE.value,
                 RoiObjectType.VERTICAL_RULER.value):
        p.drawLine(8, 2, 8, 14)
        p.drawLine(8, 2, 6, 4)
        p.drawLine(8, 2, 10, 4)
        p.drawLine(8, 14, 6, 12)
        p.drawLine(8, 14, 10, 12)
    elif key == RoiObjectType.POLYLINE.value:
        p.drawLine(2, 12, 6, 5)
        p.drawLine(6, 5, 10, 10)
        p.drawLine(10, 10, 14, 4)
    elif key == RoiObjectType.CROSS_LINE.value:
        p.drawLine(8, 2, 8, 14)
        p.drawLine(2, 8, 14, 8)
    elif key in (RoiObjectType.RECTANGLE.value,
                 RoiObjectType.ANNOTATION_RECTANGLE.value):
        if "annotation" in key:
            p.setPen(_pen(_ACCENT, 1.4, dashed=True))
        p.drawRect(3, 4, 10, 8)
    elif key in (RoiObjectType.ELLIPSE.value,
                 RoiObjectType.ANNOTATION_ELLIPSE.value):
        if "annotation" in key:
            p.setPen(_pen(_ACCENT, 1.4, dashed=True))
        p.drawEllipse(2, 4, 12, 8)
    elif key == RoiObjectType.CIRCLE.value:
        p.drawEllipse(3, 3, 10, 10)
    elif key == RoiObjectType.POLYGON.value:
        from PyQt6.QtGui import QPolygon
        from PyQt6.QtCore import QPoint
        p.drawPolygon(QPolygon([QPoint(8, 2), QPoint(13, 8), QPoint(10, 14),
                                QPoint(4, 13), QPoint(3, 6)]))
    elif key == RoiObjectType.MEASURE_ANGLE.value:
        p.drawLine(3, 13, 3, 4)
        p.drawLine(3, 13, 13, 13)
        p.drawArc(3, 8, 5, 5, 0, -90 * 16)
    elif key == RoiObjectType.NOTE.value:
        p.drawRect(3, 2, 10, 12)
        p.setPen(_pen(_ACCENT, 1.2))
        p.drawLine(5, 6, 11, 6)
        p.drawLine(5, 9, 11, 9)
        p.drawLine(5, 12, 9, 12)
    elif key in (RoiObjectType.ARROW.value,):
        p.drawLine(3, 13, 12, 4)
        p.drawLine(12, 4, 12, 8)
        p.drawLine(12, 4, 8, 4)
    else:
        p.drawRect(4, 4, 8, 8)


__all__ = ["icon_for"]
