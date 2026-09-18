"""Phase 12.4: ROI UI integration (Configuration workspace, headless/offscreen).

Covers: session table population from the same snapshot as the overlay,
table<->overlay selection sync, shared coordinate conversion (round
trip, letterbox, sizes, boundaries), shape-aware hit testing, drag /
resize commit + cancel, name labels, frame-arrival independence, and
position-switch clearing.

Live/Observer window hosts no ROI UI (verified by audit); all tests
target the Configuration session path.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from thermal_monitor.roi.context import RoiActiveContext
from thermal_monitor.roi.coordinate_system import (
    ViewportMapping,
    mapping_for_widget,
)
from thermal_monitor.roi.editor import RoiEditor
from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.geometry import (
    CircleGeometry,
    EllipseGeometry,
    PolygonGeometry,
    RectangleGeometry,
)
from thermal_monitor.roi.models import RoiDefinition


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _context(position_id="p1", generation=5, roi_ids=("a", "c")):
    return RoiActiveContext(
        camera_id="cam1", ptz_id="ptz1", position_id=position_id,
        position_generation=1, session_generation=2,
        context_generation=generation, state="active",
        roi_ids=roi_ids, image_width=640, image_height=480)


def _rect_def(rid="a", name="Alpha", row1=100.0, col1=100.0,
              row2=200.0, col2=220.0):
    return RoiDefinition(
        roi_id=rid, camera_id="cam1", ptz_id="ptz1", position_id="p1",
        object_type=RoiObjectType.RECTANGLE,
        geometry=RectangleGeometry(row1=row1, col1=col1, row2=row2,
                                   col2=col2),
        name=name)


def _circle_def(rid="c", name="Circ"):
    return RoiDefinition(
        roi_id=rid, camera_id="cam1", ptz_id="ptz1", position_id="p1",
        object_type=RoiObjectType.CIRCLE,
        geometry=CircleGeometry(center_row=300.0, center_col=300.0,
                                radius=40.0),
        name=name)


def _mapping(widget_w=640, widget_h=480):
    return ViewportMapping(image_width=640, image_height=480,
                           widget_width=widget_w, widget_height=widget_h)


# ---------------- coordinate conversion ----------------

def test_round_trip_native_and_scaled():
    for ww, wh in [(640, 480), (1280, 960), (320, 240), (800, 480)]:
        m = _mapping(ww, wh)
        for col, row in [(0.0, 0.0), (639.0, 479.0), (123.4, 321.7)]:
            wx, wy = m.image_to_widget(col, row)
            c2, r2 = m.widget_to_image(wx, wy)
            assert (c2, r2) == pytest.approx((col, row), abs=1.0)


def test_letterboxed_widget_centers_image():
    m = _mapping(800, 480)  # wider than 4:3 -> side bars
    dx, dy = m.draw_origin
    assert dx == pytest.approx((800 - 640) / 2.0)
    assert dy == pytest.approx(0.0)
    assert not m.is_inside_image(10.0, 240.0)  # in the bar
    assert m.is_inside_image(400.0, 240.0)


def test_boundary_clamping():
    m = _mapping()
    assert m.widget_to_image(-100.0, -100.0) == (0.0, 0.0)
    assert m.widget_to_image(9999.0, 9999.0) == (639.0, 479.0)


def test_mapping_for_widget_none_without_image():
    assert mapping_for_widget(object()) is None


def test_mapping_for_widget_from_display_image(qapp):
    from PyQt6.QtGui import QImage
    from PyQt6.QtCore import QPointF

    class Fake:
        _display_image = QImage(640, 480, QImage.Format.Format_RGB32)
        _temperature_image = None
        _zoom = None
        _pan_offset = QPointF(0.0, 0.0)

        def width(self):
            return 800

        def height(self):
            return 600

    m = mapping_for_widget(Fake())
    assert m is not None
    assert m.scale == pytest.approx(600 / 480)
    c, r = m.widget_to_image(*m.image_to_widget(320.0, 240.0))
    assert (c, r) == pytest.approx((320.0, 240.0), abs=1.0)


# ---------------- hit testing ----------------

def _hit(col, row, defs):
    from thermal_monitor.ui.widgets.roi_canvas import _Proxy  # noqa
    from thermal_monitor.ui.widgets.roi_interaction import RoiInteractionState
    # _Proxy is canvas-private; replicate its two fields here.
    items = [type("I", (), {"roi_id": d.roi_id, "geometry": d.geometry})
             for d in defs]
    return RoiInteractionState().hit_test(col, row, items)


def test_rectangle_hit_inside_and_outside():
    assert _hit(150.0, 150.0, [_rect_def()]) == "a"
    assert _hit(10.0, 10.0, [_rect_def()]) is None


def test_circle_hit_inside_edge_outside():
    assert _hit(300.0, 300.0, [_circle_def()]) == "c"  # center
    assert _hit(335.0, 300.0, [_circle_def()]) == "c"  # inside
    assert _hit(300.0 + 40.0 + 3.0, 300.0, [_circle_def()]) == "c"  # boundary
    assert _hit(500.0, 100.0, [_circle_def()]) is None  # far outside


def test_ellipse_hit():
    e = RoiDefinition(
        roi_id="e", camera_id="cam1", ptz_id="ptz1", position_id="p1",
        object_type=RoiObjectType.ELLIPSE,
        geometry=EllipseGeometry(center_row=200.0, center_col=200.0,
                                 radius1=60.0, radius2=20.0, phi=0.0),
        name="E")
    assert _hit(200.0, 200.0, [e]) == "e"
    assert _hit(200.0, 200.0 + 60.0 + 30.0, [e]) is None  # beyond row extent
    assert _hit(200.0 + 20.0 + 30.0, 200.0, [e]) is None  # beyond col extent


def test_polygon_hit_inside_and_outside():
    p = RoiDefinition(
        roi_id="p", camera_id="cam1", ptz_id="ptz1", position_id="p1",
        object_type=RoiObjectType.POLYGON,
        geometry=PolygonGeometry(points=((100.0, 100.0), (100.0, 200.0),
                                          (200.0, 150.0))),
        name="P")
    assert _hit(150.0, 130.0, [p]) == "p"
    assert _hit(400.0, 400.0, [p]) is None


def test_overlap_last_rendered_wins_and_outside_clears():
    first = _rect_def(rid="first", row1=100.0, col1=100.0,
                      row2=200.0, col2=200.0)
    second = _rect_def(rid="second", row1=100.0, col1=100.0,
                       row2=200.0, col2=200.0)
    assert _hit(150.0, 150.0, [first, second]) == "second"


# ---------------- session table ----------------

class _StubService:
    def __init__(self, analysis=None):
        self._analysis = analysis

    def get_analysis_config(self, camera_id):
        return self._analysis


def _panel(qapp, service=None):
    from thermal_monitor.ui.widgets.roi_panel import ROIPanel
    return ROIPanel(service or _StubService())


def test_session_table_populates_from_snapshot(qapp):
    panel = _panel(qapp)
    rois = [_rect_def(), _circle_def()]
    panel.set_session_rois(rois, camera_id="cam1", position_id="p1",
                           context_generation=5)
    assert panel.session_key == ("cam1", "p1", 5)
    assert panel._roi_tree.topLevelItemCount() == 2
    ids = {panel._roi_tree.topLevelItem(i).data(
        0, Qt.ItemDataRole.UserRole) for i in range(2)}
    assert ids == {"a", "c"}
    names = {panel._roi_tree.topLevelItem(i).text(1) for i in range(2)}
    assert names == {"Alpha", "Circ"}


def test_session_table_empty_and_clear(qapp):
    panel = _panel(qapp)
    panel.set_session_rois([], camera_id="cam1", position_id="p1",
                           context_generation=5)
    assert panel._roi_tree.topLevelItemCount() == 0
    panel.clear_session()
    assert panel.session_key is None
    assert panel._roi_tree.topLevelItemCount() == 0


def test_session_table_position_switch_replaces_rows(qapp):
    panel = _panel(qapp)
    panel.set_session_rois([_rect_def()], camera_id="cam1",
                           position_id="p1", context_generation=5)
    other = RoiDefinition(
        roi_id="z", camera_id="cam1", ptz_id="ptz1", position_id="p2",
        object_type=RoiObjectType.RECTANGLE,
        geometry=RectangleGeometry(row1=10.0, col1=10.0, row2=20.0,
                                   col2=20.0),
        name="Zed")
    panel.set_session_rois([other], camera_id="cam1", position_id="p2",
                           context_generation=6)
    assert panel._roi_tree.topLevelItemCount() == 1
    assert panel._roi_tree.topLevelItem(0).text(0) == "z"


def test_programmatic_row_selection(qapp):
    panel = _panel(qapp)
    panel.set_session_rois([_rect_def(), _circle_def()], camera_id="cam1",
                           position_id="p1", context_generation=5)
    assert panel.select_roi("c") is True
    assert panel._selected_roi_id == "c"
    assert panel.select_roi("missing") is False
    assert panel.select_roi(None) is True
    assert panel._selected_roi_id is None


# ---------------- canvas: overlay, selection, editing ----------------

def _controller(rois, widget_w=640, widget_h=480):
    from thermal_monitor.ui.widgets.roi_canvas import RoiCanvasController
    controller = RoiCanvasController(
        lambda: _mapping(widget_w, widget_h), toolbar=None, editable=True)
    controller.set_editor(RoiEditor(_context(), list(rois)))
    return controller


def test_overlay_and_table_share_snapshot(qapp):
    rois = [_rect_def(), _circle_def()]
    controller = _controller(rois)
    panel = _panel(qapp)
    panel.set_session_rois(controller.editor.rois, camera_id="cam1",
                           position_id="p1", context_generation=5)
    overlay_ids = {o.roi_id for o in controller.build_overlays()}
    table_ids = {panel._roi_tree.topLevelItem(i).text(0)
                 for i in range(panel._roi_tree.topLevelItemCount())}
    assert overlay_ids == table_ids == {"a", "c"}


def test_overlays_carry_names():
    controller = _controller([_rect_def(name="Alpha")])
    (overlay,) = controller.build_overlays()
    assert overlay.name == "Alpha"
    assert overlay.label == "Alpha"


def test_overlay_label_falls_back_to_id():
    controller = _controller([_rect_def(name="")])
    (overlay,) = controller.build_overlays()
    assert overlay.label == "a"


def test_canvas_select_validates_and_syncs(qapp):
    controller = _controller([_rect_def(), _circle_def()])
    seen = []
    controller.on_selected = seen.append
    assert controller.select("c") is True
    assert controller.interaction.selected_id == "c"
    assert seen == ["c"]
    assert controller.select("ghost") is True  # unknown clears
    assert controller.interaction.selected_id is None
    panel = _panel(qapp)
    panel.set_session_rois(controller.editor.rois, camera_id="cam1",
                           position_id="p1", context_generation=5)
    panel.select_roi("a")
    assert panel._selected_roi_id == "a"


def test_click_selects_and_outside_clears():
    controller = _controller([_rect_def()])
    assert controller.press(150.0, 150.0) is True
    assert controller.interaction.selected_id == "a"
    controller.release(150.0, 150.0)
    assert controller.press(10.0, 10.0) is True
    assert controller.interaction.selected_id is None


def test_drag_rectangle_commits_on_release():
    changed = []
    controller = _controller([_rect_def()])
    controller.on_changed = lambda roi: changed.append(roi.roi_id)
    assert controller.press(150.0, 150.0) is True
    assert controller.move(170.0, 160.0) is True
    assert controller.release(170.0, 160.0) is True
    roi = controller.editor.get("a")
    assert (roi.geometry.row1, roi.geometry.col1,
            roi.geometry.row2, roi.geometry.col2) == pytest.approx(
                (110.0, 120.0, 210.0, 240.0))
    assert changed == ["a"]  # committed once via end_drag


def test_resize_rectangle_via_handle():
    controller = _controller([_rect_def()])
    controller.select("a", emit=False)
    mapping = _mapping()
    hx, hy = mapping.image_to_widget(220.0, 200.0)  # se handle
    assert controller.press(hx, hy) is True
    assert controller._drag_mode == "handle"
    assert controller.move(230.0, 210.0) is True
    assert controller.release(230.0, 210.0) is True
    roi = controller.editor.get("a")
    assert (roi.geometry.col2, roi.geometry.row2) == pytest.approx(
        (230.0, 210.0))


def test_escape_cancels_drag():
    controller = _controller([_rect_def()])
    assert controller.press(150.0, 150.0) is True
    assert controller.move(190.0, 190.0) is True
    controller.escape()
    roi = controller.editor.get("a")
    assert (roi.geometry.row1, roi.geometry.col1) == pytest.approx(
        (100.0, 100.0))


def test_position_switch_during_edit_clears_state():
    controller = _controller([_rect_def()])
    assert controller.press(150.0, 150.0) is True
    assert controller.move(170.0, 170.0) is True
    controller.set_editor(None)  # position switch / clear
    assert controller.interaction.selected_id is None
    assert controller.release(170.0, 170.0) is False  # no crash, no commit


def test_selection_survives_overlay_rebuilds():
    controller = _controller([_rect_def(), _circle_def()])
    controller.select("c", emit=False)
    for _ in range(3):  # simulated frame arrivals repaint only
        overlays = controller.build_overlays()
    assert controller.interaction.selected_id == "c"
    selected = [o for o in overlays if o.selected]
    assert [o.roi_id for o in selected] == ["c"]


# ---------------- window wiring (unbound methods, mock host) ----------------

def _wired_host(qapp):
    from unittest.mock import MagicMock
    from thermal_monitor.ui.widgets.roi_canvas import RoiCanvasController
    from thermal_monitor.ui.windows.configuration_window import (
        ConfigurationModeWidget as Window,
    )
    controller = RoiCanvasController(lambda: _mapping(), toolbar=None,
                                     editable=True)
    controller.set_editor(RoiEditor(_context(), [_rect_def(),
                                                 _circle_def()]))
    host = MagicMock()
    host._roi_canvas = controller
    host._roi_panel = _panel(qapp)
    host._image_widget = MagicMock()
    host._roi_toolbar = MagicMock()
    host._roi_session_active = True
    host._repaint_roi_session = (
        lambda: Window._repaint_roi_session(host))
    host._refresh_session_table = (
        lambda: Window._refresh_session_table(host))
    return host, Window


def test_overlay_to_table_sync_through_window(qapp):
    host, Window = _wired_host(qapp)
    host._roi_panel.set_session_rois(
        host._roi_canvas.editor.rois, camera_id="cam1", position_id="p1",
        context_generation=5)
    Window._on_roi_canvas_selected(host, "c")
    assert host._roi_panel._selected_roi_id == "c"
    assert host._image_widget.set_roi_overlays.called


def test_table_to_overlay_sync_through_window(qapp):
    host, Window = _wired_host(qapp)
    host._roi_panel.set_session_rois(
        host._roi_canvas.editor.rois, camera_id="cam1", position_id="p1",
        context_generation=5)
    Window._on_roi_selected(host, "a")
    assert host._roi_canvas.interaction.selected_id == "a"


def test_canvas_mutation_refreshes_session_table(qapp):
    host, Window = _wired_host(qapp)
    host._roi_panel.set_session_rois(
        host._roi_canvas.editor.rois, camera_id="cam1", position_id="p1",
        context_generation=5)
    host._roi_canvas.editor.delete("a")
    Window._on_roi_canvas_deleted(host, "a")
    assert host._roi_panel._roi_tree.topLevelItemCount() == 1
    assert host._roi_panel._roi_tree.topLevelItem(0).text(0) == "c"


def test_session_load_installs_table_and_overlay(qapp):
    from thermal_monitor.roi.serialization import roi_to_dict
    host, Window = _wired_host(qapp)
    host._ptz_is_current = lambda camera_id, generation: True
    host._pos_panel = None
    from unittest.mock import MagicMock
    host._roi_counts = MagicMock()
    host._ptz_notice = MagicMock()
    rois = [_rect_def(), _circle_def()]
    context = _context()
    payload = {"context": context,
               "roi_dicts": [roi_to_dict(r) for r in rois]}
    # Fresh host canvas without editor (pre-session state).
    from thermal_monitor.ui.widgets.roi_canvas import RoiCanvasController
    host._roi_canvas = RoiCanvasController(lambda: _mapping(), toolbar=None,
                                           editable=True)
    host._roi_session_active = False
    Window._on_roi_session_loaded(host, "cam1", 9, payload)
    assert host._roi_session_active is True
    assert host._roi_panel.session_key == ("cam1", "p1", 5)
    assert host._roi_panel._roi_tree.topLevelItemCount() == 2
    assert host._image_widget.set_roi_overlays.called
    overlays = host._image_widget.set_roi_overlays.call_args[0][0]
    assert {o.roi_id for o in overlays} == {"a", "c"}
    assert {o.name for o in overlays} == {"Alpha", "Circ"}


def test_stale_session_load_rejected(qapp):
    host, Window = _wired_host(qapp)
    host._ptz_is_current = lambda camera_id, generation: False
    before = host._roi_panel._roi_tree.topLevelItemCount()
    Window._on_roi_session_loaded(host, "cam1", 9, {"context": None})
    assert host._roi_session_active is True  # unchanged host flag
    assert host._roi_panel._roi_tree.topLevelItemCount() == before

# ---------------- labels on the painted widget ----------------

def test_painted_labels_do_not_crash(qapp):
    import numpy as np
    from PyQt6.QtGui import QImage
    from thermal_monitor.ui.modes.observer_image import (
        LiveThermalWidget,
        ROIOverlay,
    )
    widget = LiveThermalWidget()
    widget.resize(640, 480)
    widget._temperature_image = np.zeros((480, 640), dtype=np.float32)
    widget._display_image = QImage(640, 480, QImage.Format.Format_RGB32)
    widget._display_image.fill(0)
    widget.set_roi_overlays([
        ROIOverlay(roi_id="a", shape="rectangle1",
                   geometry={"y1": 100, "x1": 100, "y2": 200, "x2": 220},
                   name="Alpha"),
        ROIOverlay(roi_id="b", shape="rectangle1",
                   geometry={"y1": 0, "x1": 0, "y2": 479, "x2": 639},
                   name="X" * 120),  # long name near boundaries
        ROIOverlay(roi_id="c", shape="circle",
                   geometry={"center_y": 300, "center_x": 300,
                             "radius": 40},
                   selected=True),  # empty name -> roi_id fallback
        ROIOverlay(roi_id="d", shape="polygon",
                   geometry={"points": [(10, 10), (10, 60), (60, 35)]},
                   name="Poly"),
    ])
    widget.show()
    widget.update()
    qapp.processEvents()
    widget.hide()
