"""Phase 10 interaction tests: handles, editor, canvas controller, import/export.

Headless-capable: the canvas controller runs its full editing logic
without PyQt painting; widget tests guard on QApplication.
"""

import pytest

from thermal_monitor.roi.context import RoiActiveContext
from thermal_monitor.roi.editor import (
    MAX_NAME_LEN,
    MAX_POLYGON_VERTICES,
    RoiEditor,
)
from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.errors import RoiBindingError, RoiValidationError
from thermal_monitor.roi.geometry import (
    AngleGeometry,
    CircleGeometry,
    HottestSpotGeometry,
    LineGeometry,
    NoteGeometry,
    PolygonGeometry,
    RectangleGeometry,
    SpotGeometry,
)
from thermal_monitor.roi.handles import (
    clamp_to_image,
    handles_for,
    move_geometry,
    resize_with_handle,
)
from thermal_monitor.roi.io import (
    export_collection,
    import_collection,
    preview_collection,
)


def _ctx():
    return RoiActiveContext(camera_id="cam_1", ptz_id="ptz_1", position_id="pos_A",
                            position_generation=1, session_generation=1,
                            context_generation=5, state="active")


def _editor():
    return RoiEditor(_ctx())


# -- handles ---------------------------------------------------------------

def test_rectangle_handles_and_resize():
    geom = RectangleGeometry(row1=10, col1=10, row2=50, col2=60)
    handles = dict((h, (c, r)) for h, c, r in handles_for(geom))
    assert handles["nw"] == (10, 10) and handles["se"] == (60, 50)
    resized = resize_with_handle(geom, "se", 100.0, 80.0)
    assert (resized.col2, resized.row2) == (100.0, 80.0)
    # Minimum-size rule: cannot invert or collapse.
    resized = resize_with_handle(geom, "nw", 1000.0, 1000.0)
    assert resized.row2 - resized.row1 >= 1.0
    assert resized.col2 - resized.col1 >= 1.0


def test_circle_keeps_radius_on_resize():
    geom = CircleGeometry(center_row=50, center_col=50, radius=10)
    resized = resize_with_handle(geom, "e", 70.0, 50.0)
    assert resized.radius == pytest.approx(20.0)
    assert (resized.center_row, resized.center_col) == (50, 50)


def test_line_endpoint_handles():
    geom = LineGeometry(row1=0, col1=0, row2=10, col2=0)
    moved = resize_with_handle(geom, "end", 20.0, 0.0)
    assert (moved.row2, moved.col2) == (0.0, 20.0)


def test_polygon_vertex_edit():
    geom = PolygonGeometry(points=((0, 0), (10, 0), (5, 10)))
    edited = resize_with_handle(geom, "v1", 20.0, 0.0)
    assert edited.points[1] == (0.0, 20.0)
    with pytest.raises(RoiValidationError):
        resize_with_handle(geom, "v9", 0.0, 0.0)


def test_move_preserves_shape():
    geom = CircleGeometry(center_row=10, center_col=10, radius=5)
    moved = move_geometry(geom, 3.0, -2.0)
    assert (moved.center_col, moved.center_row, moved.radius) == (13.0, 8.0, 5)


def test_clamp_keeps_inside_image():
    geom = SpotGeometry(row=-5, col=700)
    clamped = clamp_to_image(geom, 640, 480)
    assert (clamped.col, clamped.row) == (639, 0)


# -- editor -----------------------------------------------------------------

def test_create_select_move_resize_delete_undo_redo():
    editor = _editor()
    roi = editor.create(RoiObjectType.RECTANGLE,
                        RectangleGeometry(row1=10, col1=10, row2=30, col2=40),
                        "Heater")
    assert roi.camera_id == "cam_1" and roi.position_id == "pos_A"
    moved = editor.move(roi.roi_id, 5.0, 5.0)
    assert (moved.geometry.col1, moved.geometry.row1) == (15.0, 15.0)
    resized = editor.resize(roi.roi_id, "se", 100.0, 100.0)
    assert (resized.geometry.col2, resized.geometry.row2) == (100.0, 100.0)
    renamed = editor.rename(roi.roi_id, "Motor 1")
    assert renamed.name == "Motor 1"
    hidden = editor.set_visibility(roi.roi_id, False)
    assert hidden.visible is False
    editor.delete(roi.roi_id)
    assert editor.get(roi.roi_id) is None
    # Undo restores delete, then visibility, rename, resize, move, create.
    editor.undo()
    assert editor.get(roi.roi_id) is not None
    editor.undo()
    assert editor.get(roi.roi_id).visible is True
    editor.redo()
    assert editor.get(roi.roi_id).visible is False


