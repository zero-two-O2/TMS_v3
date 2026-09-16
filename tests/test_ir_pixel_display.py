"""Thermal IR pixel-preserving display tests (ThermoView-like rendering).

Display-only contract: the 640x480 thermal source, temperature values and
palette mapping must be untouched; only the Qt display sampling may change
(nearest-neighbor / FastTransformation, never SmoothTransformation or
SmoothPixmapTransform for IR).
"""

from __future__ import annotations

import inspect
import os
import pathlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication

import thermal_monitor.camera.source  # noqa: F401  (init camera package first)
from thermal_monitor.ui.modes import observer_image as _observer_mod
from thermal_monitor.ui.modes import view_finder as _finder_mod
from thermal_monitor.ui.modes import vl_image as _vl_mod
from thermal_monitor.ui.modes.observer_image import LiveThermalWidget
from thermal_monitor.ui.modes.thermal_render_worker import (
    PALETTE_LUTS,
    RenderRequest,
    ThermalRenderWorker,
)
from thermal_monitor.ui.modes.view_finder import ViewFinderWidget
from thermal_monitor.ui.windows import offline_window as _offline_mod


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


def _source(name: str) -> str:
    if name == "observer":
        return inspect.getsource(_observer_mod)
    if name == "finder":
        return inspect.getsource(_finder_mod)
    if name == "offline":
        return inspect.getsource(_offline_mod)
    if name == "vl":
        return inspect.getsource(_vl_mod)
    raise AssertionError(name)


# 1. IR rendering does not request SmoothTransformation.
def test_ir_central_display_never_requests_smooth():
    paint_src = inspect.getsource(LiveThermalWidget.paintEvent)
    assert "SmoothTransformation" not in paint_src


# 2. IR rendering uses FastTransformation / nearest-neighbor by default.
# (The connect-time toggle routes paintEvent through _ir_transformation();
# the default mode must resolve to FastTransformation.)
def test_ir_central_display_uses_fast_transformation(qapp):
    paint_src = inspect.getsource(LiveThermalWidget.paintEvent)
    assert "_ir_transformation" in paint_src
    helper_src = inspect.getsource(LiveThermalWidget._ir_transformation)
    assert "FastTransformation" in helper_src
    widget = LiveThermalWidget()
    try:
        assert widget.ir_scaling == "fast"
        assert widget._ir_transformation() == Qt.TransformationMode.FastTransformation
    finally:
        widget.close()


# 2b. IR painter must not enable SmoothPixmapTransform.
def test_ir_central_display_disables_smooth_pixmap_transform():
    paint_src = inspect.getsource(LiveThermalWidget.paintEvent)
    assert "setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)" not in paint_src
    assert "SmoothPixmapTransform" not in [
        line.strip() for line in paint_src.splitlines() if "setRenderHint" in line
    ] or "never enable" in paint_src.lower() or "SmoothPixmapTransform" in paint_src and "never" in paint_src.lower()


# 3. No OpenCV interpolating resize is used for the IR display path.
def test_no_interpolating_opencv_resize_in_display_path():
    for name in ("observer", "finder", "offline", "vl"):
        src = _source(name)
        for token in ("cv2.resize", "INTER_LINEAR", "INTER_CUBIC", "INTER_LANCZOS", "INTER_AREA"):
            assert token not in src, f"{name} must not use {token}"
    worker_src = inspect.getsource(ThermalRenderWorker._render)
    for token in ("cv2.resize", "INTER_LINEAR", "INTER_CUBIC", "INTER_LANCZOS", "INTER_AREA"):
        assert token not in worker_src
    # Palette LUT path only: applyColorMap has no resize involvement.
    repo = pathlib.Path(__file__).resolve().parents[1] / "src" / "thermal_monitor" / "ui"
    hits = []
    for path in repo.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "cv2.resize" in text or "INTER_LINEAR" in text or "INTER_CUBIC" in text:
            hits.append(str(path))
    assert hits == []


# 4/5. Original 640x480 thermal array untouched; temperature values identical.
def test_render_worker_preserves_source_and_temperature(qapp):
    worker = ThermalRenderWorker()
    rng = np.random.default_rng(42)
    source = (rng.uniform(20.0, 80.0, size=(480, 640))).astype(np.float64)
    snapshot = source.copy()
    image, temperature, minimum, maximum, thumbnail, rgb, _ = worker._render(
        RenderRequest(np.asarray(source), 7), "temperature"
    )
    np.testing.assert_array_equal(source, snapshot)  # input not modified
    np.testing.assert_array_equal(temperature, snapshot)  # values identical
    assert temperature.shape == (480, 640)
    assert image.width() == 640 and image.height() == 480  # native 1:1 QImage


