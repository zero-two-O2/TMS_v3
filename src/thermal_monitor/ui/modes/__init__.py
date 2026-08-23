"""UI modes package."""

from thermal_monitor.ui.modes.configuration import ConfigurationModeWidget
from thermal_monitor.ui.modes.launcher import LauncherWidget
from thermal_monitor.ui.modes.live import LiveModeWidget
from thermal_monitor.ui.modes.offline import OfflineModeWidget

__all__ = [
    "ConfigurationModeWidget",
    "LauncherWidget",
    "LiveModeWidget",
    "OfflineModeWidget",
]