def test_drag_commits_single_undo():
    editor = _editor()
    roi = editor.create(RoiObjectType.SPOT, SpotGeometry(row=10, col=10))
    editor.begin_drag(roi.roi_id)
    for i in range(50):  # many mousemove-sized steps
        editor.move(roi.roi_id, 1.0, 0.0)
    undos_before = len(editor._undo)
    editor.end_drag()
    assert len(editor._undo) == undos_before - 50 + 1
    editor.undo()
    assert editor.get(roi.roi_id).geometry.col == 10.0


def test_cancel_drag_restores_geometry():
    editor = _editor()
    roi = editor.create(RoiObjectType.SPOT, SpotGeometry(row=10, col=10))
    editor.begin_drag(roi.roi_id)
    editor.move(roi.roi_id, 25.0, 25.0)
    restored = editor.cancel_drag()
    assert (restored.geometry.col, restored.geometry.row) == (10.0, 10.0)


def test_editor_rejects_foreign_rois():
    from thermal_monitor.roi.models import RoiDefinition
    foreign = RoiDefinition(
        roi_id="x", camera_id="cam_1", ptz_id="ptz_1", position_id="pos_B",
        object_type=RoiObjectType.SPOT, geometry=SpotGeometry(row=1, col=1))
    with pytest.raises(RoiBindingError):
        RoiEditor(_ctx(), [foreign])


def test_editor_requires_active_context():
    from thermal_monitor.roi.context import inactive_context
    with pytest.raises(RoiBindingError):
        RoiEditor(inactive_context("cam_1", 1))


def test_name_length_bounded():
    editor = _editor()
    with pytest.raises(RoiValidationError):
        editor.create(RoiObjectType.NOTE, NoteGeometry(row=1, col=1, text="t"),
                      "n" * (MAX_NAME_LEN + 1))


def test_polygon_vertex_count_bounded():
    import math
    editor = _editor()
    many = tuple((240.0 + 200.0 * math.cos(2 * math.pi * i / 300),
                  320.0 + 200.0 * math.sin(2 * math.pi * i / 300))
                 for i in range(MAX_POLYGON_VERTICES + 1))
    from thermal_monitor.roi.geometry import PolygonGeometry
    with pytest.raises(RoiValidationError):
        editor.create(RoiObjectType.POLYGON, PolygonGeometry(points=many))
    # Direct geometry construction with too many collinear points is
    # rejected at validation time only via the editor bound check path:
    from thermal_monitor.roi import geometry as _g
    long_line = _g.PolylineGeometry(points=tuple((0.0, float(i)) for i in range(300)))
    with pytest.raises(RoiValidationError):
        editor.create(RoiObjectType.POLYLINE, long_line)


def test_duplicate_is_explicit_with_new_identity():
    editor = _editor()
    roi = editor.create(RoiObjectType.SPOT, SpotGeometry(row=1, col=1), "S")
    copy = editor.duplicate(roi.roi_id)
    assert copy.roi_id != roi.roi_id
    assert copy.position_id == "pos_A" and copy.name == "S copy"


# -- import/export ------------------------------------------------------------

def test_export_import_round_trip_replace():
    editor = _editor()
    roi = editor.create(RoiObjectType.SPOT, SpotGeometry(row=1, col=2), "S")
    payload = export_collection(camera_id="cam_1", ptz_id="ptz_1",
                                position_id="pos_A", rois=[roi])
    assert payload["format"] == "tms-roi-collection" and payload["version"] == 1
    preview = preview_collection(payload)
    assert preview["count"] == 1 and preview["roi_ids"] == [roi.roi_id]
    imported = import_collection(payload, camera_id="cam_1", ptz_id="ptz_1",
                                 position_id="pos_A", mode="replace")
    assert [r.roi_id for r in imported] == [roi.roi_id]


