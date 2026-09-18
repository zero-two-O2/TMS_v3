"""ui.widgets.camera_region -- Camera Region: image + ROI toolbar + overlays.

The Camera Region is the visual unit owning the displayed image: the
existing ``LiveThermalWidget`` renderer plus the Phase 10 ROI toolbar
docked directly above it. Rendering, pan/zoom, IR/VL handling, and the
latest-frame-wins path are unchanged; this widget only adds:

- compact toolbar (tool selection, undo/redo, delete, overlay toggle)
- position-scoped overlay publication (only the active context paints)
- mouse drawing/selection forwarded to RoiInteractionState in image
  coordinates (widget coords converted via the shared view transform)

Live/Observer tiles keep their existing behavior: overlays are
read-only there unless a controller explicitly enables editing.
"""

from __future__ import annotations

from typing import Callable, Optional

try:
    from PyQt6.QtCore import Qt, pyqtSignal
    from PyQt6.QtWidgets import QVBoxLayout, QWidget
    _HAS_PYQT6 = True
except ImportError:
    _HAS_PYQT6 = False
    QWidget = object  # type: ignore

from thermal_monitor.roi.context import RoiActiveContext
from thermal_monitor.roi.coordinate_system import ViewportMapping
from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.models import RoiDefinition
from thermal_monitor.ui.widgets.roi_interaction import EditCommand, RoiInteractionState
from thermal_monitor.ui.widgets.roi_overlay import (
    RoiOverlayItem, RoiOverlaySet, overlay_color,
)
from thermal_monitor.ui.widgets.roi_toolbar import RoiToolbar


