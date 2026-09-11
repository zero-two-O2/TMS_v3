"""
ui.widgets.statistics_panel -- Statistics display panel for Configuration mode.

Shows overall frame statistics and per-ROI statistics.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QGroupBox,
    QFormLayout,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QLabel,
)

from thermal_monitor.core.models import AnalysisResult, TemperatureUnit
from thermal_monitor.ui.theme import ThemeManager
from thermal_monitor.ui.theme.properties import set_role


class StatisticsPanel(QWidget):
    """Statistics display panel."""

    def __init__(self, theme_manager: Optional[ThemeManager] = None) -> None:
        super().__init__()
        self._theme = theme_manager
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Overall stats
        self._overall_group = QGroupBox("Overall Statistics")
        overall_form = QFormLayout(self._overall_group)
        overall_form.setSpacing(4)

        self._overall_min = QLabel("—")
        self._overall_max = QLabel("—")
        self._overall_mean = QLabel("—")
        self._overall_stddev = QLabel("—")

        for label in [
            self._overall_min, self._overall_max,
            self._overall_mean, self._overall_stddev
        ]:
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            set_role(label, "mono")

        overall_form.addRow("Min:", self._overall_min)
        overall_form.addRow("Max:", self._overall_max)
        overall_form.addRow("Mean:", self._overall_mean)
        overall_form.addRow("Std Dev:", self._overall_stddev)

        layout.addWidget(self._overall_group)

        # Per-ROI stats table
        roi_group = QGroupBox("Per-ROI Statistics")
        roi_layout = QVBoxLayout(roi_group)

        self._roi_stats_table = QTableWidget(0, 7)
        self._roi_stats_table.setHorizontalHeaderLabels(["ROI", "Name", "Min", "Max", "Mean", "Std Dev", "Range"])
        self._roi_stats_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._roi_stats_table.setAlternatingRowColors(True)
        self._roi_stats_table.verticalHeader().setVisible(False)
        self._apply_table_style(self._roi_stats_table)

        roi_layout.addWidget(self._roi_stats_table)
        layout.addWidget(roi_group, 1)

    def _apply_table_style(self, table: QTableWidget) -> None:
        # Tables are styled centrally; nothing per-widget to do.
        return

    # Public API

    def update_from_analysis(self, analysis: AnalysisResult) -> None:
        """Update statistics from analysis result."""
        unit_symbol = "°C"
        if analysis.unit:
            unit_map = {
                TemperatureUnit.CELSIUS: "°C",
                TemperatureUnit.FAHRENHEIT: "°F",
                TemperatureUnit.KELVIN: "K",
            }
            unit_symbol = unit_map.get(analysis.unit, "°C")

        if analysis.overall_min is not None:
            self._overall_min.setText(f"{analysis.overall_min:.2f} {unit_symbol}")
        if analysis.overall_max is not None:
            self._overall_max.setText(f"{analysis.overall_max:.2f} {unit_symbol}")
        if analysis.overall_mean is not None:
            self._overall_mean.setText(f"{analysis.overall_mean:.2f} {unit_symbol}")

        # Update ROI stats table
        self._roi_stats_table.setRowCount(0)
        for roi_id, stat in analysis.roi_results.items():
            row = self._roi_stats_table.rowCount()
            self._roi_stats_table.insertRow(row)
            self._roi_stats_table.setItem(row, 0, QTableWidgetItem(roi_id))
            self._roi_stats_table.setItem(row, 1, QTableWidgetItem(stat.roi_name))
            self._roi_stats_table.setItem(row, 2, QTableWidgetItem(f"{stat.min_temp:.2f} {unit_symbol}"))
            self._roi_stats_table.setItem(row, 3, QTableWidgetItem(f"{stat.max_temp:.2f} {unit_symbol}"))
            self._roi_stats_table.setItem(row, 4, QTableWidgetItem(f"{stat.mean_temp:.2f} {unit_symbol}"))
            self._roi_stats_table.setItem(row, 5, QTableWidgetItem(f"{stat.deviation:.2f} {unit_symbol}"))
            self._roi_stats_table.setItem(row, 6, QTableWidgetItem(f"{stat.range_temp:.2f} {unit_symbol}"))

    def clear(self) -> None:
        self._overall_min.setText("—")
        self._overall_max.setText("—")
        self._overall_mean.setText("—")
        self._overall_stddev.setText("—")
        self._roi_stats_table.setRowCount(0)


__all__ = ["StatisticsPanel"]