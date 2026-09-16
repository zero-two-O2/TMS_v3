"""Temperature palette expansion + dropdown preview tests.

Covers the task acceptance criteria:
1. required names available, 2. no duplicates, 3. Temperature works,
4. selecting every palette works, 5. preview per palette,
6. preview == rendering definition, 7. delegate renders preview,
8. long names do not clip, 9/10. hover/selected keep preview,
11. popup open/close preserves selection,
12/13. existing scale + font-scaling behavior intact.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QStyle, QStyleOptionViewItem

from thermal_monitor.ui import palettes
from thermal_monitor.ui.palettes import (
    PALETTE_DISPLAY,
    PALETTE_LUTS,
    PALETTE_ORDER,
    REQUIRED_PALETTES,
)
from thermal_monitor.ui.widgets.thermal_scale_panel import (
    PaletteComboDelegate,
    ThermalScalePanel,
)

REQUIRED_DISPLAY = [
    "Temperature",
    "Rainbow",
    "Iron",
    "Gray",
    "RContrast",
    "Rain900",
    "Rain",
    "Fire",
    "Yellow",
    "Grayred",
    "Midgray",
    "Y-Glow",
]


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _paint_row(qapp, panel, row: int, state) -> QImage:
    """Paint one delegate row into an image with the given extra state."""
    delegate = panel._palette_combo.itemDelegate()
    assert isinstance(delegate, PaletteComboDelegate)
    model = panel._palette_combo.model()
    index = model.index(row, 0)
    option = QStyleOptionViewItem()
    option.initFrom(panel._palette_combo.view())
    option.rect = panel._palette_combo.view().visualRect(index)
    if option.rect.isNull() or option.rect.width() < 10:
        size = delegate.sizeHint(option, index)
        option.rect.setSize(size)
    option.state |= state
    option.font = panel._palette_combo.font()
    image = QImage(
        max(1, option.rect.width()),
        max(1, option.rect.height()),
        QImage.Format.Format_RGB32,
    )
    image.fill(0xFFFFFF)
    painter = QPainter(image)
    # Translate so option.rect maps to image origin.
    painter.translate(-option.rect.left(), -option.rect.top())
    delegate.paint(painter, option, index)
    painter.end()
    return image


def _preview_center_color(image: QImage) -> tuple[int, int, int]:
    x = PaletteComboDelegate.LEFT_MARGIN + PaletteComboDelegate.PREVIEW_WIDTH // 2
    y = image.height() // 2
    c = image.pixelColor(min(x, image.width() - 1), min(y, image.height() - 1))
    return (c.red(), c.green(), c.blue())


# 1. All required palette names are available.
def test_all_required_palette_names_available():
    displays = palettes.palette_display_names()
    for name in REQUIRED_DISPLAY:
        assert name in displays, f"missing palette {name}"
    for key in REQUIRED_PALETTES:
        assert key in PALETTE_ORDER
        assert key in PALETTE_LUTS


# 2. No duplicate palette names exist.
def test_no_duplicate_palette_names():
    assert len(PALETTE_ORDER) == len(set(PALETTE_ORDER))
    displays = palettes.palette_display_names()
    assert len(displays) == len(set(displays))
    assert len(PALETTE_LUTS) == len(PALETTE_ORDER)


# 3. Existing "Temperature" palette still works.
def test_temperature_palette_still_works(qapp):
    lut = palettes.get_lut("temperature")
    assert lut.shape == (256, 3) and lut.dtype == np.uint8
    panel = ThermalScalePanel(None)
    try:
        panel.set_palette("temperature")
        assert panel._legend._palette == "temperature"
        assert panel._palette_combo.currentText() == "Temperature"
        colors = panel._legend._get_palette_colors()
        assert len(colors) >= 2
        # Rendering path accepts the palette.
        from thermal_monitor.ui.modes.thermal_render_worker import ThermalRenderWorker

        worker = ThermalRenderWorker()
        image, _, _, _, _, rgb, _ = worker._render.__get__(worker, ThermalRenderWorker)(
            __import__(
                "thermal_monitor.ui.modes.thermal_render_worker",
                fromlist=["RenderRequest"],
            ).RenderRequest(np.zeros((4, 4), dtype=np.float32), 1),
            "temperature",
        )
        assert rgb.shape == (4, 4, 3)
    finally:
        panel.close()


# 4. Selecting every palette changes the active palette correctly.
def test_selecting_every_palette_changes_active(qapp):
    panel = ThermalScalePanel(None)
    try:
        emitted: list[str] = []
        panel.palette_changed.connect(emitted.append)
        for key in PALETTE_ORDER:
            emitted.clear()
            panel.set_palette(key)
            qapp.processEvents()
            assert panel._legend._palette == key
            assert panel._palette_combo.currentText() == palettes.key_to_display(key)
            # set_palette routes through the combo signal when the index
            # actually changes; re-selecting the current item is a no-op.
            if emitted:
                assert emitted[-1] == key
        # Signal path: changing via the combo emits the palette key.
        emitted.clear()
        other = PALETTE_ORDER[1] if panel._legend._palette != PALETTE_ORDER[1] else PALETTE_ORDER[2]
        panel._palette_combo.setCurrentText(palettes.key_to_display(other))
        qapp.processEvents()
        assert emitted and emitted[-1] == other
        assert panel._legend._palette == other
    finally:
        panel.close()


# 5. Each palette has a preview representation.
def test_each_palette_has_preview(qapp):
    panel = ThermalScalePanel(None)
    try:
        for row, key in enumerate(PALETTE_ORDER):
            image = palettes.build_preview_image(key, 64, 12)
            assert not image.isNull()
            assert image.width() == 64 and image.height() == 12
            pixmap = palettes.build_preview_pixmap(key, 48, 12)
            assert not pixmap.isNull()
            icon = panel._palette_combo.itemIcon(row)
            assert not icon.isNull()
            assert not icon.pixmap(48, 12).isNull()
    finally:
        panel.close()


# 6. Preview uses the same authoritative definition as rendering.
def test_preview_matches_rendering_lut(qapp):
    for key in PALETTE_ORDER:
        lut = palettes.get_lut(key)
        image = palettes.build_preview_image(key, 256, 4)
        assert image.width() == 256
        for x, lut_idx in ((0, 0), (128, 128), (255, 255)):
            c = image.pixelColor(x, 0)
            expected = tuple(int(v) for v in lut[lut_idx])
            assert (c.red(), c.green(), c.blue()) == expected, key
        # Legend stops come from the same stops as the LUT.
        stops = palettes.PALETTE_STOPS[key]
        qcolors = palettes.get_qcolors(key)
        assert len(qcolors) == len(stops)
        assert (qcolors[0].red(), qcolors[0].green(), qcolors[0].blue()) == stops[0]
        assert (qcolors[-1].red(), qcolors[-1].green(), qcolors[-1].blue()) == stops[-1]


# 7. Dropdown delegate/model renders a preview for every palette.
def test_delegate_renders_preview_for_every_palette(qapp):
    panel = ThermalScalePanel(None)
    try:
        delegate = panel._palette_combo.itemDelegate()
        assert isinstance(delegate, PaletteComboDelegate)
        model = panel._palette_combo.model()
        option = QStyleOptionViewItem()
        option.initFrom(panel._palette_combo.view())
        option.font = panel._palette_combo.font()
        for row, key in enumerate(PALETTE_ORDER):
            index = model.index(row, 0)
            assert index.data(Qt.ItemDataRole.UserRole) == key
            size = delegate.sizeHint(option, index)
            assert size.width() > 0 and size.height() > 0
            image = _paint_row(qapp, panel, row, QStyle.StateFlag.State_None)
            center = _preview_center_color(image)
            expected = tuple(int(v) for v in palettes.get_lut(key)[128])
            # Qt gradient interpolation between the 32 sampled stops is
            # very close to, but not bit-identical with, the NumPy LUT.
            assert all(abs(a - b) <= 48 for a, b in zip(center, expected)), key
    finally:
        panel.close()


# 8. Long palette names do not clip.
def test_long_palette_names_do_not_clip(qapp):
    panel = ThermalScalePanel(None)
    try:
        delegate = panel._palette_combo.itemDelegate()
        option = QStyleOptionViewItem()
        option.initFrom(panel._palette_combo.view())
        option.font = panel._palette_combo.font()
        from PyQt6.QtGui import QFontMetrics

        fm = QFontMetrics(option.font)
        model = panel._palette_combo.model()
        for row in range(model.rowCount()):
            index = model.index(row, 0)
            size = delegate.sizeHint(option, index)
            text = str(index.data(Qt.ItemDataRole.DisplayRole))
            needed = (
                delegate.LEFT_MARGIN
                + delegate.PREVIEW_WIDTH
                + delegate.GAP
                + fm.horizontalAdvance(text)
                + delegate.RIGHT_MARGIN
            )
            assert size.width() >= needed, text
    finally:
        panel.close()


# 9. Hover state does not hide the preview.
def test_hover_does_not_hide_preview(qapp):
    panel = ThermalScalePanel(None)
    try:
        for row, key in enumerate(PALETTE_ORDER):
            image = _paint_row(
                qapp, panel, row, QStyle.StateFlag.State_MouseOver
            )
            center = _preview_center_color(image)
            expected = tuple(int(v) for v in palettes.get_lut(key)[128])
            assert all(abs(a - b) <= 48 for a, b in zip(center, expected)), key
    finally:
        panel.close()


# 10. Selected state does not hide the preview.
def test_selected_does_not_hide_preview(qapp):
    panel = ThermalScalePanel(None)
    try:
        for row, key in enumerate(PALETTE_ORDER):
            image = _paint_row(qapp, panel, row, QStyle.StateFlag.State_Selected)
            center = _preview_center_color(image)
            expected = tuple(int(v) for v in palettes.get_lut(key)[128])
            assert all(abs(a - b) <= 48 for a, b in zip(center, expected)), key
    finally:
        panel.close()


# 11. Opening and closing the dropdown does not alter the selection.
def test_popup_open_close_preserves_selection(qapp):
    panel = ThermalScalePanel(None)
    try:
        panel.show()
        qapp.processEvents()
        for key in ("iron", "rain900", "yglow", "temperature"):
            panel.set_palette(key)
            qapp.processEvents()
            before = panel._palette_combo.currentText()
            panel._palette_combo.showPopup()
            qapp.processEvents()
            panel._palette_combo.hidePopup()
            qapp.processEvents()
            assert panel._palette_combo.currentText() == before
            assert panel._legend._palette == key
    finally:
        panel.close()


# 12. Closed combo shows a preview icon for the selected palette.
def test_closed_combo_shows_preview_icon(qapp):
    panel = ThermalScalePanel(None)
    try:
        for key in PALETTE_ORDER:
            panel.set_palette(key)
            icon = panel._palette_combo.currentData(Qt.ItemDataRole.DecorationRole)
            # QComboBox stores the icon in DecorationRole; fall back to itemIcon.
            pixmap = None
            if isinstance(icon, QPixmap):
                pixmap = icon
            else:
                pixmap = panel._palette_combo.itemIcon(
                    panel._palette_combo.currentIndex()
                ).pixmap(48, 12)
            assert pixmap is not None and not pixmap.isNull(), key
    finally:
        panel.close()


# 13. Font scaling keeps the palette UI functional.
def test_font_scaling_keeps_palette_ui_functional(qapp):
    import thermal_monitor.ui.theme.fonts as fonts

    panel = ThermalScalePanel(None)
    try:
        fonts.apply_font_scale(130, theme_manager=None, app=qapp)
        qapp.processEvents()
        try:
            assert panel._palette_combo.count() == len(PALETTE_ORDER)
            panel.set_palette("fire")
            assert panel._palette_combo.currentText() == "Fire"
            delegate = panel._palette_combo.itemDelegate()
            option = QStyleOptionViewItem()
            option.initFrom(panel._palette_combo.view())
            option.font = panel._palette_combo.font()
            size = delegate.sizeHint(option, panel._palette_combo.model().index(0, 0))
            assert size.height() >= 22
        finally:
            fonts.apply_font_scale(100, theme_manager=None, app=qapp)
            qapp.processEvents()
    finally:
        panel.close()
