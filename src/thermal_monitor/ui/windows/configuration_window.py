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
from PyQt6.QtGui import QColor, QAction, QFont

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
    ThermalScalePanel,
    FrameInfoPanel,
    ROIPanel,
    AlarmPanel,
    StatisticsPanel,
    CameraSelectionDialog,
    AcquisitionSetupDialog,
    ImageAcquisitionPanel,
)
from thermal_monitor.ui.theme import ThemeManager
from thermal_monitor.ui.theme.properties import set_role, set_status, set_variant


logger = logging.getLogger(__name__)


_DOCK_SETTINGS_ORG = "ThermalMonitoringSystem"
_DOCK_SETTINGS_APP = "ConfigWorkstation"
_DOCK_SETTINGS_KEY = "dock_layout_v1"

#: Lifecycle state -> top-bar status role (mirrors the status roles used
#: by the panel indicators; kept local so the removed top camera selector
#: module is not imported for a two-line map).
_TOP_CONNECTION_STATUS_MAP = {
    CameraConnectionState.DISCONNECTED: "disconnected",
    CameraConnectionState.CONNECTING: "connecting",
    CameraConnectionState.CONNECTED: "connected",
    CameraConnectionState.STARTING: "connecting",
    CameraConnectionState.ACQUIRING: "acquiring",
    CameraConnectionState.STOPPING: "connecting",
    CameraConnectionState.DISCONNECTING: "connecting",
    CameraConnectionState.DEGRADED: "degraded",
    CameraConnectionState.RECONNECTING: "connecting",
    CameraConnectionState.ERROR: "error",
}

# Bounded waits (seconds) for teardown phases. No arbitrary sleeps: each
# phase is an event/process-state join with its own timeout, after which
# the operation escalates (terminate) instead of blocking the GUI.
_TEARDOWN_PROCESS_TIMEOUT_S = 5.0
_TEARDOWN_VERIFY_TIMEOUT_S = 1.0
_OBSERVER_STOP_TIMEOUT_S = 2.0

#: -- Side-shelf geometry (ThermoView-style narrow vertical tabs) ---------
#: CENTRAL shelf-size constants: change these to resize the shelf rails.
#: No other shelf dimension is hardcoded elsewhere. The two point sizes
#: live in ui.theme.fonts (SHELF_TAB_FONT_PT / PANEL_TITLE_FONT_PT —
#: the central QSS rules must read them from there, since the
#: stylesheet cannot import this module); the PANEL_* names below are
#: aliases kept so this block stays the single documented place to look.
#: To change a size, edit the fonts.py value.
from thermal_monitor.ui.theme.fonts import (
    PANEL_TITLE_FONT_PT as _PANEL_TITLE_FONT_PT,
)
from thermal_monitor.ui.theme.fonts import (
    SHELF_TAB_FONT_PT as _SHELF_TAB_FONT_PT,
)

PANEL_SHELF_WIDTH = 25  #: rail width in px (floating tabs + margins)
PANEL_TAB_WIDTH = 21  #: floating tab thickness in px (industrial tool strip)
PANEL_TAB_MIN_HEIGHT = 60  #: clickability floor in px (short titles pad to this)
PANEL_TAB_MAX_HEIGHT = 400  #: extreme-scale ceiling (long titles never clip below this)
PANEL_TAB_FONT_SIZE_PT = _SHELF_TAB_FONT_PT  #: readable 12 pt tab font (at 100%)
PANEL_TAB_SPACING = 6  #: vertical gap between floating tabs in px
PANEL_TAB_MARGIN = 2  #: rail/host contents margin in px (rail = tab + 2 * margin)
PANEL_PIN_SIZE = 20  #: pin button size in px (icon-only, no large rectangle)
PANEL_PIN_ICON_SIZE = 16  #: pin icon visual size in px
PANEL_HEADER_FONT_SIZE_PT = _PANEL_TITLE_FONT_PT  #: readable 10 pt title font (at 100%)

#: -- Floating-shelf palette (light industrial, shelf-specific) --------------
#: Fixed hex values (deliberately NOT theme tokens): the shelf keeps its
#: light-instrument look in every theme while the global application
#: accent/theme stays untouched. Normal weight dark text on a very light
#: neutral tab; hover slightly darker; pressed a muted steel-blue.
_SHELF_TAB_BG = "#EFF1F4"  #: minimized tab background (very light neutral grey)
_SHELF_TAB_BG_HOVER = "#DFE4EA"  #: hovered tab background (slightly darker)
_SHELF_TAB_BG_PRESSED = "#607D8B"  #: pressed tab background (muted steel-blue)
_SHELF_TAB_BORDER = "#BCC4CC"  #: tab border (subtle medium-light grey)
_SHELF_TAB_TEXT = "#23272C"  #: tab text (dark charcoal, normal weight)
_SHELF_TAB_TEXT_PRESSED = "#FFFFFF"  #: pressed tab text (light on steel-blue)
_SHELF_TAB_RADIUS = 4  #: tab corner radius in px (industrial, not pill-shaped)
_PIN_GREY = "#6E777F"  #: unpinned pin outline/needle (neutral grey)
_PIN_GREY_FILL = "#C3CAD1"  #: unpinned pin head fill (light grey)
_PIN_STEEL = "#546E7A"  #: pinned pin head/needle (muted industrial steel-blue)
_PIN_STEEL_DARK = "#37474F"  #: pinned pin outline (dark steel)




_UNIT_SYMBOLS = {
    "celsius": "°C",
    "fahrenheit": "°F",
    "kelvin": "K",
}


def _make_pin_icon(pinned: bool):  # -> QIcon (import-deferred for headless tests)
    """Small industrial push-pin icon, drawn programmatically.

    Cached per state. Unpinned = light-grey head with a neutral grey
    outline/needle (subtle); pinned = muted steel-blue head/needle with a
    dark steel outline and white highlight (clearly distinct). Drawn on a
    transparent 16x16 pixmap and shown at 16 px inside a flat icon-only
    button (no large square): the state difference comes from the icon
    itself, never from a button background.
    """
    from PyQt6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap

    cache = _make_pin_icon.__dict__.setdefault("cache", {})
    if pinned in cache:
        return cache[pinned]
    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if pinned:
            head_fill = QColor(_PIN_STEEL)
            outline = QColor(_PIN_STEEL_DARK)
            needle = QColor(_PIN_STEEL_DARK)
            dot = QColor(0xFF, 0xFF, 0xFF)
        else:
            head_fill = QColor(_PIN_GREY_FILL)
            outline = QColor(_PIN_GREY)
            needle = QColor(_PIN_GREY)
            dot = QColor(0xFF, 0xFF, 0xFF)
        # Pin head: bold industrial ellipse with a visible outline.
        painter.setPen(QPen(outline, 1.8))
        painter.setBrush(QBrush(head_fill))
        painter.drawEllipse(2, 1, 12, 8)
        # Needle: thick tilted shaft below the head.
        painter.setPen(QPen(needle, 2.2))
        painter.drawLine(9, 8, 5, 15)
        # Highlight dot on the head.
        painter.setPen(QPen(outline, 1.0))
        painter.setBrush(QBrush(dot))
        painter.drawEllipse(5, 2, 5, 5)
    finally:
        painter.end()
    icon = QIcon(pixmap)
    cache[pinned] = icon
    return icon


