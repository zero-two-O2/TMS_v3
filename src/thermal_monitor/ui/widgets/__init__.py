"""UI widgets package."""

from thermal_monitor.ui.widgets.config_camera_header import ConfigCameraHeader
from thermal_monitor.ui.widgets.thermal_scale_panel import ThermalScalePanel
from thermal_monitor.ui.widgets.frame_info_panel import FrameInfoPanel
from thermal_monitor.ui.widgets.roi_panel import ROIPanel
from thermal_monitor.ui.widgets.alarm_panel import AlarmPanel
from thermal_monitor.ui.widgets.statistics_panel import StatisticsPanel
from thermal_monitor.ui.widgets.camera_selection_dialog import CameraSelectionDialog
from thermal_monitor.ui.widgets.acquisition_setup_dialog import AcquisitionSetupDialog
from thermal_monitor.ui.widgets.image_acquisition_panel import ImageAcquisitionPanel
from thermal_monitor.ui.widgets.wheel_guard import (
    WheelForwardFilter,
    enclosing_scroll_area,
    install_wheel_guards,
)

__all__ = [
    "ConfigCameraHeader",
    "ThermalScalePanel",
    "FrameInfoPanel",
    "ROIPanel",
    "AlarmPanel",
    "StatisticsPanel",
    "CameraSelectionDialog",
    "AcquisitionSetupDialog",
    "ImageAcquisitionPanel",
    "WheelForwardFilter",
    "enclosing_scroll_area",
    "install_wheel_guards",
]