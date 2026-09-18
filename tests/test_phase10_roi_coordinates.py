"""Phase 10C: coordinate conversion tests (widget <-> image, zoom/pan/letterbox)."""

import pytest

from thermal_monitor.roi.coordinate_system import ViewportMapping


def _fit_mapping():
    # 640x480 image in an 800x600 widget: fit scale = 1.25, no letterbox.
    return ViewportMapping(image_width=640, image_height=480,
                           widget_width=800, widget_height=600)


def test_fit_scale_no_letterbox_round_trip():
    m = _fit_mapping()
    assert m.scale == pytest.approx(1.25)
    col, row = m.widget_to_image(400.0, 300.0)
    assert (col, row) == pytest.approx((320.0, 240.0))
    wx, wy = m.image_to_widget(col, row)
    assert (wx, wy) == pytest.approx((400.0, 300.0))


def test_letterbox_offset():
    # Wide widget: image centered horizontally with side bars.
    m = ViewportMapping(image_width=640, image_height=480,
                        widget_width=1000, widget_height=480)
    assert m.scale == pytest.approx(1.0)
    dx, dy = m.draw_origin
    assert dx == pytest.approx(180.0) and dy == pytest.approx(0.0)
    col, row = m.widget_to_image(dx, 0.0)
    assert (col, row) == pytest.approx((0.0, 0.0))


def test_zoom_and_pan_round_trip():
    m = ViewportMapping(image_width=640, image_height=480,
                        widget_width=800, widget_height=600,
                        zoom=2.0, pan_x=10.0, pan_y=-5.0)
    assert m.scale == pytest.approx(2.0)
    for col, row in [(0.0, 0.0), (639.0, 479.0), (123.4, 321.7)]:
        wx, wy = m.image_to_widget(col, row)
        back_col, back_row = m.widget_to_image(wx, wy)
        assert (back_col, back_row) == pytest.approx((col, row), abs=1e-6)


def test_boundary_clamping():
    m = _fit_mapping()
    col, row = m.widget_to_image(-1000.0, -1000.0)
    assert col == 0.0 and row == 0.0
    col, row = m.widget_to_image(100000.0, 100000.0)
    assert col == 639.0 and row == 479.0


def test_resize_stability_image_coords_unchanged():
    small = ViewportMapping(image_width=640, image_height=480,
                            widget_width=400, widget_height=300)
    large = ViewportMapping(image_width=640, image_height=480,
                            widget_width=1600, widget_height=1200)
    # Same stored image point maps to different widget pixels, but the
    # stored image coordinate itself is never modified by resize.
    assert small.image_to_widget(100.0, 100.0) != large.image_to_widget(100.0, 100.0)
    for mapping in (small, large):
        wx, wy = mapping.image_to_widget(100.0, 100.0)
        assert mapping.widget_to_image(wx, wy) == pytest.approx((100.0, 100.0))


def test_is_inside_image():
    m = _fit_mapping()
    assert m.is_inside_image(400.0, 300.0)
    assert not m.is_inside_image(-5.0, 300.0)
