"""
ui.modes.configuration -- Configuration mode widget.

Provides the UI for configuring a single selected camera:
- Camera selector (navigation between cameras)
- Camera identity/details (read-only, populated from discovery)
- ROI configuration (all shapes: Rectangle1, Rectangle2, Circle, Ellipse, Polygon)
- PTZ position configuration
- Alarm configuration
- Recording configuration
- Calibration information
- System configuration

Discovery and camera add/remove are handled in the Launcher/startup screen.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QTabWidget,
    QComboBox,
    QGroupBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QDoubleSpinBox,
    QComboBox,
    QCheckBox,
    QPushButton,
    QLabel,
    QTextEdit,
    QMessageBox,
    QScrollArea,
    QTreeWidget,
    QTreeWidgetItem,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QHBoxLayout,
)

from thermal_monitor.core.modes import ApplicationMode
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.runtime import CameraRuntimeService
from thermal_monitor.storage.database import Database
from thermal_monitor.core.models import (
    CameraConfig,
    CameraIdentity,
    PTZConfig,
    PTZPosition,
    PTZLimits,
    ROIConfig,
    ROIGeometry,
    ROIShape,
    TemperatureLimits,
    TemperatureUnit,
    AlarmRule,
    AlarmCondition,
    AlarmSeverity,
    RecordingConfig,
    SystemConfig,
    AnalysisConfig,
    PositionROIAssociation,
)


class ConfigurationModeWidget(QWidget):
    """Main widget for Configuration mode - single camera detailed configuration."""

    def __init__(
        self,
        config_service: ConfigurationService,
        mode_service: ModeService,
        database: Database | None = None,
        runtime_service: CameraRuntimeService | None = None,
    ) -> None:
        super().__init__()

        self._config_service = config_service
        self._mode_service = mode_service
        self._database = database
        self._runtime_service = runtime_service
        self._selected_camera_id: str | None = None

        self._setup_ui()
        self._connect_signals()
        self._load_initial_data()

    def _setup_ui(self) -> None:
        """Set up the UI layout."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Camera selector at top
        selector_group = QGroupBox("Camera Selection")
        selector_layout = QHBoxLayout(selector_group)

        self._prev_btn = QPushButton("◀ Previous Camera")
        self._prev_btn.clicked.connect(self._select_prev_camera)
        self._prev_btn.setEnabled(False)

        self._camera_combo = QComboBox()
        self._camera_combo.setMinimumWidth(300)
        self._camera_combo.currentIndexChanged.connect(self._on_camera_selected)

        self._next_btn = QPushButton("Next Camera ▶")
        self._next_btn.clicked.connect(self._select_next_camera)
        self._next_btn.setEnabled(False)

        selector_layout.addWidget(self._prev_btn)
        selector_layout.addWidget(QLabel("Camera:"))
        selector_layout.addWidget(self._camera_combo, 1)
        selector_layout.addWidget(self._next_btn)

        layout.addWidget(selector_group)

        # Main tab widget for configuration categories
        self._tabs = QTabWidget()
        layout.addWidget(self._tabs, 1)

        # Create configuration tabs (without camera discovery tab)
        self._identity_tab = CameraIdentityTab(self._config_service)
        self._roi_tab = ROIConfigurationTab(self._config_service, self._database)
        self._ptz_tab = PTZConfigurationTab(self._config_service, self._database)
        self._alarm_tab = AlarmConfigurationTab(self._config_service, self._database)
        self._recording_tab = RecordingConfigurationTab(self._config_service, self._database)
        self._calibration_tab = CalibrationInformationTab(self._config_service, self._database)
        self._system_tab = SystemConfigurationTab(self._config_service, self._database)

        self._tabs.addTab(self._identity_tab, "Identity")
        self._tabs.addTab(self._roi_tab, "ROIs")
        self._tabs.addTab(self._ptz_tab, "PTZ Positions")
        self._tabs.addTab(self._alarm_tab, "Alarms")
        self._tabs.addTab(self._recording_tab, "Recording")
        self._tabs.addTab(self._calibration_tab, "Calibration")
        self._tabs.addTab(self._system_tab, "System")

    def _connect_signals(self) -> None:
        """Connect internal signals."""
        self._config_service.add_camera_change_callback(self._on_camera_config_changed)
        self._config_service.add_analysis_change_callback(self._on_analysis_config_changed)
        self._config_service.add_recording_change_callback(self._on_recording_config_changed)
        self._config_service.add_system_change_callback(self._on_system_config_changed)

    def _load_initial_data(self) -> None:
        """Load initial configuration data."""
        self._refresh_camera_selector()
        if self._selected_camera_id:
            self._load_camera_config(self._selected_camera_id)

    def _refresh_camera_selector(self) -> None:
        """Refresh the camera selector combo box."""
        current_id = self._camera_combo.currentData()
        self._camera_combo.clear()

        cameras = self._config_service.get_all_camera_configs()
        for config in cameras:
            display = f"{config.identity.serial_number} — {config.identity.model}"
            if config.name and config.name != config.identity.camera_id:
                display = f"{config.name} ({display})"
            self._camera_combo.addItem(display, config.identity.camera_id)

        # Restore selection if possible
        if current_id:
            index = self._camera_combo.findData(current_id)
            if index >= 0:
                self._camera_combo.setCurrentIndex(index)

        self._update_navigation_buttons()

    def _on_camera_selected(self, index: int) -> None:
        """Handle camera selection change."""
        camera_id = self._camera_combo.itemData(index)
        if camera_id:
            self._selected_camera_id = camera_id
            self._load_camera_config(camera_id)
        self._update_navigation_buttons()

    def _select_prev_camera(self) -> None:
        """Select previous camera in list."""
        current = self._camera_combo.currentIndex()
        if current > 0:
            self._camera_combo.setCurrentIndex(current - 1)

    def _select_next_camera(self) -> None:
        """Select next camera in list."""
        current = self._camera_combo.currentIndex()
        if current < self._camera_combo.count() - 1:
            self._camera_combo.setCurrentIndex(current + 1)

    def _update_navigation_buttons(self) -> None:
        """Update prev/next button states."""
        current = self._camera_combo.currentIndex()
        count = self._camera_combo.count()
        self._prev_btn.setEnabled(current > 0)
        self._next_btn.setEnabled(current < count - 1 and count > 0)

    def _load_camera_config(self, camera_id: str) -> None:
        """Load configuration for the selected camera into all tabs."""
        self._identity_tab.set_camera(camera_id)
        self._roi_tab.set_camera(camera_id)
        self._ptz_tab.set_camera(camera_id)
        self._alarm_tab.set_camera(camera_id)
        self._recording_tab.set_camera(camera_id)
        self._calibration_tab.set_camera(camera_id)

    def _on_camera_config_changed(self, camera_id: str, config: CameraConfig) -> None:
        self._refresh_camera_selector()
        if camera_id == self._selected_camera_id:
            self._identity_tab.set_camera(camera_id)
        self._roi_tab.refresh_cameras()
        self._ptz_tab.refresh_cameras()
        self._alarm_tab.refresh_cameras()
        self._recording_tab.refresh_cameras()

    def _on_analysis_config_changed(self, camera_id: str, config: AnalysisConfig) -> None:
        if camera_id == self._selected_camera_id:
            self._roi_tab.refresh_camera_rois(camera_id)
            self._alarm_tab.refresh_camera_alarms(camera_id)

    def _on_recording_config_changed(self, camera_id: str, config: RecordingConfig) -> None:
        if camera_id == self._selected_camera_id:
            self._recording_tab.refresh_camera(camera_id)

    def _on_system_config_changed(self, config: SystemConfig) -> None:
        self._system_tab.refresh()

    def on_mode_activated(self) -> None:
        """Called when configuration mode becomes active."""
        self._refresh_camera_selector()
        if self._selected_camera_id:
            self._load_camera_config(self._selected_camera_id)
        else:
            # Select first camera if available
            cameras = self._config_service.get_all_camera_configs()
            if cameras:
                self._camera_combo.setCurrentIndex(0)

    def show_camera_config(self) -> None:
        self._tabs.setCurrentWidget(self._identity_tab)

    def show_roi_config(self) -> None:
        self._tabs.setCurrentWidget(self._roi_tab)

    def show_alarm_config(self) -> None:
        self._tabs.setCurrentWidget(self._alarm_tab)

    def show_system_config(self) -> None:
        self._tabs.setCurrentWidget(self._system_tab)

    def closeEvent(self, event) -> None:
        self._config_service.remove_camera_change_callback(self._on_camera_config_changed)
        self._config_service.remove_analysis_change_callback(self._on_analysis_config_changed)
        self._config_service.remove_recording_change_callback(self._on_recording_config_changed)
        self._config_service.remove_system_change_callback(self._on_system_config_changed)
        super().closeEvent(event)


