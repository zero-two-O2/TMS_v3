"""ui.widgets.roi_icons -- industrial SVG toolbar icons for ROI tools.

Primary source: local vector assets in
``ui/resources/icons/<key>.svg`` (original line-art authored for this
application, 24x24 viewBox, consistent 1.8px stroke). QtSvg renders
them crisply at any toolbar size.

Fallback: if an asset is missing or QtSvg cannot load it, the legacy
QPainter 16x16 glyph is drawn so the toolbar never shows a blank
button. ``icon_for`` keeps its signature; results are cached per key.

Key space: RoiObjectType values plus the action keys used by
``roi_toolbar`` ("select", "profile", "undo", "redo", "delete",
"hide"). See ICON_KEYS.
"""

from __future__ import annotations

from pathlib import Path

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
    _HAS_PYQT6 = True
except ImportError:
    _HAS_PYQT6 = False

from thermal_monitor.roi.enums import RoiObjectType

_ICON_DIR = Path(__file__).resolve().parent.parent / "resources" / "icons"

#: Ink remap for dark toolbar backgrounds (the application default
#: theme is industrial_dark). Accent colors are kept: they read on
#: both dark and light surfaces.
_LIGHT_INK_MAP = (
    (b"#1F2329", b"#ECEFF3"),
    (b"#4A545E", b"#A8B2BC"),
    (b"#5B6670", b"#A8B2BC"),
)

#: Every icon key the toolbar may request. The test suite asserts an
#: asset exists for each of these.
ICON_KEYS: tuple[str, ...] = tuple(
    [t.value for t in RoiObjectType]
    + ["select", "profile", "undo", "redo", "delete", "hide"]
)

_CACHE: dict[str, object] = {}

_INK = (35, 39, 44)
_ACCENT = (84, 110, 122)
_WARM = (200, 90, 40)
_COOL = (40, 120, 200)


def icon_path(key: str) -> Path:
    """Filesystem asset for an icon key (may not exist; see fallback)."""
    safe = "".join(c for c in key if c.isalnum() or c in ("_", "-"))
    return _ICON_DIR / f"{safe}.svg"


def icon_source(key: str, ink: str = "light") -> str:
    """Where ``icon_for(key)`` loaded from: 'svg', 'fallback', or 'none'."""
    if not _HAS_PYQT6:
        return "none"
    if _svg_icon(key, ink) is not None:
        return "svg"
    return "fallback"


def _svg_icon(key: str, ink: str):  # noqa: ANN201
    """QIcon from the SVG asset, or None when unavailable/invalid.

    The icon carries Normal/Off (toolbar ink) and Normal/On (light ink
    for the blue checked/selected background, which is blue in every
    theme) pixmaps. Qt selects the On variant automatically for checked
    buttons. Disabled rendering is left to Qt's automatic style.
    """
    from PyQt6.QtGui import QIcon as _QIcon

    path = icon_path(key)
    if not path.is_file():
        return None
    if ink == "dark":
        # NOTE: availableSizes() is empty for scalable SVG icons even
        # when they load fine; non-null is the correct gate.
        candidate = _QIcon(str(path))
        if candidate.isNull():
            return None
        _add_on_state(candidate, key)
        return candidate
    master = _render_light_master(key)
    if master is None:
        return None
    icon = _QIcon(master)
    if icon.isNull():
        return None
    _add_on_state(icon, key)
    return icon


def _render_light_master(key: str):
    """192px light-ink master pixmap, or None. Shared by both paths so
    the checked-state pixmap never depends on cache ordering."""
    from PyQt6.QtGui import QPixmap as _QPixmap
    from PyQt6.QtCore import Qt as _Qt

    try:
        data = icon_path(key).read_bytes()
    except OSError:
        return None
    for old, new in _LIGHT_INK_MAP:
        data = data.replace(old, new)
    from PyQt6.QtCore import QByteArray
    from PyQt6.QtSvg import QSvgRenderer
    renderer = QSvgRenderer(QByteArray(data))
    if not renderer.isValid():
        return None
    # Master at 192px: QIcon downscales to toolbar sizes from solid
    # cores instead of a pre-blurred small raster (crisper small icons,
    # undiluted ink colors).
    pixmap = _QPixmap(192, 192)
    pixmap.fill(_Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter)
    finally:
        painter.end()
    return pixmap


