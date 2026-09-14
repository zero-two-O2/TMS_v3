"""
Tests for the Configuration workstation IR zoom/pan and View Finder.

View-only behavior: the source thermal frame, conversion and calibration
are never altered; zoom/pan change only the widget transform. Headless
(offscreen) throughout.
"""

from __future__ import annotations

import numpy as np
import pytest

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QImage, QKeyEvent, QMouseEvent, QWheelEvent
from PyQt6.QtWidgets import QApplication

import thermal_monitor.camera.source  # noqa: F401  (init camera package first)
from thermal_monitor.ui.modes.observer_image import LiveThermalWidget
from thermal_monitor.ui.modes.view_finder import ViewFinderWidget


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def ir_widget(qapp):
    widget = LiveThermalWidget()
    widget.resize(640, 480)
    widget.show()
    qapp.processEvents()
    image = QImage(640, 480, QImage.Format.Format_RGB888)
    image.fill(0)
    widget._display_image = image
    widget._temperature_image = np.zeros((480, 640), dtype=np.float64)
    yield widget
    widget.close()


def _wheel(pos: QPointF, angle_y: int) -> QWheelEvent:
    return QWheelEvent(
        pos,
        QPointF(0, 0),
        QPoint(0, 0),
        QPoint(0, angle_y),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


def test_wheel_up_zooms_in(ir_widget) -> None:
    assert ir_widget.is_fit()
    ir_widget.wheelEvent(_wheel(QPointF(320, 240), 120))
    assert not ir_widget.is_fit()
    assert ir_widget._zoom is not None and ir_widget._zoom > 0
    assert ir_widget.zoom_percent() != "Fit"


def test_wheel_down_at_fit_stays_fit(ir_widget) -> None:
    """Fit is the minimum zoom: wheeling down never inverts or breaks."""
    ir_widget.wheelEvent(_wheel(QPointF(320, 240), -120))
    assert ir_widget.is_fit()
    assert ir_widget.viewport_rect_normalized() is None


def test_zoom_is_bounded(ir_widget) -> None:
    for _ in range(60):
        ir_widget.zoom_in()
    assert ir_widget._zoom <= LiveThermalWidget._ZOOM_MAX
    for _ in range(200):
        ir_widget.zoom_out()
    assert ir_widget.is_fit()


def test_fit_restores_full_view(ir_widget) -> None:
    ir_widget.zoom_in()
    ir_widget.zoom_in()
    assert not ir_widget.is_fit()
    ir_widget.zoom_fit()
    assert ir_widget.is_fit()
    assert ir_widget.zoom_percent() == "Fit"
    assert ir_widget.viewport_rect_normalized() is None


def test_one_to_one(ir_widget) -> None:
    ir_widget.zoom_one_to_one()
    assert ir_widget._zoom == pytest.approx(1.0)
    assert ir_widget.zoom_percent() == "100%"


def test_zoom_anchored_around_cursor(ir_widget) -> None:
    """The image point under the cursor stays under it while zooming."""
    ir_widget.resize(800, 600)
    cursor = QPointF(600, 100)
    before = ir_widget._widget_to_image_coords(cursor.x(), cursor.y())
    ir_widget.zoom_at(cursor, 2.0)
    after = ir_widget._widget_to_image_coords(cursor.x(), cursor.y())
    assert abs(before[0] - after[0]) <= 1
    assert abs(before[1] - after[1]) <= 1


def test_left_drag_pans_when_zoomed(ir_widget, qapp) -> None:
    ir_widget.zoom_one_to_one()
    ir_widget.resize(320, 240)  # smaller than the 1:1 image: pan range exists
    qapp.processEvents()
    pan_before = QPointF(ir_widget._pan_offset)
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(160, 120),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    ir_widget.mousePressEvent(press)
    assert ir_widget._is_panning
    move = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(100, 90),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    ir_widget.mouseMoveEvent(move)
    release = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPointF(100, 90),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    ir_widget.mouseReleaseEvent(release)
    assert not ir_widget._is_panning
    assert ir_widget._pan_offset != pan_before


def test_pan_stays_constrained(ir_widget) -> None:
    ir_widget.zoom_one_to_one()
    ir_widget.set_pan_offset(1e6, -1e6)
    transform = ir_widget._view_transform()
    assert transform is not None
    _, draw_x, draw_y, scaled_w, scaled_h = transform
    # Image still covers the viewport: no oversized empty areas.
    assert draw_x <= 0
    assert draw_y <= 0
    assert draw_x + scaled_w >= ir_widget.width()
    assert draw_y + scaled_h >= ir_widget.height()


def test_source_image_unchanged_by_view_ops(ir_widget, qapp) -> None:
    source_id = id(ir_widget._display_image)
    thermal_copy = ir_widget._temperature_image.copy()
    for _ in range(5):
        ir_widget.zoom_in()
    ir_widget.pan_to_normalized(0.7, 0.3)
    ir_widget.zoom_fit()
    qapp.processEvents()
    assert id(ir_widget._display_image) == source_id
    assert np.array_equal(ir_widget._temperature_image, thermal_copy)


def test_keyboard_shortcuts(ir_widget) -> None:
    plus = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Plus, Qt.KeyboardModifier.NoModifier, "+")
    ir_widget.keyPressEvent(plus)
    assert not ir_widget.is_fit()
    zero = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_0, Qt.KeyboardModifier.NoModifier, "0")
    ir_widget.keyPressEvent(zero)
    assert ir_widget.is_fit()
    one = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_1, Qt.KeyboardModifier.NoModifier, "1")
    ir_widget.keyPressEvent(one)
    assert ir_widget._zoom == pytest.approx(1.0)


