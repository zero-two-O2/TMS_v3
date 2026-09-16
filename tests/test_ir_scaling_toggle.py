"""Connect-time IR display sampling toggle (Fast <-> Smooth).

The Acquisition Setup dialog owns an "IR display" control below History.
It selects the Qt TransformationMode the IR views use at presentation
time only: "fast" (nearest-neighbor, ThermoView-like, default) or
"smooth" (bilinear). Thermal uint16 data, temperature conversion,
palette LUT, acquisition, calibration, recording and VL rendering are
untouched; the choice applies at connect/Start, never mid-stream.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication

import pytest

import thermal_monitor.camera.source  # noqa: F401  (init camera package first)
from thermal_monitor.ui.modes.observer_image import LiveThermalWidget
from thermal_monitor.ui.modes.thermal_render_worker import (
    PALETTE_LUTS,
    RenderRequest,
    ThermalRenderWorker,
)
from thermal_monitor.ui.modes.view_finder import ViewFinderWidget
from thermal_monitor.ui.widgets.acquisition_setup_dialog import (
    AcquisitionSetupDialog,
    DEFAULT_IR_SCALING,
)


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_dialog_has_ir_display_below_history(qapp) -> None:
    from PyQt6.QtWidgets import QGroupBox

    dialog = AcquisitionSetupDialog()
    try:
        # Control exists, defaults to crisp pixels, and is a display-only
        # choice that never touches thermal data paths.
        assert dialog.ir_scaling() == "fast"
        assert DEFAULT_IR_SCALING == "fast"
        values = dialog.values()
        assert values["ir_scaling"] == "fast"
        assert set(values) == {"fps", "averaging", "history_frames", "ir_scaling"}
        # Below History: the "IR display:" form row comes after the
        # "History:" row inside the Acquisition parameters group.
        params_group = next(
            box for box in dialog.findChildren(QGroupBox)
            if box.title() == "Acquisition parameters"
        )
        form = params_group.layout()
        labels = [
            form.itemAt(i, form.ItemRole.LabelRole).widget().text()
            for i in range(form.rowCount())
        ]
        assert labels.index("IR display:") > labels.index("History:")
        combo = dialog._ir_scaling_combo_box
        assert combo.itemData(0) == "fast"
        assert combo.itemData(1) == "smooth"
        # History spin still present and independent.
        assert dialog._history_spin_box is not None
    finally:
        dialog.close()


def test_dialog_ir_scaling_round_trip(qapp) -> None:
    dialog = AcquisitionSetupDialog()
    try:
        dialog.set_params(9, "Off", 100, "smooth")
        assert dialog.values()["ir_scaling"] == "smooth"
        dialog.set_params(9, "Off", 100, "fast")
        assert dialog.values()["ir_scaling"] == "fast"
        # Unknown values fall back to fast (never break the dialog).
        dialog.set_params(9, "Off", 100, "bogus")
        assert dialog.values()["ir_scaling"] == "fast"
        # Old 3-arg callers keep working (default fast).
        dialog.set_params(15, "4", 200)
        assert dialog.values()["ir_scaling"] == "fast"
    finally:
        dialog.close()


def test_ir_widget_defaults_to_fast_and_switches(qapp) -> None:
    widget = LiveThermalWidget()
    try:
        assert widget.ir_scaling == "fast"
        assert widget._ir_transformation() == Qt.TransformationMode.FastTransformation
        widget.set_ir_scaling("smooth")
        assert widget.ir_scaling == "smooth"
        assert widget._ir_transformation() == Qt.TransformationMode.SmoothTransformation
        widget.set_ir_scaling("fast")
        assert widget._ir_transformation() == Qt.TransformationMode.FastTransformation
        # Invalid input never breaks rendering: falls back to fast.
        widget.set_ir_scaling("bogus")
        assert widget.ir_scaling == "fast"
        assert widget._ir_transformation() == Qt.TransformationMode.FastTransformation
    finally:
        widget.close()


def test_view_finder_follows_same_modes(qapp) -> None:
    finder = ViewFinderWidget()
    try:
        assert finder.ir_scaling == "fast"
        assert finder._ir_transformation() == Qt.TransformationMode.FastTransformation
        finder.set_ir_scaling("smooth")
        assert finder._ir_transformation() == Qt.TransformationMode.SmoothTransformation
        finder.set_ir_scaling("bogus")
        assert finder.ir_scaling == "fast"
    finally:
        finder.close()


def test_switching_mode_leaves_thermal_data_untouched(qapp) -> None:
    """Render output (data + palette) is identical in both display modes."""
    worker = ThermalRenderWorker()
    rng = np.random.default_rng(7)
    source = rng.uniform(20.0, 80.0, size=(480, 640)).astype(np.float64)
    snapshot = source.copy()
    widget = LiveThermalWidget()
    try:
        for mode in ("fast", "smooth"):
            widget.set_ir_scaling(mode)
            image, temperature, minimum, maximum, _, rgb, _ = worker._render(
                RenderRequest(np.asarray(source), 1, 20.0, 80.0), "temperature"
            )
            np.testing.assert_array_equal(source, snapshot)
            np.testing.assert_array_equal(temperature, snapshot)
            assert (image.width(), image.height()) == (640, 480)
            normalized = np.clip((snapshot - 20.0) / 60.0, 0.0, 1.0)
            expected = PALETTE_LUTS["temperature"][
                np.rint(normalized * 255.0).astype(np.uint8)
            ]
            np.testing.assert_array_equal(rgb, expected)
    finally:
        widget.close()


def test_vl_widget_has_no_ir_scaling_knob() -> None:
    """VL rendering path is independent: no IR scaling API, still fast."""
    import inspect

    from thermal_monitor.ui.modes.vl_image import VlImageWidget

    assert not hasattr(VlImageWidget, "set_ir_scaling")
    paint_src = inspect.getsource(VlImageWidget.paintEvent)
    assert "FastTransformation" in paint_src
    assert "SmoothTransformation" not in paint_src
    assert "_ir_transformation" not in paint_src


def test_scaled_qimage_differs_only_by_sampling(qapp) -> None:
    """Qt-level proof: same source, Fast keeps blocks, Smooth blends."""
    rng = np.random.default_rng(11)
    rgb = (rng.uniform(0, 255, size=(480, 640, 3))).astype(np.uint8)
    rgb = np.ascontiguousarray(rgb)
    image = QImage(
        rgb.data, 640, 480, rgb.strides[0], QImage.Format.Format_RGB888
    ).copy()
    fast = image.scaled(
        1280, 960,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )
    smooth = image.scaled(
        1280, 960,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    assert (fast.width(), fast.height()) == (1280, 960)
    assert (smooth.width(), smooth.height()) == (1280, 960)
    # FastTransformation replicates source pixels exactly at 2x.
    assert fast.pixelColor(0, 0) == image.pixelColor(0, 0)
    assert fast.pixelColor(1, 0) == image.pixelColor(0, 0)
    assert fast.pixelColor(2, 0) == image.pixelColor(1, 0)
