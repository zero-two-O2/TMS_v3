"""
ui.windows.configuration_window -- Configuration mode window.

ThermoView-style configuration workspace for one selected camera:
- Camera selector
- Live thermal image with temperature display controls
- Camera information panel
- Acquisition controls
- ROI/Analysis workspace
- Alarm configuration
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QGroupBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QSpinBox,
    QDoubleSpinBox,
    QCheckBox,
    QTreeWidget,
    QTreeWidgetItem,
    QScrollArea,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QMessageBox,
    QStatusBar,
    QFrame,
    QLineEdit,
)
from PyQt6.QtGui import QFont

import numpy as np

from thermal_monitor.core.modes import ApplicationMode
from thermal_monitor.core.models import (
    CameraConfig,
    CameraIdentity,
    AnalysisConfig,
    ROIConfig,
    ROIGeometry,
    ROIShape,
    TemperatureLimits,
    TemperatureUnit,
    AlarmRule,
    AlarmCondition,
    AlarmSeverity,
    PositionROIAssociation,
)
from thermal_monitor.processing import ProcessingResult
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.runtime import CameraRuntimeService
from thermal_monitor.services.observer import ObserverService
from thermal_monitor.ui.modes.observer_image import LiveThermalWidget
from thermal_monitor.ui.theme import ThemeManager


_UNIT_SYMBOLS = {
    TemperatureUnit.CELSIUS: "°C",
    TemperatureUnit.FAHRENHEIT: "°F",
    TemperatureUnit.KELVIN: "K",
}


class ConfigurationModeWidget(QWidget):
    """Main widget for Configuration mode - single camera detailed configuration."""

    def __init__(
        self,
        config_service: ConfigurationService,
        mode_service: ModeService,
        runtime_service: CameraRuntimeService | None = None,
        theme_manager: Optional[ThemeManager] = None,
    ) -> None:
        super().__init__()

        self._config_service = config_service
        self._mode_service = mode_service
        self._runtime_service = runtime_service
        self._theme = theme_manager
        self._selected_camera_id: str | None = None
        self._observer: ObserverService | None = None
        self._latest_result: ProcessingResult | None = None

        self._setup_ui()
        self._connect_signals()
        self._load_initial_data()

    def _setup_ui(self) -> None:
        """Set up the ThermoView-style UI layout."""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)

        # --- Top: Camera selector toolbar ---
        self._create_camera_selector(main_layout)

        # --- Main content area: splitter ---
        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(main_splitter, 1)

        # Left side: Image + controls
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        # Camera/Acquisition toolbar
        self._create_acquisition_toolbar(left_layout)

        # Image display area
        image_splitter = QSplitter(Qt.Orientation.Vertical)
        left_layout.addWidget(image_splitter, 1)

        # Live thermal image
        self._image_widget = LiveThermalWidget()
        self._image_widget.setMinimumSize(480, 360)
        image_splitter.addWidget(self._image_widget)

        # Image info panel (below image)
        self._create_image_info_panel(image_splitter)

        # Right side: Temperature/Scale + Analysis/ROI
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        # Temperature scale panel
        self._create_temperature_scale_panel(right_layout)

        # Analysis/ROI panel
        self._create_analysis_panel(right_layout)

        main_splitter.addWidget(left_widget)
        main_splitter.addWidget(right_widget)
        main_splitter.setSizes([800, 400])

        # Status bar at bottom
        self._create_status_bar(main_layout)

    def _create_camera_selector(self, parent_layout: QVBoxLayout) -> None:
        """Create camera selector toolbar."""
        selector_group = QGroupBox("Camera Selection")
        selector_layout = QHBoxLayout(selector_group)
        selector_layout.setSpacing(8)

        self._prev_btn = QPushButton("◀ Previous")
        self._prev_btn.clicked.connect(self._select_prev_camera)
        self._prev_btn.setEnabled(False)
        if self._theme:
            self._prev_btn.setStyleSheet(self._theme.secondary_button_stylesheet())

        self._camera_combo = QComboBox()
        self._camera_combo.setMinimumWidth(300)
        self._camera_combo.currentIndexChanged.connect(self._on_camera_selected)

        self._next_btn = QPushButton("Next ▶")
        self._next_btn.clicked.connect(self._select_next_camera)
        self._next_btn.setEnabled(False)
        if self._theme:
            self._next_btn.setStyleSheet(self._theme.secondary_button_stylesheet())

        selector_layout.addWidget(self._prev_btn)
        if self._theme:
            selector_layout.addWidget(QLabel("Camera:"))
        else:
            selector_layout.addWidget(QLabel("Camera:"))
        selector_layout.addWidget(self._camera_combo, 1)
        selector_layout.addWidget(self._next_btn)
        selector_layout.addStretch()

        parent_layout.addWidget(selector_group)

    def _create_acquisition_toolbar(self, parent_layout: QVBoxLayout) -> None:
        """Create acquisition controls toolbar."""
        acq_group = QGroupBox("Acquisition")
        acq_layout = QFormLayout(acq_group)
        acq_layout.setSpacing(6)

        self._acq_camera_label = QLabel("—")
        self._acq_state_label = QLabel("STOPPED")
        state_style = "font-weight: bold;"
        if self._theme:
            state_style += f" color: {self._theme.disabled_text()};"
        else:
            state_style += " color: #757575;"
        self._acq_state_label.setStyleSheet(state_style)

        self._target_fps_spin = QSpinBox()
        self._target_fps_spin.setRange(1, 60)
        self._target_fps_spin.setValue(9)

        self._reconnect_spin = QDoubleSpinBox()
        self._reconnect_spin.setRange(0.1, 60.0)
        self._reconnect_spin.setSuffix(" s")
        self._reconnect_spin.setDecimals(2)
        self._reconnect_spin.setValue(3.0)

        self._nuc_duration_spin = QDoubleSpinBox()
        self._nuc_duration_spin.setRange(0.1, 30.0)
        self._nuc_duration_spin.setSuffix(" s")
        self._nuc_duration_spin.setDecimals(2)
        self._nuc_duration_spin.setValue(1.0)

        self._grab_timeout_spin = QDoubleSpinBox()
        self._grab_timeout_spin.setRange(0.1, 60.0)
        self._grab_timeout_spin.setSuffix(" s")
        self._grab_timeout_spin.setDecimals(2)
        self._grab_timeout_spin.setValue(0.5)

        self._start_btn = QPushButton("Start")
        self._start_btn.clicked.connect(self._on_start_acquisition)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.clicked.connect(self._on_stop_acquisition)
        self._stop_btn.setEnabled(False)
        self._reconnect_btn = QPushButton("Reconnect")
        self._reconnect_btn.clicked.connect(self._on_reconnect)

        if self._theme:
            self._start_btn.setStyleSheet(self._theme.primary_button_stylesheet())
            self._stop_btn.setStyleSheet(self._theme.secondary_button_stylesheet())
            self._reconnect_btn.setStyleSheet(self._theme.accent_button_stylesheet())

        btn_layout = QHBoxLayout()
        btn_layout.addWidget(self._start_btn)
        btn_layout.addWidget(self._stop_btn)
        btn_layout.addWidget(self._reconnect_btn)

        acq_layout.addRow("Camera:", self._acq_camera_label)
        acq_layout.addRow("State:", self._acq_state_label)
        acq_layout.addRow("Target FPS:", self._target_fps_spin)
        acq_layout.addRow("Reconnect Interval:", self._reconnect_spin)
        acq_layout.addRow("NUC Duration:", self._nuc_duration_spin)
        acq_layout.addRow("Grab Timeout:", self._grab_timeout_spin)
        acq_layout.addRow("", btn_layout)

        parent_layout.addWidget(acq_group)

    def _create_image_info_panel(self, parent_splitter: QSplitter) -> None:
        """Create image info panel (like ThermoView's Image Info)."""
        info_widget = QWidget()
        info_layout = QVBoxLayout(info_widget)
        info_layout.setContentsMargins(4, 4, 4, 4)
        info_layout.setSpacing(4)

        # Frame info
        frame_group = QGroupBox("Frame Information")
        frame_form = QFormLayout(frame_group)
        frame_form.setSpacing(4)

        self._info_camera_id = QLabel("—")
        self._info_serial = QLabel("—")
        self._info_model = QLabel("—")
        self._info_ip = QLabel("—")
        self._info_frame_size = QLabel("—")
        self._info_acq_fps = QLabel("—")
        self._info_disp_fps = QLabel("—")
        self._info_sequence = QLabel("—")
        self._info_timestamp = QLabel("—")
        self._info_cal_range = QLabel("—")
        self._info_emissivity = QLabel("—")
        self._info_ambient = QLabel("—")
        self._info_proc_time = QLabel("—")

        frame_form.addRow("Camera ID:", self._info_camera_id)
        frame_form.addRow("Serial:", self._info_serial)
        frame_form.addRow("Model:", self._info_model)
        frame_form.addRow("IP Address:", self._info_ip)
        frame_form.addRow("Frame Size:", self._info_frame_size)
        frame_form.addRow("Acquisition FPS:", self._info_acq_fps)
        frame_form.addRow("Display FPS:", self._info_disp_fps)
        frame_form.addRow("Sequence:", self._info_sequence)
        frame_form.addRow("Timestamp:", self._info_timestamp)
        frame_form.addRow("Calibration Range:", self._info_cal_range)
        frame_form.addRow("Emissivity:", self._info_emissivity)
        frame_form.addRow("Ambient Temp:", self._info_ambient)
        frame_form.addRow("Processing Time:", self._info_proc_time)

        info_layout.addWidget(frame_group)

        # Camera info (read-only identity)
        cam_group = QGroupBox("Camera Identity (from Discovery)")
        cam_form = QFormLayout(cam_group)
        cam_form.setSpacing(4)

        self._cam_vendor = QLabel("—")
        self._cam_firmware = QLabel("—")
        self._cam_user_name = QLabel("—")
        self._cam_device_id = QLabel("—")

        cam_form.addRow("Vendor:", self._cam_vendor)
        cam_form.addRow("Firmware:", self._cam_firmware)
        cam_form.addRow("User Name:", self._cam_user_name)
        cam_form.addRow("Device ID:", self._cam_device_id)

        info_layout.addWidget(cam_group)
        info_layout.addStretch()

        parent_splitter.addWidget(info_widget)

    def _create_temperature_scale_panel(self, parent_layout: QVBoxLayout) -> None:
        """Create temperature scale / display controls panel."""
        scale_group = QGroupBox("Temperature Scale")
        scale_layout = QFormLayout(scale_group)
        scale_layout.setSpacing(6)

        # Palette
        self._palette_combo = QComboBox()
        self._palette_combo.addItems(["temperature", "iron", "rainbow", "gray", "hot"])
        self._palette_combo.currentTextChanged.connect(self._on_palette_changed)

        # Range mode
        self._auto_range_check = QCheckBox("Auto Range")
        self._auto_range_check.setChecked(True)
        self._auto_range_check.toggled.connect(self._on_auto_range_toggled)

        # Custom range
        self._custom_min_spin = QDoubleSpinBox()
        self._custom_min_spin.setRange(-273.15, 2000.0)
        self._custom_min_spin.setDecimals(1)
        self._custom_min_spin.setSuffix(" °C")
        self._custom_min_spin.setValue(20.0)
        self._custom_min_spin.setEnabled(False)

        self._custom_max_spin = QDoubleSpinBox()
        self._custom_max_spin.setRange(-273.15, 2000.0)
        self._custom_max_spin.setDecimals(1)
        self._custom_max_spin.setSuffix(" °C")
        self._custom_max_spin.setValue(80.0)
        self._custom_max_spin.setEnabled(False)

        self._apply_range_btn = QPushButton("Apply Range")
        self._apply_range_btn.clicked.connect(self._on_apply_range)
        self._apply_range_btn.setEnabled(False)

        # Zoom
        self._zoom_combo = QComboBox()
        self._zoom_combo.addItems(["Fit to Window", "50%", "100%", "200%", "400%"])
        self._zoom_combo.setCurrentIndex(0)
        self._zoom_combo.currentTextChanged.connect(self._on_zoom_changed)

        # Point temperature readout
        self._cursor_temp_label = QLabel("Cursor: — °C")
        self._cursor_temp_label.setStyleSheet("font-family: monospace; font-size: 12px;")

        scale_layout.addRow("Palette:", self._palette_combo)
        scale_layout.addRow("", self._auto_range_check)
        scale_layout.addRow("Custom Min:", self._custom_min_spin)
        scale_layout.addRow("Custom Max:", self._custom_max_spin)
        scale_layout.addRow("", self._apply_range_btn)
        scale_layout.addRow("Zoom:", self._zoom_combo)
        scale_layout.addRow("", self._cursor_temp_label)

        parent_layout.addWidget(scale_group)

    def _create_analysis_panel(self, parent_layout: QVBoxLayout) -> None:
        """Create analysis/ROI workspace panel."""
        # Use tab widget for different analysis views
        self._analysis_tabs = QTabWidget()
        parent_layout.addWidget(self._analysis_tabs, 1)

        # ROI List tab
        self._roi_list_tab = self._create_roi_list_tab()
        self._analysis_tabs.addTab(self._roi_list_tab, "ROIs")

        # Alarm Configuration tab
        self._alarm_tab = self._create_alarm_tab()
        self._analysis_tabs.addTab(self._alarm_tab, "Alarms")

        # Statistics tab
        self._stats_tab = self._create_stats_tab()
        self._analysis_tabs.addTab(self._stats_tab, "Statistics")

    def _create_roi_list_tab(self) -> QWidget:
        """Create ROI list and editor tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # ROI toolbar
        roi_toolbar = QHBoxLayout()
        self._add_roi_btn = QPushButton("Add ROI")
        self._add_roi_btn.clicked.connect(self._on_add_roi)
        self._edit_roi_btn = QPushButton("Edit ROI")
        self._edit_roi_btn.clicked.connect(self._on_edit_roi)
        self._edit_roi_btn.setEnabled(False)
        self._delete_roi_btn = QPushButton("Delete ROI")
        self._delete_roi_btn.clicked.connect(self._on_delete_roi)
        self._delete_roi_btn.setEnabled(False)
        roi_toolbar.addWidget(self._add_roi_btn)
        roi_toolbar.addWidget(self._edit_roi_btn)
        roi_toolbar.addWidget(self._delete_roi_btn)
        roi_toolbar.addStretch()
        layout.addLayout(roi_toolbar)

        # ROI tree
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
        self._roi_tree.itemSelectionChanged.connect(self._on_roi_selection_changed)
        layout.addWidget(self._roi_tree, 1)

        # ROI detail editor (initially hidden)
        self._roi_editor = self._create_roi_editor()
        self._roi_editor.setVisible(False)
        layout.addWidget(self._roi_editor)

        return tab

    def _create_roi_editor(self) -> QWidget:
        """Create ROI geometry and limits editor."""
        editor = QWidget()
        layout = QVBoxLayout(editor)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        group = QGroupBox("ROI Editor")
        group_layout = QVBoxLayout(group)

        # Shape info (read-only)
        shape_form = QFormLayout()
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
        group_layout.addWidget(self._editor_save_btn)

        group_layout.addStretch()
        layout.addWidget(group)

        return editor

    def _create_alarm_tab(self) -> QWidget:
        """Create alarm configuration tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Alarm toolbar
        alarm_toolbar = QHBoxLayout()
        self._add_alarm_btn = QPushButton("Add Alarm Rule")
        self._add_alarm_btn.clicked.connect(self._on_add_alarm)
        self._edit_alarm_btn = QPushButton("Edit Alarm")
        self._edit_alarm_btn.clicked.connect(self._on_edit_alarm)
        self._edit_alarm_btn.setEnabled(False)
        self._delete_alarm_btn = QPushButton("Delete Alarm")
        self._delete_alarm_btn.clicked.connect(self._on_delete_alarm)
        self._delete_alarm_btn.setEnabled(False)
        alarm_toolbar.addWidget(self._add_alarm_btn)
        alarm_toolbar.addWidget(self._edit_alarm_btn)
        alarm_toolbar.addWidget(self._delete_alarm_btn)
        alarm_toolbar.addStretch()
        layout.addLayout(alarm_toolbar)

        # Alarm tree
        self._alarm_tree = QTreeWidget()
        self._alarm_tree.setHeaderLabels(["Rule ID", "ROI", "Condition", "Threshold", "Severity", "Enabled"])
        self._alarm_tree.setColumnWidth(0, 100)
        self._alarm_tree.setColumnWidth(1, 100)
        self._alarm_tree.setColumnWidth(2, 100)
        self._alarm_tree.setColumnWidth(3, 100)
        self._alarm_tree.setColumnWidth(4, 80)
        self._alarm_tree.setColumnWidth(5, 60)
        self._alarm_tree.itemSelectionChanged.connect(self._on_alarm_selection_changed)
        layout.addWidget(self._alarm_tree, 1)

        # Alarm editor
        self._alarm_editor = self._create_alarm_editor()
        self._alarm_editor.setVisible(False)
        layout.addWidget(self._alarm_editor)

        return tab

    def _create_alarm_editor(self) -> QWidget:
        """Create alarm rule editor."""
        editor = QWidget()
        layout = QVBoxLayout(editor)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        group = QGroupBox("Alarm Rule Editor")
        form = QFormLayout(group)

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
        form.addRow("", self._alarm_editor_save_btn)

        self._alarm_editor_condition.currentTextChanged.connect(self._on_alarm_condition_changed)

        layout.addWidget(group)
        layout.addStretch()

        return editor

    def _create_stats_tab(self) -> QWidget:
        """Create statistics tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)

        # Overall stats
        self._overall_group = QGroupBox("Overall Statistics")
        overall_form = QFormLayout(self._overall_group)
        self._overall_min = QLabel("—")
        self._overall_max = QLabel("—")
        self._overall_mean = QLabel("—")
        self._overall_stddev = QLabel("—")
        overall_form.addRow("Min:", self._overall_min)
        overall_form.addRow("Max:", self._overall_max)
        overall_form.addRow("Mean:", self._overall_mean)
        overall_form.addRow("Std Dev:", self._overall_stddev)
        layout.addWidget(self._overall_group)

        # Per-ROI stats table
        self._roi_stats_table = QTableWidget(0, 7)
        self._roi_stats_table.setHorizontalHeaderLabels(["ROI", "Name", "Min", "Max", "Mean", "Std Dev", "Range"])
        self._roi_stats_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._roi_stats_table, 1)

        return tab

    def _create_status_bar(self, parent_layout: QVBoxLayout) -> None:
        """Create status bar at bottom."""
        status_frame = QFrame()
        status_frame.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Sunken)
        status_layout = QHBoxLayout(status_frame)
        status_layout.setContentsMargins(8, 4, 8, 4)

        self._status_fps = QLabel("FPS: —")
        self._status_proc = QLabel("Processing: — ms")
        self._status_conn = QLabel("Connection: —")
        self._status_frames = QLabel("Frames: 0")

        status_layout.addWidget(self._status_fps)
        status_layout.addWidget(QLabel("|"))
        status_layout.addWidget(self._status_proc)
        status_layout.addWidget(QLabel("|"))
        status_layout.addWidget(self._status_conn)
        status_layout.addWidget(QLabel("|"))
        status_layout.addWidget(self._status_frames)
        status_layout.addStretch()

        parent_layout.addWidget(status_frame)

    def _connect_signals(self) -> None:
        """Connect internal signals."""
        self._config_service.add_camera_change_callback(self._on_camera_config_changed)
        self._config_service.add_analysis_change_callback(self._on_analysis_config_changed)

        # Stats timer
        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._update_stats)
        self._stats_timer.start(1000)

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
            self._switch_camera(camera_id)
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

    def _switch_camera(self, camera_id: str) -> None:
        """Switch to a different camera."""
        # Stop previous camera's observer
        if self._observer is not None:
            self._observer.stop()
            self._observer = None

        self._selected_camera_id = camera_id
        self._load_camera_config(camera_id)
        self._update_acquisition_controls(camera_id)

        # Start observer for new camera
        if self._runtime_service is not None:
            analysis = self._config_service.get_analysis_config(camera_id)
            if analysis is None:
                analysis = AnalysisConfig(camera_id=camera_id)

            try:
                self._observer = self._runtime_service.start_observer(camera_id, analysis_config=analysis)
                self._observer.result_ready.connect(self._on_processing_result, Qt.ConnectionType.QueuedConnection)
                self._observer.error_occurred.connect(self._on_observer_error, Qt.ConnectionType.QueuedConnection)
                self._acq_state_label.setText("RUNNING")
                self._acq_state_label.setStyleSheet("color: #2E7D32; font-weight: bold;")
                self._start_btn.setEnabled(False)
                self._stop_btn.setEnabled(True)
            except Exception as exc:
                self._acq_state_label.setText("ERROR")
                self._acq_state_label.setStyleSheet("color: #D32F2F; font-weight: bold;")
                QMessageBox.warning(self, "Observer Error", f"Failed to start observer: {exc}")

    def _load_camera_config(self, camera_id: str) -> None:
        """Load configuration for the selected camera into all panels."""
        config = self._config_service.get_camera_config(camera_id)
        if not config:
            self._clear_all_panels()
            return

        identity = config.identity
        metadata = config.metadata or {}

        # Update camera selector label
        self._acq_camera_label.setText(f"{identity.serial_number} ({identity.model})")

        # Update image info panel
        self._info_camera_id.setText(identity.camera_id)
        self._info_serial.setText(identity.serial_number)
        self._info_model.setText(identity.model or "(not provided)")
        self._info_ip.setText(metadata.get("ip_address", "(not provided)"))
        self._cam_vendor.setText(identity.vendor or "(not provided)")
        self._cam_firmware.setText(identity.firmware or "(not provided)")
        self._cam_user_name.setText(identity.user_name or "(not provided)")
        self._cam_device_id.setText(metadata.get("device_identifier", "(not provided)"))

        # Update acquisition controls from metadata
        self._target_fps_spin.setValue(int(metadata.get("frame_rate", 9)))
        self._reconnect_spin.setValue(float(metadata.get("reconnect_interval_s", 3.0)))
        self._nuc_duration_spin.setValue(float(metadata.get("nuc_duration_s", 1.0)))
        self._grab_timeout_spin.setValue(float(metadata.get("grab_timeout_ms", 500)) / 1000.0)

        # Update ROI list
        self._refresh_roi_list(camera_id)

        # Update alarm list
        self._refresh_alarm_list(camera_id)

        # Update ROI combo in alarm editor
        self._update_alarm_roi_combo(camera_id)

    def _update_acquisition_controls(self, camera_id: str) -> None:
        """Update acquisition control states based on runtime."""
        if self._runtime_service is not None:
            running = self._runtime_service.is_camera_running(camera_id)
            self._acq_state_label.setText("RUNNING" if running else "STOPPED")
            self._acq_state_label.setStyleSheet(
                "color: #2E7D32; font-weight: bold;" if running else "color: #757575; font-weight: bold;"
            )
            self._start_btn.setEnabled(not running)
            self._stop_btn.setEnabled(running)

    def _clear_all_panels(self) -> None:
        """Clear all panels when no camera selected."""
        self._acq_camera_label.setText("—")
        self._acq_state_label.setText("STOPPED")
        self._acq_state_label.setStyleSheet("color: #757575; font-weight: bold;")
        self._info_camera_id.setText("—")
        self._info_serial.setText("—")
        self._info_model.setText("—")
        self._info_ip.setText("—")
        self._info_frame_size.setText("—")
        self._info_acq_fps.setText("—")
        self._info_disp_fps.setText("—")
        self._info_sequence.setText("—")
        self._info_timestamp.setText("—")
        self._info_cal_range.setText("—")
        self._info_emissivity.setText("—")
        self._info_ambient.setText("—")
        self._info_proc_time.setText("—")
        self._cam_vendor.setText("—")
        self._cam_firmware.setText("—")
        self._cam_user_name.setText("—")
        self._cam_device_id.setText("—")
        self._roi_tree.clear()
        self._alarm_tree.clear()
        self._roi_stats_table.setRowCount(0)
        self._overall_min.setText("—")
        self._overall_max.setText("—")
        self._overall_mean.setText("—")
        self._overall_stddev.setText("—")

    @pyqtSlot(object)
    def _on_processing_result(self, result: ProcessingResult) -> None:
        """Receive ProcessingResult from observer."""
        self._latest_result = result

        # Copy temperature buffer for display
        temperature_image = result.temperature_image
        if temperature_image is not None:
            temperature_image = np.asarray(temperature_image).copy()

        frame = result.frame
        self._image_widget.set_frame(temperature_image, frame)

        # Update frame info
        self._info_sequence.setText(str(frame.descriptor.sequence))
        self._info_timestamp.setText(f"{frame.descriptor.timestamp:.3f}")
        self._info_frame_size.setText(f"{frame.descriptor.thermal.width}×{frame.descriptor.thermal.height}")

        # Update analysis results
        analysis = result.analysis_result
        if analysis is not None:
            unit_symbol = _UNIT_SYMBOLS.get(getattr(analysis, "unit", None), "°C")

            if analysis.overall_min is not None:
                self._overall_min.setText(f"{analysis.overall_min:.2f} {unit_symbol}")
            if analysis.overall_max is not None:
                self._overall_max.setText(f"{analysis.overall_max:.2f} {unit_symbol}")
            if analysis.overall_mean is not None:
                self._overall_mean.setText(f"{analysis.overall_mean:.2f} {unit_symbol}")

            # Update ROI tree with live stats
            self._update_roi_tree_live(analysis)

            # Update ROI stats table
            self._update_roi_stats_table(analysis)

        # Update alarm display
        alarm = result.alarm_result
        if alarm is not None and alarm.active_alarms:
            self._update_alarm_tree_live(alarm, analysis)

        # Update processing time
        self._info_proc_time.setText(f"{result.processing_time_ms:.1f} ms")

    @pyqtSlot(str)
    def _on_observer_error(self, message: str) -> None:
        self._acq_state_label.setText("ERROR")
        self._acq_state_label.setStyleSheet("color: #D32F2F; font-weight: bold;")
        self._status_conn.setText(f"Connection: Error - {message}")

    def _update_roi_tree_live(self, analysis) -> None:
        """Update ROI tree with live temperature statistics."""
        # Update existing items with live data
        for i in range(self._roi_tree.topLevelItemCount()):
            item = self._roi_tree.topLevelItem(i)
            roi_id = item.data(0, Qt.ItemDataRole.UserRole)
            roi_stat = analysis.roi_results.get(roi_id)
            if roi_stat:
                unit_symbol = _UNIT_SYMBOLS.get(getattr(analysis, "unit", None), "°C")
                item.setText(5, f"{roi_stat.min_temp:.1f} {unit_symbol}")
                item.setText(6, f"{roi_stat.mean_temp:.1f} {unit_symbol}")
                item.setText(7, f"{roi_stat.max_temp:.1f} {unit_symbol}")

    def _update_roi_stats_table(self, analysis) -> None:
        """Update ROI statistics table."""
        self._roi_stats_table.setRowCount(0)
        for roi_id, stat in analysis.roi_results.items():
            row = self._roi_stats_table.rowCount()
            self._roi_stats_table.insertRow(row)
            unit_symbol = _UNIT_SYMBOLS.get(getattr(analysis, "unit", None), "°C")
            self._roi_stats_table.setItem(row, 0, QTableWidgetItem(roi_id))
            self._roi_stats_table.setItem(row, 1, QTableWidgetItem(stat.roi_name))
            self._roi_stats_table.setItem(row, 2, QTableWidgetItem(f"{stat.min_temp:.2f} {unit_symbol}"))
            self._roi_stats_table.setItem(row, 3, QTableWidgetItem(f"{stat.max_temp:.2f} {unit_symbol}"))
            self._roi_stats_table.setItem(row, 4, QTableWidgetItem(f"{stat.mean_temp:.2f} {unit_symbol}"))
            self._roi_stats_table.setItem(row, 5, QTableWidgetItem(f"{stat.deviation:.2f} {unit_symbol}"))
            self._roi_stats_table.setItem(row, 6, QTableWidgetItem(f"{stat.range_temp:.2f} {unit_symbol}"))

    def _update_alarm_tree_live(self, alarm_result, analysis) -> None:
        """Update alarm tree with live alarm state."""
        # Mark active alarms in tree
        for i in range(self._alarm_tree.topLevelItemCount()):
            item = self._alarm_tree.topLevelItem(i)
            rule_id = item.data(0, Qt.ItemDataRole.UserRole)
            if rule_id in alarm_result.active_alarms:
                item.setBackground(0, Qt.GlobalColor.red)
                item.setBackground(1, Qt.GlobalColor.red)
            else:
                item.setBackground(0, Qt.GlobalColor.white)
                item.setBackground(1, Qt.GlobalColor.white)

    def _refresh_roi_list(self, camera_id: str) -> None:
        """Refresh ROI list from analysis config."""
        self._roi_tree.clear()
        analysis = self._config_service.get_analysis_config(camera_id)
        if not analysis:
            return

        # Get ROIs for default position
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
                "—", "—", "—"  # Live stats placeholders
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, roi.roi_id)
            self._roi_tree.addTopLevelItem(item)

    def _refresh_alarm_list(self, camera_id: str) -> None:
        """Refresh alarm list from analysis config."""
        self._alarm_tree.clear()
        analysis = self._config_service.get_analysis_config(camera_id)
        if not analysis:
            return

        for rule_id, rule in analysis.alarm_rules.items():
            item = QTreeWidgetItem([
                rule_id,
                rule.roi_id,
                rule.condition.value,
                f"{rule.threshold:.1f} °C" if rule.threshold is not None else "—",
                rule.severity.value.upper(),
                "Yes" if rule.enabled else "No",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, rule_id)
            self._alarm_tree.addTopLevelItem(item)

    def _update_alarm_roi_combo(self, camera_id: str) -> None:
        """Update ROI combo in alarm editor."""
        self._alarm_editor_roi_combo.clear()
        analysis = self._config_service.get_analysis_config(camera_id)
        if analysis:
            for roi_id in analysis.rois:
                self._alarm_editor_roi_combo.addItem(roi_id, roi_id)

    def _on_roi_selection_changed(self) -> None:
        """Handle ROI selection change."""
        items = self._roi_tree.selectedItems()
        if items:
            item = items[0]
            roi_id = item.data(0, Qt.ItemDataRole.UserRole)
            self._load_roi_editor(roi_id)
            self._edit_roi_btn.setEnabled(True)
            self._delete_roi_btn.setEnabled(True)
        else:
            self._roi_editor.setVisible(False)
            self._edit_roi_btn.setEnabled(False)
            self._delete_roi_btn.setEnabled(False)

    def _load_roi_editor(self, roi_id: str) -> None:
        """Load ROI into editor."""
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

    def _build_geometry_editor(self, geometry: ROIGeometry) -> None:
        """Build geometry parameter editors based on shape."""
        from PyQt6.QtWidgets import QFormLayout, QDoubleSpinBox, QGroupBox, QTextEdit

        # Clear existing
        while self._geometry_layout.count():
            child = self._geometry_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        shape = geometry.shape
        params = geometry.parameters

        group = QGroupBox("Geometry (row/col coordinates)")
        form = QFormLayout(group)

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

        self._geometry_layout.addWidget(group)

    def _make_spin(self, value: float, min_val: float = -10000.0, max_val: float = 10000.0, step: float = 0.1) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(min_val, max_val)
        spin.setDecimals(2)
        spin.setSingleStep(step)
        spin.setValue(value)
        return spin

    def _on_add_roi(self) -> None:
        """Add new ROI."""
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
            self._refresh_roi_list(self._selected_camera_id)
            self._update_alarm_roi_combo(self._selected_camera_id)

    def _on_edit_roi(self) -> None:
        """Edit selected ROI."""
        items = self._roi_tree.selectedItems()
        if items:
            roi_id = items[0].data(0, Qt.ItemDataRole.UserRole)
            self._load_roi_editor(roi_id)

    def _on_delete_roi(self) -> None:
        """Delete selected ROI."""
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
        self._refresh_roi_list(self._selected_camera_id)
        self._update_alarm_roi_combo(self._selected_camera_id)

    def _create_default_geometry(self, shape: ROIShape) -> ROIGeometry:
        """Create default geometry for a shape type."""
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
        """Save ROI changes."""
        if not self._selected_camera_id or not self._editor_roi_id_label.text():
            return

        roi_id = self._editor_roi_id_label.text()
        analysis = self._config_service.get_analysis_config(self._selected_camera_id)
        if not analysis or roi_id not in analysis.rois:
            return

        old_roi = analysis.rois[roi_id]

        # Build new geometry
        geometry = self._read_geometry_editor()
        if not geometry:
            return

        # Build new limits
        limits = TemperatureLimits(
            unit=TemperatureUnit(self._editor_unit_combo.currentText()),
            min_warning=self._editor_min_warning.value() if self._editor_min_warning.value() > -273.15 else None,
            max_warning=self._editor_max_warning.value() if self._editor_max_warning.value() > -273.15 else None,
            min_critical=self._editor_min_critical.value() if self._editor_min_critical.value() > -273.15 else None,
            max_critical=self._editor_max_critical.value() if self._editor_max_critical.value() > -273.15 else None,
            rate_of_change_limit=self._editor_rate_limit.value() if self._editor_rate_limit.value() > 0.0 else None,
        )

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
        self._refresh_roi_list(self._selected_camera_id)

    def _read_geometry_editor(self) -> ROIGeometry | None:
        """Read geometry parameters from editors."""
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
                return None
            return ROIGeometry(shape=ROIShape.POLYGON, parameters={"points": points})
        return None

    # Alarm handlers
    def _on_alarm_selection_changed(self) -> None:
        items = self._alarm_tree.selectedItems()
        if items:
            rule_id = items[0].data(0, Qt.ItemDataRole.UserRole)
            self._load_alarm_editor(rule_id)
            self._edit_alarm_btn.setEnabled(True)
            self._delete_alarm_btn.setEnabled(True)
        else:
            self._alarm_editor.setVisible(False)
            self._edit_alarm_btn.setEnabled(False)
            self._delete_alarm_btn.setEnabled(False)

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
        self._on_alarm_condition_changed(rule.condition.value)

    def _on_alarm_condition_changed(self, condition: str) -> None:
        is_range = condition in (AlarmCondition.OUTSIDE_RANGE.value, AlarmCondition.INSIDE_RANGE.value)
        self._alarm_editor_threshold.setVisible(not is_range)
        self._alarm_editor_threshold_low.setVisible(is_range)
        self._alarm_editor_threshold_high.setVisible(is_range)

    def _on_add_alarm(self) -> None:
        if not self._selected_camera_id:
            return

        from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QComboBox, QDoubleSpinBox

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
            self._refresh_alarm_list(self._selected_camera_id)

    def _on_edit_alarm(self) -> None:
        items = self._alarm_tree.selectedItems()
        if items:
            rule_id = items[0].data(0, Qt.ItemDataRole.UserRole)
            self._load_alarm_editor(rule_id)

    def _on_delete_alarm(self) -> None:
        items = self._alarm_tree.selectedItems()
        if not items or not self._selected_camera_id:
            return

        rule_id = items[0].data(0, Qt.ItemDataRole.UserRole)

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
        self._refresh_alarm_list(self._selected_camera_id)

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
        self._refresh_alarm_list(self._selected_camera_id)

    # Acquisition control handlers
    def _on_start_acquisition(self) -> None:
        if not self._selected_camera_id or not self._runtime_service:
            return

        config = self._config_service.get_camera_config(self._selected_camera_id)
        if not config:
            return

        # Update metadata with current spin values
        metadata = dict(config.metadata or {})
        metadata["frame_rate"] = self._target_fps_spin.value()
        metadata["reconnect_interval_s"] = self._reconnect_spin.value()
        metadata["nuc_duration_s"] = self._nuc_duration_spin.value()
        metadata["grab_timeout_ms"] = int(self._grab_timeout_spin.value() * 1000)

        # Create updated config
        updated_config = CameraConfig(
            identity=config.identity,
            name=config.name,
            description=config.description,
            enabled=config.enabled,
            thermal_enabled=config.thermal_enabled,
            visible_enabled=config.visible_enabled,
            ptz_config=config.ptz_config,
            tags=config.tags,
            metadata=metadata,
        )
        self._config_service.set_camera_config(updated_config)

        try:
            self._runtime_service.start_camera(updated_config)
            self._update_acquisition_controls(self._selected_camera_id)
        except Exception as exc:
            QMessageBox.warning(self, "Start Failed", f"Failed to start camera: {exc}")

    def _on_stop_acquisition(self) -> None:
        if not self._selected_camera_id or not self._runtime_service:
            return

        try:
            self._runtime_service.stop_camera(self._selected_camera_id)
            if self._observer:
                self._observer.stop()
                self._observer = None
            self._update_acquisition_controls(self._selected_camera_id)
        except Exception as exc:
            QMessageBox.warning(self, "Stop Failed", f"Failed to stop camera: {exc}")

    def _on_reconnect(self) -> None:
        if not self._selected_camera_id or not self._runtime_service:
            return

        try:
            self._runtime_service.stop_camera(self._selected_camera_id)
            config = self._config_service.get_camera_config(self._selected_camera_id)
            if config:
                self._runtime_service.start_camera(config)
            self._update_acquisition_controls(self._selected_camera_id)
        except Exception as exc:
            QMessageBox.warning(self, "Reconnect Failed", f"Failed to reconnect: {exc}")

    # Display control handlers
    def _on_palette_changed(self, palette: str) -> None:
        # Palette is handled by LiveThermalWidget internally
        pass

    def _on_auto_range_toggled(self, checked: bool) -> None:
        self._custom_min_spin.setEnabled(not checked)
        self._custom_max_spin.setEnabled(not checked)
        self._apply_range_btn.setEnabled(not checked)

    def _on_apply_range(self) -> None:
        # Range is applied in LiveThermalWidget display logic
        pass

    def _on_zoom_changed(self, zoom_text: str) -> None:
        # Zoom is handled by LiveThermalWidget
        pass

    # Callback handlers
    def _on_camera_config_changed(self, camera_id: str, config: CameraConfig) -> None:
        self._refresh_camera_selector()
        if camera_id == self._selected_camera_id:
            self._load_camera_config(camera_id)

    def _on_analysis_config_changed(self, camera_id: str, config: AnalysisConfig) -> None:
        if camera_id == self._selected_camera_id:
            self._refresh_roi_list(camera_id)
            self._refresh_alarm_list(camera_id)
            self._update_alarm_roi_combo(camera_id)

    def _update_stats(self) -> None:
        """Update status bar stats."""
        if self._runtime_service and self._selected_camera_id:
            cam_stats = self._runtime_service.camera_stats(self._selected_camera_id)
            if cam_stats:
                fps = cam_stats.current_fps or cam_stats.average_fps
                if fps:
                    self._status_fps.setText(f"FPS: {fps:.1f}")
                self._info_acq_fps.setText(f"{fps:.1f}" if fps else "—")
                self._status_frames.setText(f"Frames: {cam_stats.frames_acquired}")

            if self._observer:
                obs_stats = self._observer.stats()
                if obs_stats:
                    self._status_proc.setText(f"Processing: {obs_stats.average_processing_time_ms:.1f} ms")
                    self._info_disp_fps.setText(f"{obs_stats.frames_processed}")
                    self._info_proc_time.setText(f"{obs_stats.average_processing_time_ms:.1f} ms")

    def on_mode_activated(self) -> None:
        """Called when configuration mode becomes active."""
        self._refresh_camera_selector()
        if self._selected_camera_id:
            self._load_camera_config(self._selected_camera_id)
        else:
            cameras = self._config_service.get_all_camera_configs()
            if cameras:
                self._camera_combo.setCurrentIndex(0)

    def on_mode_deactivated(self) -> None:
        """Called when configuration mode is deactivated."""
        if self._observer:
            self._observer.stop()
            self._observer = None
        self._stats_timer.stop()

    def closeEvent(self, event) -> None:
        self.on_mode_deactivated()
        self._config_service.remove_camera_change_callback(self._on_camera_config_changed)
        self._config_service.remove_analysis_change_callback(self._on_analysis_config_changed)
        super().closeEvent(event)


class ConfigurationWindow(QMainWindow):
    """Top-level window for Configuration mode."""

    def __init__(
        self,
        config_service: ConfigurationService,
        mode_service: ModeService,
        runtime_service: CameraRuntimeService | None = None,
        theme_manager: Optional[ThemeManager] = None,
    ) -> None:
        super().__init__()

        self._config_service = config_service
        self._mode_service = mode_service
        self._runtime_service = runtime_service
        self._theme = theme_manager

        self.setWindowTitle("Thermal Monitoring System V3 - Configuration Mode")
        self._apply_window_config()

        # Central widget
        self._config_widget = ConfigurationModeWidget(
            config_service=config_service,
            mode_service=mode_service,
            runtime_service=runtime_service,
            theme_manager=theme_manager,
        )
        self.setCentralWidget(self._config_widget)

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_label = QLabel("Configuration Mode")
        if self._theme:
            self._status_label.setStyleSheet(f"color: {self._theme.text_secondary()};")
        self._status_bar.addWidget(self._status_label)

    def _apply_window_config(self) -> None:
        """Apply window configuration from theme manager."""
        if self._theme:
            config = self._theme.window_config()
            self.setMinimumSize(config["config_min_width"], config["config_min_height"])
        else:
            self.setMinimumSize(1400, 900)

    def on_mode_activated(self) -> None:
        """Called when Configuration mode becomes active."""
        self._config_widget.on_mode_activated()

    def on_mode_deactivated(self) -> None:
        """Called when Configuration mode is deactivated."""
        self._config_widget.on_mode_deactivated()

    def closeEvent(self, event) -> None:
        self.on_mode_deactivated()
        super().closeEvent(event)


# Need to import QLineEdit for ROI editor
from PyQt6.QtWidgets import QLineEdit


__all__ = ["ConfigurationModeWidget", "ConfigurationWindow"]