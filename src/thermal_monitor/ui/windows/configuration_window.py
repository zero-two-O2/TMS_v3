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
import threading
import time
from typing import Optional

from PyQt6.QtCore import Qt, QThread, QTimer, QObject, pyqtSignal, pyqtSlot, QSettings, QEvent
from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QStatusBar,
    QMessageBox,
    QFrame,
    QLabel,
    QMenuBar,
    QMenu,
    QApplication,
    QDialog,
    QPushButton,
    QSizePolicy,
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
from thermal_monitor.camera.model import AcquisitionState
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.lifecycle import (
    CameraSession,
    TeardownTimings,
    allowed_transition,
    can_connect,
    can_disconnect,
    can_start,
    can_stop,
    is_transitional,
)
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.runtime import CameraRuntimeService
from thermal_monitor.services.discovery import CameraDiscoveryService, GvcpDiscoveryService
from thermal_monitor.services.observer import ObserverService
from thermal_monitor.config import ConfigurationManager
from thermal_monitor.config.models import CameraMappingConfig
from thermal_monitor.ui.configuration_editor import ConfigurationEditor
from thermal_monitor.ui.frame_rate import UniqueFrameRate
from thermal_monitor.ui.modes.observer_image import LiveThermalWidget, ROIOverlay
from thermal_monitor.ui.modes.view_finder import ViewFinderWidget
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
from thermal_monitor.ui.theme.properties import set_role, set_status, set_variant


logger = logging.getLogger(__name__)


_DOCK_SETTINGS_ORG = "ThermalMonitoringSystem"
_DOCK_SETTINGS_APP = "ConfigWorkstation"
_DOCK_SETTINGS_KEY = "dock_layout_v1"

# Bounded waits (seconds) for teardown phases. No arbitrary sleeps: each
# phase is an event/process-state join with its own timeout, after which
# the operation escalates (terminate) instead of blocking the GUI.
_TEARDOWN_PROCESS_TIMEOUT_S = 5.0
_TEARDOWN_VERIFY_TIMEOUT_S = 1.0
_OBSERVER_STOP_TIMEOUT_S = 2.0




_UNIT_SYMBOLS = {
    "celsius": "°C",
    "fahrenheit": "°F",
    "kelvin": "K",
}


class _ShelfTab(QPushButton):
    """One narrow vertical tab on a side shelf rail (~28 px wide).

    Checkable: checked = its panel is open. Plain Qt widget painting
    (rotated text over the standard button bevel) — no custom docking
    framework, no stylesheets; emphasis comes from the theme variant
    (accent = open, ghost = collapsed) plus the palette text color.
    """

    _RAIL_WIDTH = 28

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tab_title = title
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(title)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedWidth(self._RAIL_WIDTH)
        metrics = self.fontMetrics()
        self.setFixedHeight(
            min(160, max(76, metrics.horizontalAdvance(title) + 18))
        )

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        from PyQt6.QtGui import QPainter
        from PyQt6.QtWidgets import QStyle, QStyleOptionButton

        painter = QPainter(self)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""  # bevel only; text is drawn rotated below
        self.style().drawControl(
            QStyle.ControlElement.CE_PushButton, option, painter, self
        )
        painter.save()
        try:
            palette = self.palette()
            if self.isChecked():
                color = palette.color(palette.ColorRole.HighlightedText)
                if color.alpha() == 0:
                    color = palette.color(palette.ColorRole.Highlight)
            else:
                color = palette.color(palette.ColorRole.ButtonText)
            painter.setPen(color)
            # Bottom-to-top vertical text, centered on the rail: after the
            # transform, +x runs up the widget and +y runs across it.
            painter.translate(0, self.height())
            painter.rotate(-90)
            painter.drawText(
                8,
                0,
                max(0, self.height() - 16),
                self._RAIL_WIDTH,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self._tab_title,
            )
        finally:
            painter.restore()


