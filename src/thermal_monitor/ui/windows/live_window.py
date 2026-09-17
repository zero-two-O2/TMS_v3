"""
ui.windows.live_window -- Live mode window (3x3 monitoring wall).

Viewport-filling 3 columns by 3 rows wall. Camera POSITION is fixed:
tile N always shows camera position N (row N//3, col N%3), disconnects
never move, remove or reorder a tile. The ninth cell (2,2) is a permanent
LIVE STATISTICS panel, never a camera. Each camera tile shows its IR and
VL feeds side by side simultaneously (16 feeds total, no tabs, no
carousel, no selection, no IR/VL toggle).

Tile pixel size follows the available Live viewport through a resize/show
refit that derives the largest fitting 4:3 feed size (see
LiveModeWidget.compute_feed_size). Hovering a tile or feed switches the
statistics panel to that camera via Qt enter/leave events only.

Presentation only: acquisition, frame transport, processing,
calibration, recording, NUC, focus, render workers and the
latest-wins frame path are consumed unchanged. Geometry is resolved
by the refit on resize and initialization only. The per-frame path
never resizes, rebuilds, recreates or restyles anything.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Optional

from PyQt6.QtCore import QEvent, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QScrollArea,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
)

import numpy as np

import threading

import logging
from pathlib import Path

from thermal_monitor.core.models import AnalysisConfig, TemperatureUnit
from thermal_monitor.core.frame_latency import (
    get_default_tracker as _latency_tracker,
    latency_enabled as _latency_enabled,
)
from thermal_monitor.processing import ProcessingResult
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.observer import ObserverService
from thermal_monitor.services.runtime import CameraRuntimeService
from thermal_monitor.ui.modes.observer_image import LiveThermalWidget
from thermal_monitor.ui.modes.vl_image import VlImageWidget
from thermal_monitor.ui.theme import ThemeManager
from thermal_monitor.ui.theme.properties import (
    set_role,
    set_status,
    set_tile_state,
    set_variant,
)

logger = logging.getLogger(__name__)


_UNIT_SYMBOLS = {
    TemperatureUnit.CELSIUS: "C",
    TemperatureUnit.FAHRENHEIT: "F",
    TemperatureUnit.KELVIN: "K",
}


class LiveTileState(str, Enum):
    """High-level lifecycle state shown on a live camera tile."""

    NOT_AVAILABLE = "not_available"
    READY = "ready"
    STARTING = "starting"
    RECONNECTING = "reconnecting"
    RUNNING = "running"
    ERROR = "error"


#: LiveTileState mapped onto the global status vocabulary (the central
#: stylesheet resolves the actual colors for every theme).
_STATE_STATUS = {
    LiveTileState.NOT_AVAILABLE: "not_available",
    LiveTileState.READY: "ready",
    LiveTileState.STARTING: "starting",
    LiveTileState.RECONNECTING: "reconnecting",
    LiveTileState.RUNNING: "running",
    LiveTileState.ERROR: "error",
}

_STATE_TEXT = {
    LiveTileState.NOT_AVAILABLE: "NOT AVAILABLE",
    LiveTileState.READY: "READY",
    LiveTileState.STARTING: "STARTING",
    LiveTileState.RECONNECTING: "RECONNECTING",
    LiveTileState.RUNNING: "LIVE",
    LiveTileState.ERROR: "ERROR",
}


# Fixed camera POSITIONS (non-negotiable wall assignment). Slot S shows
# camera S+1 at row S//3, col S%3; the ninth cell (2,2) is the permanent
# statistics panel, never a camera. Tile pixel size is NOT fixed: on every
# resize and on first show the wall derives the largest 4:3 feed size that
# fits the available viewport (see LiveModeWidget._refit_wall) and centers
# the resulting 3x3 wall.
FIXED_CAMERA_SLOTS = 8
GRID_COLUMNS = 3
GRID_ROWS = 3
STATS_GRID_ROW = 2
STATS_GRID_COL = 2

# Minimal wall chrome (pixels). Gaps stay tiny so the wall reads almost
# continuous; feeds receive everything else.
WALL_OUTER_MARGIN = 2
WALL_GRID_SPACING = 2
WALL_GRID_MARGIN = 2
TILE_MARGIN = 2
TILE_SPACING = 2
FEED_GAP = 2
FEED_TAG_SPACING = 1

# Deliberate minimums only (scroll threshold for unusually small
# windows). Normal monitors never hit these; the refit grows feeds to
# fill whatever viewport is available.
TILE_MIN_WIDTH = 248
TILE_MIN_HEIGHT = 150
FEED_MIN_WIDTH = 112
FEED_MIN_HEIGHT = 84

# Fallback chrome estimates used before the first layout pass measures
# the real row heights (theme fonts can shift these by a few pixels).
_FALLBACK_TILE_CHROME_HEIGHT = 64
_FALLBACK_HEADER_BAR_HEIGHT = 26


class LiveCameraTile(QWidget):
    """Independent live tile for one fixed camera position (1-8).

    Slot index never changes: tile N always shows camera position N.
    A disconnected camera keeps its tile, its identity header and two
    clearly marked unavailable feeds; the grid never collapses.

    Hovering the tile or one of its feeds emits :attr:`hover_changed`
    with ``(slot_index, feed)`` where feed is ``"ir"``, ``"vl"`` or None
    (tile chrome); leaving the tile emits ``(None, None)``.
    """

    hover_changed = pyqtSignal(object, object)
    cursor_temperature_changed = pyqtSignal(float)

    def __init__(
        self,
        slot_index: int,
        camera_id: str | None = None,
        name: str = "",
        serial: str = "",
        theme_manager: Optional[ThemeManager] = None,
    ) -> None:
        super().__init__()
        self._slot_index = slot_index  # 0-7, permanent
        self._camera_id = camera_id
        self._name = name or (f"CAM {slot_index + 1:02d}" if camera_id else "")
        self._serial = serial
        self._ip_address = ""
        self._theme = theme_manager
        set_role(self, "tile")
        # Expanding tile: the grid gives every tile an equal share of the
        # viewport. Minimums only guard unusually small windows (scroll).
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(TILE_MIN_WIDTH, TILE_MIN_HEIGHT)

        self._latest_result: ProcessingResult | None = None
        self._frames_received = 0
        self._latest_sequence = -1
        self._last_age_ms: float | None = None
        self._last_fps: float | None = None
        self._last_temp: float | None = None
        self._cursor_temp: float | None = None
        self._last_temp_unit: str = "C"
        self._ir_live = False
        self._vl_live = False
        self._state = LiveTileState.NOT_AVAILABLE if camera_id is None else LiveTileState.READY
        self._error_message: str | None = None

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(TILE_MARGIN, TILE_MARGIN, TILE_MARGIN, TILE_MARGIN)
        layout.setSpacing(TILE_SPACING)

        # Header: permanent positional identity, always one compact row.
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        self._num_label = QLabel(f"CAM {self._slot_index + 1}")
        set_role(self._num_label, "strong")
        self._name_label = QLabel(self._name if self._name else "Not configured")
        set_role(self._name_label, "muted")
        self._name_label.setWordWrap(False)
        # Identity text never drives tile width (feeds do); full detail
        # stays available in the tooltip.
        self._name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._pos_label = QLabel(f"POS {self._slot_index + 1}")
        set_role(self._pos_label, "muted")
        header.addWidget(self._num_label)
        header.addWidget(self._name_label, 1)
        header.addWidget(self._pos_label)
        layout.addLayout(header)

        # Feeds: IR and VL side by side, always both visible. The wall
        # sets an exact 4:3 fixed size on resize (largest that fits the
        # viewport), so the paint routines fill their widgets fully and
        # images never distort at any window size.
        feeds = QHBoxLayout()
        feeds.setContentsMargins(0, 0, 0, 0)
        feeds.setSpacing(FEED_GAP)

        ir_column = QVBoxLayout()
        ir_column.setContentsMargins(0, 0, 0, 0)
        ir_column.setSpacing(FEED_TAG_SPACING)
        self._ir_tag = QLabel("IR")
        set_role(self._ir_tag, "muted")
        self._ir_tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_widget = LiveThermalWidget()
        self._image_widget.setFixedSize(FEED_MIN_WIDTH, FEED_MIN_HEIGHT)
        self._image_widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        ir_column.addWidget(self._ir_tag)
        ir_column.addWidget(self._image_widget, 1)

        vl_column = QVBoxLayout()
        vl_column.setContentsMargins(0, 0, 0, 0)
        vl_column.setSpacing(FEED_TAG_SPACING)
        self._vl_tag = QLabel("VL")
        set_role(self._vl_tag, "muted")
        self._vl_tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._vl_widget = VlImageWidget()
        self._vl_widget.setFixedSize(FEED_MIN_WIDTH, FEED_MIN_HEIGHT)
        self._vl_widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        vl_column.addWidget(self._vl_tag)
        vl_column.addWidget(self._vl_widget, 1)

        feeds.addLayout(ir_column, 1)
        feeds.addLayout(vl_column, 1)
        layout.addLayout(feeds, 1)

        # Status: one compact row, state plus existing metrics only.
        status = QHBoxLayout()
        status.setContentsMargins(0, 0, 0, 0)
        status.setSpacing(6)
        self._state_label = QLabel(_STATE_TEXT[self._state])
        self._apply_state_style()
        self._fps_label = QLabel("-- fps")
        set_role(self._fps_label, "mono")
        self._temp_label = QLabel("--")
        set_role(self._temp_label, "readout")
        self._age_label = QLabel("-- ms")
        set_role(self._age_label, "mono")
        self._sequence_label = QLabel("--")
        set_role(self._sequence_label, "mono")
        # PTZ readout: compact status text fed by the wall's read-only
        # PTZ monitor (never commands anything). Clips like the rest.
        self._ptz_label = QLabel("")
        set_role(self._ptz_label, "mono")
        self._ptz_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        # Metric readouts never drive tile width (feeds do); they clip
        # instead of stretching narrow tiles. The state badge keeps its
        # preferred width so LIVE/ERROR stays fully readable.
        for readout in (
            self._fps_label,
            self._temp_label,
            self._age_label,
            self._sequence_label,
            self._ptz_label,
        ):
            readout.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        status.addWidget(self._state_label)
        status.addWidget(self._fps_label)
        status.addWidget(self._temp_label)
        status.addWidget(self._ptz_label)
        status.addStretch(1)
        status.addWidget(self._age_label)
        status.addWidget(self._sequence_label)
        layout.addLayout(status)

        self._refresh_identity()

        # Hover detection is event-driven only (no polling): the tile
        # watches its own boundaries plus each feed widget so the wall
        # can tell IR apart from VL. Events are never consumed here.
        self._image_widget.installEventFilter(self)
        self._vl_widget.installEventFilter(self)
        self._image_widget.cursor_temperature_changed.connect(
            self._on_cursor_temperature
        )

    def eventFilter(self, watched, event) -> bool:
        """Map feed enter/leave events onto hover_changed (never consumed)."""
        event_type = event.type()
        if event_type == QEvent.Type.Enter:
            if watched is self._image_widget:
                self.hover_changed.emit(self._slot_index, "ir")
            elif watched is self._vl_widget:
                self.hover_changed.emit(self._slot_index, "vl")
        elif event_type == QEvent.Type.Leave:
            if watched is self._image_widget or watched is self._vl_widget:
                self.hover_changed.emit(self._slot_index, None)
        return super().eventFilter(watched, event)

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self.hover_changed.emit(self._slot_index, None)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self.hover_changed.emit(None, None)

    def hover_info(self, feed: str | None) -> dict:
        """Snapshot of the identity/state/metrics shown for hover display."""
        number = self._slot_index + 1
        fps = f"{self._last_fps:.1f}" if self._last_fps is not None else "--"
        age = f"{self._last_age_ms:.0f} ms" if self._last_age_ms is not None else "--"
        seq = str(self._latest_sequence) if self._latest_sequence >= 0 else "--"
        return {
            "slot": self._slot_index,
            "number": number,
            "name": self._ip_address or self._name or "Not configured",
            "pos": number,
            "feed": feed,
            "state_text": _STATE_TEXT[self._state],
            "state_status": _STATE_STATUS.get(self._state, "not_available"),
            "temp": self.temp_text(),
            "fps": fps,
            "age": age,
            "seq": seq,
        }

    def temp_text(self) -> str:
        """Last measured temperature readout, or a placeholder."""
        temperature = self._cursor_temp if self._cursor_temp is not None else self._last_temp
        if temperature is None:
            return "--"
        return f"{temperature:.1f} {self._last_temp_unit}"

    @pyqtSlot(float)
    def _on_cursor_temperature(self, temperature: float) -> None:
        """Keep the hovered-camera statistics page in sync with the cursor."""
        self._cursor_temp = float(temperature) if np.isfinite(temperature) else None
        self._temp_label.setText(self.temp_text())
        self.cursor_temperature_changed.emit(temperature)

    def _apply_state_style(self) -> None:
        """Reflect the tile state through semantic properties (no QSS here)."""
        status = _STATE_STATUS.get(self._state, "not_available")
        set_status(self._state_label, status)
        set_tile_state(self, status)

    def _state_style(self, state: LiveTileState) -> str:
        """Legacy accessor kept for compatibility; prefers theme colors."""
        if not self._theme:
            return ""
        color_map = {
            LiveTileState.NOT_AVAILABLE: self._theme.disabled_text(),
            LiveTileState.READY: self._theme.info(),
            LiveTileState.STARTING: self._theme.warning(),
            LiveTileState.RECONNECTING: self._theme.warning(),
            LiveTileState.RUNNING: self._theme.success(),
            LiveTileState.ERROR: self._theme.error(),
        }
        color = color_map.get(state, self._theme.text())
        return f"color: {color}; font-weight: bold;"

    def set_feed_size(self, width: int, height: int) -> None:
        """Apply a wall-computed 4:3 feed size (resize path only)."""
        if (
            self._image_widget.width() != width
            or self._image_widget.height() != height
        ):
            self._image_widget.setFixedSize(width, height)
            self._vl_widget.setFixedSize(width, height)

    def chrome_height(self) -> int:
        """Vertical tile chrome surrounding the feeds (measured)."""
        header_h = self._num_label.height() or 0
        tag_h = self._ir_tag.height() or 0
        status_h = self._state_label.height() or 0
        if header_h <= 0 or tag_h <= 0 or status_h <= 0:
            return _FALLBACK_TILE_CHROME_HEIGHT
        return (
            header_h
            + tag_h
            + status_h
            + 2 * TILE_MARGIN
            + 2 * TILE_SPACING
            + FEED_TAG_SPACING
        )

    def _refresh_identity(self) -> None:
        """Update the compact identity row without touching feed geometry."""
        display_name = self._ip_address or self._name or "Not configured"
        self._name_label.setText(display_name)
        if self._camera_id:
            detail = self._ip_address or self._camera_id
            if self._ip_address and self._camera_id != self._ip_address:
                detail = detail + " / " + self._camera_id
            if self._serial:
                detail = detail + " / " + self._serial
            self._name_label.setToolTip(detail)
        else:
            self._name_label.setToolTip("No camera assigned to this position")

    @pyqtSlot(object)
    def on_result(self, result: ProcessingResult) -> None:
        """Receive a ProcessingResult for this camera on the GUI thread."""
        if self._camera_id is None:
            return

        self._latest_result = result
        self._frames_received += 1
        if _latency_enabled():
            _latency_tracker().note_stage(
                result.frame.descriptor.camera_id,
                result.frame.descriptor.sequence,
                "ui_received",
                time.perf_counter_ns(),
            )

        # CRITICAL: copy the temperature buffer before retaining any display
        # data, so we never share memory with the consumer's mutable result.
        temperature_image = result.temperature_image
        if temperature_image is not None:
            temperature_image = np.asarray(temperature_image).copy()
            self._ir_live = True

        frame = result.frame
        self._image_widget.set_frame(temperature_image, frame)

        # VL feed: same result carries the same hardware frame, so IR/VL
        # correlation holds by construction. Both feeds submit every frame
        # to their existing bounded latest-wins workers; stale sequences
        # are dropped inside the feed widgets, never queued here.
        visible = frame.payload.visible if frame is not None else None
        if visible is not None:
            self._vl_live = True
        try:
            sequence = int(frame.descriptor.sequence)
        except (TypeError, ValueError):
            sequence = self._latest_sequence + 1
        vl_sequence = (
            frame.descriptor.visible.sequence
            if frame is not None and frame.descriptor.visible.sequence is not None
            else sequence
        )
        try:
            vl_acq_ns = int(float(frame.descriptor.monotonic_timestamp) * 1e9)
        except (TypeError, ValueError):
            vl_acq_ns = None
        self._vl_widget.set_frame(
            visible,
            sequence,
            vl_sequence,
            camera_id=frame.descriptor.camera_id,
            acq_mono_ns=vl_acq_ns,
        )

        self._latest_sequence = sequence
        try:
            age_ms = (time.perf_counter() - float(frame.descriptor.monotonic_timestamp)) * 1000.0
            self._last_age_ms = age_ms if age_ms >= 0 else None
        except (TypeError, ValueError):
            self._last_age_ms = None
        self._update_display(result)

        if self._state in (LiveTileState.STARTING, LiveTileState.RECONNECTING):
            self.set_state(LiveTileState.RUNNING)

    @pyqtSlot(str)
    def on_error(self, message: str) -> None:
        self.set_state(LiveTileState.ERROR, message)

    def _update_display(self, result: ProcessingResult) -> None:
        """Update text readouts from a processing result (no styling here)."""
        self._sequence_label.setText(f"seq {result.frame.descriptor.sequence}")
        if self._last_age_ms is not None:
            self._age_label.setText(f"{self._last_age_ms:.0f} ms")

        # Temperature
        analysis = result.analysis_result
        if analysis is not None and analysis.overall_mean is not None:
            unit_symbol = _UNIT_SYMBOLS.get(getattr(analysis, "unit", None), "C")
            self._last_temp = float(analysis.overall_mean)
            self._last_temp_unit = unit_symbol
            self._temp_label.setText(f"{analysis.overall_mean:.1f} {unit_symbol}")
        else:
            self._last_temp = None
            self._temp_label.setText("--")

    def set_state(self, state: LiveTileState, message: str | None = None) -> None:
        self._state = state
        if message is not None:
            self._error_message = message
        self._state_label.setText(_STATE_TEXT[state])
        self._apply_state_style()
        if message:
            self._state_label.setToolTip(message)
            self._sequence_label.setToolTip(message)

        if state == LiveTileState.ERROR:
            # Failed cameras keep their fixed identity but never show stale data.
            self._image_widget.clear()
            self._vl_widget.clear()
            self._ir_live = False
            self._vl_live = False
            self._cursor_temp = None
            self._fps_label.setText("-- fps")
            self._sequence_label.setText("--")
            self._temp_label.setText("--")
            self._age_label.setText("-- ms")
        elif state == LiveTileState.RECONNECTING:
            # Driver-level reconnect: keep the last images and identity
            # visible; the next result flips the tile back to LIVE.
            pass
        elif state == LiveTileState.NOT_AVAILABLE:
            self._image_widget.clear()
            self._vl_widget.clear()
            self._last_temp = None
            self._ir_live = False
            self._vl_live = False
            self._fps_label.setText("-- fps")
            self._sequence_label.setText("--")
            self._sequence_label.setToolTip("")
            self._temp_label.setText("--")
            self._age_label.setText("-- ms")

    def set_error(self, message: str) -> None:
        self.set_state(LiveTileState.ERROR, message)

    def set_stats(
        self,
        *,
        fps: float | None = None,
        processed_frames: int | None = None,
        avg_processing_ms: float | None = None,
        dropped_frames: int | None = None,
    ) -> None:
        """Update stats derived from runtime/observer/processing statistics."""
        if fps is not None:
            self._last_fps = fps
            self._fps_label.setText(f"{fps:.1f} fps")
        elif avg_processing_ms is not None and self._last_fps is None:
            self._fps_label.setText(f"{avg_processing_ms:.1f} ms")
        if dropped_frames is not None and dropped_frames > 0:
            self._sequence_label.setToolTip(f"Dropped: {dropped_frames}")

    def clear(self) -> None:
        self._latest_result = None
        self._frames_received = 0
        self._latest_sequence = -1
        self._last_age_ms = None
        self._last_fps = None
        self._last_temp = None
        self._cursor_temp = None
        self._ir_live = False
        self._vl_live = False
        self._error_message = None
        self._image_widget.clear()
        self._vl_widget.clear()
        self._fps_label.setText("-- fps")
        self._sequence_label.setText("--")
        self._sequence_label.setToolTip("")
        self._temp_label.setText("--")
        self._age_label.setText("-- ms")
        self._ptz_label.setText("")
        self._ptz_label.setToolTip("")

    def set_ptz_status(self, text: str, tooltip: str = "") -> None:
        """Compact read-only PTZ readout (wall monitor only, no commands)."""
        try:
            self._ptz_label.setText(text)
            self._ptz_label.setToolTip(tooltip)
        except RuntimeError:
            pass  # teardown race; tile already gone

    def set_feed_mode(self, mode: str) -> None:
        """Deprecated compatibility shim: the wall always shows IR and VL.

        Kept so older callers do not break. Any of the historic values is
        accepted and both feeds stay visible.
        """
        if mode not in ("ir", "vl", "both"):
            raise ValueError(f"feed mode must be 'ir', 'vl' or 'both'; got {mode!r}")

    @property
    def feed_mode(self) -> str:
        """Always both feeds; retained for backward compatibility."""
        return "both"

    def set_camera(
        self,
        camera_id: str,
        name: str,
        serial: str,
        ip_address: str = "",
    ) -> None:
        """Assign a camera to this fixed position slot (READY, not started)."""
        self._camera_id = camera_id
        self._name = name
        self._serial = serial
        self._ip_address = ip_address
        self._refresh_identity()
        self.set_state(LiveTileState.READY)

    def clear_camera(self) -> None:
        """Remove camera assignment; the tile keeps its position and size."""
        self._camera_id = None
        self._name = ""
        self._serial = ""
        self._ip_address = ""
        self._refresh_identity()
        self.set_state(LiveTileState.NOT_AVAILABLE)
        self.clear()

    @property
    def camera_id(self) -> str | None:
        return self._camera_id

    @property
    def slot_index(self) -> int:
        return self._slot_index

    @property
    def state(self) -> LiveTileState:
        return self._state

    @property
    def error_message(self) -> str | None:
        return self._error_message

    @property
    def frames_received(self) -> int:
        return self._frames_received

    @property
    def latest_sequence(self) -> int:
        return self._latest_sequence


class LiveStatsPanel(QWidget):
    """Ninth grid cell: system statistics or hovered-camera details.

    Permanently occupies the last grid position; it never becomes a
    camera tile. Shows aggregate wall data by default and switches to
    the hovered camera while the pointer is over a tile or feed. All
    content is text set from already-available tile/wall state; nothing
    is polled, converted or recomputed here.
    """

    def __init__(self, theme_manager: Optional[ThemeManager] = None) -> None:
        super().__init__()
        self._theme = theme_manager
        set_role(self, "tile")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(TILE_MIN_WIDTH, TILE_MIN_HEIGHT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(TILE_MARGIN, TILE_MARGIN, TILE_MARGIN, TILE_MARGIN)
        layout.setSpacing(TILE_SPACING)

        self._title = QLabel("LIVE STATISTICS")
        set_role(self._title, "strong")
        layout.addWidget(self._title)

        self._pages = QStackedWidget()
        # The page stack never drives grid geometry (a word-wrapped page
        # must not resize the wall on switch); tiles size the cells and
        # the panel simply fills its cell.
        self._pages.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        layout.addWidget(self._pages, 1)

        system_page = QWidget()
        system_layout = QVBoxLayout(system_page)
        system_layout.setContentsMargins(0, 0, 0, 0)
        system_layout.setSpacing(2)
        self._sys_cams = QLabel("Configured: --   Running: --   Failed: --")
        set_role(self._sys_cams, "mono")
        self._sys_feeds = QLabel("IR: --/8   VL: --/8")
        set_role(self._sys_feeds, "mono")
        self._sys_fps = QLabel("Total FPS: --")
        set_role(self._sys_fps, "mono")
        self._sys_note = QLabel("")
        set_role(self._sys_note, "muted")
        self._sys_temps = QLabel("--")
        set_role(self._sys_temps, "mono")
        self._sys_temps.setWordWrap(True)
        self._sys_wall = QLabel("Feeds: 16   Wall: 3x3")
        set_role(self._sys_wall, "muted")
        for widget in (
            self._sys_cams,
            self._sys_feeds,
            self._sys_fps,
            self._sys_note,
            self._sys_temps,
            self._sys_wall,
        ):
            widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            system_layout.addWidget(widget)
        system_layout.addStretch(1)
        self._pages.addWidget(system_page)

        camera_page = QWidget()
        camera_layout = QVBoxLayout(camera_page)
        camera_layout.setContentsMargins(0, 0, 0, 0)
        camera_layout.setSpacing(2)
        self._cam_heading = QLabel("HOVERED CAMERA")
        set_role(self._cam_heading, "strong")
        self._cam_id = QLabel("--")
        set_role(self._cam_id, "mono")
        self._cam_place = QLabel("--")
        set_role(self._cam_place, "mono")
        self._cam_temp = QLabel("--")
        set_role(self._cam_temp, "readout")
        self._cam_perf = QLabel("--")
        set_role(self._cam_perf, "mono")
        self._cam_status = QLabel("--")
        for widget in (
            self._cam_id,
            self._cam_place,
            self._cam_temp,
            self._cam_perf,
        ):
            widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            widget.setWordWrap(False)
        for widget in (
            self._cam_heading,
            self._cam_id,
            self._cam_place,
            self._cam_temp,
            self._cam_perf,
            self._cam_status,
        ):
            camera_layout.addWidget(widget)
        camera_layout.addStretch(1)
        self._pages.addWidget(camera_page)

    @property
    def showing_camera(self) -> bool:
        """True while the hovered-camera page is visible."""
        return self._pages.currentIndex() == 1

    def update_system(self, snapshot: dict) -> None:
        """Refresh the aggregate page from a wall snapshot (text only)."""
        self._sys_cams.setText(
            f"Configured: {snapshot.get('configured', '--')}   "
            f"Connected: {snapshot.get('connected', '--')}   "
            f"Running: {snapshot.get('running', '--')}   "
            f"Failed: {snapshot.get('failed', '--')}   "
            f"Reconnecting: {snapshot.get('reconnecting', '--')}"
        )
        self._sys_feeds.setText(
            f"IR: {snapshot.get('ir', '--')}/8   VL: {snapshot.get('vl', '--')}/8"
        )
        total_fps = snapshot.get("total_fps")
        self._sys_fps.setText(
            f"Total FPS: {total_fps:.1f}" if total_fps is not None else "Total FPS: --"
        )
        extra = snapshot.get("extra", 0) or 0
        if extra > 0:
            self._sys_note.setText(f"Showing first 8 of {snapshot.get('configured', 8)}")
        else:
            self._sys_note.setText("")
        temps = snapshot.get("temps", [])
        if temps:
            self._sys_temps.setText("   ".join(f"CAM {num}: {text}" for num, text in temps))
        else:
            self._sys_temps.setText("--")

    def show_camera(self, info: dict) -> None:
        """Show one hovered camera (text only, no new data sources)."""
        feed = info.get("feed")
        self._cam_id.setText(f"Camera {info.get('number')} · {info.get('name')}")
        self._cam_place.setText(
            f"Position {info.get('pos')}   Feed: {feed.upper() if feed else '--'}"
        )
        self._cam_temp.setText(f"Temp: {info.get('temp')}")
        self._cam_perf.setText(
            f"FPS: {info.get('fps')}   Age: {info.get('age')}   Seq: {info.get('seq')}"
        )
        self._cam_status.setText(f"Status: {info.get('state_text')}")
        set_status(self._cam_status, info.get("state_status") or "")
        self._pages.setCurrentIndex(1)

    def show_system(self) -> None:
        """Return to the aggregate page."""
        self._pages.setCurrentIndex(0)


class _StartupWorker(QThread):
    """Blocking camera startup off the GUI thread (one acquisition path).

    Runs only ``CameraRuntimeService.start_camera`` — the single,
    potentially slow network/blocking step — for each queued camera and
    reports back per camera. Observer attach and all widget updates stay
    on the GUI thread. The runtime service serializes internally, so the
    sequential loop here never contends with GUI-thread readers.
    """

    camera_started = pyqtSignal(str)
    camera_failed = pyqtSignal(str, str)
    _LIVE_START_TIMEOUT_S = 3.0
    def __init__(self, runtime_service, configs: list, abort: threading.Event, parent=None) -> None:
        super().__init__(parent)
        self._runtime_service = runtime_service
        self._configs = list(configs)
        self._abort = abort

    def run(self) -> None:
        for config in self._configs:
            if self._abort.is_set():
                break
            camera_id = config.identity.camera_id
            try:
                if not self._runtime_service.is_camera_running(camera_id):
                    if isinstance(self._runtime_service, CameraRuntimeService):
                        self._runtime_service.start_camera(
                            config,
                            timeout=self._LIVE_START_TIMEOUT_S,
                        )
                    else:
                        self._runtime_service.start_camera(config)
                self.camera_started.emit(camera_id)
            except Exception as exc:
                self.camera_failed.emit(camera_id, str(exc))


class LiveModeWidget(QWidget):
    """Live monitoring mode with a 3x3 wall.

    Eight permanent camera positions plus one statistics panel. Position
    N is always tile N; disconnects never remove, reorder or resize.
    """

    # Read-only PTZ readout delivery: (startup token, camera_id, text,
    # tooltip). Emitted from a retained daemon poll thread; stale tokens
    # and unassigned cameras are dropped by the slot.
    _ptz_readout = pyqtSignal(int, str, str, str)

    def __init__(
        self,
        mode_service: ModeService,
        config_service: ConfigurationService,
        observer_service: ObserverService | None = None,
        *,
        runtime_service: CameraRuntimeService | None = None,
        stats_interval_ms: int = 1000,
        theme_manager: Optional[ThemeManager] = None,
        config_manager=None,
    ) -> None:
        super().__init__()
        self._mode_service = mode_service
        self._config_service = config_service
        self._runtime_service = runtime_service
        self._legacy_observer = observer_service
        self._stats_interval_ms = stats_interval_ms
        self._theme = theme_manager
        self._config_manager = config_manager

        self._tiles: list[LiveCameraTile] = []  # Fixed 8 tiles, index = slot
        self._camera_to_slot: dict[str, int] = {}  # camera_id -> slot index
        self._stats_panel: LiveStatsPanel | None = None  # Permanent ninth cell
        self._hovered: tuple[int | None, str | None] | None = None
        self._connected_observers: set[str] = set()  # camera_ids with wired signals
        self._startup_worker: _StartupWorker | None = None
        self._startup_abort = threading.Event()
        self._startup_token = 0
        self._stats_timer: QTimer | None = None
        self._last_poll: dict[str, tuple[int, float]] = {}
        # Read-only Live PTZ monitor (Phase 7): own service/session built
        # from configuration, slow poll timer, token-guarded delivery to
        # tile readouts. Never issues movement commands and never touches
        # Configuration Mode widgets.
        self._ptz_service = None
        self._ptz_timer: QTimer | None = None
        self._ptz_poll_busy = False
        self._ptz_threads: set = set()
        self._ptz_readout.connect(self._on_ptz_readout)

        self._setup_ui()
        self._create_fixed_tiles()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(WALL_OUTER_MARGIN, WALL_OUTER_MARGIN, WALL_OUTER_MARGIN, WALL_OUTER_MARGIN)
        layout.setSpacing(2)

        # Compact header bar: wall identity, connect control, summary.
        self._header_bar = QWidget()
        header = QHBoxLayout(self._header_bar)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        title = QLabel("LIVE  •  8 CAMERAS  •  16 FEEDS")
        set_role(title, "strong")
        header.addWidget(title)
        self._connect_button = QPushButton("CONNECT & START ALL")
        set_variant(self._connect_button, "primary")
        self._connect_button.clicked.connect(self._on_connect_clicked)
        header.addWidget(self._connect_button)
        header.addStretch(1)
        self._summary_label = QLabel("Initializing...")
        set_role(self._summary_label, "muted")
        header.addWidget(self._summary_label)
        layout.addWidget(self._header_bar)

        # Viewport-filling 3x3 wall. The grid keeps its content size (the
        # refit fixes exact 4:3 feed widgets) and the surrounding stretches
        # center it, so the wall dominates the viewport on any monitor
        # with only restrained margins left over. Scrollbars appear only
        # when the window shrinks below the tile minimums. Geometry is
        # resolved here on resize and first show only, never per frame.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self._center = QWidget()
        center_layout = QVBoxLayout(self._center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)
        center_layout.addStretch(1)
        middle = QHBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(0)
        middle.addStretch(1)
        self._grid_widget = QWidget()
        self._grid_widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._grid_layout = QGridLayout(self._grid_widget)
        self._grid_layout.setContentsMargins(WALL_GRID_MARGIN, WALL_GRID_MARGIN, WALL_GRID_MARGIN, WALL_GRID_MARGIN)
        self._grid_layout.setSpacing(WALL_GRID_SPACING)
        middle.addWidget(self._grid_widget)
        middle.addStretch(1)
        center_layout.addLayout(middle)
        center_layout.addStretch(1)
        self._scroll.setWidget(self._center)
        layout.addWidget(self._scroll, 1)
        self._refitting = False

    @staticmethod
    def slot_position(slot: int) -> tuple[int, int]:
        """Grid position of camera slot S: row S//3, col S%3."""
        return (slot // GRID_COLUMNS, slot % GRID_COLUMNS)

    def _create_fixed_tiles(self) -> None:
        """Create 8 camera tiles plus the permanent statistics cell."""
        self._tiles.clear()
        for slot in range(FIXED_CAMERA_SLOTS):
            tile = LiveCameraTile(slot, theme_manager=self._theme)
            tile.hover_changed.connect(self._on_tile_hover)
            tile.cursor_temperature_changed.connect(
                lambda temperature, slot=slot: self._on_tile_cursor_temperature(
                    slot, temperature
                )
            )
            self._tiles.append(tile)
            row, col = self.slot_position(slot)
            self._grid_layout.addWidget(tile, row, col)
        self._stats_panel = LiveStatsPanel(theme_manager=self._theme)
        self._grid_layout.addWidget(
            self._stats_panel, STATS_GRID_ROW, STATS_GRID_COL
        )

    def compute_feed_size(self, viewport_width: int, viewport_height: int) -> tuple[int, int]:
        """Largest 4:3 feed size fitting 8 tiles (3x3 wall, IR|VL) in a viewport.

        Width bound splits the viewport into 6 feed columns (3 tiles of
        IR|VL); height bound splits it into 3 tile rows minus header and
        tile chrome. Pure function of geometry (also used by tests);
        never touches widgets.
        """
        chrome_w = 2 * TILE_MARGIN + FEED_GAP
        horizontal_overhead = (
            2 * WALL_GRID_MARGIN
            + (GRID_COLUMNS - 1) * WALL_GRID_SPACING
            + GRID_COLUMNS * chrome_w
        )
        width_bound = (viewport_width - horizontal_overhead) / (GRID_COLUMNS * 2)
        header_h = self._header_bar.height() if hasattr(self, "_header_bar") else 0
        if header_h <= 0:
            header_h = _FALLBACK_HEADER_BAR_HEIGHT
        chrome_h = (
            self._tiles[0].chrome_height() if self._tiles else _FALLBACK_TILE_CHROME_HEIGHT
        )
        vertical_overhead = (
            header_h
            + 2  # header-to-wall spacing
            + 2 * WALL_OUTER_MARGIN
            + 2 * WALL_GRID_MARGIN
            + (GRID_ROWS - 1) * WALL_GRID_SPACING
            + GRID_ROWS * chrome_h
        )
        height_bound = ((viewport_height - vertical_overhead) / GRID_ROWS) * 4 / 3
        feed_w = int(min(width_bound, height_bound))
        feed_w = max(feed_w, FEED_MIN_WIDTH)
        return feed_w, int(round(feed_w * 3 / 4))

    def _refit_wall(self) -> None:
        """Refit feed widgets to the current viewport (resize/show only)."""
        try:
            if self._refitting or not self._tiles:
                return
        except RuntimeError:
            return  # wall already deleted by a queued refit
        viewport = self._scroll.viewport()
        if viewport is None:
            return
        size = viewport.size()
        if not size.isValid() or size.width() <= 0 or size.height() <= 0:
            return
        feed_w, feed_h = self.compute_feed_size(size.width(), size.height())
        current = self._tiles[0]._image_widget
        if current.width() == feed_w and current.height() == feed_h:
            return
        self._refitting = True
        try:
            for tile in self._tiles:
                tile.set_feed_size(feed_w, feed_h)
        finally:
            self._refitting = False

    def _schedule_refit(self) -> None:
        """Defer the refit until the viewport settled on its new size."""
        try:
            QTimer.singleShot(0, self._refit_wall)
        except RuntimeError:
            pass

    def _tile_counts(self) -> dict:
        """Single source for wall counts from existing tile/runtime state."""
        configured = len(self._config_service.get_all_camera_configs())
        assigned = len(self._camera_to_slot)
        running = sum(1 for t in self._tiles if t.state is LiveTileState.RUNNING)
        failed = sum(1 for t in self._tiles if t.state is LiveTileState.ERROR)
        reconnecting = sum(1 for t in self._tiles if t.state is LiveTileState.RECONNECTING)
        if self._runtime_service is not None and assigned:
            try:
                connected = sum(
                    1 for camera_id in self._camera_to_slot
                    if self._runtime_service.is_camera_running(camera_id)
                )
            except Exception:
                connected = running
        else:
            connected = running
        fps_values = [t._last_fps for t in self._tiles if t._last_fps is not None]
        return {
            "configured": configured,
            "assigned": assigned,
            "extra": max(0, configured - FIXED_CAMERA_SLOTS),
            "connected": connected,
            "running": running,
            "failed": failed,
            "reconnecting": reconnecting,
            "total_fps": sum(fps_values) if fps_values else None,
            "temps": [(t.slot_index + 1, t.temp_text()) for t in self._tiles],
            "ir": sum(1 for t in self._tiles if t._ir_live),
            "vl": sum(1 for t in self._tiles if t._vl_live),
        }

    def _header_status_word(self, counts: dict) -> str:
        if counts["configured"] == 0:
            return ""
        if counts["failed"] > 0:
            return "DEGRADED"
        if counts.get("reconnecting", 0) > 0:
            return "DEGRADED"
        if counts["running"] > 0:
            return "NORMAL"
        if any(t.state is LiveTileState.STARTING for t in self._tiles):
            return "STARTING"
        return "READY"

    @pyqtSlot(object, object)
    def _on_tile_hover(self, slot: int | None, feed: str | None) -> None:
        """Switch the statistics panel between hover and system views."""
        if self._stats_panel is None:
            return
        if slot is None:
            self._hovered = None
            self._stats_panel.show_system()
            return
        if not isinstance(slot, int) or not 0 <= slot < len(self._tiles):
            return
        self._hovered = (slot, feed)
        self._stats_panel.show_camera(self._tiles[slot].hover_info(feed))

    def _on_tile_cursor_temperature(self, slot: int, temperature: float) -> None:
        """Refresh hovered-camera statistics without waiting for the poll timer."""
        if self._stats_panel is None or self._hovered is None:
            return
        hovered_slot, feed = self._hovered
        if hovered_slot == slot:
            self._stats_panel.show_camera(self._tiles[slot].hover_info(feed))

    def _system_snapshot(self) -> dict:
        """Aggregate wall state from existing tile data (text-ready)."""
        return self._tile_counts()

    def _refresh_header_and_panel(self) -> None:
        """Refresh the compact header and statistics panel (text only)."""
        snapshot = self._system_snapshot()
        configured = snapshot["configured"]
        assigned = snapshot["assigned"]
        connected = snapshot["connected"]
        total_fps = snapshot["total_fps"]
        fps_text = f"{total_fps:.0f}" if total_fps is not None else "--"
        if configured == 0:
            self._set_summary("No cameras configured")
        else:
            self._set_summary(
                f"Connected: {connected}/{assigned}   "
                f"FPS: ~{fps_text}   Status: {self._header_status_word(snapshot)}"
            )
        if self._stats_panel is not None:
            self._stats_panel.update_system(snapshot)
            if self._hovered is not None:
                slot, feed = self._hovered
                if isinstance(slot, int) and 0 <= slot < len(self._tiles):
                    self._stats_panel.show_camera(self._tiles[slot].hover_info(feed))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule_refit()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._schedule_refit()

    @property
    def feed_mode(self) -> str:
        """Deprecated compatibility accessor: the wall always shows both."""
        return "both"

    def on_mode_activated(self) -> None:
        """Show the wall with assigned cameras in READY state.

        Entering Live mode never starts acquisition by itself; the
        operator starts cameras explicitly with CONNECT & START ALL.
        This keeps entry fast and start idempotent (no auto + button
        duplicate observers).
        """
        logger.info("LIVE MODE ACTIVATED: config_service id=%s total_before=%d", hex(id(self._config_service)), len(self._config_service.get_all_camera_configs()))
        self._assign_cameras_to_slots()
        self._update_summary()
        self._update_button_state()
        self._start_stats_timer()
        self._start_ptz_timer()
        # Post-activation summary for diagnostics
        try:
            counts = self._tile_counts()
            logger.info("LIVE MODE POST-ACTIVATED: configured=%d assigned=%d eligible=%d button=%r summary=%r", counts["configured"], counts["assigned"], len(self._eligible_configs()), self._connect_button.text(), self._summary_label.text())
        except Exception:
            pass

    def on_mode_deactivated(self) -> None:
        """Stop live monitoring and tear down when leaving Live mode.

        Visible-transition fast path: the GUI thread only detaches the
        display path (signals, timers, render workers) and returns
        immediately. Camera/process/SHM teardown runs in a background
        daemon thread, so the Launcher (or the next mode) is already
        interactive while the child processes exit. Lifecycle safety is
        preserved: the startup token is bumped first (late queued
        results are dropped as stale), the background thread reaps the
        startup worker BEFORE stopping cameras (no orphan starts can
        leak), and only this wall's own assigned cameras are stopped.
        """
        cameras, worker, legacy = self._begin_transition_detach()
        self._stop_cameras_background(cameras, worker, legacy)

    def _begin_transition_detach(self) -> tuple[list[str], object, object]:
        """GUI-thread immediate part of leaving Live mode (never blocks).

        Bumps the startup generation FIRST so any late queued
        ``camera_started``/``camera_failed``/result signal is dropped as
        stale, disconnects every observer/tile signal, stops timers and
        render workers, and snapshots this wall's assigned cameras for
        the background teardown. Returns ``(camera_ids, worker, legacy
        observer)`` for :meth:`_stop_cameras_background`.
        """
        import time as _time

        _t0 = _time.perf_counter_ns()
        self._startup_token += 1
        try:
            self._startup_abort.set()
        except Exception:
            pass
        # Non-blocking startup-worker detach: signals are cut now; the
        # thread itself is reaped by the background teardown (never
        # wait() on the GUI thread).
        worker = self._detach_startup_worker()
        self._stop_stats_timer()
        self._stop_ptz_timer()
        # Feed renderers, two passes: FIRST wake every renderer without
        # waiting (session drop + stop request, so no stale frame can be
        # accepted from this instant on), THEN join them all with a
        # bounded wait. Joining before any C++ object dies guarantees Qt
        # can never delete a still-running QThread (which aborts the
        # process with no Python traceback). The wakes overlap, so the
        # total cost is ~one renderer exit, not sixteen.
        # (Pass 1 below wakes; pass 2 joins.)
        feed_widgets = []
        for tile in self._tiles:
            for widget in (
                getattr(tile, "_image_widget", None),
                getattr(tile, "_vl_widget", None),
            ):
                if widget is not None:
                    feed_widgets.append(widget)
        for widget in feed_widgets:
            try:
                widget.prepare_for_transition()
            except RuntimeError:
                pass  # C++ object already gone
            except Exception:
                logger.debug("Tile transition detach failed", exc_info=True)
        for widget in feed_widgets:
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
        self._disconnect_all_tiles()
        cameras = list(self._camera_to_slot.keys())
        legacy, self._legacy_observer = self._legacy_observer, None
        # Reset tiles to not available (fast: text + clear only, the
        # renderers were already asked to stop above).
        for tile in self._tiles:
            try:
                tile.clear_camera()
            except RuntimeError:
                pass
            except Exception:
                logger.debug("Tile clear failed", exc_info=True)
        self._camera_to_slot.clear()
        self._connected_observers.clear()
        self._hovered = None
        try:
            if self._stats_panel is not None:
                self._stats_panel.show_system()
        except RuntimeError:
            pass
        try:
            self._set_summary("Stopped")
            self._update_button_state()
        except RuntimeError:
            pass
        logger.info(
            "[MODE-TRANSITION] live_detach_complete cameras=%d detach_ms=%.1f",
            len(cameras),
            (_time.perf_counter_ns() - _t0) / 1e6,
        )
        return cameras, worker, legacy

    def _stop_cameras_background(
        self, cameras: list[str], worker: object, legacy: object
    ) -> None:
        """Tear down Live cameras off the GUI thread (lifecycle-safe).

        Order: reap the orphaned startup worker FIRST (bounded wait —
        its abort flag is already set, so at most one in-flight
        ``start_camera`` can still be running), then ``stop_camera``
        each of THIS wall's assigned cameras through the runtime's
        hardened ``CameraProcessHandle`` state machine
        (RUNNING -> STOPPING -> CHILD_STOPPED -> SHM_RELEASED ->
        PIPES_CLOSED -> STOPPED). Only the snapshotted camera set is
        touched: cameras owned by another mode sharing the runtime are
        never stopped here.
        """
        import threading as _threading
        import time as _time

        runtime = self._runtime_service
        if worker is None and not cameras and legacy is None:
            return

        def _teardown() -> None:
            _t0 = _time.perf_counter_ns()
            logger.info(
                "[MODE-TRANSITION] background_teardown_started cameras=%d",
                len(cameras),
            )
            if worker is not None:
                try:
                    running = worker.isRunning()
                except RuntimeError:
                    running = False
                if running:
                    try:
                        finished = worker.wait(8000)
                    except RuntimeError:
                        finished = True
                    if not finished:
                        logger.warning(
                            "Startup worker still running after 8000 ms; "
                            "leaving it to quit itself on operation completion"
                        )
                        try:
                            worker.finished.connect(worker.deleteLater)
                        except RuntimeError:
                            pass
                    else:
                        try:
                            worker.deleteLater()
                        except RuntimeError:
                            pass
                else:
                    try:
                        worker.deleteLater()
                    except RuntimeError:
                        pass
            if runtime is not None:
                for camera_id in cameras:
                    try:
                        runtime.stop_camera(camera_id)
                    except Exception:
                        logger.exception(
                            "Failed to stop camera %s on mode exit", camera_id
                        )
            elif legacy is not None:
                try:
                    legacy.stop()
                except Exception:
                    pass
            try:
                self._shutdown_ptz_services()
            except Exception:
                pass
            logger.info(
                "[MODE-TRANSITION] background_teardown_complete "
                "cameras=%d teardown_ms=%.1f",
                len(cameras),
                (_time.perf_counter_ns() - _t0) / 1e6,
            )

        thread = _threading.Thread(
            target=_teardown, name="LiveModeTeardown", daemon=True
        )
        thread.start()

    def _log_live_config_diagnostics(self) -> None:
        """INFO-level trace of the exact configuration path Live sees.

        Logs:
          - ConfigurationService object identity
          - configuration file/path being used
          - total camera configurations returned
          - each camera id / name / serial / enabled / thermal_enabled / position
          - counts before vs after filtering
          - why each rejected camera was excluded
        No side effects on assignment, filtering or rendering.
        """
        try:
            svc = self._config_service
            svc_id = hex(id(svc))
            # Resolve config path from injected manager or via default discovery
            config_path_str = "unknown"
            config_exists = "unknown"
            tmp_exists = "unknown"
            try:
                if self._config_manager is not None and hasattr(self._config_manager, "config_path"):
                    p = Path(self._config_manager.config_path)
                    config_path_str = str(p)
                    config_exists = str(p.exists())
                    tmp = p.with_suffix(p.suffix + ".tmp")
                    tmp_exists = f"{tmp} exists={tmp.exists()}"
                    # Also check .bak
                    bak = p.with_suffix(p.suffix + ".bak")
                    if bak.exists():
                        tmp_exists += f" bak_exists={bak.exists()}"
                else:
                    config_path_str = "unknown (no config_manager injected; single loader is AppController)"
                    config_exists = "unknown"
                    tmp_exists = "unknown"
            except Exception as exc:
                config_path_str = f"resolve_failed: {exc}"

            all_configs = svc.get_all_camera_configs()
            total = len(all_configs)
            logger.info("LIVE CONFIG: config_service=%s (%s) config_path=%s exists=%s tmp=%s total cameras=%d", svc_id, type(svc).__name__, config_path_str, config_exists, tmp_exists, total)
            if total == 0:
                logger.info("LIVE CONFIG: no camera configurations returned by ConfigurationService.get_all_camera_configs()")
            for idx, cfg in enumerate(all_configs):
                try:
                    cid = getattr(cfg.identity, "camera_id", "?") if hasattr(cfg, "identity") else getattr(cfg, "camera_id", "?")
                except Exception:
                    cid = "?"
                try:
                    name = getattr(cfg, "name", "")
                except Exception:
                    name = ""
                try:
                    serial = getattr(cfg.identity, "serial_number", "") if hasattr(cfg, "identity") else getattr(cfg, "serial", "")
                except Exception:
                    serial = ""
                # actual semantics: enabled / thermal_enabled defaults must be explicit
                has_enabled = hasattr(cfg, "enabled")
                has_thermal = hasattr(cfg, "thermal_enabled")
                try:
                    enabled_val = cfg.enabled if has_enabled else "MISSING"
                except Exception as e:
                    enabled_val = f"ERROR:{e}"
                try:
                    thermal_val = cfg.thermal_enabled if has_thermal else "MISSING"
                except Exception as e:
                    thermal_val = f"ERROR:{e}"
                pos = idx + 1
                logger.info("  CAM %d: id=%r name=%r serial=%r enabled=%r (has_attr=%s) thermal_enabled=%r (has_attr=%s) position=%d", pos, cid, name, serial, enabled_val, has_enabled, thermal_val, has_thermal, pos)
                # Rejection reason
                if has_enabled and not enabled_val:
                    logger.info("    -> REJECTED: enabled=False")
                elif has_thermal and not thermal_val:
                    logger.info("    -> REJECTED: thermal_enabled=False")
                elif not has_enabled:
                    logger.info("    -> NOTE: enabled attribute missing, would default to True if using getattr fallback (hides problem)")
                elif not has_thermal:
                    logger.info("    -> NOTE: thermal_enabled attribute missing, would default to True if using getattr fallback (hides problem)")

            # Counts before vs after filtering
            enabled_list = self._enabled_cameras()
            enabled_count = len(enabled_list)
            # thermal_enabled specific count
            thermal_count = 0
            for c in all_configs:
                try:
                    if getattr(c, "thermal_enabled", True):
                        thermal_count += 1
                except Exception:
                    thermal_count += 0
            eligible = enabled_list[:FIXED_CAMERA_SLOTS]
            logger.info("LIVE COUNTS: get_all_camera_configs()=%d enabled=%d thermal_enabled=%d eligible(FIXED_CAMERA_SLOTS=%d)=%d", total, enabled_count, thermal_count, FIXED_CAMERA_SLOTS, len(eligible))
            logger.info("LIVE ELIGIBLE CAMERAS = %d", len(eligible))
            if len(eligible) == 0 and total > 0:
                logger.info("LIVE DIAGNOSIS: %d configured cameras but zero eligible -> check enabled/thermal_enabled filtering", total)
            if len(eligible) == 0 and total == 0:
                logger.info("LIVE DIAGNOSIS: zero configured cameras -> check config file path, ConfigurationService hydration, mapping hydration, database, or lifecycle timing")
            # Also log camera assignment order
            for slot, cfg in enumerate(eligible):
                logger.info("  ASSIGNMENT: slot %d (POS %d) <- camera_id=%r name=%r serial=%r", slot, slot + 1, cfg.identity.camera_id, getattr(cfg, "name", ""), getattr(cfg.identity, "serial_number", ""))
        except Exception as exc:
            logger.exception("LIVE CONFIG diagnostics failed: %s", exc)

    def _assign_cameras_to_slots(self) -> None:
        """Assign enabled cameras to fixed slots (1-8) in config order."""
        self._log_live_config_diagnostics()
        enabled_cameras = self._enabled_cameras()

        # Clear all tiles first
        for tile in self._tiles:
            tile.clear_camera()
        self._camera_to_slot.clear()

        # Assign cameras to slots 0-7 (up to 8 cameras)
        for slot_index, config in enumerate(enabled_cameras[:FIXED_CAMERA_SLOTS]):
            camera_id = config.identity.camera_id
            tile = self._tiles[slot_index]
            metadata = dict(config.metadata or {})
            tile.set_camera(
                camera_id,
                config.name or camera_id,
                config.identity.serial_number,
                str(metadata.get("ip_address") or ""),
            )
            self._camera_to_slot[camera_id] = slot_index
        # Log post-assignment tile states
        try:
            assigned = len(self._camera_to_slot)
            logger.info("LIVE ASSIGNMENT COMPLETE: assigned=%d tiles=%d", assigned, len(self._tiles))
            for idx, tile in enumerate(self._tiles):
                logger.info("  TILE %d: camera_id=%r name=%r state=%s", idx + 1, tile.camera_id, getattr(tile, "_name", ""), tile.state.value if hasattr(tile, "state") else "?")
        except Exception:
            pass

    def _enabled_cameras(self) -> list:
        result = []
        for config in self._config_service.get_all_camera_configs():
            # Do not hide missing attributes with getattr default; check actual semantics
            # CameraConfig defines enabled and thermal_enabled; if missing, treat as misconfiguration
            has_enabled = hasattr(config, "enabled")
            has_thermal = hasattr(config, "thermal_enabled")
            enabled = config.enabled if has_enabled else True
            thermal = config.thermal_enabled if has_thermal else True
            if not has_enabled or not has_thermal:
                logger.warning("LIVE FILTER: camera %r missing enabled/thermal_enabled attributes (has_enabled=%s has_thermal=%s) -> using defaults enabled=%s thermal=%s", getattr(getattr(config, "identity", None), "camera_id", "?"), has_enabled, has_thermal, enabled, thermal)
            if enabled and thermal:
                result.append(config)
        return result

    def _eligible_configs(self) -> list:
        """Configured Live cameras in fixed position order (max 8)."""
        return self._enabled_cameras()[:FIXED_CAMERA_SLOTS]

    def _wants_startup(self) -> bool:
        """True while a startup worker owned by this session is active."""
        worker = self._startup_worker
        return worker is not None and worker.isRunning()

    @pyqtSlot()
    def _on_connect_clicked(self) -> None:
        """CONNECT & START ALL / RETRY FAILED: start eligible cameras.

        Returns immediately; the blocking runtime calls run in a worker
        thread and each camera attaches on the GUI thread as it completes.
        Pressing again only queues cameras that are not running, so healthy
        cameras are never reconnected and observers are never duplicated.
        """
        if self._startup_worker is not None:
            # A startup session is already owned by this wall (running or
            # just created): ignore the repeat click so a second worker
            # can never duplicate cameras, observers or connections.
            return
        if self._runtime_service is None:
            if self._legacy_observer is not None:
                self._start_via_legacy()
            return
        eligible = self._eligible_configs()
        if not eligible:
            return
        desired = {config.identity.camera_id: slot for slot, config in enumerate(eligible)}
        if desired != self._camera_to_slot:
            self._assign_cameras_to_slots()
        queue = [
            config for config in eligible
            if not self._is_camera_live(config.identity.camera_id)
        ]
        if not queue:
            self._update_button_state()
            return
        for config in queue:
            self._tiles[self._camera_to_slot[config.identity.camera_id]].set_state(
                LiveTileState.STARTING
            )
        self._startup_abort = threading.Event()
        worker = _StartupWorker(self._runtime_service, queue, self._startup_abort, parent=self)
        token = self._startup_token
        worker.camera_started.connect(
            lambda camera_id, _token=token: self._on_startup_camera_ready(camera_id, _token)
        )
        worker.camera_failed.connect(
            lambda camera_id, message, _token=token: self._on_startup_camera_failed(
                camera_id, message, _token
            )
        )
        worker.finished.connect(lambda _token=token: self._on_startup_finished(_token))
        self._startup_worker = worker
        # Show STARTING synchronously: QThread.isRunning() lags behind
        # start(), so the button must not depend on the thread state here.
        # Progress/finish handlers reconcile it via _update_button_state.
        self._connect_button.setText("STARTING...")
        self._connect_button.setEnabled(False)
        self._refresh_header_and_panel()
        worker.start()

    def _is_camera_live(self, camera_id: str) -> bool:
        """True when the camera runs and its observer signals are wired."""
        if self._runtime_service is None:
            return False
        try:
            running = self._runtime_service.is_camera_running(camera_id)
        except Exception:
            return False
        return bool(running) and camera_id in self._connected_observers

    def _detach_startup_worker(self):
        """Detach the camera-startup worker WITHOUT waiting (GUI fast path).

        Disconnects GUI slots FIRST so late queued results cannot reach
        them, sets the abort flag (checked between cameras), reparents
        the worker OFF the wall (a parented QThread deleted by window
        teardown while still running aborts the process with
        ``QThread: Destroyed while thread is still running``), and
        chains ``deleteLater`` to the thread's ``finished`` signal. The
        thread itself is reaped by the background teardown (see
        :meth:`_stop_cameras_background`), never by ``wait()`` here, so
        the GUI thread never blocks. Returns the worker (or None).
        """
        worker, self._startup_worker = self._startup_worker, None
        if worker is None:
            return None
        try:
            self._startup_abort.set()
        except Exception:
            pass
        for signal_name in ("camera_started", "camera_failed", "finished"):
            try:
                getattr(worker, signal_name).disconnect()
            except Exception:
                pass
        try:
            # Ownership leaves the dying window FIRST: window teardown
            # must never delete this (possibly blocked in start_camera)
            # thread. The finished -> deleteLater chain below owns it
            # from here on, plus the background teardown's bounded wait.
            worker.setParent(None)
            worker.finished.connect(worker.deleteLater)
        except RuntimeError:
            return None  # C++ object already gone; nothing to reap
        return worker

    def _take_down_startup_worker(self) -> None:
        """Stop the camera-startup worker deterministically.

        Sets the abort flag (checked between cameras), disconnects GUI
        slots FIRST so late queued results cannot reach them, then waits
        (bounded) for the thread to finish BEFORE deleteLater. Deleting
        a running QThread aborts the process with no Python traceback,
        so the wait-then-delete order here is load-bearing.

        Blocks up to ~8 s: use only on the shutdown path (where no UI
        must stay responsive), never on a visible mode transition (use
        :meth:`_detach_startup_worker` there).
        """
        worker, self._startup_worker = self._startup_worker, None
        if worker is None:
            return
        try:
            self._startup_abort.set()
        except Exception:
            pass
        for signal_name in ("camera_started", "camera_failed", "finished"):
            try:
                getattr(worker, signal_name).disconnect()
            except Exception:
                pass
        try:
            running = worker.isRunning()
        except RuntimeError:
            return  # C++ object already gone; nothing to reap
        if running:
            # One in-flight start_camera (3 s timeout) at most: the abort
            # flag stops the queue between cameras.
            if not worker.wait(8000):
                logger.warning(
                    "Startup worker still running after 8000 ms; "
                    "deferring deleteLater to its finished signal"
                )
                try:
                    worker.finished.connect(worker.deleteLater)
                except RuntimeError:
                    pass
                return
        try:
            worker.deleteLater()
        except RuntimeError:
            pass

    @pyqtSlot(str)
    def _on_startup_camera_ready(self, camera_id: str, token: int) -> None:
        """Attach the observer for a started camera (GUI thread, fast)."""
        if token != self._startup_token or camera_id not in self._camera_to_slot:
            self._stop_orphan_camera(camera_id)
            return
        if camera_id in self._connected_observers:
            try:
                if self._runtime_service.is_camera_running(camera_id):
                    return
            except Exception:
                pass
            self._connected_observers.discard(camera_id)
        analysis = self._config_service.get_analysis_config(camera_id)
        if analysis is None:
            analysis = AnalysisConfig(camera_id=camera_id)
        try:
            if isinstance(self._runtime_service, CameraRuntimeService):
                observer = self._runtime_service.start_observer(
                    camera_id, analysis_config=analysis, latest_wins=True
                )
            else:
                observer = self._runtime_service.start_observer(
                    camera_id, analysis_config=analysis
                )
        except Exception as exc:
            self._tiles[self._camera_to_slot[camera_id]].set_error(str(exc))
            self._refresh_header_and_panel()
            self._update_button_state()
            return
        tile = self._tiles[self._camera_to_slot[camera_id]]
        if hasattr(observer, "latest_result_ready") and hasattr(
            observer, "take_latest_result"
        ):
            observer.latest_result_ready.connect(
                lambda camera_id=camera_id: self._on_latest_result_available(
                    camera_id
                ),
                Qt.ConnectionType.QueuedConnection,
            )
        else:
            observer.result_ready.connect(
                tile.on_result, Qt.ConnectionType.QueuedConnection
            )
        observer.error_occurred.connect(tile.on_error, Qt.ConnectionType.QueuedConnection)
        self._connected_observers.add(camera_id)
        if tile.state is not LiveTileState.ERROR:
            tile.set_state(LiveTileState.STARTING)
        self._refresh_header_and_panel()

    @pyqtSlot()
    def _on_latest_result_available(self, camera_id: str) -> None:
        """Render only the newest result waiting for a Live camera."""
        if camera_id not in self._camera_to_slot or self._runtime_service is None:
            return
        observer = self._runtime_service.observer_service(camera_id)
        if observer is None or not hasattr(observer, "take_latest_result"):
            return
        result = observer.take_latest_result()
        if result is not None:
            self._tiles[self._camera_to_slot[camera_id]].on_result(result)

    @pyqtSlot(str, str)
    def _on_startup_camera_failed(self, camera_id: str, message: str, token: int) -> None:
        """Mark one camera failed; every other tile is untouched."""
        if token != self._startup_token or camera_id not in self._camera_to_slot:
            return
        self._tiles[self._camera_to_slot[camera_id]].set_error(message or "Camera failed")
        self._refresh_header_and_panel()
        self._update_button_state()

    @pyqtSlot()
    def _on_startup_finished(self, token: int) -> None:
        """Finalize the button once every queued camera has reported."""
        self._take_down_startup_worker()
        if token != self._startup_token:
            return
        self._refresh_header_and_panel()
        self._update_button_state()

    def _stop_orphan_camera(self, camera_id: str) -> None:
        """Tear down a camera that started after Live mode was left."""
        if self._runtime_service is None:
            return
        try:
            if self._runtime_service.is_camera_running(camera_id):
                self._runtime_service.stop_camera(camera_id)
        except Exception:
            pass

    def _update_button_state(self) -> None:
        """Reflect startup progress on the connect button (text only)."""
        button = self._connect_button
        if self._startup_worker is not None:
            # Owned startup session (running, queued, or finishing):
            # isRunning() alone would miss the just-created window.
            button.setText("STARTING...")
            button.setEnabled(False)
            return
        eligible = self._eligible_configs()
        all_configs = self._config_service.get_all_camera_configs()
        configured = len(all_configs)
        if self._runtime_service is None and self._legacy_observer is None:
            button.setText("NO CAMERAS")
            button.setEnabled(False)
            button.setToolTip("No runtime service available")
            return
        if configured == 0:
            button.setText("NO CAMERAS")
            button.setEnabled(False)
            button.setToolTip("No cameras configured (get_all_camera_configs()==0)")
            return
        if not eligible:
            # Configured cameras exist but none eligible (disabled or thermal_enabled=False)
            button.setText("NO ELIGIBLE CAMERAS")
            button.setEnabled(False)
            # Build explanation for tooltip
            disabled = sum(1 for c in all_configs if not getattr(c, "enabled", True))
            no_thermal = sum(1 for c in all_configs if not getattr(c, "thermal_enabled", True))
            button.setToolTip(f"Configured={configured} but eligible=0 (disabled={disabled} thermal_disabled={no_thermal})")
            logger.info("LIVE BUTTON: NO ELIGIBLE CAMERAS configured=%d disabled=%d thermal_disabled=%d", configured, disabled, no_thermal)
            return
        button.setToolTip("")
        counts = self._tile_counts()
        if counts["failed"] > 0:
            button.setText("RETRY FAILED")
            button.setEnabled(True)
            return
        if counts["assigned"] > 0 and counts["connected"] >= counts["assigned"]:
            button.setText("ALL CAMERAS LIVE")
            button.setEnabled(False)
            return
        button.setText("CONNECT & START ALL")
        button.setEnabled(True)

    def _start_via_legacy(self) -> None:
        """Start the legacy injected ObserverService (display-only tests)."""
        camera = self._first_configured_camera()
        if camera is None:
            self._set_summary("No configured cameras - configure one first")
            return
        camera_id = camera.identity.camera_id
        analysis = self._config_service.get_analysis_config(camera_id)
        if analysis is None:
            analysis = AnalysisConfig(camera_id=camera_id)

        slot = self._camera_to_slot.get(camera_id)
        if slot is not None:
            tile = self._tiles[slot]
            tile.set_state(LiveTileState.STARTING)
            self._legacy_observer.result_ready.connect(tile.on_result, Qt.ConnectionType.QueuedConnection)
            self._legacy_observer.error_occurred.connect(tile.on_error, Qt.ConnectionType.QueuedConnection)

        try:
            self._legacy_observer.start(camera_id, analysis_config=analysis)
            self._set_summary("Cameras: 1  Running: 1  Failed: 0")
        except Exception as exc:
            if slot is not None:
                self._tiles[slot].set_error(str(exc))
            self._set_summary("Cameras: 1  Running: 0  Failed: 1")

    def _disconnect_all_tiles(self) -> None:
        if self._runtime_service is not None:
            for camera_id, slot in self._camera_to_slot.items():
                observer = self._runtime_service.observer_service(camera_id)
                if observer is not None:
                    tile = self._tiles[slot]
                    if hasattr(observer, "latest_result_ready"):
                        try:
                            observer.latest_result_ready.disconnect()
                        except (TypeError, RuntimeError):
                            pass
                    self._safe_disconnect(observer.result_ready, tile.on_result)
                    self._safe_disconnect(observer.error_occurred, tile.on_error)
        elif self._legacy_observer is not None:
            for slot in self._camera_to_slot.values():
                tile = self._tiles[slot]
                self._safe_disconnect(self._legacy_observer.result_ready, tile.on_result)
                self._safe_disconnect(self._legacy_observer.error_occurred, tile.on_error)

    @staticmethod
    def _safe_disconnect(signal, slot) -> None:
        try:
            signal.disconnect(slot)
        except Exception:
            pass

    def _first_configured_camera(self):
        enabled = self._enabled_cameras()
        return enabled[0] if enabled else None

    def _update_summary(self, failed: list[str] | None = None) -> None:
        """Refresh the header summary and statistics panel (text only)."""
        counts = self._tile_counts()
        total_fps = counts["total_fps"]
        fps_text = f"{total_fps:.0f}" if total_fps is not None else "--"
        if counts["configured"] == 0:
            self._set_summary("No cameras configured")
        else:
            self._set_summary(
                f"Connected: {counts['connected']}/{counts['assigned']}   "
                f"FPS: ~{fps_text}   Status: {self._header_status_word(counts)}"
            )
        if self._stats_panel is not None:
            self._stats_panel.update_system(counts)

    def _set_summary(self, text: str) -> None:
        self._summary_label.setText(text)

    def _start_stats_timer(self) -> None:
        if self._stats_timer is None:
            self._stats_timer = QTimer(self)
            self._stats_timer.timeout.connect(self._poll_stats)
        self._stats_timer.start(self._stats_interval_ms)

    def _stop_stats_timer(self) -> None:
        if self._stats_timer is not None:
            self._stats_timer.stop()

    # -- Read-only Live PTZ monitor (Phase 7) ------------------------------
    #
    # A 2 s timer fans out to ONE retained daemon thread per tick (busy
    # guard skips overlaps). The thread reads authoritative status
    # through this wall's own PtzService -- get_status only, so Live can
    # never command a PTZ -- and results return via _ptz_readout with the
    # startup token. Stale tokens and unassigned cameras are dropped.

    def _start_ptz_timer(self) -> None:
        if self._ptz_timer is None:
            self._ptz_timer = QTimer(self)
            self._ptz_timer.timeout.connect(self._poll_ptz_status)
            self._ptz_timer.setInterval(2000)
        if not self._ptz_timer.isActive():
            self._ptz_timer.start()

    def _stop_ptz_timer(self) -> None:
        if self._ptz_timer is not None:
            try:
                self._ptz_timer.stop()
            except RuntimeError:
                pass

    def _live_ptz_service(self):
        """Lazily build this wall's read-only PTZ service (bg thread).

        Simulator profile only auto-connects; anything else leaves the
        tiles blank with a logged reason (never a silent fallback).
        """
        if self._ptz_service is not None:
            return self._ptz_service
        try:
            ptz_cfg = self._config_manager.get_config().ptz
            mappings = self._config_manager.get_config().cameras.mapping
        except Exception:
            return None
        from thermal_monitor.ptz.siemens import validate_profile

        status = validate_profile(
            getattr(ptz_cfg, "profile", "simulator"),
            getattr(ptz_cfg, "endpoint", ""),
            getattr(ptz_cfg, "security_mode", "none"),
            getattr(ptz_cfg, "username", ""),
        )
        if not status.ready or status.mapping != "simulator":
            logger.info("[PTZ-LIVE] monitor disabled: %s", status.reason)
            return None
        ptz_ids = tuple(
            sorted(
                {
                    (getattr(m, "ptz_id", "") or "").strip()
                    for m in (mappings or [])
                    if (getattr(m, "ptz_id", "") or "").strip()
                }
            )
        )
        if not ptz_ids:
            return None
        try:
            from thermal_monitor.ptz.mapping import SimulatorPtzMapping
            from thermal_monitor.ptz.models import PtzLimits, PtzStationBinding
            from thermal_monitor.ptz.station import build_service, build_service_config

            service = build_service(
                status.endpoint,
                SimulatorPtzMapping(ptz_ids=ptz_ids),
                build_service_config(
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
                ),
            )
            for m in mappings or []:
                ptz_id = (getattr(m, "ptz_id", "") or "").strip()
                if ptz_id:
                    try:
                        service.register_binding(
                            PtzStationBinding(
                                camera_id=m.camera_id, ptz_id=ptz_id
                            )
                        )
                    except Exception:
                        pass
            service.connect()
        except Exception as exc:
            logger.warning("[PTZ-LIVE] PTZ service unavailable: %s", exc)
            return None
        self._ptz_service = service
        return service

    def _poll_ptz_status(self) -> None:
        token = self._startup_token
        assigned = [
            camera_id
            for camera_id, slot in self._camera_to_slot.items()
            if 0 <= slot < len(self._tiles)
        ]
        if not assigned or self._ptz_poll_busy:
            return
        self._ptz_poll_busy = True

        def _bg() -> None:
            try:
                service = self._live_ptz_service()
                for camera_id in assigned:
                    if service is None:
                        break
                    try:
                        text, tip = self._ptz_text_for(service, camera_id)
                    except Exception:
                        continue
                    try:
                        self._ptz_readout.emit(token, camera_id, text, tip)
                    except RuntimeError:
                        return  # teardown; wall already gone
            finally:
                self._ptz_poll_busy = False
                self._ptz_threads.discard(thread)

        thread = threading.Thread(target=_bg, name="LivePtzPoll", daemon=True)
        self._ptz_threads.add(thread)
        thread.start()

    @staticmethod
    def _ptz_text_for(service, camera_id: str) -> tuple[str, str]:
        """Compact tile text + tooltip from authoritative status."""
        try:
            binding = service.binding_for_camera(camera_id)
        except Exception:
            return "", ""
        try:
            status = service.get_status(camera_id)
        except Exception as exc:
            return "PTZ --", f"{binding.ptz_id}: PTZ unavailable ({exc})"
        head = f"{binding.ptz_id} {status.actual_pan:.1f},{status.actual_tilt:.1f}"
        if status.error is not None:
            return "PTZ !err", f"{head} ERROR {status.error.message}"
        if status.calibration.value == "active":
            return "PTZ cal", f"{head} calibrating"
        if status.moving:
            return f"PTZ {status.actual_pan:.1f},{status.actual_tilt:.1f}…", head
        if not status.communication_ok or not status.plc_connected:
            return "PTZ --", f"{head} disconnected"
        if status.position_reached:
            return f"PTZ {status.actual_pan:.1f},{status.actual_tilt:.1f} =", head
        if not status.ready:
            return "PTZ n/a", f"{head} not ready"
        return f"PTZ {status.actual_pan:.1f},{status.actual_tilt:.1f}", head

    def _on_ptz_readout(
        self, token: int, camera_id: str, text: str, tooltip: str
    ) -> None:
        """Apply a readout only for the current wall generation."""
        if token != self._startup_token:
            return
        slot = self._camera_to_slot.get(camera_id)
        if slot is None or not 0 <= slot < len(self._tiles):
            return
        try:
            self._tiles[slot].set_ptz_status(text, tooltip)
        except RuntimeError:
            pass

    def _shutdown_ptz_services(self) -> None:
        service, self._ptz_service = self._ptz_service, None
        if service is None:
            return
        try:
            service.shutdown(timeout_s=5.0)
        except Exception:
            pass

    def _poll_stats(self) -> None:
        if self._runtime_service is not None:
            for camera_id, slot in self._camera_to_slot.items():
                tile = self._tiles[slot]
                if tile.state in (LiveTileState.ERROR, LiveTileState.NOT_AVAILABLE):
                    continue
                observer = self._runtime_service.observer_service(camera_id)
                obs_stats = observer.stats() if observer is not None else None
                cam_stats = self._runtime_service.camera_stats(camera_id)
                self._apply_worker_state(tile, cam_stats)
                self._apply_stats(tile, camera_id, obs_stats, cam_stats)
        elif self._legacy_observer is not None:
            for tile in self._tiles:
                if tile.camera_id:
                    self._apply_stats(tile, tile.camera_id, self._legacy_observer.stats(), None)
        self._refresh_header_and_panel()

    def _apply_worker_state(self, tile, cam_stats) -> None:
        """Reflect a driver-level reconnect on the tile (read-only).

        Compares against the ``"reconnecting"`` value without importing
        the acquisition domain, so the wall never depends on acquisition
        internals. The acquisition worker keeps reconnecting by itself;
        the tile only mirrors the state and keeps its last images.
        """
        worker_state = getattr(cam_stats, "state", None) if cam_stats is not None else None
        if worker_state == "reconnecting":
            if tile.state in (
                LiveTileState.RUNNING,
                LiveTileState.STARTING,
                LiveTileState.RECONNECTING,
            ):
                tile.set_state(LiveTileState.RECONNECTING)
        elif tile.state is LiveTileState.RECONNECTING:
            # Worker recovered; the next result flips the tile back to LIVE.
            tile.set_state(LiveTileState.STARTING)

    def _apply_stats(self, tile, camera_id, obs_stats, cam_stats) -> None:
        fps = None
        processed = None
        avg_ms = None
        dropped = None
        if cam_stats is not None:
            fps = cam_stats.current_fps or cam_stats.average_fps
            dropped = cam_stats.dropped
        if obs_stats is not None:
            processed = obs_stats.frames_processed
            avg_ms = obs_stats.average_processing_time_ms
            if fps is None and obs_stats.frames_processed > 0:
                import time
                now = obs_stats.last_processed_at or 0.0
                prev = self._last_poll.get(camera_id)
                if prev is not None and now > prev[1]:
                    fps = max(0.0, (obs_stats.frames_processed - prev[0]) / (now - prev[1]))
                self._last_poll[camera_id] = (obs_stats.frames_processed, now)
        tile.set_stats(
            fps=fps,
            processed_frames=processed,
            avg_processing_ms=avg_ms,
            dropped_frames=dropped,
        )

    def closeEvent(self, event) -> None:
        self.on_mode_deactivated()
        super().closeEvent(event)


class LiveWindow(QMainWindow):
    """Top-level window for Live mode."""

    def __init__(
        self,
        mode_service: ModeService,
        config_service: ConfigurationService,
        observer_service: ObserverService | None = None,
        *,
        runtime_service: CameraRuntimeService | None = None,
        theme_manager: Optional[ThemeManager] = None,
        config_manager=None,
    ) -> None:
        super().__init__()

        self._mode_service = mode_service
        self._config_service = config_service
        self._runtime_service = runtime_service
        self._theme = theme_manager
        self._config_manager = config_manager
        self._settings_menu_controller = None

        self.setWindowTitle("Thermal Monitoring System V3 - Live Mode")
        self._apply_window_config()

        # Central widget
        logger.info("LIVE WINDOW CREATED: config_service id=%s (%s) runtime_service id=%s", hex(id(config_service)), type(config_service).__name__, hex(id(runtime_service)) if runtime_service else "None")
        if config_manager is not None and hasattr(config_manager, "config_path"):
            logger.info("LIVE WINDOW config_path=%s exists=%s", config_manager.config_path, Path(config_manager.config_path).exists() if hasattr(config_manager, "config_path") else "?")
            tmp = Path(config_manager.config_path).with_suffix(Path(config_manager.config_path).suffix + ".tmp")
            logger.info("LIVE WINDOW tmp_path=%s exists=%s", tmp, tmp.exists())
        self._live_widget = LiveModeWidget(
            mode_service=mode_service,
            config_service=config_service,
            observer_service=observer_service,
            runtime_service=runtime_service,
            theme_manager=theme_manager,
            config_manager=config_manager,
        )
        logger.info("LIVE MODE WIDGET CREATED: widget_config_service id=%s same_as_window=%s", hex(id(self._live_widget._config_service)), hex(id(config_service)) == hex(id(self._live_widget._config_service)))
        self.setCentralWidget(self._live_widget)
        self._setup_settings_menu()

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_label = QLabel("Live Mode")
        set_role(self._status_label, "status")
        self._status_bar.addWidget(self._status_label)

    def _setup_settings_menu(self) -> None:
        """Add the top-left Settings menu with live Theme switching.

        Pure GUI chrome above the wall: it never touches grid geometry,
        camera positioning, feed sizing, or the render path.
        """
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
            self.setMinimumSize(config["live_min_width"], config["live_min_height"])
        else:
            self.setMinimumSize(640, 480)

    def on_mode_activated(self) -> None:
        """Called when Live mode becomes active."""
        self._live_widget.on_mode_activated()

    def on_mode_deactivated(self) -> None:
        """Called when Live mode is deactivated."""
        self._live_widget.on_mode_deactivated()

    def closeEvent(self, event) -> None:
        self.on_mode_deactivated()
        super().closeEvent(event)


__all__ = [
    "LiveCameraTile",
    "LiveStatsPanel",
    "LiveTileState",
    "LiveModeWidget",
    "LiveWindow",
    "FIXED_CAMERA_SLOTS",
    "GRID_COLUMNS",
    "GRID_ROWS",
    "STATS_GRID_ROW",
    "STATS_GRID_COL",
]
