"""ui.widgets.roi_toolbar -- compact Camera Region analysis toolbar.

Lives INSIDE the Camera Region (directly above the image), not in the
application top bar: a single compact row of icon buttons with dropdown
menus, mirroring the ThermoView grouping (Select / Spots / Lines /
Regions / Measurements / Annotations). No text labels on buttons;
tooltips and accessible names carry the full tool names.

Emits tool changes; drawing itself is handled by roi_interaction +
roi.editor. Only QToolButton/QToolButton menus are used here (never
QPushButton), preserving the configuration center's no-button chrome.
"""

from __future__ import annotations

from typing import Optional

try:
    from PyQt6.QtCore import QSize, pyqtSignal
    from PyQt6.QtWidgets import (
        QHBoxLayout, QLabel, QMenu, QToolButton, QWidget,
    )
    _HAS_PYQT6 = True
except ImportError:  # headless/test import
    _HAS_PYQT6 = False
    QWidget = object  # type: ignore

from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.ui.widgets.roi_icons import dropdown_icon_for, icon_for

#: Uniform icon/button geometry for the industrial toolbar. SVG assets
#: render crisply at any size.
TOOL_ICON_SIZE = 35
TOOL_BUTTON_SIZE = 35
#: Dropdown buttons reserve a dedicated arrow zone beside the icon so
#: the style-drawn menu indicator never overlaps the artwork. Height
#: and spacing are unchanged; only menu buttons grow horizontally.
MENU_BUTTON_WIDTH = 50
MENU_ARROW_ZONE = 10


