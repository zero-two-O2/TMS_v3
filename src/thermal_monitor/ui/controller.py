"""
ui.controller -- Application controller managing window lifecycle and mutual exclusion.

Owns the four top-level windows and coordinates their visibility and lifecycle.
Enforces mutual exclusion between Live and Configuration modes.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QObject, pyqtSlot
from PyQt6.QtWidgets import QMessageBox

from thermal_monitor.core.modes import ApplicationMode, ModeState
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.offline import OfflineService
from thermal_monitor.services.runtime import CameraRuntimeService
from thermal_monitor.storage.database import Database
from thermal_monitor.ui.windows.launcher_window import LauncherWindow
from thermal_monitor.ui.windows.live_window import LiveWindow
from thermal_monitor.ui.windows.configuration_window import ConfigurationWindow
from thermal_monitor.ui.windows.offline_window import OfflineWindow
from thermal_monitor.services.discovery import CameraDiscoveryService
from thermal_monitor.services.observer import ObserverService


class AppController(QObject):
    """Application-level controller managing window lifecycle.

    Responsibilities:
    - Creates and owns all top-level windows
    - Enforces mutual exclusion: Live + Configuration cannot be open simultaneously
    - Coordinates window show/hide transitions
    - Connects mode requests from Launcher to window activation
    - Manages application shutdown
    """

    def __init__(
        self,
        mode_service: ModeService,
        config_service: ConfigurationService,
        offline_service: OfflineService,
        runtime_service: CameraRuntimeService,
        database: Database | None = None,
        *,
        discovery_service: CameraDiscoveryService | None = None,
        observer_service: ObserverService | None = None,
    ) -> None:
        super().__init__()

        self._mode_service = mode_service
        self._config_service = config_service
        self._offline_service = offline_service
        self._runtime_service = runtime_service
        self._database = database
        self._discovery_service = discovery_service or CameraDiscoveryService()
        self._observer_service = observer_service

        # Window instances (created lazily)
        self._launcher_window: LauncherWindow | None = None
        self._live_window: LiveWindow | None = None
        self._config_window: ConfigurationWindow | None = None
        self._offline_window: OfflineWindow | None = None

        # Track which mode windows are currently open
        self._live_open = False
        self._config_open = False

        # Connect mode service for mutual exclusion enforcement
        self._mode_service.add_observer(self._on_mode_changed)

    def initialize(self) -> None:
        """Create and show the launcher window maximized."""
        self._create_launcher_window()
        self._launcher_window.showMaximized()

    def _create_launcher_window(self) -> None:
        """Create the launcher window."""
        if self._launcher_window is not None:
            return

        self._launcher_window = LauncherWindow(
            mode_service=self._mode_service,
            config_service=self._config_service,
            discovery_service=self._discovery_service,
        )
        self._launcher_window.mode_requested.connect(self._on_mode_requested)

    def _create_live_window(self) -> LiveWindow:
        """Create the live window."""
        if self._live_window is None:
            self._live_window = LiveWindow(
                mode_service=self._mode_service,
                config_service=self._config_service,
                observer_service=self._observer_service,
                runtime_service=self._runtime_service,
            )
            self._live_window.destroyed.connect(self._on_live_window_destroyed)
        return self._live_window

    def _create_config_window(self) -> ConfigurationWindow:
        """Create the configuration window."""
        if self._config_window is None:
            self._config_window = ConfigurationWindow(
                config_service=self._config_service,
                mode_service=self._mode_service,
                runtime_service=self._runtime_service,
            )
            self._config_window.destroyed.connect(self._on_config_window_destroyed)
        return self._config_window

    def _create_offline_window(self) -> OfflineWindow:
        """Create the offline window."""
        if self._offline_window is None:
            self._offline_window = OfflineWindow(
                offline_service=self._offline_service,
                config_service=self._config_service,
                mode_service=self._mode_service,
                database=self._database,
            )
            self._offline_window.destroyed.connect(self._on_offline_window_destroyed)
        return self._offline_window

    @pyqtSlot(ApplicationMode)
    def _on_mode_requested(self, mode: ApplicationMode) -> None:
        """Handle mode request from launcher."""
        if mode == ApplicationMode.LIVE:
            self._request_live_mode()
        elif mode == ApplicationMode.CONFIGURATION:
            self._request_configuration_mode()
        elif mode == ApplicationMode.OFFLINE:
            self._request_offline_mode()
        # LAUNCHER is not requested from launcher

    def _request_live_mode(self) -> None:
        """Request to open Live mode with mutual exclusion check."""
        if self._mode_service.is_configuration_active():
            QMessageBox.warning(
                None,
                "Configuration Mode Active",
                "Configuration Mode is currently active.\n\n"
                "Please close Configuration Mode before opening Live Mode.",
                QMessageBox.StandardButton.Ok
            )
            return

        self._ensure_launcher_hidden()
        live_window = self._create_live_window()
        live_window.on_mode_activated()
        live_window.showMaximized()
        self._live_open = True
        self._mode_service.set_live_active(True)
        self._update_launcher_buttons()

    def _request_configuration_mode(self) -> None:
        """Request to open Configuration mode with mutual exclusion check."""
        if self._mode_service.is_live_active():
            QMessageBox.warning(
                None,
                "Live Mode Active",
                "Live Mode is currently active.\n\n"
                "Please close Live Mode before opening Configuration Mode.",
                QMessageBox.StandardButton.Ok
            )
            return

        self._ensure_launcher_hidden()
        config_window = self._create_config_window()
        config_window.on_mode_activated()
        config_window.showMaximized()
        self._config_open = True
        self._mode_service.set_configuration_active(True)
        self._update_launcher_buttons()

    def _request_offline_mode(self) -> None:
        """Request to open Offline mode (independent, no mutual exclusion)."""
        offline_window = self._create_offline_window()
        offline_window.on_mode_activated()
        offline_window.showMaximized()

    def _ensure_launcher_hidden(self) -> None:
        """Hide launcher window if visible."""
        if self._launcher_window and self._launcher_window.isVisible():
            self._launcher_window.hide()

    def _show_launcher(self) -> None:
        """Show launcher window maximized."""
        if self._launcher_window is None:
            self._create_launcher_window()
        self._launcher_window.showMaximized()
        self._launcher_window.refresh_discovery()
        self._update_launcher_buttons()

    def _update_launcher_buttons(self) -> None:
        """Update launcher mode button enabled states based on mutual exclusion."""
        if self._launcher_window:
            self._launcher_window.set_mode_buttons_enabled(
                live_enabled=not self._config_open,
                config_enabled=not self._live_open,
            )

    @pyqtSlot(ModeState)
    def _on_mode_changed(self, state: ModeState) -> None:
        """Handle mode changes from ModeService (for external transitions)."""
        # This handles programmatic mode changes
        # The actual window management is done via _request_* methods
        pass

    def _on_live_window_destroyed(self) -> None:
        """Handle Live window close."""
        self._live_open = False
        self._mode_service.set_live_active(False)
        self._live_window = None
        self._update_launcher_buttons()
        self._show_launcher()

    def _on_config_window_destroyed(self) -> None:
        """Handle Configuration window close."""
        self._config_open = False
        self._mode_service.set_configuration_active(False)
        self._config_window = None
        self._update_launcher_buttons()
        self._show_launcher()

    def _on_offline_window_destroyed(self) -> None:
        """Handle Offline window close."""
        self._offline_window = None
        # Offline is independent, launcher not affected

    def shutdown(self) -> None:
        """Clean shutdown of all windows."""
        # Close mode windows first (they stop their runtimes)
        if self._live_window:
            self._live_window.close()
        if self._config_window:
            self._config_window.close()
        if self._offline_window:
            self._offline_window.close()
        if self._launcher_window:
            self._launcher_window.close()

        # Shutdown runtime service
        self._runtime_service.shutdown()

    @property
    def launcher_window(self) -> LauncherWindow | None:
        return self._launcher_window

    @property
    def live_window(self) -> LiveWindow | None:
        return self._live_window

    @property
    def configuration_window(self) -> ConfigurationWindow | None:
        return self._config_window

    @property
    def offline_window(self) -> OfflineWindow | None:
        return self._offline_window

    @property
    def is_live_open(self) -> bool:
        return self._live_open

    @property
    def is_configuration_open(self) -> bool:
        return self._config_open


__all__ = ["AppController"]