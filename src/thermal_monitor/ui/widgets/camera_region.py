"""ui.widgets.camera_region -- Camera Region: image + ROI toolbar + overlays.

The Camera Region is the visual unit owning the displayed image: the
existing ``LiveThermalWidget`` renderer plus the Phase 10 compact ROI
toolbar docked directly above it. Rendering, pan/zoom, IR/VL handling,
and the latest-frame-wins path are unchanged; this widget adds:

- compact icon toolbar (tool selection, undo/redo, delete, overlay toggle)
- position-scoped overlay publication (only the active context paints)
- full mouse drawing/selection/drag/resize via RoiCanvasController in
  image coordinates (widget coords converted centrally)

Live/Observer tiles keep their existing behavior: overlays are
read-only there unless a controller explicitly enables editing.
"""

from __future__ import annotations

try:
    from PyQt6.QtCore import Qt, pyqtSignal
    from PyQt6.QtWidgets import QVBoxLayout, QWidget
    _HAS_PYQT6 = True
except ImportError:
    _HAS_PYQT6 = False
    QWidget = object  # type: ignore

from thermal_monitor.roi.context import RoiActiveContext
from thermal_monitor.roi.coordinate_system import ViewportMapping
from thermal_monitor.roi.editor import RoiEditor
from thermal_monitor.roi.models import RoiDefinition
from thermal_monitor.ui.widgets.roi_canvas import RoiCanvasController
from thermal_monitor.ui.widgets.roi_toolbar import RoiToolbar


class CameraRegion(QWidget if _HAS_PYQT6 else object):
    """Image + toolbar composite for Configuration (editable) and Live (read-only)."""

    if _HAS_PYQT6:
        roi_created = pyqtSignal(object)  # RoiDefinition
        roi_changed = pyqtSignal(object)  # RoiDefinition
        roi_deleted = pyqtSignal(str)  # roi_id
        roi_selected = pyqtSignal(object)  # roi_id | None
        drawing_cancelled = pyqtSignal()

    def __init__(self, image_widget=None, *, editable: bool = True,
                 parent=None) -> None:
        if not _HAS_PYQT6:
            self._controller = RoiCanvasController(lambda: None)
            self._context = None
            return
        super().__init__(parent)
        self._editable = editable
        self._context: RoiActiveContext | None = None
        self._results: dict[str, object] = {}
        self._overlays_visible = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._toolbar = RoiToolbar(self)
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
        self._controller = RoiCanvasController(
            self._mapping, toolbar=self._toolbar, editable=editable)
        self._controller.on_created = self._on_created
        self._controller.on_changed = self._on_changed
        self._controller.on_deleted = self._on_deleted
        self._controller.on_selected = self._on_selected
        self._controller.on_repaint = self._repaint_overlays
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
    def interaction(self):
        return self._controller.interaction

    @property
    def controller(self) -> RoiCanvasController:
        return self._controller

    @property
    def editor(self) -> RoiEditor | None:
        return self._controller.editor

    def set_editable(self, editable: bool) -> None:
        self._editable = editable
        if _HAS_PYQT6:
            self._controller.set_editable(editable)

    def set_context(self, context: RoiActiveContext | None,
                    rois: list[RoiDefinition]) -> None:
        """Atomically replace the visible set; clears selection + undo history."""
        self._context = context
        self._results = {}
        if not _HAS_PYQT6:
            return
        if context is None or context.state != "active":
            self._controller.set_editor(None)
            self._toolbar.set_context_active(
                False, "No position — select and reach a saved position")
            self._image.set_roi_overlays([])
            self.roi_selected.emit(None)
            return
        try:
            editor = RoiEditor(context, rois)
        except Exception:
            self._controller.set_editor(None)
            self._toolbar.set_context_active(False, "ROI: Activation failed")
            self._image.set_roi_overlays([])
            return
        self._controller.set_editor(editor)
        if not rois:
            self._toolbar.set_context_active(
                True, f"Camera: {context.camera_id}  PTZ: {context.ptz_id}  "
                f"Position: {context.position_id}  "
                f"ROI: No ROI configured for this position")
        else:
            self._toolbar.set_context_active(
                True, f"Camera: {context.camera_id}  PTZ: {context.ptz_id}  "
                f"Position: {context.position_id}  "
                f"ROI: {len(rois)} object(s)")
        self._repaint_overlays()
        self.roi_selected.emit(None)

    def clear_camera(self) -> None:
        """Camera switch: drop the session so no foreign ROI can display."""
        self.set_context(None, [])

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
    def _mapping(self) -> ViewportMapping | None:
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
        self._image.set_roi_overlays(self._controller.build_overlays())

    if _HAS_PYQT6:
        def eventFilter(self, watched, event) -> bool:  # noqa: N802
            from PyQt6.QtCore import QEvent
            if watched is self._image and self._editable:
                etype = event.type()
                if etype == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                    try:
                        pos = event.position()
                        if self._controller.press(float(pos.x()), float(pos.y())):
                            return True
                    except Exception:
                        pass
                elif etype == QEvent.Type.MouseMove:
                    try:
                        pos = event.position()
                        if self._controller.move(float(pos.x()), float(pos.y())):
                            return True
                    except Exception:
                        pass
                elif etype == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                    try:
                        pos = event.position()
                        if self._controller.release(float(pos.x()), float(pos.y())):
                            return True
                    except Exception:
                        pass
                elif etype == QEvent.Type.MouseButtonDblClick:
                    try:
                        pos = event.position()
                        if self._controller.double_click(float(pos.x()), float(pos.y())):
                            return True
                    except Exception:
                        pass
                elif etype == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                    if self._controller.escape():
                        self.drawing_cancelled.emit()
                        return True
            return super().eventFilter(watched, event)

    def _on_created(self, roi: RoiDefinition) -> None:
        self._refresh_count_label()
        self.roi_created.emit(roi)

    def _on_changed(self, roi: RoiDefinition) -> None:
        self.roi_changed.emit(roi)

    def _on_deleted(self, roi_id: str) -> None:
        self._refresh_count_label()
        self.roi_deleted.emit(roi_id)

    def _on_selected(self, roi_id) -> None:
        self._repaint_overlays()
        self.roi_selected.emit(roi_id)

    def _refresh_count_label(self) -> None:
        if self._context is None:
            return
        editor = self._controller.editor
        count = len(editor.rois) if editor else 0
        if count == 0:
            text = (f"Camera: {self._context.camera_id}  "
                    f"PTZ: {self._context.ptz_id}  "
                    f"Position: {self._context.position_id}  "
                    f"ROI: No ROI configured for this position")
        else:
            text = (f"Camera: {self._context.camera_id}  "
                    f"PTZ: {self._context.ptz_id}  "
                    f"Position: {self._context.position_id}  "
                    f"ROI: {count} object(s)")
        self._toolbar.set_context_active(True, text)

    def _on_delete_requested(self) -> None:
        self._controller.delete_selected()

    def _on_undo(self) -> None:
        self._controller.undo()

    def _on_redo(self) -> None:
        self._controller.redo()

    def _on_overlays_toggled(self, visible: bool) -> None:
        self._overlays_visible = visible
        self._repaint_overlays()


__all__ = ["CameraRegion"]
