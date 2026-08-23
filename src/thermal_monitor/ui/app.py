"""
ui.app -- PyQt6 application entry point.

Sets up QApplication, applies global styles, and creates the application controller
which manages the four independent top-level windows.
"""

from __future__ import annotations

import sys
from typing import Optional

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

# Import the camera package first: it transitively imports core.shm to
# completion, so later imports of core.shm (via processing/services) never hit
# the core.shm <-> camera circular import (core.shm re-exports PublishResult
# from camera.model).
import thermal_monitor.camera  # noqa: E402,F401

from thermal_monitor.ui.controller import AppController
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.offline import OfflineService
from thermal_monitor.services.runtime import CameraRuntimeService
from thermal_monitor.storage.database import Database


class ThermalMonitorApp:
    """Main application class."""

    def __init__(self, argv: list[str] | None = None) -> None:
        if argv is None:
            argv = sys.argv

        # Enable high DPI scaling (Qt6 scales by default; only the rounding
        # policy is configurable -- the legacy AA_*HighDpi* attributes were
        # removed in Qt 6).
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

        self._app = QApplication(argv)
        self._app.setApplicationName("Thermal Monitoring System V3")
        self._app.setApplicationVersion("3.0.0")
        self._app.setOrganizationName("ThermalMonitor")

        # Services
        self._mode_service = ModeService()
        self._config_service = ConfigurationService()
        self._offline_service = OfflineService()
        self._runtime_service = CameraRuntimeService()
        self._database: Optional[Database] = None

        # Application controller (owns window lifecycle)
        self._controller: Optional[AppController] = None

    def set_database(self, database: Database) -> None:
        """Set the database connection."""
        self._database = database

    def initialize(self) -> None:
        """Initialize the application and create the controller."""
        self._controller = AppController(
            mode_service=self._mode_service,
            config_service=self._config_service,
            offline_service=self._offline_service,
            runtime_service=self._runtime_service,
            database=self._database,
        )
        self._controller.initialize()

    def run(self) -> int:
        """Run the application event loop."""
        try:
            return self._app.exec()
        finally:
            if self._controller:
                self._controller.shutdown()
            self._runtime_service.shutdown()

    @property
    def mode_service(self) -> ModeService:
        return self._mode_service

    @property
    def config_service(self) -> ConfigurationService:
        return self._config_service

    @property
    def offline_service(self) -> OfflineService:
        return self._offline_service

    @property
    def runtime_service(self) -> CameraRuntimeService:
        return self._runtime_service

    @property
    def database(self) -> Database | None:
        return self._database


def main() -> int:
    """Application entry point."""
    app = ThermalMonitorApp()
    app.initialize()
    return app.run()


if __name__ == "__main__":
    sys.exit(main())