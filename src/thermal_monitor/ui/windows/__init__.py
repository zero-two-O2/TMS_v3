"""
ui.windows -- Top-level window package for TMS V3.

Contains the four independent mode windows:
- LauncherWindow: Startup screen with camera discovery
- LiveWindow: Fixed 8-camera monitoring wall
- ConfigurationWindow: ThermoView-style configuration workspace
- OfflineWindow: Independent recording playback
"""

from thermal_monitor.ui.windows.launcher_window import LauncherWindow
from thermal_monitor.ui.windows.live_window import LiveWindow
from thermal_monitor.ui.windows.configuration_window import ConfigurationWindow
from thermal_monitor.ui.windows.offline_window import OfflineWindow


__all__ = [
    "LauncherWindow",
    "LiveWindow",
    "ConfigurationWindow",
    "OfflineWindow",
]