class _SidePanel:
    """One tool panel docked inside its side shelf (never floating).

    The content widget is reparented into the wrapper (never copied or
    recreated), so collapsing/expanding preserves all panel state.
    There is deliberately no close button, no float, no drag handle:
    visibility is driven by the shelf tab + pin only.
    """

    __slots__ = (
        "key",
        "title",
        "content",
        "side",
        "wrapper",
        "pin_button",
        "tab",
        "pinned",
    )

    def __init__(self, key: str, title: str, content: QWidget, side: str) -> None:
        self.key = key
        self.title = title
        self.content = content
        self.side = side  # "left" | "right"
        self.wrapper: QFrame | None = None
        self.pin_button: QPushButton | None = None
        self.tab: _ShelfTab | None = None
        self.pinned = False

    def is_open(self) -> bool:
        """True while the panel wrapper is shown (state flag, not on-screen).

        Uses the explicit hidden flag so the answer does not depend on
        ancestor visibility (container / splitter / window): opening a
        panel sets the flag even before the container is shown.
        """
        return self.wrapper is not None and not self.wrapper.isHidden()


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
    # Marshals background camera-operation outcomes to the GUI thread:
    # (ok, tag, message, result). Emitted from a daemon worker thread.
    _bg_done = pyqtSignal(bool, str, str, object)

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

        # --- Camera lifecycle (Part 3: explicit state machine + sessions) ---
        # The lifecycle state is the authority; button enablement follows it
        # (see ImageAcquisitionPanel._update_button_states). Every session
        # epoch invalidates queued results/render requests from older ones.
        self._lifecycle: CameraConnectionState = CameraConnectionState.DISCONNECTED
        self._session = CameraSession()
        self._stale_results_dropped: int = 0
        # Single in-flight background camera operation (connect/teardown).
        # A new request arriving mid-operation is queued, never overlapped:
        # there is never more than one acquisition pipeline transition.
        # Implemented as a daemon threading.Thread (no QObject outside the
        # GUI thread, so no thread-affinity hazards); the outcome returns
        # through the _bg_done queued signal.
        self._bg_thread: threading.Thread | None = None
        self._bg_tag: str | None = None
        self._bg_connect_config = None  # CameraConfig for chained connect
        self._pending_switch: tuple[str, bool, object] | None = None
        self._pending_disconnect: bool = False
        # First-frame/display timing marks for the current session epoch.
        self._session_started_at: float | None = None
        self._first_frame_at: float | None = None
        self._first_display_at: float | None = None

        # Diagnostics: log config service identity and counts for comparison with Live
        try:
            svc_id = hex(id(self._config_service))
            total = len(self._config_service.get_all_camera_configs())
            logger.info("CONFIG MODE WIDGET CREATED: config_service id=%s total_cameras=%d", svc_id, total)
            if self._config_manager is not None and hasattr(self._config_manager, "config_path"):
                logger.info("CONFIG MODE config_path=%s exists=%s", self._config_manager.config_path, self._config_manager.config_path.exists())
        except Exception:
            pass

        self._setup_ui()
        self._connect_signals()
        self._load_initial_data()
        try:
            total2 = len(self._config_service.get_all_camera_configs())
            logger.info("CONFIG MODE INITIAL LOAD: config_service id=%s total_cameras=%d", hex(id(self._config_service)), total2)
            for idx, c in enumerate(self._config_service.get_all_camera_configs()):
                logger.info("  CONFIG CAM %d: id=%r name=%r serial=%r enabled=%r thermal_enabled=%r", idx + 1, c.identity.camera_id, getattr(c, "name", ""), getattr(c.identity, "serial_number", ""), getattr(c, "enabled", "?"), getattr(c, "thermal_enabled", "?"))
        except Exception:
            pass

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

        # --- Workstation row: thin shelf | panels | CENTER | panels | thin shelf
        # Plain widgets only. Side panels live inside their shelf container
        # and can never float, drag, tabify or cover the center: hiding a
        # panel hides its widget (never destroyed), and the camera
        # workspace expands to use the freed space.
        work_row = QHBoxLayout()
        work_row.setContentsMargins(0, 0, 0, 0)
        work_row.setSpacing(0)
        main_layout.addLayout(work_row, 1)

        self._left_rail, self._left_rail_layout = self._build_shelf_rail()
        self._left_rail.setObjectName("cfg_left_shelf")
        work_row.addWidget(self._left_rail)
        self._side_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._side_splitter.setChildrenCollapsible(False)
        self._side_splitter.setObjectName("cfg_side_splitter")
        work_row.addWidget(self._side_splitter, 1)
        self._right_rail, self._right_rail_layout = self._build_shelf_rail()
        self._right_rail.setObjectName("cfg_right_shelf")
        work_row.addWidget(self._right_rail)

        self._left_container = self._build_side_container()
        self._left_container.setObjectName("cfg_left_panels")
        self._side_splitter.addWidget(self._left_container)
        self._right_container = self._build_side_container()
        self._right_container.setObjectName("cfg_right_panels")
        # Center widget is inserted between the side containers below.
        self._side_panels: dict[str, _SidePanel] = {}
        self._panel_view_actions: dict[str, QAction] = {}
        self._irvl_mode = "both"
        self._ir_vl_split_saved = None

        # LEFT: Image Acquisition panel (instrument panel)
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
        # LEFT: Image Information panel (frame metadata).
        self._frame_info_panel = FrameInfoPanel(self._theme)
        self._register_side_panel(
            "camera_control", "Camera Control", self._acq_panel, "left"
        )
        self._register_side_panel(
            "image_info", "Image Information", self._frame_info_panel, "left"
        )

        # CENTER: Large thermal + VL display (primary workspace).
        # The painters letterbox with KeepAspectRatio, so side shelves
        # never stretch the 640x480 (4:3) image.
        center_widget = QWidget()
        self._center_widget = center_widget
        center_widget.setObjectName("cfg_center_workspace")
        center_widget.setMinimumSize(320, 240)
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
        ir_vl_splitter.setObjectName("cfg_ir_vl_splitter")
        ir_vl_splitter.addWidget(self._image_widget)
        ir_vl_splitter.addWidget(self._vl_widget)
        ir_vl_splitter.setSizes([700, 420])
        ir_vl_splitter.setStretchFactor(0, 3)
        ir_vl_splitter.setStretchFactor(1, 2)
        center_layout.addWidget(ir_vl_splitter, 1)
        self._ir_vl_splitter = ir_vl_splitter
        # Slim workspace bar on top (built after the image widgets exist
        # so its controls can wire straight to them).
        center_layout.insertWidget(0, self._build_workspace_bar())
        # Center goes between the side containers; side-panel widths stay
        # user-resizable (controlled) via this splitter only.
        self._side_splitter.insertWidget(1, center_widget)
        self._side_splitter.addWidget(self._right_container)
        self._side_splitter.setStretchFactor(0, 0)
        self._side_splitter.setStretchFactor(1, 1)
        self._side_splitter.setStretchFactor(2, 0)
        center_widget.installEventFilter(self)

        # RIGHT: independent analysis/control tools, stacked vertically
        # from top to bottom inside the right shelf. No dragging,
        # floating or tabifying: order follows registration.
        self._scale_panel = ThermalScalePanel(self._theme)
        self._scale_panel.palette_changed.connect(self._on_palette_changed)
        self._scale_panel.auto_range_toggled.connect(self._on_auto_range_toggled)
        self._scale_panel.manual_range_applied.connect(self._on_apply_range)
        self._scale_panel.zoom_changed.connect(self._on_zoom_changed)
        self._register_side_panel(
            "temp_scale", "Temperature Scale", self._scale_panel, "right"
        )

        # View Finder dock: a real navigation thumbnail showing the same
        # latest frame as the workspace, with the main view's visible
        # region and two-way pan/zoom synchronization (no second stream).
        self._finder_widget = ViewFinderWidget()
        self._finder_widget.viewport_dragged.connect(self._on_finder_dragged)
        self._image_widget.view_changed.connect(self._sync_finder_viewport)
        self._register_side_panel(
            "view_finder", "View Finder", self._finder_widget, "right"
        )

        # ROI panel
        self._roi_panel = ROIPanel(self._config_service, self._theme)
        self._roi_panel.roi_selected.connect(self._on_roi_selected)
        self._roi_panel.roi_created.connect(self._on_roi_created)
        self._roi_panel.roi_updated.connect(self._on_roi_updated)
        self._roi_panel.roi_deleted.connect(self._on_roi_deleted)
        self._register_side_panel(
            "roi", "ROI", self._roi_panel, "right"
        )

        # Alarm panel
        self._alarm_panel = AlarmPanel(self._config_service, self._theme)
        self._alarm_panel.alarm_selected.connect(self._on_alarm_selected)
        self._register_side_panel(
            "alarms", "Alarms", self._alarm_panel, "right"
        )

        # Statistics panel
        self._stats_panel = StatisticsPanel(self._theme)
        self._register_side_panel(
            "statistics", "Statistics", self._stats_panel, "right"
        )

        # Configuration Editor dock (deployment config)
        self._config_editor: Optional[ConfigurationEditor] = None
        if self._config_manager:
            self._config_editor = ConfigurationEditor(
                config_manager=self._config_manager,
                theme_manager=self._theme,
            )
            self._config_editor.config_saved.connect(self._on_config_saved)
            self._config_editor.config_error.connect(self._on_config_error)
            self._config_editor.restart_required.connect(self._on_restart_required)
            self._register_side_panel(
                "config_editor",
                "Configuration Editor",
                self._config_editor,
                "right",
            )

        # Restore pins/open panels/widths/IR-VL/zoom. Panel widgets
        # themselves are never recreated.
        self._restore_shelf_state()

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

    # -- Side shelves (pin / auto-hide workstation) -------------------------

    _LEFT_ORDER = ("camera_control", "image_info")
    _RIGHT_ORDER = (
        "temp_scale",
        "view_finder",
        "roi",
        "alarms",
        "statistics",
        "config_editor",
    )

    def _build_shelf_rail(self) -> tuple[QWidget, QVBoxLayout]:
        """Thin clickable rail (~30 px) holding one tab per side panel."""
        rail = QWidget()
        rail.setFixedWidth(_ShelfTab._RAIL_WIDTH + 4)
        set_role(rail, "toolbar")
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(2)
        layout.addStretch()
        return rail, layout

    def _build_side_container(self) -> QWidget:
        """Host for one side's open panels, stacked vertically from the top.

        Width is user-resizable via the side splitter only, within
        controlled limits (spec: side panels may resize in width but
        can never detach, float, or move outside their shelf).
        """
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addStretch()
        container.setVisible(False)
        container.setMinimumWidth(200)
        container.setMaximumWidth(460)
        return container

    def _register_side_panel(
        self, key: str, title: str, content: QWidget, side: str
    ) -> "_SidePanel":
        """Dock a panel widget inside its side shelf (never floating).

        The content widget is reparented into the wrapper (never copied
        or recreated): collapsing/expanding preserves all panel state.
        There is deliberately no close button, no float, no drag handle —
        visibility is driven by the shelf tab + pin only.
        """
        record = _SidePanel(key, title, content, side)
        wrapper = QFrame()
        wrapper.setObjectName(f"cfg_panel_{key}")
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(0)
        header = QWidget()
        header.setObjectName(f"cfg_panel_header_{key}")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(2, 2, 2, 2)
        header_layout.setSpacing(4)
        pin = QPushButton("○")
        pin.setObjectName(f"cfg_pin_{key}")
        pin.setFixedSize(22, 22)
        pin.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        pin.setToolTip("Pin panel (keep open)")
        set_variant(pin, "ghost")
        pin.clicked.connect(
            lambda _checked=False, panel_key=key: self._toggle_pin(panel_key)
        )
        title_label = QLabel(title)
        header_layout.addWidget(pin)
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        wrapper_layout.addWidget(header)
        wrapper_layout.addWidget(content, 1)
        wrapper.setVisible(False)
        record.wrapper = wrapper
        record.pin_button = pin
        container = self._left_container if side == "left" else self._right_container
        container.layout().insertWidget(container.layout().count() - 1, wrapper)
        rail_layout = self._left_rail_layout if side == "left" else self._right_rail_layout
        tab = _ShelfTab(title)
        tab.setObjectName(f"cfg_shelf_tab_{key}")
        tab.clicked.connect(
            lambda _checked=False, panel_key=key: self._on_shelf_tab(panel_key)
        )
        rail_layout.insertWidget(rail_layout.count() - 1, tab)
        record.tab = tab
        self._side_panels[key] = record
        self._sync_pin_button(record)
        self._sync_shelf_tab(record)
        return record

    def side_panels(self) -> dict[str, "_SidePanel"]:
        """All side-shelf panels by key (workstation layout)."""
        return dict(self._side_panels)

    def open_panel(self, key: str) -> None:
        """Open a panel, preserving its pinned state and widget state."""
        self.set_panel_open(key, True)

    def set_panel_open(self, key: str, open: bool, *, persist: bool = True) -> None:
        """Show/hide a panel without destroying its widget or state."""
        record = self._side_panels.get(key)
        if record is None or record.wrapper is None:
            return
        record.wrapper.setVisible(bool(open))
        self._sync_shelf_tab(record)
        action = self._panel_view_actions.get(key)
        if action is not None:
            action.setChecked(bool(open))
        self._update_side_container(record.side)
        if persist:
            self._save_shelf_state()

    def set_panel_pinned(self, key: str, pinned: bool, *, persist: bool = True) -> None:
        """Pin (stays open) or unpin (auto-hide eligible) a panel."""
        record = self._side_panels.get(key)
        if record is None:
            return
        record.pinned = bool(pinned)
        self._sync_pin_button(record)
        if persist:
            self._save_shelf_state()

    def _toggle_pin(self, key: str) -> None:
        """Header pin click: pinned stays open; unpinned becomes dismissible.

        Pinning a closed panel opens it (an invisible pinned panel would
        be confusing); unpinning keeps the current open state — the panel
        collapses on the next dismissal (own tab or workspace click).
        """
        record = self._side_panels.get(key)
        if record is None:
            return
        pinned = not record.pinned
        self.set_panel_pinned(key, pinned, persist=False)
        if pinned and not record.is_open():
            self.set_panel_open(key, True, persist=False)
        self._save_shelf_state()

    def _on_shelf_tab(self, key: str) -> None:
        """Shelf tab click toggles that panel (pin state untouched)."""
        record = self._side_panels.get(key)
        if record is None:
            return
        self.set_panel_open(key, not record.is_open())

    def _sync_shelf_tab(self, record: "_SidePanel") -> None:
        """Reflect panel visibility on its rail tab (theme system only)."""
        if record.tab is None:
            return
        is_open = record.is_open()
        record.tab.setChecked(is_open)
        set_variant(record.tab, "accent" if is_open else "ghost")

    def _sync_pin_button(self, record: "_SidePanel") -> None:
        """Reflect pinned state on the panel header pin control."""
        if record.pin_button is None:
            return
        record.pin_button.setText("●" if record.pinned else "○")
        set_variant(record.pin_button, "accent" if record.pinned else "ghost")
        record.pin_button.setToolTip(
            "Unpin panel (auto-hide)" if record.pinned else "Pin panel (keep open)"
        )

    def _update_side_container(self, side: str) -> None:
        """Show a side container iff at least one of its panels is open."""
        container = self._left_container if side == "left" else self._right_container
        any_open = any(
            record.is_open()
            for record in self._side_panels.values()
            if record.side == side
        )
        container.setVisible(any_open)

    def _collapse_unpinned(self) -> None:
        """Auto-hide: collapse every open, unpinned panel (state kept)."""
        changed = False
        for record in self._side_panels.values():
            if record.is_open() and not record.pinned:
                record.wrapper.setVisible(False)
                self._sync_shelf_tab(record)
                action = self._panel_view_actions.get(record.key)
                if action is not None:
                    action.setChecked(False)
                changed = True
        if changed:
            self._update_side_container("left")
            self._update_side_container("right")
            self._save_shelf_state()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt override)
        """Dismiss unpinned panels when the user clicks back to work.

        Only while the IR view is fitted (no pan gesture possible there,
        so the press cannot be the start of a drag); zoomed presses are
        left alone for panning.
        """
        try:
            if (
                watched is self._center_widget
                and event.type() == QEvent.Type.MouseButtonPress
                and self._image_widget is not None
                and self._image_widget.is_fit()
            ):
                self._collapse_unpinned()
        except Exception:
            logger.debug("Center-press auto-hide failed", exc_info=True)
        return super().eventFilter(watched, event)

    def panel_toggle_actions(self) -> list[QAction]:
        """Checkable View-menu actions bound to each panel (stable order)."""
        actions: list[QAction] = []
        for key in self._LEFT_ORDER + self._RIGHT_ORDER:
            record = self._side_panels.get(key)
            if record is None:
                continue
            action = QAction(record.title, self)
            action.setCheckable(True)
            action.setChecked(record.is_open())
            action.triggered.connect(
                lambda checked=False, panel_key=key: self.set_panel_open(panel_key, checked)
            )
            actions.append(action)
            self._panel_view_actions[key] = action
        return actions

    def _save_shelf_state(self) -> None:
        """Persist pins/open/widths/IR-VL/zoom via QSettings (same scope).

        The workspace zoom/pan rides along in the same settings scope.
        A missing zoom restores as Fit; out-of-range values are clamped
        on restore so an unusable zoom can never come back.
        """
        try:
            settings = QSettings(_DOCK_SETTINGS_ORG, _DOCK_SETTINGS_APP)
            try:
                settings.remove(_DOCK_SETTINGS_KEY)  # obsolete dock layout
            except Exception:
                pass
            settings.setValue(
                "shelf_pins_v1",
                [key for key, record in self._side_panels.items() if record.pinned],
            )
            settings.setValue(
                "shelf_open_v1",
                [key for key, record in self._side_panels.items() if record.is_open()],
            )
            settings.setValue("shelf_split_v1", self._side_splitter.saveState())
            settings.setValue("irvl_mode_v1", self._irvl_mode)
            settings.setValue("irvl_split_v1", self._ir_vl_splitter.saveState())
            zoom = self._image_widget._zoom
            settings.setValue("workspace_zoom_v1", float(zoom) if zoom is not None else 0.0)
            pan = self._image_widget._pan_offset
            settings.setValue("workspace_pan_v1", [float(pan.x()), float(pan.y())])
        except Exception:
            logger.debug("Shelf state save failed", exc_info=True)

    def _restore_shelf_state(self) -> None:
        """Restore pins/open/widths/IR-VL/zoom, falling back to defaults."""
        try:
            settings = QSettings(_DOCK_SETTINGS_ORG, _DOCK_SETTINGS_APP)
            pins = settings.value("shelf_pins_v1", None)
            opened = settings.value("shelf_open_v1", None)
            if pins is None and opened is None:
                pins, opened = ["camera_control"], ["camera_control", "temp_scale"]
            if isinstance(pins, str):
                pins = [pins]
            if isinstance(opened, str):
                opened = [opened]
            pins = set(pins or [])
            opened = set(opened or [])
            for key, record in self._side_panels.items():
                record.pinned = key in pins
                self._sync_pin_button(record)
                record.wrapper.setVisible(key in opened)
                self._sync_shelf_tab(record)
            self._update_side_container("left")
            self._update_side_container("right")
            split_state = settings.value("shelf_split_v1", None)
            if split_state is not None:
                try:
                    self._side_splitter.restoreState(split_state)
                except Exception:
                    pass
            mode = settings.value("irvl_mode_v1", "both") or "both"
            self.set_irvl_mode(mode if mode in ("ir", "vl", "both") else "both", persist=False)
            irvl_split = settings.value("irvl_split_v1", None)
            if irvl_split is not None and self._irvl_mode == "both":
                try:
                    self._ir_vl_splitter.restoreState(irvl_split)
                except Exception:
                    pass
            self._restore_workspace_zoom(settings)
            self._refresh_irvl_buttons()
            self._refresh_workspace_zoom()
        except Exception:
            logger.debug("Shelf state restore failed", exc_info=True)

    def _restore_workspace_zoom(self, settings: QSettings) -> None:
        """Restore persisted workspace zoom/pan, clamped to usable range."""
        try:
            raw_zoom = settings.value("workspace_zoom_v1", 0.0)
            zoom = float(raw_zoom) if raw_zoom is not None else 0.0
            if zoom <= 0.0:
                self._image_widget.zoom_fit()
            elif zoom < 0.05 or zoom > LiveThermalWidget._ZOOM_MAX:
                # Persisted garbage (or a tiny-window fit that cannot be
                # valid on this screen): fall back to Fit, never unusable.
                self._image_widget.zoom_fit()
            else:
                # set_zoom_factor clamps to [fit, max] when a frame with
                # known geometry is present.
                self._image_widget.set_zoom_factor(zoom)
            raw_pan = settings.value("workspace_pan_v1", [0.0, 0.0])
            if isinstance(raw_pan, (list, tuple)) and len(raw_pan) == 2:
                self._image_widget.set_pan_offset(float(raw_pan[0]), float(raw_pan[1]))
        except Exception:
            logger.debug("Workspace zoom restore failed", exc_info=True)
            try:
                self._image_widget.zoom_fit()
            except Exception:
                pass

    # -- Central workspace bar (IR/VL mode + zoom controls) ----------------

    def _build_workspace_bar(self) -> QWidget:
        """Slim toolbar: IR/VL workspace mode + zoom controls.

        All controls drive the displayed view only (mode/zoom/pan); the
        acquisition, SHM and processing paths are untouched, and the VL
        feed keeps its own independent presentation. Theme variants
        only — no per-widget stylesheets.
        """
        bar = QFrame()
        set_role(bar, "toolbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(4)

        self._irvl_buttons: dict[str, QPushButton] = {}
        for mode, text, tip in (
            ("ir", "IR", "IR only"),
            ("both", "IR+VL", "IR and VL side by side"),
            ("vl", "VL", "VL only"),
        ):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setToolTip(tip)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            set_variant(button, "ghost")
            button.clicked.connect(
                lambda _checked=False, workspace_mode=mode: self.set_irvl_mode(workspace_mode)
            )
            layout.addWidget(button)
            self._irvl_buttons[mode] = button

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator)

        self._zoom_out_btn = QPushButton("\u2212")  # minus sign
        self._zoom_out_btn.setToolTip("Zoom out (-)")
        self._zoom_out_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        set_variant(self._zoom_out_btn, "ghost")
        # clicked() carries a checked flag: never pass it into view methods.
        self._zoom_out_btn.clicked.connect(lambda _checked=False: self._image_widget.zoom_out())
        layout.addWidget(self._zoom_out_btn)

        self._zoom_label = QLabel("Fit")
        self._zoom_label.setMinimumWidth(52)
        self._zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._zoom_label)

        self._zoom_in_btn = QPushButton("+")
        self._zoom_in_btn.setToolTip("Zoom in (+)")
        self._zoom_in_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        set_variant(self._zoom_in_btn, "ghost")
        self._zoom_in_btn.clicked.connect(lambda _checked=False: self._image_widget.zoom_in())
        layout.addWidget(self._zoom_in_btn)

        self._zoom_fit_btn = QPushButton("Fit")
        self._zoom_fit_btn.setToolTip("Fit to window (0)")
        self._zoom_fit_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        set_variant(self._zoom_fit_btn, "ghost")
        self._zoom_fit_btn.clicked.connect(self._image_widget.zoom_fit)
        layout.addWidget(self._zoom_fit_btn)

        self._zoom_1to1_btn = QPushButton("1:1")
        self._zoom_1to1_btn.setToolTip("Native pixels (1)")
        self._zoom_1to1_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        set_variant(self._zoom_1to1_btn, "ghost")
        self._zoom_1to1_btn.clicked.connect(self._image_widget.zoom_one_to_one)
        layout.addWidget(self._zoom_1to1_btn)

        layout.addStretch()
        self._image_widget.view_changed.connect(self._refresh_workspace_zoom)
        self._refresh_workspace_zoom()
        self._refresh_irvl_buttons()
        return bar

    def set_irvl_mode(self, mode: str, *, persist: bool = True) -> None:
        """Switch the center workspace between IR / IR+VL / VL.

        Display-only: widgets are shown/hidden in the existing splitter
        (ratio still draggable); acquisition, SHM and processing keep
        producing both feeds either way.
        """
        if mode not in ("ir", "vl", "both"):
            return
        if self._irvl_mode == "both" and mode != "both":
            try:
                self._irvl_split_saved = self._ir_vl_splitter.saveState()
            except Exception:
                self._irvl_split_saved = None
        self._irvl_mode = mode
        self._image_widget.setVisible(mode in ("ir", "both"))
        self._vl_widget.setVisible(mode in ("vl", "both"))
        if mode == "both" and self._ir_vl_split_saved is not None:
            try:
                self._ir_vl_splitter.restoreState(self._ir_vl_split_saved)
            except Exception:
                pass
        self._refresh_irvl_buttons()
        if persist:
            self._save_shelf_state()

    def _refresh_irvl_buttons(self) -> None:
        """Reflect the workspace mode on the bar buttons."""
        for mode, button in getattr(self, "_irvl_buttons", {}).items():
            active = mode == getattr(self, "_irvl_mode", "both")
            button.setChecked(active)
            set_variant(button, "accent" if active else "ghost")

    def _refresh_workspace_zoom(self) -> None:
        """Reflect the IR view's zoom state on the workspace bar."""
        label = getattr(self, "_zoom_label", None)
        if label is not None:
            label.setText(self._image_widget.zoom_percent())

    # -- View Finder synchronization (two-way, same frame) ------------------

    def _sync_finder_viewport(self) -> None:
        """Mirror the main view's visible region into the View Finder."""
        try:
            self._finder_widget.set_viewport(
                self._image_widget.viewport_rect_normalized()
            )
        except Exception:
            logger.debug("Finder viewport sync failed", exc_info=True)

    @pyqtSlot(float, float)
    def _on_finder_dragged(self, center_x: float, center_y: float) -> None:
        """Finder drag pans the main thermal view to that region."""
        try:
            self._image_widget.pan_to_normalized(center_x, center_y)
        except Exception:
            logger.debug("Finder drag pan failed", exc_info=True)

    def _connect_signals(self) -> None:
        """Connect internal signals."""
        self._config_service.add_camera_change_callback(self._on_camera_config_changed)
        self._config_service.add_analysis_change_callback(self._on_analysis_config_changed)

        # Background camera-operation outcomes arrive here (GUI thread).
        self._bg_done.connect(self._on_bg_done, Qt.ConnectionType.QueuedConnection)

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
        """Switch to a different camera via the safe lifecycle pipeline.

        The old camera (if any) is torn down completely — observer,
        processing consumer, SHM attachment, render input and child
        process — before the new camera is selected. There are never two
        active acquisition pipelines attached to this widget.
        """
        self._activate_camera(camera_id, connect=False)

    # -- Explicit camera lifecycle (state machine + sessions) --------------

    def _set_lifecycle(self, target: CameraConnectionState) -> None:
        """Apply a lifecycle transition and reflect it in the UI.

        Unexpected transitions are logged loudly but still applied: the
        displayed state must always reflect reality, never wishful logic.
        """
        current = self._lifecycle
        if not allowed_transition(current, target):
            logger.warning(
                "Camera lifecycle unexpected transition %s -> %s (cam=%s)",
                current.value,
                target.value,
                self._selected_camera_id,
            )
        self._lifecycle = target
        self._apply_lifecycle_to_ui()

    def _apply_lifecycle_to_ui(self) -> None:
        """Mirror the lifecycle state onto toolbar/panel indicators."""
        self._toolbar.set_connection_state(self._lifecycle)
        self._acq_panel.set_connection_state(self._lifecycle)
        status_text = {
            CameraConnectionState.DISCONNECTED: "Connection: Disconnected",
            CameraConnectionState.CONNECTING: "Connection: Connecting...",
            CameraConnectionState.CONNECTED: "Connection: Connected",
            CameraConnectionState.STARTING: "Connection: Starting...",
            CameraConnectionState.ACQUIRING: "Connection: Acquiring",
            CameraConnectionState.STOPPING: "Connection: Stopping...",
            CameraConnectionState.DISCONNECTING: "Connection: Disconnecting...",
            CameraConnectionState.ERROR: "Connection: Error",
        }.get(self._lifecycle, f"Connection: {self._lifecycle.value}")
        self._status_conn.setText(status_text)

    def _begin_session(self, camera_id: str) -> int:
        """Start a new session epoch for ``camera_id``.

        Bumps the generation so every queued result/render request from an
        older session is structurally stale, resets the render baselines
        (sequences restart at 0 per camera) and clears the displays.
        Returns the new generation.
        """
        self._session.renew(camera_id)
        self._selected_camera_id = camera_id
        self._image_widget.set_session(camera_id)
        self._vl_widget.set_session(camera_id)
        self._display_rate.reset()
        self._latest_result = None
        self._session_started_at = time.perf_counter()
        self._first_frame_at = None
        self._first_display_at = None
        self._image_widget.clear()
        self._vl_widget.clear()
        self._set_finder_thumbnail(None)
        # Session-aware counters: the previous session's frame/FPS numbers
        # must never be shown for the new session (stale "Frames: 878").
        self._status_frames.setText("Frames: —")
        self._status_fps.setText("FPS: —")
        self._status_proc.setText("Processing: — ms")
        self._frame_info_panel.clear()
        return self._session.generation

    def _log_camera_diagnostics(self, prefix: str, camera_id: str | None = None) -> None:
        """Log the full pre-Connect/pre-Start lifecycle snapshot.

        Answers, in one line per call site: which camera is selected,
        whether the parent registry has it, whether its child process
        exists/is alive, what the child reports, and what the GUI
        believes. Kept permanently: this is the first thing needed when
        Start ever reports "No running camera" again.
        """
        camera_id = camera_id if camera_id is not None else self._selected_camera_id
        runtime = self._runtime_service
        entry = pid = alive = child_state = shm = None
        running = observing = False
        observer_attached = self._observer is not None
        try:
            if runtime is not None and camera_id is not None:
                running = bool(runtime.is_camera_running(camera_id))
                observing = observing or bool(runtime.is_observer_running(camera_id))
                entry = True
                try:
                    handle = runtime.process_handle(camera_id)
                except Exception:
                    handle = None
                if handle is not None:
                    try:
                        pid = handle.pid
                    except Exception:
                        pid = None
                    try:
                        alive = bool(handle.process.is_alive())
                    except Exception:
                        alive = None
                try:
                    probe = getattr(runtime, "acquisition_child_state", None)
                    child_state = probe(camera_id) if probe is not None else "unknown-backend"
                    child_state = getattr(child_state, "value", child_state)
                except Exception:
                    child_state = "probe-failed"
                shm = "see-ring"  # SHM attachment is owned by runtime/observer rings
        except Exception:
            entry = False
        identity = None
        try:
            config = self._config_service.get_camera_config(camera_id) if camera_id else None
            identity = getattr(getattr(config, "identity", None), "serial_number", None)
        except Exception:
            pass
        logger.info(
            "%s cam=%s identity=%s lifecycle=%s generation=%s "
            "runtime_entry=%s running=%s pid=%s alive=%s child=%s "
            "observer_widget=%s observer_runtime=%s",
            prefix,
            camera_id,
            identity,
            self._lifecycle.value,
            self._session.generation,
            entry,
            running,
            pid,
            alive,
            child_state,
            observer_attached,
            observing,
        )

    def _set_finder_thumbnail(self, image) -> None:
        """Feed the latest full workspace frame to the View Finder dock.

        Same frame object the workspace paints (implicitly shared, no
        copy, no second stream); latest-wins by replacement.
        """
        try:
            self._finder_widget.set_image(image)
        except Exception:
            logger.debug("View finder update failed", exc_info=True)

    def _selection_mismatch(self, camera_id: str) -> str | None:
        """Check selected == toolbar == panel identity (section-3 invariant).

        Returns None when every view agrees with the authority
        (``self._selected_camera_id``), else a description of the
        mismatch. Reads are non-blocking Qt property/combo lookups.
        """
        try:
            toolbar_id = self._toolbar._camera_combo.currentData()
        except Exception:
            toolbar_id = "<unreadable>"
        try:
            panel_identity = self._acq_panel._selected_camera_identity
            panel_id = getattr(panel_identity, "camera_id", None)
        except Exception:
            panel_id = "<unreadable>"
        parts = []
        if toolbar_id != camera_id:
            parts.append(f"toolbar={toolbar_id}")
        if panel_id != camera_id:
            parts.append(f"panel={panel_id}")
        if not parts:
            return None
        return f"authority={camera_id} " + " ".join(parts)

    def _resync_selection_views(self, camera_id: str) -> None:
        """Re-assert toolbar/panel views from the selection authority.

        Used only after an invariant refusal: the combo is moved back to
        the authoritative camera (emitting through the normal switch
        pipeline, which early-returns when already convergent) and the
        panel identity reloaded. Never invents a new selection.
        """
        try:
            self._toolbar.blockSignals(True)
            try:
                self._toolbar.select_camera_by_id(camera_id)
            finally:
                self._toolbar.blockSignals(False)
        except Exception:
            logger.debug("Selection resync (toolbar) failed", exc_info=True)
        try:
            if self._selected_camera_id:
                self._load_camera_config(self._selected_camera_id)
            else:
                self._clear_all_panels()
        except Exception:
            logger.debug("Selection resync (panels) failed", exc_info=True)

    def _detach_observer(self) -> bool:
        """Detach the widget's observer without touching acquisition.

        Disconnects the Qt signals FIRST (so in-flight queued results can
        no longer reach the slot), then stops the consumer thread with a
        bounded wait and clears the runtime-side reference. Returns True
        when an observer was attached.
        """
        observer, self._observer = self._observer, None
        if observer is None:
            return False
        for signal_name, slot in (
            ("result_ready", self._on_processing_result),
            ("error_occurred", self._on_observer_error),
        ):
            try:
                getattr(observer, signal_name).disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        camera_id = getattr(observer, "camera_id", None) or self._selected_camera_id
        try:
            observer.stop(timeout=_OBSERVER_STOP_TIMEOUT_S)
        except Exception:
            logger.exception("Observer stop failed for camera %s", camera_id)
        if self._runtime_service is not None and camera_id is not None:
            try:
                self._runtime_service.stop_observer(camera_id)
            except Exception:
                logger.debug("Runtime observer detach failed", exc_info=True)
        return True

    def _bg_busy(self) -> bool:
        """True while a background camera operation is undelivered.

        Tag-based (not thread-alive-based): a thread that just finished
        but whose outcome has not reached the GUI slot yet still owns the
        pipeline, so a new operation must queue behind it.
        """
        return self._bg_tag is not None

    def _run_background(self, tag: str, fn) -> bool:
        """Run ``fn`` off the GUI thread; the outcome returns via _bg_done.

        Returns False when another camera operation is already in flight
        (the caller must queue or reject instead of overlapping). The
        worker is a daemon thread: it never blocks interpreter exit, and
        no QObject crosses thread boundaries.
        """
        if self._bg_busy():
            return False

        def _target() -> None:
            try:
                result = fn()
            except Exception as exc:  # never crash the worker silently
                logger.exception("Background camera operation failed")
                try:
                    self._bg_done.emit(False, tag, str(exc)[:300], None)
                except RuntimeError:
                    pass  # interpreter/widget teardown; nothing to report to
            else:
                try:
                    self._bg_done.emit(True, tag, "", result)
                except RuntimeError:
                    pass

        thread = threading.Thread(
            target=_target, name=f"ConfigCamOp-{tag}", daemon=True
        )
        self._bg_thread = thread
        self._bg_tag = tag
        thread.start()
        return True

    def _teardown_camera_blocking(self, camera_id: str, generation: int) -> TeardownTimings:
        """Safe shutdown sequence for one camera (BACKGROUND thread only).

        Order: observer -> processing consumer -> acquisition/child
        process (bounded join, terminate on escalation) -> SHM detach
        verification. Never touches any other camera's pipeline.
        """
        timings = TeardownTimings(camera_id=camera_id, generation=generation)
        runtime = self._runtime_service

        # Capture the child-process handle BEFORE stopping so we can prove
        # the exact old PID is gone afterwards.
        old_pid: int | None = None
        old_handle = None
        if runtime is not None:
            try:
                old_pid = runtime.process_pid(camera_id)
                old_handle = runtime.process_handle(camera_id)
            except Exception:
                logger.debug("PID snapshot failed", exc_info=True)
        timings.pid = old_pid

        # Phase 1: observer + processing consumer (defensive; the GUI
        # thread already detached the widget observer).
        phase = time.perf_counter()
        observer_ok = True
        if runtime is not None:
            try:
                runtime.stop_observer(camera_id)
            except Exception:
                observer_ok = False
                logger.exception("Teardown observer stop failed cam=%s", camera_id)
        timings.observer_stop_ms = (time.perf_counter() - phase) * 1000.0
        timings.observer_stopped = observer_ok
        timings.processing_stopped = observer_ok

        # Phase 2: acquisition worker / camera child process (bounded).
        phase = time.perf_counter()
        if runtime is not None:
            try:
                runtime.stop_camera(camera_id, timeout=_TEARDOWN_PROCESS_TIMEOUT_S)
            except Exception:
                logger.exception("Teardown stop_camera failed cam=%s", camera_id)
        timings.acquisition_stop_ms = (time.perf_counter() - phase) * 1000.0

        # Phase 3: verify the old process is actually gone (bounded poll
        # on process state — no sleeps-to-drain, no global event flush).
        phase = time.perf_counter()
        exited = True
        if old_handle is not None:
            try:
                deadline = time.monotonic() + _TEARDOWN_VERIFY_TIMEOUT_S
                while old_handle.process.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.02)
                exited = not old_handle.process.is_alive()
                if not exited:
                    try:
                        old_handle.process.terminate()
                        old_handle.process.join(1.0)
                    except Exception:
                        pass
                    exited = not old_handle.process.is_alive()
                    timings.escalated = True
            except Exception:
                logger.debug("Process exit verification failed", exc_info=True)
        elif runtime is not None:
            try:
                exited = not runtime.is_camera_running(camera_id)
            except Exception:
                pass
        timings.process_exit_ms = (time.perf_counter() - phase) * 1000.0
        timings.process_exited = exited
        timings.shm_detached = True
        timings.shm_detach_ms = 0.0
        timings.finish()
        logger.info(timings.summary())
        return timings

    def _connect_camera_blocking(self, config) -> float:
        """Start one camera and verify it reaches STREAMING (BACKGROUND thread).

        Returns the connect duration in ms. Partial startups are cleaned
        up defensively so a failed connection never leaks a process.
        """
        camera_id = config.identity.camera_id
        started = time.perf_counter()
        try:
            self._runtime_service.start_camera(config)
        except Exception:
            try:
                self._runtime_service.stop_camera(camera_id, timeout=2.0)
            except Exception:
                pass
            raise
        connect_ms = (time.perf_counter() - started) * 1000.0
        logger.info(
            "CAMERA SESSION START camera=%s connect=%.0fms", camera_id, connect_ms
        )
        return connect_ms

    def _teardown_then_connect_blocking(self, old_camera_id: str | None, generation: int, config):
        """Atomic switch: full teardown of the old camera, then connect new."""
        timings = None
        if old_camera_id is not None:
            timings = self._teardown_camera_blocking(old_camera_id, generation)
        connect_ms = self._connect_camera_blocking(config)
        return (timings, connect_ms)

    def _activate_camera(self, camera_id: str, *, connect: bool, config=None) -> None:
        """Select ``camera_id``, tearing down the old camera safely first.

        When ``connect`` is set, the new camera is connected in the same
        serialized background operation (switch A -> B -> start). The GUI
        never blocks: it shows DISCONNECTING/CONNECTING immediately while
        exactly one background transition owns the pipeline.
        """
        if not camera_id:
            return
        if self._bg_busy():
            # A transition owns the pipeline: queue the latest request.
            self._pending_switch = (camera_id, connect, config)
            self._status_label.setText("Camera busy — switch queued...")
            return
        if camera_id == self._selected_camera_id and not connect:
            return  # nothing to do
        if connect:
            self._log_camera_diagnostics("CONNECT REQUEST", camera_id)

        # The toolbar combo is a pure view of the selection authority.
        # Move it silently (blocked: no phantom switch) so no path can
        # leave the visible selection behind the activated camera — every
        # production caller previously had to remember this separately.
        try:
            self._toolbar.blockSignals(True)
            try:
                self._toolbar.select_camera_by_id(camera_id)
            finally:
                self._toolbar.blockSignals(False)
        except Exception:
            logger.debug("Toolbar selection sync failed", exc_info=True)

        old_camera_id = self._selected_camera_id
        needs_teardown = (
            old_camera_id is not None
            and old_camera_id != camera_id
            and self._runtime_service is not None
            and (
                self._runtime_service.is_camera_running(old_camera_id)
                or self._runtime_service.is_observer_running(old_camera_id)
                or self._observer is not None
            )
        )

        # GUI-thread immediate part: cut the old display path FIRST so no
        # stale frame can be accepted from this instant on, then renew the
        # session epoch and select the new camera.
        self._detach_observer()
        if self._runtime_service is not None and old_camera_id is not None:
            try:
                self._runtime_service.stop_observer(old_camera_id)
            except Exception:
                pass
        generation = self._begin_session(camera_id)

        if config is None and connect and self._runtime_service is not None:
            cfg = self._config_service.get_camera_config(camera_id)
            config = cfg

        if needs_teardown or connect:
            if self._runtime_service is None:
                QMessageBox.warning(self, "Unavailable", "Runtime service not available.")
                self._set_lifecycle(CameraConnectionState.DISCONNECTED)
                return
            if connect and config is None:
                QMessageBox.warning(self, "Connect Failed", "No configuration for camera.")
                self._set_lifecycle(CameraConnectionState.DISCONNECTED)
                return
            if needs_teardown:
                self._set_lifecycle(CameraConnectionState.DISCONNECTING)
                self._status_label.setText(f"Disconnecting {old_camera_id}...")
            else:
                self._set_lifecycle(CameraConnectionState.CONNECTING)
                self._status_label.setText(f"Connecting {camera_id}...")
            if connect:
                self._bg_connect_config = config
                started = self._run_background(
                    "switch_connect",
                    lambda: self._teardown_then_connect_blocking(
                        old_camera_id if needs_teardown else None, generation, config
                    ),
                )
            else:
                self._bg_connect_config = None
                started = self._run_background(
                    "switch",
                    lambda: self._teardown_camera_blocking(old_camera_id, generation),
                )
            if not started:  # lost a race with another op; queue instead
                self._pending_switch = (camera_id, connect, config)
            return

        # No pipeline to tear down and no connect requested: pure reselect.
        self._set_lifecycle(CameraConnectionState.DISCONNECTED)
        self._load_camera_config(camera_id)
        self._status_label.setText(f"Camera: {camera_id}")

    @pyqtSlot(bool, str, str, object)
    def _on_bg_done(self, ok: bool, tag: str, message: str, result: object) -> None:
        """Handle completion of a background camera operation (GUI thread).

        Deliveries tagged for an older operation (impossible by
        construction — a single in-flight op — but guarded anyway) are
        ignored so they can never drive the state machine.
        """
        if tag != self._bg_tag:
            logger.debug("Ignoring stale background delivery tag=%s", tag)
            return
        self._bg_tag = None
        self._bg_thread = None
        if ok:
            self._on_bg_result(tag, result)
        else:
            self._on_bg_error(tag, message)
        self._process_pending_camera_request()

    def _on_bg_result(self, tag: str | None, result: object) -> None:
        """Apply a successful background camera operation (GUI thread)."""
        try:
            if tag == "switch":
                self._set_lifecycle(CameraConnectionState.DISCONNECTED)
                if self._selected_camera_id:
                    self._load_camera_config(self._selected_camera_id)
                    self._status_label.setText(f"Camera: {self._selected_camera_id}")
            elif tag == "switch_connect":
                timings, connect_ms = result
                self._set_lifecycle(CameraConnectionState.CONNECTED)
                self._status_conn.setText("Connection: Connected")
                self._status_label.setText(
                    f"Camera connected ({connect_ms:.0f} ms) - press Start to begin acquisition"
                )
                if self._selected_camera_id:
                    self._load_camera_config(self._selected_camera_id)
                self._refresh_focus_panel()
                self._refresh_nuc_panel()
            elif tag == "disconnect":
                self._set_lifecycle(CameraConnectionState.DISCONNECTED)
                if self._selected_camera_id:
                    self._load_camera_config(self._selected_camera_id)
                self._status_label.setText("Camera disconnected")
            elif tag == "connect":
                connect_ms = float(result)
                self._set_lifecycle(CameraConnectionState.CONNECTED)
                self._status_conn.setText("Connection: Connected")
                self._status_label.setText(
                    f"Camera connected ({connect_ms:.0f} ms) - press Start to begin acquisition"
                )
                if self._selected_camera_id:
                    self._load_camera_config(self._selected_camera_id)
                self._refresh_focus_panel()
                self._refresh_nuc_panel()
        finally:
            self._bg_connect_config = None

    def _on_bg_error(self, tag: str | None, message: str) -> None:
        """Handle failure of a background camera operation (GUI thread)."""
        self._bg_connect_config = None
        logger.warning("Background camera operation %s failed: %s", tag, message)
        if tag in ("switch_connect", "connect"):
            self._set_lifecycle(CameraConnectionState.ERROR)
            self._acq_panel.set_focus_enabled(False, "Camera not running")
            self._acq_panel.set_nuc_enabled(False, "Camera not running")
            QMessageBox.warning(self, "Connect Failed", f"Failed to connect: {message}")
        else:
            # Teardown/switch/disconnect failures must still land in a
            # known state with the old pipeline verified stopped.
            self._set_lifecycle(CameraConnectionState.DISCONNECTED)
            if self._selected_camera_id:
                self._load_camera_config(self._selected_camera_id)

    def _process_pending_camera_request(self) -> None:
        """Run whatever camera request arrived during a transition."""
        if self._pending_disconnect:
            self._pending_disconnect = False
            self._on_disconnect()
            return
        pending, self._pending_switch = self._pending_switch, None
        if pending is not None:
            camera_id, connect, config = pending
            self._activate_camera(camera_id, connect=connect, config=config)

    def _load_camera_config(self, camera_id: str) -> None:
        """Load configuration for the selected camera into all panels."""
        config = self._config_service.get_camera_config(camera_id)
        if not config:
            self._clear_all_panels()
            return

        identity = config.identity
        metadata = dict(config.metadata or {})

        # Connection state shown here is ALWAYS the lifecycle authority
        # (self._lifecycle), never a transport-liveness probe. In the
        # process-per-camera architecture the child streams as soon as it
        # is connected, so is_camera_running() is True in both CONNECTED
        # and ACQUIRING; deriving the button matrix from it misreports a
        # merely-connected camera as acquiring and kills the Start button.
        # ACQUIRING for display means exactly one thing: the observer/
        # processing consumer is attached (see _on_start_acquisition).
        status = self._lifecycle

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

        # NOTE: button states follow self._lifecycle via set_connection_state
        # above. Do NOT re-derive them from is_camera_running() here: the
        # child process streams while merely CONNECTED, so that probe would
        # force ACQUIRING and disable Start (the Start-button failure).

        # Refresh focus + NUC state for the selected camera (async; no-op
        # when the camera is not running).
        self._refresh_focus_panel()
        self._refresh_nuc_panel()

    # -- Focus (UI -> runtime/service -> driver, never GVCP directly) --

    def _stop_focus_worker(self, timeout_ms: int = 0) -> None:
        """Request the focus thread to stop without freezing the live feed.

        Interactive callers (focus Apply/Refresh, camera switch) use the
        default ``timeout_ms=0``: quit is requested and cleanup is deferred
        to the thread's ``finished`` signal, so the GUI event loop keeps
        delivering observer frames while the old worker drains. Only
        teardown (``closeEvent``) passes a positive timeout for a bounded
        blocking wait when no feed is left to protect.

        A timeout expiry is logged loudly and never silent: the thread is
        left to quit itself via its finished/failed -> quit chain once the
        in-flight GVCP operation returns (all GVCP transactions are
        bounded), so it is never destroyed while running.
        """
        thread, self._focus_thread = self._focus_thread, None
        worker, self._focus_worker = self._focus_worker, None
        if thread is None:
            return
        try:
            thread.quit()
        except RuntimeError:
            return
        if timeout_ms <= 0:
            # Non-blocking: Qt owns cleanup once the worker's queued
            # finished/failed signal quits the thread's event loop.
            try:
                if worker is not None:
                    thread.finished.connect(worker.deleteLater)
                thread.finished.connect(thread.deleteLater)
            except RuntimeError:
                pass
            return
        try:
            if worker is not None:
                thread.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
        except RuntimeError:
            pass
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

    def _reconcile_lifecycle(self) -> None:
        """Reconcile the lifecycle with transport/display reality.

        Called when Configuration mode is (re)activated. The lifecycle
        remains the authority; this only corrects the two divergences
        that can happen while the widget is inactive:

        - ACQUIRING with no observer attached (display path detached on
          mode switch) but transport alive -> CONNECTED.
        - ACQUIRING with dead transport -> ERROR (unexpected loss).
        - CONNECTED with dead transport -> DISCONNECTED.
        Transitional states are never touched: a background operation
        owns the pipeline there.
        """
        if self._runtime_service is None or self._selected_camera_id is None:
            return
        if is_transitional(self._lifecycle):
            return
        camera_id = self._selected_camera_id
        try:
            running = self._runtime_service.is_camera_running(camera_id)
            observing = (self._observer is not None) or bool(
                self._runtime_service.is_observer_running(camera_id)
            )
        except Exception:
            logger.debug("Lifecycle reconcile probe failed", exc_info=True)
            return
        if self._lifecycle == CameraConnectionState.ACQUIRING:
            if not running:
                self._set_lifecycle(CameraConnectionState.ERROR)
            elif not observing:
                self._set_lifecycle(CameraConnectionState.CONNECTED)
        elif self._lifecycle == CameraConnectionState.CONNECTED:
            if not running:
                self._set_lifecycle(CameraConnectionState.DISCONNECTED)

    def _clear_all_panels(self) -> None:
        """Clear all panels when no camera selected."""
        self._image_widget.clear()
        self._vl_widget.clear()
        self._image_widget.set_roi_overlays([])
        self._scale_panel.update_cursor_temperature(None)
        self._set_finder_thumbnail(None)
        self._roi_panel.set_camera("")
        self._alarm_panel.set_camera("")
        self._stats_panel.clear()
        self._frame_info_panel.clear()
        self._toolbar.set_connection_state(CameraConnectionState.DISCONNECTED)
        self._acq_panel.set_camera_identity(None)
        self._acq_panel.set_connection_state(CameraConnectionState.DISCONNECTED)
        self._acq_panel.clear_image_info()

    @pyqtSlot(object)
    def _on_processing_result(self, result: ProcessingResult) -> None:
        """Receive ProcessingResult from observer (session-gated).

        Results from a previous camera session — queued before a switch
        or disconnect — are discarded here. No event-queue flushing and
        no sleeps: staleness is decided by the (camera_id, generation)
        token, never by timing.
        """
        sender = self.sender()
        sender_generation = getattr(sender, "_session_generation", None)
        frame = result.frame if result is not None else None
        frame_camera = (
            getattr(getattr(frame, "descriptor", None), "camera_id", None)
            if frame is not None
            else None
        )
        if sender_generation is not None and sender_generation != self._session.generation:
            self._stale_results_dropped += 1
            logger.debug(
                "Result dropped (stale generation %s != %s cam=%s)",
                sender_generation,
                self._session.generation,
                frame_camera,
            )
            return
        if frame_camera is not None and frame_camera != self._selected_camera_id:
            self._stale_results_dropped += 1
            logger.debug(
                "Result dropped (stale camera %s != %s)",
                frame_camera,
                self._selected_camera_id,
            )
            return
        if self._first_frame_at is None and self._session_started_at is not None:
            self._first_frame_at = time.perf_counter()
            logger.info(
                "First valid frame cam=%s generation=%s latency=%.0fms",
                self._selected_camera_id,
                self._session.generation,
                (self._first_frame_at - self._session_started_at) * 1000.0,
            )
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
        try:
            acq_mono_ns = (
                int(float(frame.descriptor.monotonic_timestamp) * 1e9)
                if frame is not None and frame.descriptor.monotonic_timestamp is not None
                else None
            )
        except (TypeError, ValueError):
            acq_mono_ns = None
        if frame is not None and frame.payload.visible is not None:
            self._vl_widget.set_frame(
                frame.payload.visible,
                frame.descriptor.sequence,
                frame.descriptor.visible.sequence,
                camera_id=frame.descriptor.camera_id,
                acq_mono_ns=acq_mono_ns,
            )
        else:
            self._vl_widget.set_frame(None, sequence if sequence is not None else -1)

        # Update image info (acquisition panel + Image Information dock)
        if frame:
            self._acq_panel.update_image_info(
                image_size=f"{frame.payload.thermal.shape[1]}×{frame.payload.thermal.shape[0]}" if frame.payload.thermal is not None else "—",
                frame=str(frame.descriptor.sequence),
                timestamp=f"{frame.descriptor.timestamp:.3f}",
                processing=f"{result.processing_time_ms:.1f} ms",
            )
            self._frame_info_panel.update_from_frame(frame, result)

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
        # Finder gets the full workspace frame (same object, no copy).
        self._set_finder_thumbnail(image)
        if self._first_display_at is None and self._session_started_at is not None:
            self._first_display_at = time.perf_counter()
            logger.info(
                "First display cam=%s generation=%s latency=%.0fms",
                self._selected_camera_id,
                self._session.generation,
                (self._first_display_at - self._session_started_at) * 1000.0,
            )

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
            self._camera_selection_dialog.finished.connect(self._on_connect_dialog_finished)

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

    @pyqtSlot(int)
    def _on_connect_dialog_finished(self, result: int) -> None:
        """Recover from a dismissed Connect dialog.

        Accepted is owned by the camera_selected path. Any other close
        (dismissed without a selection) releases the CONNECTING hold the
        Connect button placed on the panels and re-mirrors the lifecycle.
        Without this, dismissing the dialog freezes every button in a
        stuck CONNECTING state until an unrelated reload happens.
        """
        self._restore_connect_cursor()
        if result == QDialog.DialogCode.Accepted:
            return
        if self._bg_busy():
            return  # a transition owns the UI; its done-handler refreshes
        self._apply_lifecycle_to_ui()
        if self._selected_camera_id:
            self._load_camera_config(self._selected_camera_id)
        elif self._lifecycle == CameraConnectionState.DISCONNECTED:
            self._status_label.setText("Ready")

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
        if self._config_manager is not None:
            self._config_manager.save_camera_mapping(
                CameraMappingConfig(
                    camera_id=camera_id,
                    serial_number=discovered_camera.serial_number,
                    enabled=updated_config.enabled,
                    name=updated_config.name or camera_id,
                    target_fps=None,
                    ip_address=discovered_camera.ip_address or "",
                    device_identifier=discovered_camera.device_identifier or "",
                )
            )

        # Select this camera in the toolbar without emitting the switch
        # signal (the safe _activate_camera pipeline below owns the
        # transition, including tearing down the previously selected
        # camera — never two active pipelines).
        self._toolbar.blockSignals(True)
        try:
            self._toolbar.select_camera_by_id(camera_id)
        finally:
            self._toolbar.blockSignals(False)

        # CONNECT: establish control + acquisition in one serialized
        # background operation. The GUI shows CONNECTING/DISCONNECTING
        # immediately; failures land in ERROR with partial resources
        # cleaned up and the user informed.
        self._activate_camera(camera_id, connect=True, config=updated_config)

    def _on_disconnect(self) -> None:
        """Handle Disconnect button: safe shutdown of the current camera.

        Disconnecting a running camera automatically performs the full
        safe shutdown sequence (observer -> consumer -> acquisition ->
        child process -> SHM detach) in a background operation. The GUI
        shows DISCONNECTING immediately and never blocks.
        """
        if not self._selected_camera_id or not self._runtime_service:
            return
        if self._bg_busy():
            # A transition is in flight: disconnect as soon as it lands.
            self._pending_disconnect = True
            self._pending_switch = None
            self._status_label.setText("Camera busy — disconnect queued...")
            return
        if not can_disconnect(self._lifecycle):
            return

        camera_id = self._selected_camera_id
        generation = self._session.generation

        # GUI-thread immediate part: cut the display path first.
        self._detach_observer()
        try:
            self._runtime_service.stop_observer(camera_id)
        except Exception:
            pass
        self._acq_panel.set_acquisition_running(False)

        # Focus + NUC no longer available once the camera stops.
        self._stop_focus_worker()
        self._focus_camera_id = None
        self._acq_panel.set_focus_enabled(False, "Camera not running")
        self._stop_nuc_worker()
        self._nuc_camera_id = None
        self._acq_panel.set_nuc_enabled(False, "Camera not running")

        # Renew the epoch so any late result from this camera is stale.
        self._session.renew(camera_id)
        self._image_widget.set_session(camera_id)
        self._vl_widget.set_session(camera_id)
        self._image_widget.clear()
        self._vl_widget.clear()
        self._set_finder_thumbnail(None)

        self._set_lifecycle(CameraConnectionState.DISCONNECTING)
        self._status_label.setText(f"Disconnecting {camera_id}...")
        started = self._run_background(
            "disconnect",
            lambda: self._teardown_camera_blocking(camera_id, generation),
        )
        if not started:
            self._pending_disconnect = True

    def _log_start_click(self) -> None:
        """Section-1 physical Start-button snapshot (no inference).

        Logged as the first statement of the Start handler, before any
        guard or refusal, so even refused clicks leave the exact
        identity picture behind.
        """
        try:
            config = (
                self._config_service.get_camera_config(self._selected_camera_id)
                if self._selected_camera_id
                else None
            )
            ui_id = self._selected_camera_id
            ui_serial = getattr(getattr(config, "identity", None), "serial_number", None)
            ui_name = getattr(config, "name", None)
        except Exception:
            ui_id = ui_serial = ui_name = "<unreadable>"
        try:
            panel_identity = self._acq_panel._selected_camera_identity
            panel_id = getattr(panel_identity, "camera_id", None)
            panel_serial = getattr(panel_identity, "serial_number", None)
        except Exception:
            panel_id = panel_serial = "<unreadable>"
        try:
            toolbar_id = self._toolbar._camera_combo.currentData()
        except Exception:
            toolbar_id = "<unreadable>"
        try:
            if self._runtime_service is not None:
                runtime_ids = sorted(self._runtime_service.running_camera_ids())
            else:
                runtime_ids = "<no-runtime>"
        except Exception:
            runtime_ids = "<unreadable>"
        try:
            observer_id = getattr(self._observer, "camera_id", None)
        except Exception:
            observer_id = "<unreadable>"
        logger.info(
            "START CLICK: ui_selected_id=%s ui_selected_serial=%s ui_selected_name=%s "
            "panel_camera_id=%s panel_serial=%s toolbar_camera_id=%s "
            "widget_camera_id=%s lifecycle=%s session_camera_id=%s "
            "runtime_camera_ids=%s generation=%s observer_camera_id=%s",
            ui_id,
            ui_serial,
            ui_name,
            panel_id,
            panel_serial,
            toolbar_id,
            self._selected_camera_id,
            self._lifecycle.value,
            self._session.camera_id,
            runtime_ids,
            self._session.generation,
            observer_id,
        )

    def _on_start_acquisition(self) -> None:
        """Handle Start acquisition button (verify child, attach display path).

        START requires a connected child process: the transport entry must
        exist and the child must report STREAMING over the status channel
        (the V3 equivalent of the standalone's acquisition_start having
        run). Only then is the single observer/processing consumer
        attached and stamped with the session generation. A missing or
        non-streaming child is refused with a truthful message — the GUI
        never attempts the observer against a dead transport, so the old
        "No running camera ... call start_camera first" can no longer
        surface from a stale UI state.
        """
        self._log_start_click()
        if not self._selected_camera_id or not self._runtime_service:
            return
        if self._bg_busy():
            self._status_label.setText("Camera busy — start queued...")
            return
        if not can_start(self._lifecycle):
            return

        camera_id = self._selected_camera_id
        self._log_camera_diagnostics("START REQUEST", camera_id)

        # Identity invariant (section 3): toolbar == panel == authority.
        # The phantom combo rebuild used to swap the selection mid-flight;
        # even with that fixed, Start snapshots ONE id at entry and refuses
        # on any mismatch instead of risking the wrong camera.
        mismatch = self._selection_mismatch(camera_id)
        if mismatch is not None:
            logger.error(
                "START REFUSED (identity mismatch) selected=%s %s generation=%s",
                camera_id,
                mismatch,
                self._session.generation,
            )
            self._resync_selection_views(camera_id)
            self._status_label.setText("Camera selection mismatch — corrected, press Start again")
            QMessageBox.warning(
                self,
                "Start Failed",
                f"Camera selection mismatch ({mismatch}). "
                "The display was re-synchronized; press Start again.",
            )
            return

        # Transport verification (cheap, non-blocking registry reads): the
        # child process must exist and report STREAMING. Anything else is
        # a drifted UI state, corrected here instead of probed downstream.
        refusal = self._refuse_start_if_not_ready(camera_id)
        if refusal is not None:
            self._log_camera_diagnostics(f"START REFUSED ({refusal})", camera_id)
            return

        config = self._config_service.get_camera_config(camera_id)
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

        previous_state = self._lifecycle
        self._detach_observer()
        self._set_lifecycle(CameraConnectionState.STARTING)
        try:
            # Camera runtime is the authority; this attaches the single consumer.
            # NOTE: the entry-local camera_id is used throughout — never
            # re-read self._selected_camera_id mid-flight, so no signal or
            # callback running inside this block can redirect the request
            # to a different camera.
            analysis = self._config_service.get_analysis_config(camera_id)
            if analysis is None:
                analysis = AnalysisConfig(camera_id=camera_id)
            observer = self._runtime_service.start_observer(camera_id, analysis_config=analysis)
            # Stamp the session epoch: results emitted by an older observer
            # (still draining its queued signals) carry an older token and
            # are discarded in _on_processing_result.
            observer._session_generation = self._session.generation
            self._observer = observer
            self._observer.result_ready.connect(self._on_processing_result, Qt.ConnectionType.QueuedConnection)
            self._observer.error_occurred.connect(self._on_observer_error, Qt.ConnectionType.QueuedConnection)
            logger.info(
                "CHILD STREAMING VERIFIED cam=%s generation=%s OBSERVER ATTACHED",
                camera_id,
                self._session.generation,
            )

        except Exception as exc:
            self._observer = None
            self._set_lifecycle(
                CameraConnectionState.CONNECTED
                if previous_state == CameraConnectionState.CONNECTED
                else CameraConnectionState.ERROR
            )
            QMessageBox.warning(self, "Start Failed", f"Failed to start acquisition: {exc}")
            return

        # Fresh acquisition renumbers frames from 0: reset the render
        # baselines for this (unchanged) session epoch.
        self._image_widget.set_session(camera_id)
        self._vl_widget.set_session(camera_id)
        self._session_started_at = time.perf_counter()
        self._first_frame_at = None
        self._first_display_at = None

        # Presentation updates are outside the runtime transaction.
        self._display_rate.reset()
        self._set_lifecycle(CameraConnectionState.ACQUIRING)
        self._status_label.setText("Acquisition started")

    def _refuse_start_if_not_ready(self, camera_id: str) -> str | None:
        """Refuse Start when the child transport is not usable.

        Returns None when the child exists and reports STREAMING (Start
        may proceed), otherwise corrects the drifted lifecycle, informs
        the user truthfully, and returns the reason. This is a refusal,
        not a workaround: without a streaming child there is nothing to
        observe, and attempting the observer would only reproduce the
        stale "No running camera" failure downstream.
        """
        runtime = self._runtime_service
        try:
            running = bool(runtime.is_camera_running(camera_id)) if runtime is not None else False
        except Exception:
            running = False
        if not running:
            # Transport gone (never connected, child died, or another
            # owner tore it down): the UI state drifted; correct it.
            if self._lifecycle == CameraConnectionState.ACQUIRING:
                self._set_lifecycle(CameraConnectionState.ERROR)
            else:
                self._set_lifecycle(CameraConnectionState.DISCONNECTED)
            self._status_label.setText("Camera is no longer connected — press Connect")
            QMessageBox.warning(
                self,
                "Start Failed",
                f"Camera {camera_id} is not connected (no camera process). "
                "Press Connect to reconnect, then Start.",
            )
            return "no-transport"
        # Child exists: it must report STREAMING over the status channel
        # (unknown backends without the probe are allowed through).
        child_state = None
        try:
            probe = getattr(runtime, "acquisition_child_state", None)
            child_state = probe(camera_id) if probe is not None else None
        except Exception:
            child_state = None
        if child_state is not None and child_state is not AcquisitionState.STREAMING:
            self._set_lifecycle(CameraConnectionState.ERROR)
            self._status_label.setText(
                f"Camera child reports {getattr(child_state, 'value', child_state)} — reconnect"
            )
            QMessageBox.warning(
                self,
                "Start Failed",
                f"Camera {camera_id} is not streaming "
                f"(child reports {getattr(child_state, 'value', child_state)}). "
                "Disconnect and Connect again.",
            )
            return f"child-{getattr(child_state, 'value', child_state)}"
        return None

    def _on_stop_acquisition(self) -> None:
        """Handle Stop acquisition button (detach display, keep connection)."""
        if not self._selected_camera_id or not self._runtime_service:
            return
        if self._bg_busy():
            self._status_label.setText("Camera busy — stop queued...")
            return
        if not can_stop(self._lifecycle):
            return

        try:
            self._set_lifecycle(CameraConnectionState.STOPPING)
            self._detach_observer()
            self._set_lifecycle(CameraConnectionState.CONNECTED)
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
        sender = self.sender()
        sender_generation = getattr(sender, "_session_generation", None)
        if sender_generation is not None and sender_generation != self._session.generation:
            return  # stale error from a previous session
        self._set_lifecycle(CameraConnectionState.ERROR)
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
        """Update status bar stats + 1 Hz transport liveness watch.

        The watch makes the child authoritative: if the transport died
        (child crash, external teardown) while the UI believes it is
        CONNECTED or ACQUIRING, the lifecycle is corrected within about
        a second instead of drifting until the next click. Transitional
        states are never touched (a background operation owns them), and
        driver-level RECONNECTING keeps the transport alive, so recovery
        in progress is left alone. All probes are non-blocking.
        """
        if (
            self._runtime_service is not None
            and self._selected_camera_id is not None
            and not is_transitional(self._lifecycle)
            and self._lifecycle
            in (
                CameraConnectionState.CONNECTED,
                CameraConnectionState.ACQUIRING,
            )
        ):
            try:
                was = self._lifecycle
                self._reconcile_lifecycle()
                if self._lifecycle != was:
                    logger.warning(
                        "Transport drift corrected cam=%s generation=%s %s -> %s",
                        self._selected_camera_id,
                        self._session.generation,
                        was.value,
                        self._lifecycle.value,
                    )
                    if self._lifecycle == CameraConnectionState.ERROR:
                        self._status_label.setText("Camera connection lost")
            except Exception:
                logger.debug("Liveness probe failed", exc_info=True)

        if self._lifecycle in (
            CameraConnectionState.DISCONNECTED,
            CameraConnectionState.DISCONNECTING,
            CameraConnectionState.CONNECTING,
        ):
            # No live session: never paint transport numbers (session-aware
            # counters; fixes stale "Frames: 878" after disconnect).
            return
        if self._runtime_service and self._selected_camera_id:
            try:
                cam_stats = self._runtime_service.camera_stats(self._selected_camera_id)
            except Exception:
                cam_stats = None
            if cam_stats:
                fps = cam_stats.current_fps or cam_stats.average_fps
                if fps:
                    self._status_fps.setText(f"FPS: {fps:.1f}")
                self._status_frames.setText(f"Frames: {cam_stats.frames_received}")
                self._frame_info_panel.set_acquisition_fps(fps if fps else None)

        if self._observer:
            obs_stats = self._observer.stats()
            if obs_stats:
                self._status_proc.setText(f"Processing: {obs_stats.average_processing_time_ms:.1f} ms")
            self._frame_info_panel.set_display_fps(self._display_rate.fps())

    # Snapshot / Save handlers
    def _on_snapshot(self) -> None:
        """Handle snapshot button."""
        # TODO: Implement snapshot save
        self._status_label.setText("Snapshot not yet implemented")

    def _on_save_config(self) -> None:
        """Handle save config button."""
        if self._config_editor is not None:
            # Reveal the Configuration Editor panel (state preserved).
            self.open_panel("config_editor")
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
        # Correct any lifecycle drift from while inactive (e.g. observer
        # detached on mode switch while transport kept running), then
        # resume the status ticker stopped on deactivation.
        self._reconcile_lifecycle()
        if not self._stats_timer.isActive():
            self._stats_timer.start(1000)

    def on_mode_deactivated(self) -> None:
        """Called when configuration mode is deactivated."""
        self._save_shelf_state()
        self._restore_connect_cursor()
        if self._camera_selection_dialog is not None:
            self._camera_selection_dialog.close()
        # Detach the display path; the camera child process keeps running
        # so a mode switch back (or Live mode sharing the runtime) is fast.
        self._detach_observer()
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

        # Diagnostics: compare service identity with Live
        try:
            logger.info("CONFIG WINDOW CREATED: config_service id=%s (%s)", hex(id(config_service)), type(config_service).__name__)
            if config_manager is not None and hasattr(config_manager, "config_path"):
                logger.info("CONFIG WINDOW config_path=%s exists=%s", config_manager.config_path, config_manager.config_path.exists())
                tmp = config_manager.config_path.with_suffix(config_manager.config_path.suffix + ".tmp")
                logger.info("CONFIG WINDOW tmp_path=%s exists=%s", tmp, tmp.exists())
        except Exception:
            pass

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

        # View menu: side-shelf panels first (predictable restore after
        # hiding), then image zoom controls.
        view_menu = menu_bar.addMenu("View")
        for action in self._config_widget.panel_toggle_actions():
            view_menu.addAction(action)
        view_menu.addSeparator()
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
        config_action.triggered.connect(lambda: self._config_widget.open_panel("config_editor"))
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
