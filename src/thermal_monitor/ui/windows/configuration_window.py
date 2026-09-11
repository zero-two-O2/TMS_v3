"""
ui.windows.configuration_window -- Configuration mode window.

Industrial thermal-camera configuration workstation inspired by ThermoView workflow:

    CONNECT CAMERA
          ->
    CONFIGURE ACQUISITION
          ->
    START ACQUISITION
          ->
    LIVE THERMAL IMAGE (dominant workspace)
          ->
    ANALYZE IMAGE
          ->
    CONFIGURE ROIs / ALARMS / MEASUREMENTS

Layout:
- Top toolbar (menu + camera selector + connection + acquisition)
- Main splitter: Left (Image Acquisition), Center (Thermal Image), Right (Scale + Analysis)
- Bottom status bar
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import Qt, QThread, QTimer, QObject, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QTabWidget,
    QStatusBar,
    QMessageBox,
    QFrame,
    QLabel,
    QMenuBar,
    QMenu,
    QApplication,
)
from PyQt6.QtGui import QColor, QAction

import numpy as np

from thermal_monitor.core.modes import ApplicationMode
from thermal_monitor.core.models import (
    CameraConfig,
    CameraIdentity,
    AnalysisConfig,
    CameraConnectionState,
)
from thermal_monitor.processing import ProcessingResult
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.runtime import CameraRuntimeService
from thermal_monitor.services.discovery import CameraDiscoveryService, GvcpDiscoveryService
from thermal_monitor.services.observer import ObserverService
from thermal_monitor.config import ConfigurationManager
from thermal_monitor.ui.configuration_editor import ConfigurationEditor
from thermal_monitor.ui.frame_rate import UniqueFrameRate
from thermal_monitor.ui.modes.observer_image import LiveThermalWidget, ROIOverlay
from thermal_monitor.ui.modes.vl_image import VlImageWidget
from thermal_monitor.ui.widgets import (
    ConfigCameraHeader,
    ThermalScalePanel,
    FrameInfoPanel,
    ROIPanel,
    AlarmPanel,
    StatisticsPanel,
    CameraSelectionDialog,
    ImageAcquisitionPanel,
)
from thermal_monitor.ui.theme import ThemeManager
from thermal_monitor.ui.theme.properties import set_role, set_status


logger = logging.getLogger(__name__)


_UNIT_SYMBOLS = {
    "celsius": "°C",
    "fahrenheit": "°F",
    "kelvin": "K",
}


class FocusWorker(QObject):
    """Off-GUI-thread focus operations (Stage 8D).

    GVCP round-trips + motor settle can block for seconds; this worker owns
    that latency so the live thermal display never freezes. One operation
    per worker instance; results return via queued signals.
    """

    read_finished = pyqtSignal(str, int, int, int)  # camera_id, min, max, current
    write_finished = pyqtSignal(str, int, int)  # camera_id, requested, readback
    failed = pyqtSignal(str, str)  # camera_id, message

    def __init__(
        self,
        runtime_service,
        camera_id: str,
        value_mm: "int | None",
        op_id: str | None = None,
    ) -> None:
        super().__init__()
        self._runtime_service = runtime_service
        self._camera_id = camera_id
        self._value_mm = value_mm  # None = read-only refresh
        if op_id is None:
            from thermal_monitor.services.runtime import new_focus_op_id

            op_id = new_focus_op_id(
                "FOCUS-WRITE" if value_mm is not None else "FOCUS-READ"
            )
        self._op_id = op_id

    @pyqtSlot()
    def run(self) -> None:
        import threading

        from PyQt6.QtCore import QThread

        logger.debug(
            "%s worker entered cam=%s py_thread=%s qt_thread=%s",
            self._op_id,
            self._camera_id,
            threading.get_ident(),
            int(QThread.currentThreadId()),
        )
        try:
            if self._value_mm is None:
                logger.debug("%s worker read start cam=%s", self._op_id, self._camera_id)
                vmin, vmax = self._runtime_service.get_focus_limits(
                    self._camera_id, self._op_id
                )
                current = self._runtime_service.get_focus_mm(self._camera_id, self._op_id)
                logger.debug(
                    "%s worker read done cam=%s min=%r max=%r current=%r",
                    self._op_id,
                    self._camera_id,
                    vmin,
                    vmax,
                    current,
                )
                logger.debug(
                    "%s worker emitting read_finished cam=%s", self._op_id, self._camera_id
                )
                self.read_finished.emit(self._camera_id, vmin, vmax, current)
            else:
                logger.debug(
                    "%s worker write start cam=%s value=%r",
                    self._op_id,
                    self._camera_id,
                    self._value_mm,
                )
                readback = self._runtime_service.set_focus_mm(
                    self._camera_id, self._value_mm, self._op_id
                )
                logger.debug(
                    "%s worker write done cam=%s requested=%r readback=%r",
                    self._op_id,
                    self._camera_id,
                    self._value_mm,
                    readback,
                )
                logger.debug(
                    "%s worker emitting write_finished cam=%s",
                    self._op_id,
                    self._camera_id,
                )
                self.write_finished.emit(self._camera_id, self._value_mm, readback)
        except Exception as exc:
            logger.warning(
                "%s worker failed cam=%s: %s", self._op_id, self._camera_id, exc
            )
            self.failed.emit(self._camera_id, str(exc)[:200])


class NucWorker(QObject):
    """Off-GUI-thread NUC operation (Stage 8G).

    The GVCP NUC write + stream-config verification can block for a
    moment; this worker owns that latency so the live display never
    freezes. One operation per worker instance; results return via queued
    signals. The custom GVSP stream keeps running throughout.
    """

    finished = pyqtSignal(str, float)  # camera_id, nuc_duration_s
    failed = pyqtSignal(str, str)  # camera_id, message

    def __init__(self, runtime_service, camera_id: str) -> None:
        super().__init__()
        self._runtime_service = runtime_service
        self._camera_id = camera_id

    @pyqtSlot()
    def run(self) -> None:
        try:
            result = self._runtime_service.perform_nuc(self._camera_id)
            duration = float(result.get("nuc_duration_s", 0.0))
            self.finished.emit(self._camera_id, duration)
        except Exception as exc:
            self.failed.emit(self._camera_id, str(exc)[:300])


class ConfigurationModeWidget(QWidget):
    """Main widget for Configuration mode - ThermoView-style workstation."""

    # Signal emitted when there are unsaved changes and user tries to switch cameras
    camera_switch_blocked = pyqtSignal(str, str)  # (current_camera_id, target_camera_id)

    def __init__(
        self,
        config_service: ConfigurationService,
        mode_service: ModeService,
        runtime_service: CameraRuntimeService | None = None,
        discovery_service: "CameraDiscoveryService | GvcpDiscoveryService | None" = None,
        theme_manager: Optional[ThemeManager] = None,
        config_manager: Optional[ConfigurationManager] = None,
    ) -> None:
        super().__init__()

        self._config_service = config_service
        self._mode_service = mode_service
        self._runtime_service = runtime_service
        self._discovery_service = discovery_service
        self._theme = theme_manager
        self._config_manager = config_manager
        self._selected_camera_id: str | None = None
        self._observer: ObserverService | None = None
        self._latest_result: ProcessingResult | None = None
        self._display_rate = UniqueFrameRate()
        self._config_editor: Optional[ConfigurationEditor] = None
        self._camera_selection_dialog: CameraSelectionDialog | None = None
        # Focus worker thread (at most one in flight; stale results dropped
        # by camera-id token when the selection changes mid-operation).
        # The worker object is retained (never a bare local) so the Python
        # wrapper cannot be garbage-collected while its thread runs.
        self._focus_thread: QThread | None = None
        self._focus_worker: FocusWorker | None = None
        self._focus_camera_id: str | None = None
        # NUC worker thread (at most one in flight; same stale-result rule).
        self._nuc_thread: QThread | None = None
        self._nuc_worker: NucWorker | None = None
        self._nuc_camera_id: str | None = None

        # Dirty state tracking for camera-specific configurations
        self._dirty_camera_configs: set[str] = set()
        self._pending_camera_switch: str | None = None

        self._setup_ui()
        self._connect_signals()
        self._load_initial_data()

    def _setup_ui(self) -> None:
        """Set up the industrial workstation-style UI layout."""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # --- Top Toolbar ---
        self._toolbar = ConfigCameraHeader(self._theme)
        self._toolbar.camera_selected.connect(self._on_camera_selected)
        self._toolbar.prev_camera_requested.connect(self._select_prev_camera)
        self._toolbar.next_camera_requested.connect(self._select_next_camera)
        self._toolbar.snapshot_requested.connect(self._on_snapshot)
        self._toolbar.save_requested.connect(self._on_save_config)
        main_layout.addWidget(self._toolbar)

        # --- Main content area: Three-pane splitter ---
        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(main_splitter, 1)

        # LEFT PANE: Image Acquisition panel (instrument panel)
        self._acq_panel = ImageAcquisitionPanel(self._theme)
        self._acq_panel.connect_requested.connect(self._on_connect)
        self._acq_panel.disconnect_requested.connect(self._on_disconnect)
        self._acq_panel.start_requested.connect(self._on_start_acquisition)
        self._acq_panel.stop_requested.connect(self._on_stop_acquisition)
        self._acq_panel.change_requested.connect(self._on_change_acquisition)
        self._acq_panel.fps_changed.connect(self._on_fps_changed)
        self._acq_panel.averaging_changed.connect(self._on_averaging_changed)
        self._acq_panel.history_changed.connect(self._on_history_changed)
        self._acq_panel.focus_set_requested.connect(self._on_focus_set_requested)
        self._acq_panel.focus_refresh_requested.connect(self._on_focus_refresh_requested)
        self._acq_panel.nuc_requested.connect(self._on_nuc_requested)
        # Diagnostic direct slot: proves the physical click reaches Qt
        # independently of the worker chain (see _debug_focus_apply_clicked).
        self._acq_panel.focus_apply_button.clicked.connect(
            self._debug_focus_apply_clicked
        )
        logger.info(
            "APPLY BUTTON SIGNAL CONNECTED panel_id=%r btn_id=%r name=%s",
            id(self._acq_panel),
            id(self._acq_panel.focus_apply_button),
            self._acq_panel.focus_apply_button.objectName(),
        )
        logger.debug(
            "Panel wired id=%r apply_btn=%r visible=%s",
            id(self._acq_panel),
            id(self._acq_panel.focus_apply_button),
            self._acq_panel.focus_apply_button.isVisible(),
        )
        main_splitter.addWidget(self._acq_panel)

        # CENTER PANE: Large thermal image (primary workspace)
        center_widget = QWidget()
        center_layout = QVBoxLayout(center_widget)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)

        self._image_widget = LiveThermalWidget()
        self._image_widget.cursor_temperature_changed.connect(self._on_cursor_temperature)
        self._image_widget.rendered_frame.connect(self._on_rendered_frame)
        self._image_widget.render_error.connect(self._on_render_error)
        self._vl_widget = VlImageWidget()
        self._vl_widget.render_error.connect(self._on_render_error)
        # IR (dominant) + VL side-by-side for the selected camera (Stage 8D/8E
        # dual-feed); the splitter preserves the thermal workspace priority.
        ir_vl_splitter = QSplitter(Qt.Orientation.Horizontal)
        ir_vl_splitter.addWidget(self._image_widget)
        ir_vl_splitter.addWidget(self._vl_widget)
        ir_vl_splitter.setSizes([700, 420])
        ir_vl_splitter.setStretchFactor(0, 3)
        ir_vl_splitter.setStretchFactor(1, 2)
        center_layout.addWidget(ir_vl_splitter, 1)

        main_splitter.addWidget(center_widget)

        # RIGHT PANE: Temperature scale + Analysis tabs
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        # Temperature scale panel (ThermoView-style)
        self._scale_panel = ThermalScalePanel(self._theme)
        self._scale_panel.palette_changed.connect(self._on_palette_changed)
        self._scale_panel.auto_range_toggled.connect(self._on_auto_range_toggled)
        self._scale_panel.manual_range_applied.connect(self._on_apply_range)
        self._scale_panel.zoom_changed.connect(self._on_zoom_changed)
        right_layout.addWidget(self._scale_panel)

        # Analysis tabs
        self._analysis_tabs = QTabWidget()
        right_layout.addWidget(self._analysis_tabs, 1)

        # ROI tab
        self._roi_panel = ROIPanel(self._config_service, self._theme)
        self._roi_panel.roi_selected.connect(self._on_roi_selected)
        self._roi_panel.roi_created.connect(self._on_roi_created)
        self._roi_panel.roi_updated.connect(self._on_roi_updated)
        self._roi_panel.roi_deleted.connect(self._on_roi_deleted)
        self._analysis_tabs.addTab(self._roi_panel, "ROIs")

        # Alarm tab
        self._alarm_panel = AlarmPanel(self._config_service, self._theme)
        self._alarm_panel.alarm_selected.connect(self._on_alarm_selected)
        self._analysis_tabs.addTab(self._alarm_panel, "Alarms")

        # Statistics tab
        self._stats_panel = StatisticsPanel(self._theme)
        self._analysis_tabs.addTab(self._stats_panel, "Statistics")

        # Configuration Editor tab (deployment config)
        if self._config_manager:
            self._config_editor = ConfigurationEditor(
                config_manager=self._config_manager,
                theme_manager=self._theme,
            )
            self._config_editor.config_saved.connect(self._on_config_saved)
            self._config_editor.config_error.connect(self._on_config_error)
            self._config_editor.restart_required.connect(self._on_restart_required)
            self._analysis_tabs.addTab(self._config_editor, "Configuration Editor")

        main_splitter.addWidget(right_widget)

        # Set splitter proportions: Left(280), Center(700+), Right(400)
        main_splitter.setSizes([280, 740, 400])
        main_splitter.setStretchFactor(0, 0)  # Left fixed
        main_splitter.setStretchFactor(1, 1)  # Center expands
        main_splitter.setStretchFactor(2, 0)  # Right fixed

        # --- Bottom: Status bar ---
        self._create_status_bar(main_layout)

    def _create_status_bar(self, parent_layout: QVBoxLayout) -> None:
        """Create status bar at bottom."""
        status_frame = QFrame()
        status_frame.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Sunken)
        status_layout = QHBoxLayout(status_frame)
        status_layout.setContentsMargins(8, 4, 8, 4)
        status_layout.setSpacing(16)

        self._status_fps = QLabel("FPS: —")
        self._status_proc = QLabel("Processing: — ms")
        self._status_conn = QLabel("Connection: —")
        self._status_frames = QLabel("Frames: 0")
        self._status_label = QLabel("Ready")

        for label in [self._status_fps, self._status_proc, self._status_conn, self._status_frames, self._status_label]:
            set_role(label, "status")

        status_layout.addWidget(self._status_fps)
        status_layout.addWidget(QLabel("|"))
        status_layout.addWidget(self._status_proc)
        status_layout.addWidget(QLabel("|"))
        status_layout.addWidget(self._status_conn)
        status_layout.addWidget(QLabel("|"))
        status_layout.addWidget(self._status_frames)
        status_layout.addStretch()
        status_layout.addWidget(self._status_label)

        parent_layout.addWidget(status_frame)

    def _connect_signals(self) -> None:
        """Connect internal signals."""
        self._config_service.add_camera_change_callback(self._on_camera_config_changed)
        self._config_service.add_analysis_change_callback(self._on_analysis_config_changed)

        # Image widget range changes
        self._image_widget.range_changed.connect(self._scale_panel.update_range)

        # Stats timer
        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._update_stats)
        self._stats_timer.start(1000)

    def _load_initial_data(self) -> None:
        """Load initial configuration data."""
        self._refresh_camera_list()
        if self._selected_camera_id:
            self._load_camera_config(self._selected_camera_id)
        else:
            # Select first camera if available
            cameras = self._config_service.get_all_camera_configs()
            if cameras:
                self._toolbar.select_camera_by_id(cameras[0].identity.camera_id)

    def _refresh_camera_list(self) -> None:
        """Refresh the camera list in toolbar."""
        cameras = self._config_service.get_all_camera_configs()
        camera_list = []
        for config in cameras:
            identity = config.identity
            display = f"{identity.serial_number} — {identity.model}"
            if config.name and config.name != identity.camera_id:
                display = f"{config.name} ({display})"
            if not config.enabled:
                display = f"[Disabled] {display}"
            camera_list.append((identity.camera_id, display, identity, config.enabled))

        self._toolbar.set_cameras(camera_list)

    def _on_camera_selected(self, camera_id: str) -> None:
        """Handle camera selection change with dirty state check."""
        if self._has_unsaved_changes(camera_id):
            self._pending_camera_switch = camera_id
            self._show_unsaved_changes_dialog(camera_id)
        else:
            self._switch_camera(camera_id)

    def _has_unsaved_changes(self, target_camera_id: str) -> bool:
        """Check if current camera has unsaved changes."""
        return (
            self._selected_camera_id in self._dirty_camera_configs
            and self._selected_camera_id != target_camera_id
        )

    def _show_unsaved_changes_dialog(self, target_camera_id: str) -> None:
        """Show dialog for unsaved changes when switching cameras."""
        msg = QMessageBox(self)
        msg.setWindowTitle("Unsaved Changes")
        msg.setText(f"Camera '{self._selected_camera_id}' has unsaved changes.")
        msg.setInformativeText("Do you want to save before switching?")
        msg.setStandardButtons(
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel
        )
        msg.setDefaultButton(QMessageBox.StandardButton.Save)
        result = msg.exec()

        if result == QMessageBox.StandardButton.Save:
            self._save_current_camera_config()
            self._switch_camera(target_camera_id)
        elif result == QMessageBox.StandardButton.Discard:
            self._discard_current_camera_changes()
            self._switch_camera(target_camera_id)
        else:  # Cancel
            self._toolbar.select_camera_by_id(self._selected_camera_id)

    def _save_current_camera_config(self) -> None:
        """Save current camera configuration."""
        if self._selected_camera_id:
            self._dirty_camera_configs.discard(self._selected_camera_id)
            self._roi_panel.clear_dirty(self._selected_camera_id)
            self._alarm_panel.clear_dirty(self._selected_camera_id)

    def _discard_current_camera_changes(self) -> None:
        """Discard current camera changes."""
        if self._selected_camera_id:
            self._dirty_camera_configs.discard(self._selected_camera_id)
            self._roi_panel.clear_dirty(self._selected_camera_id)
            self._alarm_panel.clear_dirty(self._selected_camera_id)
            self._load_camera_config(self._selected_camera_id)

    def _select_prev_camera(self) -> None:
        """Select previous camera in list."""
        self._toolbar.select_camera_by_id(self._get_prev_camera_id())

    def _select_next_camera(self) -> None:
        """Select next camera in list."""
        self._toolbar.select_camera_by_id(self._get_next_camera_id())

    def _get_prev_camera_id(self) -> str | None:
        cameras = self._config_service.get_all_camera_configs()
        if not cameras or not self._selected_camera_id:
            return None
        current_idx = next((i for i, c in enumerate(cameras) if c.identity.camera_id == self._selected_camera_id), -1)
        if current_idx > 0:
            return cameras[current_idx - 1].identity.camera_id
        return None

    def _get_next_camera_id(self) -> str | None:
        cameras = self._config_service.get_all_camera_configs()
        if not cameras or not self._selected_camera_id:
            return None
        current_idx = next((i for i, c in enumerate(cameras) if c.identity.camera_id == self._selected_camera_id), -1)
        if current_idx >= 0 and current_idx < len(cameras) - 1:
            return cameras[current_idx + 1].identity.camera_id
        return None

    def _switch_camera(self, camera_id: str) -> None:
        """Switch to a different camera (without starting acquisition)."""
        # Stop previous camera's observer (but NOT acquisition)
        if self._observer is not None:
            self._observer.stop()
            self._observer = None

        self._selected_camera_id = camera_id
        self._load_camera_config(camera_id)

        # Update status
        self._status_label.setText(f"Camera: {camera_id}")

    def _load_camera_config(self, camera_id: str) -> None:
        """Load configuration for the selected camera into all panels."""
        config = self._config_service.get_camera_config(camera_id)
        if not config:
            self._clear_all_panels()
            return

        identity = config.identity
        metadata = dict(config.metadata or {})

        # Determine connection status
        status = self._get_camera_connection_status(camera_id)

        # Update toolbar
        self._toolbar.set_connection_state(status)

        # Update acquisition panel
        self._acq_panel.set_camera_identity(identity)
        self._acq_panel.set_connection_state(status)

        # Update acquisition controls from metadata
        fps = int(metadata.get("frame_rate", 9))
        self._acq_panel.set_requested_fps(fps)

        # Update panels
        self._roi_panel.set_camera(camera_id)
        self._alarm_panel.set_camera(camera_id)
        self._stats_panel.clear()

        # Update ROI overlays
        self._update_roi_overlays()

        # Update acquisition button states (only on acq_panel now)
        if self._runtime_service is not None:
            running = self._runtime_service.is_camera_running(camera_id)
            self._acq_panel.set_acquisition_running(running)

        # Refresh focus + NUC state for the selected camera (async; no-op
        # when the camera is not running).
        self._refresh_focus_panel()
        self._refresh_nuc_panel()

    # -- Focus (UI -> runtime/service -> driver, never GVCP directly) --

    def _stop_focus_worker(self, timeout_ms: int = 3000) -> None:
        """Request the focus thread to stop and wait for it (bounded).

        A timeout expiry is logged loudly and never silent: the thread is
        left to quit itself via its finished/failed -> quit chain once the
        in-flight GVCP operation returns (all GVCP transactions are
        bounded), so it is never destroyed while running.
        """
        thread, self._focus_thread = self._focus_thread, None
        self._focus_worker = None
        if thread is not None:
            thread.quit()
            if not thread.wait(timeout_ms):
                logger.warning(
                    "Focus worker thread still running after %d ms; "
                    "leaving it to quit itself on operation completion",
                    timeout_ms,
                )

    def _start_focus_operation(self, camera_id: str, value_mm: "int | None") -> None:
        """Run one focus read (None) or write in a worker thread."""
        from thermal_monitor.services.runtime import new_focus_op_id

        op_id = new_focus_op_id("FOCUS-WRITE" if value_mm is not None else "FOCUS-READ")
        logger.debug(
            "%s UI request cam=%s selected=%s running=%s mode=%s",
            op_id,
            camera_id,
            self._selected_camera_id,
            (
                self._runtime_service.is_camera_running(camera_id)
                if self._runtime_service is not None
                else False
            ),
            "read" if value_mm is None else f"write {value_mm}mm",
        )
        if self._runtime_service is None:
            self._acq_panel.set_focus_enabled(False, "Runtime unavailable")
            return
        self._stop_focus_worker()
        self._focus_camera_id = camera_id
        self._focus_thread = QThread(self)
        self._focus_thread.setObjectName(f"FocusWorker-{camera_id}")
        worker = FocusWorker(self._runtime_service, camera_id, value_mm, op_id=op_id)
        self._focus_worker = worker
        worker.moveToThread(self._focus_thread)
        self._focus_thread.started.connect(worker.run)
        worker.read_finished.connect(self._on_focus_read_finished)
        worker.write_finished.connect(self._on_focus_write_finished)
        worker.failed.connect(self._on_focus_failed)
        worker.read_finished.connect(self._focus_thread.quit)
        worker.write_finished.connect(self._focus_thread.quit)
        worker.failed.connect(self._focus_thread.quit)
        logger.debug(
            "%s thread starting name=%s worker=%r",
            op_id,
            self._focus_thread.objectName(),
            worker,
        )
        self._focus_thread.start()

    def _refresh_focus_panel(self) -> None:
        """Enable + read focus when the selected camera runs; else disable."""
        camera_id = self._selected_camera_id
        if (
            camera_id is None
            or self._runtime_service is None
            or not self._runtime_service.is_camera_running(camera_id)
        ):
            self._stop_focus_worker()
            self._focus_camera_id = None
            self._acq_panel.set_focus_enabled(False, "Camera not running")
            return
        self._acq_panel.set_focus_enabled(True)
        self._acq_panel.set_focus_busy("Reading…")
        self._start_focus_operation(camera_id, None)

    def _debug_focus_apply_clicked(self, checked: bool = False) -> None:
        """Diagnostic direct slot: proves the physical click reaches Qt.

        Connected straight to ``focusApplyButton.clicked`` alongside the
        normal path. Involves no worker/runtime/driver call by design: if
        this line never appears, the mouse event never reached the button
        (overlay, geometry, enabled-state, or event-filter cause).
        """
        import threading

        from PyQt6.QtCore import QThread

        try:
            btn = self._acq_panel.focus_apply_button
            geo = btn.geometry()
            info = (
                f"name={btn.objectName()} id={id(btn)} "
                f"enabled={btn.isEnabled()} visible={btn.isVisible()} "
                f"enabledToWindow={btn.isEnabledTo(btn.window())} "
                f"visibleToWindow={btn.isVisibleTo(btn.window())} "
                f"underMouse={btn.underMouse()} "
                f"geometry={geo.x()},{geo.y()},{geo.width()}x{geo.height()} "
                f"global={btn.mapToGlobal(geo.topLeft()).x()},"
                f"{btn.mapToGlobal(geo.topLeft()).y()} "
                f"parentEnabled={btn.parentWidget().isEnabled() if btn.parentWidget() else '?'} "
                f"parentVisible={btn.parentWidget().isVisible() if btn.parentWidget() else '?'}"
            )
        except RuntimeError as exc:
            info = f"unreadable (deleted?): {exc}"
        try:
            requested = self._acq_panel.focus_spin_value
        except Exception as exc:
            requested = f"unreadable: {exc}"
        logger.warning(
            "FOCUS APPLY BUTTON CLICKED cam=%s requested=%r %s "
            "py_thread=%s qt_thread=%s",
            self._selected_camera_id,
            requested,
            info,
            threading.get_ident(),
            int(QThread.currentThreadId()),
        )

    def _on_focus_set_requested(self, value_mm: int) -> None:
        import threading

        from PyQt6.QtCore import QThread

        camera_id = self._selected_camera_id
        try:
            sender = self.sender()
            sender_info = (
                f"{type(sender).__name__}:{sender.objectName()}:id={id(sender)}"
                if sender is not None
                else "unknown"
            )
        except Exception:
            sender_info = "unknown"
        try:
            apply_btn = self._acq_panel.focus_apply_button
            btn_info = (
                f"enabled={apply_btn.isEnabled()} "
                f"visible={apply_btn.isVisible()} "
                f"enabledToWindow={apply_btn.isEnabledTo(apply_btn.window())} "
                f"name={apply_btn.objectName()} id={id(apply_btn)}"
            )
            displayed = self._acq_panel._focus_current_label.text()
        except Exception as exc:
            btn_info = f"unreadable: {exc}"
            displayed = "?"
        logger.info(
            "FOCUS-UI APPLY CLICKED cam=%s selected=%s requested=%r "
            "displayed=%r button[%s] sender=%s py_thread=%s qt_thread=%s",
            camera_id,
            self._selected_camera_id,
            value_mm,
            displayed,
            btn_info,
            sender_info,
            threading.get_ident(),
            int(QThread.currentThreadId()),
        )
        if camera_id is None:
            logger.warning("FOCUS-UI APPLY dropped: no camera selected")
            return
        self._acq_panel.set_focus_busy("Writing…")
        self._start_focus_operation(camera_id, value_mm)

    def _on_focus_refresh_requested(self) -> None:
        camera_id = self._selected_camera_id
        if camera_id is None:
            return
        self._acq_panel.set_focus_busy("Reading…")
        self._start_focus_operation(camera_id, None)

    def _on_focus_read_finished(self, camera_id: str, vmin: int, vmax: int, current: int) -> None:
        if camera_id != self._selected_camera_id:
            logger.debug(
                "Focus read dropped (stale) cam=%s selected=%s",
                camera_id,
                self._selected_camera_id,
            )
            return  # stale result after camera switch
        logger.debug(
            "Focus read finished cam=%s current=%d range=[%d,%d]",
            camera_id,
            current,
            vmin,
            vmax,
        )
        self._acq_panel.set_focus_enabled(True)
        self._acq_panel.set_focus_state(current, vmin, vmax)

    def _on_focus_write_finished(self, camera_id: str, requested: int, readback: int) -> None:
        if camera_id != self._selected_camera_id:
            logger.debug(
                "Focus write dropped (stale) cam=%s selected=%s",
                camera_id,
                self._selected_camera_id,
            )
            return
        logger.debug(
            "Focus write finished cam=%s requested=%r readback=%r",
            camera_id,
            requested,
            readback,
        )
        self._acq_panel.set_focus_result(requested, readback)

    def _on_focus_failed(self, camera_id: str, message: str) -> None:
        if camera_id != self._selected_camera_id:
            logger.debug(
                "Focus failure dropped (stale) cam=%s selected=%s: %s",
                camera_id,
                self._selected_camera_id,
                message,
            )
            return
        logger.warning("Focus operation failed cam=%s: %s", camera_id, message)
        self._acq_panel.set_focus_error(message)

    # -- NUC (Stage 8G; UI -> runtime/service -> driver, never GVCP directly) --

    def _stop_nuc_worker(self, timeout_ms: int = 8000) -> None:
        """Request the NUC thread to stop and wait for it (bounded).

        Same contract as :meth:`_stop_focus_worker`: a timeout is logged,
        never silent, and the thread is left to quit itself rather than
        destroyed while running.
        """
        thread, self._nuc_thread = self._nuc_thread, None
        self._nuc_worker = None
        if thread is not None:
            thread.quit()
            if not thread.wait(timeout_ms):
                logger.warning(
                    "NUC worker thread still running after %d ms; "
                    "leaving it to quit itself on operation completion",
                    timeout_ms,
                )

    def _refresh_nuc_panel(self) -> None:
        """Enable NUC when the selected camera runs; else disable."""
        camera_id = self._selected_camera_id
        if (
            camera_id is None
            or self._runtime_service is None
            or not self._runtime_service.is_camera_running(camera_id)
        ):
            self._stop_nuc_worker()
            self._nuc_camera_id = None
            self._acq_panel.set_nuc_enabled(False, "Camera not running")
            return
        self._acq_panel.set_nuc_enabled(True)

    def _on_nuc_requested(self) -> None:
        camera_id = self._selected_camera_id
        if camera_id is None or self._runtime_service is None:
            return
        if not self._runtime_service.is_camera_running(camera_id):
            self._acq_panel.set_nuc_error("Camera not running")
            return
        self._stop_nuc_worker()
        self._nuc_camera_id = camera_id
        self._acq_panel.set_nuc_enabled(True)
        self._acq_panel.set_nuc_busy("NUC running…")
        self._nuc_thread = QThread(self)
        self._nuc_thread.setObjectName(f"NucWorker-{camera_id}")
        worker = NucWorker(self._runtime_service, camera_id)
        self._nuc_worker = worker
        worker.moveToThread(self._nuc_thread)
        self._nuc_thread.started.connect(worker.run)
        worker.finished.connect(self._on_nuc_finished)
        worker.failed.connect(self._on_nuc_failed)
        worker.finished.connect(self._nuc_thread.quit)
        worker.failed.connect(self._nuc_thread.quit)
        logger.debug("NUC operation start cam=%s", camera_id)
        self._nuc_thread.start()

    def _on_nuc_finished(self, camera_id: str, duration_s: float) -> None:
        if camera_id != self._selected_camera_id:
            logger.debug(
                "NUC result dropped (stale) cam=%s selected=%s",
                camera_id,
                self._selected_camera_id,
            )
            return
        logger.info("NUC finished cam=%s duration=%.3fs", camera_id, duration_s)
        self._acq_panel.set_nuc_enabled(True)
        self._acq_panel.set_nuc_result(duration_s)

    def _on_nuc_failed(self, camera_id: str, message: str) -> None:
        if camera_id != self._selected_camera_id:
            logger.debug(
                "NUC failure dropped (stale) cam=%s selected=%s: %s",
                camera_id,
                self._selected_camera_id,
                message,
            )
            return
        logger.warning("NUC operation failed cam=%s: %s", camera_id, message)
        self._acq_panel.set_nuc_enabled(True)
        self._acq_panel.set_nuc_error(message)

    def _get_camera_connection_status(self, camera_id: str) -> CameraConnectionState:
        """Get the connection status of a camera."""
        if self._runtime_service is not None:
            if self._runtime_service.is_camera_running(camera_id):
                return CameraConnectionState.ACQUIRING
            # Check if camera is connected (runtime exists but not acquiring)
            # For now, we treat any configured camera as DISCONNECTED unless acquiring
            config = self._config_service.get_camera_config(camera_id)
            if config and config.enabled:
                return CameraConnectionState.DISCONNECTED
        config = self._config_service.get_camera_config(camera_id)
        if config:
            if not config.enabled:
                return CameraConnectionState.DISCONNECTED
        return CameraConnectionState.DISCONNECTED

    def _clear_all_panels(self) -> None:
        """Clear all panels when no camera selected."""
        self._image_widget.clear()
        self._vl_widget.clear()
        self._image_widget.set_roi_overlays([])
        self._scale_panel.update_cursor_temperature(None)
        self._roi_panel.set_camera("")
        self._alarm_panel.set_camera("")
        self._stats_panel.clear()
        self._toolbar.set_connection_state(CameraConnectionState.DISCONNECTED)
        self._acq_panel.set_camera_identity(None)
        self._acq_panel.set_connection_state(CameraConnectionState.DISCONNECTED)
        self._acq_panel.clear_image_info()

    @pyqtSlot(object)
    def _on_processing_result(self, result: ProcessingResult) -> None:
        """Receive ProcessingResult from observer."""
        frame = result.frame
        sequence = frame.descriptor.sequence if frame is not None else None
        if sequence is not None and not self._display_rate.add(sequence):
            return

        self._latest_result = result

        # CRITICAL: copy the temperature buffer before retaining any display
        # data, so we never share memory with the consumer's mutable result
        # (same guarantee as the live wall tiles).
        temperature_image = result.temperature_image
        if temperature_image is not None:
            temperature_image = np.asarray(temperature_image).copy()
        minimum = maximum = None
        if result.analysis_result is not None:
            minimum = result.analysis_result.overall_min
            maximum = result.analysis_result.overall_max
        self._image_widget.set_frame(temperature_image, frame, minimum, maximum)

        # VL display (Stage 8E): same result -> same hardware frame, so the
        # VL image shown always corresponds to the IR image shown.
        if frame is not None and frame.payload.visible is not None:
            self._vl_widget.set_frame(
                frame.payload.visible,
                frame.descriptor.sequence,
                frame.descriptor.visible.sequence,
            )
        else:
            self._vl_widget.set_frame(None, sequence if sequence is not None else -1)

        # Update image info
        if frame:
            self._acq_panel.update_image_info(
                image_size=f"{frame.payload.thermal.shape[1]}×{frame.payload.thermal.shape[0]}" if frame.payload.thermal is not None else "—",
                frame=str(frame.descriptor.sequence),
                timestamp=f"{frame.descriptor.timestamp:.3f}",
                processing=f"{result.processing_time_ms:.1f} ms",
            )

        # Update analysis results
        analysis = result.analysis_result
        if analysis is not None:
            self._stats_panel.update_from_analysis(analysis)
            self._roi_panel.update_live_stats(analysis)
            self._alarm_panel.update_live_alarms(result.alarm_result, analysis)

        # Update ROI overlays
        self._update_roi_overlays()

        # Update FPS
        if self._runtime_service and self._selected_camera_id:
            stats = self._runtime_service.camera_stats(self._selected_camera_id)
            if stats:
                self._acq_panel.set_acquisition_fps(stats.current_fps or stats.average_fps)

        if self._observer:
            obs_stats = self._observer.stats()
            if obs_stats:
                self._acq_panel.set_display_fps(self._display_rate.fps())

    @pyqtSlot(object, object)
    def _on_rendered_frame(self, image, thumbnail) -> None:
        """Use the worker's single rendered image for both displays."""
        self._scale_panel.update_view_finder_image(thumbnail)

    @pyqtSlot(str)
    def _on_render_error(self, message: str) -> None:
        self._status_label.setText(f"Thermal render error: {message}")

    @pyqtSlot(float)
    def _on_cursor_temperature(self, temp: float) -> None:
        """Handle cursor temperature from image widget."""
        unit_symbol = "°C"
        if self._latest_result and self._latest_result.analysis_result:
            unit = getattr(self._latest_result.analysis_result, "unit", None)
            unit_symbol = _UNIT_SYMBOLS.get(unit.value if hasattr(unit, 'value') else str(unit), "°C")
        self._scale_panel.update_cursor_temperature(temp if not np.isnan(temp) else None, unit_symbol)

    # Connection workflow handlers

    def _on_connect(self) -> None:
        """Handle Connect button - show camera selection dialog."""
        if not self._discovery_service:
            QMessageBox.warning(self, "Connect Failed", "Camera discovery service not available.")
            return

        # Create and show camera selection dialog
        if self._camera_selection_dialog is None:
            self._camera_selection_dialog = CameraSelectionDialog(
                discovery_service=self._discovery_service,
                theme_manager=self._theme,
                parent=self,
            )
            self._camera_selection_dialog.camera_selected.connect(self._on_camera_selected_from_dialog)
            self._camera_selection_dialog.discovery_finished.connect(self._restore_connect_cursor)
            self._camera_selection_dialog.discovery_failed.connect(self._on_discovery_failed)

        self._camera_selection_dialog.show()
        self._camera_selection_dialog.raise_()
        self._camera_selection_dialog.activateWindow()
        self._toolbar.set_connection_state(CameraConnectionState.CONNECTING)
        self._acq_panel.set_connection_state(CameraConnectionState.CONNECTING)
        self._status_conn.setText("Connection: Discovering...")
        self._status_label.setText("Discovering cameras...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

    def _restore_connect_cursor(self, *args) -> None:
        if QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()

    def _on_discovery_failed(self, message: str) -> None:
        self._restore_connect_cursor()
        self._status_conn.setText("Connection: Discovery failed")

    def _on_camera_selected_from_dialog(self, discovered_camera) -> None:
        """Handle camera selection from dialog - connect to the selected camera."""
        if not self._runtime_service:
            QMessageBox.warning(self, "Connect Failed", "Runtime service not available.")
            return

        # Create or find camera identity from discovered camera
        camera_id = discovered_camera.camera_id  # e.g., "cam_HB25100004"

        # Check if configuration already exists for this camera
        existing_config = self._config_service.get_camera_config(camera_id)

        if existing_config:
            # Use existing configuration, update metadata with discovered info
            config = existing_config
        else:
            # Create new default configuration from discovered camera
            identity = self._config_service.create_camera_identity(
                camera_id=camera_id,
                serial_number=discovered_camera.serial_number,
                model=discovered_camera.model,
                vendor=discovered_camera.vendor,
                firmware=discovered_camera.firmware,
                user_name=discovered_camera.user_name,
            )
            config = self._config_service.create_camera_config(
                identity=identity,
                name=discovered_camera.user_name or camera_id,
            )

        # Update config metadata with discovered camera info
        metadata = dict(config.metadata or {})
        metadata["device_identifier"] = discovered_camera.device_identifier
        metadata["ip_address"] = discovered_camera.ip_address
        metadata["model"] = discovered_camera.model
        metadata["vendor"] = discovered_camera.vendor
        metadata["firmware"] = discovered_camera.firmware
        metadata["user_name"] = discovered_camera.user_name

        updated_config = CameraConfig(
            identity=config.identity,
            name=config.name,
            description=config.description,
            enabled=config.enabled,
            thermal_enabled=config.thermal_enabled,
            visible_enabled=config.visible_enabled,
            ptz_config=config.ptz_config,
            tags=config.tags,
            metadata=metadata,
        )
        self._config_service.set_camera_config(updated_config)

        # Select this camera in the toolbar (sets _selected_camera_id)
        self._toolbar.select_camera_by_id(camera_id)
        self._selected_camera_id = camera_id

        # Update toolbar to connecting state
        self._toolbar.set_connection_state(CameraConnectionState.CONNECTING)
        self._acq_panel.set_connection_state(CameraConnectionState.CONNECTING)

        try:
            # Start camera runtime
            self._runtime_service.start_camera(updated_config)

            # Update to CONNECTED state (not yet acquiring)
            self._toolbar.set_connection_state(CameraConnectionState.CONNECTED)
            self._acq_panel.set_connection_state(CameraConnectionState.CONNECTED)
            self._status_conn.setText("Connection: Connected")
            self._status_label.setText("Camera connected - press Start to begin acquisition")
            self._refresh_focus_panel()
            self._refresh_nuc_panel()

        except Exception as exc:
            self._toolbar.set_connection_state(CameraConnectionState.ERROR)
            self._acq_panel.set_connection_state(CameraConnectionState.ERROR)
            self._status_conn.setText(f"Connection: Error")
            self._acq_panel.set_focus_enabled(False, "Camera not running")
            self._acq_panel.set_nuc_enabled(False, "Camera not running")
            QMessageBox.warning(self, "Connect Failed", f"Failed to connect to camera: {exc}")

    def _on_disconnect(self) -> None:
        """Handle Disconnect button."""
        if not self._selected_camera_id or not self._runtime_service:
            return

        try:
            # Stop acquisition if running
            if self._runtime_service.is_camera_running(self._selected_camera_id):
                self._runtime_service.stop_camera(self._selected_camera_id)

            if self._observer:
                self._observer.stop()
                self._observer = None

            self._acq_panel.set_acquisition_running(False)

            # Focus + NUC no longer available once the camera stops.
            self._stop_focus_worker()
            self._focus_camera_id = None
            self._acq_panel.set_focus_enabled(False, "Camera not running")
            self._stop_nuc_worker()
            self._nuc_camera_id = None
            self._acq_panel.set_nuc_enabled(False, "Camera not running")

            # Update to DISCONNECTED state
            self._toolbar.set_connection_state(CameraConnectionState.DISCONNECTED)
            self._acq_panel.set_connection_state(CameraConnectionState.DISCONNECTED)
            self._status_conn.setText("Connection: Disconnected")
            self._status_label.setText("Camera disconnected")
            self._image_widget.clear()
            self._vl_widget.clear()

        except Exception as exc:
            QMessageBox.warning(self, "Disconnect Failed", f"Failed to disconnect: {exc}")

    def _on_start_acquisition(self) -> None:
        """Handle Start acquisition button."""
        if not self._selected_camera_id or not self._runtime_service:
            return

        config = self._config_service.get_camera_config(self._selected_camera_id)
        if not config:
            return

        # Update metadata with current spin values
        metadata = dict(config.metadata or {})
        metadata["frame_rate"] = self._acq_panel.get_requested_fps()

        updated_config = CameraConfig(
            identity=config.identity,
            name=config.name,
            description=config.description,
            enabled=config.enabled,
            thermal_enabled=config.thermal_enabled,
            visible_enabled=config.visible_enabled,
            ptz_config=config.ptz_config,
            tags=config.tags,
            metadata=metadata,
        )
        self._config_service.set_camera_config(updated_config)

        previous_state = self._acq_panel._connection_state
        try:
            # Camera runtime is the authority; this attaches the single consumer.
            analysis = self._config_service.get_analysis_config(self._selected_camera_id)
            if analysis is None:
                analysis = AnalysisConfig(camera_id=self._selected_camera_id)
            self._observer = self._runtime_service.start_observer(self._selected_camera_id, analysis_config=analysis)
            self._observer.result_ready.connect(self._on_processing_result, Qt.ConnectionType.QueuedConnection)
            self._observer.error_occurred.connect(self._on_observer_error, Qt.ConnectionType.QueuedConnection)

        except Exception as exc:
            self._observer = None
            self._acq_panel.set_connection_state(previous_state)
            QMessageBox.warning(self, "Start Failed", f"Failed to start acquisition: {exc}")
            return

        # Presentation updates are outside the runtime transaction.
        self._display_rate.reset()
        self._acq_panel.set_acquisition_running(True)
        self._toolbar.set_connection_state(CameraConnectionState.ACQUIRING)
        self._acq_panel.set_connection_state(CameraConnectionState.ACQUIRING)
        self._status_conn.setText("Connection: Acquiring")
        self._status_label.setText("Acquisition started")

    def _on_stop_acquisition(self) -> None:
        """Handle Stop acquisition button."""
        if not self._selected_camera_id or not self._runtime_service:
            return

        try:
            if self._observer:
                self._observer.stop()
                self._observer = None

            self._acq_panel.set_acquisition_running(False)
            self._toolbar.set_connection_state(CameraConnectionState.CONNECTED)
            self._acq_panel.set_connection_state(CameraConnectionState.CONNECTED)
            self._status_conn.setText("Connection: Connected")
            self._status_label.setText("Acquisition stopped")

        except Exception as exc:
            QMessageBox.warning(self, "Stop Failed", f"Failed to stop acquisition: {exc}")

    def _on_change_acquisition(self) -> None:
        """Handle Change button - reconfigure acquisition parameters."""
        # For now, just update the config with current values
        if not self._selected_camera_id:
            return

        config = self._config_service.get_camera_config(self._selected_camera_id)
        if not config:
            return

        metadata = dict(config.metadata or {})
        metadata["frame_rate"] = self._acq_panel.get_requested_fps()

        updated_config = CameraConfig(
            identity=config.identity,
            name=config.name,
            description=config.description,
            enabled=config.enabled,
            thermal_enabled=config.thermal_enabled,
            visible_enabled=config.visible_enabled,
            ptz_config=config.ptz_config,
            tags=config.tags,
            metadata=metadata,
        )
        self._config_service.set_camera_config(updated_config)
        self._status_label.setText("Acquisition parameters updated")

    def _on_fps_changed(self, fps: int) -> None:
        """Handle FPS change."""
        if self._selected_camera_id:
            config = self._config_service.get_camera_config(self._selected_camera_id)
            if config:
                metadata = dict(config.metadata or {})
                metadata["frame_rate"] = fps
                updated_config = CameraConfig(
                    identity=config.identity,
                    name=config.name,
                    description=config.description,
                    enabled=config.enabled,
                    thermal_enabled=config.thermal_enabled,
                    visible_enabled=config.visible_enabled,
                    ptz_config=config.ptz_config,
                    tags=config.tags,
                    metadata=metadata,
                )
                self._config_service.set_camera_config(updated_config)

    def _on_averaging_changed(self, value: str) -> None:
        """Handle averaging change."""
        pass  # TODO: Implement averaging

    def _on_history_changed(self, frames: int) -> None:
        """Handle history length change."""
        pass  # TODO: Implement history buffer

    def _on_observer_error(self, message: str) -> None:
        self._acq_panel.set_acquisition_running(False)
        self._toolbar.set_connection_state(CameraConnectionState.ERROR)
        self._acq_panel.set_connection_state(CameraConnectionState.ERROR)
        self._status_conn.setText(f"Connection: Error - {message}")

    # Display control handlers
    def _on_palette_changed(self, palette: str) -> None:
        self._image_widget.set_palette(palette)

    def _on_auto_range_toggled(self, checked: bool) -> None:
        self._image_widget.set_auto_range(checked)

    def _on_apply_range(self, min_temp: float, max_temp: float) -> None:
        self._image_widget.set_temperature_range(min_temp, max_temp)

    def _on_zoom_changed(self, zoom_text: str) -> None:
        self._image_widget.set_zoom(zoom_text)

    # ROI/Alarm selection handlers
    def _on_roi_selected(self, roi_id: str) -> None:
        """Handle ROI selection - highlight on image."""
        self._image_widget.highlight_roi(roi_id)
        self._update_roi_overlays()

    def _update_roi_overlays(self) -> None:
        """Update ROI overlays on the thermal image from current analysis config."""
        if not self._selected_camera_id:
            self._image_widget.set_roi_overlays([])
            return

        analysis = self._config_service.get_analysis_config(self._selected_camera_id)
        if not analysis:
            self._image_widget.set_roi_overlays([])
            return

        overlays = []
        # Get alarm states from latest result
        alarm_active_rois = set()
        if self._latest_result and self._latest_result.alarm_result:
            for alarm in self._latest_result.alarm_result.active_alarms:
                alarm_active_rois.add(alarm.roi_id)

        # Get ROIs for default position
        rois = analysis.get_rois_for_position("default")
        for roi in rois:
            if not roi.enabled:
                continue
            geometry = roi.geometry
            overlay = ROIOverlay(
                roi_id=roi.roi_id,
                shape=geometry.shape.value.lower(),
                geometry=geometry.parameters,
                color="#FFFF00",
                selected=(roi.roi_id == self._roi_panel._selected_roi_id),
                alarm_active=(roi.roi_id in alarm_active_rois),
            )
            overlays.append(overlay)

        self._image_widget.set_roi_overlays(overlays)

    def _on_roi_created(self, roi_id: str, roi_config) -> None:
        self._mark_dirty()
        self._update_roi_overlays()

    def _on_roi_updated(self, roi_id: str, roi_config) -> None:
        self._mark_dirty()
        self._update_roi_overlays()

    def _on_roi_deleted(self, roi_id: str) -> None:
        self._mark_dirty()
        self._update_roi_overlays()

    def _on_alarm_selected(self, rule_id: str) -> None:
        """Handle alarm selection."""
        pass

    def _mark_dirty(self) -> None:
        if self._selected_camera_id:
            self._dirty_camera_configs.add(self._selected_camera_id)

    # Callback handlers
    def _on_camera_config_changed(self, camera_id: str, config: CameraConfig) -> None:
        self._refresh_camera_list()
        if camera_id == self._selected_camera_id:
            self._load_camera_config(camera_id)

    def _on_analysis_config_changed(self, camera_id: str, config: AnalysisConfig) -> None:
        if camera_id == self._selected_camera_id:
            self._roi_panel.refresh_roi_list()
            self._alarm_panel.refresh_alarm_list()
            self._update_roi_overlays()

    def _update_stats(self) -> None:
        """Update status bar stats."""
        if self._runtime_service and self._selected_camera_id:
            cam_stats = self._runtime_service.camera_stats(self._selected_camera_id)
            if cam_stats:
                fps = cam_stats.current_fps or cam_stats.average_fps
                if fps:
                    self._status_fps.setText(f"FPS: {fps:.1f}")
                self._status_frames.setText(f"Frames: {cam_stats.frames_received}")

        if self._observer:
            obs_stats = self._observer.stats()
            if obs_stats:
                self._status_proc.setText(f"Processing: {obs_stats.average_processing_time_ms:.1f} ms")

    # Snapshot / Save handlers
    def _on_snapshot(self) -> None:
        """Handle snapshot button."""
        # TODO: Implement snapshot save
        self._status_label.setText("Snapshot not yet implemented")

    def _on_save_config(self) -> None:
        """Handle save config button."""
        if self._config_manager:
            # Switch to Configuration Editor tab
            if self._config_editor:
                self._analysis_tabs.setCurrentWidget(self._config_editor)
        else:
            self._status_label.setText("Configuration Editor not available")

    # Config editor handlers
    def _on_config_saved(self) -> None:
        set_status(self._status_label, "ok")
        self._status_label.setText("Configuration saved - restart required")

    def _on_config_error(self, error: str) -> None:
        set_status(self._status_label, "error")
        self._status_label.setText(f"Config error: {error}")

    def _on_restart_required(self, message: str) -> None:
        set_status(self._status_label, "warning")
        self._status_label.setText(message)

    def on_mode_activated(self) -> None:
        """Called when configuration mode becomes active."""
        self._refresh_camera_list()
        if self._selected_camera_id:
            self._load_camera_config(self._selected_camera_id)
        else:
            cameras = self._config_service.get_all_camera_configs()
            if cameras:
                self._toolbar.select_camera_by_id(cameras[0].identity.camera_id)

    def on_mode_deactivated(self) -> None:
        """Called when configuration mode is deactivated."""
        self._restore_connect_cursor()
        if self._camera_selection_dialog is not None:
            self._camera_selection_dialog.close()
        if self._observer:
            self._observer.stop()
            self._observer = None
        self._stats_timer.stop()

    def closeEvent(self, event) -> None:
        # Bounded waits: worker threads quit themselves on operation
        # completion, so these return immediately in the normal case and
        # only delay teardown while a control operation is mid-flight.
        self._stop_focus_worker(timeout_ms=10000)
        self._stop_nuc_worker(timeout_ms=15000)
        self.on_mode_deactivated()
        self._config_service.remove_camera_change_callback(self._on_camera_config_changed)
        self._config_service.remove_analysis_change_callback(self._on_analysis_config_changed)
        super().closeEvent(event)


class ConfigurationWindow(QMainWindow):
    """Top-level window for Configuration mode."""

    def __init__(
        self,
        config_service: ConfigurationService,
        mode_service: ModeService,
        runtime_service: CameraRuntimeService | None = None,
        discovery_service: "CameraDiscoveryService | GvcpDiscoveryService | None" = None,
        theme_manager: Optional[ThemeManager] = None,
        config_manager: Optional[ConfigurationManager] = None,
    ) -> None:
        super().__init__()

        self._config_service = config_service
        self._mode_service = mode_service
        self._runtime_service = runtime_service
        self._discovery_service = discovery_service
        self._theme = theme_manager
        self._config_manager = config_manager
        self._settings_menu_controller = None

        self.setWindowTitle("Thermal Monitoring System V3 - Configuration Mode")
        self._apply_window_config()

        # Central widget
        self._config_widget = ConfigurationModeWidget(
            config_service=config_service,
            mode_service=mode_service,
            runtime_service=runtime_service,
            discovery_service=discovery_service,
            theme_manager=theme_manager,
            config_manager=config_manager,
        )
        self.setCentralWidget(self._config_widget)

        self._setup_menu_bar()

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_label = QLabel("Configuration Mode")
        set_role(self._status_label, "status")
        self._status_bar.addWidget(self._status_label)

    def _apply_window_config(self) -> None:
        """Apply window configuration from theme manager."""
        if self._theme:
            config = self._theme.window_config()
            self.setMinimumSize(config["config_min_width"], config["config_min_height"])
            if config["start_maximized"]:
                self.showMaximized()
        else:
            self.setMinimumSize(1400, 900)
            self.showMaximized()

    def _setup_menu_bar(self) -> None:
        """Set up ThermoView-style menu bar."""
        menu_bar = self.menuBar()

        # File menu
        file_menu = menu_bar.addMenu("File")
        save_action = QAction("Save Configuration", self)
        save_action.triggered.connect(self._config_widget._on_save_config)
        file_menu.addAction(save_action)
        file_menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Camera menu
        camera_menu = menu_bar.addMenu("Camera")
        connect_action = QAction("Connect...", self)
        connect_action.triggered.connect(self._config_widget._on_connect)
        camera_menu.addAction(connect_action)

        disconnect_action = QAction("Disconnect", self)
        disconnect_action.triggered.connect(self._config_widget._on_disconnect)
        camera_menu.addAction(disconnect_action)

        camera_menu.addSeparator()

        refresh_action = QAction("Refresh Cameras", self)
        refresh_action.triggered.connect(self._config_widget._refresh_camera_list)
        camera_menu.addAction(refresh_action)

        # View menu
        view_menu = menu_bar.addMenu("View")
        fit_action = QAction("Fit to Window", self)
        fit_action.triggered.connect(lambda: self._config_widget._image_widget.set_zoom("Fit to Window"))
        view_menu.addAction(fit_action)

        view_menu.addSeparator()
        for zoom in ["50%", "100%", "200%", "400%"]:
            action = QAction(zoom, self)
            action.triggered.connect(lambda checked, z=zoom: self._config_widget._image_widget.set_zoom(z))
            view_menu.addAction(action)

        view_menu.addSeparator()
        fullscreen_action = QAction("Fullscreen", self)
        fullscreen_action.setShortcut("F11")
        fullscreen_action.triggered.connect(lambda: self.setWindowState(self.windowState() ^ Qt.WindowState.WindowFullScreen))
        view_menu.addAction(fullscreen_action)

        # Temperature menu
        temp_menu = menu_bar.addMenu("Temperature")
        palette_menu = temp_menu.addMenu("Palette")
        for palette in ["temperature", "iron", "rainbow", "gray", "hot"]:
            action = QAction(palette.capitalize(), self)
            action.triggered.connect(lambda checked, p=palette: self._config_widget._image_widget.set_palette(p))
            palette_menu.addAction(action)

        temp_menu.addSeparator()
        auto_range_action = QAction("Auto Range", self)
        auto_range_action.setCheckable(True)
        auto_range_action.setChecked(True)
        auto_range_action.triggered.connect(lambda checked: self._config_widget._image_widget.set_auto_range(checked))
        temp_menu.addAction(auto_range_action)

        # Analysis menu
        analysis_menu = menu_bar.addMenu("Analysis")
        add_roi_action = QAction("Add ROI...", self)
        add_roi_action.triggered.connect(self._config_widget._roi_panel._on_add_roi)
        analysis_menu.addAction(add_roi_action)

        # Window menu
        window_menu = menu_bar.addMenu("Window")
        config_action = QAction("Configuration Editor", self)
        config_action.triggered.connect(lambda: self._config_widget._analysis_tabs.setCurrentWidget(self._config_widget._config_editor) if self._config_widget._config_editor else None)
        window_menu.addAction(config_action)

        # Settings menu with live Theme switching (same manager everywhere).
        from thermal_monitor.ui.theme.menu import ThemeMenuController

        self._settings_menu_controller = ThemeMenuController(
            theme_manager=self._theme,
            config_manager=self._config_manager,
            parent=self,
        )
        self._settings_menu_controller.attach_to_menu_bar(menu_bar)

        # Help menu
        help_menu = menu_bar.addMenu("Help")
        about_action = QAction("About", self)
        help_menu.addAction(about_action)

    def on_mode_activated(self) -> None:
        """Called when Configuration mode becomes active."""
        self._config_widget.on_mode_activated()

    def on_mode_deactivated(self) -> None:
        """Called when Configuration mode is deactivated."""
        self._config_widget.on_mode_deactivated()

    def closeEvent(self, event) -> None:
        self.on_mode_deactivated()
        super().closeEvent(event)


__all__ = ["ConfigurationModeWidget", "ConfigurationWindow"]
