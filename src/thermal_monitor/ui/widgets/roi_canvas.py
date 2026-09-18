"""ui.widgets.roi_canvas -- reusable ROI canvas controller.

Binds an image widget (any ``LiveThermalWidget``-compatible surface),
an optional toolbar, and a session ``RoiEditor`` into the complete tool
lifecycle: select, draw, drag-move, handle-resize, vertex edit, delete,
hide, undo/redo, Escape cancellation. All geometry stays in image
coordinates via the host-supplied mapping; painting uses the existing
``ROIOverlay`` list so rendering behavior is unchanged.

Used by ``CameraRegion`` and by Configuration Mode's center workspace.
Headless-testable: with PyQt absent, painting is skipped but all
editing logic runs.
"""

from __future__ import annotations

from typing import Callable, Optional

try:
    from PyQt6.QtCore import Qt
    _HAS_PYQT6 = True
except ImportError:
    _HAS_PYQT6 = False

from thermal_monitor.roi.coordinate_system import ViewportMapping
from thermal_monitor.roi.editor import RoiEditor
from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.errors import RoiValidationError
from thermal_monitor.roi.handles import handles_for
from thermal_monitor.ui.widgets.roi_interaction import RoiInteractionState
from thermal_monitor.ui.widgets.roi_overlay import overlay_color

_HANDLE_TOLERANCE_WIDGET_PX = 8.0

#: Tools supporting press-drag-release creation (in addition to clicks).
_DRAG_CREATE_TOOLS = {
    RoiObjectType.FREE_LINE, RoiObjectType.HORIZONTAL_LINE,
    RoiObjectType.VERTICAL_LINE, RoiObjectType.RULER,
    RoiObjectType.HORIZONTAL_RULER, RoiObjectType.VERTICAL_RULER,
    RoiObjectType.MEASURE_LINE, RoiObjectType.ARROW,
    RoiObjectType.RECTANGLE, RoiObjectType.ANNOTATION_RECTANGLE,
    RoiObjectType.ELLIPSE, RoiObjectType.ANNOTATION_ELLIPSE,
    RoiObjectType.CIRCLE, RoiObjectType.CROSS_LINE,
    RoiObjectType.HOTTEST_SPOT, RoiObjectType.COLDEST_SPOT,
    RoiObjectType.HOT_COLD_SPOTS,
}

#: Single-click placement tools.
_CLICK_TOOLS = {RoiObjectType.SPOT, RoiObjectType.NOTE}