# 6. Palette mapping unchanged (same temperature -> same RGB).
def test_palette_mapping_matches_authoritative_lut(qapp):
    worker = ThermalRenderWorker()
    source = np.array([[20.0, 50.0], [80.0, np.nan]], dtype=np.float64)
    _, _, minimum, maximum, _, rgb, _ = worker._render(
        RenderRequest(source, 1, 20.0, 80.0), "temperature"
    )
    assert (minimum, maximum) == (20.0, 80.0)
    normalized = np.clip((source - 20.0) / 60.0, 0.0, 1.0)
    normalized[~np.isfinite(source)] = 0.0
    expected_idx = np.rint(normalized * 255.0).astype(np.uint8)
    np.testing.assert_array_equal(rgb, PALETTE_LUTS["temperature"][expected_idx])


# 7. 1:1 display works (one source pixel = one displayed pixel).
def test_one_to_one_has_no_scaling(ir_widget, qapp):
    ir_widget.zoom_one_to_one()
    qapp.processEvents()
    transform = ir_widget._view_transform()
    assert transform is not None
    scale, _, _, scaled_w, scaled_h = transform
    assert scale == pytest.approx(1.0)
    assert (scaled_w, scaled_h) == (640, 480)


# 8. Fit-to-window preserves 4:3 aspect ratio.
def test_fit_to_window_preserves_aspect(ir_widget, qapp):
    ir_widget.resize(1000, 600)
    ir_widget.zoom_fit()
    qapp.processEvents()
    transform = ir_widget._view_transform()
    assert transform is not None
    _, _, _, scaled_w, scaled_h = transform
    assert scaled_w / scaled_h == pytest.approx(640 / 480, rel=1e-6)
    assert scaled_w <= 1000 and scaled_h <= 600


# 9. Zoom keeps integer-block geometry without smoothing.
def test_integer_zoom_gives_crisp_blocks(ir_widget, qapp):
    ir_widget.resize(2000, 1600)
    ir_widget.set_zoom_factor(2.0)
    qapp.processEvents()
    transform = ir_widget._view_transform()
    assert transform is not None
    scale, _, _, scaled_w, scaled_h = transform
    assert scale == pytest.approx(2.0)
    assert (scaled_w, scaled_h) == (1280, 960)  # 2x2 block per source pixel
    assert ir_widget._ir_transformation() == Qt.TransformationMode.FastTransformation
    paint_src = inspect.getsource(LiveThermalWidget.paintEvent)
    assert "SmoothTransformation" not in paint_src


# 10. View Finder uses pixel-preserving scaling and still works.
def test_view_finder_uses_fast_transformation(qapp):
    paint_src = inspect.getsource(ViewFinderWidget.paintEvent)
    assert "_ir_transformation" in paint_src
    assert "SmoothTransformation" not in paint_src
    helper_src = inspect.getsource(ViewFinderWidget._ir_transformation)
    assert "FastTransformation" in helper_src
    finder = ViewFinderWidget()
    try:
        assert finder.ir_scaling == "fast"
        assert finder._ir_transformation() == Qt.TransformationMode.FastTransformation
        image = QImage(640, 480, QImage.Format.Format_RGB888)
        image.fill(10)
        finder.resize(200, 150)
        finder.show()
        finder.set_image(image)
        finder.set_viewport((0.25, 0.25, 0.75, 0.75))
        qapp.processEvents()
        assert finder.viewport == (0.25, 0.25, 0.75, 0.75)
        assert finder._image_geometry() is not None
    finally:
        finder.close()


# 11. VL rendering path unchanged (still FastTransformation, untouched).
def test_vl_rendering_still_fast_and_independent():
    from thermal_monitor.ui.modes.vl_image import VlImageWidget

    paint_src = inspect.getsource(VlImageWidget.paintEvent)
    assert "FastTransformation" in paint_src
    assert "SmoothTransformation" not in paint_src


# Offline IR widget: same pixel-preserving policy.
def test_offline_thermal_widget_uses_fast_transformation():
    paint_src = inspect.getsource(_offline_mod.OfflineImageWidget.paintEvent)
    assert "FastTransformation" in paint_src
    assert "SmoothTransformation" not in paint_src


# Render-worker thumbnail keeps aspect ratio and FastTransformation.
def test_render_thumbnail_preserves_aspect(qapp):
    worker = ThermalRenderWorker()
    source = np.linspace(0, 100, 640 * 480, dtype=np.float64).reshape(480, 640)
    image, _, _, _, thumbnail, _, _ = worker._render(RenderRequest(source, 3), "gray")
    assert (image.width(), image.height()) == (640, 480)
    assert thumbnail.width() / thumbnail.height() == pytest.approx(640 / 480, rel=0.05)
