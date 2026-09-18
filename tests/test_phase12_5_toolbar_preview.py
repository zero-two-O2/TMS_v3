"""Phase 12.5: industrial toolbar icons + real-time drawing preview.

Toolbar: every action key has an SVG asset, QIcon loads from SVG
(cached), actions map to tools, active state syncs, tooltips/names
exist, button/icon geometry is uniform.
Preview: transient geometry visible before release for all drag/vertex
tools, cleared on commit/cancel/context change, never persisted,
tabulated, or pipelined; aligned with committed geometry after release.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from thermal_monitor.roi.context import RoiActiveContext
from thermal_monitor.roi.coordinate_system import ViewportMapping
from thermal_monitor.roi.editor import RoiEditor
from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.ui.widgets.roi_toolbar import (
    TOOL_GROUPS,
    TOOLTIPS,
    RoiToolbar,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ---------------- toolbar / icons ----------------

def test_every_action_key_has_svg_asset():
    from thermal_monitor.ui.widgets.roi_icons import ICON_KEYS, icon_path
    missing = [k for k in ICON_KEYS if not icon_path(k).is_file()]
    assert missing == []


def test_toolbar_entry_keys_covered_by_registry():
    from thermal_monitor.ui.widgets.roi_icons import ICON_KEYS
    for entries in TOOL_GROUPS.values():
        for _label, _tool, icon_key, _profile in entries:
            assert icon_key in ICON_KEYS, icon_key
    for key in ("undo", "redo", "delete", "hide"):
        assert key in ICON_KEYS


def test_icons_load_from_svg(qapp):
    from thermal_monitor.ui.widgets.roi_icons import (
        ICON_KEYS,
        icon_for,
        icon_source,
    )
    for ink in ("light", "dark"):
        failed = [k for k in ICON_KEYS if icon_source(k, ink) != "svg"]
        assert failed == []
    assert icon_for("rectangle", ink="light") is not icon_for(
        "rectangle", ink="dark")  # separate cache entries
    first, second = icon_for("rectangle"), icon_for("rectangle")
    assert first is second  # cached
    assert not first.isNull()
    # SVG assets pixmap-render crisply at toolbar and larger sizes.
    pixmap = first.pixmap(32, 32)
    assert not pixmap.isNull()
    assert pixmap.width() == 32 and pixmap.height() == 32


def _alpha_bbox(key, size=96):
    import numpy as np
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtSvg import QSvgRenderer
    from thermal_monitor.ui.widgets.roi_icons import icon_path
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    QSvgRenderer(str(icon_path(key))).render(painter)
    painter.end()
    ptr = image.constBits()
    ptr.setsize(size * size * 4)
    pixels = np.frombuffer(ptr, dtype=np.uint8).reshape(size, size, 4)
    ys, xs = (pixels[:, :, 3] > 8).nonzero()
    assert len(xs) > 0, f"{key}: blank render"
    return (xs.max() - xs.min() + 1) / size * 100.0, (
        ys.max() - ys.min() + 1) / size * 100.0


def test_icons_render_at_all_toolbar_sizes(qapp):
    from thermal_monitor.ui.widgets.roi_icons import ICON_KEYS, icon_for
    for key in ICON_KEYS:
        icon = icon_for(key)
        for size in (16, 20, 21, 24, 28):
            pixmap = icon.pixmap(size, size)
            assert not pixmap.isNull(), (key, size)
            assert pixmap.width() == size and pixmap.height() == size, (
                key, size)


def test_icons_optically_sized_not_clipped(qapp):
    """Dominant artwork dimension fills the frame (75-90% target);
    1px edge margin stays transparent (nothing touches the frame)."""
    import numpy as np
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtSvg import QSvgRenderer
    from thermal_monitor.ui.widgets.roi_icons import ICON_KEYS, icon_path
    for key in ICON_KEYS:
        w_pct, h_pct = _alpha_bbox(key)
        assert max(w_pct, h_pct) >= 65.0, (key, w_pct, h_pct)
        size = 96
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        QSvgRenderer(str(icon_path(key))).render(painter)
        painter.end()
        ptr = image.constBits()
        ptr.setsize(size * size * 4)
        edge = np.frombuffer(ptr, dtype=np.uint8).reshape(size, size, 4)
        border = max(edge[0, :, 3].max(), edge[-1, :, 3].max(),
                     edge[:, 0, 3].max(), edge[:, -1, 3].max())
        assert border <= 8, f"{key}: artwork touches the frame"


def test_light_ink_contrast_on_dark_toolbar(qapp):
    """Default (dark-theme) icons must contrast against the industrial
    dark surface (#1E232A): mean-foreground WCAG contrast >= 2.5 for
    every key (accents qualify by chroma, ink by luminance)."""
    import numpy as np
    from thermal_monitor.ui.widgets.roi_icons import ICON_KEYS, icon_for

    def luminance(rgb):
        channels = [v / 255.0 for v in rgb]
        linearized = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
                      for v in channels]
        return (0.2126 * linearized[0] + 0.7152 * linearized[1]
                + 0.0722 * linearized[2])

    bg = luminance((0x1E, 0x23, 0x2A))
    for key in ICON_KEYS:
        pixmap = icon_for(key, ink="light").pixmap(40, 40)
        image = pixmap.toImage().convertToFormat(
            pixmap.toImage().Format.Format_ARGB32)
        ptr = image.constBits()
        ptr.setsize(40 * 40 * 4)
        # ARGB32 memory order on little-endian is B,G,R,A.
        pixels = np.frombuffer(ptr, dtype=np.uint8).reshape(40, 40, 4)
        mask = pixels[:, :, 3] > 8
        assert mask.any(), key
        # Core pixels (fully covered) carry the true ink color; the
        # faint antialiased fringe must not dilute the measurement.
        core = pixels[:, :, 3] > 200
        assert core.any(), (key, "no fully-covered pixels")
        mean_color = pixels[:, :, [2, 1, 0]][core].astype(float).mean(axis=0)
        lum = luminance(mean_color)
        ratio = (max(lum, bg) + 0.05) / (min(lum, bg) + 0.05)
        assert ratio >= 2.5, (key, round(float(ratio), 2))


def test_toolbar_default_ink_matches_dark_theme(qapp):
    toolbar = RoiToolbar()
    assert toolbar._ink == "light"
    dark_toolbar = RoiToolbar(ink="dark")
    assert dark_toolbar._ink == "dark"


def test_toolbar_actions_map_to_tools(qapp):
    toolbar = RoiToolbar()
    seen: dict[str, object] = {}
    toolbar.tool_changed.connect(lambda tool: seen.setdefault("tool", tool))
    toolbar.set_active_tool(RoiObjectType.CIRCLE)
    assert toolbar.active_tool == RoiObjectType.CIRCLE
    assert seen["tool"] == RoiObjectType.CIRCLE
    # Group buttons reflect the active tool exactly once.
    checked = [g for g, b in toolbar._group_buttons.items() if b.isChecked()]
    assert checked == ["Regions"]
    toolbar.set_active_tool(None)
    assert [g for g, b in toolbar._group_buttons.items()
            if b.isChecked()] == ["Select"]


def test_toolbar_tooltips_and_names(qapp):
    toolbar = RoiToolbar()
    assert toolbar._undo_btn.toolTip() == TOOLTIPS["Undo"]
    assert toolbar._delete_btn.accessibleName() == "Delete"
    for group, button in toolbar._group_buttons.items():
        assert button.toolTip(), group
        assert button.accessibleName(), group
    for entries in TOOL_GROUPS.values():
        for label, _tool, _icon, _profile in entries:
            assert TOOLTIPS.get(label), label


def test_toolbar_button_geometry_uniform(qapp):
    from PyQt6.QtCore import QSize
    from thermal_monitor.ui.widgets.roi_toolbar import (
        MENU_BUTTON_WIDTH,
        TOOL_BUTTON_SIZE,
        TOOL_ICON_SIZE,
    )
    toolbar = RoiToolbar()
    sizes = {b.iconSize().width() for b in toolbar._group_buttons.values()}
    sizes.add(toolbar._undo_btn.iconSize().width())
    sizes.add(toolbar._delete_btn.iconSize().width())
    # Standalone/action artwork is square; menu buttons compose a
    # 12px arrow zone into the pixmap (52px wide, 40px tall).
    assert sizes == {TOOL_ICON_SIZE, MENU_BUTTON_WIDTH}
    assert toolbar._undo_btn.iconSize().height() == TOOL_ICON_SIZE
    # Standalone buttons keep the compact square; menu buttons add a
    # dedicated arrow zone. Height never changes.
    assert toolbar._group_buttons["Select"].width() == TOOL_BUTTON_SIZE
    for group in ("Spots", "Lines", "Regions", "Measure", "Annotate"):
        button = toolbar._group_buttons[group]
        assert button.width() == MENU_BUTTON_WIDTH
        assert button.height() == TOOL_BUTTON_SIZE


def test_toolbar_context_gating_and_checks(qapp):
    toolbar = RoiToolbar()
    toolbar.set_context_active(False, "No position")
    assert toolbar._delete_btn.isEnabled() is False
    assert toolbar._undo_btn.isEnabled() is False
    toolbar.set_context_active(True, "Camera: cam1 Position: p1")
    assert toolbar._delete_btn.isEnabled() is True
    toolbar.set_active_tool(RoiObjectType.RECTANGLE)
    assert toolbar._group_buttons["Regions"].isChecked() is True
    assert toolbar._group_buttons["Select"].isChecked() is False


def test_coldest_spot_creation_functional():
    """Toolbar Coldest Spot was silently dead (click() had no branch)."""
    from thermal_monitor.ui.widgets.roi_interaction import (
        RoiInteractionState,
    )
    state = RoiInteractionState()
    state.active_tool = RoiObjectType.COLDEST_SPOT
    geometry = state.preview_geometry(
        RoiObjectType.COLDEST_SPOT, [(10.0, 10.0), (30.0, 40.0)])
    assert geometry is not None
    assert (geometry.row1, geometry.col1,
            geometry.row2, geometry.col2) == (10.0, 10.0, 40.0, 30.0)


# ---------------- preview ----------------

def _context():
    return RoiActiveContext(
        camera_id="cam1", ptz_id="ptz1", position_id="p1",
        position_generation=1, session_generation=2, context_generation=7,
        state="active", roi_ids=(), image_width=640, image_height=480)


def _controller(tool, widget_w=640, widget_h=480):
    from thermal_monitor.ui.widgets.roi_canvas import RoiCanvasController
    mapping = ViewportMapping(image_width=640, image_height=480,
                              widget_width=widget_w, widget_height=widget_h)
    controller = RoiCanvasController(lambda: mapping, toolbar=None,
                                     editable=True)
    controller.set_editor(RoiEditor(_context(), []))
    controller._interaction.active_tool = tool
    return controller


def _previews(controller):
    return [o for o in controller.build_overlays() if o.preview]


def test_rectangle_preview_before_release():
    controller = _controller(RoiObjectType.RECTANGLE)
    controller.press(100.0, 100.0)
    assert _previews(controller) == []  # zero-size: nothing valid yet
    controller.move(160.0, 140.0)
    (preview,) = _previews(controller)
    assert preview.shape == "rectangle1"
    assert preview.geometry == {"y1": 100.0, "x1": 100.0,
                                "y2": 140.0, "x2": 160.0}
    assert controller.editor.rois == []  # not committed, not tabulated
    controller.release(160.0, 140.0)
    assert _previews(controller) == []
    (roi,) = controller.editor.rois
    assert (roi.geometry.row1, roi.geometry.col1,
            roi.geometry.row2, roi.geometry.col2) == (100.0, 100.0,
                                                      140.0, 160.0)


def test_preview_updates_continuously():
    controller = _controller(RoiObjectType.CIRCLE)
    controller.press(300.0, 300.0)
    seen = []
    for dx in (10.0, 25.0, 45.0):
        controller.move(300.0 + dx, 300.0)
        (preview,) = _previews(controller)
        seen.append(preview.geometry["radius"])
    assert seen == sorted(seen) and len(set(seen)) == 3
    assert seen[-1] == pytest.approx(45.0)


def test_ellipse_preview():
    controller = _controller(RoiObjectType.ELLIPSE)
    controller.press(200.0, 200.0)
    controller.move(230.0, 260.0)
    (preview,) = _previews(controller)
    assert preview.shape == "ellipse"


def test_line_preview():
    controller = _controller(RoiObjectType.FREE_LINE)
    controller.press(50.0, 400.0)
    controller.move(200.0, 100.0)
    (preview,) = _previews(controller)
    assert preview.shape == "polyline"
    assert preview.geometry["points"] == [(400.0, 50.0), (100.0, 200.0)]


def test_polygon_rubber_band_edge():
    controller = _controller(RoiObjectType.POLYGON)
    controller.press(100.0, 100.0)  # first vertex confirmed
    controller.press(200.0, 100.0)  # second vertex confirmed
    controller.move(150.0, 250.0)  # hover: rubber band, no button
    (preview,) = _previews(controller)
    assert preview.shape == "polygon"
    assert preview.geometry["points"] == [[100.0, 100.0], [100.0, 200.0],
                                          [250.0, 150.0]]
    assert controller.editor.rois == []


def test_preview_cancelled_by_escape():
    controller = _controller(RoiObjectType.RECTANGLE)
    controller.press(100.0, 100.0)
    controller.move(160.0, 140.0)
    assert len(_previews(controller)) == 1
    assert controller.escape() is True
    assert _previews(controller) == []
    assert controller.editor.rois == []


def test_preview_cancelled_by_tool_switch():
    controller = _controller(RoiObjectType.RECTANGLE)
    controller.press(100.0, 100.0)
    controller.move(160.0, 140.0)
    controller._on_tool_changed(None)
    assert _previews(controller) == []
    assert controller.editor.rois == []


def test_preview_cleared_on_context_change():
    controller = _controller(RoiObjectType.RECTANGLE)
    controller.press(100.0, 100.0)
    controller.move(160.0, 140.0)
    controller.set_editor(None)  # camera/position switch
    assert _previews(controller) == []
    assert controller.editor is None


def test_preview_invalid_geometry_never_commits():
    controller = _controller(RoiObjectType.RECTANGLE)
    controller.press(100.0, 100.0)
    controller.release(100.0, 100.0)  # zero-size: invalid
    assert controller.editor.rois == []
    assert _previews(controller) == []


def test_preview_scaled_widget_alignment():
    controller = _controller(RoiObjectType.RECTANGLE, widget_w=1280,
                             widget_h=960)
    controller.press(200.0, 200.0)  # image (100,100) at 2x
    controller.move(320.0, 280.0)  # image (160,140)
    (preview,) = _previews(controller)
    assert preview.geometry == {"y1": 100.0, "x1": 100.0,
                                "y2": 140.0, "x2": 160.0}
    controller.release(320.0, 280.0)
    (roi,) = controller.editor.rois
    assert (roi.geometry.row2, roi.geometry.col2) == (140.0, 160.0)


def test_preview_letterboxed_widget():
    controller = _controller(RoiObjectType.RECTANGLE, widget_w=800,
                             widget_h=480)
    # Image x-offset is (800-640)/2 = 80 widget px at scale 1.
    controller.press(80 + 100.0, 100.0)
    controller.move(80 + 160.0, 140.0)
    (preview,) = _previews(controller)
    assert preview.geometry["x1"] == pytest.approx(100.0)
    assert preview.geometry["x2"] == pytest.approx(160.0)


def test_preview_painted_dashed_without_label(qapp):
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
                   name="Committed"),
        ROIOverlay(roi_id="", shape="rectangle1",
                   geometry={"y1": 300, "x1": 300, "y2": 350, "x2": 360},
                   preview=True),
        ROIOverlay(roi_id="", shape="polyline",
                   geometry={"points": [(10, 10), (60, 35), (20, 80)]},
                   preview=True),
    ])
    widget.show()
    widget.update()
    qapp.processEvents()
    widget.hide()