class RoiCanvasController:
    """Mouse-driven editing over one image widget + toolbar."""

    def __init__(self, mapping_fn: Callable[[], Optional[ViewportMapping]],
                 *, toolbar=None, editable: bool = True) -> None:
        self._mapping_fn = mapping_fn
        self._toolbar = toolbar
        self._editable = editable
        self._editor: RoiEditor | None = None
        self._interaction = RoiInteractionState()
        self._press_image: tuple[float, float] | None = None
        self._drag_mode: str | None = None  # None | "move" | "handle" | "create"
        self._drag_handle: str | None = None
        self._drag_moved = False
        # Phase 12.5 transient drawing preview (never persisted, never in
        # the editor/table/pipeline): {"tool", "press", "current", "gen"}.
        # Vertex tools (polygon/...) additionally rubber-band from
        # interaction.in_progress via _hover (button-up cursor).
        self._preview: dict | None = None
        self._hover: tuple[float, float] | None = None
        # Host callbacks (all optional).
        self.on_created: Callable | None = None
        self.on_changed: Callable | None = None
        self.on_deleted: Callable[[str], None] | None = None
        self.on_selected: Callable[[str | None], None] | None = None
        self.on_repaint: Callable | None = None
        self.on_undo_state: Callable[[bool, bool], None] | None = None
        self.on_error: Callable[[str], None] | None = None
        if toolbar is not None and hasattr(toolbar, "tool_changed"):
            try:
                toolbar.tool_changed.connect(self._on_tool_changed)
            except Exception:
                pass

    # -- session ----------------------------------------------------------
    @property
    def editor(self) -> RoiEditor | None:
        return self._editor

    @property
    def interaction(self) -> RoiInteractionState:
        return self._interaction

    @property
    def profile_armed(self) -> bool:
        toolbar = self._toolbar
        return bool(getattr(toolbar, "profile_armed", False))

    def set_editor(self, editor: RoiEditor | None) -> None:
        self._editor = editor
        self._interaction.clear_session()
        self._press_image = None
        self._drag_mode = None
        self._preview = None
        self._hover = None
        self._sync_undo()
        self._repaint()

    def set_editable(self, editable: bool) -> None:
        self._editable = editable
        if not editable:
            self._interaction.active_tool = None

    # -- mouse API (widget pixels) ------------------------------------------
    def press(self, wx: float, wy: float) -> bool:
        """Handle press; True when consumed (no pan/other handling)."""
        if not self._editable or self._editor is None:
            return False
        mapping = self._mapping_fn()
        if mapping is None or not mapping.is_inside_image(wx, wy):
            return False
        col, row = mapping.widget_to_image(wx, wy)
        tool = self._interaction.active_tool
        if tool is None:
            return self._press_select(wx, wy, col, row)
        if tool in _CLICK_TOOLS:
            self._place_click_tool(tool, col, row)
            return True
        if tool in (RoiObjectType.POLYLINE, RoiObjectType.POLYGON,
                    RoiObjectType.MEASURE_ANGLE):
            try:
                geometry = self._interaction.click(col, row)
            except RoiValidationError as exc:
                self._error(str(exc))
                return True
            if geometry is not None:
                self._commit_created(tool, geometry)
            self._hover = (col, row)
            self._repaint()
            return True
        # Two-point / box tools: start a drag (click-click also works via
        # the interaction state as fallback on release without movement).
        self._press_image = (col, row)
        self._drag_mode = "create"
        self._drag_moved = False
        self._preview = {"tool": tool, "press": (col, row),
                         "current": (col, row),
                         "gen": self._context_generation()}
        if tool not in _DRAG_CREATE_TOOLS:
            self._interaction.click(col, row)
        self._repaint()
        return True

    def move(self, wx: float, wy: float) -> bool:
        if not self._editable or self._editor is None:
            return False
        if self._drag_mode is None:
            # Hover rubber-band for vertex tools with confirmed points.
            tool = self._interaction.active_tool
            if tool in (RoiObjectType.POLYLINE, RoiObjectType.POLYGON,
                        RoiObjectType.MEASURE_ANGLE) \
                    and self._interaction.in_progress:
                mapping = self._mapping_fn()
                if mapping is None:
                    return False
                if not self._preview_gen_ok():
                    self._clear_preview()
                    return False
                col, row = mapping.widget_to_image(wx, wy)
                self._hover = (col, row)
                self._repaint()
                return True
            return False
        mapping = self._mapping_fn()
        if mapping is None:
            return False
        col, row = mapping.widget_to_image(wx, wy)
        selected = self._interaction.selected_id
        if self._drag_mode == "move" and selected is not None:
            roi = self._editor.get(selected)
            if roi is None:
                return False
            anchor = self._press_image or (col, row)
            try:
                self._editor.move(selected, col - anchor[0], row - anchor[1])
            except RoiValidationError as exc:
                self._error(str(exc))
                return True
            self._press_image = (col, row)
            self._drag_moved = True
            self._repaint()
            return True
        if self._drag_mode == "handle" and selected is not None:
            try:
                self._editor.resize(selected, self._drag_handle or "", col, row)
            except RoiValidationError as exc:
                self._error(str(exc))
                return True
            self._drag_moved = True
            self._repaint()
            return True
        if self._drag_mode == "create":
            if self._press_image is not None and (
                    abs(col - self._press_image[0]) > 0.5
                    or abs(row - self._press_image[1]) > 0.5):
                self._drag_moved = True
            if self._preview is not None:
                if not self._preview_gen_ok():
                    self._clear_preview()
                else:
                    self._preview["current"] = (col, row)
                    self._repaint()
            return True
        return False

    def release(self, wx: float, wy: float) -> bool:
        if not self._editable or self._editor is None:
            return False
        if self._drag_mode is None:
            return False
        mode, self._drag_mode = self._drag_mode, None
        if mode in ("move", "handle"):
            result = self._editor.end_drag()
            self._sync_undo()
            if result is not None and self.on_changed is not None:
                self.on_changed(result)
            self._repaint()
            return True
        if mode == "create":
            mapping = self._mapping_fn()
            if mapping is None:
                self._press_image = None
                return True
            col, row = mapping.widget_to_image(wx, wy)
            tool = self._interaction.active_tool
            press = self._press_image
            self._press_image = None
            if tool is None or press is None:
                return True
            try:
                if self._drag_moved and tool in _DRAG_CREATE_TOOLS:
                    geometry = self._interaction.click(*press)
                    if geometry is None:
                        geometry = self._interaction.click(col, row)
                else:
                    geometry = self._interaction.click(col, row)
            except RoiValidationError as exc:
                self._error(str(exc))
                self._interaction.cancel_drawing()
                return True
            if geometry is not None:
                self._commit_created(tool, geometry)
            self._preview = None
            self._repaint()
            return True
        return False

    def double_click(self, wx: float, wy: float) -> bool:
        if not self._editable or self._editor is None:
            return False
        tool = self._interaction.active_tool
        if tool not in (RoiObjectType.POLYLINE, RoiObjectType.POLYGON):
            return False
        mapping = self._mapping_fn()
        if mapping is None:
            return False
        col, row = mapping.widget_to_image(wx, wy)
        try:
            geometry = self._interaction.click(col, row, finish=True)
        except RoiValidationError as exc:
            self._error(str(exc))
            return True
        if geometry is not None:
            self._commit_created(tool, geometry)
        self._preview = None
        self._hover = None
        self._repaint()
        return True

    def escape(self) -> bool:
        if self._editor is not None and self._drag_mode in ("move", "handle"):
            self._editor.cancel_drag()
            self._drag_mode = None
            self._repaint()
            return True
        cleared = self._preview is not None or self._hover is not None
        self._clear_preview()
        if self._interaction.cancel_drawing() or cleared:
            self._repaint()
            return True
        return False

    # -- commands -------------------------------------------------------------
    def delete_selected(self) -> bool:
        if self._editor is None or self._interaction.selected_id is None:
            return False
        roi_id = self._interaction.selected_id
        try:
            self._editor.delete(roi_id)
        except RoiValidationError as exc:
            self._error(str(exc))
            return False
        self._interaction.selected_id = None
        self._sync_undo()
        if self.on_deleted is not None:
            self.on_deleted(roi_id)
        self._repaint()
        return True

    def undo(self) -> bool:
        if self._editor is None:
            return False
        command = self._editor.undo()
        if command is None:
            return False
        self._sync_undo()
        self._repaint()
        return True

    def redo(self) -> bool:
        if self._editor is None:
            return False
        command = self._editor.redo()
        if command is None:
            return False
        self._sync_undo()
        self._repaint()
        return True

    # -- internals --------------------------------------------------------------
    def _on_tool_changed(self, tool) -> None:
        self._interaction.active_tool = tool
        self._interaction.in_progress.clear()
        self._press_image = None
        self._drag_mode = None
        self._preview = None
        self._hover = None

    def _press_select(self, wx, wy, col, row) -> bool:
        if self._editor is None:
            return False
        # Handles of the selected object first (widget-px tolerance).
        selected = self._interaction.selected_id
        mapping = self._mapping_fn()
        if selected is not None and mapping is not None:
            roi = self._editor.get(selected)
            if roi is not None:
                for handle_id, hcol, hrow in handles_for(roi.geometry):
                    hx, hy = mapping.image_to_widget(hcol, hrow)
                    if abs(hx - wx) <= _HANDLE_TOLERANCE_WIDGET_PX and abs(
                            hy - wy) <= _HANDLE_TOLERANCE_WIDGET_PX:
                        self._drag_mode = "handle"
                        self._drag_handle = handle_id
                        self._editor.begin_drag(selected)
                        return True
        items = [_Proxy(r) for r in self._editor.rois]
        hit = self._interaction.hit_test(col, row, items)
        if self.on_selected is not None:
            self.on_selected(hit)
        if hit is not None:
            self._press_image = (col, row)
            self._drag_mode = "move"
            try:
                self._editor.begin_drag(hit)
            except RoiValidationError:
                self._drag_mode = None
                return True
            return True
        self._repaint()
        return True

    def _place_click_tool(self, tool, col, row) -> None:
        try:
            geometry = self._interaction.click(col, row)
        except RoiValidationError as exc:
            self._error(str(exc))
            return
        if geometry is not None:
            self._commit_created(tool, geometry)
        self._repaint()

    def _commit_created(self, tool, geometry) -> None:
        assert self._editor is not None
        name = self._default_name(tool)
        try:
            if tool == RoiObjectType.FREE_LINE and self.profile_armed:
                roi = self._editor.create(
                    tool, geometry, name,
                    analysis_config={"profile": True})
            else:
                roi = self._editor.create(tool, geometry, name)
        except (RoiValidationError, ValueError) as exc:
            self._error(str(exc))
            return
        self._interaction.selected_id = roi.roi_id
        self._interaction.active_tool = None
        if self._toolbar is not None and hasattr(self._toolbar, "select_tool"):
            try:
                self._toolbar.select_tool(None)
            except Exception:
                pass
        self._sync_undo()
        if self.on_created is not None:
            self.on_created(roi)
        if self.on_selected is not None:
            self.on_selected(roi.roi_id)

    def _default_name(self, tool) -> str:
        existing = len(self._editor.rois) if self._editor else 0
        return f"{tool.value.replace('_', ' ').title()} {existing + 1}"

    def _sync_undo(self) -> None:
        if self._toolbar is not None and hasattr(self._toolbar, "set_undo_redo"):
            try:
                editor = self._editor
                self._toolbar.set_undo_redo(
                    bool(editor and editor.can_undo),
                    bool(editor and editor.can_redo))
            except Exception:
                pass
        if self.on_undo_state is not None and self._editor is not None:
            self.on_undo_state(self._editor.can_undo, self._editor.can_redo)

    def _repaint(self) -> None:
        if self.on_repaint is not None:
            try:
                self.on_repaint()
            except Exception:
                pass

    def _error(self, message: str) -> None:
        if self.on_error is not None:
            try:
                self.on_error(message)
            except Exception:
                pass

    # -- transient drawing preview (Phase 12.5) --------------------------
    def _context_generation(self):
        editor = self._editor
        context = getattr(editor, "context", None)
        return getattr(context, "context_generation", None)

    def _preview_gen_ok(self) -> bool:
        if self._preview is None:
            return True
        return self._preview.get("gen") == self._context_generation()

    def _clear_preview(self) -> None:
        self._preview = None
        self._hover = None

    def _preview_points(self):
        """Image-coordinate points defining the live preview, if any."""
        if self._editor is None:
            return None
        if not self._preview_gen_ok():
            self._clear_preview()
            return None
        tool = self._interaction.active_tool
        if tool in (RoiObjectType.POLYLINE, RoiObjectType.POLYGON,
                    RoiObjectType.MEASURE_ANGLE):
            confirmed = list(self._interaction.in_progress)
            if not confirmed or self._hover is None:
                return None
            return tool, confirmed + [self._hover]
        if self._preview is not None and self._drag_mode == "create":
            tool = self._preview["tool"]
            press = self._preview["press"]
            current = self._preview["current"]
            return tool, [press, current]
        return None

    def _preview_overlays(self):  # noqa: ANN201
        """Transient preview overlays (never persisted, never tabulated)."""
        resolved = self._preview_points()
        if resolved is None:
            return []
        tool, points = resolved
        try:
            geometry = self._interaction.preview_geometry(tool, points)
        except RoiValidationError:
            return []
        try:
            from thermal_monitor.ui.modes.observer_image import ROIOverlay
        except ImportError:
            return []
        if geometry is None:
            # Not enough points for a shape yet: rubber-band edge.
            if len(points) < 2:
                return []
            return [ROIOverlay(roi_id="", shape="polyline",
                               geometry={"points": [
                                   (r, c) for c, r in points]},
                               color="#00E5FF", preview=True)]
        overlays = []
        for shape, geom_dict in _preview_shape_geometry(tool, geometry):
            overlays.append(ROIOverlay(
                roi_id="", shape=shape, geometry=geom_dict,
                color="#00E5FF", preview=True))
        return overlays

    # -- overlay construction (shared by all hosts) -------------------------------
    def build_overlays(self):  # noqa: ANN201
        """ROIOverlay list for the current editor state (paint-ready)."""
        if self._editor is None:
            return []
        try:
            from thermal_monitor.ui.modes.observer_image import ROIOverlay
        except ImportError:
            return []
        overlays = []
        for roi in self._editor.rois:
            if not roi.visible:
                continue
            overlays.append(ROIOverlay(
                roi_id=roi.roi_id,
                shape=_legacy_shape(roi.object_type, roi.geometry),
                geometry=_legacy_geometry(roi.geometry),
                color=overlay_color(
                    roi.object_type,
                    selected=(roi.roi_id == self._interaction.selected_id)),
                selected=(roi.roi_id == self._interaction.selected_id),
                name=roi.name or roi.roi_id))
        overlays.extend(self._preview_overlays())
        return overlays

    def select(self, roi_id: str | None, *, emit: bool = True) -> bool:
        """Programmatic selection (e.g. from the ROI table).

        Validates against the installed editor; unknown IDs clear the
        selection. Returns True when the selection changed.
        """
        current = self._interaction.selected_id
        if roi_id is not None and self._editor is not None:
            try:
                known = self._editor.get(roi_id) is not None
            except Exception:
                known = False
            if not known:
                roi_id = None
        changed = (roi_id != current)
        self._interaction.selected_id = roi_id
        if emit and self.on_selected is not None:
            try:
                self.on_selected(roi_id)
            except Exception:
                pass
        if changed:
            self._repaint()
        return changed


