"""
ui.widgets.roi_panel -- ROI workspace panel for Configuration mode.

Provides ROI list, editor, and visual overlay integration.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
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
    QTextEdit,
    QDialog,
    QDialogButtonBox,
    QMessageBox,
    QFrame,
    QLabel,
)

from thermal_monitor.core.models import (
    ROIConfig,
    ROIGeometry,
    ROIShape,
    TemperatureLimits,
    TemperatureUnit,
    AnalysisConfig,
    PositionROIAssociation,
)
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.ui.theme import ThemeManager


class ROIPanel(QWidget):
    """ROI workspace: list, editor, and visual overlay coordination."""

    # Signals
    roi_selected = pyqtSignal(str)  # roi_id
    roi_created = pyqtSignal(str, ROIConfig)
    roi_updated = pyqtSignal(str, ROIConfig)
    roi_deleted = pyqtSignal(str)

    def __init__(
        self,
        config_service: ConfigurationService,
        theme_manager: Optional[ThemeManager] = None,
    ) -> None:
        super().__init__()
        self._config_service = config_service
        self._theme = theme_manager
        self._selected_camera_id: str | None = None
        self._selected_roi_id: str | None = None
        self._dirty_camera_configs: set[str] = set()

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # ROI List group
        list_group = QGroupBox("Regions of Interest")
        list_layout = QVBoxLayout(list_group)
        list_layout.setSpacing(6)

        # Toolbar
        toolbar = QHBoxLayout()
        self._add_roi_btn = QPushButton("Add ROI")
        self._add_roi_btn.clicked.connect(self._on_add_roi)
        self._apply_button_style(self._add_roi_btn, "primary")

        self._edit_roi_btn = QPushButton("Edit")
        self._edit_roi_btn.clicked.connect(self._on_edit_roi)
        self._edit_roi_btn.setEnabled(False)
        self._apply_button_style(self._edit_roi_btn, "secondary")

        self._delete_roi_btn = QPushButton("Delete")
        self._delete_roi_btn.clicked.connect(self._on_delete_roi)
        self._delete_roi_btn.setEnabled(False)
        self._apply_button_style(self._delete_roi_btn, "accent")

        toolbar.addWidget(self._add_roi_btn)
        toolbar.addWidget(self._edit_roi_btn)
        toolbar.addWidget(self._delete_roi_btn)
        toolbar.addStretch()
        list_layout.addLayout(toolbar)

        # ROI Tree
        self._roi_tree = QTreeWidget()
        self._roi_tree.setHeaderLabels(["ROI ID", "Name", "Shape", "Enabled", "Alarm", "Min", "Mean", "Max"])
        self._roi_tree.setColumnWidth(0, 100)
        self._roi_tree.setColumnWidth(1, 120)
        self._roi_tree.setColumnWidth(2, 80)
        self._roi_tree.setColumnWidth(3, 60)
        self._roi_tree.setColumnWidth(4, 60)
        self._roi_tree.setColumnWidth(5, 70)
        self._roi_tree.setColumnWidth(6, 70)
        self._roi_tree.setColumnWidth(7, 70)
        self._roi_tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._apply_tree_style(self._roi_tree)
        list_layout.addWidget(self._roi_tree, 1)

        layout.addWidget(list_group, 1)

        # ROI Editor (initially hidden)
        self._roi_editor = self._create_roi_editor()
        self._roi_editor.setVisible(False)
        layout.addWidget(self._roi_editor)

    def _create_roi_editor(self) -> QWidget:
        editor = QWidget()
        layout = QVBoxLayout(editor)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        group = QGroupBox("ROI Editor")
        group_layout = QVBoxLayout(group)

        # Shape info (read-only)
        shape_form = QFormLayout()
        shape_form.setSpacing(6)

        self._editor_shape_label = QLabel("—")
        shape_form.addRow("Shape:", self._editor_shape_label)

        self._editor_roi_id_label = QLabel("—")
        shape_form.addRow("ROI ID:", self._editor_roi_id_label)

        self._editor_name_edit = QLineEdit()
        shape_form.addRow("Name:", self._editor_name_edit)

        group_layout.addLayout(shape_form)

        # Geometry parameters - dynamic based on shape
        self._geometry_stack = QWidget()
        self._geometry_layout = QVBoxLayout(self._geometry_stack)
        self._geometry_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.addWidget(self._geometry_stack)

        # Temperature limits
        limits_group = QGroupBox("Temperature Limits")
        limits_layout = QFormLayout(limits_group)
        limits_layout.setSpacing(6)

        self._editor_unit_combo = QComboBox()
        self._editor_unit_combo.addItems([u.value for u in TemperatureUnit])

        self._editor_min_warning = QDoubleSpinBox()
        self._editor_min_warning.setRange(-273.15, 2000.0)
        self._editor_min_warning.setDecimals(1)
        self._editor_min_warning.setSpecialValueText("Not set")
        self._editor_min_warning.setValue(-273.15)

        self._editor_max_warning = QDoubleSpinBox()
        self._editor_max_warning.setRange(-273.15, 2000.0)
        self._editor_max_warning.setDecimals(1)
        self._editor_max_warning.setSpecialValueText("Not set")
        self._editor_max_warning.setValue(-273.15)

        self._editor_min_critical = QDoubleSpinBox()
        self._editor_min_critical.setRange(-273.15, 2000.0)
        self._editor_min_critical.setDecimals(1)
        self._editor_min_critical.setSpecialValueText("Not set")
        self._editor_min_critical.setValue(-273.15)

        self._editor_max_critical = QDoubleSpinBox()
        self._editor_max_critical.setRange(-273.15, 2000.0)
        self._editor_max_critical.setDecimals(1)
        self._editor_max_critical.setSpecialValueText("Not set")
        self._editor_max_critical.setValue(-273.15)

        self._editor_rate_limit = QDoubleSpinBox()
        self._editor_rate_limit.setRange(0.0, 1000.0)
        self._editor_rate_limit.setDecimals(1)
        self._editor_rate_limit.setSuffix(" °C/s")
        self._editor_rate_limit.setSpecialValueText("Not set")

        limits_layout.addRow("Unit:", self._editor_unit_combo)
        limits_layout.addRow("Min Warning:", self._editor_min_warning)
        limits_layout.addRow("Max Warning:", self._editor_max_warning)
        limits_layout.addRow("Min Critical:", self._editor_min_critical)
        limits_layout.addRow("Max Critical:", self._editor_max_critical)
        limits_layout.addRow("Rate of Change:", self._editor_rate_limit)

        group_layout.addWidget(limits_group)

        # Alarm enabled
        self._editor_alarm_enabled = QCheckBox("Enable Alarm Evaluation")
        self._editor_alarm_enabled.setChecked(True)
        group_layout.addWidget(self._editor_alarm_enabled)

        # Save button
        self._editor_save_btn = QPushButton("Save ROI")
        self._editor_save_btn.clicked.connect(self._on_save_roi)
        self._editor_save_btn.setEnabled(False)
        self._apply_button_style(self._editor_save_btn, "primary")
        group_layout.addWidget(self._editor_save_btn)

        group_layout.addStretch()
        layout.addWidget(group)

        return editor

    def _on_selection_changed(self) -> None:
        items = self._roi_tree.selectedItems()
        if items:
            item = items[0]
            roi_id = item.data(0, Qt.ItemDataRole.UserRole)
            self._selected_roi_id = roi_id
            self._load_roi_editor(roi_id)
            self._edit_roi_btn.setEnabled(True)
            self._delete_roi_btn.setEnabled(True)
            self.roi_selected.emit(roi_id)
        else:
            self._roi_editor.setVisible(False)
            self._edit_roi_btn.setEnabled(False)
            self._delete_roi_btn.setEnabled(False)
            self._selected_roi_id = None

    def _load_roi_editor(self, roi_id: str) -> None:
        if not self._selected_camera_id:
            return
        analysis = self._config_service.get_analysis_config(self._selected_camera_id)
        if not analysis or roi_id not in analysis.rois:
            return

        roi = analysis.rois[roi_id]
        self._roi_editor.setVisible(True)
        self._editor_save_btn.setEnabled(True)

        self._editor_roi_id_label.setText(roi.roi_id)
        self._editor_shape_label.setText(roi.geometry.shape.value)
        self._editor_name_edit.setText(roi.name)

        # Build geometry editor
        self._build_geometry_editor(roi.geometry)

        # Load limits
        limits = roi.temperature_limits
        self._editor_unit_combo.setCurrentText(limits.unit.value)
        self._editor_min_warning.setValue(limits.min_warning if limits.min_warning is not None else -273.15)
        self._editor_max_warning.setValue(limits.max_warning if limits.max_warning is not None else -273.15)
        self._editor_min_critical.setValue(limits.min_critical if limits.min_critical is not None else -273.15)
        self._editor_max_critical.setValue(limits.max_critical if limits.max_critical is not None else -273.15)
        self._editor_rate_limit.setValue(limits.rate_of_change_limit if limits.rate_of_change_limit is not None else 0.0)
        self._editor_alarm_enabled.setChecked(roi.alarm_enabled)

        # Mark dirty on changes
        self._editor_name_edit.textChanged.connect(lambda: self._mark_dirty())
        for spin in [
            self._editor_min_warning, self._editor_max_warning,
            self._editor_min_critical, self._editor_max_critical, self._editor_rate_limit
        ]:
            spin.valueChanged.connect(lambda: self._mark_dirty())
        self._editor_alarm_enabled.toggled.connect(lambda: self._mark_dirty())

    def _build_geometry_editor(self, geometry: ROIGeometry) -> None:
        while self._geometry_layout.count():
            child = self._geometry_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        shape = geometry.shape
        params = geometry.parameters

        geom_group = QGroupBox("Geometry (row/col coordinates)")
        form = QFormLayout(geom_group)
        form.setSpacing(6)

        if shape == ROIShape.RECTANGLE1:
            self._editor_y1 = self._make_spin(params.get("y1", 0.0))
            self._editor_x1 = self._make_spin(params.get("x1", 0.0))
            self._editor_y2 = self._make_spin(params.get("y2", 100.0))
            self._editor_x2 = self._make_spin(params.get("x2", 100.0))
            form.addRow("Y1 (top):", self._editor_y1)
            form.addRow("X1 (left):", self._editor_x1)
            form.addRow("Y2 (bottom):", self._editor_y2)
            form.addRow("X2 (right):", self._editor_x2)

        elif shape == ROIShape.RECTANGLE2:
            self._editor_cy = self._make_spin(params.get("center_y", 0.0))
            self._editor_cx = self._make_spin(params.get("center_x", 0.0))
            self._editor_phi = self._make_spin(params.get("phi", 0.0), -3.14159, 3.14159, 0.001)
            self._editor_len1 = self._make_spin(params.get("length1", 50.0), 0.0, 10000.0)
            self._editor_len2 = self._make_spin(params.get("length2", 50.0), 0.0, 10000.0)
            form.addRow("Center Y:", self._editor_cy)
            form.addRow("Center X:", self._editor_cx)
            form.addRow("Phi (rad):", self._editor_phi)
            form.addRow("Length 1:", self._editor_len1)
            form.addRow("Length 2:", self._editor_len2)

        elif shape == ROIShape.CIRCLE:
            self._editor_cy = self._make_spin(params.get("center_y", 0.0))
            self._editor_cx = self._make_spin(params.get("center_x", 0.0))
            self._editor_radius = self._make_spin(params.get("radius", 50.0), 0.0, 10000.0)
            form.addRow("Center Y:", self._editor_cy)
            form.addRow("Center X:", self._editor_cx)
            form.addRow("Radius:", self._editor_radius)

        elif shape == ROIShape.ELLIPSE:
            self._editor_cy = self._make_spin(params.get("center_y", 0.0))
            self._editor_cx = self._make_spin(params.get("center_x", 0.0))
            self._editor_phi = self._make_spin(params.get("phi", 0.0), -3.14159, 3.14159, 0.001)
            self._editor_r1 = self._make_spin(params.get("radius1", 50.0), 0.0, 10000.0)
            self._editor_r2 = self._make_spin(params.get("radius2", 30.0), 0.0, 10000.0)
            form.addRow("Center Y:", self._editor_cy)
            form.addRow("Center X:", self._editor_cx)
            form.addRow("Phi (rad):", self._editor_phi)
            form.addRow("Radius 1:", self._editor_r1)
            form.addRow("Radius 2:", self._editor_r2)

        elif shape == ROIShape.POLYGON:
            points = params.get("points", [])
            self._editor_polygon = QTextEdit()
            self._editor_polygon.setMaximumHeight(100)
            self._editor_polygon.setPlaceholderText("One point per line: row,col")
            point_text = "\n".join(f"{p[0]}, {p[1]}" for p in points)
            self._editor_polygon.setPlainText(point_text)
            form.addRow("Points (row,col):", self._editor_polygon)

        self._geometry_layout.addWidget(geom_group)

    def _make_spin(self, value: float, min_val: float = -10000.0, max_val: float = 10000.0, step: float = 0.1) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(min_val, max_val)
        spin.setDecimals(2)
        spin.setSingleStep(step)
        spin.setValue(value)
        spin.valueChanged.connect(lambda: self._mark_dirty())
        self._apply_input_style(spin)
        return spin

    def _on_add_roi(self) -> None:
        if not self._selected_camera_id:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Add ROI")
        layout = QFormLayout(dialog)

        roi_id_edit = QLineEdit()
        roi_id_edit.setPlaceholderText("e.g., roi_001")
        name_edit = QLineEdit()
        name_edit.setPlaceholderText("e.g., Hot Spot")
        shape_combo = QComboBox()
        shape_combo.addItems([s.value for s in ROIShape])

        layout.addRow("ROI ID:", roi_id_edit)
        layout.addRow("Name:", name_edit)
        layout.addRow("Shape:", shape_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            roi_id = roi_id_edit.text().strip()
            name = name_edit.text().strip()
            shape = ROIShape(shape_combo.currentText())

            if not roi_id:
                QMessageBox.warning(self, "Invalid Input", "ROI ID is required.")
                return

            analysis = self._config_service.get_analysis_config(self._selected_camera_id)
            if not analysis:
                analysis = self._config_service.create_analysis_config(self._selected_camera_id)

            if roi_id in analysis.rois:
                QMessageBox.warning(self, "Duplicate", f"ROI '{roi_id}' already exists.")
                return

            geometry = self._create_default_geometry(shape)
            roi_config = ROIConfig(roi_id=roi_id, name=name, geometry=geometry)

            new_rois = dict(analysis.rois)
            new_rois[roi_id] = roi_config

            new_associations = dict(analysis.position_associations)
            if "default" not in new_associations:
                new_associations["default"] = PositionROIAssociation(
                    position_id="default",
                    position_name="Default",
                    roi_ids=(roi_id,),
                )
            else:
                assoc = new_associations["default"]
                new_associations["default"] = PositionROIAssociation(
                    position_id=assoc.position_id,
                    position_name=assoc.position_name,
                    roi_ids=assoc.roi_ids + (roi_id,),
                )

            updated_config = AnalysisConfig(
                camera_id=analysis.camera_id,
                rois=new_rois,
                position_associations=new_associations,
                alarm_rules=analysis.alarm_rules,
                default_emissivity=analysis.default_emissivity,
                ambient_temperature=analysis.ambient_temperature,
                distance=analysis.distance,
                humidity=analysis.humidity,
                reflected_temperature=analysis.reflected_temperature,
                unit=analysis.unit,
            )
            self._config_service.set_analysis_config(updated_config)
            self.refresh_roi_list()
            self._mark_dirty()
            self.roi_created.emit(roi_id, roi_config)

    def _on_edit_roi(self) -> None:
        if self._selected_roi_id:
            self._load_roi_editor(self._selected_roi_id)

    def _on_delete_roi(self) -> None:
        if not self._selected_roi_id or not self._selected_camera_id:
            return

        roi_id = self._selected_roi_id

        reply = QMessageBox.question(
            self,
            "Delete ROI",
            f"Delete ROI '{roi_id}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        analysis = self._config_service.get_analysis_config(self._selected_camera_id)
        if not analysis:
            return

        new_rois = {k: v for k, v in analysis.rois.items() if k != roi_id}

        new_associations = {}
        for pos_id, assoc in analysis.position_associations.items():
            new_roi_ids = tuple(r for r in assoc.roi_ids if r != roi_id)
            if new_roi_ids:
                new_associations[pos_id] = PositionROIAssociation(
                    position_id=assoc.position_id,
                    position_name=assoc.position_name,
                    roi_ids=new_roi_ids,
                )

        updated_config = AnalysisConfig(
            camera_id=analysis.camera_id,
            rois=new_rois,
            position_associations=new_associations,
            alarm_rules=analysis.alarm_rules,
            default_emissivity=analysis.default_emissivity,
            ambient_temperature=analysis.ambient_temperature,
            distance=analysis.distance,
            humidity=analysis.humidity,
            reflected_temperature=analysis.reflected_temperature,
            unit=analysis.unit,
        )
        self._config_service.set_analysis_config(updated_config)
        self.refresh_roi_list()
        self._mark_dirty()
        self.roi_deleted.emit(roi_id)

    def _create_default_geometry(self, shape: ROIShape) -> ROIGeometry:
        if shape == ROIShape.RECTANGLE1:
            return ROIGeometry(shape=ROIShape.RECTANGLE1, parameters={"y1": 100.0, "x1": 100.0, "y2": 200.0, "x2": 200.0})
        elif shape == ROIShape.RECTANGLE2:
            return ROIGeometry(shape=ROIShape.RECTANGLE2, parameters={"center_y": 150.0, "center_x": 150.0, "phi": 0.0, "length1": 50.0, "length2": 50.0})
        elif shape == ROIShape.CIRCLE:
            return ROIGeometry(shape=ROIShape.CIRCLE, parameters={"center_y": 150.0, "center_x": 150.0, "radius": 50.0})
        elif shape == ROIShape.ELLIPSE:
            return ROIGeometry(shape=ROIShape.ELLIPSE, parameters={"center_y": 150.0, "center_x": 150.0, "phi": 0.0, "radius1": 50.0, "radius2": 30.0})
        elif shape == ROIShape.POLYGON:
            return ROIGeometry(shape=ROIShape.POLYGON, parameters={"points": [(100.0, 100.0), (200.0, 100.0), (150.0, 200.0)]})
        return ROIGeometry(shape=ROIShape.RECTANGLE1)

    def _on_save_roi(self) -> None:
        if not self._selected_camera_id or not self._editor_roi_id_label.text():
            return

        roi_id = self._editor_roi_id_label.text()
        analysis = self._config_service.get_analysis_config(self._selected_camera_id)
        if not analysis or roi_id not in analysis.rois:
            return

        old_roi = analysis.rois[roi_id]

        geometry = self._read_geometry_editor()
        if not geometry:
            return

        limits = TemperatureLimits(
            unit=TemperatureUnit(self._editor_unit_combo.currentText()),
            min_warning=self._editor_min_warning.value() if self._editor_min_warning.value() > -273.15 else None,
            max_warning=self._editor_max_warning.value() if self._editor_max_warning.value() > -273.15 else None,
            min_critical=self._editor_min_critical.value() if self._editor_min_critical.value() > -273.15 else None,
            max_critical=self._editor_max_critical.value() if self._editor_max_critical.value() > -273.15 else None,
            rate_of_change_limit=self._editor_rate_limit.value() if self._editor_rate_limit.value() > 0.0 else None,
        )

        try:
            limits.__post_init__()
        except ValueError as e:
            QMessageBox.warning(self, "Invalid Limits", str(e))
            return

        new_roi = ROIConfig(
            roi_id=old_roi.roi_id,
            name=self._editor_name_edit.text(),
            enabled=old_roi.enabled,
            geometry=geometry,
            temperature_limits=limits,
            alarm_enabled=self._editor_alarm_enabled.isChecked(),
            metadata=old_roi.metadata,
        )

        new_rois = dict(analysis.rois)
        new_rois[roi_id] = new_roi

        updated_config = AnalysisConfig(
            camera_id=analysis.camera_id,
            rois=new_rois,
            position_associations=analysis.position_associations,
            alarm_rules=analysis.alarm_rules,
            default_emissivity=analysis.default_emissivity,
            ambient_temperature=analysis.ambient_temperature,
            distance=analysis.distance,
            humidity=analysis.humidity,
            reflected_temperature=analysis.reflected_temperature,
            unit=analysis.unit,
        )
        self._config_service.set_analysis_config(updated_config)
        self.refresh_roi_list()
        self._mark_dirty()
        self.roi_updated.emit(roi_id, new_roi)

    def _read_geometry_editor(self) -> ROIGeometry | None:
        shape_text = self._editor_shape_label.text()
        try:
            shape = ROIShape(shape_text)
        except ValueError:
            return None

        if shape == ROIShape.RECTANGLE1:
            return ROIGeometry(shape=ROIShape.RECTANGLE1, parameters={
                "y1": self._editor_y1.value(), "x1": self._editor_x1.value(),
                "y2": self._editor_y2.value(), "x2": self._editor_x2.value(),
            })
        elif shape == ROIShape.RECTANGLE2:
            return ROIGeometry(shape=ROIShape.RECTANGLE2, parameters={
                "center_y": self._editor_cy.value(), "center_x": self._editor_cx.value(),
                "phi": self._editor_phi.value(), "length1": self._editor_len1.value(), "length2": self._editor_len2.value(),
            })
        elif shape == ROIShape.CIRCLE:
            return ROIGeometry(shape=ROIShape.CIRCLE, parameters={
                "center_y": self._editor_cy.value(), "center_x": self._editor_cx.value(),
                "radius": self._editor_radius.value(),
            })
        elif shape == ROIShape.ELLIPSE:
            return ROIGeometry(shape=ROIShape.ELLIPSE, parameters={
                "center_y": self._editor_cy.value(), "center_x": self._editor_cx.value(),
                "phi": self._editor_phi.value(), "radius1": self._editor_r1.value(), "radius2": self._editor_r2.value(),
            })
        elif shape == ROIShape.POLYGON:
            text = self._editor_polygon.toPlainText().strip()
            points = []
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    parts = line.split(",")
                    if len(parts) == 2:
                        y = float(parts[0].strip())
                        x = float(parts[1].strip())
                        points.append((y, x))
                except ValueError:
                    continue
            if len(points) < 3:
                QMessageBox.warning(self, "Invalid Polygon", "Polygon requires at least 3 points.")
                return None
            return ROIGeometry(shape=ROIShape.POLYGON, parameters={"points": points})
        return None

    def _mark_dirty(self) -> None:
        if self._selected_camera_id:
            self._dirty_camera_configs.add(self._selected_camera_id)

    # Public API

    def set_camera(self, camera_id: str) -> None:
        """Switch to a different camera."""
        self._selected_camera_id = camera_id
        self.refresh_roi_list()
        self._roi_editor.setVisible(False)
        self._edit_roi_btn.setEnabled(False)
        self._delete_roi_btn.setEnabled(False)
        self._selected_roi_id = None

    def refresh_roi_list(self) -> None:
        """Refresh ROI list from analysis config."""
        self._roi_tree.clear()
        if not self._selected_camera_id:
            return

        analysis = self._config_service.get_analysis_config(self._selected_camera_id)
        if not analysis:
            return

        rois = analysis.get_rois_for_position("default")
        for roi in rois:
            limits_text = ""
            if roi.temperature_limits.is_configured():
                parts = []
                if roi.temperature_limits.max_critical:
                    parts.append(f"crit>={roi.temperature_limits.max_critical:.1f}")
                if roi.temperature_limits.max_warning:
                    parts.append(f"warn>={roi.temperature_limits.max_warning:.1f}")
                if roi.temperature_limits.min_warning:
                    parts.append(f"warn<={roi.temperature_limits.min_warning:.1f}")
                if roi.temperature_limits.min_critical:
                    parts.append(f"crit<={roi.temperature_limits.min_critical:.1f}")
                limits_text = ", ".join(parts)

            item = QTreeWidgetItem([
                roi.roi_id,
                roi.name,
                roi.geometry.shape.value,
                "Yes" if roi.enabled else "No",
                "Yes" if roi.alarm_enabled else "No",
                "—", "—", "—"
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, roi.roi_id)
            self._roi_tree.addTopLevelItem(item)

    def update_live_stats(self, analysis) -> None:
        """Update ROI tree with live temperature statistics."""
        unit_symbol = "°C"
        if hasattr(analysis, "unit"):
            unit_map = {TemperatureUnit.CELSIUS: "°C", TemperatureUnit.FAHRENHEIT: "°F", TemperatureUnit.KELVIN: "K"}
            unit_symbol = unit_map.get(analysis.unit, "°C")

        for i in range(self._roi_tree.topLevelItemCount()):
            item = self._roi_tree.topLevelItem(i)
            roi_id = item.data(0, Qt.ItemDataRole.UserRole)
            roi_stat = analysis.roi_results.get(roi_id)
            if roi_stat:
                item.setText(5, f"{roi_stat.min_temp:.1f} {unit_symbol}")
                item.setText(6, f"{roi_stat.mean_temp:.1f} {unit_symbol}")
                item.setText(7, f"{roi_stat.max_temp:.1f} {unit_symbol}")

    def has_unsaved_changes(self) -> bool:
        return bool(self._dirty_camera_configs)

    def clear_dirty(self, camera_id: str) -> None:
        self._dirty_camera_configs.discard(camera_id)

    def _apply_button_style(self, btn: QPushButton, style: str) -> None:
        if not self._theme:
            return
        if style == "primary":
            btn.setStyleSheet(self._theme.primary_button_stylesheet())
        elif style == "secondary":
            btn.setStyleSheet(self._theme.secondary_button_stylesheet())
        elif style == "accent":
            btn.setStyleSheet(self._theme.accent_button_stylesheet())

    def _apply_input_style(self, widget) -> None:
        if self._theme:
            widget.setStyleSheet(self._theme.base_stylesheet())

    def _apply_tree_style(self, tree: QTreeWidget) -> None:
        if self._theme:
            tree.setStyleSheet(f"""
                QTreeWidget {{
                    background-color: {self._theme.colors().background};
                    border: 1px solid {self._theme.colors().border};
                }}
                QTreeWidget::item {{
                    padding: 4px;
                }}
                QHeaderView::section {{
                    background-color: {self._theme.colors().panel};
                    color: {self._theme.colors().text_primary};
                    border: 1px solid {self._theme.colors().border};
                    padding: 6px;
                    font-weight: bold;
                }}
            """)


__all__ = ["ROIPanel"]