# --------------------------------------------------------------------------
# Camera Identity Tab (read-only, populated from discovery)
# --------------------------------------------------------------------------


class CameraIdentityTab(QWidget):
    """Read-only identity view for a single camera.

    Populated from discovery data via CameraConfig.metadata.
    User cannot manually enter serial, model, vendor, IP, etc.
    """

    def __init__(self, config_service: ConfigurationService) -> None:
        super().__init__()
        self._config_service = config_service
        self._current_camera_id: str | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content = QWidget()
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(8, 8, 8, 8)

        # Identity group (read-only)
        self._identity_group = QGroupBox("Camera Identity (from Discovery)")
        identity_layout = QFormLayout(self._identity_group)

        self._camera_id_label = QLabel("—")
        self._camera_id_label.setStyleSheet("font-family: monospace;")
        self._serial_label = QLabel("—")
        self._model_label = QLabel("—")
        self._vendor_label = QLabel("—")
        self._firmware_label = QLabel("—")
        self._user_name_label = QLabel("—")
        self._ip_label = QLabel("—")
        self._ip_label.setStyleSheet("font-family: monospace;")
        self._device_id_label = QLabel("—")
        self._device_id_label.setStyleSheet("font-family: monospace;")

        identity_layout.addRow("Camera ID:", self._camera_id_label)
        identity_layout.addRow("Serial Number:", self._serial_label)
        identity_layout.addRow("Model:", self._model_label)
        identity_layout.addRow("Vendor:", self._vendor_label)
        identity_layout.addRow("Firmware:", self._firmware_label)
        identity_layout.addRow("User Name:", self._user_name_label)
        identity_layout.addRow("IP Address:", self._ip_label)
        identity_layout.addRow("Device Identifier:", self._device_id_label)

        self._layout.addWidget(self._identity_group)

        # General settings (editable)
        self._general_group = QGroupBox("General Settings")
        general_layout = QFormLayout(self._general_group)

        self._name_edit = QLineEdit()
        self._description_edit = QTextEdit()
        self._description_edit.setMaximumHeight(60)
        self._enabled_check = QCheckBox()
        self._thermal_enabled_check = QCheckBox()
        self._visible_enabled_check = QCheckBox()

        general_layout.addRow("Display Name:", self._name_edit)
        general_layout.addRow("Description:", self._description_edit)
        general_layout.addRow("Enabled:", self._enabled_check)
        general_layout.addRow("Thermal Stream:", self._thermal_enabled_check)
        general_layout.addRow("Visible Stream:", self._visible_enabled_check)

        self._layout.addWidget(self._general_group)

        # Acquisition settings (editable)
        self._acq_group = QGroupBox("Acquisition Settings")
        acq_layout = QFormLayout(self._acq_group)

        self._fps_spin = QSpinBox()
        self._fps_spin.setRange(1, 60)
        self._reconnect_spin = QDoubleSpinBox()
        self._reconnect_spin.setRange(0.1, 60.0)
        self._reconnect_spin.setSuffix(" s")
        self._nuc_duration_spin = QDoubleSpinBox()
        self._nuc_duration_spin.setRange(0.1, 30.0)
        self._nuc_duration_spin.setSuffix(" s")
        self._grab_timeout_spin = QDoubleSpinBox()
        self._grab_timeout_spin.setRange(0.1, 60.0)
        self._grab_timeout_spin.setSuffix(" s")

        acq_layout.addRow("Target FPS:", self._fps_spin)
        acq_layout.addRow("Reconnect Interval:", self._reconnect_spin)
        acq_layout.addRow("NUC Duration:", self._nuc_duration_spin)
        acq_layout.addRow("Grab Timeout:", self._grab_timeout_spin)

        self._layout.addWidget(self._acq_group)

        # Display settings (editable)
        self._display_group = QGroupBox("Display Settings")
        display_layout = QFormLayout(self._display_group)

        self._palette_combo = QComboBox()
        self._palette_combo.addItems(["temperature", "iron", "rainbow", "gray", "hot"])
        self._zoom_spin = QSpinBox()
        self._zoom_spin.setRange(10, 500)
        self._zoom_spin.setSuffix(" %")

        display_layout.addRow("Default Palette:", self._palette_combo)
        display_layout.addRow("Default Zoom:", self._zoom_spin)

        self._layout.addWidget(self._display_group)

        self._layout.addStretch()

        # Save button
        self._save_btn = QPushButton("Save Changes")
        self._save_btn.clicked.connect(self._save)
        self._save_btn.setEnabled(False)
        self._layout.addWidget(self._save_btn)

        scroll.setWidget(self._content)
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(scroll)

    def set_camera(self, camera_id: str) -> None:
        """Load camera configuration into the detail view."""
        config = self._config_service.get_camera_config(camera_id)
        if not config:
            self.clear()
            return

        self._current_camera_id = camera_id
        self._save_btn.setEnabled(True)

        identity = config.identity
        metadata = config.metadata or {}

        # Read-only identity fields (from discovery)
        self._camera_id_label.setText(identity.camera_id)
        self._serial_label.setText(identity.serial_number)
        self._model_label.setText(identity.model or "(not provided)")
        self._vendor_label.setText(identity.vendor or "(not provided)")
        self._firmware_label.setText(identity.firmware or "(not provided)")
        self._user_name_label.setText(identity.user_name or "(not provided)")
        self._ip_label.setText(metadata.get("ip_address", "(not provided)"))
        self._device_id_label.setText(metadata.get("device_identifier", "(not provided)"))

        # Editable general settings
        self._name_edit.setText(config.name)
        self._description_edit.setPlainText(config.description)
        self._enabled_check.setChecked(config.enabled)
        self._thermal_enabled_check.setChecked(config.thermal_enabled)
        self._visible_enabled_check.setChecked(config.visible_enabled)

        # Acquisition settings from metadata
        self._fps_spin.setValue(int(metadata.get("frame_rate", 9)))
        self._reconnect_spin.setValue(float(metadata.get("reconnect_interval_s", 3.0)))
        self._nuc_duration_spin.setValue(float(metadata.get("nuc_duration_s", 1.0)))
        self._grab_timeout_spin.setValue(float(metadata.get("grab_timeout_ms", 500)) / 1000.0)

    def clear(self) -> None:
        """Clear the detail view."""
        self._current_camera_id = None
        self._save_btn.setEnabled(False)
        self._camera_id_label.setText("—")
        self._serial_label.setText("—")
        self._model_label.setText("—")
        self._vendor_label.setText("—")
        self._firmware_label.setText("—")
        self._user_name_label.setText("—")
        self._ip_label.setText("—")
        self._device_id_label.setText("—")
        self._name_edit.clear()
        self._description_edit.clear()
        self._enabled_check.setChecked(False)
        self._thermal_enabled_check.setChecked(False)
        self._visible_enabled_check.setChecked(False)
        self._fps_spin.setValue(9)
        self._reconnect_spin.setValue(3.0)
        self._nuc_duration_spin.setValue(1.0)
        self._grab_timeout_spin.setValue(0.5)

    def _save(self) -> None:
        """Save changes to the camera configuration."""
        if not self._current_camera_id:
            return

        config = self._config_service.get_camera_config(self._current_camera_id)
        if not config:
            return

        # Preserve identity (read-only) and update editable fields
        identity = config.identity
        metadata = dict(config.metadata or {})

        # Update metadata from acquisition settings
        metadata["frame_rate"] = self._fps_spin.value()
        metadata["reconnect_interval_s"] = self._reconnect_spin.value()
        metadata["nuc_duration_s"] = self._nuc_duration_spin.value()
        metadata["grab_timeout_ms"] = int(self._grab_timeout_spin.value() * 1000)

        new_config = CameraConfig(
            identity=identity,
            name=self._name_edit.text(),
            description=self._description_edit.toPlainText(),
            enabled=self._enabled_check.isChecked(),
            thermal_enabled=self._thermal_enabled_check.isChecked(),
            visible_enabled=self._visible_enabled_check.isChecked(),
            ptz_config=config.ptz_config,
            tags=config.tags,
            metadata=metadata,
        )

        self._config_service.set_camera_config(new_config)