def test_view_changed_emitted(ir_widget) -> None:
    seen: list[bool] = []
    ir_widget.view_changed.connect(lambda: seen.append(True))
    ir_widget.zoom_in()
    assert seen


# ---------------------------------------------------------------------------
# View Finder unit behavior
# ---------------------------------------------------------------------------


def _finder_image() -> QImage:
    image = QImage(640, 480, QImage.Format.Format_RGB888)
    image.fill(0)
    return image


def test_finder_shows_full_image(qapp) -> None:
    finder = ViewFinderWidget()
    finder.resize(200, 150)
    finder.show()
    qapp.processEvents()
    image = _finder_image()
    finder.set_image(image)
    assert finder._image is image  # shared, not copied
    assert finder.viewport is None
    finder.close()


def test_finder_viewport_rect_math(qapp) -> None:
    finder = ViewFinderWidget()
    finder.set_image(_finder_image())
    finder.set_viewport((0.25, 0.25, 0.75, 0.75))
    assert finder.viewport == (0.25, 0.25, 0.75, 0.75)
    # Degenerate rectangles collapse to full-image (None).
    finder.set_viewport((0.5, 0.5, 0.5, 0.5))
    assert finder.viewport is None
    finder.close()


def test_finder_viewport_constrained(qapp) -> None:
    finder = ViewFinderWidget()
    finder.set_image(_finder_image())
    finder.set_viewport((-2.0, -1.0, 5.0, 5.0))
    x0, y0, x1, y1 = finder.viewport
    assert (x0, y0, x1, y1) == (0.0, 0.0, 1.0, 1.0)
    finder.close()


def test_finder_drag_emits_normalized_center(qapp) -> None:
    finder = ViewFinderWidget()
    finder.resize(200, 150)
    finder.show()
    qapp.processEvents()
    finder.set_image(_finder_image())
    received: list[tuple[float, float]] = []
    finder.viewport_dragged.connect(lambda nx, ny: received.append((nx, ny)))
    geometry = finder._image_geometry()
    assert geometry is not None
    draw_x, draw_y, draw_w, draw_h = geometry
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(draw_x + draw_w * 0.8, draw_y + draw_h * 0.2),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    finder.mousePressEvent(press)
    assert received
    assert received[-1][0] == pytest.approx(0.8, abs=0.02)
    assert received[-1][1] == pytest.approx(0.2, abs=0.02)
    finder.close()


def test_main_view_rect_tracks_zoom_and_pan(ir_widget) -> None:
    assert ir_widget.viewport_rect_normalized() is None  # fit: whole image
    ir_widget.zoom_one_to_one()
    ir_widget.resize(320, 240)
    rect = ir_widget.viewport_rect_normalized()
    assert rect is not None
    x0, y0, x1, y1 = rect
    assert 0.0 <= x0 < x1 <= 1.0
    assert 0.0 <= y0 < y1 <= 1.0
    # Zooming further in shrinks the visible fraction.
    area_before = (x1 - x0) * (y1 - y0)
    ir_widget.zoom_in()
    x0, y0, x1, y1 = ir_widget.viewport_rect_normalized()
    area_after = (x1 - x0) * (y1 - y0)
    assert area_after < area_before


def test_finder_drag_pans_main_view(ir_widget) -> None:
    ir_widget.zoom_one_to_one()
    ir_widget.resize(320, 240)
    ir_widget.pan_to_normalized(0.5, 0.5)
    rect = ir_widget.viewport_rect_normalized()
    assert rect is not None
    x0, y0, x1, y1 = rect
    # The requested center sits in the middle of the visible region.
    assert abs((x0 + x1) / 2 - 0.5) < 0.05
    assert abs((y0 + y1) / 2 - 0.5) < 0.05
    # Edge requests stay constrained inside the image (never outside).
    ir_widget.pan_to_normalized(0.95, 0.95)
    x0, y0, x1, y1 = ir_widget.viewport_rect_normalized()
    assert 0.0 <= x0 < x1 <= 1.0
    assert 0.0 <= y0 < y1 <= 1.0
    assert x1 == pytest.approx(1.0)
    assert y1 == pytest.approx(1.0)
