"""UI modes package."""

from thermal_monitor.ui.modes.configuration import ConfigurationModeWidget
from thermal_monitor.ui.modes.offline import OfflineModeWidget
from thermal_monitor.ui.modes.observer import ObserverModeWidget

__all__ = [
    "ConfigurationModeWidget",
    "OfflineModeWidget",
    "ObserverModeWidget",
]