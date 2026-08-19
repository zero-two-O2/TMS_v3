"""
ui.modes.configuration -- Configuration mode widget.

Provides the UI for all configuration aspects:
- Camera configuration (list + detail view for up to 8 cameras)
- ROI configuration (all shapes: Rectangle1, Rectangle2, Circle, Ellipse, Polygon)
- PTZ position configuration
- Alarm configuration
- Recording configuration
- Calibration information
- System configuration
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QTabWidget,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
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
)

from thermal_monitor.core.modes import ApplicationMode
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.configuration import ConfigurationService
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
    """Main widget for Configuration mode."""

    def __init__(
        self,
        config_service: ConfigurationService,
        mode_service: ModeService,
        database: Database | None = None,
    ) -> None:
        super().__init__()

        self._config_service = config_service
        self._mode_service = mode_service
        self._database = database

        self._setup_ui()
        self._connect_signals()
        self._load_initial_data()

    def _setup_ui(self) -> None:
        """Set up the UI layout."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Main tab widget for configuration categories
        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)

        # Create configuration tabs
        self._camera_tab = CameraConfigurationTab(self._config_service, self._database)
        self._roi_tab = ROIConfigurationTab(self._config_service, self._database)
        self._ptz_tab = PTZConfigurationTab(self._config_service, self._database)
        self._alarm_tab = AlarmConfigurationTab(self._config_service, self._database)
        self._recording_tab = RecordingConfigurationTab(self._config_service, self._database)
        self._calibration_tab = CalibrationInformationTab(self._config_service, self._database)
        self._system_tab = SystemConfigurationTab(self._config_service, self._database)

        self._tabs.addTab(self._camera_tab, "Cameras")
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
        # Tabs load their own data
        pass

    def _on_camera_config_changed(self, camera_id: str, config: CameraConfig) -> None:
        self._camera_tab.refresh_camera(camera_id)
        self._roi_tab.refresh_cameras()
        self._ptz_tab.refresh_cameras()
        self._alarm_tab.refresh_cameras()
        self._recording_tab.refresh_cameras()

    def _on_analysis_config_changed(self, camera_id: str, config: AnalysisConfig) -> None:
        self._roi_tab.refresh_camera_rois(camera_id)
        self._alarm_tab.refresh_camera_alarms(camera_id)

    def _on_recording_config_changed(self, camera_id: str, config: RecordingConfig) -> None:
        self._recording_tab.refresh_camera(camera_id)

    def _on_system_config_changed(self, config: SystemConfig) -> None:
        self._system_tab.refresh()

    def on_mode_activated(self) -> None:
        """Called when configuration mode becomes active."""
        self._camera_tab.refresh_all()
        self._roi_tab.refresh_all()
        self._ptz_tab.refresh_all()
        self._alarm_tab.refresh_all()
        self._recording_tab.refresh_all()
        self._calibration_tab.refresh_all()
        self._system_tab.refresh()

    def show_camera_config(self) -> None:
        self._tabs.setCurrentWidget(self._camera_tab)

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
# Camera Configuration Tab
# --------------------------------------------------------------------------