def test_import_rejects_cross_position_payload():
    editor = _editor()
    roi = editor.create(RoiObjectType.SPOT, SpotGeometry(row=1, col=2))
    payload = export_collection(camera_id="cam_1", ptz_id="ptz_1",
                                position_id="pos_A", rois=[roi])
    with pytest.raises(RoiBindingError):
        import_collection(payload, camera_id="cam_1", ptz_id="ptz_1",
                          position_id="pos_B", mode="replace")


def test_import_rejects_invalid_geometry_before_commit():
    payload = {"format": "tms-roi-collection", "version": 1, "camera_id": "cam_1",
               "ptz_id": "ptz_1", "position_id": "pos_A",
               "rois": [{"roi_id": "bad", "camera_id": "cam_1", "ptz_id": "ptz_1",
                         "position_id": "pos_A", "object_type": "circle",
                         "geometry": {"center_row": 1, "center_col": 1,
                                      "radius": -5},
                         "schema_version": 1}]}
    with pytest.raises(RoiValidationError):
        import_collection(payload, camera_id="cam_1", ptz_id="ptz_1",
                          position_id="pos_A", mode="replace")


def test_import_merge_skips_duplicates():
    editor = _editor()
    roi = editor.create(RoiObjectType.SPOT, SpotGeometry(row=1, col=2))
    payload = export_collection(camera_id="cam_1", ptz_id="ptz_1",
                                position_id="pos_A", rois=[roi])
    assert import_collection(payload, camera_id="cam_1", ptz_id="ptz_1",
                             position_id="pos_A", existing_ids={roi.roi_id},
                             mode="merge") == []


def test_import_rejects_unknown_format_and_version():
    with pytest.raises(RoiValidationError):
        import_collection({"format": "nope", "version": 1}, camera_id="c",
                          ptz_id="p", position_id="q", mode="merge")
    with pytest.raises(RoiValidationError):
        import_collection({"format": "tms-roi-collection", "version": 99},
                          camera_id="c", ptz_id="p", position_id="q",
                          mode="merge")


# -- canvas controller (headless logic) ----------------------------------------

def _headless_canvas():
    from thermal_monitor.roi.coordinate_system import ViewportMapping
    from thermal_monitor.ui.widgets.roi_canvas import RoiCanvasController
    mapping = ViewportMapping(image_width=640, image_height=480,
                              widget_width=640, widget_height=480)
    return RoiCanvasController(lambda: mapping)


def test_canvas_click_creates_spot_and_returns_to_select():
    canvas = _headless_canvas()
    canvas.set_editor(_editor())
    canvas.interaction.active_tool = RoiObjectType.SPOT
    created = []
    canvas.on_created = created.append
    assert canvas.press(100.0, 100.0) is True
    assert len(created) == 1
    assert created[0].geometry.col == pytest.approx(100.0)
    assert canvas.interaction.active_tool is None  # back to Select
    assert canvas.interaction.selected_id == created[0].roi_id


def test_canvas_drag_moves_roi_single_undo():
    canvas = _headless_canvas()
    editor = _editor()
    canvas.set_editor(editor)
    roi = editor.create(RoiObjectType.SPOT, SpotGeometry(row=50, col=50))
    canvas.interaction.active_tool = None
    assert canvas.press(50.0, 50.0) is True  # select + begin move
    canvas.move(60.0, 70.0)
    canvas.release(60.0, 70.0)
    updated = editor.get(roi.roi_id)
    assert (updated.geometry.col, updated.geometry.row) == pytest.approx((60.0, 70.0))
    canvas.undo()
    assert editor.get(roi.roi_id).geometry.col == pytest.approx(50.0)


