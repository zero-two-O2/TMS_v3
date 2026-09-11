"""
ui.widgets.alarm_panel -- Alarm configuration panel for Configuration mode.

Provides alarm rule list and editor with live status indication.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QFormLayout,
    QTreeWidget,
    QTreeWidgetItem,
    QPushButton,
    QLineEdit,
    QComboBox,
    QDoubleSpinBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QMessageBox,
    QFrame,
    QLabel,
)

from thermal_monitor.core.models import (
    AlarmRule,
    AlarmCondition,
    AlarmSeverity,
    AnalysisConfig,
)
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.ui.theme import ThemeManager
from thermal_monitor.ui.theme.properties import set_variant
from thermal_monitor.ui.theme.themes import BUILTIN_THEMES

#: Fallback chrome colors when no theme manager is attached.
_FALLBACK = BUILTIN_THEMES["industrial_dark"]


class AlarmPanel(QWidget):
    """Alarm configuration workspace: list and editor."""

    # Signals
    alarm_selected = pyqtSignal(str)  # rule_id
    alarm_created = pyqtSignal(str, AlarmRule)
    alarm_updated = pyqtSignal(str, AlarmRule)
    alarm_deleted = pyqtSignal(str)

    def __init__(
        self,
        config_service: ConfigurationService,
        theme_manager: Optional[ThemeManager] = None,
    ) -> None:
        super().__init__()
        self._config_service = config_service
        self._theme = theme_manager
        self._selected_camera_id: str | None = None
        self._selected_rule_id: str | None = None
        self._dirty_camera_configs: set[str] = set()

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Alarm List group
        list_group = QGroupBox("Alarm Rules")
        list_layout = QVBoxLayout(list_group)
        list_layout.setSpacing(6)

        # Toolbar
        toolbar = QHBoxLayout()
        self._add_alarm_btn = QPushButton("Add Alarm Rule")
        self._add_alarm_btn.clicked.connect(self._on_add_alarm)
        self._apply_button_style(self._add_alarm_btn, "primary")

        self._edit_alarm_btn = QPushButton("Edit")
        self._edit_alarm_btn.clicked.connect(self._on_edit_alarm)
        self._edit_alarm_btn.setEnabled(False)
        self._apply_button_style(self._edit_alarm_btn, "secondary")

        self._delete_alarm_btn = QPushButton("Delete")
        self._delete_alarm_btn.clicked.connect(self._on_delete_alarm)
        self._delete_alarm_btn.setEnabled(False)
        self._apply_button_style(self._delete_alarm_btn, "danger")

        toolbar.addWidget(self._add_alarm_btn)
        toolbar.addWidget(self._edit_alarm_btn)
        toolbar.addWidget(self._delete_alarm_btn)
        toolbar.addStretch()
        list_layout.addLayout(toolbar)

        # Alarm Tree
        self._alarm_tree = QTreeWidget()
        self._alarm_tree.setHeaderLabels(["Rule ID", "ROI", "Condition", "Threshold", "Severity", "Enabled"])
        self._alarm_tree.setColumnWidth(0, 120)
        self._alarm_tree.setColumnWidth(1, 100)
        self._alarm_tree.setColumnWidth(2, 100)
        self._alarm_tree.setColumnWidth(3, 120)
        self._alarm_tree.setColumnWidth(4, 80)
        self._alarm_tree.setColumnWidth(5, 60)
        self._alarm_tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._apply_tree_style(self._alarm_tree)
        list_layout.addWidget(self._alarm_tree, 1)

        layout.addWidget(list_group, 1)

        # Alarm Editor (initially hidden)
        self._alarm_editor = self._create_alarm_editor()
        self._alarm_editor.setVisible(False)
        layout.addWidget(self._alarm_editor)

    def _create_alarm_editor(self) -> QWidget:
        editor = QWidget()
        layout = QVBoxLayout(editor)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        group = QGroupBox("Alarm Rule Editor")
        form = QFormLayout(group)
        form.setSpacing(6)

        self._alarm_editor_rule_id = QLineEdit()
        self._alarm_editor_rule_id.setPlaceholderText("e.g., alarm_high_temp")
        form.addRow("Rule ID:", self._alarm_editor_rule_id)

        self._alarm_editor_roi_combo = QComboBox()
        form.addRow("ROI:", self._alarm_editor_roi_combo)

        self._alarm_editor_condition = QComboBox()
        self._alarm_editor_condition.addItems([c.value for c in AlarmCondition])
        form.addRow("Condition:", self._alarm_editor_condition)

        self._alarm_editor_threshold = QDoubleSpinBox()
        self._alarm_editor_threshold.setRange(-273.15, 2000.0)
        self._alarm_editor_threshold.setDecimals(1)
        self._alarm_editor_threshold.setSuffix(" °C")
        form.addRow("Threshold:", self._alarm_editor_threshold)

        self._alarm_editor_threshold_low = QDoubleSpinBox()
        self._alarm_editor_threshold_low.setRange(-273.15, 2000.0)
        self._alarm_editor_threshold_low.setDecimals(1)
        self._alarm_editor_threshold_low.setSuffix(" °C")
        self._alarm_editor_threshold_low.setVisible(False)
        form.addRow("Threshold Low:", self._alarm_editor_threshold_low)

        self._alarm_editor_threshold_high = QDoubleSpinBox()
        self._alarm_editor_threshold_high.setRange(-273.15, 2000.0)
        self._alarm_editor_threshold_high.setDecimals(1)
        self._alarm_editor_threshold_high.setSuffix(" °C")
        self._alarm_editor_threshold_high.setVisible(False)
        form.addRow("Threshold High:", self._alarm_editor_threshold_high)

        self._alarm_editor_severity = QComboBox()
        self._alarm_editor_severity.addItems([s.value for s in AlarmSeverity])
        form.addRow("Severity:", self._alarm_editor_severity)

        self._alarm_editor_enabled = QCheckBox("Enabled")
        self._alarm_editor_enabled.setChecked(True)
        form.addRow("", self._alarm_editor_enabled)

        self._alarm_editor_save_btn = QPushButton("Save Alarm")
        self._alarm_editor_save_btn.clicked.connect(self._on_save_alarm)
        self._alarm_editor_save_btn.setEnabled(False)
        self._apply_button_style(self._alarm_editor_save_btn, "primary")
        form.addRow("", self._alarm_editor_save_btn)

        self._alarm_editor_condition.currentTextChanged.connect(self._on_condition_changed)

        layout.addWidget(group)
        layout.addStretch()

        return editor

    def _on_selection_changed(self) -> None:
        items = self._alarm_tree.selectedItems()
        if items:
            item = items[0]
            rule_id = item.data(0, Qt.ItemDataRole.UserRole)
            self._selected_rule_id = rule_id
            self._load_alarm_editor(rule_id)
            self._edit_alarm_btn.setEnabled(True)
            self._delete_alarm_btn.setEnabled(True)
            self.alarm_selected.emit(rule_id)
        else:
            self._alarm_editor.setVisible(False)
            self._edit_alarm_btn.setEnabled(False)
            self._delete_alarm_btn.setEnabled(False)
            self._selected_rule_id = None

    def _load_alarm_editor(self, rule_id: str) -> None:
        if not self._selected_camera_id:
            return
        analysis = self._config_service.get_analysis_config(self._selected_camera_id)
        if not analysis or rule_id not in analysis.alarm_rules:
            return

        rule = analysis.alarm_rules[rule_id]
        self._alarm_editor.setVisible(True)
        self._alarm_editor_save_btn.setEnabled(True)

        self._alarm_editor_rule_id.setText(rule.rule_id)
        self._alarm_editor_roi_combo.setCurrentText(rule.roi_id)
        self._alarm_editor_condition.setCurrentText(rule.condition.value)
        self._alarm_editor_threshold.setValue(rule.threshold if rule.threshold is not None else 0.0)
        self._alarm_editor_threshold_low.setValue(rule.threshold_low if rule.threshold_low is not None else 0.0)
        self._alarm_editor_threshold_high.setValue(rule.threshold_high if rule.threshold_high is not None else 0.0)
        self._alarm_editor_severity.setCurrentText(rule.severity.value)
        self._alarm_editor_enabled.setChecked(rule.enabled)

        # Show/hide range thresholds
        self._on_condition_changed(rule.condition.value)

        # Mark dirty on changes
        self._alarm_editor_rule_id.textChanged.connect(lambda: self._mark_dirty())
        self._alarm_editor_roi_combo.currentTextChanged.connect(lambda: self._mark_dirty())
        self._alarm_editor_condition.currentTextChanged.connect(lambda: self._mark_dirty())
        self._alarm_editor_threshold.valueChanged.connect(lambda: self._mark_dirty())
        self._alarm_editor_threshold_low.valueChanged.connect(lambda: self._mark_dirty())
        self._alarm_editor_threshold_high.valueChanged.connect(lambda: self._mark_dirty())
        self._alarm_editor_severity.currentTextChanged.connect(lambda: self._mark_dirty())
        self._alarm_editor_enabled.toggled.connect(lambda: self._mark_dirty())

    def _on_condition_changed(self, condition: str) -> None:
        is_range = condition in (AlarmCondition.OUTSIDE_RANGE.value, AlarmCondition.INSIDE_RANGE.value)
        self._alarm_editor_threshold.setVisible(not is_range)
        self._alarm_editor_threshold_low.setVisible(is_range)
        self._alarm_editor_threshold_high.setVisible(is_range)

    def _on_add_alarm(self) -> None:
        if not self._selected_camera_id:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Add Alarm Rule")
        layout = QFormLayout(dialog)

        rule_id_edit = QLineEdit()
        rule_id_edit.setPlaceholderText("e.g., alarm_high")
        roi_combo = QComboBox()
        analysis = self._config_service.get_analysis_config(self._selected_camera_id)
        if analysis:
            for roi_id in analysis.rois:
                roi_combo.addItem(roi_id, roi_id)
        condition_combo = QComboBox()
        condition_combo.addItems([c.value for c in AlarmCondition])
        threshold_spin = QDoubleSpinBox()
        threshold_spin.setRange(-273.15, 2000.0)
        threshold_spin.setDecimals(1)
        threshold_spin.setSuffix(" °C")
        severity_combo = QComboBox()
        severity_combo.addItems([s.value for s in AlarmSeverity])

        layout.addRow("Rule ID:", rule_id_edit)
        layout.addRow("ROI:", roi_combo)
        layout.addRow("Condition:", condition_combo)
        layout.addRow("Threshold:", threshold_spin)
        layout.addRow("Severity:", severity_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            rule_id = rule_id_edit.text().strip()
            roi_id = roi_combo.currentData()
            condition = AlarmCondition(condition_combo.currentText())
            threshold = threshold_spin.value()
            severity = AlarmSeverity(severity_combo.currentText())

            if not rule_id or not roi_id:
                QMessageBox.warning(self, "Invalid Input", "Rule ID and ROI are required.")
                return

            analysis = self._config_service.get_analysis_config(self._selected_camera_id)
            if not analysis:
                analysis = self._config_service.create_analysis_config(self._selected_camera_id)

            if rule_id in analysis.alarm_rules:
                QMessageBox.warning(self, "Duplicate", f"Alarm rule '{rule_id}' already exists.")
                return

            new_rule = AlarmRule(
                rule_id=rule_id,
                roi_id=roi_id,
                condition=condition,
                threshold=threshold,
                severity=severity,
                enabled=True,
            )

            new_rules = dict(analysis.alarm_rules)
            new_rules[rule_id] = new_rule

            updated_config = AnalysisConfig(
                camera_id=analysis.camera_id,
                rois=analysis.rois,
                position_associations=analysis.position_associations,
                alarm_rules=new_rules,
                default_emissivity=analysis.default_emissivity,
                ambient_temperature=analysis.ambient_temperature,
                distance=analysis.distance,
                humidity=analysis.humidity,
                reflected_temperature=analysis.reflected_temperature,
                unit=analysis.unit,
            )
            self._config_service.set_analysis_config(updated_config)
            self.refresh_alarm_list()
            self._mark_dirty()
            self.alarm_created.emit(rule_id, new_rule)

    def _on_edit_alarm(self) -> None:
        if self._selected_rule_id:
            self._load_alarm_editor(self._selected_rule_id)

    def _on_delete_alarm(self) -> None:
        if not self._selected_rule_id or not self._selected_camera_id:
            return

        rule_id = self._selected_rule_id

        reply = QMessageBox.question(
            self,
            "Delete Alarm",
            f"Delete alarm rule '{rule_id}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        analysis = self._config_service.get_analysis_config(self._selected_camera_id)
        if not analysis:
            return

        new_rules = {k: v for k, v in analysis.alarm_rules.items() if k != rule_id}

        updated_config = AnalysisConfig(
            camera_id=analysis.camera_id,
            rois=analysis.rois,
            position_associations=analysis.position_associations,
            alarm_rules=new_rules,
            default_emissivity=analysis.default_emissivity,
            ambient_temperature=analysis.ambient_temperature,
            distance=analysis.distance,
            humidity=analysis.humidity,
            reflected_temperature=analysis.reflected_temperature,
            unit=analysis.unit,
        )
        self._config_service.set_analysis_config(updated_config)
        self.refresh_alarm_list()
        self._mark_dirty()
        self.alarm_deleted.emit(rule_id)

    def _on_save_alarm(self) -> None:
        if not self._selected_camera_id:
            return

        rule_id = self._alarm_editor_rule_id.text().strip()
        roi_id = self._alarm_editor_roi_combo.currentData()
        condition = AlarmCondition(self._alarm_editor_condition.currentText())
        threshold = self._alarm_editor_threshold.value() if not self._alarm_editor_threshold_low.isVisible() else None
        threshold_low = self._alarm_editor_threshold_low.value() if self._alarm_editor_threshold_low.isVisible() else None
        threshold_high = self._alarm_editor_threshold_high.value() if self._alarm_editor_threshold_high.isVisible() else None
        severity = AlarmSeverity(self._alarm_editor_severity.currentText())
        enabled = self._alarm_editor_enabled.isChecked()

        if not rule_id or not roi_id:
            QMessageBox.warning(self, "Invalid Input", "Rule ID and ROI are required.")
            return

        # Validate range condition
        if condition in (AlarmCondition.OUTSIDE_RANGE, AlarmCondition.INSIDE_RANGE):
            if threshold_low is None or threshold_high is None:
                QMessageBox.warning(self, "Invalid Input", "Range conditions require both low and high thresholds.")
                return
            if threshold_low >= threshold_high:
                QMessageBox.warning(self, "Invalid Input", "Low threshold must be less than high threshold.")
                return

        analysis = self._config_service.get_analysis_config(self._selected_camera_id)
        if not analysis:
            return

        new_rule = AlarmRule(
            rule_id=rule_id,
            roi_id=roi_id,
            condition=condition,
            threshold=threshold,
            threshold_low=threshold_low,
            threshold_high=threshold_high,
            severity=severity,
            enabled=enabled,
        )

        new_rules = dict(analysis.alarm_rules)
        new_rules[rule_id] = new_rule

        updated_config = AnalysisConfig(
            camera_id=analysis.camera_id,
            rois=analysis.rois,
            position_associations=analysis.position_associations,
            alarm_rules=new_rules,
            default_emissivity=analysis.default_emissivity,
            ambient_temperature=analysis.ambient_temperature,
            distance=analysis.distance,
            humidity=analysis.humidity,
            reflected_temperature=analysis.reflected_temperature,
            unit=analysis.unit,
        )
        self._config_service.set_analysis_config(updated_config)
        self.refresh_alarm_list()
        self._mark_dirty()
        self.alarm_updated.emit(rule_id, new_rule)

    def _mark_dirty(self) -> None:
        if self._selected_camera_id:
            self._dirty_camera_configs.add(self._selected_camera_id)

    # Public API

    def set_camera(self, camera_id: str) -> None:
        """Switch to a different camera."""
        self._selected_camera_id = camera_id
        self.refresh_alarm_list()
        self._update_roi_combo()
        self._alarm_editor.setVisible(False)
        self._edit_alarm_btn.setEnabled(False)
        self._delete_alarm_btn.setEnabled(False)
        self._selected_rule_id = None

    def refresh_alarm_list(self) -> None:
        """Refresh alarm list from analysis config."""
        self._alarm_tree.clear()
        if not self._selected_camera_id:
            return

        analysis = self._config_service.get_analysis_config(self._selected_camera_id)
        if not analysis:
            return

        for rule_id, rule in analysis.alarm_rules.items():
            threshold_text = f"{rule.threshold:.1f} °C" if rule.threshold is not None else "—"
            if rule.condition in (AlarmCondition.OUTSIDE_RANGE, AlarmCondition.INSIDE_RANGE):
                threshold_text = f"[{rule.threshold_low:.1f}, {rule.threshold_high:.1f}] °C" if rule.threshold_low is not None and rule.threshold_high is not None else "—"

            item = QTreeWidgetItem([
                rule_id,
                rule.roi_id,
                rule.condition.value,
                threshold_text,
                rule.severity.value.upper(),
                "Yes" if rule.enabled else "No",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, rule_id)
            self._alarm_tree.addTopLevelItem(item)

    def _update_roi_combo(self) -> None:
        """Update ROI combo in alarm editor."""
        self._alarm_editor_roi_combo.clear()
        if not self._selected_camera_id:
            return
        analysis = self._config_service.get_analysis_config(self._selected_camera_id)
        if analysis:
            for roi_id in analysis.rois:
                self._alarm_editor_roi_combo.addItem(roi_id, roi_id)

    def update_live_alarms(self, alarm_result, analysis) -> None:
        """Update alarm tree with live alarm state."""
        if self._theme:
            alarm_bg = QColor(self._theme.danger_bg())
            clear_bg = QColor(self._theme.surface())
        else:
            alarm_bg = QColor(_FALLBACK.danger_bg)
            clear_bg = QColor(_FALLBACK.surface)
        if not alarm_result or not alarm_result.active_alarms:
            # Clear any active highlights
            for i in range(self._alarm_tree.topLevelItemCount()):
                item = self._alarm_tree.topLevelItem(i)
                item.setBackground(0, clear_bg)
                item.setBackground(1, clear_bg)
            return

        for i in range(self._alarm_tree.topLevelItemCount()):
            item = self._alarm_tree.topLevelItem(i)
            rule_id = item.data(0, Qt.ItemDataRole.UserRole)
            if rule_id in alarm_result.active_alarms:
                item.setBackground(0, alarm_bg)
                item.setBackground(1, alarm_bg)
            else:
                item.setBackground(0, clear_bg)
                item.setBackground(1, clear_bg)

    def has_unsaved_changes(self) -> bool:
        return bool(self._dirty_camera_configs)

    def clear_dirty(self, camera_id: str) -> None:
        self._dirty_camera_configs.discard(camera_id)

    def _apply_button_style(self, btn: QPushButton, style: str) -> None:
        # Kept for call-site compatibility: historic semantic names map
        # directly onto the global button variants.
        set_variant(btn, style if style in ("primary", "secondary", "accent", "danger", "outline", "ghost") else "outline")

    def _apply_input_style(self, widget) -> None:
        # Inputs are styled centrally; nothing per-widget to do.
        return

    def _apply_tree_style(self, tree: QTreeWidget) -> None:
        # Trees are styled centrally; nothing per-widget to do.
        return


__all__ = ["AlarmPanel"]