class _ShelfTab(QPushButton):
    """One floating minimized-tab on a side shelf (~32 px thick).

    Checkable: checked mirrors its panel's open state (compat mirror only
    — the source of truth is ``record.is_open()`` + ``record.pinned``,
    synced through ``_sync_panel_visual_state``). The FULL panel name is
    drawn as ONE rotated text string over a custom light-industrial
    floating tab (very light neutral background, subtle grey border,
    4 px corners, dark charcoal 12 pt text) — never abbreviated, never
    elided, never per-character stacked. The same canonical title renders
    in every state; only background/border/text-color change with
    hover/pressed. While its panel is OPEN the tab is physically hidden
    (``setVisible(False)``); the open panel's header carries the title.
    No custom docking framework.

    Sizing is driven by the PANEL_* module constants above. Tab height
    (vertical room for the rotated title) is measured from the actual
    rendered font advance + padding with the SAME font used for painting;
    tab thickness stays fixed so long names consume VERTICAL space, never
    horizontal. The MIN/MAX clamps are a clickability floor (60 px) and
    an extreme-scale ceiling (400 px) only — natural titles size
    dynamically between them.
    """

    #: End margin (px) on each side of the rotated title inside the tab.
    #: Height reservation = advance + 2 * _TEXT_MARGIN (~16 px padding).
    _TEXT_MARGIN = 8

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tab_title = title
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(title)
        # Styled centrally (QPushButton[shelfTab="true"]): show()/repolish
        # resets explicitly-set widget fonts to the stylesheet value, so
        # the type size lives in the QSS rule, not in setFont().
        self.setProperty("shelfTab", True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedWidth(PANEL_TAB_WIDTH)
        self.refresh_metrics()

    @property
    def panel_name(self) -> str:
        """Canonical full panel name (identical in every visual state)."""
        return self._tab_title

    @property
    def full_name(self) -> str:
        """Alias for :attr:`panel_name` (test-facing canonical title)."""
        return self._tab_title

    @property
    def displayed_text(self) -> str:
        """Text source used by paintEvent (always the canonical title)."""
        return self._tab_title

    @staticmethod
    def _tab_font() -> "QFont":
        """The rendered tab font, independent of widget polish state.

        Pixel-size based so it matches the central
        ``QPushButton[shelfTab="true"]`` QSS rule
        (``scaled_font_px(SHELF_TAB_FONT_PT, scale)``): measuring in pt
        while painting in px used to under/over-reserve height depending
        on theme state. Weight is always normal — the selected state is
        carried by background/border/text-color, never by bold (bold
        would change the advance after selection and clip the tail).
        """
        from PyQt6.QtGui import QFont
        from PyQt6.QtWidgets import QApplication

        from thermal_monitor.ui.theme.fonts import (
            current_font_scale,
            scaled_font_px,
        )

        try:
            font = QFont(QApplication.font())
        except Exception:
            font = QFont()
        try:
            font.setPixelSize(
                scaled_font_px(PANEL_TAB_FONT_SIZE_PT, current_font_scale())
            )
        except Exception:
            font.setPixelSize(-1)
            from thermal_monitor.ui.theme.fonts import scaled_point_size

            font.setPointSize(
                scaled_point_size(PANEL_TAB_FONT_SIZE_PT, current_font_scale())
            )
        try:
            font.setBold(False)
            font.setWeight(QFont.Weight.Normal)
        except Exception:
            pass
        return font

    def refresh_metrics(self) -> None:
        """Recompute tab geometry from the global font scale.

        Measured with a deterministically constructed pixel font (never
        the widget's own font: show()/repolish resets that to the
        stylesheet value at unpredictable times, which used to produce
        wrong tab heights depending on test order / theme state) — the
        SAME font ``paintEvent`` renders with. Reservation = advance +
        2 * _TEXT_MARGIN (~16 px padding), floored at PANEL_TAB_MIN_HEIGHT
        so short titles stay clickable and capped at PANEL_TAB_MAX_HEIGHT
        only for extreme scales; long titles (e.g. "Configuration Editor")
        always fit completely.

        Re-asserts the FULL fixed geometry (width + height) every time:
        a stylesheet (un)polish — e.g. via ``set_variant`` repolish or a
        theme switch — rewrites the widget's minimum sizes from the QSS
        ``min-height: 0px`` rule and silently breaks the fixed height set
        here, collapsing the tab to its ~6 px empty-text size hint. This
        method must therefore run AFTER the last polish on every visual
        transition (see ``_sync_shelf_tab`` and ``changeEvent``).
        """
        from PyQt6.QtGui import QFontMetrics

        self.setFixedWidth(PANEL_TAB_WIDTH)
        try:
            advance = QFontMetrics(self._tab_font()).horizontalAdvance(
                self._tab_title
            )
        except Exception:
            advance = self.fontMetrics().horizontalAdvance(self._tab_title)
        self.setFixedHeight(
            min(
                PANEL_TAB_MAX_HEIGHT,
                max(PANEL_TAB_MIN_HEIGHT, advance + 2 * self._TEXT_MARGIN),
            )
        )

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        """Re-assert the fixed geometry on show.

        Showing a widget while a QSS theme is active clamps its explicit
        minimum sizes down to the style minimum (verified empirically);
        re-applying here keeps the floating tab geometry exact. Idempotent.
        """
        try:
            self.refresh_metrics()
        except Exception:
            pass
        super().showEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        """Re-assert the fixed geometry after stylesheet repolishes.

        A theme switch (un)polishes every widget, which rewrites this
        tab's minimum sizes from the QSS ``min-height`` rule and would
        otherwise collapse it to its ~6 px empty-text size hint until the
        next panel transition. Re-applying on StyleChange keeps the
        minimized tab pixel-identical across theme switches. Idempotent;
        never touches the canonical title.
        """
        super().changeEvent(event)
        try:
            if event is not None and event.type() == QEvent.Type.StyleChange:
                self.refresh_metrics()
        except Exception:
            pass

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        """Repaint for the hover state (floating-tab hover tint)."""
        try:
            self.update()
        except Exception:
            pass
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        """Repaint back to the resting floating-tab look."""
        try:
            self.update()
        except Exception:
            pass
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        from PyQt6.QtGui import QColor, QPainter

        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            # Floating light-industrial tab: own visual language, never a
            # button bevel inside a dark rail. State only changes
            # background/border/text-color — never the title string.
            if self.isDown():
                background = QColor(_SHELF_TAB_BG_PRESSED)
                border = QColor(_SHELF_TAB_BG_PRESSED)
                text_color = QColor(_SHELF_TAB_TEXT_PRESSED)
            elif self.underMouse():
                background = QColor(_SHELF_TAB_BG_HOVER)
                border = QColor(_SHELF_TAB_BORDER)
                text_color = QColor(_SHELF_TAB_TEXT)
            else:
                background = QColor(_SHELF_TAB_BG)
                border = QColor(_SHELF_TAB_BORDER)
                text_color = QColor(_SHELF_TAB_TEXT)
            rect = self.rect().adjusted(1, 1, -1, -1)
            painter.setPen(border)
            painter.setBrush(background)
            painter.drawRoundedRect(
                rect, _SHELF_TAB_RADIUS, _SHELF_TAB_RADIUS
            )
            # ONE rotated string (never per-character, never elided): the
            # canonical title renders identically in every shelf state.
            painter.save()
            try:
                painter.setPen(text_color)
                painter.setFont(self._tab_font())
                # Bottom-to-top vertical text, centered on the tab: after
                # the transform, +x runs up the widget and +y runs across.
                painter.translate(0, self.height())
                painter.rotate(-90)
                margin = self._TEXT_MARGIN
                painter.drawText(
                    margin,
                    0,
                    max(0, self.height() - 2 * margin),
                    PANEL_TAB_WIDTH,
                    Qt.AlignmentFlag.AlignHCenter
                    | Qt.AlignmentFlag.AlignVCenter,
                    self.displayed_text,
                )
            finally:
                painter.restore()
        finally:
            painter.end()


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
    # PTZ marshaling signals (emitted from PTZ daemon threads, consumed on
    # the GUI thread; every payload carries (camera_id, generation) so
    # stale deliveries can never touch the wrong camera panel).
    _ptz_status = pyqtSignal(str, int, str, object)  # camera_id, gen, ptz_id, PtzStatus
    _ptz_operation = pyqtSignal(str, int, object)  # camera_id, gen, PtzOperation
    _ptz_positions = pyqtSignal(str, int, object)  # camera_id, gen, list[PtzPosition]
    _ptz_notice = pyqtSignal(str, int, str)  # camera_id, gen, message
    # REPLACE-import confirmation: (camera_id, gen, payload dict with
    # "entries" list + threading.Event "done" + dict "answer").
    _ptz_import_confirm = pyqtSignal(str, int, object)
    # Active-position label: (camera_id, gen, position name or "").
    _ptz_active = pyqtSignal(str, int, str)
    # Phase 10 ROI session: (camera_id, gen, payload dict with
    # position_id/ptz_id/position_name/context_generation/operation_id/roi_dicts).
    _roi_session_loaded = pyqtSignal(str, int, object)
    # Phase 10 ROI counts: (camera_id, gen, {position_id: count}).
    _roi_counts = pyqtSignal(str, int, object)
    # Phase 11 movement lock release: (camera_id, gen). Emitted when a Go
    # To fails or is cancelled so the previous session becomes editable.
    _roi_session_unlock = pyqtSignal(str, int)

    def __init__(
        self,
        config_service: ConfigurationService,
        mode_service: ModeService,
        runtime_service: CameraRuntimeService | None = None,
        discovery_service: "CameraDiscoveryService | GvcpDiscoveryService | None" = None,
        theme_manager: Optional[ThemeManager] = None,
        config_manager: Optional[ConfigurationManager] = None,
        database=None,
    ) -> None:
        super().__init__()

        self._config_service = config_service
        self._mode_service = mode_service
        self._runtime_service = runtime_service
        self._discovery_service = discovery_service
        self._theme = theme_manager
        self._config_manager = config_manager
        self._database = database
        # PTZ integration (Phase 6): services keyed by OPC UA endpoint so
        # one session is shared per endpoint; panels always follow the
        # selected camera via binding + generation guards. Daemon threads
        # are retained until completion; results return via _ptz_* signals.
        self._ptz_services: dict[str, object] = {}
        self._ptz_threads: set = set()
        self._ptz_panel = None
        self._pos_panel = None
        # Phase 8 retarget state: one shared active-position registry,
        # one coordinator per endpoint, latest-wins per camera.
        from thermal_monitor.ptz.roi_activation import ActivePositionRegistry

        self._ptz_registry = ActivePositionRegistry()
        self._ptz_coordinators: dict[str, object] = {}
        # Phase 9B top-status + alarm state: compact operator-visible
        # snapshots, never fabricated (unknown renders as "—").
        self._ptz_top_state: str = "Camera disconnected"
        self._alarm_active_count: int = 0
        self._active_alarm_events: dict[str, object] = {}
        self._alarm_store = None  # lazy AlarmHistoryStore (worker owns DB I/O)
        self._alarm_detail_window = None
        self._alarm_history_window = None
        self._top_ptz_label = None
        self._top_alarm_label = None
        self._top_db_label = None
        self._selected_camera_id: str | None = None
        self._observer: ObserverService | None = None
        self._latest_result: ProcessingResult | None = None
        self._display_rate = UniqueFrameRate()
        self._config_editor: Optional[ConfigurationEditor] = None
        self._camera_selection_dialog: CameraSelectionDialog | None = None
        self._acq_setup_dialog: AcquisitionSetupDialog | None = None
        # Startup acquisition parameters (FPS / averaging / history /
        # IR display sampling). Owned by the Acquisition Setup dialog;
        # initialized from the selected camera's metadata and written back
        # on dialog Start. The Start pipeline reads them from here (never
        # from panel widgets).
        self._acq_params: dict = {
            "fps": 9,
            "averaging": "Off",
            "history_frames": 100,
            "ir_scaling": "fast",
        }
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
        # Retiring control threads: non-blocking detach reparents a
        # worker thread OFF the (possibly dying) window so teardown can
        # never delete a running QThread (process abort). Unparenting
        # alone is not enough — the Python wrapper refcount would drop
        # to zero at return and SIP would delete the running C++ object
        # immediately. This set keeps one Python reference per retiring
        # thread until its finished signal fires, which chains the
        # deleteLater and releases the reference.
        self._retiring_threads: set = set()

        # Dirty state tracking for camera-specific configurations
        self._dirty_camera_configs: set[str] = set()
        self._pending_camera_switch: str | None = None
        # Side-shelf auto-hide: re-entrancy guard for the click-outside
        # handler plus a one-shot flag for the menu-bar filter install
        # (the window only exists once shown; see showEvent).
        self._in_autohide = False
        self._menu_filter_installed = False

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

        # --- Slim top action bar (no camera selector: selection lives in
        # Camera Control). Connection status + global actions only.
        self._top_bar = self._build_top_bar()
        main_layout.addWidget(self._top_bar)

        # --- Workstation row: thin shelf | panels | CENTER | panels | thin shelf
        # Plain widgets only. Side panels live inside their shelf container
        # and can never float, drag, tabify or cover the center: hiding a
        # panel hides its widget (never destroyed), and the camera
        # workspace expands to use the freed space.
        work_row = QHBoxLayout()
        work_row.setContentsMargins(0, 0, 0, 0)
        work_row.setSpacing(0)
        main_layout.addLayout(work_row, 1)

        self._left_rail, self._left_rail_layout = self._build_shelf_rail("left")
        self._left_rail.setObjectName("cfg_left_shelf")
        work_row.addWidget(self._left_rail)
        self._side_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._side_splitter.setChildrenCollapsible(False)
        self._side_splitter.setObjectName("cfg_side_splitter")
        work_row.addWidget(self._side_splitter, 1)
        self._right_rail, self._right_rail_layout = self._build_shelf_rail("right")
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

        # LEFT: Image Acquisition panel (instrument panel, owns camera
        # selection + feed display mode + Connect/Start/Stop/Focus/NUC).
        self._acq_panel = ImageAcquisitionPanel(self._theme)
        self._acq_panel.feed_mode_changed.connect(self.set_irvl_mode)
        self._acq_panel.connect_requested.connect(self._on_connect)
        self._acq_panel.disconnect_requested.connect(self._on_disconnect)
        self._acq_panel.start_requested.connect(self._on_start_acquisition)
        self._acq_panel.stop_requested.connect(self._on_stop_acquisition)
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

        # LEFT: PTZ Control panel (Phase 6). Service-agnostic surface;
        # this widget drives PtzService on background threads and feeds
        # snapshots back via _ptz_* signals (generation-guarded).
        from thermal_monitor.ui.widgets.ptz_control_panel import PtzControlPanel

        self._ptz_panel = PtzControlPanel(self._theme)
        self._ptz_panel.move_requested.connect(self._on_ptz_move_requested)
        self._ptz_panel.relative_requested.connect(self._on_ptz_relative_requested)
        self._ptz_panel.stop_requested.connect(self._on_ptz_stop_requested)
        self._ptz_panel.clear_error_requested.connect(
            self._on_ptz_clear_error_requested
        )
        self._ptz_panel.calibration_requested.connect(
            self._on_ptz_calibration_requested
        )
        self._register_side_panel(
            "ptz_control", "PTZ Control", self._ptz_panel, "left"
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
        # Phase 10: ROI/analysis toolbar INSIDE the Camera Region (above
        # the image, not the application top bar). Rendering, pan/zoom,
        # IR/VL layout, and the latest-frame-wins path are untouched.
        from thermal_monitor.ui.widgets.roi_toolbar import RoiToolbar

        self._roi_toolbar = RoiToolbar()
        self._roi_toolbar.setObjectName("cfg_roi_toolbar")
        self._roi_toolbar.tool_changed.connect(self._on_roi_tool_changed)
        self._roi_toolbar.delete_requested.connect(self._on_roi_toolbar_delete)
        center_layout.insertWidget(0, self._roi_toolbar)
        # Phase 10 canvas: full drawing/selection/drag/resize lifecycle on
        # the existing image widget (rendering and pan/zoom untouched).
        # The session (RoiEditor) is installed after a reached position
        # loads its ROI set; without one all tools stay disabled.
        from thermal_monitor.ui.widgets.roi_canvas import RoiCanvasController

        self._roi_canvas = RoiCanvasController(
            self._roi_mapping, toolbar=self._roi_toolbar, editable=True)
        self._roi_canvas.on_created = self._on_roi_canvas_created
        self._roi_canvas.on_changed = self._on_roi_canvas_changed
        self._roi_canvas.on_deleted = self._on_roi_canvas_deleted
        self._roi_canvas.on_selected = self._on_roi_canvas_selected
        self._roi_canvas.on_repaint = self._repaint_roi_session
        self._roi_canvas.on_error = self._on_roi_canvas_error
        self._image_widget.installEventFilter(self)
        self._roi_session_gens: dict[str, int] = {}
        self._roi_session_active = False
        # Clean center: camera feed only. Feed mode lives in Camera
        # Control and zoom commands live in the View menu — no workspace
        # toolbar above the image.
        # Center goes between the side containers; side-panel widths stay
        # user-resizable (controlled) via this splitter only.
        self._side_splitter.insertWidget(1, center_widget)
        self._side_splitter.addWidget(self._right_container)
        self._side_splitter.setStretchFactor(0, 0)
        self._side_splitter.setStretchFactor(1, 1)
        self._side_splitter.setStretchFactor(2, 0)
        # Center presses funnel into the click-outside auto-hide handler
        # (filters installed on all outside areas in
        # _install_outside_press_filters at the end of setup).

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
        self._alarm_panel.alarm_activated.connect(self._on_alarm_activated)
        self._alarm_panel.history_requested.connect(self._on_alarm_history_requested)
        self._register_side_panel(
            "alarms", "Alarms", self._alarm_panel, "right"
        )

        # Statistics panel
        self._stats_panel = StatisticsPanel(self._theme)
        self._register_side_panel(
            "statistics", "Statistics", self._stats_panel, "right"
        )

        # RIGHT: Position Table panel (Phase 6). Renders persisted
        # PtzPosition rows; CRUD/GoTo run on background threads.
        from thermal_monitor.ui.widgets.ptz_position_table import (
            PtzPositionTablePanel,
        )

        self._pos_panel = PtzPositionTablePanel(self._theme)
        self._pos_panel.goto_requested.connect(self._on_ptz_goto_requested)
        self._pos_panel.save_current_requested.connect(
            self._on_ptz_save_current_requested
        )
        self._pos_panel.delete_requested.connect(self._on_ptz_delete_requested)
        self._pos_panel.rename_requested.connect(self._on_ptz_rename_requested)
        self._pos_panel.edit_rois_requested.connect(
            self._on_ptz_edit_rois_requested
        )
        self._pos_panel.refresh_requested.connect(self._on_ptz_refresh_requested)
        self._pos_panel.export_requested.connect(self._on_ptz_export_requested)
        self._pos_panel.import_requested.connect(self._on_ptz_import_requested)
        self._register_side_panel(
            "ptz_positions", "Position Table", self._pos_panel, "right"
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
        # Apply the persisted global font scale to shelf/header metrics.
        self.refresh_font_metrics()
        # Click-outside auto-hide: watch every panel-free outside area
        # (center workspace, top bar, both shelf edges). Presses there
        # minimize open unpinned panels; presses inside panels never reach
        # these filters, so no position math or propagation tracking is
        # needed. The menu bar is covered once the window exists
        # (see showEvent). Filters never consume events: the clicked
        # control still receives them (no flicker, no timers).
        self._install_outside_press_filters()

        # --- Bottom: Status bar ---
        self._create_status_bar(main_layout)

    def _build_top_bar(self) -> QWidget:
        """Slim status/action strip: Camera/PTZ/Alarm/DB status + Snapshot.

        Phase 9B: the Save Config button was removed from this toolbar
        (configuration saving stays available via File -> Save
        Configuration, which calls the same ``_on_save_config`` handler).
        The freed space carries a compact system status area in the
        existing visual language (no extra colors, no redesign).
        """
        bar = QWidget()
        bar.setObjectName("cfg_top_bar")
        set_role(bar, "toolbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)
        self._top_conn_indicator = QLabel("●")
        self._top_conn_indicator.setFixedWidth(16)
        set_status(self._top_conn_indicator, "disconnected")
        self._top_conn_label = QLabel("Disconnected")
        set_role(self._top_conn_label, "strong")
        layout.addWidget(self._top_conn_indicator)
        layout.addWidget(self._top_conn_label)
        self._top_ptz_label = QLabel("PTZ: —")
        set_role(self._top_ptz_label, "status")
        layout.addWidget(self._top_ptz_label)
        self._top_alarm_label = QLabel("Alarms: 0")
        set_role(self._top_alarm_label, "status")
        layout.addWidget(self._top_alarm_label)
        self._top_db_label = QLabel("DB: —")
        set_role(self._top_db_label, "status")
        layout.addWidget(self._top_db_label)
        layout.addStretch()
        self._snapshot_btn = QPushButton("Snapshot")
        self._snapshot_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        set_variant(self._snapshot_btn, "outline")
        self._snapshot_btn.clicked.connect(self._on_snapshot)
        layout.addWidget(self._snapshot_btn)
        return bar

    def _set_top_connection_state(self, state: CameraConnectionState) -> None:
        """Mirror the lifecycle state onto the slim top bar."""
        status = _TOP_CONNECTION_STATUS_MAP.get(state, "disconnected")
        try:
            set_status(self._top_conn_indicator, status)
            self._top_conn_label.setText(state.value.replace("_", " ").title())
            set_status(self._top_conn_label, status)
        except RuntimeError:
            pass
        self._refresh_top_status()

    def _set_ptz_top_state(self, text: str) -> None:
        """Update the compact PTZ status chip (Phase 9B)."""
        self._ptz_top_state = text
        self._refresh_top_status()

    def _set_alarm_top_count(self, count: int) -> None:
        """Update the compact alarm count chip (Phase 9B)."""
        self._alarm_active_count = max(0, int(count))
        self._refresh_top_status()

    def _refresh_top_status(self) -> None:
        """Render PTZ/Alarm/DB chips from current snapshots (no I/O)."""
        try:
            if self._top_ptz_label is not None:
                self._top_ptz_label.setText(f"PTZ: {self._ptz_top_state}")
            if self._top_alarm_label is not None:
                self._top_alarm_label.setText(f"Alarms: {self._alarm_active_count}")
            if self._top_db_label is not None:
                self._top_db_label.setText(f"DB: {self._db_top_text()}")
        except RuntimeError:
            pass  # teardown race; labels already gone

    def _db_top_text(self) -> str:
        """Database chip text from real state only (never fabricated)."""
        db = self._database
        if db is None:
            return "Off"
        try:
            status = db.status  # SqliteDatabase: (state, detail)
        except AttributeError:
            try:
                return "On" if db.is_connected else "Off"
            except Exception:
                return "—"
        except Exception:
            return "—"
        state = status[0] if isinstance(status, tuple) else str(status)
        if state == "connected":
            return "SQLite"
        if state == "error":
            return "Error"
        return "—"

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

    def _build_shelf_rail(self, tag: str = "") -> tuple[QWidget, QVBoxLayout]:
        """Transparent shelf edge hosting floating minimized tabs.

        The shelf is NOT a solid rectangular rail: it is a transparent
        strip on the window edge from which individual floating tabs
        (PANEL_TAB_WIDTH px thick, spaced PANEL_TAB_SPACING px apart)
        protrude. Sizing comes from the PANEL_* module constants.

        The TAB LIST scrolls inside an outer QScrollArea (vertical only,
        as-needed): when many panels exist the shelf scrolls instead of
        compressing tabs until their names become unreadable. Scrolling
        never alters tab content — the canonical title paints identically
        in every state. The returned layout is the scrollable host layout
        (tabs insert before its trailing stretch), not the rail layout.
        """
        from PyQt6.QtWidgets import QScrollArea

        rail = QWidget()
        rail.setFixedWidth(PANEL_SHELF_WIDTH)
        rail.setProperty("shelfRail", True)
        outer = QVBoxLayout(rail)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea(rail)
        scroll.setObjectName(
            f"cfg_{tag}_shelf_scroll" if tag else "cfg_shelf_scroll"
        )
        scroll.setProperty("shelfScroll", True)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        host = QWidget()
        host.setObjectName(f"cfg_{tag}_shelf_host" if tag else "cfg_shelf_host")
        host.setMinimumWidth(PANEL_TAB_WIDTH + 2 * PANEL_TAB_MARGIN)
        host_layout = QVBoxLayout(host)
        host_layout.setContentsMargins(
            PANEL_TAB_MARGIN, PANEL_TAB_MARGIN, PANEL_TAB_MARGIN, PANEL_TAB_MARGIN
        )
        host_layout.setSpacing(PANEL_TAB_SPACING)
        host_layout.addStretch()
        scroll.setWidget(host)
        outer.addWidget(scroll, 1)
        return rail, host_layout

    def _build_side_container(self) -> QWidget:
        """Host for one side's open panels, stacked vertically from the top.

        The container scrolls as a whole when too many panels are open,
        while each panel ALSO scrolls its own content (header fixed).
        Width is user-resizable via the side splitter only, within
        controlled limits (spec: side panels may resize in width but
        can never detach, float, or move outside their shelf).
        """
        from PyQt6.QtWidgets import QScrollArea

        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea(container)
        scroll.setObjectName("cfg_side_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        host = QWidget()
        host.setObjectName("cfg_side_host")
        host_layout = QVBoxLayout(host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(2)
        host_layout.addStretch()
        scroll.setWidget(host)
        outer.addWidget(scroll, 1)
        container.setVisible(False)
        container.setMinimumWidth(220)
        container.setMaximumWidth(460)
        # Pinned for the panel registrar: panels stack inside the host.
        container._panel_host = host  # type: ignore[attr-defined]
        return container

    def _side_host(self, side: str) -> QWidget:
        """Inner stacked host inside a side container's scroll area."""
        container = self._left_container if side == "left" else self._right_container
        host = getattr(container, "_panel_host", None)
        return host if host is not None else container

    def _register_side_panel(
        self, key: str, title: str, content: QWidget, side: str
    ) -> "_SidePanel":
        """Dock a panel widget inside its side shelf (never floating).

        The content widget is reparented into the wrapper (never copied
        or recreated): collapsing/expanding preserves all panel state.
        There is deliberately no close button, no float, no drag handle —
        visibility is driven by the shelf tab + pin only.
        """
        from PyQt6.QtWidgets import QScrollArea

        record = _SidePanel(key, title, content, side)
        wrapper = QFrame()
        wrapper.setObjectName(f"cfg_panel_{key}")
        wrapper.setMinimumWidth(200)
        # Vertically Preferred: multiple open panels stack at their natural
        # heights (the host's trailing stretch absorbs the rest), while a
        # lone open panel is forced to fill via host stretch factor 1
        # (see _rebalance_side_host).
        wrapper.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(0)
        # Fixed header: never scrolls. Only the content below scrolls.
        # ThermoView-style: title left, one small pin right (only control).
        # The muted blue-grey background comes from the central
        # QWidget[panelHeader="true"] rule (theme surface_alt token).
        header = QWidget()
        header.setObjectName(f"cfg_panel_header_{key}")
        header.setProperty("panelHeader", True)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 4, 4, 4)
        header_layout.setSpacing(4)
        title_label = QLabel(title)
        title_label.setObjectName(f"cfg_panel_title_{key}")
        title_label.setProperty("panelTitle", True)
        title_label.setWordWrap(False)
        title_font = title_label.font()
        from thermal_monitor.ui.theme.fonts import (
            current_font_scale as _current_scale,
        )
        from thermal_monitor.ui.theme.fonts import (
            scaled_point_size as _scaled_pt,
        )

        title_font.setPixelSize(-1)  # see _ShelfTab.refresh_metrics
        title_font.setPointSize(
            _scaled_pt(PANEL_HEADER_FONT_SIZE_PT, _current_scale())
        )
        title_font.setBold(True)
        title_label.setFont(title_font)
        header_layout.addWidget(title_label, 1)
        pin = QPushButton()
        pin.setObjectName(f"cfg_pin_{key}")
        pin.setFixedSize(PANEL_PIN_SIZE, PANEL_PIN_SIZE)
        pin.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        pin.setFlat(True)  # icon only: no large square background
        pin.setProperty("pinButton", True)  # central icon-only pin styling
        from PyQt6.QtCore import QSize as _QSize

        pin.setIconSize(_QSize(PANEL_PIN_ICON_SIZE, PANEL_PIN_ICON_SIZE))
        pin.setToolTip("Pin panel")
        pin.clicked.connect(
            lambda _checked=False, panel_key=key: self._toggle_pin(panel_key)
        )
        header_layout.addWidget(pin)
        wrapper_layout.addWidget(header)
        # Scrollable content: vertical scroll when controls exceed height,
        # never horizontal; header above stays fixed.
        scroll = QScrollArea(wrapper)
        scroll.setObjectName(f"cfg_panel_scroll_{key}")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content.setMinimumWidth(200)
        content.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        scroll.setWidget(content)
        scroll.setMinimumHeight(80)
        # Wheel-lock: scrolling over an unfocused spin/combo scrolls the
        # panel instead of silently rewriting the value (see wheel_guard).
        from thermal_monitor.ui.widgets.wheel_guard import install_wheel_guards

        install_wheel_guards(content)
        wrapper_layout.addWidget(scroll, 1)
        wrapper.setVisible(False)
        record.wrapper = wrapper
        record.pin_button = pin
        host = self._side_host(side)
        host.layout().insertWidget(host.layout().count() - 1, wrapper)
        rail_layout = self._left_rail_layout if side == "left" else self._right_rail_layout
        tab = _ShelfTab(title)
        tab.setObjectName(f"cfg_shelf_tab_{key}")
        tab.clicked.connect(
            lambda _checked=False, panel_key=key: self._on_shelf_tab(panel_key)
        )
        # Centered in the scrollable host: when the vertical scrollbar is
        # hidden the tab sits symmetric in the rail; when it shows, the
        # symmetric 2 px overflow clips only tab border, never title text
        # (text stays centered in the 32 px tab with ~10 px margins).
        rail_layout.insertWidget(
            rail_layout.count() - 1, tab, 0, Qt.AlignmentFlag.AlignHCenter
        )
        record.tab = tab
        self._side_panels[key] = record
        self._sync_panel_visual_state(record)
        return record

    def side_panels(self) -> dict[str, "_SidePanel"]:
        """All side-shelf panels by key (workstation layout)."""
        return dict(self._side_panels)

    def open_panel(self, key: str) -> None:
        """Open a panel, preserving its pinned state and widget state."""
        self.set_panel_open(key, True)

    def set_panel_open(self, key: str, open: bool, *, persist: bool = True) -> None:
        """Show/hide a panel without destroying its widget or state.

        Opening hides that panel's shelf tab (the open panel's header
        carries the full title); closing returns the tab to the shelf.
        Other panels are untouched, so View-menu/programmatic opens can
        stack multiple panels; shelf-click exclusivity lives in
        :meth:`_on_shelf_tab`.
        """
        record = self._side_panels.get(key)
        if record is None or record.wrapper is None:
            return
        try:
            record.wrapper.setVisible(bool(open))
        except RuntimeError:
            return
        self._sync_panel_visual_state(record)
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
        """Shelf tab click opens that panel (pin state untouched).

        Opening via the shelf minimizes every other open UNPINNED panel
        first (its tab returns to the shelf), so the newly opened panel
        replaces it; PINNED panels stay open, allowing several pinned
        panels to coexist. Closing an open panel (programmatic toggle)
        returns its tab to the shelf.
        """
        record = self._side_panels.get(key)
        if record is None:
            return
        if record.is_open():
            self.set_panel_open(key, False)
            return
        for other in self._side_panels.values():
            if (
                other.key != key
                and other.wrapper is not None
                and other.is_open()
                and not other.pinned
            ):
                self.set_panel_open(other.key, False, persist=False)
        self.set_panel_open(key, True)

    def _sync_panel_visual_state(self, record: "_SidePanel") -> None:
        """Authoritative shelf/panel/pin synchronization for one panel.

        ``record.is_open()`` + ``record.pinned`` determine everything:

        - open: shelf tab physically hidden, wrapper shown (pinned panels
          stay open; unpinned panels are auto-hide eligible).
        - minimized: shelf tab visible with the full name, wrapper hidden.
        - pin icon + tooltip reflect ``pinned``; the View-menu action
          mirrors ``is_open()``.

        The canonical title string is never touched here.
        """
        self._sync_shelf_tab(record)
        self._sync_pin_button(record)
        try:
            action = self._panel_view_actions.get(record.key)
        except AttributeError:
            action = None
        if action is not None:
            try:
                action.setChecked(bool(record.is_open()))
            except RuntimeError:
                pass

    def _sync_shelf_tab(self, record: "_SidePanel") -> None:
        """Reflect panel visibility on its shelf tab (never the title).

        An OPEN panel's tab is physically hidden (``setVisible(False)``)
        — the open panel itself represents it. A MINIMIZED panel's tab is
        shown with its full fixed floating-tab geometry explicitly
        restored: ``set_variant`` repolishes the widget, and that polish
        rewrites the tab's minimum sizes from the QSS ``min-height`` rule
        (verified: min-height 186 -> 6 px), so ``refresh_metrics`` must run
        AFTER it — otherwise a tab returning to the shelf collapses to a
        small horizontal box instead of its vertical floating-tab shape.
        The checked flag is a compat mirror only; the canonical tab title
        string is never touched, so minimized/open/pinned all carry the
        identical full name.
        """
        if record.tab is None:
            return
        is_open = bool(record.is_open())
        try:
            record.tab.setVisible(not is_open)
        except RuntimeError:
            return
        try:
            if record.tab.isChecked() != is_open:
                record.tab.setChecked(is_open)
        except RuntimeError:
            pass
        try:
            set_variant(record.tab, "accent" if is_open else "outline")
        except RuntimeError:
            pass
        # LAST write wins: re-assert the fixed floating-tab geometry after
        # the repolish above (and after showEvent's own refresh, which runs
        # before this when the tab becomes visible).
        try:
            record.tab.refresh_metrics()
        except (RuntimeError, AttributeError):
            pass

    def _sync_pin_button(self, record: "_SidePanel") -> None:
        """Reflect pinned state on the panel header pin control.

        Real drawn icons (never bare Unicode): muted steel-blue pin when
        pinned, neutral grey outline pin when unpinned — immediately
        distinct at PANEL_PIN_ICON_SIZE px with icon + tooltip. The
        button itself stays a flat icon-only control (no large square):
        state comes from the icon, never from a button background.
        """
        if record.pin_button is None:
            return
        try:
            record.pin_button.setText("")
            record.pin_button.setIcon(_make_pin_icon(bool(record.pinned)))
            record.pin_button.setToolTip(
                "Unpin panel" if record.pinned else "Pin panel"
            )
        except RuntimeError:
            pass

    def _update_side_container(self, side: str) -> None:
        """Show a side container iff at least one of its panels is open."""
        container = self._left_container if side == "left" else self._right_container
        any_open = any(
            record.is_open()
            for record in self._side_panels.values()
            if record.side == side
        )
        container.setVisible(any_open)
        self._rebalance_side_host(side)

    def refresh_font_metrics(self) -> None:
        """Recompute shelf/tab/header fonts from the global font scale.

        Called once after setup (so a persisted non-100% scale applies at
        startup) and after every Settings -> Font Size change (via
        ui.theme.fonts). Layouts + scroll areas absorb the growth; no
        fixed heights are introduced here.
        """
        from thermal_monitor.ui.theme.fonts import (
            current_font_scale,
            scaled_point_size,
        )

        scale = current_font_scale()
        header_pt = scaled_point_size(PANEL_HEADER_FONT_SIZE_PT, scale)
        for record in self._side_panels.values():
            try:
                if record.tab is not None:
                    record.tab.refresh_metrics()
            except (RuntimeError, AttributeError):
                continue
            try:
                if record.wrapper is not None:
                    title = record.wrapper.findChild(
                        QLabel, f"cfg_panel_title_{record.key}"
                    )
                    if title is not None:
                        font = title.font()
                        font.setPixelSize(-1)  # see _ShelfTab.refresh_metrics
                        font.setPointSize(header_pt)
                        font.setBold(True)
                        title.setFont(font)
            except (RuntimeError, AttributeError):
                continue

    def _rebalance_side_host(self, side: str) -> None:
        """Give a lone open panel the whole side height.

        Exactly one open panel on a side stretches to occupy essentially
        the entire side-panel area (no wasted empty space); with several
        open panels each keeps its natural height at the top and the side
        container scrolls. Pure layout factors — no state is changed.
        """
        host = self._side_host(side)
        layout = host.layout()
        if layout is None:
            return
        open_wrappers = {
            id(record.wrapper)
            for record in self._side_panels.values()
            if record.side == side and record.is_open()
        }
        lone = len(open_wrappers) == 1
        for record in self._side_panels.values():
            if record.side != side or record.wrapper is None:
                continue
            try:
                layout.setStretchFactor(
                    record.wrapper,
                    1 if (lone and id(record.wrapper) in open_wrappers) else 0,
                )
            except RuntimeError:
                continue

    def _collapse_unpinned(self) -> None:
        """Auto-hide: minimize every open, unpinned panel (state kept).

        Each minimized panel's tab returns to the shelf via the
        authoritative sync path; pinned panels are untouched. Idempotent
        and re-entrancy guarded (the click-outside handler funnels here).
        """
        if getattr(self, "_in_autohide", False):
            return
        self._in_autohide = True
        try:
            changed = False
            for record in self._side_panels.values():
                if (
                    record.wrapper is not None
                    and record.is_open()
                    and not record.pinned
                ):
                    try:
                        record.wrapper.setVisible(False)
                    except RuntimeError:
                        continue
                    self._sync_panel_visual_state(record)
                    changed = True
            if changed:
                self._update_side_container("left")
                self._update_side_container("right")
                self._save_shelf_state()
        finally:
            self._in_autohide = False

    def _install_outside_press_filters(self) -> None:
        """Watch every panel-free outside area for click-outside presses.

        Installs this widget as the event filter on the center workspace,
        the top bar and both shelf edges — recursively, so buttons, image
        widgets and shelf tabs are covered too. None of these subtrees
        contains a panel wrapper, so ANY mouse press reaching them is by
        construction outside every panel: no position math and no
        propagation tracking is needed (presses inside panels live in the
        side containers, which are deliberately NOT watched — an ignored
        press propagating up from panel content must never read as an
        outside click). The event is never consumed.
        """
        roots: list[QWidget] = [
            root
            for root in (
                getattr(self, "_center_widget", None),
                getattr(self, "_top_bar", None),
                getattr(self, "_left_rail", None),
                getattr(self, "_right_rail", None),
            )
            if isinstance(root, QWidget)
        ]
        seen: set[int] = set()
        for root in roots:
            try:
                candidates = [root] + list(root.findChildren(QWidget))
            except RuntimeError:
                continue
            for candidate in candidates:
                if id(candidate) in seen:
                    continue
                seen.add(id(candidate))
                try:
                    candidate.installEventFilter(self)
                except RuntimeError:
                    continue

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        """Cover the menu bar once the top-level window exists."""
        super().showEvent(event)
        if getattr(self, "_menu_filter_installed", False):
            return
        try:
            window = self.window()
            menu_bar = window.menuBar() if hasattr(window, "menuBar") else None
        except RuntimeError:
            return
        if menu_bar is None:
            return
        try:
            menu_bar.installEventFilter(self)
            self._menu_filter_installed = True
        except RuntimeError:
            pass

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt override)
        """Auto-hide open unpinned panels on outside mouse presses.

        Only panel-free outside areas are watched (see
        :meth:`_install_outside_press_filters`), so every press here
        minimizes open unpinned panels while inside presses and pin
        clicks never arrive. Never consumes events and never uses timers
        (no flicker, no recursion: collapsing emits no clicks).

        Phase 10: presses/moves/releases on the thermal image are first
        offered to the ROI canvas; consumed gestures (drawing, selection,
        drag, resize) return True so pan/selection never double-handles
        them, and the auto-hide collapse is skipped for those presses.
        """
        try:
            if event is not None and watched is getattr(self, "_image_widget", None):
                if self._route_roi_event(event):
                    return True
            if event is not None and event.type() == QEvent.Type.MouseButtonPress:
                self._collapse_unpinned()
        except Exception:
            logger.debug("Shelf event-filter failed", exc_info=True)
        return super().eventFilter(watched, event)

    def _route_roi_event(self, event) -> bool:
        """Offer one image-widget event to the ROI canvas (never raises)."""
        try:
            canvas = getattr(self, "_roi_canvas", None)
            if canvas is None or getattr(canvas, "editor", None) is None:
                return False
            etype = event.type()
            if etype == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                pos = event.position()
                return bool(canvas.press(float(pos.x()), float(pos.y())))
            if etype == QEvent.Type.MouseMove:
                pos = event.position()
                return bool(canvas.move(float(pos.x()), float(pos.y())))
            if etype == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                pos = event.position()
                return bool(canvas.release(float(pos.x()), float(pos.y())))
            if etype == QEvent.Type.MouseButtonDblClick:
                pos = event.position()
                return bool(canvas.double_click(float(pos.x()), float(pos.y())))
            if etype == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                return bool(canvas.escape())
        except Exception:
            logger.debug("ROI canvas routing failed", exc_info=True)
        return False

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
        """Restore pins/open/widths/IR-VL/zoom, falling back to defaults.

        Fresh startup (no persisted state) minimizes everything: all
        panels closed, all unpinned, every shelf tab visible. Restored
        state always funnels through the authoritative sync path, so a
        minimized panel never loses its shelf tab and an open panel never
        keeps one.
        """
        try:
            settings = QSettings(_DOCK_SETTINGS_ORG, _DOCK_SETTINGS_APP)
            pins = settings.value("shelf_pins_v1", None)
            opened = settings.value("shelf_open_v1", None)
            if pins is None and opened is None:
                pins, opened = [], []
            if isinstance(pins, str):
                pins = [pins]
            if isinstance(opened, str):
                opened = [opened]
            pins = set(pins or [])
            opened = set(opened or [])
            for key, record in self._side_panels.items():
                record.pinned = key in pins
                if record.wrapper is not None:
                    try:
                        record.wrapper.setVisible(key in opened)
                    except RuntimeError:
                        pass
                self._sync_panel_visual_state(record)
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

    # -- Center workspace: feed mode + zoom (View menu / Camera Control) ---

    def set_irvl_mode(self, mode: str, *, persist: bool = True) -> None:
        """Switch the center workspace between IR / IR+VL / VL.

        Display-only: widgets are shown/hidden in the existing splitter
        (ratio still draggable); acquisition, SHM and processing keep
        producing both feeds either way. Never reconnects, never restarts
        acquisition, never creates another observer or frame pipeline.
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
        """Reflect the workspace mode on the Camera Control feed buttons."""
        try:
            self._acq_panel.set_feed_mode(getattr(self, "_irvl_mode", "both"))
        except Exception:
            logger.debug("Feed button sync failed", exc_info=True)

    def _refresh_workspace_zoom(self) -> None:
        """Kept for View-menu zoom state sync (no workspace bar anymore)."""
        try:
            for action in getattr(self, "_zoom_menu_actions", {}).values():
                action.setEnabled(True)
        except Exception:
            pass

    # -- IR workspace zoom delegates (View menu targets; wheel still live) --

    def zoom_in(self) -> None:
        """Zoom the IR workspace in (cursor-centered, clamped)."""
        self._image_widget.zoom_in()

    def zoom_out(self) -> None:
        """Zoom the IR workspace out (floors at Fit, never below)."""
        self._image_widget.zoom_out()

    def zoom_fit(self) -> None:
        """Fit the IR workspace to the window."""
        self._image_widget.zoom_fit()

    def zoom_one_to_one(self) -> None:
        """Show the IR workspace at native pixels."""
        self._image_widget.zoom_one_to_one()

    def zoom_reset_pan(self) -> None:
        """Reset workspace pan (keep zoom)."""
        try:
            self._image_widget.set_pan_offset(0.0, 0.0)
        except Exception:
            logger.debug("Zoom reset-pan failed", exc_info=True)

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

        # PTZ background outcomes arrive here (GUI thread, guarded by
        # camera_id + generation so stale deliveries are dropped).
        self._ptz_status.connect(self._on_ptz_status, Qt.ConnectionType.QueuedConnection)
        self._ptz_operation.connect(
            self._on_ptz_operation, Qt.ConnectionType.QueuedConnection
        )
        self._ptz_positions.connect(
            self._on_ptz_positions, Qt.ConnectionType.QueuedConnection
        )
        self._ptz_notice.connect(self._on_ptz_notice, Qt.ConnectionType.QueuedConnection)
        self._ptz_import_confirm.connect(
            self._on_ptz_import_confirm, Qt.ConnectionType.QueuedConnection
        )
        self._ptz_active.connect(self._on_ptz_active, Qt.ConnectionType.QueuedConnection)
        self._roi_session_loaded.connect(
            self._on_roi_session_loaded, Qt.ConnectionType.QueuedConnection)
        self._roi_counts.connect(
            self._on_roi_counts, Qt.ConnectionType.QueuedConnection)
        self._roi_session_unlock.connect(
            self._on_roi_session_unlock, Qt.ConnectionType.QueuedConnection)

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
            # Select first camera if available (single selection funnel).
            cameras = self._config_service.get_all_camera_configs()
            if cameras:
                self._on_camera_selected(cameras[0].identity.camera_id)

    def _refresh_camera_list(self) -> None:
        """Refresh camera-derived views (no selector list lives here anymore).

        Camera selection flows exclusively through Connect -> Acquisition
        Setup -> Camera Selection; this only reloads the selected camera's
        panels and the open setup-dialog summary.
        """
        if self._selected_camera_id:
            self._load_camera_config(self._selected_camera_id)
        self._refresh_setup_dialog()

    def _on_camera_selected(self, camera_id: str) -> None:
        """Handle camera selection change with dirty state check.

        Single programmatic selection funnel (used at startup, by mode
        activation, and by tests driving the dirty-check/switch pipeline).
        """
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
            if self._selected_camera_id:
                self._load_camera_config(self._selected_camera_id)

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
        """Mirror the lifecycle state onto top-bar/panel indicators."""
        self._set_top_connection_state(self._lifecycle)
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
        self._refresh_setup_dialog()

    def _begin_session(self, camera_id: str) -> int:
        """Start a new session epoch for ``camera_id``.

        Bumps the generation so every queued result/render request from an
        older session is structurally stale, resets the render baselines
        (sequences restart at 0 per camera) and clears the displays.
        Returns the new generation.
        """
        self._session.renew(camera_id)
        self._selected_camera_id = camera_id
        self._ptz_clear_panels()
        # A new epoch invalidates any active PTZ position context for this
        # camera: stale contexts must never gate fresh results.
        try:
            self._ptz_registry.clear(camera_id)
        except Exception:
            pass
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
        """Check selected == Camera Control panel identity (invariant).

        Returns None when the panel view agrees with the authority
        (``self._selected_camera_id``), else a description of the
        mismatch. Reads are non-blocking Qt property lookups. There is
        exactly one selection view: the Camera Control identity label,
        fed by the single Connect -> Acquisition Setup -> Camera
        Selection flow.
        """
        try:
            panel_identity = self._acq_panel._selected_camera_identity
            panel_id = getattr(panel_identity, "camera_id", None)
        except Exception:
            panel_id = "<unreadable>"
        if panel_id != camera_id:
            return f"authority={camera_id} panel={panel_id}"
        return None

    def _resync_selection_views(self, camera_id: str) -> None:
        """Re-assert Camera Control views from the selection authority.

        Used only after an invariant refusal: the panel identity is
        reloaded from the authoritative camera. Never invents a new
        selection.
        """
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

    def _detach_observer_fast(self):
        """Detach the widget's observer WITHOUT blocking (transition fast path).

        Disconnects the Qt signals FIRST (in-flight queued results can no
        longer reach the slot), renews the session epoch so any late
        delivery is stale by token, and returns the detached observer
        for :meth:`_stop_observer_background`. The consumer-thread join
        and runtime-side detach happen off the GUI thread, so the
        visible transition never waits for them.
        """
        observer, self._observer = self._observer, None
        if observer is None:
            return None
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
            self._session.renew(camera_id or "")
        except Exception:
            pass
        return observer

    def _stop_observer_background(self, observer) -> None:
        """Stop a detached observer off the GUI thread (lifecycle-safe).

        Joins the processing-consumer thread (bounded) and clears the
        runtime-side reference. The GUI thread already cut the display
        path, so late results are dropped by signal disconnect +
        session-epoch token even while this runs.
        """
        if observer is None:
            return
        import threading as _threading
        import time as _time

        camera_id = getattr(observer, "camera_id", None) or self._selected_camera_id
        runtime = self._runtime_service

        def _teardown() -> None:
            _t0 = _time.perf_counter_ns()
            logger.info("[MODE-TRANSITION] background_teardown_started cam=%s", camera_id)
            try:
                observer.stop(timeout=_OBSERVER_STOP_TIMEOUT_S)
            except Exception:
                logger.exception("Observer stop failed for camera %s", camera_id)
            if runtime is not None and camera_id is not None:
                try:
                    runtime.stop_observer(camera_id)
                except Exception:
                    logger.debug("Runtime observer detach failed", exc_info=True)
            logger.info(
                "[MODE-TRANSITION] background_teardown_complete cam=%s teardown_ms=%.1f",
                camera_id,
                (_time.perf_counter_ns() - _t0) / 1e6,
            )

        _threading.Thread(
            target=_teardown, name=f"ConfigObserverTeardown-{camera_id}", daemon=True
        ).start()

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
        #
        # SHM reader inventory (why this order is the crash-safety order):
        # - ProcessingConsumer threads copy pinned SHM views into private
        #   memory and are JOINED by observer stop below. After this phase
        #   no thread holds a live SHM view or pin.
        # - Thermal/VL render workers NEVER touch SHM: their inputs are GUI
        #   copies (see _on_processing_result / VlImageWidget.set_frame), so
        #   session gating (set_session, done on the GUI thread before this
        #   op starts) suffices during switch/disconnect. At window
        #   destruction the widgets stop their render workers via
        #   destroyed/closeEvent handlers with the SHM still mapped
        #   (runtime shutdown runs after window close in AppController).
        # Only after every reader is joined may the owner close/unlink.
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
        # Uses the handle's own serialized helpers: wait_for_exit() never
        # touches pipes/SHM (safe after stop closed them) and terminate()
        # is a no-op unless the child is still alive. Direct
        # old_handle.process.is_alive()/terminate()/join() calls are
        # forbidden here: stop_camera() above already closed this handle.
        phase = time.perf_counter()
        exited = True
        if old_handle is not None:
            try:
                exited = old_handle.wait_for_exit(_TEARDOWN_VERIFY_TIMEOUT_S)
                if not exited:
                    old_handle.terminate()
                    exited = old_handle.wait_for_exit(1.0)
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
        # Crash-safety (same contract as _on_disconnect): no stats probes
        # once a teardown owns the pipeline; control workers quit now and
        # are reaped deterministically after the background op starts.
        self._stats_timer.stop()
        self._stop_focus_worker()
        self._stop_nuc_worker(timeout_ms=0)
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
            else:
                # Deterministic control-worker shutdown (bounded waits);
                # in-flight requests abort on handle STOPPING (see
                # _on_disconnect for the contract).
                self._stop_focus_worker(timeout_ms=8000)
                self._stop_nuc_worker(timeout_ms=8000)
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
        # Teardown/connect owns the pipeline only while in flight: resume
        # stats polling now (a pending switch/disconnect below stops it
        # again before its own background op starts).
        try:
            if not self._stats_timer.isActive():
                self._stats_timer.start(1000)
        except RuntimeError:
            pass  # widget teardown; nothing left to poll
        self._process_pending_camera_request()

    def _on_bg_result(self, tag: str | None, result: object) -> None:
        """Apply a successful background camera operation (GUI thread)."""
        try:
            if tag == "switch":
                self._set_lifecycle(CameraConnectionState.DISCONNECTED)
                if self._selected_camera_id:
                    self._load_camera_config(self._selected_camera_id)
                    self._status_label.setText(f"Camera: {self._selected_camera_id}")
                self._clear_camera_scoped_state(self._selected_camera_id)
            elif tag == "switch_connect":
                timings, connect_ms = result
                self._set_lifecycle(CameraConnectionState.CONNECTED)
                self._status_conn.setText("Connection: Connected")
                self._status_label.setText(
                    f"Camera connected ({connect_ms:.0f} ms) - press Start to begin acquisition"
                )
                if self._selected_camera_id:
                    self._load_camera_config(self._selected_camera_id)
                    self._ptz_attach_async(
                        self._selected_camera_id, self._session.generation
                    )
                self._refresh_focus_panel()
                self._refresh_nuc_panel()
            elif tag == "disconnect":
                self._set_lifecycle(CameraConnectionState.DISCONNECTED)
                if self._selected_camera_id:
                    self._load_camera_config(self._selected_camera_id)
                    # PTZ stays connected at the session level, but the
                    # panels return to binding-only display: no live PTZ
                    # state is shown for a disconnected camera.
                    self._ptz_refresh_binding(self._selected_camera_id)
                    try:
                        self._ptz_panel.set_status(None)
                        self._ptz_panel.set_operation(None)
                        self._ptz_panel.set_active_position("")
                    except RuntimeError:
                        pass
                self._clear_camera_scoped_state(self._selected_camera_id)
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
                    self._ptz_attach_async(
                        self._selected_camera_id, self._session.generation
                    )
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

        # Update top bar + acquisition panel
        self._set_top_connection_state(status)

        # Update acquisition panel
        self._acq_panel.set_camera_identity(identity)
        self._acq_panel.set_connection_state(status)

        # Startup acquisition parameters live in the Acquisition Setup
        # dialog (loaded from the same metadata keys the Start pipeline
        # has always used — never invented, never reimplemented).
        try:
            fps = int(metadata.get("frame_rate", 9))
        except (TypeError, ValueError):
            fps = 9
        averaging = str(metadata.get("averaging", "Off"))
        try:
            history_frames = int(metadata.get("history_frames", 100))
        except (TypeError, ValueError):
            history_frames = 100
        ir_scaling = str(metadata.get("ir_display_scaling", "fast") or "fast").lower()
        if ir_scaling not in ("fast", "smooth"):
            ir_scaling = "fast"
        self._acq_params = {
            "fps": fps,
            "averaging": averaging,
            "history_frames": history_frames,
            "ir_scaling": ir_scaling,
        }
        self._apply_ir_scaling_to_views(ir_scaling)

        # Update panels
        self._roi_panel.set_camera(camera_id)
        # Phase 10: a camera switch drops the position-bound session so
        # the previous camera's objects can never display or persist here.
        self._clear_roi_session("No position — select and reach a saved position")
        self._alarm_panel.set_camera(camera_id)
        self._stats_panel.clear()
        self._refresh_setup_dialog()

        # PTZ binding display follows camera selection (no I/O here;
        # attach/connect happens on the connect lifecycle path).
        self._ptz_refresh_binding(camera_id)

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

    # -- PTZ integration (Phase 6: service-driven, never direct OPC UA) ----
    #
    # All PTZ I/O runs on retained daemon threads; outcomes return via
    # the _ptz_* queued signals carrying (camera_id, generation) so stale
    # deliveries from a previous camera/session can never touch panels.
    # The camera lifecycle is never blocked: attach/connect/monitor all
    # happen off the GUI thread, and no PTZ ever auto-moves on connect.

    def _ptz_mapping_entry(self, camera_id: str):
        """YAML mapping entry for a camera (None when unconfigured)."""
        try:
            mappings = self._config_manager.get_config().cameras.mapping
        except Exception:
            return None
        for entry in mappings or []:
            if getattr(entry, "camera_id", None) == camera_id:
                return entry
        return None

    def _ptz_global_config(self):
        try:
            return self._config_manager.get_config().ptz
        except Exception:
            return None

    def _ptz_refresh_binding(self, camera_id: str) -> None:
        """Cheap binding display refresh (no I/O; safe anywhere)."""
        if self._ptz_panel is None:
            return
        entry = self._ptz_mapping_entry(camera_id)
        ptz_id = (getattr(entry, "ptz_id", "") or "").strip() or None
        ptz_cfg = self._ptz_global_config()
        endpoint = ""
        if ptz_cfg is not None and ptz_id is not None:
            from thermal_monitor.ptz.station import resolve_endpoint

            endpoint = resolve_endpoint(
                getattr(ptz_cfg, "endpoint", ""),
                getattr(entry, "ptz_endpoint", "") or "",
            )
        try:
            self._ptz_panel.set_binding(camera_id, ptz_id, bool(endpoint))
            self._pos_panel.set_station(camera_id, ptz_id)
        except RuntimeError:
            pass  # teardown race; panels already gone

    def _ptz_clear_panels(self) -> None:
        try:
            if self._ptz_panel is not None:
                self._ptz_panel.clear()
            if self._pos_panel is not None:
                self._pos_panel.clear()
        except RuntimeError:
            pass

    def _clear_camera_scoped_state(self, camera_id: str | None) -> None:
        """Drop camera-scoped PTZ/alarm state on disconnect/switch.

        Phase 9B: the active-position registry entry, the panel's active
        label, and the tracked live alarm events belong to one camera
        session. Clearing them here (with the session epoch already
        renewed by the caller) guarantees no stale Camera A state can
        appear under Camera B. Shared PTZ services/sessions are kept:
        another camera may still need them.
        """
        if camera_id:
            try:
                self._ptz_registry.clear(camera_id)
            except Exception:
                pass
        self._active_alarm_events.clear()
        self._set_alarm_top_count(0)
        self._set_ptz_top_state(
            "Camera disconnected" if self._lifecycle.name == "DISCONNECTED"
            else self._ptz_top_state
        )
        try:
            if self._ptz_panel is not None:
                self._ptz_panel.set_active_position("")
                self._ptz_panel.set_operation(None)
        except RuntimeError:
            pass

    def _ptz_run(self, name: str, fn) -> None:
        """Run ``fn`` on a retained daemon thread (never blocks GUI)."""

        def _target() -> None:
            try:
                fn()
            except Exception:
                logger.exception("PTZ background operation %s failed", name)
            finally:
                self._ptz_threads.discard(thread)

        thread = threading.Thread(target=_target, name=f"ConfigPtz-{name}", daemon=True)
        self._ptz_threads.add(thread)
        thread.start()

    def _ptz_service_for(self, endpoint: str, ptz_ids: tuple):
        """Get-or-create the shared service for one endpoint (bg only)."""
        from thermal_monitor.ptz.client import (
            AsyncuaTransport,
            OpcUaClientConfig,
            OpcUaSession,
        )
        from thermal_monitor.ptz.mapping import SimulatorPtzMapping
        from thermal_monitor.ptz.service import PtzService
        from thermal_monitor.ptz.station import build_service_config

        service = self._ptz_services.get(endpoint)
        if service is not None:
            return service
        ptz_cfg = self._ptz_global_config()
        mapping = SimulatorPtzMapping(ptz_ids=tuple(ptz_ids))
        session = OpcUaSession(
            OpcUaClientConfig(
                endpoint=endpoint,
                connect_timeout_s=5.0,
                read_timeout_s=2.0,
                write_timeout_s=3.0,
            ),
            AsyncuaTransport(),
        )
        if ptz_cfg is not None:
            from thermal_monitor.ptz.models import PtzLimits

            service_config = build_service_config(
                limits=PtzLimits(
                    min_pan=ptz_cfg.limits.min_pan,
                    max_pan=ptz_cfg.limits.max_pan,
                    min_tilt=ptz_cfg.limits.min_tilt,
                    max_tilt=ptz_cfg.limits.max_tilt,
                    min_velocity=0.1,
                    max_velocity=360.0,
                ),
                tolerance_pan=ptz_cfg.tolerance_pan,
                tolerance_tilt=ptz_cfg.tolerance_tilt,
                move_timeout_s=ptz_cfg.move_timeout_s,
                calibration_timeout_s=ptz_cfg.calibration_timeout_s,
                monitor_interval_s=ptz_cfg.monitor_interval_s,
            )
        else:
            service_config = None
        service = PtzService(session, mapping, service_config)
        service.add_status_listener(self._ptz_on_service_status)
        self._ptz_services[endpoint] = service
        return service

    def _ptz_on_service_status(self, ptz_id: str, status) -> None:
        """Service monitor callback (background thread): re-emit guarded."""
        camera_id = self._selected_camera_id
        if camera_id is None:
            return
        try:
            self._ptz_status.emit(camera_id, self._session.generation, ptz_id, status)
        except RuntimeError:
            pass  # teardown; nothing left to update

    def _ptz_attach_async(self, camera_id: str, generation: int) -> None:
        """Resolve binding and connect the PTZ service (background).

        Phase 9B: the panel immediately shows the connecting state on
        the GUI thread (no blocking); the OPC UA connect/session/status
        read happens on the daemon below. Failures arrive as actionable
        notices, never tracebacks.
        """
        entry = self._ptz_mapping_entry(camera_id)
        _pending_ptz = (getattr(entry, "ptz_id", "") or "").strip() if entry else ""
        if _pending_ptz:
            self._set_ptz_top_state(f"{_pending_ptz} (connecting)")
            try:
                if self._ptz_panel is not None:
                    self._ptz_panel.set_binding(camera_id, _pending_ptz, True)
                    self._ptz_panel.show_message("Connecting…")
            except RuntimeError:
                pass
        else:
            self._set_ptz_top_state("Not configured")
            # Phase 9C: make the unconfigured state explicit on the
            # panel as well (attach-from-start path does not run
            # _load_camera_config first). Never invents a binding.
            try:
                if self._ptz_panel is not None:
                    self._ptz_panel.set_binding(camera_id, None, False)
                    self._ptz_panel.show_message(
                        f"No PTZ configured for {camera_id}. "
                        "Add ptz_id to cameras.mapping for this camera."
                    )
            except RuntimeError:
                pass

        def _attach() -> None:
            from thermal_monitor.ptz.station import (
                resolve_binding,
                resolve_endpoint,
            )

            entry = self._ptz_mapping_entry(camera_id)
            ptz_id = (getattr(entry, "ptz_id", "") or "").strip() if entry else ""
            binding = resolve_binding(camera_id, ptz_id) if entry else None
            if binding is None:
                return  # no PTZ configured; binding display already shows it
            ptz_cfg = self._ptz_global_config()
            endpoint = resolve_endpoint(
                getattr(ptz_cfg, "endpoint", "") if ptz_cfg else "",
                getattr(entry, "ptz_endpoint", "") or "",
            )
            if not endpoint:
                self._ptz_notice.emit(
                    camera_id, generation, f"{binding.ptz_id}: OPC UA endpoint not configured"
                )
                return
            try:
                mappings = self._config_manager.get_config().cameras.mapping
            except Exception:
                mappings = []
            ptz_ids = tuple(
                sorted(
                    {
                        (getattr(m, "ptz_id", "") or "").strip()
                        for m in (mappings or [])
                        if (getattr(m, "ptz_id", "") or "").strip()
                    }
                )
            ) or (binding.ptz_id,)
            try:
                service = self._ptz_service_for(endpoint, ptz_ids)
            except Exception as exc:
                logger.warning("PTZ service creation failed: %s", exc)
                self._ptz_notice.emit(camera_id, generation, f"PTZ unavailable: {exc}")
                return
            try:
                service.register_binding(binding)
            except Exception as exc:
                logger.warning("PTZ binding failed: %s", exc)
                self._ptz_notice.emit(camera_id, generation, f"PTZ binding failed: {exc}")
                return
            try:
                service.connect()
                service.start_monitoring()
            except Exception as exc:
                logger.warning("PTZ connect failed: %s", exc)
                self._ptz_notice.emit(camera_id, generation, f"PTZ connect failed: {exc}")
                return
            try:
                status = service.get_status(camera_id)
                self._ptz_status.emit(camera_id, generation, binding.ptz_id, status)
            except Exception as exc:
                logger.debug("PTZ status refresh failed", exc_info=True)
            self._ptz_load_positions(camera_id, generation)

        self._ptz_run(f"attach-{camera_id}", _attach)

    def _ptz_positions_repo(self):
        if self._database is None:
            return None
        from thermal_monitor.storage.repositories.ptz import PtzPositionRepository

        try:
            return PtzPositionRepository(self._database)
        except Exception:
            logger.debug("PTZ position repository unavailable", exc_info=True)
            return None

    def _ptz_load_positions(self, camera_id: str, generation: int) -> None:
        repo = self._ptz_positions_repo()
        if repo is None:
            return  # persistence unavailable; table stays empty, no error spam
        try:
            result = repo.list_positions_for_camera(camera_id)
        except Exception as exc:
            logger.debug("PTZ position load failed", exc_info=True)
            return
        if result.success and result.data is not None:
            try:
                self._ptz_positions.emit(camera_id, generation, list(result.data))
            except RuntimeError:
                pass

    def _ptz_teardown_all_async(self) -> None:
        """Shut down every PTZ service off the GUI thread (never blocks)."""

        def _teardown() -> None:
            services = list(self._ptz_services.values())
            self._ptz_services.clear()
            for service in services:
                try:
                    service.shutdown(timeout_s=5.0)
                except Exception:
                    pass

        self._ptz_run("teardown-all", _teardown)
        self._ptz_clear_panels()

    # -- PTZ panel request handlers (GUI thread -> background) -------------

    def _ptz_guarded_service(self, camera_id: str):
        """Resolve (service, binding, generation) or show why not."""
        entry = self._ptz_mapping_entry(camera_id)
        ptz_id = (getattr(entry, "ptz_id", "") or "").strip() if entry else ""
        if not ptz_id:
            self._ptz_panel.show_message("No PTZ configured for this camera.")
            return None
        ptz_cfg = self._ptz_global_config()
        from thermal_monitor.ptz.station import resolve_endpoint

        endpoint = resolve_endpoint(
            getattr(ptz_cfg, "endpoint", "") if ptz_cfg else "",
            getattr(entry, "ptz_endpoint", "") or "",
        )
        service = self._ptz_services.get(endpoint) if endpoint else None
        if service is None:
            self._ptz_panel.show_message("PTZ not connected. Connect the camera first.")
            return None
        try:
            binding = service.binding_for_camera(camera_id)
        except Exception as exc:
            self._ptz_panel.show_message(str(exc))
            return None
        return service, binding, self._session.generation

    def _on_ptz_move_requested(
        self, pan: float, tilt: float, velocity: float, mode: str
    ) -> None:
        camera_id = self._selected_camera_id
        if camera_id is None or self._ptz_panel is None:
            return
        resolved = self._ptz_guarded_service(camera_id)
        if resolved is None:
            return
        service, binding, generation = resolved

        def _move() -> None:
            try:
                # Phase 9C: the panel emits plain strings ("single" /
                # "per_axis") but PtzCommand requires a VelocityMode
                # (isinstance check). Coerce here so absolute moves are
                # not rejected as COMMAND_REJECTED before reaching OPC UA.
                from thermal_monitor.ptz.models import VelocityMode

                try:
                    _mode = VelocityMode(str(mode or "single").strip().lower())
                except ValueError:
                    _mode = VelocityMode.SINGLE
                if _mode == VelocityMode.PER_AXIS:
                    op = service.move_absolute(
                        camera_id, pan, tilt, velocity_mode=_mode,
                        pan_velocity=velocity, tilt_velocity=velocity,
                    )
                else:
                    op = service.move_absolute(
                        camera_id, pan, tilt, velocity=velocity,
                        velocity_mode=_mode,
                    )
                self._ptz_operation.emit(camera_id, generation, op)
                try:
                    status = service.get_status(camera_id)
                    self._ptz_status.emit(
                        camera_id, generation, binding.ptz_id, status
                    )
                except Exception:
                    pass
            except Exception as exc:
                self._ptz_notice.emit(camera_id, generation, f"Move failed: {exc}")

        self._ptz_run(f"move-{camera_id}", _move)

    def _on_ptz_relative_requested(
        self, delta_pan: float, delta_tilt: float, velocity: float
    ) -> None:
        camera_id = self._selected_camera_id
        if camera_id is None or self._ptz_panel is None:
            return
        resolved = self._ptz_guarded_service(camera_id)
        if resolved is None:
            return
        service, binding, generation = resolved

        def _move() -> None:
            try:
                op = service.move_relative(
                    camera_id, delta_pan, delta_tilt, velocity=velocity
                )
                self._ptz_operation.emit(camera_id, generation, op)
                try:
                    status = service.get_status(camera_id)
                    self._ptz_status.emit(
                        camera_id, generation, binding.ptz_id, status
                    )
                except Exception:
                    pass
            except Exception as exc:
                self._ptz_notice.emit(camera_id, generation, f"Move failed: {exc}")

        self._ptz_run(f"rel-{camera_id}", _move)

    def _on_ptz_stop_requested(self) -> None:
        camera_id = self._selected_camera_id
        if camera_id is None:
            return
        resolved = self._ptz_guarded_service(camera_id)
        if resolved is None:
            return
        service, binding, generation = resolved

        def _stop() -> None:
            try:
                op = service.stop(camera_id)
                self._ptz_operation.emit(camera_id, generation, op)
            except Exception as exc:
                self._ptz_notice.emit(camera_id, generation, f"STOP failed: {exc}")

        self._ptz_run(f"stop-{camera_id}", _stop)

    def _on_ptz_clear_error_requested(self) -> None:
        camera_id = self._selected_camera_id
        if camera_id is None:
            return
        resolved = self._ptz_guarded_service(camera_id)
        if resolved is None:
            return
        service, binding, generation = resolved

        def _clear() -> None:
            try:
                status = service.clear_error(camera_id)
                self._ptz_status.emit(camera_id, generation, binding.ptz_id, status)
            except Exception as exc:
                self._ptz_notice.emit(camera_id, generation, f"Clear failed: {exc}")

        self._ptz_run(f"clear-{camera_id}", _clear)

    def _on_ptz_calibration_requested(self) -> None:
        camera_id = self._selected_camera_id
        if camera_id is None:
            return
        resolved = self._ptz_guarded_service(camera_id)
        if resolved is None:
            return
        service, binding, generation = resolved

        def _calibrate() -> None:
            try:
                self._ptz_notice.emit(camera_id, generation, "Calibration started…")
                status = service.request_calibration(camera_id)
                self._ptz_status.emit(camera_id, generation, binding.ptz_id, status)
                self._ptz_notice.emit(camera_id, generation, "Calibration complete.")
            except Exception as exc:
                self._ptz_notice.emit(
                    camera_id, generation, f"Calibration failed: {exc}"
                )

        self._ptz_run(f"cal-{camera_id}", _calibrate)

    # -- PTZ position handlers (GUI thread -> background) ------------------

    def _on_ptz_refresh_requested(self) -> None:
        camera_id = self._selected_camera_id
        if camera_id is None:
            return
        self._ptz_run(
            f"pos-load-{camera_id}",
            lambda: self._ptz_load_positions(camera_id, self._session.generation),
        )

    def _on_ptz_export_requested(self) -> None:
        from PyQt6.QtWidgets import QFileDialog

        camera_id = self._selected_camera_id
        if camera_id is None or self._pos_panel is None:
            return
        entry = self._ptz_mapping_entry(camera_id)
        ptz_id = (getattr(entry, "ptz_id", "") or "").strip()
        path, _ = QFileDialog.getSaveFileName(
            self, "Export PTZ Positions", f"{camera_id}_positions.json",
            "JSON (*.json)",
        )
        if not path:
            return
        generation = self._session.generation
        repo = self._ptz_positions_repo()
        if repo is None:
            self._pos_panel.show_message("Position database unavailable.")
            return

        def _export() -> None:
            from thermal_monitor.ptz.positions_io import export_positions

            try:
                result = repo.list_positions_for_camera(camera_id)
            except Exception as exc:
                self._ptz_notice.emit(camera_id, generation, f"Export failed: {exc}")
                return
            if not result.success:
                self._ptz_notice.emit(
                    camera_id, generation, f"Export failed: {result.error}"
                )
                return
            try:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(
                        export_positions(
                            list(result.data or []),
                            camera_id=camera_id,
                            ptz_id=ptz_id,
                        )
                    )
            except OSError as exc:
                self._ptz_notice.emit(camera_id, generation, f"Export failed: {exc}")
                return
            self._ptz_notice.emit(
                camera_id, generation,
                f"Exported {len(result.data or [])} positions to {path}.",
            )

        self._ptz_run(f"pos-export-{camera_id}", _export)

    def _on_ptz_import_requested(self) -> None:
        from PyQt6.QtWidgets import QFileDialog

        camera_id = self._selected_camera_id
        if camera_id is None or self._pos_panel is None:
            return
        entry = self._ptz_mapping_entry(camera_id)
        ptz_id = (getattr(entry, "ptz_id", "") or "").strip()
        path, _ = QFileDialog.getOpenFileName(
            self, "Import PTZ Positions", "", "JSON (*.json)"
        )
        if not path:
            return
        generation = self._session.generation
        repo = self._ptz_positions_repo()
        if repo is None:
            self._pos_panel.show_message("Position database unavailable.")
            return

        def _import() -> None:
            from thermal_monitor.ptz.positions_io import (
                ConflictPolicy,
                commit_import,
                preview_import,
            )

            try:
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
            except OSError as exc:
                self._ptz_notice.emit(camera_id, generation, f"Import failed: {exc}")
                return
            try:
                existing = repo.list_positions_for_camera(camera_id)
            except Exception as exc:
                self._ptz_notice.emit(camera_id, generation, f"Import failed: {exc}")
                return
            if not existing.success:
                self._ptz_notice.emit(
                    camera_id, generation, f"Import failed: {existing.error}"
                )
                return
            current = list(existing.data or [])
            preview, parsed = preview_import(
                text,
                {p.position_id for p in current},
                {(p.camera_id, p.name): p.position_id for p in current},
                policy=ConflictPolicy.REJECT,
                expected_camera_id=camera_id,
                expected_ptz_id=ptz_id,
            )
            if not preview.valid:
                # REPLACE path: when conflicts are the ONLY problem, ask
                # for explicit confirmation instead of failing silently.
                # Anything else is a hard rejection.
                if preview.conflicts and all(
                    "conflicts with existing record" in error
                    for error in preview.errors
                ):
                    if not self._ptz_confirm_replace(
                        camera_id, generation, preview.conflicts
                    ):
                        self._ptz_notice.emit(
                            camera_id, generation,
                            "Import cancelled: existing positions kept.",
                        )
                        return
                    preview, parsed = preview_import(
                        text,
                        {p.position_id for p in current},
                        {(p.camera_id, p.name): p.position_id for p in current},
                        policy=ConflictPolicy.REPLACE,
                        expected_camera_id=camera_id,
                        expected_ptz_id=ptz_id,
                    )
                    if not preview.valid:
                        self._ptz_notice.emit(
                            camera_id, generation,
                            f"Import rejected: {'; '.join(preview.errors[:3])}",
                        )
                        return
                    result = commit_import(repo, parsed, policy=ConflictPolicy.REPLACE)
                    self._ptz_finish_import(
                        camera_id, generation, result, created_label="Replaced"
                    )
                    return
                self._ptz_notice.emit(
                    camera_id, generation,
                    f"Import rejected: {'; '.join(preview.errors[:3])}",
                )
                return
            result = commit_import(repo, parsed, policy=ConflictPolicy.REJECT)
            self._ptz_finish_import(camera_id, generation, result)

        self._ptz_run(f"pos-import-{camera_id}", _import)

    def _ptz_confirm_replace(
        self, camera_id: str, generation: int, conflicts: tuple
    ) -> bool:
        """Ask the operator to confirm REPLACE (GUI dialog, bg waits).

        Returns False on cancel, stale session, teardown, or timeout.
        Never blocks the GUI thread: the dialog runs there, the waiter
        is the background importer.
        """
        done = threading.Event()
        answer: dict = {}
        try:
            self._ptz_import_confirm.emit(
                camera_id,
                generation,
                {"entries": list(conflicts), "done": done, "answer": answer},
            )
        except RuntimeError:
            return False
        while not done.wait(0.2):
            try:
                if not self._ptz_is_current_bg(camera_id, generation):
                    return False
            except Exception:
                return False
        return bool(answer.get("confirmed", False))

    def _ptz_finish_import(self, camera_id: str, generation: int, result,
                           created_label: str = "Imported") -> None:
        if not result.committed:
            self._ptz_notice.emit(
                camera_id, generation,
                f"Import rolled back: {'; '.join(result.errors[:2])}",
            )
            return
        parts = []
        if result.created:
            parts.append(f"{created_label} {result.created}")
        if result.replaced:
            parts.append(f"replaced {result.replaced}")
        if result.skipped:
            parts.append(f"skipped {result.skipped}")
        self._ptz_notice.emit(
            camera_id, generation,
            f"Import complete: {', '.join(parts) or 'nothing to do'}.",
        )
        self._ptz_load_positions(camera_id, generation)

    def _on_ptz_save_current_requested(self, name: str) -> None:
        camera_id = self._selected_camera_id
        if camera_id is None or self._pos_panel is None:
            return
        resolved = self._ptz_guarded_service(camera_id)
        if resolved is None:
            self._pos_panel.show_message("PTZ not connected.")
            return
        service, binding, generation = resolved
        repo = self._ptz_positions_repo()
        if repo is None:
            self._pos_panel.show_message("Position database unavailable.")
            return
        ptz_cfg = self._ptz_global_config()

        def _save() -> None:
            from thermal_monitor.ptz.positions import PtzPosition, generate_position_id

            try:
                status = service.get_status(camera_id)
            except Exception as exc:
                self._ptz_notice.emit(camera_id, generation, f"Save failed: {exc}")
                return
            velocity = (
                ptz_cfg.default_velocity if ptz_cfg is not None else 10.0
            )
            position = PtzPosition(
                position_id=generate_position_id(),
                camera_id=camera_id,
                ptz_id=binding.ptz_id,
                name=name,
                pan=status.actual_pan,
                tilt=status.actual_tilt,
                velocity=velocity,
            )
            try:
                result = repo.create_position(position)
            except Exception as exc:
                self._ptz_notice.emit(camera_id, generation, f"Save failed: {exc}")
                return
            if not result.success:
                self._ptz_notice.emit(
                    camera_id, generation, f"Save failed: {result.error}"
                )
                return
            self._ptz_load_positions(camera_id, generation)

        self._ptz_run(f"pos-save-{camera_id}", _save)

    def _on_ptz_delete_requested(self, position_id: str) -> None:
        camera_id = self._selected_camera_id
        if camera_id is None:
            return
        generation = self._session.generation

        def _delete() -> None:
            # Phase 11 orphan policy: the position row and ALL of its ROI
            # definitions are deleted in ONE database transaction, scoped
            # by (camera_id, position_id). No orphaned ROI row may survive
            # to be loaded for another position later.
            database = getattr(self, "_database", None)
            if database is None or not hasattr(database, "transaction"):
                self._ptz_notice.emit(
                    camera_id, generation, "Delete failed: database unavailable.")
                return
            try:
                with database.transaction() as cursor:
                    try:
                        cursor.execute(
                            "DELETE FROM roi_definitions "
                            "WHERE camera_id = ? AND position_id = ?",
                            (camera_id, position_id),
                        )
                        roi_removed = cursor.rowcount or 0
                    except Exception as exc:
                        # Pre-ROI databases have no roi_definitions table;
                        # then there is nothing orphaned to clean up.
                        message = str(exc).lower()
                        if ("no such table" not in message
                                and "invalid object name" not in message):
                            raise
                        roi_removed = 0
                    cursor.execute(
                        "DELETE FROM ptz_positions "
                        "WHERE position_id = ? AND camera_id = ?",
                        (position_id, camera_id),
                    )
                    pos_removed = cursor.rowcount
            except Exception as exc:
                self._ptz_notice.emit(camera_id, generation, f"Delete failed: {exc}")
                return
            if not pos_removed:
                self._ptz_notice.emit(camera_id, generation, "Position not found.")
                return
            if roi_removed:
                logger.info("Position %s deleted with %d ROI definitions",
                            position_id, roi_removed)
            self._ptz_load_positions(camera_id, generation)

        self._ptz_run(f"pos-del-{camera_id}", _delete)

    def _on_ptz_rename_requested(self, position_id: str, name: str) -> None:
        camera_id = self._selected_camera_id
        if camera_id is None:
            return
        generation = self._session.generation
        repo = self._ptz_positions_repo()
        if repo is None:
            return

        def _rename() -> None:
            try:
                current = repo.get_position(position_id)
            except Exception as exc:
                self._ptz_notice.emit(camera_id, generation, f"Rename failed: {exc}")
                return
            if not current.success or current.data is None:
                self._ptz_notice.emit(camera_id, generation, "Position not found.")
                return
            try:
                updated = repo.update_position(
                    current.data.with_updated(name=name)
                )
            except Exception as exc:
                self._ptz_notice.emit(camera_id, generation, f"Rename failed: {exc}")
                return
            if not updated.success:
                self._ptz_notice.emit(
                    camera_id, generation, f"Rename failed: {updated.error}"
                )
                return
            self._ptz_load_positions(camera_id, generation)

        self._ptz_run(f"pos-rename-{camera_id}", _rename)

    def _on_ptz_edit_rois_requested(self, position_id: str) -> None:
        """Phase 11 Set ROI workflow: edit ROIs for one saved position.

        - Session already active for that position -> editing is enabled;
          just confirm the context.
        - Otherwise -> controlled Go To-and-wait through the existing
          workflow; the session installs automatically on reached.
        - No PTZ attached -> actionable message, never a silent edit
          under the wrong position.
        """
        camera_id = self._selected_camera_id
        if camera_id is None or self._pos_panel is None:
            return
        canvas = getattr(self, "_roi_canvas", None)
        editor = getattr(canvas, "editor", None) if canvas is not None else None
        if editor is not None and editor.context.position_id == position_id:
            self._ptz_notice.emit(
                camera_id, self._session.generation,
                f"Editing ROIs for {editor.context.position_id}.")
            return
        if self._ptz_guarded_service(camera_id) is None:
            self._pos_panel.show_message(
                "PTZ not connected. Connect the PTZ, then Set ROI will "
                "move to the position and enable editing.")
            return
        self._on_ptz_goto_requested(position_id)

    def _on_ptz_roi_associate_requested(self, position_id: str, roi_set_ref: str) -> None:
        """Legacy manual ROI-set association (no UI path; kept for compat).

        The visible Position Table no longer exposes roi_set_ref. This
        handler is retained so older callers/signals do not break, but
        nothing in the UI connects to it.
        """
        camera_id = self._selected_camera_id
        if camera_id is None:
            return
        generation = self._session.generation
        repo = self._ptz_positions_repo()
        if repo is None:
            return

        def _associate() -> None:
            try:
                result = repo.associate_roi_set(position_id, roi_set_ref)
            except Exception as exc:
                self._ptz_notice.emit(
                    camera_id, generation, f"ROI association failed: {exc}"
                )
                return
            if not result.success:
                self._ptz_notice.emit(
                    camera_id, generation, f"ROI association failed: {result.error}"
                )
                return
            self._ptz_load_positions(camera_id, generation)

        self._ptz_run(f"pos-roi-{camera_id}", _associate)

    def _on_ptz_goto_requested(self, position_id: str) -> None:
        camera_id = self._selected_camera_id
        if camera_id is None or self._pos_panel is None:
            return
        resolved = self._ptz_guarded_service(camera_id)
        if resolved is None:
            self._pos_panel.show_message("PTZ not connected.")
            return
        service, binding, generation = resolved
        repo = self._ptz_positions_repo()
        ptz_cfg = self._ptz_global_config()
        # Phase 11 movement lock: the previous ROI context stays visible
        # but is NOT editable while the PTZ moves, so geometry can never
        # be drawn onto a moving view under a stale context.
        try:
            position_name = next(
                (p.name for p in self._pos_panel._positions
                 if p.position_id == position_id),
                position_id,
            )
            canvas = getattr(self, "_roi_canvas", None)
            if canvas is not None:
                canvas.set_editable(False)
            toolbar = getattr(self, "_roi_toolbar", None)
            if toolbar is not None:
                toolbar.set_context_active(
                    False, f"Moving to {position_name}… (ROI locked)")
        except RuntimeError:
            pass

        def _goto() -> None:
            position = None
            if repo is not None:
                try:
                    current = repo.get_position(position_id)
                except Exception as exc:
                    self._ptz_notice.emit(
                        camera_id, generation, f"Go To failed: {exc}"
                    )
                    return
                if not current.success or current.data is None:
                    self._ptz_notice.emit(camera_id, generation, "Position not found.")
                    return
                position = current.data
            else:
                self._ptz_notice.emit(
                    camera_id, generation, "Position database unavailable."
                )
                return
            # Safe Go To + observer retarget (Phase 8): the coordinator
            # moves through PtzService, requires authoritative reached,
            # validates the ROI set, publishes the context to the
            # observer, and commits the registry exactly once. Latest
            # request wins per camera; failures keep prior context.
            from thermal_monitor.ptz.retarget import ObserverRetargetCoordinator
            from thermal_monitor.ptz.roi_activation import RoiActivationState
            from thermal_monitor.ptz.station import resolve_endpoint as _resolve_ep

            default_velocity = (
                ptz_cfg.default_velocity if ptz_cfg is not None else 10.0
            )
            move_timeout = (
                ptz_cfg.move_timeout_s if ptz_cfg is not None else 30.0
            )
            entry = self._ptz_mapping_entry(camera_id)
            endpoint = _resolve_ep(
                getattr(ptz_cfg, "endpoint", "") if ptz_cfg else "",
                getattr(entry, "ptz_endpoint", "") or "" if entry else "",
            )
            coordinator = self._ptz_coordinators.get(endpoint)
            if coordinator is None:
                coordinator = ObserverRetargetCoordinator(
                    service, registry=self._ptz_registry
                )
                self._ptz_coordinators[endpoint] = coordinator

            def _observer_for_retarget():
                observer = self._observer
                try:
                    if observer is not None and getattr(
                        observer, "camera_id", None
                    ) == camera_id:
                        return observer
                except Exception:
                    pass
                return None

            try:
                result = coordinator.retarget(
                    camera_id,
                    position,
                    analysis_config_provider=(
                        lambda cam: self._config_service.get_analysis_config(cam)
                    ),
                    observer_provider=_observer_for_retarget,
                    session_generation_provider=lambda: self._session.generation,
                    velocity=default_velocity,
                    timeout_s=move_timeout,
                )
            except Exception as exc:
                self._ptz_notice.emit(camera_id, generation, f"Go To failed: {exc}")
                return
            if result.operation is not None:
                self._ptz_operation.emit(camera_id, generation, result.operation)
            try:
                status = service.get_status(camera_id)
                self._ptz_status.emit(
                    camera_id, generation, binding.ptz_id, status
                )
            except Exception:
                pass
            if result.state == RoiActivationState.COMPLETED:
                self._ptz_active.emit(camera_id, generation, position.name)
                if position.roi_set_ref:
                    self._ptz_notice.emit(
                        camera_id,
                        generation,
                        f"Reached {position.name}; ROI set "
                        f"'{position.roi_set_ref}' activated "
                        f"({len(result.roi_ids)} ROIs).",
                    )
                else:
                    self._ptz_notice.emit(
                        camera_id, generation, f"Reached {position.name}."
                    )
                # Phase 11: load the position-bound ROI set through the
                # single authoritative loader (same worker, after reached
                # only). Failures keep the previous session; nothing
                # partial is ever published.
                try:
                    from thermal_monitor.roi.loading import load_rois_for_position
                    from thermal_monitor.roi.serialization import roi_to_dict

                    roi_repo = self._roi_repository()
                    retained = self._ptz_registry.get(camera_id)
                    gens = getattr(self, "_roi_session_gens", {})
                    if roi_repo is not None:
                        context, rois = load_rois_for_position(
                            camera_id, position.position_id,
                            self._session.generation,
                            int(gens.get(camera_id, 0)) + 1,
                            position_provider=lambda pid: position,
                            roi_repository=roi_repo,
                            current_session_generation=(
                                self._session.generation),
                            context_generation=(
                                retained.context_generation
                                if retained is not None else 0),
                            operation_id=(
                                result.operation.operation_id
                                if result.operation is not None else ""),
                            source="goto",
                        )
                        gens[camera_id] = context.position_generation
                        self._roi_session_loaded.emit(camera_id, generation, {
                            "context": context,
                            "roi_dicts": [roi_to_dict(r) for r in rois],
                        })
                    else:
                        self._ptz_notice.emit(
                            camera_id, generation,
                            "ROI persistence unavailable; live view only.")
                except Exception as exc:
                    logger.warning("ROI session load failed: %s", exc)
                    self._ptz_notice.emit(
                        camera_id, generation,
                        f"ROI session failed: {exc}")
            elif result.state == RoiActivationState.CANCELLED:
                self._ptz_notice.emit(
                    camera_id, generation, f"Go To {position.name} cancelled."
                )
                self._roi_session_unlock.emit(camera_id, generation)
            else:
                detail = result.error.message if result.error is not None else "failed"
                self._ptz_notice.emit(
                    camera_id, generation, f"Go To failed: {detail}"
                )
                self._roi_session_unlock.emit(camera_id, generation)

        self._ptz_run(f"goto-{camera_id}", _goto)

    def _ptz_is_current_bg(self, camera_id: str, generation: int) -> bool:
        """Generation check safe to call from background threads."""
        try:
            return (
                camera_id == self._selected_camera_id
                and generation == self._session.generation
            )
        except Exception:
            return False

    def _ptz_reapply_context(self, camera_id: str) -> bool:
        """Re-apply the retained registry context to a fresh observer.

        Returns True when a context was re-applied. Safe to call from
        the GUI thread; never raises.
        """
        try:
            observer = self._observer
            retained = self._ptz_registry.get(camera_id)
            if (
                observer is None
                or retained is None
                or retained.session_generation != self._session.generation
            ):
                return False
            if getattr(observer, "camera_id", None) != camera_id:
                return False
            observer.set_active_position(
                retained.roi_set_ref or "default",
                retained.context_generation,
            )
            return True
        except Exception:
            logger.debug("PTZ context re-apply failed", exc_info=True)
            return False

    # -- PTZ marshaled slots (GUI thread, generation-guarded) -------------

    def _ptz_is_current(self, camera_id: str, generation: int) -> bool:
        return (
            camera_id == self._selected_camera_id
            and generation == self._session.generation
        )

    def _on_ptz_status(
        self, camera_id: str, generation: int, ptz_id: str, status
    ) -> None:
        if not self._ptz_is_current(camera_id, generation):
            return
        # Phase 9C: one shared PtzService fans monitor callbacks out per
        # PTZ controller. Without this guard the last unrelated PTZ in
        # the loop overwrote the selected camera's panel (cross-routing).
        # Only the binding's own ptz_id may drive this camera's panel.
        try:
            _entry = self._ptz_mapping_entry(camera_id)
            _bound = (getattr(_entry, "ptz_id", "") or "").strip() if _entry else ""
            if _bound and ptz_id != _bound:
                return
        except Exception:
            pass
        try:
            if self._ptz_panel is not None:
                self._ptz_panel.set_status(status)
        except RuntimeError:
            pass
        try:
            if status is None:
                self._set_ptz_top_state(f"{ptz_id} (unknown)")
            elif getattr(status, "error", None) is not None:
                self._set_ptz_top_state(f"{ptz_id} (error)")
            elif not getattr(status, "communication_ok", False):
                self._set_ptz_top_state(f"{ptz_id} (comm lost)")
            elif getattr(status, "moving", False):
                self._set_ptz_top_state(f"{ptz_id} (moving)")
            elif str(getattr(getattr(status, "calibration", None), "value", "")) == "active":
                self._set_ptz_top_state(f"{ptz_id} (calibrating)")
            elif getattr(status, "ready", False):
                self._set_ptz_top_state(f"{ptz_id} (ready)")
            else:
                self._set_ptz_top_state(f"{ptz_id} (not ready)")
        except Exception:
            pass

    def _on_ptz_operation(self, camera_id: str, generation: int, operation) -> None:
        if not self._ptz_is_current(camera_id, generation):
            return
        try:
            if self._ptz_panel is not None:
                self._ptz_panel.set_operation(operation)
        except RuntimeError:
            pass

    def _on_ptz_positions(
        self, camera_id: str, generation: int, positions
    ) -> None:
        if not self._ptz_is_current(camera_id, generation):
            return
        try:
            if self._pos_panel is not None:
                self._pos_panel.set_positions(list(positions or []))
                # Phase 11: a deleted position must take its session with
                # it — never keep displaying ROIs for a vanished record.
                canvas = getattr(self, "_roi_canvas", None)
                editor = (getattr(canvas, "editor", None)
                          if canvas is not None else None)
                if editor is not None:
                    known = {p.position_id for p in (positions or [])}
                    if editor.context.position_id not in known:
                        self._clear_roi_session(
                            "Position removed — select and reach a saved position")
        except RuntimeError:
            pass

    def _on_ptz_notice(self, camera_id: str, generation: int, message: str) -> None:
        if not self._ptz_is_current(camera_id, generation):
            return
        try:
            if self._ptz_panel is not None:
                self._ptz_panel.show_message(message)
        except RuntimeError:
            pass
        lowered = (message or "").lower()
        if "no ptz" in lowered or "not configured" in lowered:
            self._set_ptz_top_state("Not configured")
        elif (
            "failed" in lowered
            or "unavailable" in lowered
            or "unreachable" in lowered
            or "error" in lowered
        ):
            entry = self._ptz_mapping_entry(camera_id)
            ptz_id = (getattr(entry, "ptz_id", "") or "").strip() if entry else ""
            self._set_ptz_top_state(f"{ptz_id or 'PTZ'} (error)" if ptz_id else "Error")
        logger.info("PTZ notice cam=%s: %s", camera_id, message)

    def _on_ptz_active(self, camera_id: str, generation: int, name: str) -> None:
        if not self._ptz_is_current(camera_id, generation):
            return
        try:
            if self._ptz_panel is not None:
                self._ptz_panel.set_active_position(name)
        except RuntimeError:
            pass
        # Phase 10: the toolbar shows the reached position context; stale
        # overlays/results from the previous position were already dropped
        # by the retarget publish, so only refresh the label here.
        try:
            toolbar = getattr(self, "_roi_toolbar", None)
            if toolbar is not None and camera_id == self._selected_camera_id:
                toolbar.set_context_active(True, f"Camera: {camera_id}  Position: {name}  Context: Active")
        except RuntimeError:
            pass

    def _on_roi_tool_changed(self, tool) -> None:
        """Phase 10: toolbar tool selection (drawing is interaction-driven)."""
        try:
            self._status_label.setText(
                f"ROI tool: {tool.value if tool is not None else 'select'}")
        except RuntimeError:
            pass

    def _on_roi_toolbar_delete(self) -> None:
        """Phase 10: toolbar delete forwards to the ROI panel selection."""
        try:
            canvas = getattr(self, "_roi_canvas", None)
            if canvas is not None and canvas.delete_selected():
                return
            if hasattr(self, "_roi_panel") and self._roi_panel is not None:
                self._roi_panel._on_delete_roi()
        except RuntimeError:
            pass

    # -- Phase 10: position-bound ROI session ---------------------------
    # The canvas owns drawing/selection/drag/resize on the existing image
    # widget. Its RoiEditor session is installed only after a reached
    # position loads its validated ROI set (see _on_ptz_goto_requested);
    # camera switches and failures clear it so stale objects can never
    # display or persist under the wrong owner.

    def _roi_mapping(self):
        """Central widget<->image mapping for the configuration workspace."""
        from thermal_monitor.roi.coordinate_system import ViewportMapping

        image_widget = getattr(self, "_image_widget", None)
        if image_widget is None:
            return None
        image = getattr(image_widget, "_display_image", None)
        if image is None:
            return None
        try:
            iw, ih = image.width(), image.height()
        except Exception:
            return None
        temp = getattr(image_widget, "_temperature_image", None)
        if temp is not None:
            try:
                ih, iw = temp.shape[:2]
            except Exception:
                pass
        zoom = getattr(image_widget, "_zoom", None)
        pan = getattr(image_widget, "_pan_offset", None)
        try:
            px = float(pan.x()) if pan is not None else 0.0
            py = float(pan.y()) if pan is not None else 0.0
        except Exception:
            px, py = 0.0, 0.0
        return ViewportMapping(
            image_width=int(iw), image_height=int(ih),
            widget_width=max(1, image_widget.width()),
            widget_height=max(1, image_widget.height()),
            zoom=zoom, pan_x=px, pan_y=py)

    def _repaint_roi_session(self) -> None:
        try:
            canvas = getattr(self, "_roi_canvas", None)
            if canvas is None:
                return
            self._image_widget.set_roi_overlays(canvas.build_overlays())
        except RuntimeError:
            pass

    def _clear_roi_session(self, label: str = "No position") -> None:
        try:
            canvas = getattr(self, "_roi_canvas", None)
            if canvas is not None:
                canvas.set_editor(None)
            toolbar = getattr(self, "_roi_toolbar", None)
            if toolbar is not None:
                toolbar.set_context_active(False, label)
            panel = getattr(self, "_pos_panel", None)
            if panel is not None:
                try:
                    panel.set_active_position(None)
                except RuntimeError:
                    pass
            self._roi_session_active = False
        except RuntimeError:
            pass

    def _on_roi_session_loaded(self, camera_id: str, generation: int,
                               payload: object) -> None:
        """Install a validated position-bound editing session (GUI thread).

        The payload carries the immutable context built by
        ``load_rois_for_position`` in the Go To worker AFTER the PTZ
        reached the target. Installing it here atomically replaces the
        visible set; the previous session survives every failure above.
        """
        if not self._ptz_is_current(camera_id, generation):
            return
        try:
            data = dict(payload or {})
            context = data.get("context")
            if context is None:
                raise ValueError("ROI session payload has no context")
            from thermal_monitor.roi.editor import RoiEditor
            from thermal_monitor.roi.serialization import roi_from_dict

            rois = [roi_from_dict(item) for item in data.get("roi_dicts", [])]
            if [r.roi_id for r in rois] != list(context.roi_ids):
                raise ValueError("ROI session payload mismatches its context")
            gens = getattr(self, "_roi_session_gens", {})
            gens[camera_id] = context.position_generation
            editor = RoiEditor(context, rois)
            canvas = getattr(self, "_roi_canvas", None)
            if canvas is None:
                return
            canvas.set_editable(True)
            canvas.set_editor(editor)
            self._roi_session_active = True
            toolbar = getattr(self, "_roi_toolbar", None)
            position_name = next(
                (p.name for p in self._pos_panel._positions
                 if p.position_id == context.position_id),
                context.position_id,
            ) if getattr(self, "_pos_panel", None) is not None else context.position_id
            if toolbar is not None:
                if rois:
                    toolbar.set_context_active(
                        True, f"Camera: {camera_id}  PTZ: {context.ptz_id}  "
                        f"Position: {position_name}  "
                        f"ROI: {len(rois)} object(s)")
                else:
                    toolbar.set_context_active(
                        True, f"Camera: {camera_id}  PTZ: {context.ptz_id}  "
                        f"Position: {position_name}  "
                        f"ROI: No ROI configured for this position")
            panel = getattr(self, "_pos_panel", None)
            if panel is not None:
                try:
                    panel.set_active_position(context.position_id)
                except RuntimeError:
                    pass
            self._repaint_roi_session()
        except Exception as exc:
            # Never publish a partial context: the previous valid session
            # (if any) stays installed and on screen.
            logger.warning("ROI session install failed: %s", exc)
            self._ptz_notice.emit(camera_id, generation,
                                  f"ROI session failed: {exc}")
        else:
            try:
                self._roi_counts.emit(
                    camera_id, generation,
                    {str(context.position_id): len(rois)})
            except Exception:
                pass

    def _on_roi_session_unlock(self, camera_id: str, generation: int) -> None:
        """Re-enable the previous session after a failed/cancelled Go To."""
        if not self._ptz_is_current(camera_id, generation):
            return
        try:
            canvas = getattr(self, "_roi_canvas", None)
            editor = getattr(canvas, "editor", None) if canvas is not None else None
            toolbar = getattr(self, "_roi_toolbar", None)
            if editor is not None:
                if canvas is not None:
                    canvas.set_editable(True)
                if toolbar is not None:
                    toolbar.set_context_active(
                        True, f"Camera: {camera_id}  "
                        f"PTZ: {editor.context.ptz_id}  "
                        f"Position: {editor.context.position_id}  "
                        f"Context: Active")
            elif toolbar is not None:
                toolbar.set_context_active(False, "No position")
        except RuntimeError:
            pass

    def _on_roi_counts(self, camera_id: str, generation: int,
                       counts: object) -> None:
        """Refresh Position Table ROI counts (GUI thread, guarded)."""
        if not self._ptz_is_current(camera_id, generation):
            return
        try:
            panel = getattr(self, "_pos_panel", None)
            if panel is not None and isinstance(counts, dict):
                for position_id, count in counts.items():
                    panel.set_roi_count(position_id, int(count))
        except RuntimeError:
            pass

    def _roi_repository(self):
        """Phase 10 repository over the active database (None if unusable)."""
        database = getattr(self, "_database", None)
        if database is None:
            return None
        if not hasattr(database, "fetch_all") or not hasattr(
                database, "transaction"):
            return None
        try:
            from thermal_monitor.roi.repository import RoiDefinitionRepository

            return RoiDefinitionRepository(database)
        except Exception:
            return None

    def _on_roi_canvas_created(self, roi) -> None:
        self._repaint_roi_session()
        self._persist_roi_async("create", roi)

    def _on_roi_canvas_changed(self, roi) -> None:
        self._repaint_roi_session()
        self._persist_roi_async("update", roi)

    def _on_roi_canvas_deleted(self, roi_id: str) -> None:
        self._repaint_roi_session()
        self._persist_roi_async("delete", roi_id)

    def _on_roi_canvas_selected(self, roi_id) -> None:
        self._repaint_roi_session()

    def _on_roi_canvas_error(self, message: str) -> None:
        try:
            self._status_label.setText(f"ROI: {message}")
        except RuntimeError:
            pass

    def _persist_roi_async(self, operation: str, payload) -> None:
        """Save one ROI mutation on a worker thread (never GUI I/O)."""
        canvas = getattr(self, "_roi_canvas", None)
        editor = getattr(canvas, "editor", None) if canvas is not None else None
        if editor is None:
            return
        context = editor.context
        camera_id, generation = context.camera_id, self._session.generation

        def _save() -> None:
            repo = self._roi_repository()
            if repo is None:
                self._ptz_notice.emit(
                    camera_id, generation,
                    "ROI persistence unavailable; change kept for this session.")
                return
            try:
                from thermal_monitor.roi.serialization import roi_to_dict

                if operation == "create":
                    repo.create(payload)
                elif operation == "update":
                    repo.update(payload)
                elif operation == "delete":
                    repo.delete(payload)
                else:
                    return
            except Exception as exc:
                logger.warning("ROI persist %s failed: %s", operation, exc)
                self._ptz_notice.emit(
                    camera_id, generation, f"ROI save failed: {exc}")
                return
            # Refresh the session label count (read-only; editor unchanged).
            try:
                count = len(repo.list_for_position(
                    context.camera_id, context.position_id))
                self._ptz_notice.emit(
                    camera_id, generation,
                    f"ROI {operation}d; position now holds {count} object(s).")
                self._roi_counts.emit(
                    camera_id, generation, {context.position_id: count})
            except Exception:
                pass

        self._ptz_run(f"roi-{operation}-{camera_id}", _save)

    def _on_ptz_import_confirm(
        self, camera_id: str, generation: int, payload: object
    ) -> None:
        """GUI-thread REPLACE confirmation dialog for a waiting importer."""
        from PyQt6.QtWidgets import QMessageBox

        try:
            entries = list((payload or {}).get("entries", []))
            done = (payload or {}).get("done")
            answer = (payload or {}).get("answer")
        except Exception:
            return
        if not self._ptz_is_current(camera_id, generation):
            try:
                answer["confirmed"] = False
                done.set()
            except Exception:
                pass
            return
        detail = "\n".join(f"• {entry}" for entry in entries[:12])
        if len(entries) > 12:
            detail += f"\n• …and {len(entries) - 12} more"
        reply = QMessageBox.question(
            self,
            "Replace Existing Positions?",
            "Import would OVERWRITE these existing positions:\n\n"
            f"{detail}\n\nReplace them? (No keeps existing records.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        try:
            answer["confirmed"] = reply == QMessageBox.StandardButton.Yes
            done.set()
        except Exception:
            pass

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
            # Non-blocking: ownership leaves the (possibly dying) window
            # FIRST — window teardown must never delete this running
            # QThread (that aborts the process) — then Qt owns cleanup
            # once the worker's queued finished/failed signal quits the
            # thread's event loop. The retiring set keeps one Python
            # reference alive until finished fires: without it the
            # wrapper refcount would hit zero at return and SIP would
            # delete the still-running C++ object immediately (same
            # abort, different owner).
            try:
                thread.setParent(None)
                self._retiring_threads.add(thread)
                if worker is not None:
                    thread.finished.connect(worker.deleteLater)
                thread.finished.connect(thread.deleteLater)
                thread.finished.connect(
                    lambda _t=thread: self._retire_thread_done(_t)
                )
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

    def _retire_thread_done(self, thread) -> None:
        """Release a retired control thread after it finished.

        Runs on the GUI thread via the thread's own ``finished``
        signal, AFTER the chained ``deleteLater`` was queued: the C++
        object is already scheduled for deletion and the Python wrapper
        may follow it at GC. Only discards the registry reference; never
        touches the (possibly half-dead) thread object itself.
        """
        try:
            self._retiring_threads.discard(thread)
        except Exception:
            pass

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
        destroyed while running. With ``timeout_ms<=0`` ownership leaves
        the (possibly dying) window first so teardown can never delete
        the running QThread (which aborts the process).
        """
        thread, self._nuc_thread = self._nuc_thread, None
        worker, self._nuc_worker = self._nuc_worker, None
        if thread is None:
            return
        try:
            thread.quit()
        except RuntimeError:
            return
        if timeout_ms <= 0:
            try:
                thread.setParent(None)
                self._retiring_threads.add(thread)
                if worker is not None:
                    thread.finished.connect(worker.deleteLater)
                thread.finished.connect(thread.deleteLater)
                thread.finished.connect(
                    lambda _t=thread: self._retire_thread_done(_t)
                )
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
        self._clear_roi_session("No position")
        self._alarm_panel.set_camera("")
        self._stats_panel.clear()
        self._frame_info_panel.clear()
        self._set_top_connection_state(CameraConnectionState.DISCONNECTED)
        self._acq_panel.set_camera_identity(None)
        self._acq_panel.set_connection_state(CameraConnectionState.DISCONNECTED)

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
        # Phase 8 retarget guard: once a PTZ position context is active
        # for this camera, results produced under any other context
        # generation (in-flight frames from before the switch) are
        # dropped so old ROI results can never overwrite the new
        # display state. Results without context metadata (legacy path)
        # pass through unchanged.
        if frame_camera is not None:
            try:
                active = self._ptz_registry.get(frame_camera)
            except Exception:
                active = None
            if active is not None:
                try:
                    analysis_meta = getattr(result, "analysis_result", None)
                    result_meta = (
                        getattr(analysis_meta, "metadata", None)
                        if analysis_meta is not None
                        else None
                    )
                    result_gen = (
                        result_meta.get("context_generation")
                        if result_meta is not None
                        else None
                    )
                except Exception:
                    result_gen = None
                if (
                    result_gen is not None
                    and result_gen != active.context_generation
                ):
                    self._stale_results_dropped += 1
                    logger.debug(
                        "Result dropped (stale PTZ context %s != %s cam=%s)",
                        result_gen,
                        active.context_generation,
                        frame_camera,
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

        # Update image info (single owner: the Image Information side panel)
        if frame:
            self._frame_info_panel.update_from_frame(frame, result)

        # Update analysis results
        analysis = result.analysis_result
        if analysis is not None:
            self._stats_panel.update_from_analysis(analysis)
            self._roi_panel.update_live_stats(analysis)
            self._alarm_panel.update_live_alarms(result.alarm_result, analysis)
            # Phase 9B: track active alarms + persist trigger/clear edges
            # (worker-owned DB I/O; acquisition/GUI never block).
            try:
                self._record_alarm_history(result.alarm_result)
            except Exception:
                pass

        # Update ROI overlays
        self._update_roi_overlays()

    @pyqtSlot(object, object)
    def _on_rendered_frame(self, image, thumbnail) -> None:
        """Use the worker's single rendered image for both displays."""
        # Finder gets the full workspace frame (same object, no copy, no
        # second stream). Temperature Scale owns no View Finder anymore.
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

    # Connection workflow handlers (Connect -> Acquisition Setup ->
    # Connect... -> Camera Selection -> Connect -> Start)

    def _on_connect(self) -> None:
        """Handle Camera Control Connect button: open Acquisition Setup."""
        if not self._discovery_service:
            QMessageBox.warning(self, "Connect Failed", "Camera discovery service not available.")
            return
        self._open_acquisition_setup()

    def _open_acquisition_setup(self) -> None:
        """Show the Acquisition Setup dialog (reused, never recreated)."""
        if self._acq_setup_dialog is None:
            self._acq_setup_dialog = AcquisitionSetupDialog(
                theme_manager=self._theme,
                parent=self,
            )
            self._acq_setup_dialog.connect_requested.connect(
                self._on_setup_connect_requested
            )
            self._acq_setup_dialog.start_requested.connect(self._on_setup_start)
        params = self._acq_params
        self._acq_setup_dialog.set_params(
            params.get("fps", 9),
            params.get("averaging", "Off"),
            params.get("history_frames", 100),
            params.get("ir_scaling", "fast"),
        )
        self._acq_setup_dialog.show()
        self._acq_setup_dialog.raise_()
        self._acq_setup_dialog.activateWindow()
        # Refresh AFTER show: _refresh_setup_dialog only touches a visible
        # dialog, so the Start enablement always reflects current state.
        self._refresh_setup_dialog()

    def _setup_dialog_visible(self) -> bool:
        return (
            self._acq_setup_dialog is not None
            and self._acq_setup_dialog.isVisible()
        )

    def _setup_camera_summary(self) -> str:
        """One-line identity of the selected camera for the setup dialog."""
        camera_id = self._selected_camera_id
        if not camera_id:
            return "Camera: Not connected / selected"
        try:
            config = self._config_service.get_camera_config(camera_id)
        except Exception:
            config = None
        if config is None:
            return f"Camera: {camera_id}"
        identity = config.identity
        model = getattr(identity, "model", "") or "TV46L"
        serial = getattr(identity, "serial_number", "") or camera_id
        metadata = dict(getattr(config, "metadata", None) or {})
        ip = metadata.get("ip_address", "") or "—"
        return f"Camera: {model}-{serial}  |  IP: {ip}"

    def _refresh_setup_dialog(self) -> None:
        """Mirror selection + lifecycle into the open setup dialog (if any)."""
        if not self._setup_dialog_visible():
            return
        assert self._acq_setup_dialog is not None
        self._acq_setup_dialog.set_camera_summary(self._setup_camera_summary())
        self._acq_setup_dialog.set_connection_status(
            f"Status: {self._lifecycle.value.replace('_', ' ').title()}"
        )
        self._acq_setup_dialog.set_start_enabled(
            self._selected_camera_id is not None and can_start(self._lifecycle)
        )

    def _on_setup_connect_requested(self) -> None:
        """Acquisition Setup [Connect...]: open the Camera Selection dialog."""
        if not self._discovery_service:
            QMessageBox.warning(self, "Connect Failed", "Camera discovery service not available.")
            return

        # Create and show camera selection dialog (existing GVCP discovery
        # path, reused unchanged; stacked above the setup dialog).
        if self._camera_selection_dialog is None:
            self._camera_selection_dialog = CameraSelectionDialog(
                discovery_service=self._discovery_service,
                theme_manager=self._theme,
                parent=self._acq_setup_dialog
                if self._setup_dialog_visible()
                else self,
                fps_lookup=self._discovered_camera_fps,
            )
            self._camera_selection_dialog.camera_selected.connect(self._on_camera_selected_from_dialog)
            self._camera_selection_dialog.discovery_finished.connect(self._restore_connect_cursor)
            self._camera_selection_dialog.discovery_failed.connect(self._on_discovery_failed)
            self._camera_selection_dialog.finished.connect(self._on_connect_dialog_finished)

        self._camera_selection_dialog.show()
        self._camera_selection_dialog.raise_()
        self._camera_selection_dialog.activateWindow()
        self._set_top_connection_state(CameraConnectionState.CONNECTING)
        self._acq_panel.set_connection_state(CameraConnectionState.CONNECTING)
        self._status_conn.setText("Connection: Discovering...")
        self._status_label.setText("Discovering cameras...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

    def _discovered_camera_fps(self, discovered_camera) -> int:
        """Configured FPS for a discovered camera (selection-row naming)."""
        try:
            config = self._config_service.get_camera_config(
                discovered_camera.camera_id
            )
            metadata = dict(getattr(config, "metadata", None) or {})
            return int(metadata.get("frame_rate", 9))
        except (TypeError, ValueError, AttributeError):
            return 9

    def _on_setup_start(self) -> None:
        """Acquisition Setup [Start]: apply params, close, start acquisition.

        The Start itself flows through the unchanged ``_on_start_acquisition``
        pipeline (transport verification + observer attach + lifecycle
        STARTING -> ACQUIRING); the dialog only supplies the parameters.
        """
        if self._acq_setup_dialog is None:
            return
        if not can_start(self._lifecycle):
            self._refresh_setup_dialog()
            return
        values = self._acq_setup_dialog.values()
        self._apply_acquisition_params(
            values["fps"], values["averaging"], values["history_frames"],
            values.get("ir_scaling", "fast"),
        )
        self._acq_setup_dialog.accept()
        self._on_start_acquisition()

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
            # Phase 9C: discovery persistence must never clobber the
            # camera-to-PTZ station association. A connect previously
            # overwrote ptz_id with "" (verified: HEAD PTZ_08 -> "" in
            # the working tree), which made resolve_binding() return
            # None and froze the panel at "No PTZ configured" even
            # though acquisition was streaming. Preserve every ptz_*
            # field from the existing mapping entry.
            _existing_ptz = self._ptz_mapping_entry(camera_id)
            self._config_manager.save_camera_mapping(
                CameraMappingConfig(
                    camera_id=camera_id,
                    serial_number=discovered_camera.serial_number,
                    enabled=updated_config.enabled,
                    name=updated_config.name or camera_id,
                    target_fps=(
                        getattr(_existing_ptz, "target_fps", None)
                        if _existing_ptz is not None
                        else None
                    ),
                    ip_address=discovered_camera.ip_address or "",
                    device_identifier=discovered_camera.device_identifier or "",
                    ptz_id=(
                        getattr(_existing_ptz, "ptz_id", "") or ""
                    )
                    if _existing_ptz is not None
                    else "",
                    ptz_endpoint=(
                        getattr(_existing_ptz, "ptz_endpoint", "") or ""
                    )
                    if _existing_ptz is not None
                    else "",
                    ptz_min_pan=(
                        getattr(_existing_ptz, "ptz_min_pan", None)
                        if _existing_ptz is not None
                        else None
                    ),
                    ptz_max_pan=(
                        getattr(_existing_ptz, "ptz_max_pan", None)
                        if _existing_ptz is not None
                        else None
                    ),
                    ptz_min_tilt=(
                        getattr(_existing_ptz, "ptz_min_tilt", None)
                        if _existing_ptz is not None
                        else None
                    ),
                    ptz_max_tilt=(
                        getattr(_existing_ptz, "ptz_max_tilt", None)
                        if _existing_ptz is not None
                        else None
                    ),
                )
            )

        # CONNECT: establish control + acquisition in one serialized
        # background operation through the single _activate_camera
        # pipeline (including tearing down the previously selected
        # camera — never two active pipelines).
        # background operation. The GUI shows CONNECTING/DISCONNECTING
        # immediately; failures land in ERROR with partial resources
        # cleaned up and the user informed.
        self._activate_camera(camera_id, connect=True, config=updated_config)

    def _on_disconnect(self) -> None:
        """Handle Disconnect button: safe shutdown of the current camera.

        Disconnecting a running camera automatically performs the full
        safe shutdown sequence (observer -> consumer -> acquisition ->
        child process -> SHM detach) in a background operation. The GUI
        shows DISCONNECTING immediately; control-worker shutdown uses
        bounded waits so a running QThread is never destroyed.
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
        # Non-blocking quit here (live feed protection); the deterministic
        # bounded wait happens below, AFTER the background teardown has
        # started, so an in-flight focus/NUC request aborts as soon as the
        # handle reaches STOPPING instead of settling to completion.
        self._stop_focus_worker()
        self._focus_camera_id = None
        self._acq_panel.set_focus_enabled(False, "Camera not running")
        self._stop_nuc_worker(timeout_ms=0)
        self._nuc_camera_id = None
        self._acq_panel.set_nuc_enabled(False, "Camera not running")

        # Renew the epoch so any late result from this camera is stale.
        self._session.renew(camera_id)
        self._image_widget.set_session(camera_id)
        self._vl_widget.set_session(camera_id)
        self._image_widget.clear()
        self._vl_widget.clear()
        self._set_finder_thumbnail(None)

        # Crash-safety: stop stats polling BEFORE the background teardown
        # starts so no queued QTimer callback can enter camera_stats()
        # (pipe/SHM reads) after teardown begins. Restarted in _on_bg_done.
        self._stats_timer.stop()

        self._set_lifecycle(CameraConnectionState.DISCONNECTING)
        self._status_label.setText(f"Disconnecting {camera_id}...")
        started = self._run_background(
            "disconnect",
            lambda: self._teardown_camera_blocking(camera_id, generation),
        )
        if not started:
            self._pending_disconnect = True

        # Deterministic control-worker shutdown (bounded waits). The
        # teardown above drives the handle to STOPPING, which aborts any
        # in-flight focus/NUC request quickly; these waits then return
        # fast and guarantee no QThread is destroyed while running.
        self._stop_focus_worker(timeout_ms=8000)
        self._stop_nuc_worker(timeout_ms=8000)

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
            setup_summary = self._setup_camera_summary()
        except Exception:
            setup_summary = "<unreadable>"
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
            "panel_camera_id=%s panel_serial=%s setup=%s "
            "widget_camera_id=%s lifecycle=%s session_camera_id=%s "
            "runtime_camera_ids=%s generation=%s observer_camera_id=%s",
            ui_id,
            ui_serial,
            ui_name,
            panel_id,
            panel_serial,
            setup_summary,
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

        # Update metadata with the Acquisition Setup parameters (the
        # Start pipeline reads them from here, never from panel widgets).
        metadata = dict(config.metadata or {})
        metadata["frame_rate"] = int(self._acq_params.get("fps", 9))
        metadata["averaging"] = str(self._acq_params.get("averaging", "Off"))
        metadata["history_frames"] = int(self._acq_params.get("history_frames", 100))
        metadata["ir_display_scaling"] = str(self._acq_params.get("ir_scaling", "fast") or "fast")
        self._apply_ir_scaling_to_views(metadata["ir_display_scaling"])

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
            # Re-apply a retained PTZ position context to the fresh
            # pipeline (observer restarts reset the override). The stored
            # generation is re-applied exactly so in-flight consistency
            # is preserved; a session mismatch means _begin_session
            # already cleared it.
            self._ptz_reapply_context(camera_id)
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
        # Phase 9C: acquisition is the operator-visible "connected"
        # state from the issue report. Re-attach the camera's PTZ here
        # (idempotent: shared service reused, identical binding
        # registration is a no-op, monitor starts exactly once). This
        # covers connects that predated a config fix and any attach
        # dropped by a generation race, without ever auto-moving.
        try:
            self._ptz_attach_async(camera_id, self._session.generation)
        except Exception:
            logger.debug("PTZ attach after acquisition start failed", exc_info=True)

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

    def _apply_acquisition_params(self, fps: int, averaging: str, history_frames: int,
                                    ir_scaling: str = "fast") -> None:
        """Persist Acquisition Setup parameters to the selected camera config.

        Same metadata keys the Start pipeline has always used
        (``frame_rate``) plus ``averaging`` / ``history_frames`` carried
        alongside (history preserves the previous buffer-length semantic)
        plus ``ir_display_scaling`` (display sampling only — thermal data,
        calibration, palette and VL rendering untouched). The IR scaling
        is applied to the IR views here, at connect/Start time only.
        """
        mode = str(ir_scaling or "fast").lower()
        if mode not in ("fast", "smooth"):
            mode = "fast"
        self._acq_params = {
            "fps": int(fps),
            "averaging": str(averaging),
            "history_frames": int(history_frames),
            "ir_scaling": mode,
        }
        self._apply_ir_scaling_to_views(mode)
        if not self._selected_camera_id:
            return
        config = self._config_service.get_camera_config(self._selected_camera_id)
        if not config:
            return
        metadata = dict(config.metadata or {})
        metadata["frame_rate"] = int(fps)
        metadata["averaging"] = str(averaging)
        metadata["history_frames"] = int(history_frames)
        metadata["ir_display_scaling"] = mode
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

    def _apply_ir_scaling_to_views(self, mode: str) -> None:
        """Apply the connect-time IR display sampling to the IR views.

        Display only: the central thermal widget and the View Finder. The
        VL widget, render worker, acquisition, calibration, palette and
        recording paths are untouched.
        """
        normalized = str(mode or "fast").lower()
        if normalized not in ("fast", "smooth"):
            normalized = "fast"
        try:
            self._image_widget.set_ir_scaling(normalized)
        except AttributeError:
            pass
        try:
            self._finder_widget.set_ir_scaling(normalized)
        except AttributeError:
            pass

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
        # Phase 11: the Configuration display has ONE overlay owner — the
        # position-bound session. The legacy camera-global path must not
        # paint (and must not clobber the session) in either state.
        if getattr(self, "_roi_session_active", False):
            return
        try:
            self._image_widget.set_roi_overlays([])
        except RuntimeError:
            pass
        return

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

    # -- Phase 9B: alarm detail / history / persistence -------------------

    def _alarm_store_lazy(self):
        """Get-or-create the threaded alarm-history writer (never on GUI I/O)."""
        if self._alarm_store is not None:
            return self._alarm_store
        if self._database is None:
            return None
        try:
            from thermal_monitor.storage.alarm_store import AlarmHistoryStore

            store = AlarmHistoryStore(self._database)
            store.start()
            self._alarm_store = store
            return store
        except Exception:
            logger.debug("Alarm history store unavailable", exc_info=True)
            return None

    def _record_alarm_history(self, alarm_result) -> None:
        """Track live alarm events, update the top count, persist edges.

        Called on the GUI thread per processed frame; only enqueues
        evaluator transition events (trigger/clear) with station context
        attached — repeated samples of an ACTIVE alarm never write.
        """
        if alarm_result is None:
            return
        try:
            active = tuple(getattr(alarm_result, "active_alarms", ()) or ())
            events = tuple(getattr(alarm_result, "events", ()) or ())
        except Exception:
            return
        camera_id = self._selected_camera_id or getattr(alarm_result, "camera_id", "")
        enriched: list = []
        for event in events:
            try:
                eid = getattr(event, "event_id", "") or ""
                rule_id = getattr(event, "rule_id", "") or ""
                event = self._enrich_alarm_event(event, camera_id)
                if eid.startswith("clear_"):
                    self._active_alarm_events.pop(rule_id, None)
                elif eid.startswith("alarm_"):
                    self._active_alarm_events[rule_id] = event
                else:
                    continue
                enriched.append(event)
            except Exception:
                continue
        # Rules that vanished from active without a clear event (e.g. rule
        # set emptied) leave tracking so the count mirrors the evaluator.
        stale = [r for r in self._active_alarm_events if r not in active]
        for rule_id in stale:
            self._active_alarm_events.pop(rule_id, None)
        self._set_alarm_top_count(len(self._active_alarm_events))
        store = self._alarm_store_lazy()
        if store is not None:
            for event in enriched:
                try:
                    store.record_event(event)
                except Exception:
                    pass
        self._refresh_top_status()

    def _enrich_alarm_event(self, event, camera_id: str):
        """Attach PTZ/position context (returns enriched copy; frozen-safe)."""
        try:
            meta = dict(getattr(event, "metadata", None) or {})
        except Exception:
            return event
        if meta.get("ptz_id"):
            return event  # producer already attached context
        try:
            import dataclasses

            entry = self._ptz_mapping_entry(camera_id) if camera_id else None
            ptz_id = (getattr(entry, "ptz_id", "") or "").strip() if entry else ""
            position = None
            try:
                ctx = self._ptz_registry.get(camera_id) if camera_id else None
                position = getattr(ctx, "roi_set_ref", None)
            except Exception:
                position = None
            merged = dict(meta)
            merged.update({"ptz_id": ptz_id, "position_id": position or ""})
            return dataclasses.replace(event, metadata=merged)
        except Exception:
            return event

    def _on_alarm_activated(self, rule_id: str) -> None:
        """Double-click a rule -> open the read-only detail window."""
        event = self._active_alarm_events.get(rule_id)
        camera_id = self._selected_camera_id or ""
        rule = None
        try:
            analysis = self._config_service.get_analysis_config(camera_id) if camera_id else None
            rule = (analysis.alarm_rules.get(rule_id) if analysis else None)
        except Exception:
            rule = None
        if event is None and rule is None:
            return  # unknown rule; nothing to show (never fabricate)
        try:
            from thermal_monitor.ui.windows.alarm_detail_window import AlarmDetailWindow

            meta = dict(getattr(event, "metadata", None) or {}) if event is not None else {}
            window = AlarmDetailWindow(
                event if event is not None else self._synthetic_rule_event(rule, camera_id),
                rule=rule,
                camera_id=camera_id,
                ptz_id=str(meta.get("ptz_id") or self._live_ptz_id(camera_id)),
                position_id=str(
                    getattr(event, "position_id", None) or meta.get("position_id") or ""
                ),
                parent=self,
            )
            window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._alarm_detail_window = window
            window.show()
            window.raise_()
            window.activateWindow()
        except RuntimeError:
            pass

    def _synthetic_rule_event(self, rule, camera_id: str):
        """Placeholder snapshot for a configured-but-inactive rule."""
        from thermal_monitor.core.models import AlarmEvent, AlarmSeverity

        return AlarmEvent(
            event_id=f"rule_{rule.rule_id}",
            rule_id=rule.rule_id,
            camera_id=camera_id,
            roi_id=rule.roi_id,
            severity=rule.severity or AlarmSeverity.INFO,
            measured_value=0.0,
            threshold_value=rule.threshold or 0.0,
            timestamp=0.0,
            frame_sequence=0,
            metadata={"status": "CONFIGURED (not active)"},
        )

    def _live_ptz_id(self, camera_id: str) -> str:
        try:
            entry = self._ptz_mapping_entry(camera_id) if camera_id else None
            return (getattr(entry, "ptz_id", "") or "").strip() if entry else ""
        except Exception:
            return ""

    def _on_alarm_history_requested(self) -> None:
        """Open the bounded alarm-history window (worker-loaded)."""
        try:
            from thermal_monitor.ui.windows.alarm_history_window import AlarmHistoryWindow

            if self._alarm_history_window is not None:
                try:
                    self._alarm_history_window.show()
                    self._alarm_history_window.raise_()
                    self._alarm_history_window.activateWindow()
                    self._alarm_history_window.refresh()
                    return
                except RuntimeError:
                    self._alarm_history_window = None
            window = AlarmHistoryWindow(database=self._database, parent=self)
            window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            try:
                window.finished.connect(self._on_alarm_history_closed)
            except Exception:
                pass
            self._alarm_history_window = window
            window.show()
            window.raise_()
            window.activateWindow()
        except RuntimeError:
            pass

    def _on_alarm_history_closed(self) -> None:
        self._alarm_history_window = None

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

        Crash-safety: while ANY background camera operation owns the
        pipeline (connect/switch/disconnect/teardown), no transport probe
        runs here. The teardown thread closes pipes/SHM; even though the
        handle serializes those against polls, skipping probes during
        teardown removes the GUI thread from the shutdown path entirely.
        """
        if self._bg_busy():
            return
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
                self._switch_camera(cameras[0].identity.camera_id)
        # Correct any lifecycle drift from while inactive (e.g. observer
        # detached on mode switch while transport kept running), then
        # resume the status ticker stopped on deactivation.
        self._reconcile_lifecycle()
        if not self._stats_timer.isActive():
            self._stats_timer.start(1000)

    def on_mode_deactivated(self) -> None:
        """Called when configuration mode is deactivated.

        Visible-transition fast path: the GUI thread only cuts the
        display path (observer signals, session epoch, timers, render
        workers) and returns immediately. The observer/consumer join
        runs in a background daemon thread. The camera child process
        keeps running so a mode switch back (or Live mode sharing the
        runtime) is fast.
        """
        import time as _time

        _t0 = _time.perf_counter_ns()
        self._save_shelf_state()
        self._restore_connect_cursor()
        if self._camera_selection_dialog is not None:
            try:
                self._camera_selection_dialog.close()
            except RuntimeError:
                pass
        if self._acq_setup_dialog is not None:
            try:
                self._acq_setup_dialog.close()
            except RuntimeError:
                pass
        # Detach the display path; the camera child process keeps running
        # so a mode switch back (or Live mode sharing the runtime) is fast.
        detached = self._detach_observer_fast()
        # Render workers, two passes (see LiveModeWidget): wake both
        # without waiting, then join bounded — Qt must never delete a
        # still-running QThread during window teardown.
        _feed_widgets = [
            widget
            for widget in (
                getattr(self, "_image_widget", None),
                getattr(self, "_vl_widget", None),
            )
            if widget is not None
        ]
        for widget in _feed_widgets:
            try:
                widget.prepare_for_transition()
            except RuntimeError:
                pass
            except Exception:
                logger.debug("Render detach failed", exc_info=True)
        for widget in _feed_widgets:
            try:
                if not widget.wait_for_renderer(timeout_ms=200):
                    logger.warning(
                        "Render worker still running after 200 ms; "
                        "leaving it to quit itself on render completion"
                    )
            except RuntimeError:
                pass
            except Exception:
                logger.debug("Render reap failed", exc_info=True)
        try:
            self._stats_timer.stop()
        except RuntimeError:
            pass
        # PTZ services shut down off the GUI thread (never blocks the
        # visible transition); panels are cleared synchronously above
        # via _ptz_clear_panels inside teardown, and re-attach on return.
        self._ptz_teardown_all_async()
        self._stop_observer_background(detached)
        logger.info(
            "[MODE-TRANSITION] config_detach_complete detach_ms=%.1f",
            (_time.perf_counter_ns() - _t0) / 1e6,
        )

    def _shutdown_alarm_phase9b(self) -> None:
        """Stop Phase 9B alarm workers/windows (idempotent, bounded)."""
        for name in ("_alarm_detail_window", "_alarm_history_window"):
            window = getattr(self, name, None)
            try:
                if window is not None:
                    window.close()
            except RuntimeError:
                pass
            setattr(self, name, None)
        store, self._alarm_store = self._alarm_store, None
        if store is not None:
            try:
                store.stop(timeout_s=5.0)
            except Exception:
                pass

    def closeEvent(self, event) -> None:
        # Transition fast path: NEVER block the GUI thread waiting for
        # control workers here. Quit is requested (non-blocking) and the
        # threads quit themselves on operation completion via their
        # finished -> deleteLater chain; the observer detach above stops
        # its consumer in the background. The Launcher is already visible
        # while all of this drains.
        try:
            self._stop_focus_worker(timeout_ms=0)
            self._stop_nuc_worker(timeout_ms=0)
        except RuntimeError:
            pass
        # Phase 9B: alarm/database workers stop bounded; alarm windows
        # close so no stale callback can fire after destruction.
        try:
            self._shutdown_alarm_phase9b()
        except Exception:
            pass
        # PTZ teardown runs inside on_mode_deactivated (async, non-blocking).
        self.on_mode_deactivated()
        try:
            self._config_service.remove_camera_change_callback(self._on_camera_config_changed)
            self._config_service.remove_analysis_change_callback(self._on_analysis_config_changed)
        except Exception:
            pass
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
        database=None,
    ) -> None:
        super().__init__()

        self._config_service = config_service
        self._mode_service = mode_service
        self._runtime_service = runtime_service
        self._discovery_service = discovery_service
        self._theme = theme_manager
        self._config_manager = config_manager
        self._database = database
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
            database=self._database,
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
        # hiding), then feed mode (display only), then workspace zoom.
        view_menu = menu_bar.addMenu("View")
        for action in self._config_widget.panel_toggle_actions():
            view_menu.addAction(action)
        view_menu.addSeparator()
        ir_action = QAction("IR View", self)
        ir_action.triggered.connect(lambda: self._config_widget.set_irvl_mode("ir"))
        view_menu.addAction(ir_action)
        vl_action = QAction("VL View", self)
        vl_action.triggered.connect(lambda: self._config_widget.set_irvl_mode("vl"))
        view_menu.addAction(vl_action)
        both_action = QAction("IR + VL", self)
        both_action.triggered.connect(lambda: self._config_widget.set_irvl_mode("both"))
        view_menu.addAction(both_action)
        view_menu.addSeparator()
        zoom_in_action = QAction("Zoom In", self)
        zoom_in_action.setShortcut("+")
        zoom_in_action.triggered.connect(self._config_widget.zoom_in)
        view_menu.addAction(zoom_in_action)
        zoom_out_action = QAction("Zoom Out", self)
        zoom_out_action.setShortcut("-")
        zoom_out_action.triggered.connect(self._config_widget.zoom_out)
        view_menu.addAction(zoom_out_action)
        fit_action = QAction("Fit to Window", self)
        fit_action.setShortcut("0")
        fit_action.triggered.connect(self._config_widget.zoom_fit)
        view_menu.addAction(fit_action)
        one_action = QAction("1:1", self)
        one_action.setShortcut("1")
        one_action.triggered.connect(self._config_widget.zoom_one_to_one)
        view_menu.addAction(one_action)
        pan_action = QAction("Reset Pan", self)
        pan_action.triggered.connect(self._config_widget.zoom_reset_pan)
        view_menu.addAction(pan_action)
        self._config_widget._zoom_menu_actions = {
            "zoom_in": zoom_in_action,
            "zoom_out": zoom_out_action,
            "fit": fit_action,
            "one": one_action,
            "pan": pan_action,
        }

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
        from thermal_monitor.ui.palettes import PALETTE_DISPLAY, PALETTE_ORDER
        for palette in PALETTE_ORDER:
            action = QAction(PALETTE_DISPLAY[palette], self)
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
