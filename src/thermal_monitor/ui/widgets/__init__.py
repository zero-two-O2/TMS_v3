"""ui.widgets package -- widget exports (Phase 10 additions)."""

from thermal_monitor.ui.widgets.acquisition_setup_dialog import AcquisitionSetupDialog
from thermal_monitor.ui.widgets.alarm_panel import AlarmPanel
from thermal_monitor.ui.widgets.camera_region import CameraRegion
from thermal_monitor.ui.widgets.camera_selection_dialog import CameraSelectionDialog
from thermal_monitor.ui.widgets.config_camera_header import ConfigCameraHeader
from thermal_monitor.ui.widgets.frame_info_panel import FrameInfoPanel
from thermal_monitor.ui.widgets.image_acquisition_panel import ImageAcquisitionPanel
from thermal_monitor.ui.widgets.ptz_control_panel import PtzControlPanel
from thermal_monitor.ui.widgets.ptz_position_table import PtzPositionTablePanel
from thermal_monitor.ui.widgets.roi_canvas import RoiCanvasController
from thermal_monitor.ui.widgets.roi_icons import icon_for
from thermal_monitor.ui.widgets.roi_interaction import EditCommand, RoiInteractionState
from thermal_monitor.ui.widgets.roi_overlay import RoiOverlayItem, RoiOverlaySet, overlay_color
from thermal_monitor.ui.widgets.roi_panel import ROIPanel
from thermal_monitor.ui.widgets.roi_properties import RoiPropertiesPanel
from thermal_monitor.ui.widgets.roi_toolbar import RoiToolbar
from thermal_monitor.ui.widgets.statistics_panel import StatisticsPanel
from thermal_monitor.ui.widgets.thermal_scale_panel import ThermalScalePanel
from thermal_monitor.ui.widgets.wheel_guard import (
    WheelForwardFilter,
    enclosing_scroll_area,
    install_wheel_guards,
)

__all__ = [
    "AcquisitionSetupDialog",
    "AlarmPanel",
    "CameraRegion",
    "CameraSelectionDialog",
    "ConfigCameraHeader",
    "EditCommand",
    "FrameInfoPanel",
    "ImageAcquisitionPanel",
    "PtzControlPanel",
    "PtzPositionTablePanel",
    "RoiInteractionState",
    "RoiCanvasController",
    "RoiOverlayItem",
    "RoiOverlaySet",
    "ROIPanel",
    "RoiPropertiesPanel",
    "RoiToolbar",
    "icon_for",
    "StatisticsPanel",
    "ThermalScalePanel",
    "WheelForwardFilter",
    "enclosing_scroll_area",
    "install_wheel_guards",
    "overlay_color",
]