# --------------------------------------------------------------------------
# ROI Configuration Tab
# --------------------------------------------------------------------------


class ROIConfigurationTab(QWidget):
    """ROI configuration for all shape types."""

    def __init__(
        self,
        config_service: ConfigurationService,
        database: Database | None = None,
    ) -> None:
        super().__init__()
        self._config_service = config_service
        self._database = database
        self._selected_camera_id: str | None = None
        self._selected_position_id: str | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Position selector
        selector_group = QGroupBox("PTZ Position Selection")
        selector_form = QFormLayout(selector_group)

        self._position_combo = QComboBox()
        self._position_combo.currentTextChanged.connect(self._on_position_changed)
        selector_form.addRow("PTZ Position:", self._position_combo)

        layout.addWidget(selector_group)

        # ROI list for selected position
        list_group = QGroupBox("ROIs for Selected Position")
        list_layout = QVBoxLayout(list_group)

        self._roi_tree = QTreeWidget()
        self._roi_tree.setHeaderLabels(["ROI ID", "Name", "Shape", "Enabled", "Limits"])
        self._roi_tree.setColumnWidth(0, 120)
        self._roi_tree.setColumnWidth(1, 150)
        self._roi_tree.setColumnWidth(2, 100)
        self._roi_tree.itemSelectionChanged.connect(self._on_roi_selected)
        list_layout.addWidget(self._roi_tree)

        # ROI buttons
        roi_btn_layout = QHBoxLayout()
        self._add_roi_btn = QPushButton("Add ROI")
        self._add_roi_btn.clicked.connect(self._add_roi)
        self._edit_roi_btn = QPushButton("Edit ROI")
        self._edit_roi_btn.clicked.connect(self._edit_roi)
        self._edit_roi_btn.setEnabled(False)
        self._delete_roi_btn = QPushButton("Delete ROI")
        self._delete_roi_btn.clicked.connect(self._delete_roi)
        self._delete_roi_btn.setEnabled(False)
        roi_btn_layout.addWidget(self._add_roi_btn)
        roi_btn_layout.addWidget(self._edit_roi_btn)
        roi_btn_layout.addWidget(self._delete_roi_btn)
        roi_btn_layout.addStretch()
        list_layout.addLayout(roi_btn_layout)

        layout.addWidget(list_group, 1)

        # ROI Editor
        self._roi_editor = ROIEditorWidget(self._config_service)
        layout.addWidget(self._roi_editor)

    def set_camera(self, camera_id: str) -> None:
        """Set the camera and load its positions/ROIs."""
        self._selected_camera_id = camera_id
        self._load_positions(camera_id)
        self._load_rois(camera_id, None)

    def _on_position_changed(self, position_text: str) -> None:
        position_id = self._position_combo.currentData()
        self._selected_position_id = position_id
        self._load_rois(self._selected_camera_id or "", position_id)

    def _on_roi_selected(self) -> None:
        items = self._roi_tree.selectedItems()
        if items:
            item = items[0]
            roi_id = item.data(0, Qt.ItemDataRole.UserRole)
            self._roi_editor.load_roi(roi_id, self._selected_camera_id, self._selected_position_id)
            self._edit_roi_btn.setEnabled(True)
            self._delete_roi_btn.setEnabled(True)
        else:
            self._roi_editor.clear()
            self._edit_roi_btn.setEnabled(False)
            self._delete_roi_btn.setEnabled(False)

    def _load_positions(self, camera_id: str) -> None:
        """Load PTZ positions for the selected camera."""
        self._position_combo.clear()
        self._position_combo.addItem("Default (no position)", "default")

        if self._database:
            # TODO: Load positions from database
            pass

        # Also check analysis config for position associations
        analysis_config = self._config_service.get_analysis_config(camera_id)
        if analysis_config:
            for pos_id, assoc in analysis_config.position_associations.items():
                if pos_id != "default":
                    self._position_combo.addItem(assoc.position_name or pos_id, pos_id)

    def _load_rois(self, camera_id: str, position_id: str | None = None) -> None:
        """Load ROIs for the selected camera and position."""
        self._roi_tree.clear()
        if not camera_id:
            return

        analysis_config = self._config_service.get_analysis_config(camera_id)
        if not analysis_config:
            return

        # Determine which position to use
        pos_id = position_id or "default"
        rois = analysis_config.get_rois_for_position(pos_id)

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
                limits_text,
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, roi.roi_id)
            self._roi_tree.addTopLevelItem(item)

    def _add_roi(self) -> None:
        if not self._selected_camera_id:
            return

        from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QComboBox

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

            analysis_config = self._config_service.get_analysis_config(self._selected_camera_id)
            if not analysis_config:
                analysis_config = self._config_service.create_analysis_config(self._selected_camera_id)

            # Check for duplicate
            if roi_id in analysis_config.rois:
                QMessageBox.warning(self, "Duplicate", f"ROI '{roi_id}' already exists.")
                return

            # Create default geometry based on shape
            geometry = self._create_default_geometry(shape)
            roi_config = ROIConfig(
                roi_id=roi_id,
                name=name,
                geometry=geometry,
            )

            # Add to analysis config
            new_rois = dict(analysis_config.rois)
            new_rois[roi_id] = roi_config

            # Add to position association
            pos_id = self._selected_position_id or "default"
            new_associations = dict(analysis_config.position_associations)
            if pos_id not in new_associations:
                new_associations[pos_id] = PositionROIAssociation(
                    position_id=pos_id,
                    position_name=self._position_combo.currentText(),
                    roi_ids=(roi_id,),
                )
            else:
                assoc = new_associations[pos_id]
                new_associations[pos_id] = PositionROIAssociation(
                    position_id=assoc.position_id,
                    position_name=assoc.position_name,
                    roi_ids=assoc.roi_ids + (roi_id,),
                )

            updated_config = AnalysisConfig(
                camera_id=analysis_config.camera_id,
                rois=new_rois,
                position_associations=new_associations,
                alarm_rules=analysis_config.alarm_rules,
                default_emissivity=analysis_config.default_emissivity,
                ambient_temperature=analysis_config.ambient_temperature,
                distance=analysis_config.distance,
                humidity=analysis_config.humidity,
                reflected_temperature=analysis_config.reflected_temperature,
                unit=analysis_config.unit,
            )
            self._config_service.set_analysis_config(updated_config)
            self._load_rois(self._selected_camera_id, self._selected_position_id)

    def _edit_roi(self) -> None:
        items = self._roi_tree.selectedItems()
        if items:
            roi_id = items[0].data(0, Qt.ItemDataRole.UserRole)
            self._roi_editor.load_roi(roi_id, self._selected_camera_id, self._selected_position_id)

    def _delete_roi(self) -> None:
        items = self._roi_tree.selectedItems()
        if not items or not self._selected_camera_id:
            return

        roi_id = items[0].data(0, Qt.ItemDataRole.UserRole)

        reply = QMessageBox.question(
            self,
            "Delete ROI",
            f"Delete ROI '{roi_id}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        analysis_config = self._config_service.get_analysis_config(self._selected_camera_id)
        if not analysis_config:
            return

        new_rois = {k: v for k, v in analysis_config.rois.items() if k != roi_id}

        # Remove from position associations
        new_associations = {}
        for pos_id, assoc in analysis_config.position_associations.items():
            new_roi_ids = tuple(r for r in assoc.roi_ids if r != roi_id)
            if new_roi_ids:
                new_associations[pos_id] = PositionROIAssociation(
                    position_id=assoc.position_id,
                    position_name=assoc.position_name,
                    roi_ids=new_roi_ids,
                )

        updated_config = AnalysisConfig(
            camera_id=analysis_config.camera_id,
            rois=new_rois,
            position_associations=new_associations,
            alarm_rules=analysis_config.alarm_rules,
            default_emissivity=analysis_config.default_emissivity,
            ambient_temperature=analysis_config.ambient_temperature,
            distance=analysis_config.distance,
            humidity=analysis_config.humidity,
            reflected_temperature=analysis_config.reflected_temperature,
            unit=analysis_config.unit,
        )
        self._config_service.set_analysis_config(updated_config)
        self._load_rois(self._selected_camera_id, self._selected_position_id)

    def _create_default_geometry(self, shape: ROIShape) -> ROIGeometry:
        """Create default geometry for a shape type."""
        if shape == ROIShape.RECTANGLE1:
            return ROIGeometry(
                shape=ROIShape.RECTANGLE1,
                parameters={"y1": 100.0, "x1": 100.0, "y2": 200.0, "x2": 200.0},
            )
        elif shape == ROIShape.RECTANGLE2:
            return ROIGeometry(
                shape=ROIShape.RECTANGLE2,
                parameters={"center_y": 150.0, "center_x": 150.0, "phi": 0.0, "length1": 50.0, "length2": 50.0},
            )
        elif shape == ROIShape.CIRCLE:
            return ROIGeometry(
                shape=ROIShape.CIRCLE,
                parameters={"center_y": 150.0, "center_x": 150.0, "radius": 50.0},
            )
        elif shape == ROIShape.ELLIPSE:
            return ROIGeometry(
                shape=ROIShape.ELLIPSE,
                parameters={"center_y": 150.0, "center_x": 150.0, "phi": 0.0, "radius1": 50.0, "radius2": 30.0},
            )
        elif shape == ROIShape.POLYGON:
            return ROIGeometry(
                shape=ROIShape.POLYGON,
                parameters={"points": [(100.0, 100.0), (200.0, 100.0), (150.0, 200.0)]},
            )
        return ROIGeometry(shape=ROIShape.RECTANGLE1)

    def refresh_cameras(self) -> None:
        pass  # Handled by main widget

    def refresh_camera_rois(self, camera_id: str) -> None:
        if self._selected_camera_id == camera_id:
            self._load_rois(camera_id, self._selected_position_id)

    def refresh_all(self) -> None:
        if self._selected_camera_id:
            self._load_positions(self._selected_camera_id)
            self._load_rois(self._selected_camera_id, self._selected_position_id)


# --------------------------------------------------------------------------
# ROI Editor Widget
# --------------------------------------------------------------------------


class ROIEditorWidget(QWidget):
    """Editor for individual ROI geometry and limits.

    Uses UI coordinates (x, y) but converts to/from domain row/col at boundary.
    """

    def __init__(self, config_service: ConfigurationService) -> None:
        super().__init__()
        self._config_service = config_service
        self._current_roi_id: str | None = None
        self._current_camera_id: str | None = None
        self._current_position_id: str | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        self._group = QGroupBox("ROI Editor")
        layout = QVBoxLayout(self._group)

        # Shape info (read-only)
        shape_layout = QFormLayout()
        self._shape_label = QLabel("—")
        shape_layout.addRow("Shape:", self._shape_label)
        layout.addLayout(shape_layout)

        # Geometry parameters - dynamic based on shape
        self._geometry_stack = QWidget()
        self._geometry_layout = QVBoxLayout(self._geometry_stack)
        self._geometry_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._geometry_stack)

        # Temperature limits
        limits_group = QGroupBox("Temperature Limits")
        limits_layout = QFormLayout(limits_group)

        self._unit_combo = QComboBox()
        self._unit_combo.addItems([u.value for u in TemperatureUnit])

        self._min_warning_spin = QDoubleSpinBox()
        self._min_warning_spin.setRange(-273.15, 2000.0)
        self._min_warning_spin.setDecimals(1)
        self._min_warning_spin.setSpecialValueText("Not set")
        self._min_warning_spin.setValue(-273.15)

        self._max_warning_spin = QDoubleSpinBox()
        self._max_warning_spin.setRange(-273.15, 2000.0)
        self._max_warning_spin.setDecimals(1)
        self._max_warning_spin.setSpecialValueText("Not set")
        self._max_warning_spin.setValue(-273.15)

        self._min_critical_spin = QDoubleSpinBox()
        self._min_critical_spin.setRange(-273.15, 2000.0)
        self._min_critical_spin.setDecimals(1)
        self._min_critical_spin.setSpecialValueText("Not set")
        self._min_critical_spin.setValue(-273.15)

        self._max_critical_spin = QDoubleSpinBox()
        self._max_critical_spin.setRange(-273.15, 2000.0)
        self._max_critical_spin.setDecimals(1)
        self._max_critical_spin.setSpecialValueText("Not set")
        self._max_critical_spin.setValue(-273.15)

        self._rate_limit_spin = QDoubleSpinBox()
        self._rate_limit_spin.setRange(0.0, 1000.0)
        self._rate_limit_spin.setDecimals(1)
        self._rate_limit_spin.setSuffix(" °C/s")
        self._rate_limit_spin.setSpecialValueText("Not set")

        limits_layout.addRow("Unit:", self._unit_combo)
        limits_layout.addRow("Min Warning:", self._min_warning_spin)
        limits_layout.addRow("Max Warning:", self._max_warning_spin)
        limits_layout.addRow("Min Critical:", self._min_critical_spin)
        limits_layout.addRow("Max Critical:", self._max_critical_spin)
        limits_layout.addRow("Rate of Change:", self._rate_limit_spin)

        layout.addWidget(limits_group)

        # Alarm enabled
        self._alarm_enabled_check = QCheckBox("Enable Alarm Evaluation")
        self._alarm_enabled_check.setChecked(True)
        layout.addWidget(self._alarm_enabled_check)

        # Save button
        self._save_btn = QPushButton("Save ROI")
        self._save_btn.clicked.connect(self._save)
        self._save_btn.setEnabled(False)
        layout.addWidget(self._save_btn)

        layout.addStretch()

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self._group)

    def load_roi(self, roi_id: str, camera_id: str, position_id: str | None) -> None:
        """Load an ROI for editing."""
        analysis_config = self._config_service.get_analysis_config(camera_id)
        if not analysis_config or roi_id not in analysis_config.rois:
            self.clear()
            return

        roi = analysis_config.rois[roi_id]
        self._current_roi_id = roi_id
        self._current_camera_id = camera_id
        self._current_position_id = position_id
        self._save_btn.setEnabled(True)

        self._shape_label.setText(roi.geometry.shape.value)

        # Build geometry editor based on shape
        self._clear_geometry_editor()
        self._build_geometry_editor(roi.geometry)

        # Load limits
        limits = roi.temperature_limits
        self._unit_combo.setCurrentText(limits.unit.value)
        self._min_warning_spin.setValue(limits.min_warning if limits.min_warning is not None else -273.15)
        self._max_warning_spin.setValue(limits.max_warning if limits.max_warning is not None else -273.15)
        self._min_critical_spin.setValue(limits.min_critical if limits.min_critical is not None else -273.15)
        self._max_critical_spin.setValue(limits.max_critical if limits.max_critical is not None else -273.15)
        self._rate_limit_spin.setValue(limits.rate_of_change_limit if limits.rate_of_change_limit is not None else 0.0)
        self._alarm_enabled_check.setChecked(roi.alarm_enabled)

    def _clear_geometry_editor(self) -> None:
        while self._geometry_layout.count():
            child = self._geometry_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def _build_geometry_editor(self, geometry: ROIGeometry) -> None:
        """Build geometry parameter editors based on shape."""
        from PyQt6.QtWidgets import QFormLayout, QDoubleSpinBox, QGroupBox

        shape = geometry.shape
        params = geometry.parameters

        group = QGroupBox("Geometry (row/col coordinates)")
        form = QFormLayout(group)

        if shape == ROIShape.RECTANGLE1:
            self._y1_spin = self._make_spin(params.get("y1", 0.0))
            self._x1_spin = self._make_spin(params.get("x1", 0.0))
            self._y2_spin = self._make_spin(params.get("y2", 100.0))
            self._x2_spin = self._make_spin(params.get("x2", 100.0))
            form.addRow("Y1 (top):", self._y1_spin)
            form.addRow("X1 (left):", self._x1_spin)
            form.addRow("Y2 (bottom):", self._y2_spin)
            form.addRow("X2 (right):", self._x2_spin)

        elif shape == ROIShape.RECTANGLE2:
            self._center_y_spin = self._make_spin(params.get("center_y", 0.0))
            self._center_x_spin = self._make_spin(params.get("center_x", 0.0))
            self._phi_spin = self._make_spin(params.get("phi", 0.0), -3.14159, 3.14159, 0.001)
            self._length1_spin = self._make_spin(params.get("length1", 50.0), 0.0, 10000.0)
            self._length2_spin = self._make_spin(params.get("length2", 50.0), 0.0, 10000.0)
            form.addRow("Center Y:", self._center_y_spin)
            form.addRow("Center X:", self._center_x_spin)
            form.addRow("Phi (rad):", self._phi_spin)
            form.addRow("Length 1:", self._length1_spin)
            form.addRow("Length 2:", self._length2_spin)

        elif shape == ROIShape.CIRCLE:
            self._center_y_spin = self._make_spin(params.get("center_y", 0.0))
            self._center_x_spin = self._make_spin(params.get("center_x", 0.0))
            self._radius_spin = self._make_spin(params.get("radius", 50.0), 0.0, 10000.0)
            form.addRow("Center Y:", self._center_y_spin)
            form.addRow("Center X:", self._center_x_spin)
            form.addRow("Radius:", self._radius_spin)

        elif shape == ROIShape.ELLIPSE:
            self._center_y_spin = self._make_spin(params.get("center_y", 0.0))
            self._center_x_spin = self._make_spin(params.get("center_x", 0.0))
            self._phi_spin = self._make_spin(params.get("phi", 0.0), -3.14159, 3.14159, 0.001)
            self._radius1_spin = self._make_spin(params.get("radius1", 50.0), 0.0, 10000.0)
            self._radius2_spin = self._make_spin(params.get("radius2", 30.0), 0.0, 10000.0)
            form.addRow("Center Y:", self._center_y_spin)
            form.addRow("Center X:", self._center_x_spin)
            form.addRow("Phi (rad):", self._phi_spin)
            form.addRow("Radius 1:", self._radius1_spin)
            form.addRow("Radius 2:", self._radius2_spin)

        elif shape == ROIShape.POLYGON:
            points = params.get("points", [])
            self._polygon_text = QTextEdit()
            self._polygon_text.setMaximumHeight(100)
            self._polygon_text.setPlaceholderText("One point per line: row,col")
            point_text = "\n".join(f"{p[0]}, {p[1]}" for p in points)
            self._polygon_text.setPlainText(point_text)
            form.addRow("Points (row,col):", self._polygon_text)

        self._geometry_layout.addWidget(group)

    def _make_spin(self, value: float, min_val: float = -10000.0, max_val: float = 10000.0, step: float = 0.1) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(min_val, max_val)
        spin.setDecimals(2)
        spin.setSingleStep(step)
        spin.setValue(value)
        return spin

    def _save(self) -> None:
        if not self._current_roi_id or not self._current_camera_id:
            return

        analysis_config = self._config_service.get_analysis_config(self._current_camera_id)
        if not analysis_config or self._current_roi_id not in analysis_config.rois:
            return

        old_roi = analysis_config.rois[self._current_roi_id]

        # Build new geometry
        geometry = self._read_geometry()
        if not geometry:
            return

        # Build new limits
        limits = TemperatureLimits(
            unit=TemperatureUnit(self._unit_combo.currentText()),
            min_warning=self._min_warning_spin.value() if self._min_warning_spin.value() > -273.15 else None,
            max_warning=self._max_warning_spin.value() if self._max_warning_spin.value() > -273.15 else None,
            min_critical=self._min_critical_spin.value() if self._min_critical_spin.value() > -273.15 else None,
            max_critical=self._max_critical_spin.value() if self._max_critical_spin.value() > -273.15 else None,
            rate_of_change_limit=self._rate_limit_spin.value() if self._rate_limit_spin.value() > 0.0 else None,
        )

        new_roi = ROIConfig(
            roi_id=old_roi.roi_id,
            name=old_roi.name,
            enabled=old_roi.enabled,
            geometry=geometry,
            temperature_limits=limits,
            alarm_enabled=self._alarm_enabled_check.isChecked(),
            metadata=old_roi.metadata,
        )

        new_rois = dict(analysis_config.rois)
        new_rois[self._current_roi_id] = new_roi

        updated_config = AnalysisConfig(
            camera_id=analysis_config.camera_id,
            rois=new_rois,
            position_associations=analysis_config.position_associations,
            alarm_rules=analysis_config.alarm_rules,
            default_emissivity=analysis_config.default_emissivity,
            ambient_temperature=analysis_config.ambient_temperature,
            distance=analysis_config.distance,
            humidity=analysis_config.humidity,
            reflected_temperature=analysis_config.reflected_temperature,
            unit=analysis_config.unit,
        )
        self._config_service.set_analysis_config(updated_config)

    def _read_geometry(self) -> ROIGeometry | None:
        """Read geometry parameters from editors."""
        shape_text = self._shape_label.text()
        try:
            shape = ROIShape(shape_text)
        except ValueError:
            return None

        if shape == ROIShape.RECTANGLE1:
            return ROIGeometry(
                shape=ROIShape.RECTANGLE1,
                parameters={
                    "y1": self._y1_spin.value(),
                    "x1": self._x1_spin.value(),
                    "y2": self._y2_spin.value(),
                    "x2": self._x2_spin.value(),
                },
            )
        elif shape == ROIShape.RECTANGLE2:
            return ROIGeometry(
                shape=ROIShape.RECTANGLE2,
                parameters={
                    "center_y": self._center_y_spin.value(),
                    "center_x": self._center_x_spin.value(),
                    "phi": self._phi_spin.value(),
                    "length1": self._length1_spin.value(),
                    "length2": self._length2_spin.value(),
                },
            )
        elif shape == ROIShape.CIRCLE:
            return ROIGeometry(
                shape=ROIShape.CIRCLE,
                parameters={
                    "center_y": self._center_y_spin.value(),
                    "center_x": self._center_x_spin.value(),
                    "radius": self._radius_spin.value(),
                },
            )
        elif shape == ROIShape.ELLIPSE:
            return ROIGeometry(
                shape=ROIShape.ELLIPSE,
                parameters={
                    "center_y": self._center_y_spin.value(),
                    "center_x": self._center_x_spin.value(),
                    "phi": self._phi_spin.value(),
                    "radius1": self._radius1_spin.value(),
                    "radius2": self._radius2_spin.value(),
                },
            )
        elif shape == ROIShape.POLYGON:
            text = self._polygon_text.toPlainText().strip()
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
                return None
            return ROIGeometry(
                shape=ROIShape.POLYGON,
                parameters={"points": points},
            )
        return None

    def clear(self) -> None:
        self._current_roi_id = None
        self._current_camera_id = None
        self._current_position_id = None
        self._save_btn.setEnabled(False)
        self._shape_label.setText("—")
        self._clear_geometry_editor()
        self._unit_combo.setCurrentIndex(0)
        self._min_warning_spin.setValue(-273.15)
        self._max_warning_spin.setValue(-273.15)
        self._min_critical_spin.setValue(-273.15)
        self._max_critical_spin.setValue(-273.15)
        self._rate_limit_spin.setValue(0.0)
        self._alarm_enabled_check.setChecked(True)