class _Proxy:
    def __init__(self, roi) -> None:
        self.roi_id = roi.roi_id
        self.geometry = roi.geometry


def _preview_shape_geometry(tool, geometry) -> list:
    """Map preview geometry to painter-ready (shape, dict) overlays."""
    line_tools = {
        RoiObjectType.FREE_LINE, RoiObjectType.HORIZONTAL_LINE,
        RoiObjectType.VERTICAL_LINE, RoiObjectType.RULER,
        RoiObjectType.HORIZONTAL_RULER, RoiObjectType.VERTICAL_RULER,
        RoiObjectType.MEASURE_LINE, RoiObjectType.ARROW,
        RoiObjectType.POLYLINE,
    }
    if tool in (RoiObjectType.RECTANGLE, RoiObjectType.ANNOTATION_RECTANGLE):
        return [("rectangle1", _legacy_geometry(geometry))]
    if tool in (RoiObjectType.ELLIPSE, RoiObjectType.ANNOTATION_ELLIPSE):
        return [("ellipse", _legacy_geometry(geometry))]
    if tool == RoiObjectType.CIRCLE:
        return [("circle", _legacy_geometry(geometry))]
    if tool == RoiObjectType.POLYGON:
        return [("polygon", _legacy_geometry(geometry))]
    if tool in (RoiObjectType.HOTTEST_SPOT, RoiObjectType.HOT_COLD_SPOTS,
                RoiObjectType.COLDEST_SPOT):
        return [("rectangle1", {"y1": geometry.row1, "x1": geometry.col1,
                                "y2": geometry.row2, "x2": geometry.col2})]
    if tool in line_tools:
        g = geometry
        if hasattr(g, "points"):  # polyline
            pts = [(float(r), float(c)) for r, c in g.points]
        else:
            pts = [(float(g.row1), float(g.col1)),
                   (float(g.row2), float(g.col2))]
        return [("polyline", {"points": pts})]
    if tool == RoiObjectType.MEASURE_ANGLE:
        g = geometry
        return [("polyline", {"points": [
            (float(g.end1_row), float(g.end1_col)),
            (float(g.center_row), float(g.center_col)),
            (float(g.end2_row), float(g.end2_col))]})]
    if tool == RoiObjectType.CROSS_LINE:
        g = geometry
        return [
            ("polyline", {"points": [
                (float(g.center_row - g.half_length_row),
                 float(g.center_col)),
                (float(g.center_row + g.half_length_row),
                 float(g.center_col))]}),
            ("polyline", {"points": [
                (float(g.center_row),
                 float(g.center_col - g.half_length_col)),
                (float(g.center_row),
                 float(g.center_col + g.half_length_col))]}),
        ]
    # SPOT/NOTE commit on click: no drag preview exists for them.
    return []


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
    out = dict(geometry.to_dict())
    for old, new in (("row1", "y1"), ("col1", "x1"), ("row2", "y2"),
                     ("col2", "x2"), ("center_row", "center_y"),
                     ("center_col", "center_x")):
        if old in out and new not in out:
            out[new] = out.pop(old)
    return out


__all__ = ["RoiCanvasController"]
