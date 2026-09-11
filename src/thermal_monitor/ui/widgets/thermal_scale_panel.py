"""
ui.widgets.thermal_scale_panel -- Temperature scale and display controls for Configuration mode.

ThermoView-style right panel with:
- Visual vertical temperature gradient legend
- Min/Max temperature readouts
- Palette selector
- Range controls (Auto/Manual)
- Zoom controls
- View finder (thumbnail)
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, QRect
from PyQt6.QtGui import QPainter, QColor, QLinearGradient, QFont, QPen, QBrush, QImage, QPixmap
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
)

import numpy as np

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

        self.setMinimumWidth(60)
        self.setMaximumWidth(80)
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

        rect = self.rect().adjusted(10, 10, -10, -10)
        if rect.height() < 50:
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

                # Cursor temp label
                cursor_label = f"{self._cursor_temp:.1f}{self._unit_symbol}"
                painter.setPen(indicator_color)
                painter.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
                painter.drawText(rect.right() + 14, int(y) + 3, cursor_label)

    def _get_palette_colors(self) -> list[QColor]:
        """Get color stops for the current palette."""
        if self._palette == "temperature":
            return [
                QColor(0, 0, 128),    # Dark blue
                QColor(0, 0, 255),    # Blue
                QColor(0, 255, 255),  # Cyan
                QColor(0, 255, 0),    # Green
                QColor(255, 255, 0),  # Yellow
                QColor(255, 128, 0),  # Orange
                QColor(255, 0, 0),    # Red
                QColor(128, 0, 0),    # Dark red
            ]
        elif self._palette == "iron":
            return [
                QColor(0, 0, 0),      # Black
                QColor(64, 0, 0),     # Dark red
                QColor(128, 0, 0),    # Red
                QColor(255, 64, 0),   # Orange
                QColor(255, 128, 0),  # Light orange
                QColor(255, 255, 0),  # Yellow
                QColor(255, 255, 128),# Light yellow
                QColor(255, 255, 255),# White
            ]
        elif self._palette == "rainbow":
            return [
                QColor(128, 0, 128),  # Purple
                QColor(0, 0, 255),    # Blue
                QColor(0, 255, 255),  # Cyan
                QColor(0, 255, 0),    # Green
                QColor(255, 255, 0),  # Yellow
                QColor(255, 128, 0),  # Orange
                QColor(255, 0, 0),    # Red
            ]
        elif self._palette == "gray":
            return [
                QColor(0, 0, 0),
                QColor(64, 64, 64),
                QColor(128, 128, 128),
                QColor(192, 192, 192),
                QColor(255, 255, 255),
            ]
        elif self._palette == "hot":
            return [
                QColor(0, 0, 0),
                QColor(128, 0, 0),
                QColor(255, 0, 0),
                QColor(255, 128, 0),
                QColor(255, 255, 0),
                QColor(255, 255, 128),
                QColor(255, 255, 255),
            ]
        return [QColor(0, 0, 255), QColor(255, 0, 0)]


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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # --- TEMPERATURE SCALE GROUP ---
        scale_group = QGroupBox("TEMPERATURE SCALE")
        scale_layout = QHBoxLayout(scale_group)
        scale_layout.setContentsMargins(8, 12, 8, 8)
        scale_layout.setSpacing(8)

        # Visual legend (left side of group)
        self._legend = ThermalScaleLegend(self._theme)
        scale_layout.addWidget(self._legend)

        # Controls (right side of group)
        controls_widget = QWidget()
        controls_layout = QFormLayout(controls_widget)
        controls_layout.setSpacing(8)
        controls_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # Palette selector
        self._palette_combo = QComboBox()
        self._palette_combo.addItems(["Temperature", "Iron", "Rainbow", "Gray", "Hot"])
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

        # Separator
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        self._apply_border_style(separator)
        layout.addWidget(separator)

        # --- VIEW FINDER GROUP ---
        finder_group = QGroupBox("VIEW FINDER")
        finder_layout = QVBoxLayout(finder_group)
        finder_layout.setContentsMargins(8, 12, 8, 8)

        self._view_finder = QLabel("View Finder\n(thumbnail)")
        self._view_finder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._view_finder.setMinimumHeight(120)
        set_role(self._view_finder, "viewfinder")
        finder_layout.addWidget(self._view_finder)

        layout.addWidget(finder_group)

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

    def _on_palette_changed(self, text: str) -> None:
        palette_map = {
            "Temperature": "temperature",
            "Iron": "iron",
            "Rainbow": "rainbow",
            "Gray": "gray",
            "Hot": "hot",
        }
        palette = palette_map.get(text, "temperature")
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
        palette_display = {
            "temperature": "Temperature",
            "iron": "Iron",
            "rainbow": "Rainbow",
            "gray": "Gray",
            "hot": "Hot",
        }
        display = palette_display.get(palette, "Temperature")
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
        """Update the View Finder with a thumbnail of the thermal image."""
        if temperature_image is None:
            self._view_finder.setText("View Finder\n(thumbnail)")
            return
        
        # Create a small thumbnail
        from thermal_monitor.ui.modes.observer_image import LiveThermalWidget
        # Use the same palette logic to create a display image
        temp_img = temperature_image
        finite = np.isfinite(temp_img)
        if not np.any(finite):
            display = np.zeros(temp_img.shape, dtype=np.uint8)
        else:
            lo = float(temp_img[finite].min())
            hi = float(temp_img[finite].max())
            if hi <= lo:
                hi = lo + 1.0
            normalized = np.clip((temp_img - lo) / (hi - lo), 0.0, 1.0)
            normalized[~finite] = 0.0
            display = (normalized * 255.0).astype(np.uint8)
        
        # Apply palette (simplified - grayscale for thumbnail)
        h, w = display.shape
        qimg = QImage(display.data, w, h, display.strides[0], QImage.Format.Format_Grayscale8)
        
        # Scale to fit the view finder
        pixmap = QPixmap.fromImage(qimg)
        scaled = pixmap.scaled(
            self._view_finder.width() - 4,
            self._view_finder.height() - 4,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._view_finder.setPixmap(scaled)

    def update_view_finder_image(self, image: QImage | None) -> None:
        """Display a thumbnail already rendered by the thermal renderer."""
        if image is None:
            self._view_finder.setText("View Finder\n(thumbnail)")
            return
        self._view_finder.setPixmap(QPixmap.fromImage(image))

    def set_unit(self, unit_symbol: str) -> None:
        self._custom_min_spin.setSuffix(f" {unit_symbol}")
        self._custom_max_spin.setSuffix(f" {unit_symbol}")
        self._legend.set_unit(unit_symbol)
        self.update_cursor_temperature(self._legend._cursor_temp, unit_symbol)


__all__ = ["ThermalScalePanel", "ThermalScaleLegend"]