def _add_on_state(icon, key: str) -> None:
    """Attach the checked-state (blue background) light-ink pixmap."""
    from PyQt6.QtCore import Qt as _Qt
    from PyQt6.QtGui import QIcon as _QIcon

    master = _render_light_master(key)
    if master is None:
        return
    for size in (16, 20, 24, 28, 32, 40, 48):
        scaled = master.scaled(
            size, size, _Qt.AspectRatioMode.KeepAspectRatio,
            _Qt.TransformationMode.SmoothTransformation)
        if not scaled.isNull():
            icon.addPixmap(scaled, _QIcon.Mode.Normal, _QIcon.State.On)


def dropdown_icon_for(key: str, ink: str = "light",
                      arrow_zone: int = 12):  # noqa: ANN201
    """QIcon for MenuButtonPopup buttons with a reserved arrow zone.

    Qt centers the icon pixmap in the full button rect (verified
    empirically), so the zone is composed into the pixmap itself: 40px
    artwork left-aligned + transparent arrow strip on the right. The
    style-drawn indicator then paints only over transparent pixels.
    Both Off (toolbar ink) and On (light ink, blue checked background)
    states are composed. Cached per (key, ink, arrow_zone).
    """
    from PyQt6.QtGui import QIcon as _QIcon
    from PyQt6.QtGui import QPixmap as _QPixmap
    from PyQt6.QtCore import Qt as _Qt

    cache_key = ("dropdown", key, ink, arrow_zone)
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    base = icon_for(key, ink=ink)
    art = base.pixmap(40, 40)
    composed = _QPixmap(40 + arrow_zone, 40)
    composed.fill(_Qt.GlobalColor.transparent)
    painter = QPainter(composed)
    try:
        painter.drawPixmap(0, 0, art)
    finally:
        painter.end()
    icon = _QIcon(composed)
    on_art = base.pixmap(
        40, 40, mode=_QIcon.Mode.Normal, state=_QIcon.State.On)
    if not on_art.isNull():
        on_composed = _QPixmap(40 + arrow_zone, 40)
        on_composed.fill(_Qt.GlobalColor.transparent)
        painter = QPainter(on_composed)
        try:
            painter.drawPixmap(0, 0, on_art)
        finally:
            painter.end()
        icon.addPixmap(on_composed, _QIcon.Mode.Normal, _QIcon.State.On)
    _CACHE[cache_key] = icon
    return icon


def icon_for(key: str, ink: str = "light"):  # noqa: ANN201
    """QIcon for a tool key (object-type value or action name). Cached.

    ``ink`` selects the toolbar background: "light" (default, for the
    application's dark themes) recolours the asset's ink; "dark" loads
    the file as authored for light surfaces.
    """
    if not _HAS_PYQT6:
        return None
    cache_key = (key, ink)
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    icon = _svg_icon(key, ink)
    if icon is None:
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            _draw(painter, key)
        finally:
            painter.end()
        icon = QIcon(pixmap)
    _CACHE[cache_key] = icon
    return icon


def _pen(color, width: float = 1.6, dashed: bool = False) -> "QPen":
    pen = QPen(QColor(*color), width)
    if dashed:
        pen.setStyle(Qt.PenStyle.DashLine)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    return pen


def _draw(p: "QPainter", key: str) -> None:
    """Legacy fallback glyphs (used only when an SVG asset is missing)."""
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


__all__ = ["ICON_KEYS", "dropdown_icon_for", "icon_for", "icon_path",
           "icon_source"]
