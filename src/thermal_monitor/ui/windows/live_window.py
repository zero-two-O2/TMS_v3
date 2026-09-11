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

from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QScrollArea,
    QLabel,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
)

import numpy as np

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
)


_UNIT_SYMBOLS = {
    TemperatureUnit.CELSIUS: "C",
    TemperatureUnit.FAHRENHEIT: "F",
    TemperatureUnit.KELVIN: "K",
}


class LiveTileState(str, Enum):
    """High-level lifecycle state shown on a live camera tile."""

    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"
    NOT_AVAILABLE = "not_available"


#: LiveTileState mapped onto the global status vocabulary (the central
#: stylesheet resolves the actual colors for every theme).
_STATE_STATUS = {
    LiveTileState.STARTING: "starting",
    LiveTileState.RUNNING: "running",
    LiveTileState.ERROR: "error",
    LiveTileState.NOT_AVAILABLE: "not_available",
}

_STATE_TEXT = {
    LiveTileState.STARTING: "STARTING",
    LiveTileState.RUNNING: "LIVE",
    LiveTileState.ERROR: "ERROR",
    LiveTileState.NOT_AVAILABLE: "NOT AVAILABLE",
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
        self._last_temp_unit: str = "C"
        self._state = LiveTileState.NOT_AVAILABLE if camera_id is None else LiveTileState.STARTING
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
        # Metric readouts never drive tile width (feeds do); they clip
        # instead of stretching narrow tiles. The state badge keeps its
        # preferred width so LIVE/ERROR stays fully readable.
        for readout in (
            self._fps_label,
            self._temp_label,
            self._age_label,
            self._sequence_label,
        ):
            readout.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        status.addWidget(self._state_label)
        status.addWidget(self._fps_label)
        status.addWidget(self._temp_label)
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
            "name": self._name if self._name else "Not configured",
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
        if self._last_temp is None:
            return "--"
        return f"{self._last_temp:.1f} {self._last_temp_unit}"

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
            LiveTileState.STARTING: self._theme.warning(),
            LiveTileState.RUNNING: self._theme.success(),
            LiveTileState.ERROR: self._theme.error(),
            LiveTileState.NOT_AVAILABLE: self._theme.disabled_text(),
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
        display_name = self._name if self._name else "Not configured"
        self._name_label.setText(display_name)
        if self._camera_id:
            detail = self._camera_id
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

        frame = result.frame
        self._image_widget.set_frame(temperature_image, frame)

        # VL feed: same result carries the same hardware frame, so IR/VL
        # correlation holds by construction. Both feeds submit every frame
        # to their existing bounded latest-wins workers; stale sequences
        # are dropped inside the feed widgets, never queued here.
        visible = frame.payload.visible if frame is not None else None
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

        if self._state is LiveTileState.STARTING:
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
            # Keep the last images and the camera identity visible; the
            # reason travels in tooltips so the fixed row never grows.
            pass
        elif state == LiveTileState.NOT_AVAILABLE:
            self._image_widget.clear()
            self._vl_widget.clear()
            self._last_temp = None
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
        self._error_message = None
        self._image_widget.clear()
        self._vl_widget.clear()
        self._fps_label.setText("-- fps")
        self._sequence_label.setText("--")
        self._sequence_label.setToolTip("")
        self._temp_label.setText("--")
        self._age_label.setText("-- ms")

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

    def set_camera(self, camera_id: str, name: str, serial: str) -> None:
        """Assign a camera to this fixed position slot."""
        self._camera_id = camera_id
        self._name = name
        self._serial = serial
        self._refresh_identity()
        self.set_state(LiveTileState.STARTING)

    def clear_camera(self) -> None:
        """Remove camera assignment; the tile keeps its position and size."""
        self._camera_id = None
        self._name = ""
        self._serial = ""
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
            f"Running: {snapshot.get('running', '--')}   "
            f"Failed: {snapshot.get('failed', '--')}"
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


class LiveModeWidget(QWidget):
    """Live monitoring mode with a 3x3 wall.

    Eight permanent camera positions plus one statistics panel. Position
    N is always tile N; disconnects never remove, reorder or resize.
    """

    def __init__(
        self,
        mode_service: ModeService,
        config_service: ConfigurationService,
        observer_service: ObserverService | None = None,
        *,
        runtime_service: CameraRuntimeService | None = None,
        stats_interval_ms: int = 1000,
        theme_manager: Optional[ThemeManager] = None,
    ) -> None:
        super().__init__()
        self._mode_service = mode_service
        self._config_service = config_service
        self._runtime_service = runtime_service
        self._legacy_observer = observer_service
        self._stats_interval_ms = stats_interval_ms
        self._theme = theme_manager

        self._tiles: list[LiveCameraTile] = []  # Fixed 8 tiles, index = slot
        self._camera_to_slot: dict[str, int] = {}  # camera_id -> slot index
        self._stats_panel: LiveStatsPanel | None = None  # Permanent ninth cell
        self._hovered: tuple[int | None, str | None] | None = None
        self._stats_timer: QTimer | None = None
        self._last_poll: dict[str, tuple[int, float]] = {}

        self._setup_ui()
        self._create_fixed_tiles()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(WALL_OUTER_MARGIN, WALL_OUTER_MARGIN, WALL_OUTER_MARGIN, WALL_OUTER_MARGIN)
        layout.setSpacing(2)

        # Compact header bar: wall identity plus overall summary only.
        self._header_bar = QWidget()
        header = QHBoxLayout(self._header_bar)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        title = QLabel("LIVE  •  8 CAMERAS  •  16 FEEDS")
        set_role(title, "strong")
        header.addWidget(title)
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

    def _system_snapshot(self) -> dict:
        """Aggregate wall state from existing tile data (text-ready)."""
        configured = len(self._config_service.get_all_camera_configs())
        running = sum(
            1 for t in self._tiles
            if t.state is not LiveTileState.ERROR and t.state is not LiveTileState.NOT_AVAILABLE
        )
        failed = sum(1 for t in self._tiles if t.state is LiveTileState.ERROR)
        fps_values = [t._last_fps for t in self._tiles if t._last_fps is not None]
        return {
            "configured": configured,
            "extra": max(0, configured - FIXED_CAMERA_SLOTS),
            "running": running,
            "failed": failed,
            "total_fps": sum(fps_values) if fps_values else None,
            "temps": [(t.slot_index + 1, t.temp_text()) for t in self._tiles],
        }

    def _refresh_header_and_panel(self) -> None:
        """Refresh the compact header and statistics panel (text only)."""
        snapshot = self._system_snapshot()
        configured = snapshot["configured"]
        running = snapshot["running"]
        failed = snapshot["failed"]
        total_fps = snapshot["total_fps"]
        fps_text = f"{total_fps:.0f}" if total_fps is not None else "--"
        if configured == 0:
            self._set_summary("No cameras configured")
        else:
            status = "NORMAL" if failed == 0 else "DEGRADED"
            self._set_summary(
                f"Connected: {running}/{configured}   "
                f"FPS: ~{fps_text}   Status: {status}"
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
        """Start live monitoring when Live mode becomes active."""
        self._assign_cameras_to_slots()
        if self._runtime_service is not None:
            self._start_via_runtime()
        elif self._legacy_observer is not None:
            self._start_via_legacy()
        else:
            self._set_summary("Runtime service not available")
        self._start_stats_timer()

    def on_mode_deactivated(self) -> None:
        """Stop live monitoring and tear down when leaving Live mode."""
        self._stop_stats_timer()
        self._disconnect_all_tiles()
        if self._runtime_service is not None:
            for camera_id in list(self._camera_to_slot.keys()):
                try:
                    self._runtime_service.stop_camera(camera_id)
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception("Failed to stop camera %s on mode exit", camera_id)
        elif self._legacy_observer is not None:
            try:
                self._legacy_observer.stop()
            except Exception:
                pass
        # Reset tiles to not available
        for tile in self._tiles:
            tile.clear_camera()
        self._camera_to_slot.clear()
        self._hovered = None
        if self._stats_panel is not None:
            self._stats_panel.show_system()
        self._set_summary("Stopped")

    def _assign_cameras_to_slots(self) -> None:
        """Assign enabled cameras to fixed slots (1-8) in config order."""
        enabled_cameras = self._enabled_cameras()

        # Clear all tiles first
        for tile in self._tiles:
            tile.clear_camera()
        self._camera_to_slot.clear()

        # Assign cameras to slots 0-7 (up to 8 cameras)
        for slot_index, config in enumerate(enabled_cameras[:FIXED_CAMERA_SLOTS]):
            camera_id = config.identity.camera_id
            tile = self._tiles[slot_index]
            tile.set_camera(camera_id, config.name or camera_id, config.identity.serial_number)
            self._camera_to_slot[camera_id] = slot_index

    def _enabled_cameras(self) -> list:
        result = []
        for config in self._config_service.get_all_camera_configs():
            if getattr(config, "enabled", True) and getattr(config, "thermal_enabled", True):
                result.append(config)
        return result

    def _start_via_runtime(self) -> None:
        """Start every enabled camera through the lifecycle service."""
        enabled = self._enabled_cameras()
        if not enabled:
            self._set_summary("Cameras: 0  Running: 0  Failed: 0")
            return

        failed: list[str] = []
        for config in enabled[:FIXED_CAMERA_SLOTS]:
            camera_id = config.identity.camera_id
            slot = self._camera_to_slot.get(camera_id)
            if slot is None:
                continue

            tile = self._tiles[slot]

            try:
                if not self._runtime_service.is_camera_running(camera_id):
                    self._runtime_service.start_camera(config)
            except Exception as exc:
                tile.set_error(str(exc))
                failed.append(camera_id)
                continue

            analysis = self._config_service.get_analysis_config(camera_id)
            if analysis is None:
                analysis = AnalysisConfig(camera_id=camera_id)

            try:
                observer = self._runtime_service.start_observer(
                    camera_id, analysis_config=analysis
                )
            except Exception as exc:
                tile.set_error(str(exc))
                failed.append(camera_id)
                continue

            tile.set_state(LiveTileState.STARTING)
            observer.result_ready.connect(tile.on_result, Qt.ConnectionType.QueuedConnection)
            observer.error_occurred.connect(tile.on_error, Qt.ConnectionType.QueuedConnection)

        self._update_summary(failed)

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
        configured = len(self._config_service.get_all_camera_configs())
        running = sum(
            1 for t in self._tiles if t.state is not LiveTileState.ERROR and t.state is not LiveTileState.NOT_AVAILABLE
        )
        if failed is None:
            failed_count = sum(
                1 for t in self._tiles if t.state is LiveTileState.ERROR
            )
        else:
            failed_count = len(failed)
        total_values = [t._last_fps for t in self._tiles if t._last_fps is not None]
        fps_text = f"{sum(total_values):.0f}" if total_values else "--"
        if configured == 0:
            self._set_summary("No cameras configured")
        else:
            status = "NORMAL" if failed_count == 0 else "DEGRADED"
            self._set_summary(
                f"Connected: {running}/{configured}   "
                f"FPS: ~{fps_text}   Status: {status}"
            )
        if self._stats_panel is not None:
            self._stats_panel.update_system(self._system_snapshot())

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

    def _poll_stats(self) -> None:
        if self._runtime_service is not None:
            for camera_id, slot in self._camera_to_slot.items():
                tile = self._tiles[slot]
                if tile.state in (LiveTileState.ERROR, LiveTileState.NOT_AVAILABLE):
                    continue
                observer = self._runtime_service.observer_service(camera_id)
                obs_stats = observer.stats() if observer is not None else None
                cam_stats = self._runtime_service.camera_stats(camera_id)
                self._apply_stats(tile, camera_id, obs_stats, cam_stats)
        elif self._legacy_observer is not None:
            for tile in self._tiles:
                if tile.camera_id:
                    self._apply_stats(tile, tile.camera_id, self._legacy_observer.stats(), None)
        self._refresh_header_and_panel()

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
        self._live_widget = LiveModeWidget(
            mode_service=mode_service,
            config_service=config_service,
            observer_service=observer_service,
            runtime_service=runtime_service,
            theme_manager=theme_manager,
        )
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
