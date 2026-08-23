"""
ui.main_window -- Main application window with mode switching.

The main window holds the central widget that changes based on the current
application mode (LAUNCHER, LIVE, CONFIGURATION, OFFLINE).
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import (
    QMainWindow,
    QStackedWidget,
    QToolBar,
    QStatusBar,
    QLabel,
    QMenuBar,
    QMenu,
    QMessageBox,
)

from thermal_monitor.core.modes import ApplicationMode, ModeState
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.offline import OfflineService
from thermal_monitor.services.observer import ObserverService
from thermal_monitor.services.runtime import CameraRuntimeService
from thermal_monitor.services.discovery import CameraDiscoveryService
from thermal_monitor.storage.database import Database

from thermal_monitor.ui.modes.configuration import ConfigurationModeWidget
from thermal_monitor.ui.modes.launcher import LauncherWidget
from thermal_monitor.ui.modes.live import LiveModeWidget
from thermal_monitor.ui.modes.offline import OfflineModeWidget
from thermal_monitor.ui.theme import ThemeManager


class MainWindow(QMainWindow):
    """Main application window with mode-aware central widget."""

    def __init__(
        self,
        mode_service: ModeService,
        config_service: ConfigurationService,
        offline_service: OfflineService,
        observer_service: ObserverService | None = None,
        database: Database | None = None,
        *,
        runtime_service: CameraRuntimeService | None = None,
        theme_manager: ThemeManager | None = None,
    ) -> None:
        super().__init__()

        self._mode_service = mode_service
        self._config_service = config_service
        self._offline_service = offline_service
        self._observer_service = observer_service
        self._database = database
        self._runtime_service = runtime_service
        self._theme = theme_manager

        self.setWindowTitle("Thermal Monitoring System V3")
        self._apply_window_config()

        # Central stacked widget for mode switching
        self._stacked_widget = QStackedWidget()
        self.setCentralWidget(self._stacked_widget)

        # Create mode widgets
        self._discovery_service = CameraDiscoveryService()
        self._launcher_widget = LauncherWidget(
            mode_service=mode_service,
            config_service=config_service,
            discovery_service=self._discovery_service,
            theme_manager=self._theme,
        )
        self._live_widget = LiveModeWidget(
            mode_service=mode_service,
            config_service=config_service,
            observer_service=observer_service,
            runtime_service=runtime_service,
            theme_manager=self._theme,
        )
        self._config_widget = ConfigurationModeWidget(
            config_service=config_service,
            mode_service=mode_service,
            database=database,
            runtime_service=runtime_service,
            theme_manager=self._theme,
        )
        self._offline_widget = OfflineModeWidget(
            offline_service=offline_service,
            config_service=config_service,
            mode_service=mode_service,
            database=database,
            theme_manager=self._theme,
        )

        # Add to stack in mode order
        self._stacked_widget.addWidget(self._launcher_widget)   # index 0 - LAUNCHER
        self._stacked_widget.addWidget(self._live_widget)       # index 1 - LIVE
        self._stacked_widget.addWidget(self._config_widget)     # index 2 - CONFIGURATION
        self._stacked_widget.addWidget(self._offline_widget)    # index 3 - OFFLINE

        # Toolbar for mode switching
        self._create_toolbar()

        # Status bar
        self._status_bar = QStatusBar()
        self._mode_label = QLabel("Mode: LAUNCHER")
        if self._theme:
            self._mode_label.setStyleSheet(f"color: {self._theme.text_secondary()};")
        self._status_bar.addPermanentWidget(self._mode_label)
        self.setStatusBar(self._status_bar)

        # Menu bar
        self._create_menu_bar()

        # Connect mode changes
        self._mode_service.add_observer(self._on_mode_changed)

        # Connect launcher mode requests
        self._launcher_widget.mode_requested.connect(self._on_launcher_mode_requested)

        # Initial mode
        self._update_ui_for_mode(self._mode_service.state)

    def _create_toolbar(self) -> None:
        """Create the main toolbar with mode switching actions."""
        toolbar = QToolBar("Mode Toolbar")
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        # Live mode action
        self._live_action = toolbar.addAction("Live")
        self._live_action.setCheckable(True)
        self._live_action.triggered.connect(
            lambda: self._request_live_mode()
        )

        # Configuration mode action
        self._config_action = toolbar.addAction("Configuration")
        self._config_action.setCheckable(True)
        self._config_action.triggered.connect(
            lambda: self._request_configuration_mode()
        )

        # Offline mode action
        self._offline_action = toolbar.addAction("Offline")
        self._offline_action.setCheckable(True)
        self._offline_action.triggered.connect(
            lambda: self._mode_service.transition_to_offline("toolbar")
        )

        # Group actions for exclusive checking (Launcher is startup-only, not in toolbar)
        self._mode_actions = {
            ApplicationMode.LIVE: self._live_action,
            ApplicationMode.CONFIGURATION: self._config_action,
            ApplicationMode.OFFLINE: self._offline_action,
        }

    def _request_live_mode(self) -> None:
        """Request transition to Live mode with mutual exclusion check."""
        if self._mode_service.current_mode == ApplicationMode.CONFIGURATION:
            QMessageBox.warning(
                self,
                "Configuration Mode Active",
                "Configuration Mode Is Active\n\n"
                "Please close Configuration before starting Live Mode.",
                QMessageBox.StandardButton.Ok
            )
            self._live_action.setChecked(False)
            return
        self._mode_service.transition_to_live("toolbar")

    def _request_configuration_mode(self) -> None:
        """Request transition to Configuration mode with mutual exclusion check."""
        if self._mode_service.current_mode == ApplicationMode.LIVE:
            QMessageBox.warning(
                self,
                "Live Mode Active",
                "Live Mode Is Active\n\n"
                "Please stop Live Mode before opening Configuration.",
                QMessageBox.StandardButton.Ok
            )
            self._config_action.setChecked(False)
            return
        self._mode_service.transition_to_configuration("toolbar")

    def _create_menu_bar(self) -> None:
        """Create the application menu bar."""
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("File")
        file_menu.addAction("Open Recording...", self._offline_widget.open_recording_dialog)
        file_menu.addAction("Close Recording", self._offline_widget.close_recording)
        file_menu.addSeparator()
        file_menu.addAction("Exit", self.close)

        # Mode menu
        mode_menu = menubar.addMenu("Mode")
        mode_menu.addAction("Live", lambda: self._request_live_mode())
        mode_menu.addAction("Configuration", lambda: self._request_configuration_mode())
        mode_menu.addAction("Offline", lambda: self._mode_service.transition_to_offline("menu"))

        # View menu
        view_menu = menubar.addMenu("View")
        view_menu.addAction("Camera Configuration", self._config_widget.show_camera_config)
        view_menu.addAction("ROI Configuration", self._config_widget.show_roi_config)
        view_menu.addAction("Alarm Configuration", self._config_widget.show_alarm_config)
        view_menu.addAction("System Configuration", self._config_widget.show_system_config)

    @pyqtSlot(ModeState)
    def _on_mode_changed(self, state: ModeState) -> None:
        """Handle mode change from ModeService."""
        self._update_ui_for_mode(state)

    @pyqtSlot(ApplicationMode)
    def _on_launcher_mode_requested(self, mode: ApplicationMode) -> None:
        """Handle mode request from launcher widget."""
        if mode == ApplicationMode.LIVE:
            self._request_live_mode()
        elif mode == ApplicationMode.CONFIGURATION:
            self._request_configuration_mode()
        elif mode == ApplicationMode.OFFLINE:
            self._mode_service.transition_to_offline("launcher")
        # LAUNCHER is not requested from launcher

    def _update_ui_for_mode(self, state: ModeState) -> None:
        """Update UI to reflect current mode."""
        mode = state.mode

        # Deactivate the currently active mode widget (if it supports it)
        current_widget = self._stacked_widget.currentWidget()
        if current_widget is not None:
            deactivate = getattr(current_widget, "on_mode_deactivated", None)
            if callable(deactivate):
                deactivate()

        # Switch stacked widget
        mode_index = {
            ApplicationMode.LAUNCHER: 0,
            ApplicationMode.LIVE: 1,
            ApplicationMode.CONFIGURATION: 2,
            ApplicationMode.OFFLINE: 3,
        }
        self._stacked_widget.setCurrentIndex(mode_index[mode])

        # Update toolbar button states
        for m, action in self._mode_actions.items():
            action.setChecked(m == mode)

        # Update status bar
        self._mode_label.setText(f"Mode: {mode.value.upper()}")

        # Notify mode widgets
        if mode == ApplicationMode.LAUNCHER:
            self._launcher_widget.on_mode_activated()
        elif mode == ApplicationMode.LIVE:
            self._live_widget.on_mode_activated()
        elif mode == ApplicationMode.CONFIGURATION:
            self._config_widget.on_mode_activated()
        elif mode == ApplicationMode.OFFLINE:
            self._offline_widget.on_mode_activated()

    def closeEvent(self, event) -> None:
        """Clean up on close."""
        self._mode_service.remove_observer(self._on_mode_changed)
        if self._runtime_service is not None:
            self._runtime_service.shutdown()
        super().closeEvent(event)

    def _apply_window_config(self) -> None:
        """Apply window configuration from theme manager."""
        if self._theme:
            config = self._theme.window_config()
            self.setMinimumSize(config.get("live_min_width", 1200), config.get("live_min_height", 800))
        else:
            self.setMinimumSize(1200, 800)