def ink_for_surface(surface_hex: str) -> str:
    """Icon ink for a toolbar surface color (luminance rule).

    Dark surfaces (all dark themes) take light ink; light surfaces take
    dark ink. Unknown input falls back to light ink (dark default theme).
    """
    text = str(surface_hex or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    try:
        red, green, blue = (int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except (ValueError, IndexError):
        return "light"

    def linear(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    luminance = (0.2126 * linear(red) + 0.7152 * linear(green)
                 + 0.0722 * linear(blue))
    return "dark" if luminance > 0.4 else "light"


def default_toolbar_ink() -> str:
    """Ink matching the live application stylesheet, if any.

    Reads the QWidget background-color from the QApplication
    stylesheet (written by ThemeManager.apply). Falls back to light
    ink (dark default theme) when no stylesheet is installed, e.g. in
    headless unit tests.
    """
    import re

    try:
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        sheet = app.styleSheet() if app is not None else ""
    except Exception:
        return "light"
    match = re.search(
        r"QWidget\s*\{[^}]*background-color:\s*(#[0-9A-Fa-f]{3,6})", sheet)
    if not match:
        return "light"
    return ink_for_surface(match.group(1))

# Group -> [(menu label, object type or None, icon key, profile flag)].
# None object type = Select (no drawing). "profile" entries arm a free
# line whose analysis_config requests profile display.
TOOL_GROUPS: dict[str, list[tuple[str, object, str, bool]]] = {
    "Select": [("Select", None, "select", False)],
    "Spots": [("Spot", RoiObjectType.SPOT, RoiObjectType.SPOT.value, False),
              ("Hottest Spot", RoiObjectType.HOTTEST_SPOT,
               RoiObjectType.HOTTEST_SPOT.value, False),
              ("Coldest Spot", RoiObjectType.COLDEST_SPOT,
               RoiObjectType.COLDEST_SPOT.value, False),
              ("Hot/Cold Spots", RoiObjectType.HOT_COLD_SPOTS,
               RoiObjectType.HOT_COLD_SPOTS.value, False)],
    "Lines": [("Free Line", RoiObjectType.FREE_LINE,
               RoiObjectType.FREE_LINE.value, False),
              ("Horizontal Line", RoiObjectType.HORIZONTAL_LINE,
               RoiObjectType.HORIZONTAL_LINE.value, False),
              ("Vertical Line", RoiObjectType.VERTICAL_LINE,
               RoiObjectType.VERTICAL_LINE.value, False),
              ("Polyline", RoiObjectType.POLYLINE,
               RoiObjectType.POLYLINE.value, False),
              ("Cross Line", RoiObjectType.CROSS_LINE,
               RoiObjectType.CROSS_LINE.value, False),
              ("On-Image Profile", RoiObjectType.FREE_LINE, "profile", True)],
    "Regions": [("Rectangle", RoiObjectType.RECTANGLE,
                 RoiObjectType.RECTANGLE.value, False),
                ("Ellipse", RoiObjectType.ELLIPSE,
                 RoiObjectType.ELLIPSE.value, False),
                ("Circle", RoiObjectType.CIRCLE,
                 RoiObjectType.CIRCLE.value, False),
                ("Polygon", RoiObjectType.POLYGON,
                 RoiObjectType.POLYGON.value, False)],
    "Measure": [("Ruler", RoiObjectType.RULER,
                 RoiObjectType.RULER.value, False),
                ("Horizontal Ruler", RoiObjectType.HORIZONTAL_RULER,
                 RoiObjectType.HORIZONTAL_RULER.value, False),
                ("Vertical Ruler", RoiObjectType.VERTICAL_RULER,
                 RoiObjectType.VERTICAL_RULER.value, False),
                ("Measure Line", RoiObjectType.MEASURE_LINE,
                 RoiObjectType.MEASURE_LINE.value, False),
                ("Measure Angle", RoiObjectType.MEASURE_ANGLE,
                 RoiObjectType.MEASURE_ANGLE.value, False)],
    "Annotate": [("Note", RoiObjectType.NOTE,
                  RoiObjectType.NOTE.value, False),
                 ("Arrow", RoiObjectType.ARROW,
                  RoiObjectType.ARROW.value, False),
                 ("Annotation Rectangle",
                  RoiObjectType.ANNOTATION_RECTANGLE,
                  RoiObjectType.ANNOTATION_RECTANGLE.value, False),
                 ("Annotation Ellipse", RoiObjectType.ANNOTATION_ELLIPSE,
                  RoiObjectType.ANNOTATION_ELLIPSE.value, False)],
}

TOOLTIPS: dict[str, str] = {
    "Select": "Select, move, or resize an existing object (Esc clears selection)",
    "Spot": "Spot: click to measure temperature at a point",
    "Hottest Spot": "Hottest Spot: hottest point inside a search region",
    "Coldest Spot": "Coldest Spot: coldest point inside a search region",
    "Hot/Cold Spots": "Hot/Cold Spots: extrema inside a search region",
    "Free Line": "Free Line: temperature profile between two points",
    "Horizontal Line": "Horizontal Line: constrained to the image row axis",
    "Vertical Line": "Vertical Line: constrained to the image column axis",
    "Polyline": "Polyline: click vertices, double-click to finish, Esc to cancel",
    "Cross Line": "Cross Line: intersecting reference lines",
    "On-Image Profile": "On-Image Profile: free line with profile readout",
    "Rectangle": "Rectangle: drag to create an area ROI",
    "Ellipse": "Ellipse: drag to create an area ROI",
    "Circle": "Circle: drag to create an area ROI",
    "Polygon": "Polygon: click vertices, double-click to finish, Esc to cancel",
    "Ruler": "Ruler: pixel distance (physical units need calibration)",
    "Horizontal Ruler": "Horizontal Ruler: pixel distance along image x",
    "Vertical Ruler": "Vertical Ruler: pixel distance along image y",
    "Measure Line": "Measure Line: pixel distance between two points",
    "Measure Angle": "Measure Angle: angle between two segments",
    "Note": "Note: text annotation bound to this camera + position",
    "Arrow": "Arrow: pointer annotation bound to this camera + position",
    "Annotation Rectangle": "Annotation Rectangle: visual only, no thermal stats",
    "Annotation Ellipse": "Annotation Ellipse: visual only, no thermal stats",
    "Undo": "Undo last edit in this position session",
    "Redo": "Redo last undone edit",
    "Delete": "Delete the selected object (asks before bulk delete)",
    "Hide": "Hide/show all overlays (geometry kept)",
}


class RoiToolbar(QWidget if _HAS_PYQT6 else object):
    """Compact icon toolbar; disabled until a valid active position exists."""

    if _HAS_PYQT6:
        tool_changed = pyqtSignal(object)  # RoiObjectType | None (None = select)
        undo_requested = pyqtSignal()
        redo_requested = pyqtSignal()
        delete_requested = pyqtSignal()
        overlays_toggled = pyqtSignal(bool)

    def __init__(self, parent=None, *, ink: str = "auto") -> None:
        resolved = (default_toolbar_ink() if ink == "auto" else ink)
        if not _HAS_PYQT6:
            self._active_tool = None
            self._profile_armed = False
            self._enabled = False
            self._ink = resolved
            return
        super().__init__(parent)
        # "auto" follows the live application stylesheet; explicit
        # "light"/"dark" pins the ink for fixed surfaces.
        self._ink = resolved if resolved in ("light", "dark") else "light"
        self._active_tool: RoiObjectType | None = None
        self._profile_armed = False
        self._group_buttons: dict[str, QToolButton] = {}
        self._menu_actions: dict[str, list] = {}
        self._last_entry: dict[str, tuple] = {}
        self._build()

    # -- construction ----------------------------------------------------
    def _build(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 1, 2, 1)
        layout.setSpacing(1)
        self._zoom_label = QLabel("Fit")
        self._zoom_label.setToolTip("View zoom (display only)")
        layout.addWidget(self._zoom_label)
        self._undo_btn = self._action_button("Undo", "undo", self.undo_requested.emit)
        self._redo_btn = self._action_button("Redo", "redo", self.redo_requested.emit)
        layout.addWidget(self._undo_btn)
        layout.addWidget(self._redo_btn)
        for group in ("Select", "Spots", "Lines", "Regions", "Measure",
                      "Annotate"):
            button = self._group_button(group)
            self._group_buttons[group] = button
            self._last_entry[group] = TOOL_GROUPS[group][0]
            layout.addWidget(button)
        self._delete_btn = self._action_button(
            "Delete", "delete", self.delete_requested.emit)
        layout.addWidget(self._delete_btn)
        self._overlay_btn = QToolButton(self)
        self._overlay_btn.setIcon(icon_for("hide", ink=self._ink))
        self._overlay_btn.setIconSize(QSize(TOOL_ICON_SIZE, TOOL_ICON_SIZE))
        self._overlay_btn.setFixedSize(
            QSize(TOOL_BUTTON_SIZE, TOOL_BUTTON_SIZE))
        self._overlay_btn.setToolTip(TOOLTIPS["Hide"])
        self._overlay_btn.setAccessibleName("Hide overlays")
        self._overlay_btn.setCheckable(True)
        self._overlay_btn.setChecked(True)
        self._overlay_btn.toggled.connect(
            lambda checked: self.overlays_toggled.emit(checked))
        layout.addWidget(self._overlay_btn)
        layout.addStretch(1)
        self._context_label = QLabel("No position")
        self._context_label.setToolTip("Active camera + PTZ + position context")
        layout.addWidget(self._context_label)
        self._sync_checks()
        self.set_context_active(False, "")

    def _action_button(self, name: str, icon_key: str, slot) -> "QToolButton":
        button = QToolButton(self)
        button.setIcon(icon_for(icon_key, ink=self._ink))
        button.setIconSize(QSize(TOOL_ICON_SIZE, TOOL_ICON_SIZE))
        button.setFixedSize(QSize(TOOL_BUTTON_SIZE, TOOL_BUTTON_SIZE))
        button.setToolTip(TOOLTIPS[name])
        button.setAccessibleName(name)
        button.clicked.connect(slot)
        return button

    def _group_button(self, group: str) -> "QToolButton":
        label, object_type, icon_key, _ = TOOL_GROUPS[group][0]
        button = QToolButton(self)
        button.setCheckable(True)
        button.setIconSize(QSize(TOOL_ICON_SIZE, TOOL_ICON_SIZE))
        button.setToolTip(group if group != "Select" else TOOLTIPS["Select"])
        button.setAccessibleName(group)
        if group == "Select":
            button.setIcon(icon_for(icon_key, ink=self._ink))
            button.setIconSize(QSize(TOOL_ICON_SIZE, TOOL_ICON_SIZE))
            button.setFixedSize(QSize(TOOL_BUTTON_SIZE, TOOL_BUTTON_SIZE))
            button.clicked.connect(lambda: self.set_active_tool(None))
            return button
        menu = QMenu(button)
        entries = []
        for entry_label, entry_type, entry_icon, profile in TOOL_GROUPS[group]:
            action = menu.addAction(
                icon_for(entry_icon, ink=self._ink), entry_label)
            action.setToolTip(TOOLTIPS.get(entry_label, ""))
            # NOTE: QAction carries its accessible name via its text in Qt6
            # (no setAccessibleName API); the toolbar button itself has one.
            action.triggered.connect(
                lambda _c, g=group, t=entry_type, p=profile:
                self._choose(g, t, profile))
            entries.append((action, entry_icon))
        self._menu_actions[group] = entries
        button.setMenu(menu)
        from PyQt6.QtWidgets import QToolButton as _QTB
        button.setPopupMode(_QTB.ToolButtonPopupMode.MenuButtonPopup)
        # Dedicated arrow zone composed into the pixmap itself (Qt
        # centers the icon in the full button rect, verified
        # empirically): 40px artwork left-aligned, transparent strip on
        # the right where the style draws the indicator. Width grows;
        # height and spacing are unchanged.
        button.setIcon(dropdown_icon_for(
            icon_key, ink=self._ink, arrow_zone=MENU_ARROW_ZONE))
        button.setIconSize(QSize(MENU_BUTTON_WIDTH, TOOL_BUTTON_SIZE))
        button.setFixedSize(QSize(MENU_BUTTON_WIDTH, TOOL_BUTTON_SIZE))
        button.clicked.connect(lambda: self._rearm(group))
        return button

    # -- behavior ----------------------------------------------------------
    def _choose(self, group: str, tool, profile: bool) -> None:
        entries = TOOL_GROUPS[group]
        for entry in entries:
            if entry[1] is tool and entry[3] == profile:
                self._last_entry[group] = entry
                break
        self._group_buttons[group].setIcon(
            dropdown_icon_for(self._last_entry[group][2], ink=self._ink,
                              arrow_zone=MENU_ARROW_ZONE))
        self.set_active_tool(tool, profile=profile)

    def _rearm(self, group: str) -> None:
        _label, tool, _icon, profile = self._last_entry[group]
        self.set_active_tool(tool, profile=profile)

    @property
    def active_tool(self) -> Optional[RoiObjectType]:
        return getattr(self, "_active_tool", None)

    @property
    def profile_armed(self) -> bool:
        return getattr(self, "_profile_armed", False)

    def set_active_tool(self, tool: Optional[RoiObjectType],
                        *, profile: bool = False) -> None:
        self._active_tool = tool
        self._profile_armed = bool(profile)
        if _HAS_PYQT6:
            self._sync_checks()
            self.tool_changed.emit(tool)

    def select_tool(self, tool: Optional[RoiObjectType]) -> None:
        """Set the tool without re-emitting (e.g. after a finished drawing)."""
        self._active_tool = tool
        self._profile_armed = False
        if _HAS_PYQT6:
            self._sync_checks()

    def _sync_checks(self) -> None:
        for group, button in self._group_buttons.items():
            if group == "Select":
                button.setChecked(self._active_tool is None)
                continue
            tools = [entry[1] for entry in TOOL_GROUPS[group]]
            button.setChecked(self._active_tool in tools)

    def set_zoom_text(self, text: str) -> None:
        if _HAS_PYQT6:
            self._zoom_label.setText(text)

    def set_context_active(self, active: bool, text: str) -> None:
        self._context_active = active
        if not _HAS_PYQT6:
            return
        self._context_label.setText(text or ("Active" if active else "No position"))
        for button in list(self._group_buttons.values()) + [
                self._delete_btn, self._undo_btn, self._redo_btn]:
            button.setEnabled(active)

    def set_undo_redo(self, can_undo: bool, can_redo: bool) -> None:
        if _HAS_PYQT6:
            active = getattr(self, "_context_active", False)
            self._undo_btn.setEnabled(can_undo and active)
            self._redo_btn.setEnabled(can_redo and active)

    # -- theme ------------------------------------------------------------
    @property
    def ink(self) -> str:
        """Current icon ink ('light' for dark surfaces, else 'dark')."""
        return getattr(self, "_ink", "light")

    def refresh_theme_icons(self, surface_hex: str) -> str:
        """Re-resolve every icon for a toolbar surface color.

        Called by the theme switch funnel (and tests). Returns the ink
        in use. Safe while a popup menu is open: only QIcon payloads
        are replaced, no widget is recreated.
        """
        self._ink = ink_for_surface(surface_hex)
        if not _HAS_PYQT6:
            return self._ink
        for name, icon_key in (("_undo_btn", "undo"), ("_redo_btn", "redo"),
                               ("_delete_btn", "delete"),
                               ("_overlay_btn", "hide")):
            getattr(self, name).setIcon(icon_for(icon_key, ink=self._ink))
        for group, button in self._group_buttons.items():
            entry = self._last_entry.get(group)
            if entry is not None:
                if group == "Select":
                    button.setIcon(icon_for(entry[2], ink=self._ink))
                else:
                    button.setIcon(dropdown_icon_for(
                        entry[2], ink=self._ink, arrow_zone=MENU_ARROW_ZONE))
            for action, icon_key in self._menu_actions.get(group, []):
                try:
                    action.setIcon(icon_for(icon_key, ink=self._ink))
                except RuntimeError:
                    pass
        return self._ink


__all__ = ["RoiToolbar", "TOOL_GROUPS", "TOOLTIPS",
           "TOOL_ICON_SIZE", "TOOL_BUTTON_SIZE",
           "MENU_BUTTON_WIDTH", "MENU_ARROW_ZONE", "ink_for_surface"]
