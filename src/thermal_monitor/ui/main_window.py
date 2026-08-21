"""
ui.main_window -- Main application window with mode switching.

The main window holds the central widget that changes based on the current
application mode (CONFIGURATION, OBSERVER, OFFLINE).
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QStackedWidget,
    QToolBar,
    QStatusBar,
    QLabel,
    QMenuBar,
    QMenu,
)

from thermal_monitor.core.modes import ApplicationMode, ModeState
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.offline import OfflineService
from thermal_monitor.services.observer import ObserverService
from thermal_monitor.services.runtime import CameraRuntimeService
from thermal_monitor.storage.database import Database

from thermal_monitor.ui.modes.configuration import ConfigurationModeWidget
from thermal_monitor.ui.modes.offline import OfflineModeWidget
from thermal_monitor.ui.modes.observer import ObserverModeWidget


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
    ) -> None:
        super().__init__()

        self._mode_service = mode_service
        self._config_service = config_service
        self._offline_service = offline_service
        self._observer_service = observer_service
        self._database = database
        self._runtime_service = runtime_service

        self.setWindowTitle("Thermal Monitoring System V3")
        self.setMinimumSize(1200, 800)

        # Central stacked widget for mode switching
        self._stacked_widget = QStackedWidget()
        self.setCentralWidget(self._stacked_widget)

        # Create mode widgets
        self._config_widget = ConfigurationModeWidget(
            config_service=config_service,
            mode_service=mode_service,
            database=database,
            runtime_service=runtime_service,
        )
        self._offline_widget = OfflineModeWidget(
            offline_service=offline_service,
            config_service=config_service,
            mode_service=mode_service,
            database=database,
        )
        self._observer_widget = ObserverModeWidget(
            mode_service=mode_service,
            config_service=config_service,
            observer_service=observer_service,
            runtime_service=runtime_service,
        )

        # Add to stack in mode order
        self._stacked_widget.addWidget(self._config_widget)   # index 0
        self._stacked_widget.addWidget(self._observer_widget) # index 1
        self._stacked_widget.addWidget(self._offline_widget)  # index 2

        # Toolbar for mode switching
        self._create_toolbar()

        # Status bar
        self._status_bar = QStatusBar()
        self._mode_label = QLabel("Mode: CONFIGURATION")
        self._status_bar.addPermanentWidget(self._mode_label)
        self.setStatusBar(self._status_bar)

        # Menu bar
        self._create_menu_bar()

        # Connect mode changes
        self._mode_service.add_observer(self._on_mode_changed)

        # Initial mode
        self._update_ui_for_mode(self._mode_service.state)

    def _create_toolbar(self) -> None:
        """Create the main toolbar with mode switching actions."""
        toolbar = QToolBar("Mode Toolbar")
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        # Configuration mode action
        self._config_action = toolbar.addAction("Configuration")
        self._config_action.setCheckable(True)
        self._config_action.setChecked(True)
        self._config_action.triggered.connect(
            lambda: self._mode_service.transition_to_configuration("toolbar")
        )

        # Observer mode action
        self._observer_action = toolbar.addAction("Observer")
        self._observer_action.setCheckable(True)
        self._observer_action.triggered.connect(
            lambda: self._mode_service.transition_to_observer("toolbar")
        )

        # Offline mode action
        self._offline_action = toolbar.addAction("Offline")
        self._offline_action.setCheckable(True)
        self._offline_action.triggered.connect(
            lambda: self._mode_service.transition_to_offline("toolbar")
        )

        # Group actions for exclusive checking
        self._mode_actions = {
            ApplicationMode.CONFIGURATION: self._config_action,
            ApplicationMode.OBSERVER: self._observer_action,
            ApplicationMode.OFFLINE: self._offline_action,
        }

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
        mode_menu.addAction("Configuration", lambda: self._mode_service.transition_to_configuration("menu"))
        mode_menu.addAction("Observer", lambda: self._mode_service.transition_to_observer("menu"))
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
            ApplicationMode.CONFIGURATION: 0,
            ApplicationMode.OBSERVER: 1,
            ApplicationMode.OFFLINE: 2,
        }
        self._stacked_widget.setCurrentIndex(mode_index[mode])

        # Update toolbar button states
        for m, action in self._mode_actions.items():
            action.setChecked(m == mode)

        # Update status bar
        self._mode_label.setText(f"Mode: {mode.value.upper()}")

        # Notify mode widgets
        if mode == ApplicationMode.CONFIGURATION:
            self._config_widget.on_mode_activated()
        elif mode == ApplicationMode.OFFLINE:
            self._offline_widget.on_mode_activated()
        elif mode == ApplicationMode.OBSERVER:
            self._observer_widget.on_mode_activated()

    def closeEvent(self, event) -> None:
        """Clean up on close."""
        self._mode_service.remove_observer(self._on_mode_changed)
        if self._runtime_service is not None:
            self._runtime_service.shutdown()
        super().closeEvent(event)