class CameraConfigurationTab(QWidget):
    """Camera configuration with list + detail view for up to 8 cameras."""

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

        # Horizontal splitter: camera list | detail
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        # Left: Camera list
        list_widget = QWidget()
        list_layout = QVBoxLayout(list_widget)
        list_layout.setContentsMargins(0, 0, 0, 0)

        self._camera_tree = QTreeWidget()
        self._camera_tree.setHeaderLabels(["Camera", "Serial", "Status"])
        self._camera_tree.setColumnWidth(0, 150)
        self._camera_tree.setColumnWidth(1, 150)
        self._camera_tree.itemSelectionChanged.connect(self._on_camera_selected)
        list_layout.addWidget(self._camera_tree)

        # Camera list buttons
        button_layout = QVBoxLayout()
        self._add_camera_btn = QPushButton("Add Camera")
        self._add_camera_btn.clicked.connect(self._add_camera)
        self._remove_camera_btn = QPushButton("Remove Camera")
        self._remove_camera_btn.clicked.connect(self._remove_camera)
        self._remove_camera_btn.setEnabled(False)
        button_layout.addWidget(self._add_camera_btn)
        button_layout.addWidget(self._remove_camera_btn)
        button_layout.addStretch()
        list_layout.addLayout(button_layout)

        splitter.addWidget(list_widget)

        # Right: Camera detail
        self._detail_widget = CameraDetailWidget(self._config_service)
        splitter.addWidget(self._detail_widget)

        splitter.setSizes([300, 600])

    def _on_camera_selected(self) -> None:
        items = self._camera_tree.selectedItems()
        if items:
            item = items[0]
            self._selected_camera_id = item.data(0, Qt.ItemDataRole.UserRole)
            self._detail_widget.set_camera(self._selected_camera_id)
            self._remove_camera_btn.setEnabled(True)
        else:
            self._selected_camera_id = None
            self._detail_widget.clear()
            self._remove_camera_btn.setEnabled(False)

    def _add_camera(self) -> None:
        """Add a new camera configuration."""
        from thermal_monitor.core.models import CameraIdentity, PTZConfig

        # Simple dialog for camera ID and serial
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLineEdit

        dialog = QDialog(self)
        dialog.setWindowTitle("Add Camera")
        layout = QFormLayout(dialog)

        camera_id_edit = QLineEdit()
        camera_id_edit.setPlaceholderText("e.g., cam_001")
        serial_edit = QLineEdit()
        serial_edit.setPlaceholderText("e.g., SN123456")
        ip_edit = QLineEdit()
        ip_edit.setPlaceholderText("e.g., 192.168.1.100")

        layout.addRow("Camera ID:", camera_id_edit)
        layout.addRow("Serial Number:", serial_edit)
        layout.addRow("IP Address:", ip_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            camera_id = camera_id_edit.text().strip()
            serial = serial_edit.text().strip()
            ip = ip_edit.text().strip()

            if not camera_id or not serial:
                QMessageBox.warning(self, "Invalid Input", "Camera ID and Serial Number are required.")
                return

            if self._config_service.get_camera_config(camera_id):
                QMessageBox.warning(self, "Duplicate", f"Camera '{camera_id}' already exists.")
                return

            identity = CameraIdentity(camera_id=camera_id, serial_number=serial)
            config = self._config_service.create_camera_config(identity=identity, name=ip)
            self._config_service.set_camera_config(config)
            self.refresh_all()

    def _remove_camera(self) -> None:
        if self._selected_camera_id:
            reply = QMessageBox.question(
                self,
                "Remove Camera",
                f"Remove camera '{self._selected_camera_id}' and all associated configuration?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._config_service.remove_camera_config(self._selected_camera_id)
                self._config_service.remove_analysis_config(self._selected_camera_id)
                self._config_service.remove_recording_config(self._selected_camera_id)
                self.refresh_all()

    def refresh_camera(self, camera_id: str) -> None:
        """Refresh a specific camera in the list."""
        self.refresh_all()

    def refresh_all(self) -> None:
        """Refresh the entire camera list."""
        self._camera_tree.clear()
        for config in self._config_service.get_all_camera_configs():
            item = QTreeWidgetItem([
                config.name or config.identity.camera_id,
                config.identity.serial_number,
                "Enabled" if config.enabled else "Disabled",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, config.identity.camera_id)
            self._camera_tree.addTopLevelItem(item)

        if self._selected_camera_id:
            config = self._config_service.get_camera_config(self._selected_camera_id)
            if config:
                self._detail_widget.set_camera(self._selected_camera_id)


class CameraDetailWidget(QWidget):
    """Detail view for a single camera configuration."""

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

        # Identity group
        self._identity_group = QGroupBox("Identity")
        identity_layout = QFormLayout(self._identity_group)
        self._camera_id_edit = QLineEdit()
        self._camera_id_edit.setReadOnly(True)
        self._serial_edit = QLineEdit()
        self._model_edit = QLineEdit()
        self._vendor_edit = QLineEdit()
        self._firmware_edit = QLineEdit()
        self._user_name_edit = QLineEdit()
        identity_layout.addRow("Camera ID:", self._camera_id_edit)
        identity_layout.addRow("Serial Number:", self._serial_edit)
        identity_layout.addRow("Model:", self._model_edit)
        identity_layout.addRow("Vendor:", self._vendor_edit)
        identity_layout.addRow("Firmware:", self._firmware_edit)
        identity_layout.addRow("User Name:", self._user_name_edit)
        self._layout.addWidget(self._identity_group)

        # General settings
        self._general_group = QGroupBox("General Settings")
        general_layout = QFormLayout(self._general_group)
        self._name_edit = QLineEdit()
        self._description_edit = QTextEdit()
        self._description_edit.setMaximumHeight(60)
        self._enabled_check = QCheckBox()
        self._thermal_enabled_check = QCheckBox()
        self._visible_enabled_check = QCheckBox()
        general_layout.addRow("Name:", self._name_edit)
        general_layout.addRow("Description:", self._description_edit)
        general_layout.addRow("Enabled:", self._enabled_check)
        general_layout.addRow("Thermal Stream:", self._thermal_enabled_check)
        general_layout.addRow("Visible Stream:", self._visible_enabled_check)
        self._layout.addWidget(self._general_group)

        # Acquisition settings
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

        # Display settings
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

        # Calibration association
        self._cal_group = QGroupBox("Calibration")
        cal_layout = QFormLayout(self._cal_group)
        self._calibration_combo = QComboBox()
        self._calibration_combo.addItem("None", None)
        cal_layout.addRow("Calibration:", self._calibration_combo)
        self._layout.addWidget(self._cal_group)

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
        self._camera_id_edit.setText(identity.camera_id)
        self._serial_edit.setText(identity.serial_number)
        self._model_edit.setText(identity.model)
        self._vendor_edit.setText(identity.vendor)
        self._firmware_edit.setText(identity.firmware)
        self._user_name_edit.setText(identity.user_name)

        self._name_edit.setText(config.name)
        self._description_edit.setPlainText(config.description)
        self._enabled_check.setChecked(config.enabled)
        self._thermal_enabled_check.setChecked(config.thermal_enabled)
        self._visible_enabled_check.setChecked(config.visible_enabled)

    def clear(self) -> None:
        """Clear the detail view."""
        self._current_camera_id = None
        self._save_btn.setEnabled(False)
        self._camera_id_edit.clear()
        self._serial_edit.clear()
        self._model_edit.clear()
        self._vendor_edit.clear()
        self._firmware_edit.clear()
        self._user_name_edit.clear()
        self._name_edit.clear()
        self._description_edit.clear()
        self._enabled_check.setChecked(False)
        self._thermal_enabled_check.setChecked(False)
        self._visible_enabled_check.setChecked(False)

    def _save(self) -> None:
        """Save changes to the camera configuration."""
        if not self._current_camera_id:
            return

        config = self._config_service.get_camera_config(self._current_camera_id)
        if not config:
            return

        # Create updated identity
        identity = CameraIdentity(
            camera_id=self._camera_id_edit.text(),
            serial_number=self._serial_edit.text(),
            model=self._model_edit.text(),
            vendor=self._vendor_edit.text(),
            firmware=self._firmware_edit.text(),
            user_name=self._user_name_edit.text(),
        )

        # Create updated config
        new_config = CameraConfig(
            identity=identity,
            name=self._name_edit.text(),
            description=self._description_edit.toPlainText(),
            enabled=self._enabled_check.isChecked(),
            thermal_enabled=self._thermal_enabled_check.isChecked(),
            visible_enabled=self._visible_enabled_check.isChecked(),
            ptz_config=config.ptz_config,
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

        # Top: Camera and Position selectors
        selector_layout = QVBoxLayout()
        selector_group = QGroupBox("Camera / Position Selection")
        selector_form = QFormLayout(selector_group)

        self._camera_combo = QComboBox()
        self._camera_combo.currentTextChanged.connect(self._on_camera_changed)
        selector_form.addRow("Camera:", self._camera_combo)

        self._position_combo = QComboBox()
        self._position_combo.currentTextChanged.connect(self._on_position_changed)
        selector_form.addRow("PTZ Position:", self._position_combo)

        selector_layout.addWidget(selector_group)
        layout.addLayout(selector_layout)

        # Middle: ROI list for selected position
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
        roi_btn_layout = QVBoxLayout()
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

        # Bottom: ROI Editor
        self._roi_editor = ROIEditorWidget(self._config_service)
        layout.addWidget(self._roi_editor)

    def _on_camera_changed(self, camera_text: str) -> None:
        camera_id = self._camera_combo.currentData()
        if camera_id:
            self._selected_camera_id = camera_id
            self._load_positions(camera_id)
            self._load_rois(camera_id)
        else:
            self._selected_camera_id = None
            self._position_combo.clear()
            self._roi_tree.clear()

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
        self._camera_combo.clear()
        for config in self._config_service.get_all_camera_configs():
            self._camera_combo.addItem(config.name or config.identity.camera_id, config.identity.camera_id)

    def refresh_camera_rois(self, camera_id: str) -> None:
        if self._selected_camera_id == camera_id:
            self._load_rois(camera_id, self._selected_position_id)

    def refresh_all(self) -> None:
        self.refresh_cameras()
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
    """PTZ position configuration per camera."""

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

        # Camera selector
        selector_group = QGroupBox("Camera Selection")
        selector_layout = QFormLayout(selector_group)
        self._camera_combo = QComboBox()
        self._camera_combo.currentTextChanged.connect(self._on_camera_changed)
        selector_layout.addRow("Camera:", self._camera_combo)
        layout.addWidget(selector_group)

        # PTZ limits
        limits_group = QGroupBox("PTZ Limits")
        limits_layout = QFormLayout(limits_group)
        self._min_pan_spin = QDoubleSpinBox()
        self._min_pan_spin.setRange(-360.0, 360.0)
        self._min_pan_spin.setValue(-170.0)
        self._max_pan_spin = QDoubleSpinBox()
        self._max_pan_spin.setRange(-360.0, 360.0)
        self._max_pan_spin.setValue(170.0)
        self._min_tilt_spin = QDoubleSpinBox()
        self._min_tilt_spin.setRange(-90.0, 90.0)
        self._min_tilt_spin.setValue(-90.0)
        self._max_tilt_spin = QDoubleSpinBox()
        self._max_tilt_spin.setRange(-90.0, 90.0)
        self._max_tilt_spin.setValue(90.0)
        self._min_zoom_spin = QDoubleSpinBox()
        self._min_zoom_spin.setRange(1.0, 100.0)
        self._min_zoom_spin.setValue(1.0)
        self._max_zoom_spin = QDoubleSpinBox()
        self._max_zoom_spin.setRange(1.0, 100.0)
        self._max_zoom_spin.setValue(30.0)
        limits_layout.addRow("Min Pan:", self._min_pan_spin)
        limits_layout.addRow("Max Pan:", self._max_pan_spin)
        limits_layout.addRow("Min Tilt:", self._min_tilt_spin)
        limits_layout.addRow("Max Tilt:", self._max_tilt_spin)
        limits_layout.addRow("Min Zoom:", self._min_zoom_spin)
        limits_layout.addRow("Max Zoom:", self._max_zoom_spin)
        layout.addWidget(limits_group)

        # Preset positions
        preset_group = QGroupBox("Preset Positions")
        preset_layout = QVBoxLayout(preset_group)

        self._preset_tree = QTreeWidget()
        self._preset_tree.setHeaderLabels(["Preset ID", "Name", "Pan", "Tilt", "Zoom"])
        self._preset_tree.setColumnWidth(0, 80)
        self._preset_tree.setColumnWidth(1, 150)
        preset_layout.addWidget(self._preset_tree)

        preset_btn_layout = QVBoxLayout()
        self._add_preset_btn = QPushButton("Add Preset")
        self._add_preset_btn.clicked.connect(self._add_preset)
        self._edit_preset_btn = QPushButton("Edit Preset")
        self._edit_preset_btn.clicked.connect(self._edit_preset)
        self._edit_preset_btn.setEnabled(False)
        self._delete_preset_btn = QPushButton("Delete Preset")
        self._delete_preset_btn.clicked.connect(self._delete_preset)
        self._delete_preset_btn.setEnabled(False)
        preset_btn_layout.addWidget(self._add_preset_btn)
        preset_btn_layout.addWidget(self._edit_preset_btn)
        preset_btn_layout.addWidget(self._delete_preset_btn)
        preset_btn_layout.addStretch()
        preset_layout.addLayout(preset_btn_layout)

        layout.addWidget(preset_group, 1)

        # PTZ speeds
        speed_group = QGroupBox("PTZ Speeds")
        speed_layout = QFormLayout(speed_group)
        self._speed_pan_spin = QDoubleSpinBox()
        self._speed_pan_spin.setRange(0.1, 100.0)
        self._speed_pan_spin.setValue(10.0)
        self._speed_pan_spin.setSuffix(" °/s")
        self._speed_tilt_spin = QDoubleSpinBox()
        self._speed_tilt_spin.setRange(0.1, 100.0)
        self._speed_tilt_spin.setValue(10.0)
        self._speed_tilt_spin.setSuffix(" °/s")
        self._speed_zoom_spin = QDoubleSpinBox()
        self._speed_zoom_spin.setRange(0.1, 100.0)
        self._speed_zoom_spin.setValue(5.0)
        self._speed_zoom_spin.setSuffix(" x/s")
        speed_layout.addRow("Pan Speed:", self._speed_pan_spin)
        speed_layout.addRow("Tilt Speed:", self._speed_tilt_spin)
        speed_layout.addRow("Zoom Speed:", self._speed_zoom_spin)
        layout.addWidget(speed_group)

        # Save button
        self._save_btn = QPushButton("Save PTZ Configuration")
        self._save_btn.clicked.connect(self._save)
        self._save_btn.setEnabled(False)
        layout.addWidget(self._save_btn)

    def _on_camera_changed(self, text: str) -> None:
        camera_id = self._camera_combo.currentData()
        if camera_id:
            self._selected_camera_id = camera_id
            self._load_ptz_config(camera_id)
            self._save_btn.setEnabled(True)
        else:
            self._selected_camera_id = None
            self._clear_form()
            self._save_btn.setEnabled(False)

    def _load_ptz_config(self, camera_id: str) -> None:
        config = self._config_service.get_camera_config(camera_id)
        if not config:
            return

        ptz = config.ptz_config
        self._min_pan_spin.setValue(ptz.limits.min_pan)
        self._max_pan_spin.setValue(ptz.limits.max_pan)
        self._min_tilt_spin.setValue(ptz.limits.min_tilt)
        self._max_tilt_spin.setValue(ptz.limits.max_tilt)
        self._min_zoom_spin.setValue(ptz.limits.min_zoom)
        self._max_zoom_spin.setValue(ptz.limits.max_zoom)
        self._speed_pan_spin.setValue(ptz.speed_pan)
        self._speed_tilt_spin.setValue(ptz.speed_tilt)
        self._speed_zoom_spin.setValue(ptz.speed_zoom)

        self._preset_tree.clear()
        for preset_id, pos in ptz.preset_positions.items():
            item = QTreeWidgetItem([
                str(preset_id),
                pos.name,
                f"{pos.pan:.1f}",
                f"{pos.tilt:.1f}",
                f"{pos.zoom:.1f}",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, preset_id)
            self._preset_tree.addTopLevelItem(item)

    def _clear_form(self) -> None:
        self._min_pan_spin.setValue(-170.0)
        self._max_pan_spin.setValue(170.0)
        self._min_tilt_spin.setValue(-90.0)
        self._max_tilt_spin.setValue(90.0)
        self._min_zoom_spin.setValue(1.0)
        self._max_zoom_spin.setValue(30.0)
        self._speed_pan_spin.setValue(10.0)
        self._speed_tilt_spin.setValue(10.0)
        self._speed_zoom_spin.setValue(5.0)
        self._preset_tree.clear()

    def _add_preset(self) -> None:
        if not self._selected_camera_id:
            return

        from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QSpinBox, QDoubleSpinBox

        dialog = QDialog(self)
        dialog.setWindowTitle("Add PTZ Preset")
        layout = QFormLayout(dialog)

        preset_id_spin = QSpinBox()
        preset_id_spin.setRange(1, 255)
        name_edit = QLineEdit()
        pan_spin = QDoubleSpinBox()
        pan_spin.setRange(-360.0, 360.0)
        tilt_spin = QDoubleSpinBox()
        tilt_spin.setRange(-90.0, 90.0)
        zoom_spin = QDoubleSpinBox()
        zoom_spin.setRange(1.0, 100.0)
        zoom_spin.setValue(1.0)

        layout.addRow("Preset ID:", preset_id_spin)
        layout.addRow("Name:", name_edit)
        layout.addRow("Pan:", pan_spin)
        layout.addRow("Tilt:", tilt_spin)
        layout.addRow("Zoom:", zoom_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            preset_id = preset_id_spin.value()
            config = self._config_service.get_camera_config(self._selected_camera_id)
            if config and config.ptz_config.get_preset(preset_id):
                QMessageBox.warning(self, "Duplicate", f"Preset {preset_id} already exists.")
                return

            position = PTZPosition(
                pan=pan_spin.value(),
                tilt=tilt_spin.value(),
                zoom=zoom_spin.value(),
                name=name_edit.text(),
                preset_id=preset_id,
            )

            new_config = config.ptz_config.with_preset(preset_id, position)
            updated_config = CameraConfig(
                identity=config.identity,
                name=config.name,
                description=config.description,
                enabled=config.enabled,
                thermal_enabled=config.thermal_enabled,
                visible_enabled=config.visible_enabled,
                ptz_config=new_config,
            )
            self._config_service.set_camera_config(updated_config)
            self._load_ptz_config(self._selected_camera_id)

    def _edit_preset(self) -> None:
        items = self._preset_tree.selectedItems()
        if not items or not self._selected_camera_id:
            return

        preset_id = items[0].data(0, Qt.ItemDataRole.UserRole)
        config = self._config_service.get_camera_config(self._selected_camera_id)
        if not config:
            return

        position = config.ptz_config.get_preset(preset_id)
        if not position:
            return

        from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QDoubleSpinBox

        dialog = QDialog(self)
        dialog.setWindowTitle("Edit PTZ Preset")
        layout = QFormLayout(dialog)

        name_edit = QLineEdit(position.name)
        pan_spin = QDoubleSpinBox()
        pan_spin.setRange(-360.0, 360.0)
        pan_spin.setValue(position.pan)
        tilt_spin = QDoubleSpinBox()
        tilt_spin.setRange(-90.0, 90.0)
        tilt_spin.setValue(position.tilt)
        zoom_spin = QDoubleSpinBox()
        zoom_spin.setRange(1.0, 100.0)
        zoom_spin.setValue(position.zoom)

        layout.addRow("Name:", name_edit)
        layout.addRow("Pan:", pan_spin)
        layout.addRow("Tilt:", tilt_spin)
        layout.addRow("Zoom:", zoom_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_position = PTZPosition(
                pan=pan_spin.value(),
                tilt=tilt_spin.value(),
                zoom=zoom_spin.value(),
                name=name_edit.text(),
                preset_id=preset_id,
            )

            new_config = config.ptz_config.with_preset(preset_id, new_position)
            updated_config = CameraConfig(
                identity=config.identity,
                name=config.name,
                description=config.description,
                enabled=config.enabled,
                thermal_enabled=config.thermal_enabled,
                visible_enabled=config.visible_enabled,
                ptz_config=new_config,
            )
            self._config_service.set_camera_config(updated_config)
            self._load_ptz_config(self._selected_camera_id)

    def _delete_preset(self) -> None:
        items = self._preset_tree.selectedItems()
        if not items or not self._selected_camera_id:
            return

        preset_id = items[0].data(0, Qt.ItemDataRole.UserRole)

        reply = QMessageBox.question(
            self,
            "Delete Preset",
            f"Delete preset {preset_id}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Note: Need to implement removal in PTZConfig
        # For now, just warn
        QMessageBox.information(self, "Not Implemented", "Preset removal not yet implemented.")

    def _save(self) -> None:
        if not self._selected_camera_id:
            return

        config = self._config_service.get_camera_config(self._selected_camera_id)
        if not config:
            return

        limits = PTZLimits(
            min_pan=self._min_pan_spin.value(),
            max_pan=self._max_pan_spin.value(),
            min_tilt=self._min_tilt_spin.value(),
            max_tilt=self._max_tilt_spin.value(),
            min_zoom=self._min_zoom_spin.value(),
            max_zoom=self._max_zoom_spin.value(),
        )

        ptz_config = PTZConfig(
            limits=limits,
            default_position=config.ptz_config.default_position,
            preset_positions=config.ptz_config.preset_positions,
            mode=config.ptz_config.mode,
            speed_pan=self._speed_pan_spin.value(),
            speed_tilt=self._speed_tilt_spin.value(),
            speed_zoom=self._speed_zoom_spin.value(),
        )

        updated_config = CameraConfig(
            identity=config.identity,
            name=config.name,
            description=config.description,
            enabled=config.enabled,
            thermal_enabled=config.thermal_enabled,
            visible_enabled=config.visible_enabled,
            ptz_config=ptz_config,
        )
        self._config_service.set_camera_config(updated_config)

    def refresh_cameras(self) -> None:
        self._camera_combo.clear()
        for config in self._config_service.get_all_camera_configs():
            self._camera_combo.addItem(config.name or config.identity.camera_id, config.identity.camera_id)

    def refresh_all(self) -> None:
        self.refresh_cameras()
        if self._selected_camera_id:
            self._load_ptz_config(self._selected_camera_id)


# --------------------------------------------------------------------------
# Alarm Configuration Tab
# --------------------------------------------------------------------------


class AlarmConfigurationTab(QWidget):
    """Alarm rules configuration per camera and ROI."""

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

        # Camera selector
        selector_group = QGroupBox("Camera Selection")
        selector_layout = QFormLayout(selector_group)
        self._camera_combo = QComboBox()
        self._camera_combo.currentTextChanged.connect(self._on_camera_changed)
        selector_layout.addRow("Camera:", self._camera_combo)
        layout.addWidget(selector_group)

        # Alarm rules list
        list_group = QGroupBox("Alarm Rules")
        list_layout = QVBoxLayout(list_group)

        self._alarm_tree = QTreeWidget()
        self._alarm_tree.setHeaderLabels(["Rule ID", "ROI", "Condition", "Severity", "Threshold", "Enabled"])
        self._alarm_tree.setColumnWidth(0, 120)
        self._alarm_tree.setColumnWidth(1, 150)
        self._alarm_tree.setColumnWidth(2, 120)
        self._alarm_tree.setColumnWidth(3, 100)
        self._alarm_tree.setColumnWidth(4, 120)
        self._alarm_tree.itemSelectionChanged.connect(self._on_alarm_selected)
        list_layout.addWidget(self._alarm_tree)

        alarm_btn_layout = QVBoxLayout()
        self._add_alarm_btn = QPushButton("Add Alarm Rule")
        self._add_alarm_btn.clicked.connect(self._add_alarm)
        self._edit_alarm_btn = QPushButton("Edit Alarm Rule")
        self._edit_alarm_btn.clicked.connect(self._edit_alarm)
        self._edit_alarm_btn.setEnabled(False)
        self._delete_alarm_btn = QPushButton("Delete Alarm Rule")
        self._delete_alarm_btn.clicked.connect(self._delete_alarm)
        self._delete_alarm_btn.setEnabled(False)
        alarm_btn_layout.addWidget(self._add_alarm_btn)
        alarm_btn_layout.addWidget(self._edit_alarm_btn)
        alarm_btn_layout.addWidget(self._delete_alarm_btn)
        alarm_btn_layout.addStretch()
        list_layout.addLayout(alarm_btn_layout)

        layout.addWidget(list_group, 1)

        # Alarm rule editor
        self._alarm_editor = AlarmRuleEditorWidget(self._config_service)
        layout.addWidget(self._alarm_editor)

    def _on_camera_changed(self, text: str) -> None:
        camera_id = self._camera_combo.currentData()
        if camera_id:
            self._selected_camera_id = camera_id
            self._load_alarms(camera_id)
        else:
            self._selected_camera_id = None
            self._alarm_tree.clear()
            self._alarm_editor.clear()

    def _on_alarm_selected(self) -> None:
        items = self._alarm_tree.selectedItems()
        if items:
            item = items[0]
            rule_id = item.data(0, Qt.ItemDataRole.UserRole)
            self._alarm_editor.load_rule(rule_id, self._selected_camera_id)
            self._edit_alarm_btn.setEnabled(True)
            self._delete_alarm_btn.setEnabled(True)
        else:
            self._alarm_editor.clear()
            self._edit_alarm_btn.setEnabled(False)
            self._delete_alarm_btn.setEnabled(False)

    def _load_alarms(self, camera_id: str) -> None:
        self._alarm_tree.clear()
        analysis_config = self._config_service.get_analysis_config(camera_id)
        if not analysis_config:
            return

        for rule in analysis_config.alarm_rules.values():
            roi_name = analysis_config.rois.get(rule.roi_id, type('obj', (object,), {'name': rule.roi_id})()).name
            threshold_text = str(rule.threshold)
            if rule.condition in (AlarmCondition.OUTSIDE_RANGE, AlarmCondition.INSIDE_RANGE):
                threshold_text = f"{rule.threshold_low} - {rule.threshold_high}"

            item = QTreeWidgetItem([
                rule.rule_id,
                roi_name,
                rule.condition.value,
                rule.severity.value,
                threshold_text,
                "Yes" if rule.enabled else "No",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, rule.rule_id)
            self._alarm_tree.addTopLevelItem(item)

    def _add_alarm(self) -> None:
        if not self._selected_camera_id:
            return

        analysis_config = self._config_service.get_analysis_config(self._selected_camera_id)
        if not analysis_config or not analysis_config.rois:
            QMessageBox.warning(self, "No ROIs", "No ROIs configured for this camera.")
            return

        from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QComboBox, QDoubleSpinBox

        dialog = QDialog(self)
        dialog.setWindowTitle("Add Alarm Rule")
        layout = QFormLayout(dialog)

        rule_id_edit = QLineEdit()
        rule_id_edit.setPlaceholderText("e.g., alarm_001")
        roi_combo = QComboBox()
        for roi_id, roi in analysis_config.rois.items():
            roi_combo.addItem(roi.name or roi_id, roi_id)
        condition_combo = QComboBox()
        condition_combo.addItems([c.value for c in AlarmCondition])
        severity_combo = QComboBox()
        severity_combo.addItems([s.value for s in AlarmSeverity])
        threshold_spin = QDoubleSpinBox()
        threshold_spin.setRange(-273.15, 2000.0)
        threshold_spin.setDecimals(1)
        threshold_low_spin = QDoubleSpinBox()
        threshold_low_spin.setRange(-273.15, 2000.0)
        threshold_low_spin.setDecimals(1)
        threshold_high_spin = QDoubleSpinBox()
        threshold_high_spin.setRange(-273.15, 2000.0)
        threshold_high_spin.setDecimals(1)
        unit_combo = QComboBox()
        unit_combo.addItems([u.value for u in TemperatureUnit])
        enabled_check = QCheckBox()
        enabled_check.setChecked(True)
        desc_edit = QLineEdit()

        layout.addRow("Rule ID:", rule_id_edit)
        layout.addRow("ROI:", roi_combo)
        layout.addRow("Condition:", condition_combo)
        layout.addRow("Severity:", severity_combo)
        layout.addRow("Threshold:", threshold_spin)
        layout.addRow("Threshold Low:", threshold_low_spin)
        layout.addRow("Threshold High:", threshold_high_spin)
        layout.addRow("Unit:", unit_combo)
        layout.addRow("Enabled:", enabled_check)
        layout.addRow("Description:", desc_edit)

        # Show/hide threshold fields based on condition
        def update_visibility():
            condition = AlarmCondition(condition_combo.currentText())
            is_range = condition in (AlarmCondition.OUTSIDE_RANGE, AlarmCondition.INSIDE_RANGE)
            threshold_spin.setVisible(not is_range)
            threshold_low_spin.setVisible(is_range)
            threshold_high_spin.setVisible(is_range)

        condition_combo.currentTextChanged.connect(update_visibility)
        update_visibility()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            rule_id = rule_id_edit.text().strip()
            if not rule_id:
                QMessageBox.warning(self, "Invalid Input", "Rule ID is required.")
                return

            if rule_id in analysis_config.alarm_rules:
                QMessageBox.warning(self, "Duplicate", f"Rule '{rule_id}' already exists.")
                return

            condition = AlarmCondition(condition_combo.currentText())
            severity = AlarmSeverity(severity_combo.currentText())
            unit = TemperatureUnit(unit_combo.currentText())

            if condition in (AlarmCondition.OUTSIDE_RANGE, AlarmCondition.INSIDE_RANGE):
                rule = AlarmRule(
                    rule_id=rule_id,
                    roi_id=roi_combo.currentData(),
                    condition=condition,
                    severity=severity,
                    threshold=0.0,
                    threshold_low=threshold_low_spin.value(),
                    threshold_high=threshold_high_spin.value(),
                    unit=unit,
                    enabled=enabled_check.isChecked(),
                    description=desc_edit.text(),
                )
            else:
                rule = AlarmRule(
                    rule_id=rule_id,
                    roi_id=roi_combo.currentData(),
                    condition=condition,
                    severity=severity,
                    threshold=threshold_spin.value(),
                    unit=unit,
                    enabled=enabled_check.isChecked(),
                    description=desc_edit.text(),
                )

            new_rules = dict(analysis_config.alarm_rules)
            new_rules[rule_id] = rule

            updated_config = AnalysisConfig(
                camera_id=analysis_config.camera_id,
                rois=analysis_config.rois,
                position_associations=analysis_config.position_associations,
                alarm_rules=new_rules,
                default_emissivity=analysis_config.default_emissivity,
                ambient_temperature=analysis_config.ambient_temperature,
                distance=analysis_config.distance,
                humidity=analysis_config.humidity,
                reflected_temperature=analysis_config.reflected_temperature,
                unit=analysis_config.unit,
            )
            self._config_service.set_analysis_config(updated_config)
            self._load_alarms(self._selected_camera_id)

    def _edit_alarm(self) -> None:
        items = self._alarm_tree.selectedItems()
        if items:
            rule_id = items[0].data(0, Qt.ItemDataRole.UserRole)
            self._alarm_editor.load_rule(rule_id, self._selected_camera_id)

    def _delete_alarm(self) -> None:
        items = self._alarm_tree.selectedItems()
        if not items or not self._selected_camera_id:
            return

        rule_id = items[0].data(0, Qt.ItemDataRole.UserRole)

        reply = QMessageBox.question(
            self,
            "Delete Alarm Rule",
            f"Delete alarm rule '{rule_id}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        analysis_config = self._config_service.get_analysis_config(self._selected_camera_id)
        if not analysis_config:
            return

        new_rules = {k: v for k, v in analysis_config.alarm_rules.items() if k != rule_id}

        updated_config = AnalysisConfig(
            camera_id=analysis_config.camera_id,
            rois=analysis_config.rois,
            position_associations=analysis_config.position_associations,
            alarm_rules=new_rules,
            default_emissivity=analysis_config.default_emissivity,
            ambient_temperature=analysis_config.ambient_temperature,
            distance=analysis_config.distance,
            humidity=analysis_config.humidity,
            reflected_temperature=analysis_config.reflected_temperature,
            unit=analysis_config.unit,
        )
        self._config_service.set_analysis_config(updated_config)
        self._load_alarms(self._selected_camera_id)

    def refresh_cameras(self) -> None:
        self._camera_combo.clear()
        for config in self._config_service.get_all_camera_configs():
            self._camera_combo.addItem(config.name or config.identity.camera_id, config.identity.camera_id)

    def refresh_camera_alarms(self, camera_id: str) -> None:
        if self._selected_camera_id == camera_id:
            self._load_alarms(camera_id)

    def refresh_all(self) -> None:
        self.refresh_cameras()
        if self._selected_camera_id:
            self._load_alarms(self._selected_camera_id)


class AlarmRuleEditorWidget(QWidget):
    """Editor for individual alarm rule."""

    def __init__(self, config_service: ConfigurationService) -> None:
        super().__init__()
        self._config_service = config_service
        self._current_rule_id: str | None = None
        self._current_camera_id: str | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        self._group = QGroupBox("Alarm Rule Editor")
        layout = QVBoxLayout(self._group)

        form = QFormLayout()

        self._roi_label = QLabel("—")
        form.addRow("ROI:", self._roi_label)

        self._condition_combo = QComboBox()
        self._condition_combo.addItems([c.value for c in AlarmCondition])
        self._condition_combo.currentTextChanged.connect(self._on_condition_changed)
        form.addRow("Condition:", self._condition_combo)

        self._severity_combo = QComboBox()
        self._severity_combo.addItems([s.value for s in AlarmSeverity])
        form.addRow("Severity:", self._severity_combo)

        self._threshold_spin = QDoubleSpinBox()
        self._threshold_spin.setRange(-273.15, 2000.0)
        self._threshold_spin.setDecimals(1)
        form.addRow("Threshold:", self._threshold_spin)

        self._threshold_low_spin = QDoubleSpinBox()
        self._threshold_low_spin.setRange(-273.15, 2000.0)
        self._threshold_low_spin.setDecimals(1)
        self._threshold_low_spin.setVisible(False)
        form.addRow("Threshold Low:", self._threshold_low_spin)

        self._threshold_high_spin = QDoubleSpinBox()
        self._threshold_high_spin.setRange(-273.15, 2000.0)
        self._threshold_high_spin.setDecimals(1)
        self._threshold_high_spin.setVisible(False)
        form.addRow("Threshold High:", self._threshold_high_spin)

        self._unit_combo = QComboBox()
        self._unit_combo.addItems([u.value for u in TemperatureUnit])
        form.addRow("Unit:", self._unit_combo)

        self._enabled_check = QCheckBox()
        self._enabled_check.setChecked(True)
        form.addRow("Enabled:", self._enabled_check)

        self._desc_edit = QLineEdit()
        form.addRow("Description:", self._desc_edit)

        layout.addLayout(form)

        self._save_btn = QPushButton("Save Alarm Rule")
        self._save_btn.clicked.connect(self._save)
        self._save_btn.setEnabled(False)
        layout.addWidget(self._save_btn)

        layout.addStretch()

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self._group)

    def _on_condition_changed(self, text: str) -> None:
        condition = AlarmCondition(text)
        is_range = condition in (AlarmCondition.OUTSIDE_RANGE, AlarmCondition.INSIDE_RANGE)
        self._threshold_spin.setVisible(not is_range)
        self._threshold_low_spin.setVisible(is_range)
        self._threshold_high_spin.setVisible(is_range)

    def load_rule(self, rule_id: str, camera_id: str) -> None:
        analysis_config = self._config_service.get_analysis_config(camera_id)
        if not analysis_config or rule_id not in analysis_config.alarm_rules:
            self.clear()
            return

        rule = analysis_config.alarm_rules[rule_id]
        self._current_rule_id = rule_id
        self._current_camera_id = camera_id
        self._save_btn.setEnabled(True)

        roi_name = analysis_config.rois.get(rule.roi_id, type('obj', (object,), {'name': rule.roi_id})()).name
        self._roi_label.setText(f"{roi_name} ({rule.roi_id})")

        self._condition_combo.setCurrentText(rule.condition.value)
        self._severity_combo.setCurrentText(rule.severity.value)
        self._threshold_spin.setValue(rule.threshold)
        self._threshold_low_spin.setValue(rule.threshold_low if rule.threshold_low is not None else 0.0)
        self._threshold_high_spin.setValue(rule.threshold_high if rule.threshold_high is not None else 0.0)
        self._unit_combo.setCurrentText(rule.unit.value)
        self._enabled_check.setChecked(rule.enabled)
        self._desc_edit.setText(rule.description)

        self._on_condition_changed(rule.condition.value)

    def clear(self) -> None:
        self._current_rule_id = None
        self._current_camera_id = None
        self._save_btn.setEnabled(False)
        self._roi_label.setText("—")
        self._condition_combo.setCurrentIndex(0)
        self._severity_combo.setCurrentIndex(0)
        self._threshold_spin.setValue(0.0)
        self._threshold_low_spin.setValue(0.0)
        self._threshold_high_spin.setValue(0.0)
        self._unit_combo.setCurrentIndex(0)
        self._enabled_check.setChecked(True)
        self._desc_edit.clear()

    def _save(self) -> None:
        if not self._current_rule_id or not self._current_camera_id:
            return

        analysis_config = self._config_service.get_analysis_config(self._current_camera_id)
        if not analysis_config or self._current_rule_id not in analysis_config.alarm_rules:
            return

        old_rule = analysis_config.alarm_rules[self._current_rule_id]
        condition = AlarmCondition(self._condition_combo.currentText())

        if condition in (AlarmCondition.OUTSIDE_RANGE, AlarmCondition.INSIDE_RANGE):
            new_rule = AlarmRule(
                rule_id=old_rule.rule_id,
                roi_id=old_rule.roi_id,
                condition=condition,
                severity=AlarmSeverity(self._severity_combo.currentText()),
                threshold=0.0,
                threshold_low=self._threshold_low_spin.value(),
                threshold_high=self._threshold_high_spin.value(),
                unit=TemperatureUnit(self._unit_combo.currentText()),
                enabled=self._enabled_check.isChecked(),
                description=self._desc_edit.text(),
            )
        else:
            new_rule = AlarmRule(
                rule_id=old_rule.rule_id,
                roi_id=old_rule.roi_id,
                condition=condition,
                severity=AlarmSeverity(self._severity_combo.currentText()),
                threshold=self._threshold_spin.value(),
                unit=TemperatureUnit(self._unit_combo.currentText()),
                enabled=self._enabled_check.isChecked(),
                description=self._desc_edit.text(),
            )

        new_rules = dict(analysis_config.alarm_rules)
        new_rules[self._current_rule_id] = new_rule

        updated_config = AnalysisConfig(
            camera_id=analysis_config.camera_id,
            rois=analysis_config.rois,
            position_associations=analysis_config.position_associations,
            alarm_rules=new_rules,
            default_emissivity=analysis_config.default_emissivity,
            ambient_temperature=analysis_config.ambient_temperature,
            distance=analysis_config.distance,
            humidity=analysis_config.humidity,
            reflected_temperature=analysis_config.reflected_temperature,
            unit=analysis_config.unit,
        )
        self._config_service.set_analysis_config(updated_config)


# --------------------------------------------------------------------------
# Recording Configuration Tab
# --------------------------------------------------------------------------


class RecordingConfigurationTab(QWidget):
    """Recording configuration per camera."""

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

        # Camera selector
        selector_group = QGroupBox("Camera Selection")
        selector_layout = QFormLayout(selector_group)
        self._camera_combo = QComboBox()
        self._camera_combo.currentTextChanged.connect(self._on_camera_changed)
        selector_layout.addRow("Camera:", self._camera_combo)
        layout.addWidget(selector_group)

        # Recording settings
        self._settings_group = QGroupBox("Recording Settings")
        settings_layout = QFormLayout(self._settings_group)

        self._enabled_check = QCheckBox()
        self._enabled_check.setChecked(True)
        settings_layout.addRow("Enabled:", self._enabled_check)

        self._pre_alarm_spin = QDoubleSpinBox()
        self._pre_alarm_spin.setRange(0.0, 3600.0)
        self._pre_alarm_spin.setValue(10.0)
        self._pre_alarm_spin.setSuffix(" s")
        settings_layout.addRow("Pre-alarm Duration:", self._pre_alarm_spin)

        self._post_alarm_spin = QDoubleSpinBox()
        self._post_alarm_spin.setRange(0.0, 3600.0)
        self._post_alarm_spin.setValue(30.0)
        self._post_alarm_spin.setSuffix(" s")
        settings_layout.addRow("Post-alarm Duration:", self._post_alarm_spin)

        self._max_duration_spin = QDoubleSpinBox()
        self._max_duration_spin.setRange(1.0, 86400.0)
        self._max_duration_spin.setValue(300.0)
        self._max_duration_spin.setSuffix(" s")
        settings_layout.addRow("Max Recording Duration:", self._max_duration_spin)

        self._storage_path_edit = QLineEdit()
        self._storage_path_edit.setPlaceholderText("e.g., D:/recordings")
        settings_layout.addRow("Storage Path:", self._storage_path_edit)

        layout.addWidget(self._settings_group)

        # Save button
        self._save_btn = QPushButton("Save Recording Configuration")
        self._save_btn.clicked.connect(self._save)
        self._save_btn.setEnabled(False)
        layout.addWidget(self._save_btn)

        layout.addStretch()

    def _on_camera_changed(self, text: str) -> None:
        camera_id = self._camera_combo.currentData()
        if camera_id:
            self._selected_camera_id = camera_id
            self._load_recording_config(camera_id)
            self._save_btn.setEnabled(True)
        else:
            self._selected_camera_id = None
            self._clear_form()
            self._save_btn.setEnabled(False)

    def _load_recording_config(self, camera_id: str) -> None:
        config = self._config_service.get_recording_config(camera_id)
        if not config:
            config = self._config_service.create_recording_config(camera_id)
            self._config_service.set_recording_config(config)

        self._enabled_check.setChecked(config.enabled)
        self._pre_alarm_spin.setValue(config.pre_alarm_seconds)
        self._post_alarm_spin.setValue(config.post_alarm_seconds)
        self._max_duration_spin.setValue(config.max_duration_seconds)
        self._storage_path_edit.setText(config.storage_path or "")

    def _clear_form(self) -> None:
        self._enabled_check.setChecked(False)
        self._pre_alarm_spin.setValue(10.0)
        self._post_alarm_spin.setValue(30.0)
        self._max_duration_spin.setValue(300.0)
        self._storage_path_edit.clear()

    def _save(self) -> None:
        if not self._selected_camera_id:
            return

        config = self._config_service.get_recording_config(self._selected_camera_id)
        if not config:
            config = self._config_service.create_recording_config(self._selected_camera_id)

        from thermal_monitor.core.models import RecordingConfig

        new_config = RecordingConfig(
            camera_id=config.camera_id,
            enabled=self._enabled_check.isChecked(),
            pre_alarm_seconds=self._pre_alarm_spin.value(),
            post_alarm_seconds=self._post_alarm_spin.value(),
            max_duration_seconds=self._max_duration_spin.value(),
            storage_path=self._storage_path_edit.text() or None,
        )
        self._config_service.set_recording_config(new_config)

    def refresh_cameras(self) -> None:
        self._camera_combo.clear()
        for config in self._config_service.get_all_camera_configs():
            self._camera_combo.addItem(config.name or config.identity.camera_id, config.identity.camera_id)

    def refresh_camera(self, camera_id: str) -> None:
        if self._selected_camera_id == camera_id:
            self._load_recording_config(camera_id)

    def refresh_all(self) -> None:
        self.refresh_cameras()
        if self._selected_camera_id:
            self._load_recording_config(self._selected_camera_id)


# --------------------------------------------------------------------------
# Calibration Information Tab
# --------------------------------------------------------------------------


class CalibrationInformationTab(QWidget):
    """Display calibration information (read-only in configuration mode)."""

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

        # Camera selector
        selector_group = QGroupBox("Camera Selection")
        selector_layout = QFormLayout(selector_group)
        self._camera_combo = QComboBox()
        self._camera_combo.currentTextChanged.connect(self._on_camera_changed)
        selector_layout.addRow("Camera:", self._camera_combo)
        layout.addWidget(selector_group)

        # Calibration info display
        self._info_group = QGroupBox("Calibration Information")
        info_layout = QVBoxLayout(self._info_group)

        self._cal_info_text = QTextEdit()
        self._cal_info_text.setReadOnly(True)
        self._cal_info_text.setFontFamily("Consolas")
        self._cal_info_text.setPlainText("Select a camera to view calibration information.")
        info_layout.addWidget(self._cal_info_text)

        layout.addWidget(self._info_group, 1)

        # Refresh button
        self._refresh_btn = QPushButton("Refresh from Database")
        self._refresh_btn.clicked.connect(self._refresh_from_database)
        layout.addWidget(self._refresh_btn)

    def _on_camera_changed(self, text: str) -> None:
        camera_id = self._camera_combo.currentData()
        if camera_id:
            self._load_calibration_info(camera_id)
        else:
            self._cal_info_text.setPlainText("Select a camera to view calibration information.")

    def _load_calibration_info(self, camera_id: str) -> None:
        """Load and display calibration information."""
        if self._database:
            # TODO: Load from database
            self._cal_info_text.setPlainText(
                f"Camera: {camera_id}\n"
                "Calibration data loading from database not yet implemented.\n"
                "Use the calibration processor to load calibration files."
            )
        else:
            self._cal_info_text.setPlainText(
                f"Camera: {camera_id}\n"
                "No database connection available.\n"
                "Calibration information unavailable."
            )

    def _refresh_from_database(self) -> None:
        camera_id = self._camera_combo.currentData()
        if camera_id:
            self._load_calibration_info(camera_id)

    def refresh_cameras(self) -> None:
        self._camera_combo.clear()
        for config in self._config_service.get_all_camera_configs():
            self._camera_combo.addItem(config.name or config.identity.camera_id, config.identity.camera_id)

    def refresh_all(self) -> None:
        self.refresh_cameras()


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

        # Database settings
        db_group = QGroupBox("Database Configuration")
        db_layout = QFormLayout(db_group)
        self._db_server_edit = QLineEdit()
        self._db_server_edit.setPlaceholderText("localhost\\SQLEXPRESS")
        self._db_name_edit = QLineEdit()
        self._db_name_edit.setPlaceholderText("ThermalMonitor")
        self._db_auth_combo = QComboBox()
        self._db_auth_combo.addItems(["Windows Authentication", "SQL Authentication"])
        self._db_user_edit = QLineEdit()
        self._db_pass_edit = QLineEdit()
        self._db_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        db_layout.addRow("Server:", self._db_server_edit)
        db_layout.addRow("Database:", self._db_name_edit)
        db_layout.addRow("Authentication:", self._db_auth_combo)
        db_layout.addRow("Username:", self._db_user_edit)
        db_layout.addRow("Password:", self._db_pass_edit)
        layout.addWidget(db_group)

        # Application settings
        app_group = QGroupBox("Application Settings")
        app_layout = QFormLayout(app_group)
        self._log_level_combo = QComboBox()
        self._log_level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self._log_level_combo.setCurrentText("INFO")
        self._auto_save_check = QCheckBox()
        self._auto_save_check.setChecked(True)
        app_layout.addRow("Log Level:", self._log_level_combo)
        app_layout.addRow("Auto-save Configuration:", self._auto_save_check)
        layout.addWidget(app_group)

        # Save button
        self._save_btn = QPushButton("Save System Configuration")
        self._save_btn.clicked.connect(self._save)
        layout.addWidget(self._save_btn)

        layout.addStretch()

    def _save(self) -> None:
        config = SystemConfig(
            database_server=self._db_server_edit.text() or None,
            database_name=self._db_name_edit.text() or None,
            database_trusted=(self._db_auth_combo.currentIndex() == 0),
            database_username=self._db_user_edit.text() or None,
            database_password=self._db_pass_edit.text() or None,
            log_level=self._log_level_combo.currentText(),
            auto_save_config=self._auto_save_check.isChecked(),
        )
        self._config_service.update_system_config(config)

    def refresh(self) -> None:
        config = self._config_service.system_config
        self._db_server_edit.setText(config.database_server or "")
        self._db_name_edit.setText(config.database_name or "")
        self._db_auth_combo.setCurrentIndex(0 if config.database_trusted else 1)
        self._db_user_edit.setText(config.database_username or "")
        self._db_pass_edit.setText(config.database_password or "")
        self._log_level_combo.setCurrentText(config.log_level)
        self._auto_save_check.setChecked(config.auto_save_config)

    def refresh_all(self) -> None:
        self.refresh()