# --------------------------------------------------------------------------
# PTZ Configuration Tab
# --------------------------------------------------------------------------


class PTZConfigurationTab(QWidget):
    """PTZ position configuration for a single camera."""

    def __init__(
        self,
        config_service: ConfigurationService,
        database: Database | None = None,
    ) -> None:
        super().__init__()
        self._config_service = config_service
        self._database = database
        self._selected_camera_id: str | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        label = QLabel("PTZ Configuration - Select a camera to configure PTZ positions")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: #888; font-size: 14px;")
        layout.addWidget(label, 1)

    def set_camera(self, camera_id: str) -> None:
        self._selected_camera_id = camera_id
        # TODO: Implement PTZ configuration UI

    def refresh_cameras(self) -> None:
        pass

    def refresh_all(self) -> None:
        pass


# --------------------------------------------------------------------------
# Alarm Configuration Tab
# --------------------------------------------------------------------------


class AlarmConfigurationTab(QWidget):
    """Alarm configuration for a single camera."""

    def __init__(
        self,
        config_service: ConfigurationService,
        database: Database | None = None,
    ) -> None:
        super().__init__()
        self._config_service = config_service
        self._database = database
        self._selected_camera_id: str | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        label = QLabel("Alarm Configuration - Select a camera to configure alarms")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: #888; font-size: 14px;")
        layout.addWidget(label, 1)

    def set_camera(self, camera_id: str) -> None:
        self._selected_camera_id = camera_id
        # TODO: Implement alarm configuration UI

    def refresh_cameras(self) -> None:
        pass

    def refresh_camera_alarms(self, camera_id: str) -> None:
        pass

    def refresh_all(self) -> None:
        pass


