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
from thermal_monitor.services.discovery import CameraDiscoveryService
from thermal_monitor.storage.database import Database
from thermal_monitor.config import ConfigurationManager, create_config_manager
from thermal_monitor.ui.theme import ThemeManager


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

        # Initialize ConfigurationManager FIRST - single source of truth
        self._config_manager = create_config_manager()
        config = self._config_manager.get_config()

        # Apply application identity from config
        self._app.setApplicationName(config.application.name)
        self._app.setApplicationVersion(config.application.version)
        self._app.setOrganizationName("ThermalMonitor")

        # Initialize logging from configuration
        self._configure_logging(config.logging)

        # Create ThemeManager and apply theme
        self._theme_manager = ThemeManager(self._config_manager)
        self._theme_manager.apply(self._app)

        # Services - created with configuration injection
        self._mode_service = ModeService()
        self._config_service = ConfigurationService()
        self._offline_service = OfflineService()
        self._discovery_service = CameraDiscoveryService()
        self._runtime_service = CameraRuntimeService()
        self._database: Optional[Database] = None

        # Application controller (owns window lifecycle)
        self._controller: Optional[AppController] = None

    def _configure_logging(self, logging_config) -> None:
        """Configure application logging from configuration."""
        import logging
        from thermal_monitor.core.logging import logger as app_logger

        # Get the root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(getattr(logging, logging_config.level))

        # Clear existing handlers
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

        # Configure handler
        if logging_config.file_path:
            # File path will be resolved by the logging system if needed
            from logging.handlers import RotatingFileHandler
            log_path = self._config_manager.resolve_log_path()
            if log_path:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(
                    log_path,
                    maxBytes=logging_config.max_size_mb * 1024 * 1024,
                    backupCount=logging_config.backup_count,
                    encoding="utf-8"
                )
            else:
                handler = logging.StreamHandler()
        else:
            handler = logging.StreamHandler()

        formatter = logging.Formatter(logging_config.format, logging_config.date_format)
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)

        # Also update our internal logger
        app_logger._logger.setLevel(getattr(logging, logging_config.level))
        for h in app_logger._logger.handlers[:]:
            app_logger._logger.removeHandler(h)
        app_logger._logger.addHandler(handler)

    def set_database(self, database: Database) -> None:
        """Set the database connection."""
        self._database = database

    def initialize(self) -> None:
        """Initialize the application and create the controller."""
        # Pass ConfigurationManager and ThemeManager to controller for dependency injection
        self._controller = AppController(
            mode_service=self._mode_service,
            config_service=self._config_service,
            offline_service=self._offline_service,
            runtime_service=self._runtime_service,
            database=self._database,
            discovery_service=self._discovery_service,
            config_manager=self._config_manager,
            theme_manager=self._theme_manager,
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

    @property
    def config_manager(self) -> ConfigurationManager:
        return self._config_manager

    @property
    def theme_manager(self) -> ThemeManager:
        return self._theme_manager


def main() -> int:
    """Application entry point."""
    app = ThermalMonitorApp()
    app.initialize()
    return app.run()


if __name__ == "__main__":
    sys.exit(main())