def test_canvas_drag_resizes_rectangle_via_handle():
    from thermal_monitor.roi.coordinate_system import ViewportMapping
    from thermal_monitor.ui.widgets.roi_canvas import RoiCanvasController
    mapping = ViewportMapping(image_width=640, image_height=480,
                              widget_width=640, widget_height=480)
    canvas = RoiCanvasController(lambda: mapping)
    editor = _editor()
    canvas.set_editor(editor)
    roi = editor.create(RoiObjectType.RECTANGLE,
                        RectangleGeometry(row1=10, col1=10, row2=30, col2=30))
    canvas.interaction.active_tool = None
    canvas.press(20.0, 20.0)  # select interior
    canvas.release(20.0, 20.0)
    assert canvas.interaction.selected_id == roi.roi_id
    canvas.press(30.0, 30.0)  # SE handle grab
    canvas.move(50.0, 50.0)
    canvas.release(50.0, 50.0)
    updated = editor.get(roi.roi_id)
    assert (updated.geometry.col2, updated.geometry.row2) == pytest.approx((50.0, 50.0))


def test_canvas_drag_create_rectangle():
    canvas = _headless_canvas()
    canvas.set_editor(_editor())
    canvas.interaction.active_tool = RoiObjectType.RECTANGLE
    created = []
    canvas.on_created = created.append
    canvas.press(10.0, 10.0)
    canvas.move(40.0, 30.0)
    canvas.release(40.0, 30.0)
    assert len(created) == 1
    assert created[0].object_type == RoiObjectType.RECTANGLE
    assert (created[0].geometry.col2, created[0].geometry.row2) == pytest.approx((40.0, 30.0))


def test_canvas_double_click_finishes_polygon():
    from thermal_monitor.roi.geometry import PolygonGeometry
    canvas = _headless_canvas()
    canvas.set_editor(_editor())
    canvas.interaction.active_tool = RoiObjectType.POLYGON
    created = []
    canvas.on_created = created.append
    canvas.press(0.0, 0.0)
    canvas.release(0.0, 0.0)
    canvas.press(10.0, 0.0)
    canvas.release(10.0, 0.0)
    assert created == []
    canvas.double_click(5.0, 10.0)
    assert len(created) == 1
    assert isinstance(created[0].geometry, PolygonGeometry)


def test_canvas_escape_cancels_and_no_duplicate_on_drag():
    canvas = _headless_canvas()
    canvas.set_editor(_editor())
    canvas.interaction.active_tool = RoiObjectType.POLYGON
    canvas.press(0.0, 0.0)
    canvas.release(0.0, 0.0)
    assert canvas.escape() is True
    assert canvas.interaction.in_progress == []
    # Draw one rectangle via drag: exactly one object, not two.
    canvas.interaction.active_tool = RoiObjectType.RECTANGLE
    created = []
    canvas.on_created = created.append
    canvas.press(5.0, 5.0)
    canvas.move(15.0, 15.0)
    canvas.move(25.0, 25.0)
    canvas.release(25.0, 25.0)
    assert len(created) == 1


def test_canvas_delete_and_empty_deselect():
    canvas = _headless_canvas()
    editor = _editor()
    canvas.set_editor(editor)
    roi = editor.create(RoiObjectType.SPOT, SpotGeometry(row=50, col=50))
    canvas.interaction.active_tool = None
    canvas.press(50.0, 50.0)
    canvas.release(50.0, 50.0)
    deleted = []
    canvas.on_deleted = deleted.append
    assert canvas.delete_selected() is True
    assert deleted == [roi.roi_id]
    assert editor.get(roi.roi_id) is None


def test_coldest_spot_evaluation():
    import numpy as np
    from thermal_monitor.roi.evaluator import RoiEvaluator
    from thermal_monitor.roi.geometry import HottestSpotGeometry
    evaluator = RoiEvaluator()
    image = np.arange(100, dtype=np.float64).reshape(10, 10)
    evaluator.submit(image, 1, 0.0)
    from thermal_monitor.roi.models import RoiDefinition
    roi = RoiDefinition(roi_id="c", camera_id="cam_1", ptz_id="ptz_1",
                        position_id="pos_A", object_type=RoiObjectType.COLDEST_SPOT,
                        geometry=HottestSpotGeometry(row1=0, col1=0, row2=9, col2=9))
    out = evaluator.evaluate([roi], _ctx())
    assert out[0].kind == "coldest_spot"
    assert out[0].values["value"] == pytest.approx(0.0)