class CameraRegion(QWidget if _HAS_PYQT6 else object):
    """Image + toolbar composite for Configuration (editable) and Live (read-only)."""

    if _HAS_PYQT6:
        roi_created = pyqtSignal(object)  # RoiDefinition (geometry only; caller binds+saves)
        roi_selected = pyqtSignal(object)  # roi_id | None
        drawing_cancelled = pyqtSignal()
        edit_command = pyqtSignal(object)  # EditCommand

    def __init__(self, image_widget=None, *, editable: bool = True,
                 parent=None) -> None:
        if not _HAS_PYQT6:
            self._interaction = RoiInteractionState()
            self._context = None
            self._rois = []
            return
        super().__init__(parent)
        self._editable = editable
        self._interaction = RoiInteractionState()
        self._context: RoiActiveContext | None = None
        self._rois: list[RoiDefinition] = []
        self._results: dict[str, object] = {}
        self._overlays_visible = True
        self._make_geometry: Optional[Callable] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._toolbar = RoiToolbar(self)
        self._toolbar.tool_changed.connect(self._on_tool_changed)
        self._toolbar.delete_requested.connect(self._on_delete_requested)
        self._toolbar.undo_requested.connect(self._on_undo)
        self._toolbar.redo_requested.connect(self._on_redo)
        self._toolbar.overlays_toggled.connect(self._on_overlays_toggled)
        layout.addWidget(self._toolbar)

        if image_widget is None:
            from thermal_monitor.ui.modes.observer_image import LiveThermalWidget
            image_widget = LiveThermalWidget()
        self._image = image_widget
        layout.addWidget(self._image, 1)
        if hasattr(self._image, "zoom_changed"):
            try:
                self._image.zoom_changed.connect(self._toolbar.set_zoom_text)
            except Exception:
                pass
        self._image.installEventFilter(self)
        self.set_editable(editable)

    # -- public API -----------------------------------------------------
    @property
    def image_widget(self):
        return self._image

    @property
    def toolbar(self) -> RoiToolbar:
        return self._toolbar

    @property
    def interaction(self) -> RoiInteractionState:
        return self._interaction

    def set_editable(self, editable: bool) -> None:
        self._editable = editable
        if _HAS_PYQT6 and not editable:
            self._interaction.active_tool = None
            self._toolbar.set_context_active(
                self._context is not None and self._context.state == "active",
                self._toolbar._context_label.text())

    def set_context(self, context: RoiActiveContext | None,
                    rois: list[RoiDefinition]) -> None:
        """Atomically replace the visible set; clears selection + undo history."""
        self._interaction.clear_session()
        self._context = context
        self._rois = [r for r in (rois or []) if r.visible]
        self._results = {}
        if _HAS_PYQT6:
            if context is None or context.state != "active":
                self._toolbar.set_context_active(
                    False, "No position — select and reach a saved position")
                self._image.set_roi_overlays([])
            else:
                self._toolbar.set_context_active(
                    True,
                    f"Camera: {context.camera_id}  PTZ: {context.ptz_id}  "
                    f"Position: {context.position_id}  Context: Active")
                self._repaint_overlays()
            self._toolbar.set_undo_redo(False, False)
            self.roi_selected.emit(None)

    def update_results(self, results: list, context: RoiActiveContext) -> None:
        """Apply measurements only when they match the published context."""
        if self._context is None or context.context_generation != self._context.context_generation:
            return  # stale context: never paint old values
        if context.position_id != self._context.position_id:
            return
        for m in results:
            if (getattr(m, "context_generation", None) == self._context.context_generation
                    and getattr(m, "position_id", None) == self._context.position_id):
                self._results[m.roi_id] = m
        if _HAS_PYQT6:
            self._repaint_overlays()

    def clear_results(self) -> None:
        self._results = {}
        if _HAS_PYQT6 and self._context is not None:
            self._repaint_overlays()

    # -- internals --------------------------------------------------------
    def _mapping(self) -> Optional[ViewportMapping]:
        image = getattr(self._image, "_display_image", None)
        temp = getattr(self._image, "_temperature_image", None)
        if image is None:
            return None
        try:
            iw, ih = image.width(), image.height()
        except Exception:
            return None
        if temp is not None:
            try:
                ih, iw = temp.shape[:2]
            except Exception:
                pass
        zoom = getattr(self._image, "_zoom", None)
        pan = getattr(self._image, "_pan_offset", None)
        try:
            px = float(pan.x()) if pan is not None else 0.0
            py = float(pan.y()) if pan is not None else 0.0
        except Exception:
            px, py = 0.0, 0.0
        return ViewportMapping(image_width=int(iw), image_height=int(ih),
                               widget_width=max(1, self._image.width()),
                               widget_height=max(1, self._image.height()),
                               zoom=zoom, pan_x=px, pan_y=py)

    def _repaint_overlays(self) -> None:
        if not self._overlays_visible or self._context is None:
            self._image.set_roi_overlays([])
            return
        from thermal_monitor.ui.modes.observer_image import ROIOverlay
        overlays: list[ROIOverlay] = []
        for roi in self._rois:
            g = roi.geometry
            overlays.append(ROIOverlay(
                roi_id=roi.roi_id, shape=_legacy_shape(roi.object_type, g),
                geometry=_legacy_geometry(g),
                color=overlay_color(roi.object_type,
                                    selected=(roi.roi_id == self._interaction.selected_id)),
                selected=(roi.roi_id == self._interaction.selected_id)))
        self._image.set_roi_overlays(overlays)

    if _HAS_PYQT6:
        def eventFilter(self, watched, event) -> bool:  # noqa: N802
            from PyQt6.QtCore import QEvent
            if watched is self._image and self._editable:
                if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                    mapping = self._mapping()
                    if mapping is not None and self._interaction.active_tool is not None:
                        try:
                            pos = event.position()
                            col, row = mapping.widget_to_image(float(pos.x()), float(pos.y()))
                        except Exception:
                            return super().eventFilter(watched, event)
                        try:
                            geometry = self._interaction.click(col, row)
                        except Exception:
                            return super().eventFilter(watched, event)
                        if geometry is not None:
                            self._finish_drawing(geometry)
                        return True
                    if mapping is not None and self._interaction.active_tool is None:
                        try:
                            pos = event.position()
                            col, row = mapping.widget_to_image(float(pos.x()), float(pos.y()))
                        except Exception:
                            return super().eventFilter(watched, event)
                        items = [_ItemProxy(r) for r in self._rois]
                        selected = self._interaction.hit_test(col, row, items)
                        self._repaint_overlays()
                        self.roi_selected.emit(selected)
                        return True
                if event.type() == QEvent.Type.MouseButtonDblClick and self._editable:
                    mapping = self._mapping()
                    if mapping is not None and self._interaction.active_tool in (
                            RoiObjectType.POLYLINE, RoiObjectType.POLYGON):
                        try:
                            pos = event.position()
                            col, row = mapping.widget_to_image(float(pos.x()), float(pos.y()))
                            geometry = self._interaction.click(col, row, finish=True)
                        except Exception:
                            return super().eventFilter(watched, event)
                        if geometry is not None:
                            self._finish_drawing(geometry)
                        return True
                if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                    if self._interaction.cancel_drawing():
                        self.drawing_cancelled.emit()
                        return True
            return super().eventFilter(watched, event)

    def _finish_drawing(self, geometry) -> None:
        self._toolbar.select_tool(None)
        self.edit_command.emit(EditCommand(kind="create", roi_id="",
                                           after={"geometry": geometry}))

    def _on_tool_changed(self, tool) -> None:
        if not self._editable:
            return
        self._interaction.active_tool = tool
        self._interaction.in_progress.clear()

    def _on_delete_requested(self) -> None:
        if self._interaction.selected_id:
            self.edit_command.emit(EditCommand(
                kind="delete", roi_id=self._interaction.selected_id))

    def _on_undo(self) -> None:
        command = self._interaction.pop_undo()
        if command is not None:
            self._toolbar.set_undo_redo(self._interaction.can_undo,
                                        self._interaction.can_redo)
            self.edit_command.emit(EditCommand(
                kind=f"undo_{command.kind}", roi_id=command.roi_id,
                before=command.before, after=command.after))

    def _on_redo(self) -> None:
        command = self._interaction.pop_redo()
        if command is not None:
            self._toolbar.set_undo_redo(self._interaction.can_undo,
                                        self._interaction.can_redo)
            self.edit_command.emit(EditCommand(
                kind=f"redo_{command.kind}", roi_id=command.roi_id,
                before=command.before, after=command.after))

    def _on_overlays_toggled(self, visible: bool) -> None:
        self._overlays_visible = visible
        self._repaint_overlays()