# --------------------------------------------------------------------------
# Recording Configuration Tab
# --------------------------------------------------------------------------


class RecordingConfigurationTab(QWidget):
    """Recording configuration for a single camera."""

    def __init__(
        self,
        config_service: ConfigurationService,
        database: Database | None = None,
    ) -> None:
        super().__init__()
        self._config_service = config_service
        self._database = database
        self._selected_camera_id: str | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        label = QLabel("Recording Configuration - Select a camera to configure recording")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: #888; font-size: 14px;")
        layout.addWidget(label, 1)

    def set_camera(self, camera_id: str) -> None:
        self._selected_camera_id = camera_id
        # TODO: Implement recording configuration UI

    def refresh_cameras(self) -> None:
        pass

    def refresh_camera(self, camera_id: str) -> None:
        pass

    def refresh_all(self) -> None:
        pass


# --------------------------------------------------------------------------
# Calibration Information Tab
# --------------------------------------------------------------------------


class CalibrationInformationTab(QWidget):
    """Calibration information for a single camera."""

    def __init__(
        self,
        config_service: ConfigurationService,
        database: Database | None = None,
    ) -> None:
        super().__init__()
        self._config_service = config_service
        self._database = database
        self._selected_camera_id: str | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        label = QLabel("Calibration Information - Select a camera to view calibration")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: #888; font-size: 14px;")
        layout.addWidget(label, 1)

    def set_camera(self, camera_id: str) -> None:
        self._selected_camera_id = camera_id
        # TODO: Implement calibration info UI

    def refresh_cameras(self) -> None:
        pass

    def refresh_all(self) -> None:
        pass


# --------------------------------------------------------------------------
# System Configuration Tab
# --------------------------------------------------------------------------


class SystemConfigurationTab(QWidget):
    """System-wide configuration."""

    def __init__(
        self,
        config_service: ConfigurationService,
        database: Database | None = None,
    ) -> None:
        super().__init__()
        self._config_service = config_service
        self._database = database
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        label = QLabel("System Configuration")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: #888; font-size: 14px;")
        layout.addWidget(label, 1)

    def refresh(self) -> None:
        pass

    def refresh_all(self) -> None:
        pass


__all__ = ["ConfigurationModeWidget"]