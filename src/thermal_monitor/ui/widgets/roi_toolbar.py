"""ui.widgets.roi_toolbar -- compact Camera Region analysis toolbar.

Lives INSIDE the Camera Region (directly above the image), not in the
application top bar. Groups mirror the ThermoView layout: Selection,
Spots, Lines, Areas, Measurements, Annotations. Emits tool changes;
drawing itself is handled by roi_interaction.
"""

from __future__ import annotations

from typing import Optional

try:
    from PyQt6.QtCore import pyqtSignal
    from PyQt6.QtWidgets import (
        QComboBox, QHBoxLayout, QLabel, QToolButton,
        QVBoxLayout, QWidget,
    )
    _HAS_PYQT6 = True
except ImportError:  # headless/test import
    _HAS_PYQT6 = False
    QWidget = object  # type: ignore

from thermal_monitor.roi.enums import RoiObjectType

TOOL_GROUPS: dict[str, list[tuple[str, RoiObjectType | None]]] = {
    "Select": [("Select", None)],
    "Spots": [("Spot", RoiObjectType.SPOT),
              ("Hottest Spot", RoiObjectType.HOTTEST_SPOT),
              ("Hot/Cold Spots", RoiObjectType.HOT_COLD_SPOTS)],
    "Lines": [("Free Line", RoiObjectType.FREE_LINE),
              ("Horizontal Line", RoiObjectType.HORIZONTAL_LINE),
              ("Vertical Line", RoiObjectType.VERTICAL_LINE),
              ("Polyline", RoiObjectType.POLYLINE),
              ("Cross Line", RoiObjectType.CROSS_LINE)],
    "Areas": [("Rectangle", RoiObjectType.RECTANGLE),
              ("Ellipse", RoiObjectType.ELLIPSE),
              ("Circle", RoiObjectType.CIRCLE),
              ("Polygon", RoiObjectType.POLYGON)],
    "Measure": [("Ruler", RoiObjectType.RULER),
                ("Horizontal Ruler", RoiObjectType.HORIZONTAL_RULER),
                ("Vertical Ruler", RoiObjectType.VERTICAL_RULER),
                ("Measure Line", RoiObjectType.MEASURE_LINE),
                ("Measure Angle", RoiObjectType.MEASURE_ANGLE)],
    "Annotate": [("Note", RoiObjectType.NOTE),
                 ("Arrow", RoiObjectType.ARROW),
                 ("Annotation Rectangle", RoiObjectType.ANNOTATION_RECTANGLE),
                 ("Annotation Ellipse", RoiObjectType.ANNOTATION_ELLIPSE)],
}

TOOLTIPS: dict[str, str] = {
    "Select": "Select, move, or resize an existing object (Esc clears selection)",
    "Spot": "Spot: click to measure temperature at a point",
    "Hottest Spot": "Hottest Spot: hottest point inside a search region",
    "Hot/Cold Spots": "Hot/Cold Spots: extrema inside a search region",
    "Free Line": "Free Line: temperature profile between two points",
    "Horizontal Line": "Horizontal Line: constrained to the image row axis",
    "Vertical Line": "Vertical Line: constrained to the image column axis",
    "Polyline": "Polyline: click vertices, double-click to finish, Esc to cancel",
    "Cross Line": "Cross Line: intersecting reference lines",
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
}


