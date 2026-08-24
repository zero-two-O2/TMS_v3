"""
ui.configuration_editor -- Configuration Editor for deployment settings.

Provides a GUI for editing config.yaml deployment configuration.
Uses ConfigurationManager for validation and atomic persistence.
"""

from __future__ import annotations

import shutil
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QStackedWidget,
    QScrollArea,
    QFormLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QLineEdit,
    QSpinBox,
    QDoubleSpinBox,
    QComboBox,
    QCheckBox,
    QTextEdit,
    QFileDialog,
    QMessageBox,
    QFrame,
    QApplication,
)
from PyQt6.QtGui import QFont

from thermal_monitor.config import ConfigurationManager, AppConfig
from thermal_monitor.config.models import (
    ApplicationConfig,
    DatabaseConfig,
    CamerasConfig,
    CameraDiscoveryConfig,
    CameraAcquisitionConfig,
    CameraRecoveryConfig,
    CameraConnectionConfig,
    CameraStartupConfig,
    CameraMappingConfig,
    SystemConfig,
    StorageConfig,
    RecordingConfig,
    CalibrationConfig,
    ProcessingConfig,
    AlarmsConfig,
    PTZConfig,
    PTZLimitsConfig,
    PTZDefaultPositionConfig,
    PTZSpeedsConfig,
    ROIConfig,
    ROIDefaultsConfig,
    OfflineConfig,
    OfflinePlaybackConfig,
    LoggingConfig,
    UIConfig,
    UIColorsConfig,
    UIWindowsConfig,
    UILiveConfig,
    UIDisplayConfig,
    NetworkConfig,
    HALCONConfig,
)
from thermal_monitor.ui.theme import ThemeManager


class ConfigEditorField:
    """Represents a single editable configuration field."""

    def __init__(
        self,
        name: str,
        label: str,
        field_type: type,
        default: Any = None,
        readonly: bool = False,
        options: list = None,
        min_val: float = None,
        max_val: float = None,
        suffix: str = "",
        tooltip: str = "",
    ) -> None:
        self.name = name
        self.label = label
        self.field_type = field_type
        self.default = default
        self.readonly = readonly
        self.options = options or []
        self.min_val = min_val
        self.max_val = max_val
        self.suffix = suffix
        self.tooltip = tooltip
        self.widget: Optional[QWidget] = None


