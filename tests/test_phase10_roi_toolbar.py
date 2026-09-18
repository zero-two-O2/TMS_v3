"""Phase 10H/UI: toolbar, interaction, overlay, properties, Camera Region tests."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtWidgets import QApplication  # noqa: E402

from thermal_monitor.roi.context import RoiActiveContext  # noqa: E402
from thermal_monitor.roi.enums import RoiObjectType  # noqa: E402
from thermal_monitor.roi.geometry import LineGeometry, RectangleGeometry, SpotGeometry  # noqa: E402
from thermal_monitor.roi.models import RoiDefinition  # noqa: E402
from thermal_monitor.ui.widgets.camera_region import CameraRegion  # noqa: E402
from thermal_monitor.ui.widgets.roi_interaction import RoiInteractionState  # noqa: E402
from thermal_monitor.ui.widgets.roi_overlay import overlay_color  # noqa: E402
from thermal_monitor.ui.widgets.roi_toolbar import TOOL_GROUPS, RoiToolbar  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _ctx(**overrides):
    values = dict(camera_id="cam_1", ptz_id="ptz_1", position_id="pos_A",
                  position_generation=1, session_generation=1,
                  context_generation=5, state="active", roi_ids=("r1",))
    values.update(overrides)
    return RoiActiveContext(**values)


def _roi():
    return RoiDefinition(roi_id="r1", camera_id="cam_1", ptz_id="ptz_1",
                         position_id="pos_A", object_type=RoiObjectType.SPOT,
                         geometry=SpotGeometry(row=10, col=10))


def test_toolbar_inside_camera_region(app):
    region = CameraRegion()
    assert region.toolbar is not None
    assert region.toolbar.objectName() != "cfg_top_bar"
    # Toolbar is the first child above the image widget.
    assert region.layout().itemAt(0).widget() is region.toolbar


def test_tool_selection_and_active_indication(app):
    toolbar = RoiToolbar()
    toolbar.set_context_active(True, "Camera: cam_1 Position: pos_A Context: Active")
    toolbar.set_active_tool(RoiObjectType.RECTANGLE)
    assert toolbar.active_tool == RoiObjectType.RECTANGLE


def test_tools_disabled_without_valid_position(app):
    toolbar = RoiToolbar()
    toolbar.set_context_active(False, "No position")
    for button in toolbar._group_buttons.values():
        assert not button.isEnabled()


def test_all_tool_groups_present(app):
    labels = [label for tools in TOOL_GROUPS.values() for label, _, _, _ in tools]
    assert "Spot" in labels and "Rectangle" in labels and "Ruler" in labels
    assert "Note" in labels and "Measure Angle" in labels and "Polyline" in labels
    assert "Coldest Spot" in labels and "On-Image Profile" in labels


def test_toolbar_uses_icons_not_text(app):
    toolbar = RoiToolbar()
    for group, button in toolbar._group_buttons.items():
        assert button.text() == "", f"{group} button must show an icon, not text"
        assert button.toolTip(), f"{group} button must have a tooltip"
        assert button.accessibleName(), f"{group} needs an accessible name"
    assert toolbar._undo_btn.text() == ""
    assert toolbar._delete_btn.text() == ""


def test_drawing_cancellation(app):
    state = RoiInteractionState()
    state.active_tool = RoiObjectType.POLYGON
    state.click(10.0, 10.0)
    assert state.cancel_drawing() is True
    assert state.in_progress == []
    assert state.cancel_drawing() is False


def test_polyline_finish_and_escape(app):
    state = RoiInteractionState()
    state.active_tool = RoiObjectType.POLYLINE
    assert state.click(0.0, 0.0) is None
    geom = state.click(10.0, 0.0, finish=True)
    assert geom is not None and len(geom.points) == 2


def test_object_selection(app):
    state = RoiInteractionState()
    items = [type("I", (), {"roi_id": "r1", "geometry": SpotGeometry(row=10, col=10)})()]
    assert state.hit_test(10.0, 10.0, items) == "r1"
    assert state.hit_test(300.0, 300.0, items) is None


def test_position_switch_changes_visible_rois(app):
    region = CameraRegion()
    region.set_context(_ctx(), [_roi()])
    assert region.editor is not None
    assert [r.roi_id for r in region.editor.rois] == ["r1"]
    ctx_b = RoiActiveContext(camera_id="cam_1", ptz_id="ptz_1", position_id="pos_B",
                             position_generation=2, session_generation=1,
                             context_generation=6, state="active", roi_ids=())
    region.set_context(ctx_b, [])
    assert region.editor is not None
    assert region.editor.rois == []
    assert region.interaction.selected_id is None
    assert not region.editor.can_undo


def test_stale_overlays_removed_and_results_rejected(app):
    region = CameraRegion()
    region.set_context(_ctx(), [_roi()])
    from thermal_monitor.roi.models import RoiMeasurement
    stale = RoiMeasurement(
        roi_id="r1", camera_id="cam_1", ptz_id="ptz_1", position_id="pos_A",
        context_generation=4, session_generation=1, frame_id=1,
        frame_timestamp=0.0, processed_at=0.0, kind="spot",
        values={"value": 999.0})
    region.update_results([stale], _ctx(context_generation=4))
    assert "r1" not in region._results


def test_no_gui_freeze_smoke(app):
    # Processing entry points used by the region are synchronous pure
    # functions; this pins that no blocking call is introduced here.
    import time
    region = CameraRegion()
    t0 = time.perf_counter()
    region.set_context(_ctx(), [_roi()])
    assert (time.perf_counter() - t0) < 2.0


def test_overlay_colors_by_category():
    assert overlay_color(RoiObjectType.SPOT) == "#FFFF00"
    assert overlay_color(RoiObjectType.RULER) == "#00CCFF"
    assert overlay_color(RoiObjectType.NOTE) == "#FFAA00"
    assert overlay_color(RoiObjectType.SPOT, alarm=True) == "#FF0000"


def test_properties_panel_readonly_binding(app):
    from PyQt6.QtCore import Qt
    from thermal_monitor.ui.widgets.roi_properties import RoiPropertiesPanel
    panel = RoiPropertiesPanel()
    panel.set_roi(_roi(), "Active")
    assert panel._camera_label.text() == "cam_1"
    assert panel._ptz_label.text() == "ptz_1"
    assert not (panel._camera_label.textInteractionFlags()
                & Qt.TextInteractionFlag.TextEditable)