class _ItemProxy:
    """Minimal adapter so hit-testing works on RoiDefinitions."""

    def __init__(self, roi: RoiDefinition) -> None:
        self.roi_id = roi.roi_id
        self.geometry = roi.geometry


def _legacy_shape(object_type: RoiObjectType, geometry) -> str:
    mapping = {
        RoiObjectType.RECTANGLE: "rectangle1",
        RoiObjectType.ANNOTATION_RECTANGLE: "rectangle1",
        RoiObjectType.CIRCLE: "circle",
        RoiObjectType.ELLIPSE: "ellipse",
        RoiObjectType.ANNOTATION_ELLIPSE: "ellipse",
        RoiObjectType.POLYGON: "polygon",
    }
    return mapping.get(object_type, "rectangle1")


def _legacy_geometry(geometry) -> dict:
    d = geometry.to_dict()
    # LiveThermalWidget expects HALCON row/col keys (y1/x1...).
    out: dict = {}
    for key, value in d.items():
        out[key] = value
    rename = {"row1": "y1", "col1": "x1", "row2": "y2", "col2": "x2",
              "center_row": "center_y", "center_col": "center_x"}
    for old, new in rename.items():
        if old in out and new not in out:
            out[new] = out.pop(old)
    if "points" in out:
        out["points"] = [(p[0], p[1]) for p in out["points"]]
    return out


__all__ = ["CameraRegion"]