class RoiToolbar(QWidget if _HAS_PYQT6 else object):
    """Compact toolbar; disabled until a valid active position exists."""

    if _HAS_PYQT6:
        tool_changed = pyqtSignal(object)  # RoiObjectType | None (None = select)
        undo_requested = pyqtSignal()
        redo_requested = pyqtSignal()
        delete_requested = pyqtSignal()
        overlays_toggled = pyqtSignal(bool)

    def __init__(self, parent=None) -> None:
        if not _HAS_PYQT6:
            self._active_tool = None
            self._enabled = False
            return
        super().__init__(parent)
        self._active_tool: RoiObjectType | None = None
        self._combo_by_group: dict[str, QComboBox] = {}
        self._build()

    def _build(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)
        self._zoom_label = QLabel("Fit")
        self._zoom_label.setToolTip("View zoom (display only)")
        layout.addWidget(self._zoom_label)
        self._undo_btn = QToolButton()
        self._undo_btn.setText("Undo")
        self._undo_btn.setToolTip("Undo last edit in this position session")
        self._undo_btn.clicked.connect(self.undo_requested.emit)
        self._redo_btn = QToolButton()
        self._redo_btn.setText("Redo")
        self._redo_btn.setToolTip("Redo last undone edit")
        self._redo_btn.clicked.connect(self.redo_requested.emit)
        layout.addWidget(self._undo_btn)
        layout.addWidget(self._redo_btn)
        for group, tools in TOOL_GROUPS.items():
            combo = QComboBox()
            combo.setToolTip(group)
            for label, _ in tools:
                combo.addItem(label)
                combo.setItemData(combo.count() - 1, TOOLTIPS.get(label, ""), 256 + 1)
            combo.currentIndexChanged.connect(
                lambda _i, g=group: self._on_group_chosen(g))
            self._combo_by_group[group] = combo
            layout.addWidget(combo)
        self._delete_btn = QToolButton()
        self._delete_btn.setText("Delete")
        self._delete_btn.setToolTip("Delete the selected object (asks before bulk delete)")
        self._delete_btn.clicked.connect(self.delete_requested.emit)
        layout.addWidget(self._delete_btn)
        self._overlay_btn = QToolButton()
        self._overlay_btn.setText("Hide")
        self._overlay_btn.setCheckable(True)
        self._overlay_btn.setToolTip("Hide/show all overlays (geometry kept)")
        self._overlay_btn.toggled.connect(
            lambda checked: self.overlays_toggled.emit(not checked))
        layout.addWidget(self._overlay_btn)
        layout.addStretch(1)
        self._context_label = QLabel("No position")
        self._context_label.setToolTip("Active camera + PTZ + position context")
        layout.addWidget(self._context_label)
        self.set_context_active(False, "")

    def _on_group_chosen(self, group: str) -> None:
        combo = self._combo_by_group[group]
        label = combo.currentText()
        for text, object_type in TOOL_GROUPS[group]:
            if text == label:
                self.set_active_tool(object_type)
                return

    @property
    def active_tool(self) -> Optional[RoiObjectType]:
        return getattr(self, "_active_tool", None)

    def set_active_tool(self, tool: Optional[RoiObjectType]) -> None:
        self._active_tool = tool
        if _HAS_PYQT6:
            self.tool_changed.emit(tool)

    def select_tool(self, tool: Optional[RoiObjectType]) -> None:
        """Set the tool without re-emitting (e.g. after a finished drawing)."""
        self._active_tool = tool
        if _HAS_PYQT6 and tool is None:
            try:
                self._combo_by_group["Select"].setCurrentIndex(0)
            except Exception:
                pass

    def set_zoom_text(self, text: str) -> None:
        if _HAS_PYQT6:
            self._zoom_label.setText(text)

    def set_context_active(self, active: bool, text: str) -> None:
        self._context_active = active
        if not _HAS_PYQT6:
            return
        self._context_label.setText(text or ("Active" if active else "No position"))
        for combo in self._combo_by_group.values():
            combo.setEnabled(active)
        self._delete_btn.setEnabled(active)
        self._undo_btn.setEnabled(active)
        self._redo_btn.setEnabled(active)

    def set_undo_redo(self, can_undo: bool, can_redo: bool) -> None:
        if _HAS_PYQT6:
            self._undo_btn.setEnabled(can_undo and getattr(self, "_context_active", False))
            self._redo_btn.setEnabled(can_redo and getattr(self, "_context_active", False))


__all__ = ["RoiToolbar", "TOOL_GROUPS", "TOOLTIPS"]
