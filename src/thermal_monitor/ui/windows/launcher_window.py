"""
ui.windows.launcher_window -- Launcher window (startup screen).

Shows discovered cameras and provides entry points to the three
product modes: LIVE, CONFIGURATION, OFFLINE.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QPushButton,
    QLabel,
    QMessageBox,
    QStatusBar,
)

from thermal_monitor.core.modes import ApplicationMode
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.discovery import CameraDiscoveryService, DiscoveredCamera, CameraDiscoveryError, GvcpDiscoveryService
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.ui.theme import ThemeManager
from thermal_monitor.ui.theme.properties import set_role, set_variant
from thermal_monitor.ui.theme.themes import BUILTIN_THEMES

#: Fallback status colors when no theme manager is attached (the app
#: always provides one).  Sourced centrally, never scattered literals.
_FALLBACK = BUILTIN_THEMES["industrial_dark"]


class LauncherWindow(QMainWindow):
    """Startup/launcher window showing discovered cameras and mode entry points."""

    # Signal emitted when user requests a mode change
    mode_requested = pyqtSignal(ApplicationMode)
    # Signal emitted when the user opens the standalone PLC & PTZ monitor.
    ptz_monitor_requested = pyqtSignal()
    # Carries background discovery outcomes to the GUI thread:
    # (cameras, error_message). Empty error means success.
    discovery_updated = pyqtSignal(list, str)

    def __init__(
        self,
        mode_service: ModeService,
        config_service: ConfigurationService,
        discovery_service: "Optional[CameraDiscoveryService | GvcpDiscoveryService]" = None,
        theme_manager: Optional[ThemeManager] = None,
        config_manager=None,
    ) -> None:
        super().__init__()

        self._mode_service = mode_service
        self._config_service = config_service
        self._discovery = discovery_service or CameraDiscoveryService()
        self._discovered: list[DiscoveredCamera] = []
        self._theme = theme_manager
        self._config_manager = config_manager
        self._settings_menu_controller = None

        self.setWindowTitle("Thermal Monitoring System V3 - Launcher")
        self._apply_window_config()
        self._setup_ui()
        self._setup_settings_menu()
        self._create_status_bar()
        # Background discovery coordination (transition fast path): only
        # one scan runs at a time; outcomes return via queued signal so
        # the table is always updated on the GUI thread.
        self._discovery_in_flight = False
        # First-paint marker for mode-transition instrumentation; armed
        # by notify_transition_shown() and consumed by paintEvent().
        self._transition_paint_pending = False
        self.discovery_updated.connect(self._apply_discovery_result)
        self._perform_initial_discovery()

    def _setup_settings_menu(self) -> None:
        """Add the top-left Settings menu with live Theme switching."""
        from thermal_monitor.ui.theme.menu import ThemeMenuController

        self._settings_menu_controller = ThemeMenuController(
            theme_manager=self._theme,
            config_manager=self._config_manager,
            parent=self,
        )
        self._settings_menu_controller.attach_to_window(self)

    def _apply_window_config(self) -> None:
        """Apply window configuration from theme manager."""
        if self._theme:
            config = self._theme.window_config()
            self.setMinimumSize(config["launcher_min_width"], config["launcher_min_height"])
        else:   
            self.setMinimumSize(1000, 700)

    # --- Semantic styling -------------------------------------------------
    # Widgets declare variant/role properties only; the central
    # stylesheet resolves all colors.  No per-widget stylesheets here.

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Title
        title = QLabel("Thermal Monitoring System")
        set_role(title, "title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("Available Cameras")
        set_role(subtitle, "subtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        # Camera table
        camera_group = QGroupBox("Discovered Cameras")
        camera_layout = QVBoxLayout(camera_group)

        self._camera_table = QTableWidget(0, 5)
        self._camera_table.setHorizontalHeaderLabels(
            ["#", "Serial", "Model", "IP Address", "Status"]
        )
        self._camera_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._camera_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._camera_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._camera_table.setAlternatingRowColors(True)
        camera_layout.addWidget(self._camera_table)

        # Search button
        search_layout = QHBoxLayout()
        search_layout.addStretch()
        self._search_btn = QPushButton("Search Cameras")
        self._search_btn.setMinimumWidth(160)
        self._search_btn.clicked.connect(self._on_search_clicked)
        search_layout.addWidget(self._search_btn)
        search_layout.addStretch()
        camera_layout.addLayout(search_layout)

        layout.addWidget(camera_group, 1)

        # Mode buttons
        mode_group = QGroupBox("Select Mode")
        mode_layout = QHBoxLayout(mode_group)
        mode_layout.setSpacing(20)

        self._live_btn = QPushButton("LIVE")
        self._live_btn.setMinimumSize(140, 60)
        set_variant(self._live_btn, "primary")
        self._live_btn.clicked.connect(lambda: self.mode_requested.emit(ApplicationMode.LIVE))

        self._config_btn = QPushButton("CONFIGURATION")
        self._config_btn.setMinimumSize(140, 60)
        set_variant(self._config_btn, "secondary")
        self._config_btn.clicked.connect(lambda: self.mode_requested.emit(ApplicationMode.CONFIGURATION))

        self._offline_btn = QPushButton("OFFLINE")
        self._offline_btn.setMinimumSize(140, 60)
        set_variant(self._offline_btn, "accent")
        self._offline_btn.clicked.connect(lambda: self.mode_requested.emit(ApplicationMode.OFFLINE))

        mode_layout.addStretch()
        mode_layout.addWidget(self._live_btn)
        mode_layout.addWidget(self._config_btn)
        mode_layout.addWidget(self._offline_btn)
        mode_layout.addStretch()

        layout.addWidget(mode_group)

        # Standalone PLC & PTZ monitor (independent of all modes).
        monitor_group = QGroupBox("Diagnostics")
        monitor_layout = QHBoxLayout(monitor_group)
        monitor_layout.addStretch()
        self._ptz_monitor_btn = QPushButton("PLC && PTZ MONITOR")
        self._ptz_monitor_btn.setMinimumSize(200, 44)
        set_variant(self._ptz_monitor_btn, "secondary")
        self._ptz_monitor_btn.clicked.connect(self.ptz_monitor_requested.emit)
        monitor_layout.addWidget(self._ptz_monitor_btn)
        monitor_layout.addStretch()

        layout.addWidget(monitor_group)

        # Status
        self._status_label = QLabel("Discovering cameras...")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_role(self._status_label, "status")
        layout.addWidget(self._status_label)

    def _create_status_bar(self) -> None:
        status_bar = QStatusBar()
        self.setStatusBar(status_bar)
        self._status_bar_label = QLabel("Ready")
        status_bar.addWidget(self._status_bar_label)

    def _perform_initial_discovery(self) -> None:
        """Perform initial camera discovery at startup."""
        self._status_bar_label.setText("Discovering cameras...")
        try:
            self._discovered = self._discovery.discover_cameras()
            self._refresh_camera_table()
            self._update_status()
            self._status_bar_label.setText(f"Discovery complete: {len(self._discovered)} camera(s) found")
        except CameraDiscoveryError as exc:
            self._status_bar_label.setText(f"Discovery failed: {exc}")
            self._discovered = []
            self._refresh_camera_table()

    def _on_search_clicked(self) -> None:
        """Handle manual search button click."""
        self._status_bar_label.setText("Searching for cameras...")
        self._search_btn.setEnabled(False)
        try:
            self._discovered = self._discovery.refresh()
            self._refresh_camera_table()
            self._update_status()
            self._status_bar_label.setText(f"Discovery complete: {len(self._discovered)} camera(s) found")
        except CameraDiscoveryError as exc:
            QMessageBox.warning(self, "Camera Discovery", f"Camera discovery failed: {exc}")
            self._status_bar_label.setText(f"Discovery failed: {exc}")
        finally:
            self._search_btn.setEnabled(True)

    def _refresh_camera_table(self) -> None:
        """Refresh the camera table with discovered cameras."""
        max_slots = 8

        self._camera_table.setRowCount(0)

        # Add discovered cameras
        for i, camera in enumerate(self._discovered[:max_slots]):
            row = self._camera_table.rowCount()
            self._camera_table.insertRow(row)

            # Slot number
            self._camera_table.setItem(row, 0, QTableWidgetItem(str(i + 1)))

            # Serial
            serial = camera.serial_number or "(not provided)"
            self._camera_table.setItem(row, 1, QTableWidgetItem(serial))

            # Model
            model = camera.model or "(unknown)"
            self._camera_table.setItem(row, 2, QTableWidgetItem(model))

            # IP
            ip = camera.ip_address or "(unknown)"
            self._camera_table.setItem(row, 3, QTableWidgetItem(ip))

            # Status
            status = "Available"
            status_item = QTableWidgetItem(status)
            if self._theme:
                status_item.setForeground(QColor(self._theme.success()))
            else:
                status_item.setForeground(QColor(_FALLBACK.camera_connected))
            self._camera_table.setItem(row, 4, status_item)

        # Fill remaining slots as unavailable
        for i in range(len(self._discovered), max_slots):
            row = self._camera_table.rowCount()
            self._camera_table.insertRow(row)
            self._camera_table.setItem(row, 0, QTableWidgetItem(str(i + 1)))
            self._camera_table.setItem(row, 1, QTableWidgetItem("—"))
            self._camera_table.setItem(row, 2, QTableWidgetItem("—"))
            self._camera_table.setItem(row, 3, QTableWidgetItem("—"))
            status_item = QTableWidgetItem("Not Available")
            if self._theme:
                status_item.setForeground(QColor(self._theme.disabled_text()))
            else:
                status_item.setForeground(QColor(_FALLBACK.camera_disconnected))
            self._camera_table.setItem(row, 4, status_item)

    def _update_status(self) -> None:
        """Update the status label."""
        available = len(self._discovered)
        self._status_label.setText(f"Available Cameras: {available} / 8")

    def refresh_discovery(self) -> None:
        """Public method to refresh discovery (e.g., after returning from other modes)."""
        self._perform_initial_discovery()

    def refresh_discovery_async(self) -> None:
        """Refresh discovery WITHOUT blocking the GUI thread.

        Runs the (potentially multi-second GVCP broadcast) scan in a
        daemon thread while the Launcher stays fully interactive; the
        table updates via the queued ``discovery_updated`` signal when
        the scan lands. Overlapping scans are coalesced: a refresh
        requested while one is in flight is a no-op.
        """
        import logging as _logging
        import threading as _threading

        _logger = _logging.getLogger(__name__)
        if self._discovery_in_flight:
            _logger.debug("Launcher discovery refresh coalesced (scan in flight)")
            return
        self._discovery_in_flight = True
        try:
            self._status_bar_label.setText("Discovering cameras...")
        except RuntimeError:
            self._discovery_in_flight = False
            return  # C++ object gone

        discovery = self._discovery

        def _scan() -> None:
            try:
                cameras = discovery.discover_cameras()
            except Exception as exc:
                try:
                    self.discovery_updated.emit([], str(exc)[:300])
                except RuntimeError:
                    pass  # window torn down mid-scan
            else:
                try:
                    self.discovery_updated.emit(list(cameras), "")
                except RuntimeError:
                    pass

        _threading.Thread(target=_scan, name="LauncherDiscovery", daemon=True).start()

    @pyqtSlot(list, str)
    def _apply_discovery_result(self, cameras: list, error: str) -> None:
        """Apply a background discovery outcome (GUI thread only)."""
        self._discovery_in_flight = False
        if error:
            try:
                self._status_bar_label.setText(f"Discovery failed: {error}")
            except RuntimeError:
                pass
            return
        self._discovered = list(cameras)
        try:
            self._refresh_camera_table()
            self._update_status()
            self._status_bar_label.setText(
                f"Discovery complete: {len(self._discovered)} camera(s) found"
            )
        except RuntimeError:
            pass  # torn down mid-update

    def notify_transition_shown(self) -> None:
        """Arm the first-paint marker for transition instrumentation."""
        self._transition_paint_pending = True

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._transition_paint_pending:
            self._transition_paint_pending = False
            import logging as _logging
            import time as _time

            _logging.getLogger(__name__).info(
                "[MODE-TRANSITION] launcher_first_paint t_ns=%d",
                _time.perf_counter_ns(),
            )
        super().paintEvent(event)

    def set_mode_buttons_enabled(self, live_enabled: bool, config_enabled: bool) -> None:
        """Enable/disable mode buttons based on mutual exclusion."""
        self._live_btn.setEnabled(live_enabled)
        self._config_btn.setEnabled(config_enabled)

    def closeEvent(self, event) -> None:
        """Clean up on close."""
        super().closeEvent(event)


__all__ = ["LauncherWindow"]