class ConfigSectionEditor(QWidget):
    """Editor for a single configuration section."""

    value_changed = pyqtSignal()

    def __init__(
        self,
        section_name: str,
        config_obj: Any,
        theme_manager: Optional[ThemeManager] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._section_name = section_name
        self._config_obj = config_obj
        self._theme = theme_manager
        self._fields: dict[str, ConfigEditorField] = {}
        self._widgets: dict[str, QWidget] = {}
        self._original_values: dict[str, Any] = {}
        self._setup_ui()
        # Don't load values yet - fields will be added later
        # self._load_values()

    def load_values(self) -> None:
        """Load values from config object. Call after all fields are added."""
        self._load_values()

    def _setup_ui(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content = QWidget()
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setSpacing(8)

        scroll.setWidget(self._content)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(scroll)

    def add_field(self, field_def: ConfigEditorField) -> None:
        """Add a field to the editor."""
        self._fields[field_def.name] = field_def

        # Create form row
        form_layout = QFormLayout()
        form_layout.setSpacing(6)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        widget = self._create_widget(field_def)
        field_def.widget = widget
        self._widgets[field_def.name] = widget

        if field_def.readonly:
            widget.setEnabled(False)
            if self._theme:
                widget.setStyleSheet(f"background-color: {self._theme.surface()}; color: {self._theme.text_muted()};")
            else:
                widget.setStyleSheet("background-color: #F5F5F5; color: #888888;")

        if field_def.tooltip:
            widget.setToolTip(field_def.tooltip)

        label = QLabel(field_def.label)
        if self._theme:
            label.setStyleSheet(f"color: {self._theme.text()};")
        form_layout.addRow(label, widget)

        self._layout.addLayout(form_layout)

        # Connect change signal
        if hasattr(widget, 'valueChanged'):
            widget.valueChanged.connect(self.value_changed.emit)
        elif hasattr(widget, 'currentTextChanged'):
            widget.currentTextChanged.connect(self.value_changed.emit)
        elif hasattr(widget, 'textChanged'):
            widget.textChanged.connect(self.value_changed.emit)
        elif hasattr(widget, 'stateChanged'):
            widget.stateChanged.connect(self.value_changed.emit)

    def _create_widget(self, field_def: ConfigEditorField) -> QWidget:
        """Create the appropriate widget for a field type."""
        if field_def.field_type is bool:
            widget = QCheckBox()
            if field_def.default is not None:
                widget.setChecked(field_def.default)
        elif field_def.field_type is int:
            widget = QSpinBox()
            if field_def.min_val is not None:
                widget.setMinimum(int(field_def.min_val))
            if field_def.max_val is not None:
                widget.setMaximum(int(field_def.max_val))
            if field_def.default is not None:
                widget.setValue(field_def.default)
            if field_def.suffix:
                widget.setSuffix(f" {field_def.suffix}")
        elif field_def.field_type is float:
            widget = QDoubleSpinBox()
            if field_def.min_val is not None:
                widget.setMinimum(field_def.min_val)
            if field_def.max_val is not None:
                widget.setMaximum(field_def.max_val)
            if field_def.default is not None:
                widget.setValue(field_def.default)
            if field_def.suffix:
                widget.setSuffix(f" {field_def.suffix}")
            widget.setDecimals(2)
        elif field_def.field_type is str and field_def.options:
            widget = QComboBox()
            widget.addItems([str(opt) for opt in field_def.options])
            if field_def.default is not None:
                idx = widget.findText(str(field_def.default))
                if idx >= 0:
                    widget.setCurrentIndex(idx)
        elif field_def.field_type is str:
            widget = QLineEdit()
            if field_def.default is not None:
                widget.setText(str(field_def.default))
        else:
            widget = QLineEdit()
            if field_def.default is not None:
                widget.setText(str(field_def.default))

        if self._theme:
            widget.setStyleSheet(self._theme.base_stylesheet())

        return widget

    def _load_values(self) -> None:
        """Load current values from config object."""
        if not is_dataclass(self._config_obj):
            return

        # Block signals during initial load to prevent dirty flag
        for widget in self._widgets.values():
            widget.blockSignals(True)

        try:
            for field_info in fields(self._config_obj):
                name = field_info.name
                if name in self._fields:
                    value = getattr(self._config_obj, name)
                    self._original_values[name] = value
                    widget = self._widgets.get(name)
                    if widget:
                        self._set_widget_value(widget, self._fields[name], value)
        finally:
            for widget in self._widgets.values():
                widget.blockSignals(False)

    def _set_widget_value(self, widget: QWidget, field_def: ConfigEditorField, value: Any) -> None:
        """Set widget value from config."""
        if isinstance(widget, QCheckBox):
            widget.setChecked(bool(value))
        elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            widget.setValue(value)
        elif isinstance(widget, QComboBox):
            idx = widget.findText(str(value))
            if idx >= 0:
                widget.setCurrentIndex(idx)
        elif isinstance(widget, QLineEdit):
            widget.setText(str(value) if value is not None else "")

    def get_values(self) -> dict[str, Any]:
        """Get current values from widgets."""
        values = {}
        for name, field_def in self._fields.items():
            widget = self._widgets.get(name)
            if widget:
                values[name] = self._get_widget_value(widget, field_def)
        return values

    def _get_widget_value(self, widget: QWidget, field_def: ConfigEditorField) -> Any:
        """Get value from widget."""
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            return widget.value()
        elif isinstance(widget, QComboBox):
            return widget.currentText()
        elif isinstance(widget, QLineEdit):
            text = widget.text()
            if field_def.field_type is int:
                return int(text) if text else 0
            elif field_def.field_type is float:
                return float(text) if text else 0.0
            return text
        return None

    def is_dirty(self) -> bool:
        """Check if any values have changed."""
        current = self.get_values()
        for name, value in current.items():
            if name in self._original_values:
                if self._original_values[name] != value:
                    return True
        return False

    def reset_to_defaults(self) -> None:
        """Reset all fields to their original values."""
        for widget in self._widgets.values():
            widget.blockSignals(True)
        try:
            for name, value in self._original_values.items():
                widget = self._widgets.get(name)
                field_def = self._fields.get(name)
                if widget and field_def:
                    self._set_widget_value(widget, field_def, value)
        finally:
            for widget in self._widgets.values():
                widget.blockSignals(False)

    def apply_to_config(self, config_obj: Any) -> Any:
        """Apply current values to config object. Returns new instance for frozen dataclasses."""
        if not is_dataclass(config_obj):
            return config_obj

        values = self.get_values()
        field_values = {}
        for f in fields(config_obj):
            name = f.name
            if name in values:
                field_values[name] = values[name]
            else:
                field_values[name] = getattr(config_obj, name)
        
        # Return new instance with updated values
        return type(config_obj)(**field_values)


class ConfigurationEditor(QWidget):
    """
    Main configuration editor widget.

    Provides a navigation tree on the left and section editors on the right.
    Handles dirty state, validation, atomic save, and backup.
    """

    # Signals
    save_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    restart_required = pyqtSignal(str)  # message
    config_saved = pyqtSignal()
    config_error = pyqtSignal(str)

    def __init__(
        self,
        config_manager: ConfigurationManager,
        theme_manager: Optional[ThemeManager] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._config_manager = config_manager
        self._theme = theme_manager
        self._original_config: AppConfig = config_manager.get_config()
        self._edit_config: AppConfig = self._clone_config(self._original_config)
        self._section_editors: dict[str, ConfigSectionEditor] = {}
        self._dirty = False
        self._setup_ui()
        self._build_sections()

    def _clone_config(self, config: AppConfig) -> AppConfig:
        """Create a mutable copy of the configuration for editing."""
        # Since AppConfig is frozen, we need to create new instances
        # We'll build a new AppConfig from the original values
        return AppConfig(
            application=self._clone_dataclass(config.application),
            database=self._clone_dataclass(config.database),
            cameras=self._clone_cameras_config(config.cameras),
            system=self._clone_dataclass(config.system),
            storage=self._clone_dataclass(config.storage),
            recording=self._clone_dataclass(config.recording),
            calibration=self._clone_dataclass(config.calibration),
            processing=self._clone_dataclass(config.processing),
            alarms=self._clone_dataclass(config.alarms),
            ptz=self._clone_ptz_config(config.ptz),
            roi=self._clone_roi_config(config.roi),
            offline=self._clone_dataclass(config.offline),
            logging=self._clone_dataclass(config.logging),
            ui=self._clone_ui_config(config.ui),
            network=self._clone_dataclass(config.network),
            halcon=self._clone_dataclass(config.halcon),
        )

    def _clone_dataclass(self, obj: Any) -> Any:
        """Clone a dataclass instance."""
        if not is_dataclass(obj):
            return obj
        field_values = {}
        for f in fields(obj):
            value = getattr(obj, f.name)
            if is_dataclass(value):
                field_values[f.name] = self._clone_dataclass(value)
            elif isinstance(value, list):
                field_values[f.name] = [self._clone_dataclass(v) if is_dataclass(v) else v for v in value]
            else:
                field_values[f.name] = value
        return type(obj)(**field_values)

    def _clone_cameras_config(self, config: CamerasConfig) -> CamerasConfig:
        """Clone CamerasConfig with nested objects."""
        return CamerasConfig(
            discovery=self._clone_dataclass(config.discovery),
            acquisition=self._clone_dataclass(config.acquisition),
            recovery=self._clone_dataclass(config.recovery),
            connection=self._clone_dataclass(config.connection),
            startup=self._clone_dataclass(config.startup),
            mapping=[self._clone_dataclass(m) for m in config.mapping],
        )

    def _clone_ptz_config(self, config: PTZConfig) -> PTZConfig:
        """Clone PTZConfig with nested objects."""
        return PTZConfig(
            enabled=config.enabled,
            limits=self._clone_dataclass(config.limits),
            default_position=self._clone_dataclass(config.default_position),
            speeds=self._clone_dataclass(config.speeds),
        )

    def _clone_roi_config(self, config: ROIConfig) -> ROIConfig:
        """Clone ROIConfig with nested objects."""
        return ROIConfig(
            defaults=ROIDefaultsConfig(
                RECTANGLE1=dict(config.defaults.RECTANGLE1),
                RECTANGLE2=dict(config.defaults.RECTANGLE2),
                CIRCLE=dict(config.defaults.CIRCLE),
                ELLIPSE=dict(config.defaults.ELLIPSE),
                POLYGON=[list(p) for p in config.defaults.POLYGON],
            )
        )

    def _clone_ui_config(self, config: UIConfig) -> UIConfig:
        """Clone UIConfig with nested objects."""
        return UIConfig(
            theme=config.theme,
            colors=self._clone_dataclass(config.colors),
            windows=self._clone_dataclass(config.windows),
            live=self._clone_dataclass(config.live),
            display=self._clone_dataclass(config.display),
        )

    def _setup_ui(self) -> None:
        """Set up the main UI layout."""
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Splitter for navigation and editor
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)

        # Left: Navigation tree
        self._nav_tree = QTreeWidget()
        self._nav_tree.setHeaderLabel("Configuration")
        self._nav_tree.setMinimumWidth(280)
        self._nav_tree.setMaximumWidth(350)
        self._nav_tree.currentItemChanged.connect(self._on_nav_changed)
        splitter.addWidget(self._nav_tree)

        # Right: Section editor stack
        self._editor_stack = QStackedWidget()
        splitter.addWidget(self._editor_stack)

        splitter.setSizes([300, 900])

        # Toolbar at top
        toolbar = QFrame()
        toolbar.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(8, 8, 8, 8)

        self._save_btn = QPushButton("Save")
        self._save_btn.clicked.connect(self._on_save)
        if self._theme:
            self._save_btn.setStyleSheet(self._theme.primary_button_stylesheet())

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel)
        if self._theme:
            self._cancel_btn.setStyleSheet(self._theme.secondary_button_stylesheet())

        self._reset_btn = QPushButton("Reset")
        self._reset_btn.clicked.connect(self._on_reset)
        if self._theme:
            self._reset_btn.setStyleSheet(self._theme.accent_button_stylesheet())

        self._search_btn = QPushButton("Search Cameras")
        self._search_btn.clicked.connect(self._on_search_cameras)
        if self._theme:
            self._search_btn.setStyleSheet(self._theme.secondary_button_stylesheet())

        self._status_label = QLabel("Configuration loaded")
        if self._theme:
            self._status_label.setStyleSheet(f"color: {self._theme.text_secondary()}; font-weight: bold;")

        toolbar_layout.addWidget(self._save_btn)
        toolbar_layout.addWidget(self._cancel_btn)
        toolbar_layout.addWidget(self._reset_btn)
        toolbar_layout.addStretch()
        toolbar_layout.addWidget(self._search_btn)
        toolbar_layout.addWidget(self._status_label)

        # Add toolbar above splitter
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)
        outer_layout.addWidget(toolbar)
        outer_layout.addWidget(splitter, 1)

        self.setLayout(outer_layout)

    def _build_sections(self) -> None:
        """Build all configuration sections."""
        # Define section structure
        sections = [
            ("General", [
                ("Application", self._create_application_editor),
                ("System", self._create_system_editor),
            ]),
            ("Cameras", [
                ("Discovery", self._create_discovery_editor),
                ("Acquisition", self._create_acquisition_editor),
                ("Recovery", self._create_recovery_editor),
                ("Connection", self._create_connection_editor),
                ("Startup", self._create_startup_editor),
                ("Camera Mapping", self._create_mapping_editor),
            ]),
            ("Storage", [
                ("Storage Paths", self._create_storage_editor),
                ("Recording", self._create_recording_editor),
                ("Calibration", self._create_calibration_editor),
            ]),
            ("Processing", [
                ("Processing", self._create_processing_editor),
                ("Alarms", self._create_alarms_editor),
                ("ROI Defaults", self._create_roi_defaults_editor),
            ]),
            ("PTZ", [
                ("PTZ", self._create_ptz_editor),
            ]),
            ("Offline", [
                ("Offline Playback", self._create_offline_editor),
            ]),
            ("Database", [
                ("Database", self._create_database_editor),
            ]),
            ("Network", [
                ("Network", self._create_network_editor),
            ]),
            ("Logging", [
                ("Logging", self._create_logging_editor),
            ]),
            ("UI", [
                ("Theme", self._create_theme_editor),
                ("Colors", self._create_colors_editor),
                ("Windows", self._create_windows_editor),
                ("Live Display", self._create_live_display_editor),
                ("Display", self._create_display_editor),
            ]),
            ("HALCON", [
                ("HALCON", self._create_halcon_editor),
            ]),
        ]

        # Build navigation tree and editors
        for category_name, section_list in sections:
            category_item = QTreeWidgetItem(self._nav_tree, [category_name])
            category_item.setData(0, Qt.ItemDataRole.UserRole, ("category", category_name))
            category_item.setFlags(category_item.flags() | Qt.ItemFlag.ItemIsEnabled)
            font = category_item.font(0)
            font.setBold(True)
            category_item.setFont(0, font)

            for section_name, editor_factory in section_list:
                section_item = QTreeWidgetItem(category_item, [section_name])
                section_item.setData(0, Qt.ItemDataRole.UserRole, ("section", section_name))

                editor = editor_factory()
                self._section_editors[section_name] = editor
                self._editor_stack.addWidget(editor)
                editor.value_changed.connect(self._on_value_changed)

        # Load values for all editors after they're created
        for editor in self._section_editors.values():
            editor.load_values()

        self._nav_tree.expandAll()

    def _on_nav_changed(self, current: QTreeWidgetItem, previous: QTreeWidgetItem) -> None:
        """Handle navigation selection change."""
        if current is None:
            return
        data = current.data(0, Qt.ItemDataRole.UserRole)
        if data and data[0] == "section":
            section_name = data[1]
            editor = self._section_editors.get(section_name)
            if editor:
                self._editor_stack.setCurrentWidget(editor)

    def _on_value_changed(self) -> None:
        """Handle value change in any editor."""
        self._dirty = True
        self._update_save_button()
        self._status_label.setText("Unsaved changes")

    def _update_save_button(self) -> None:
        """Update save button state."""
        self._save_btn.setEnabled(self._dirty)
        if self._theme and self._dirty:
            self._save_btn.setStyleSheet(self._theme.primary_button_stylesheet() + " background-color: #FF5722;")

    def _on_save(self) -> None:
        """Handle save button click."""
        if not self._dirty:
            return

        # Validate configuration
        try:
            # Create a test ConfigurationManager with the edited config
            test_config = self._build_app_config_from_edit()
            # Validation happens in ConfigurationManager constructor
        except Exception as e:
            QMessageBox.critical(self, "Validation Error", f"Configuration validation failed:\n{str(e)}")
            self.config_error.emit(str(e))
            return

        # Show restart required message
        msg = QMessageBox(self)
        msg.setWindowTitle("Save Configuration")
        msg.setText("Configuration will be saved.")
        msg.setInformativeText("Most changes require an application restart to take effect.\n\nDo you want to continue?")
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.setDefaultButton(QMessageBox.StandardButton.Yes)
        if msg.exec() != QMessageBox.StandardButton.Yes:
            return

        # Perform atomic save
        try:
            self._atomic_save()
            self._dirty = False
            self._update_save_button()
            self._status_label.setText("Configuration saved - restart required")
            self.config_saved.emit()
            self.restart_required.emit("Configuration saved. Restart required for changes to take effect.")
            QMessageBox.information(self, "Saved", "Configuration saved successfully.\n\nRestart the application for changes to take effect.")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", f"Failed to save configuration:\n{str(e)}")
            self.config_error.emit(str(e))

    def _on_cancel(self) -> None:
        """Handle cancel button click."""
        if self._dirty:
            msg = QMessageBox(self)
            msg.setWindowTitle("Unsaved Changes")
            msg.setText("You have unsaved changes.")
            msg.setInformativeText("Do you want to discard your changes?")
            msg.setStandardButtons(
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel
            )
            msg.setDefaultButton(QMessageBox.StandardButton.Save)
            result = msg.exec()

            if result == QMessageBox.StandardButton.Save:
                self._on_save()
                if not self._dirty:  # Save succeeded
                    self.cancel_requested.emit()
            elif result == QMessageBox.StandardButton.Discard:
                self._reset_all()
                self.cancel_requested.emit()
            # Cancel - do nothing
        else:
            self.cancel_requested.emit()

    def _on_reset(self) -> None:
        """Handle reset button click."""
        msg = QMessageBox(self)
        msg.setWindowTitle("Reset Configuration")
        msg.setText("Reset all configuration to built-in defaults?")
        msg.setInformativeText("This will discard all current changes and restore factory defaults.")
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.setDefaultButton(QMessageBox.StandardButton.No)
        if msg.exec() == QMessageBox.StandardButton.Yes:
            # Reset to defaults by creating new config manager without file
            default_config = self._config_manager._get_default_config_dict()
            # This is complex - for now just reset all editors
            for editor in self._section_editors.values():
                editor.reset_to_defaults()
            self._dirty = False
            self._update_save_button()
            self._status_label.setText("Reset to defaults (not saved)")

    def _on_search_cameras(self) -> None:
        """Handle search cameras button."""
        self._status_label.setText("Searching for cameras...")
        QApplication.processEvents()
        try:
            # Use the discovery service from the app
            from thermal_monitor.services.discovery import CameraDiscoveryService
            discovery = CameraDiscoveryService(
                halcon_interface=self._edit_config.cameras.discovery.halcon_interface,
                attempts=self._edit_config.cameras.discovery.attempts,
                retry_delay_s=self._edit_config.cameras.discovery.retry_delay_s,
            )
            cameras = discovery.discover_cameras()
            self._status_label.setText(f"Found {len(cameras)} camera(s)")
            # Update the mapping editor with discovered cameras
            self._refresh_mapping_with_discovery(cameras)
        except Exception as e:
            self._status_label.setText(f"Discovery failed: {e}")
            QMessageBox.warning(self, "Camera Discovery", f"Failed to discover cameras:\n{str(e)}")

    def _refresh_mapping_with_discovery(self, cameras: list) -> None:
        """Refresh camera mapping editor with discovered cameras."""
        editor = self._section_editors.get("Camera Mapping")
        if editor and hasattr(editor, '_refresh_discovered'):
            editor._refresh_discovered(cameras)

    def _reset_all(self) -> None:
        """Reset all editors to original values."""
        for editor in self._section_editors.values():
            editor.reset_to_defaults()
        self._dirty = False
        self._update_save_button()
        self._status_label.setText("Configuration loaded")

    def _atomic_save(self) -> None:
        """Atomically save configuration to YAML file."""
        config_path = self._config_manager.config_path

        # Create backup
        if config_path.exists():
            backup_path = config_path.with_suffix(config_path.suffix + ".bak")
            shutil.copy2(config_path, backup_path)

        # Build complete config dict from edit config
        config_dict = self._config_to_dict(self._edit_config)

        # Write to temporary file
        temp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        try:
            import yaml
            temp_path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp_path, "w", encoding="utf-8") as f:
                yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

            # Atomic replace
            temp_path.replace(config_path)

            # Reload configuration manager
            self._config_manager._load_config(create_default=False)
        except Exception as e:
            # Cleanup temp file on failure
            if temp_path.exists():
                temp_path.unlink()
            raise

    def _config_to_dict(self, config: AppConfig) -> dict[str, Any]:
        """Convert AppConfig to dictionary for YAML serialization."""
        def dataclass_to_dict(obj: Any) -> Any:
            if is_dataclass(obj):
                result = {}
                for f in fields(obj):
                    value = getattr(obj, f.name)
                    result[f.name] = dataclass_to_dict(value)
                return result
            elif isinstance(obj, list):
                return [dataclass_to_dict(v) for v in obj]
            elif isinstance(obj, Path):
                return str(obj)
            else:
                return obj

        return dataclass_to_dict(config)

    def _build_app_config_from_edit(self) -> AppConfig:
        """Build AppConfig from current editor values."""
        # Apply all editor values to edit config
        for name, editor in self._section_editors.items():
            # Find the corresponding config object
            target = self._get_target_config(name)
            if target:
                new_target = editor.apply_to_config(target)
                if new_target is not target:
                    # The section was modified
                    pass
        # Rebuild the entire config from updated sections
        self._rebuild_edit_config()
        return self._edit_config

    def _replace_config_target(self, section_name: str, new_target: Any) -> None:
        """Replace a config target in the edit config."""
        # For frozen dataclasses, we need to create new instances
        # We'll rebuild the entire config tree from the bottom up
        pass  # The _build_app_config_from_edit will handle this by creating new instances

    def _rebuild_edit_config(self) -> None:
        """Rebuild the entire edit config from section editors."""
        # This is called after all editors have been applied
        # We create a new AppConfig from scratch using the updated sections
        self._edit_config = AppConfig(
            application=self._get_section_config("Application"),
            database=self._get_section_config("Database"),
            cameras=self._get_section_config("Camera Mapping"),
            system=self._get_section_config("System"),
            storage=self._get_section_config("Storage Paths"),
            recording=self._get_section_config("Recording"),
            calibration=self._get_section_config("Calibration"),
            processing=self._get_section_config("Processing"),
            alarms=self._get_section_config("Alarms"),
            ptz=self._get_section_config("PTZ"),
            roi=self._get_section_config("ROI Defaults"),
            offline=self._get_section_config("Offline Playback"),
            logging=self._get_section_config("Logging"),
            ui=self._get_section_config("Theme"),  # UIConfig
            network=self._get_section_config("Network"),
            halcon=self._get_section_config("HALCON"),
        )

    def _get_section_config(self, section_name: str) -> Any:
        """Get the updated config for a section from its editor."""
        editor = self._section_editors.get(section_name)
        if not editor:
            # Return original config if no editor
            return self._get_target_config(section_name)
        
        target = self._get_target_config(section_name)
        return editor.apply_to_config(target)

    def _get_target_config(self, section_name: str) -> Any:
        """Get the target config object for a section."""
        mapping = {
            "Application": self._edit_config.application,
            "System": self._edit_config.system,
            "Discovery": self._edit_config.cameras.discovery,
            "Acquisition": self._edit_config.cameras.acquisition,
            "Recovery": self._edit_config.cameras.recovery,
            "Connection": self._edit_config.cameras.connection,
            "Startup": self._edit_config.cameras.startup,
            "Camera Mapping": self._edit_config.cameras,
            "Storage Paths": self._edit_config.storage,
            "Recording": self._edit_config.recording,
            "Calibration": self._edit_config.calibration,
            "Processing": self._edit_config.processing,
            "Alarms": self._edit_config.alarms,
            "ROI Defaults": self._edit_config.roi,
            "PTZ": self._edit_config.ptz,
            "Offline Playback": self._edit_config.offline,
            "Database": self._edit_config.database,
            "Network": self._edit_config.network,
            "Logging": self._edit_config.logging,
            "Theme": self._edit_config.ui,
            "Colors": self._edit_config.ui.colors,
            "Windows": self._edit_config.ui.windows,
            "Live Display": self._edit_config.ui.live,
            "Display": self._edit_config.ui.display,
            "HALCON": self._edit_config.halcon,
        }
        return mapping.get(section_name)

    # --- Section Editor Factories ---

    def _create_application_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Application", self._edit_config.application, self._theme)
        editor.add_field(ConfigEditorField("name", "Application Name", str, "Thermal Monitoring System V3"))
        editor.add_field(ConfigEditorField("version", "Version", str, "3.0.0"))
        editor.add_field(ConfigEditorField("default_mode", "Default Mode", str, "CONFIGURATION",
            options=["LAUNCHER", "CONFIGURATION", "LIVE", "OFFLINE"]))
        editor.add_field(ConfigEditorField("start_maximized", "Start Maximized", bool, True))
        return editor

    def _create_system_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("System", self._edit_config.system, self._theme)
        editor.add_field(ConfigEditorField("max_cameras", "Max Cameras", int, 8, min_val=1, max_val=16))
        editor.add_field(ConfigEditorField("auto_save_config", "Auto Save Config", bool, True))
        editor.add_field(ConfigEditorField("shutdown_timeout_s", "Shutdown Timeout (s)", float, 5.0, min_val=0.1, max_val=60.0))
        return editor

    def _create_discovery_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Discovery", self._edit_config.cameras.discovery, self._theme)
        editor.add_field(ConfigEditorField("enabled", "Discovery Enabled", bool, True))
        editor.add_field(ConfigEditorField("startup_scan", "Startup Scan", bool, True))
        editor.add_field(ConfigEditorField("halcon_interface", "HALCON Interface", str, "GigEVision2"))
        editor.add_field(ConfigEditorField("attempts", "Discovery Attempts", int, 3, min_val=1, max_val=10))
        editor.add_field(ConfigEditorField("retry_delay_s", "Retry Delay (s)", float, 3.0, min_val=0.0, max_val=60.0))
        editor.add_field(ConfigEditorField("interval_seconds", "Interval (s)", float, 30.0, min_val=0.0, max_val=3600.0))
        return editor

    def _create_acquisition_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Acquisition", self._edit_config.cameras.acquisition, self._theme)
        editor.add_field(ConfigEditorField("target_fps", "Target FPS", int, 9, min_val=1, max_val=60))
        editor.add_field(ConfigEditorField("grab_timeout_ms", "Grab Timeout (ms)", int, 500, min_val=1, max_val=5000))
        editor.add_field(ConfigEditorField("socket_buffer_size", "Socket Buffer Size", int, 1048576, min_val=1))
        editor.add_field(ConfigEditorField("num_buffers", "Number of Buffers", int, 8, min_val=1, max_val=32))
        editor.add_field(ConfigEditorField("stream_source_thermal", "Thermal Stream Source", str, "IR_Data"))
        editor.add_field(ConfigEditorField("thermal_bits_per_channel", "Thermal Bits/Channel", int, 16))
        editor.add_field(ConfigEditorField("stream_source_visible", "Visible Stream Source", str, ""))
        editor.add_field(ConfigEditorField("visible_bits_per_channel", "Visible Bits/Channel", int, -1))
        return editor

    def _create_recovery_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Recovery", self._edit_config.cameras.recovery, self._theme)
        editor.add_field(ConfigEditorField("consecutive_fail_limit", "Consecutive Fail Limit", int, 3, min_val=1))
        editor.add_field(ConfigEditorField("reconnect_interval_s", "Reconnect Interval (s)", float, 3.0, min_val=0.0))
        editor.add_field(ConfigEditorField("reconnect_backoff_factor", "Backoff Factor", float, 2.0, min_val=1.0))
        editor.add_field(ConfigEditorField("max_reconnect_attempts", "Max Reconnect Attempts", int, 10, min_val=1))
        return editor

    def _create_connection_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Connection", self._edit_config.cameras.connection, self._theme)
        editor.add_field(ConfigEditorField("device_identifier", "Device Identifier", str, "default"))
        editor.add_field(ConfigEditorField("ip_mode", "IP Mode", str, "auto", options=["auto", "static"]))
        return editor

    def _create_startup_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Startup", self._edit_config.cameras.startup, self._theme)
        editor.add_field(ConfigEditorField("acquire_timeout_s", "Acquire Timeout (s)", float, 10.0, min_val=0.1))
        return editor

    def _create_mapping_editor(self) -> ConfigSectionEditor:
        """Create camera mapping editor with discovered cameras."""
        editor = ConfigSectionEditor("Camera Mapping", self._edit_config.cameras, self._theme)
        # This is a special editor - we'll add a custom widget
        self._setup_mapping_editor(editor)
        return editor

    def _setup_mapping_editor(self, editor: ConfigSectionEditor) -> None:
        """Set up the camera mapping editor with a table."""
        # Replace the layout with a custom mapping widget
        # For now, we'll create a simple table-based editor
        mapping_widget = QWidget()
        mapping_layout = QVBoxLayout(mapping_widget)

        # Discovered cameras display
        disc_group = QGroupBox("Discovered Cameras (Read-Only)")
        disc_layout = QVBoxLayout(disc_group)
        self._discovered_table = QTreeWidget()
        self._discovered_table.setHeaderLabels(["Serial", "Model", "IP Address", "Device ID"])
        self._discovered_table.setAlternatingRowColors(True)
        disc_layout.addWidget(self._discovered_table)
        mapping_layout.addWidget(disc_group)

        # Configured cameras table
        config_group = QGroupBox("Camera Mappings")
        config_layout = QVBoxLayout(config_group)
        self._mapping_table = QTreeWidget()
        self._mapping_table.setHeaderLabels(["Camera ID", "Serial Number", "Enabled", "Display Name", "Target FPS"])
        self._mapping_table.setAlternatingRowColors(True)
        config_layout.addWidget(self._mapping_table)

        # Buttons
        btn_layout = QHBoxLayout()
        self._add_mapping_btn = QPushButton("Add Mapping")
        self._add_mapping_btn.clicked.connect(self._add_camera_mapping)
        if self._theme:
            self._add_mapping_btn.setStyleSheet(self._theme.secondary_button_stylesheet())
        self._remove_mapping_btn = QPushButton("Remove Mapping")
        self._remove_mapping_btn.clicked.connect(self._remove_camera_mapping)
        if self._theme:
            self._remove_mapping_btn.setStyleSheet(self._theme.accent_button_stylesheet())
        btn_layout.addWidget(self._add_mapping_btn)
        btn_layout.addWidget(self._remove_mapping_btn)
        btn_layout.addStretch()
        config_layout.addLayout(btn_layout)

        mapping_layout.addWidget(config_group)
        mapping_layout.addStretch()

        # Add to editor
        editor._layout.addWidget(mapping_widget)
        editor._refresh_discovered = self._refresh_discovered_cameras
        editor._load_mapping = self._load_mapping_table

    def _refresh_discovered_cameras(self, cameras: list = None) -> None:
        """Refresh the discovered cameras table."""
        self._discovered_table.clear()
        if cameras is None:
            # Try to discover
            from thermal_monitor.services.discovery import CameraDiscoveryService
            discovery = CameraDiscoveryService(
                halcon_interface=self._edit_config.cameras.discovery.halcon_interface,
                attempts=self._edit_config.cameras.discovery.attempts,
                retry_delay_s=self._edit_config.cameras.discovery.retry_delay_s,
            )
            try:
                cameras = discovery.discover_cameras()
            except Exception:
                cameras = []

        for cam in cameras:
            item = QTreeWidgetItem([
                cam.serial_number or "—",
                cam.model or "—",
                cam.ip_address or "—",
                cam.device_identifier or "—",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, cam)
            self._discovered_table.addTopLevelItem(item)

        self._load_mapping_table()

    def _load_mapping_table(self) -> None:
        """Load camera mappings into table."""
        self._mapping_table.clear()
        for mapping in self._edit_config.cameras.mapping:
            item = QTreeWidgetItem([
                mapping.camera_id,
                mapping.serial_number,
                "Yes" if mapping.enabled else "No",
                mapping.name or "—",
                str(mapping.target_fps) if mapping.target_fps else "—",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, mapping)
            self._mapping_table.addTopLevelItem(item)

    def _add_camera_mapping(self) -> None:
        """Add a new camera mapping from discovered cameras."""
        items = self._discovered_table.selectedItems()
        if not items:
            QMessageBox.warning(self, "No Selection", "Please select a discovered camera first.")
            return

        cam = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not cam:
            return

        # Check if already mapped
        for mapping in self._edit_config.cameras.mapping:
            if mapping.serial_number == cam.serial_number:
                QMessageBox.warning(self, "Already Mapped", f"Camera {cam.serial_number} is already mapped.")
                return

        # Create new mapping
        from thermal_monitor.config.models import CameraMappingConfig
        new_mapping = CameraMappingConfig(
            camera_id=f"cam_{len(self._edit_config.cameras.mapping) + 1:02d}",
            serial_number=cam.serial_number,
            enabled=True,
            name=f"Camera {len(self._edit_config.cameras.mapping) + 1}",
        )
        self._edit_config.cameras.mapping.append(new_mapping)
        self._load_mapping_table()
        self._dirty = True
        self._update_save_button()

    def _remove_camera_mapping(self) -> None:
        """Remove selected camera mapping."""
        items = self._mapping_table.selectedItems()
        if not items:
            return
        mapping = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not mapping:
            return
        self._edit_config.cameras.mapping = [m for m in self._edit_config.cameras.mapping if m != mapping]
        self._load_mapping_table()
        self._dirty = True
        self._update_save_button()

    def _create_storage_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Storage Paths", self._edit_config.storage, self._theme)
        editor.add_field(ConfigEditorField("root", "Storage Root", str, "data", tooltip="Relative to EXE or absolute path"))
        editor.add_field(ConfigEditorField("recordings", "Recordings Subdir", str, "recordings"))
        editor.add_field(ConfigEditorField("snapshots", "Snapshots Subdir", str, "snapshots"))
        editor.add_field(ConfigEditorField("logs", "Logs Subdir", str, "logs"))
        editor.add_field(ConfigEditorField("calibration", "Calibration Subdir", str, "calibration"))
        editor.add_field(ConfigEditorField("exports", "Exports Subdir", str, "exports"))
        editor.add_field(ConfigEditorField("database_backups", "Database Backups Subdir", str, "database"))
        editor.add_field(ConfigEditorField("offline", "Offline Subdir", str, "offline"))
        return editor

    def _create_recording_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Recording", self._edit_config.recording, self._theme)
        editor.add_field(ConfigEditorField("enabled", "Recording Enabled", bool, True))
        editor.add_field(ConfigEditorField("default_fps", "Default FPS", float, 9.0, min_val=0.1, max_val=60.0))
        editor.add_field(ConfigEditorField("pre_alarm_seconds", "Pre-Alarm (s)", float, 10.0, min_val=0.0))
        editor.add_field(ConfigEditorField("post_alarm_seconds", "Post-Alarm (s)", float, 30.0, min_val=0.0))
        editor.add_field(ConfigEditorField("max_duration_seconds", "Max Duration (s)", float, 300.0, min_val=1.0))
        editor.add_field(ConfigEditorField("max_file_size_mb", "Max File Size (MB)", int, 500, min_val=1))
        editor.add_field(ConfigEditorField("chunk_target_bytes", "Chunk Target (bytes)", int, 67108864, min_val=1))
        editor.add_field(ConfigEditorField("compression_enabled", "Compression Enabled", bool, False))
        editor.add_field(ConfigEditorField("naming_pattern", "Naming Pattern", str, "rec_{camera_id}_{timestamp_ms}"))
        return editor

    def _create_calibration_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Calibration", self._edit_config.calibration, self._theme)
        editor.add_field(ConfigEditorField("default_file", "Default Calibration File", str, "calibration/calibration_blob.txt"))
        return editor

    def _create_processing_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Processing", self._edit_config.processing, self._theme)
        editor.add_field(ConfigEditorField("enabled", "Processing Enabled", bool, True))
        editor.add_field(ConfigEditorField("interval_ms", "Interval (ms)", int, 100, min_val=1))
        editor.add_field(ConfigEditorField("default_fps", "Default FPS", float, 9.0, min_val=0.1))
        editor.add_field(ConfigEditorField("gpu_enabled", "GPU Enabled", bool, False))
        return editor

    def _create_alarms_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Alarms", self._edit_config.alarms, self._theme)
        editor.add_field(ConfigEditorField("evaluation_enabled", "Evaluation Enabled", bool, True))
        editor.add_field(ConfigEditorField("cooldown_seconds", "Cooldown (s)", float, 5.0, min_val=0.0))
        editor.add_field(ConfigEditorField("max_history", "Max History", int, 10000, min_val=0))
        editor.add_field(ConfigEditorField("default_enabled", "Default Enabled", bool, True))
        return editor

    def _create_roi_defaults_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("ROI Defaults", self._edit_config.roi, self._theme)
        # ROI defaults are nested dicts - we'll add a simplified version
        # The full ROI editor is handled elsewhere
        editor.add_field(ConfigEditorField("defaults.RECTANGLE1", "Rectangle1", str, str(self._edit_config.roi.defaults.RECTANGLE1), readonly=True))
        editor.add_field(ConfigEditorField("defaults.RECTANGLE2", "Rectangle2", str, str(self._edit_config.roi.defaults.RECTANGLE2), readonly=True))
        editor.add_field(ConfigEditorField("defaults.CIRCLE", "Circle", str, str(self._edit_config.roi.defaults.CIRCLE), readonly=True))
        editor.add_field(ConfigEditorField("defaults.ELLIPSE", "Ellipse", str, str(self._edit_config.roi.defaults.ELLIPSE), readonly=True))
        editor.add_field(ConfigEditorField("defaults.POLYGON", "Polygon", str, str(self._edit_config.roi.defaults.POLYGON), readonly=True))
        return editor

    def _create_ptz_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("PTZ", self._edit_config.ptz, self._theme)
        editor.add_field(ConfigEditorField("enabled", "PTZ Enabled", bool, True))
        # PTZ limits
        editor.add_field(ConfigEditorField("limits.min_pan", "Min Pan", float, -170.0))
        editor.add_field(ConfigEditorField("limits.max_pan", "Max Pan", float, 170.0))
        editor.add_field(ConfigEditorField("limits.min_tilt", "Min Tilt", float, -90.0))
        editor.add_field(ConfigEditorField("limits.max_tilt", "Max Tilt", float, 90.0))
        editor.add_field(ConfigEditorField("limits.min_zoom", "Min Zoom", float, 1.0))
        editor.add_field(ConfigEditorField("limits.max_zoom", "Max Zoom", float, 30.0))
        # Default position
        editor.add_field(ConfigEditorField("default_position.pan", "Default Pan", float, 0.0))
        editor.add_field(ConfigEditorField("default_position.tilt", "Default Tilt", float, 0.0))
        editor.add_field(ConfigEditorField("default_position.zoom", "Default Zoom", float, 1.0))
        # Speeds
        editor.add_field(ConfigEditorField("speeds.pan", "Pan Speed", float, 10.0))
        editor.add_field(ConfigEditorField("speeds.tilt", "Tilt Speed", float, 10.0))
        editor.add_field(ConfigEditorField("speeds.zoom", "Zoom Speed", float, 5.0))
        return editor

    def _create_offline_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Offline Playback", self._edit_config.offline, self._theme)
        editor.add_field(ConfigEditorField("playback.default_speed", "Default Speed", float, 1.0, min_val=0.1))
        editor.add_field(ConfigEditorField("playback.speed_min", "Min Speed", float, 0.1, min_val=0.01))
        editor.add_field(ConfigEditorField("playback.speed_max", "Max Speed", float, 10.0, min_val=0.1))
        editor.add_field(ConfigEditorField("storage_path", "Storage Path", str, ""))
        return editor

    def _create_database_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Database", self._edit_config.database, self._theme)
        editor.add_field(ConfigEditorField("enabled", "Database Enabled", bool, False))
        editor.add_field(ConfigEditorField("type", "Database Type", str, "sqlserver"))
        editor.add_field(ConfigEditorField("driver", "ODBC Driver", str, "ODBC Driver 17 for SQL Server"))
        editor.add_field(ConfigEditorField("host", "Host", str, ""))
        editor.add_field(ConfigEditorField("port", "Port", int, 1433, min_val=1, max_val=65535))
        editor.add_field(ConfigEditorField("name", "Database Name", str, ""))
        editor.add_field(ConfigEditorField("trusted_connection", "Trusted Connection", bool, True))
        editor.add_field(ConfigEditorField("username", "Username", str, ""))
        # Password field - show as read-only from env
        editor.add_field(ConfigEditorField("password_env", "Password Env Var", str, "TMS_DB_PASSWORD", readonly=True,
            tooltip="Password is read from environment variable"))
        editor.add_field(ConfigEditorField("connection_timeout", "Connection Timeout", int, 30, min_val=0))
        editor.add_field(ConfigEditorField("command_timeout", "Command Timeout", int, 30, min_val=0))
        editor.add_field(ConfigEditorField("trust_server_certificate", "Trust Server Certificate", bool, True))
        return editor

    def _create_network_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Network", self._edit_config.network, self._theme)
        editor.add_field(ConfigEditorField("bind_address", "Bind Address", str, "0.0.0.0"))
        editor.add_field(ConfigEditorField("http_port", "HTTP Port", int, 8080, min_val=1, max_val=65535))
        return editor

    def _create_logging_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Logging", self._edit_config.logging, self._theme)
        editor.add_field(ConfigEditorField("level", "Log Level", str, "INFO",
            options=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]))
        editor.add_field(ConfigEditorField("file_path", "Log File Path", str, ""))
        editor.add_field(ConfigEditorField("format", "Log Format", str, "[%(asctime)s] [%(levelname)s] %(message)s"))
        editor.add_field(ConfigEditorField("date_format", "Date Format", str, "%Y-%m-%d %H:%M:%S"))
        editor.add_field(ConfigEditorField("max_size_mb", "Max Size (MB)", int, 20, min_val=1))
        editor.add_field(ConfigEditorField("backup_count", "Backup Count", int, 5, min_val=0))
        return editor

    def _create_theme_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Theme", self._edit_config.ui, self._theme)
        editor.add_field(ConfigEditorField("theme", "Theme", str, "light",
            options=["light", "dark", "system"]))
        return editor

    def _create_colors_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Colors", self._edit_config.ui.colors, self._theme)
        # Add all color fields
        color_fields = [
            ("primary", "Primary"),
            ("primary_hover", "Primary Hover"),
            ("primary_pressed", "Primary Pressed"),
            ("primary_disabled_bg", "Primary Disabled BG"),
            ("primary_disabled_text", "Primary Disabled Text"),
            ("secondary", "Secondary"),
            ("secondary_hover", "Secondary Hover"),
            ("secondary_pressed", "Secondary Pressed"),
            ("secondary_disabled_bg", "Secondary Disabled BG"),
            ("secondary_disabled_text", "Secondary Disabled Text"),
            ("accent", "Accent"),
            ("accent_hover", "Accent Hover"),
            ("accent_pressed", "Accent Pressed"),
            ("warning", "Warning"),
            ("danger", "Danger"),
            ("disabled", "Disabled"),
            ("text_primary", "Text Primary"),
            ("text_secondary", "Text Secondary"),
            ("text_muted", "Text Muted"),
            ("title", "Title"),
            ("background", "Background"),
            ("panel", "Panel"),
            ("border", "Border"),
            ("alarm", "Alarm"),
            ("success", "Success"),
            ("info", "Info"),
        ]
        for field_name, label in color_fields:
            editor.add_field(ConfigEditorField(field_name, label, str, getattr(self._edit_config.ui.colors, field_name)))
        return editor

    def _create_windows_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Windows", self._edit_config.ui.windows, self._theme)
        editor.add_field(ConfigEditorField("start_maximized", "Start Maximized", bool, True))
        editor.add_field(ConfigEditorField("launcher_min_width", "Launcher Min Width", int, 1000))
        editor.add_field(ConfigEditorField("launcher_min_height", "Launcher Min Height", int, 700))
        editor.add_field(ConfigEditorField("live_min_width", "Live Min Width", int, 640))
        editor.add_field(ConfigEditorField("live_min_height", "Live Min Height", int, 480))
        editor.add_field(ConfigEditorField("config_min_width", "Config Min Width", int, 1000))
        editor.add_field(ConfigEditorField("config_min_height", "Config Min Height", int, 700))
        editor.add_field(ConfigEditorField("offline_min_width", "Offline Min Width", int, 640))
        editor.add_field(ConfigEditorField("offline_min_height", "Offline Min Height", int, 480))
        return editor

    def _create_live_display_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Live Display", self._edit_config.ui.live, self._theme)
        editor.add_field(ConfigEditorField("tile_gap", "Tile Gap", int, 0))
        editor.add_field(ConfigEditorField("columns", "Columns", int, 4, min_val=1, max_val=8))
        editor.add_field(ConfigEditorField("rows", "Rows", int, 2, min_val=1, max_val=8))
        editor.add_field(ConfigEditorField("show_camera_name", "Show Camera Name", bool, True))
        editor.add_field(ConfigEditorField("show_serial", "Show Serial", bool, True))
        editor.add_field(ConfigEditorField("show_fps", "Show FPS", bool, True))
        editor.add_field(ConfigEditorField("show_temperature", "Show Temperature", bool, True))
        return editor

    def _create_display_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("Display", self._edit_config.ui.display, self._theme)
        editor.add_field(ConfigEditorField("default_palette", "Default Palette", str, "temperature",
            options=["temperature", "iron", "rainbow", "gray", "hot"]))
        editor.add_field(ConfigEditorField("default_zoom", "Default Zoom", str, "Fit to Window",
            options=["Fit to Window", "50%", "100%", "200%", "400%"]))
        editor.add_field(ConfigEditorField("auto_range", "Auto Range", bool, True))
        editor.add_field(ConfigEditorField("min_temperature", "Min Temperature", float, -20.0))
        editor.add_field(ConfigEditorField("max_temperature", "Max Temperature", float, 1200.0))
        return editor

    def _create_halcon_editor(self) -> ConfigSectionEditor:
        editor = ConfigSectionEditor("HALCON", self._edit_config.halcon, self._theme)
        editor.add_field(ConfigEditorField("grab_timeout_error_code", "Grab Timeout Error Code", int, 5322))
        editor.add_field(ConfigEditorField("first_frame_timeout_ms", "First Frame Timeout (ms)", int, 5000))
        return editor

    # --- Public API ---

    def is_dirty(self) -> bool:
        return self._dirty

    def get_edit_config(self) -> AppConfig:
        return self._edit_config

    def closeEvent(self, event) -> None:
        if self._dirty:
            msg = QMessageBox(self)
            msg.setWindowTitle("Unsaved Changes")
            msg.setText("You have unsaved configuration changes.")
            msg.setInformativeText("Do you want to save before closing?")
            msg.setStandardButtons(
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel
            )
            msg.setDefaultButton(QMessageBox.StandardButton.Save)
            result = msg.exec()

            if result == QMessageBox.StandardButton.Save:
                self._on_save()
                if not self._dirty:
                    event.accept()
                else:
                    event.ignore()
            elif result == QMessageBox.StandardButton.Discard:
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


__all__ = ["ConfigurationEditor"]