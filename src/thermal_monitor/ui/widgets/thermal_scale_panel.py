"""
ui.widgets.thermal_scale_panel -- Temperature scale and display controls for Configuration mode.

ThermoView-style right panel with:
- Visual vertical temperature gradient legend
- Min/Max temperature readouts
- Palette selector
- Range controls (Auto/Manual)
- Zoom controls

NOTE (GUI polish): this panel owns temperature-scale functionality ONLY.
The navigation View Finder lives in its own dedicated side-shelf panel
(ui.modes.view_finder.ViewFinderWidget) and must not be duplicated here.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, QRect, QSize
from PyQt6.QtGui import (
    QPainter,
    QColor,
    QLinearGradient,
    QFont,
    QFontMetrics,
    QPen,
    QBrush,
    QImage,
    QPixmap,
    QIcon,
)
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QGroupBox,
    QFormLayout,
    QComboBox,
    QCheckBox,
    QDoubleSpinBox,
    QPushButton,
    QLabel,
    QFrame,
    QHBoxLayout,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
)

import numpy as np

from thermal_monitor.ui import palettes as _palettes
from thermal_monitor.ui.theme import ThemeManager
from thermal_monitor.ui.theme.properties import set_role, set_variant
from thermal_monitor.ui.theme.themes import BUILTIN_THEMES

#: Fallback chrome colors when no theme manager is attached (the app
#: always provides one).  Sourced centrally, never scattered literals.
_FALLBACK = BUILTIN_THEMES["industrial_dark"]


class ThermalScaleLegend(QWidget):
    """Visual vertical temperature scale legend (ThermoView-style)."""

    def __init__(self, theme_manager: Optional[ThemeManager] = None) -> None:
        super().__init__()
        self._theme = theme_manager
        self._min_temp = 20.0
        self._max_temp = 80.0
        self._palette = "temperature"
        self._cursor_temp: float | None = None
        self._unit_symbol = "°C"

        self.setMinimumWidth(48)
        self.setMaximumWidth(64)
        self.setSizePolicy(self.sizePolicy().Policy.Fixed, self.sizePolicy().Policy.Expanding)

    def set_range(self, min_temp: float, max_temp: float) -> None:
        self._min_temp = min_temp
        self._max_temp = max_temp
        self.update()

    def set_palette(self, palette: str) -> None:
        self._palette = palette
        self.update()

    def set_cursor_temperature(self, temp: float | None) -> None:
        self._cursor_temp = temp
        self.update()

    def set_unit(self, unit: str) -> None:
        self._unit_symbol = unit
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Narrow bar with reserved label space on the right so tick
        # values never clip (the widget itself stays 48-64 px wide).
        rect = self.rect().adjusted(6, 10, -30, -10)
        if rect.height() < 50 or rect.width() < 12:
            return

        # Draw gradient bar
        gradient = QLinearGradient(rect.left(), rect.bottom(), rect.left(), rect.top())
        colors = self._get_palette_colors()
        for i, color in enumerate(colors):
            gradient.setColorAt(i / (len(colors) - 1), color)

        painter.fillRect(rect, QBrush(gradient))

        # Draw border (chrome; the gradient itself is thermal data and stays)
        if self._theme:
            painter.setPen(QPen(QColor(self._theme.colors().border), 1))
        else:
            painter.setPen(QPen(QColor(_FALLBACK.border), 1))
        painter.drawRect(rect)

        # Draw tick marks and labels
        painter.setFont(QFont("Segoe UI", 8))
        if self._theme:
            painter.setPen(QColor(self._theme.colors().text_primary))
        else:
            painter.setPen(QColor(_FALLBACK.text))

        num_ticks = 5
        for i in range(num_ticks + 1):
            y = rect.bottom() - (i / num_ticks) * rect.height()
            temp = self._min_temp + (i / num_ticks) * (self._max_temp - self._min_temp)

            # Tick mark
            painter.drawLine(rect.right(), int(y), rect.right() + 5, int(y))

            # Label
            label = f"{temp:.0f}"
            text_rect = painter.fontMetrics().boundingRect(label)
            painter.drawText(rect.right() + 8, int(y) + text_rect.height() // 2, label)

        # Draw cursor temperature indicator
        if self._cursor_temp is not None and not (self._cursor_temp != self._cursor_temp):  # not NaN
            if self._min_temp <= self._cursor_temp <= self._max_temp:
                ratio = (self._cursor_temp - self._min_temp) / (self._max_temp - self._min_temp)
                y = rect.bottom() - ratio * rect.height()

                # Triangle indicator
                if self._theme:
                    indicator_color = QColor(self._theme.colors().accent)
                else:
                    indicator_color = QColor(_FALLBACK.accent)

                painter.setBrush(QBrush(indicator_color))
                painter.setPen(Qt.PenStyle.NoPen)
                triangle = [
                    rect.right() + 5, int(y),
                    rect.right() + 12, int(y) - 4,
                    rect.right() + 12, int(y) + 4,
                ]
                from PyQt6.QtGui import QPolygon
                from PyQt6.QtCore import QPoint
                points = [QPoint(triangle[i], triangle[i+1]) for i in range(0, len(triangle), 2)]
                painter.drawPolygon(QPolygon(points))
                # NOTE: no in-legend cursor text (it clipped the narrow
                # scale); the exact value is shown in the readout label
                # below the controls.

    def _get_palette_colors(self) -> list[QColor]:
        """Get color stops for the current palette.

        Derived from the central registry (same definition as the
        rendering LUT), so legend and image can never disagree.
        """
        return _palettes.get_qcolors(self._palette)


class PaletteComboDelegate(QStyledItemDelegate):
    """Dropdown delegate rendering ``[gradient preview] Name`` per row.

    The gradient is sampled from the SAME 256-entry rendering LUT used
    for thermal images (via :mod:`thermal_monitor.ui.palettes`), so the
    preview always matches actual rendering.

    The selection/hover background is painted first and the gradient is
    drawn on top with its own thin border, so the preview stays visible
    in normal, hovered, and selected states.
    """

    PREVIEW_WIDTH = 64
    PREVIEW_HEIGHT = 10
    LEFT_MARGIN = 8
    GAP = 8
    RIGHT_MARGIN = 8
    TOP_BOTTOM = 4
    _GRADIENT_SAMPLES = 32

    def paint(
        self,
        painter: QPainter | None,
        option: QStyleOptionViewItem,
        index,
    ) -> None:
        if painter is None:
            return
        # Qt may invoke the delegate with an invalid/dangling index during
        # stylesheet repolish or view layout (e.g. QApplication.setStyleSheet
        # triggers a synchronous layout pass on the combo popup view).
        # Dereferencing such an index crashes (access violation), so fall
        # back to the default delegate rendering in that case.
        try:
            valid = index is not None and bool(index.isValid())
        except RuntimeError:
            valid = False
        if not valid:
            super().paint(painter, option, index)
            return
        painter.save()
        try:
            try:
                display = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
                key = index.data(Qt.ItemDataRole.UserRole)
            except RuntimeError:
                super().paint(painter, option, index)
                return
            if not isinstance(key, str) or not key:
                key = _palettes.display_to_key(display)

            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

            # --- Background (selection/hover aware, preview stays on top).
            if selected:
                painter.fillRect(option.rect, option.palette.highlight().color())
                text_color = option.palette.highlightedText().color()
            elif hovered:
                hover = option.palette.highlight().color()
                hover.setAlpha(48)
                painter.fillRect(option.rect, hover)
                text_color = option.palette.text().color()
            else:
                text_color = option.palette.text().color()

            rect = option.rect
            pw = self.PREVIEW_WIDTH
            ph = self.PREVIEW_HEIGHT
            preview_y = rect.center().y() - ph // 2
            preview_rect = QRect(
                rect.left() + self.LEFT_MARGIN, preview_y, pw, ph
            )

            # --- Gradient preview from the authoritative rendering LUT.
            lut = _palettes.get_lut(key)
            n = self._GRADIENT_SAMPLES
            xs = np.linspace(0, 255, n).astype(int)
            gradient = QLinearGradient(
                float(preview_rect.left()), 0.0,
                float(preview_rect.right() + 1), 0.0,
            )
            for pos, xi in enumerate(xs):
                r, g, b = (int(v) for v in lut[int(xi)])
                gradient.setColorAt(pos / (n - 1), QColor(r, g, b))
            painter.fillRect(preview_rect, QBrush(gradient))
            # Thin border keeps the strip visible on any background.
            border = (
                QColor(255, 255, 255, 200)
                if selected
                else QColor(0, 0, 0, 110)
            )
            painter.setPen(QPen(border, 1))
            painter.drawRect(preview_rect.adjusted(0, 0, -1, -1))

            # --- Label (existing app font; never shrunk for previews).
            text_rect = QRect(
                preview_rect.right() + 1 + self.GAP,
                rect.top(),
                rect.right() - (preview_rect.right() + 1 + self.GAP)
                - self.RIGHT_MARGIN + 1,
                rect.height(),
            )
            painter.setPen(QPen(text_color))
            painter.setFont(option.font)
            elided = QFontMetrics(option.font).elidedText(
                display, Qt.TextElideMode.ElideRight, max(0, text_rect.width())
            )
            painter.drawText(
                text_rect,
                int(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                ),
                elided,
            )
        finally:
            painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        fm = QFontMetrics(option.font)
        fallback = QSize(
            self.LEFT_MARGIN
            + self.PREVIEW_WIDTH
            + self.GAP
            + fm.averageCharWidth() * 8
            + self.RIGHT_MARGIN,
            max(
                fm.height() + 2 * self.TOP_BOTTOM,
                self.PREVIEW_HEIGHT + 2 * self.TOP_BOTTOM,
                22,
            ),
        )
        # Same invalid-index hazard as paint(): stylesheet changes trigger
        # synchronous sizeHint() calls with invalid indexes. Never
        # dereference such an index (access violation); return a safe
        # fallback size instead.
        try:
            valid = index is not None and bool(index.isValid())
        except RuntimeError:
            return fallback
        if not valid:
            return fallback
        try:
            display = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        except RuntimeError:
            return fallback
        text_w = fm.horizontalAdvance(display)
        width = (
            self.LEFT_MARGIN
            + self.PREVIEW_WIDTH
            + self.GAP
            + text_w
            + self.RIGHT_MARGIN
        )
        height = max(
            fm.height() + 2 * self.TOP_BOTTOM,
            self.PREVIEW_HEIGHT + 2 * self.TOP_BOTTOM,
            22,
        )
        return QSize(width, height)


class ThermalScalePanel(QWidget):
    """Temperature scale / display controls panel with visual legend."""

    # Signals
    palette_changed = pyqtSignal(str)
    auto_range_toggled = pyqtSignal(bool)
    manual_range_applied = pyqtSignal(float, float)  # min, max
    zoom_changed = pyqtSignal(str)

    def __init__(self, theme_manager: Optional[ThemeManager] = None) -> None:
        super().__init__()
        self._theme = theme_manager
        self._auto_range = True
        self._setup_ui()
        self._apply_theme()

    def _setup_ui(self) -> None:
        from thermal_monitor.ui.theme.tokens import metrics_for

        m = metrics_for(self._theme)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(m.panel_spacing)

        # --- TEMPERATURE SCALE GROUP ---
        scale_group = QGroupBox("TEMPERATURE SCALE")
        scale_layout = QHBoxLayout(scale_group)
        scale_layout.setContentsMargins(
            m.panel_group_margin, m.panel_group_margin_top,
            m.panel_group_margin, m.panel_group_margin,
        )
        scale_layout.setSpacing(m.panel_group_spacing)

        # Visual legend (narrow vertical gradient, left side of group)
        self._legend = ThermalScaleLegend(self._theme)
        scale_layout.addWidget(self._legend)

        # Controls (right side of group)
        controls_widget = QWidget()
        controls_layout = QFormLayout(controls_widget)
        controls_layout.setSpacing(m.panel_form_spacing)
        controls_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # Palette selector: [gradient preview] Name per row, driven by
        # the central registry. Icons cover the closed state; the custom
        # delegate renders the richer gradient inside the dropdown.
        self._palette_combo = QComboBox()
        self._populate_palette_combo()
        self._palette_combo.setItemDelegate(
            PaletteComboDelegate(self._palette_combo)
        )
        self._palette_combo.setIconSize(QSize(48, 12))
        self._palette_combo.currentTextChanged.connect(self._on_palette_changed)
        self._apply_input_style(self._palette_combo)

        # Auto Range
        self._auto_range_check = QCheckBox("Auto Range")
        self._auto_range_check.setChecked(True)
        self._auto_range_check.toggled.connect(self._on_auto_range_toggled)
        self._apply_input_style(self._auto_range_check)

        # Custom range (enabled when auto range is off)
        self._custom_min_spin = QDoubleSpinBox()
        self._custom_min_spin.setRange(-273.15, 2000.0)
        self._custom_min_spin.setDecimals(1)
        self._custom_min_spin.setSuffix(" °C")
        self._custom_min_spin.setValue(20.0)
        self._custom_min_spin.setEnabled(False)
        self._apply_input_style(self._custom_min_spin)

        self._custom_max_spin = QDoubleSpinBox()
        self._custom_max_spin.setRange(-273.15, 2000.0)
        self._custom_max_spin.setDecimals(1)
        self._custom_max_spin.setSuffix(" °C")
        self._custom_max_spin.setValue(80.0)
        self._custom_max_spin.setEnabled(False)
        self._apply_input_style(self._custom_max_spin)

        self._apply_range_btn = QPushButton("Apply Range")
        self._apply_range_btn.clicked.connect(self._on_apply_range)
        self._apply_range_btn.setEnabled(False)
        self._apply_button_style(self._apply_range_btn, "accent")

        # Zoom
        self._zoom_combo = QComboBox()
        self._zoom_combo.addItems(["Fit to Window", "50%", "100%", "200%", "400%"])
        self._zoom_combo.setCurrentIndex(0)
        self._zoom_combo.currentTextChanged.connect(self.zoom_changed.emit)
        self._apply_input_style(self._zoom_combo)

        # Cursor temperature readout
        self._cursor_temp_label = QLabel("Cursor: — °C")
        set_role(self._cursor_temp_label, "readout")

        controls_layout.addRow("Palette:", self._palette_combo)
        controls_layout.addRow("", self._auto_range_check)
        controls_layout.addRow("Min:", self._custom_min_spin)
        controls_layout.addRow("Max:", self._custom_max_spin)
        controls_layout.addRow("", self._apply_range_btn)
        controls_layout.addRow("Zoom:", self._zoom_combo)
        controls_layout.addRow("", self._cursor_temp_label)

        scale_layout.addWidget(controls_widget, 1)
        layout.addWidget(scale_group)

        layout.addStretch()

    def _apply_theme(self) -> None:
        # Groups, labels, and inputs are styled centrally; nothing
        # per-widget to do.
        return

    def _apply_button_style(self, btn: QPushButton, style: str) -> None:
        # Kept for call-site compatibility: maps historic local style
        # names onto the global semantic button variants.
        set_variant(btn, {"primary": "accent", "accent": "ghost"}.get(style, "outline"))

    def _apply_input_style(self, widget) -> None:
        # Inputs are styled centrally; nothing per-widget to do.
        return

    def _apply_border_style(self, widget) -> None:
        # Separators inherit the central QFrame border color.
        # Separators inherit the central QFrame border color.
        return

    def _populate_palette_combo(self) -> None:
        """Fill the combo from the central registry with preview icons."""
        self._palette_combo.clear()
        for key in _palettes.palette_keys():
            display = _palettes.key_to_display(key)
            icon = QIcon(_palettes.build_preview_pixmap(key, 48, 12))
            self._palette_combo.addItem(icon, display)
            idx = self._palette_combo.count() - 1
            self._palette_combo.setItemData(
                idx, key, Qt.ItemDataRole.UserRole
            )

    def _on_palette_changed(self, text: str) -> None:
        palette = _palettes.display_to_key(text)
        self._legend.set_palette(palette)
        self.palette_changed.emit(palette)

    def _on_auto_range_toggled(self, checked: bool) -> None:
        self._auto_range = checked
        self._custom_min_spin.setEnabled(not checked)
        self._custom_max_spin.setEnabled(not checked)
        self._apply_range_btn.setEnabled(not checked)
        self.auto_range_toggled.emit(checked)

    def _on_apply_range(self) -> None:
        self.manual_range_applied.emit(self._custom_min_spin.value(), self._custom_max_spin.value())

    # Public API

    def set_palette(self, palette: str) -> None:
        display = _palettes.key_to_display(palette)
        idx = self._palette_combo.findText(display)
        if idx >= 0:
            self._palette_combo.setCurrentIndex(idx)
        self._legend.set_palette(palette)

    def set_auto_range(self, enabled: bool) -> None:
        self._auto_range_check.setChecked(enabled)

    def set_manual_range(self, min_temp: float, max_temp: float) -> None:
        self._custom_min_spin.setValue(min_temp)
        self._custom_max_spin.setValue(max_temp)
        self._legend.set_range(min_temp, max_temp)

    def update_range(self, min_temp: float, max_temp: float) -> None:
        """Update the legend range (called from image widget when auto-range changes)."""
        self._legend.set_range(min_temp, max_temp)
        if self._auto_range:
            self._custom_min_spin.setValue(min_temp)
            self._custom_max_spin.setValue(max_temp)

    def set_zoom(self, zoom_text: str) -> None:
        idx = self._zoom_combo.findText(zoom_text)
        if idx >= 0:
            self._zoom_combo.setCurrentIndex(idx)

    def update_cursor_temperature(self, temp: float | None, unit_symbol: str = "°C") -> None:
        self._legend.set_cursor_temperature(temp)
        self._legend.set_unit(unit_symbol)
        if temp is not None:
            self._cursor_temp_label.setText(f"Cursor: {temp:.1f} {unit_symbol}")
        else:
            self._cursor_temp_label.setText(f"Cursor: — {unit_symbol}")

    def update_view_finder(self, temperature_image: np.ndarray | None) -> None:
        """Deprecated no-op: the View Finder lives in its own side panel."""
        return

    def update_view_finder_image(self, image: QImage | None) -> None:
        """Deprecated no-op: the View Finder lives in its own side panel."""
        return

    def set_unit(self, unit_symbol: str) -> None:
        self._custom_min_spin.setSuffix(f" {unit_symbol}")
        self._custom_max_spin.setSuffix(f" {unit_symbol}")
        self._legend.set_unit(unit_symbol)
        self.update_cursor_temperature(self._legend._cursor_temp, unit_symbol)


__all__ = ["ThermalScalePanel", "ThermalScaleLegend", "PaletteComboDelegate"]
