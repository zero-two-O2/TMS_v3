"""
ui.modes.live -- Live mode widget (fixed 8-camera monitoring wall).

Displays the live thermal stream for up to 8 cameras in a fixed 2x4 grid.
Each camera slot remains stable regardless of availability.
Unavailable cameras show a clear "NOT AVAILABLE" state.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QScrollArea,
    QGroupBox,
    QLabel,
    QMessageBox,
)

import numpy as np

from thermal_monitor.core.models import AnalysisConfig, TemperatureUnit
from thermal_monitor.processing import ProcessingResult
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.observer import ObserverService
from thermal_monitor.services.runtime import CameraRuntimeService
from thermal_monitor.ui.modes.observer_image import LiveThermalWidget
from thermal_monitor.ui.theme import ThemeManager


_UNIT_SYMBOLS = {
    TemperatureUnit.CELSIUS: "°C",
    TemperatureUnit.FAHRENHEIT: "°F",
    TemperatureUnit.KELVIN: "K",
}


class LiveTileState(str, Enum):
    """High-level lifecycle state shown on a live camera tile."""

    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"
    NOT_AVAILABLE = "not_available"


_STATE_STYLES = {
    LiveTileState.STARTING: "color: #FFA000; font-weight: bold;",
    LiveTileState.RUNNING: "color: #2E7D32; font-weight: bold;",
    LiveTileState.ERROR: "color: #D32F2F; font-weight: bold;",
    LiveTileState.NOT_AVAILABLE: "color: #757575; font-weight: bold;",
}

_STATE_TEXT = {
    LiveTileState.STARTING: "STARTING",
    LiveTileState.RUNNING: "LIVE",
    LiveTileState.ERROR: "ERROR",
    LiveTileState.NOT_AVAILABLE: "NOT AVAILABLE",
}


# Fixed 8 camera slots - positions are stable
FIXED_CAMERA_SLOTS = 8
GRID_COLUMNS = 2
GRID_ROWS = 4


class LiveCameraTile(QWidget):
    """Independent live thermal tile for one camera slot.

    Each slot corresponds to a fixed camera position (1-8).
    If a camera is assigned to this slot, it shows live data.
    If no camera is assigned, it shows "NOT AVAILABLE".
    """

    def __init__(
        self,
        slot_index: int,
        camera_id: str | None = None,
        name: str = "",
        serial: str = "",
        theme_manager: Optional[ThemeManager] = None,
    ) -> None:
        super().__init__()
        self._slot_index = slot_index  # 0-7
        self._camera_id = camera_id
        self._name = name or (f"CAM {slot_index + 1:02d}" if camera_id else f"CAM {slot_index + 1:02d}")
        self._serial = serial
        self._theme = theme_manager

        self._latest_result: ProcessingResult | None = None
        self._frames_received = 0
        self._latest_sequence = -1
        self._state = LiveTileState.NOT_AVAILABLE if camera_id is None else LiveTileState.STARTING
        self._error_message: str | None = None

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Header: camera label + serial + state
        header = QHBoxLayout()
        header.setSpacing(6)
        self._label = QLabel(self._name)
        label_style = "font-weight: bold; font-size: 13px;"
        if self._theme:
            label_style += f" color: {self._theme.text()};"
        self._label.setStyleSheet(label_style)
        self._serial_label = QLabel(self._serial)
        serial_style = "font-size: 11px;"
        if self._theme:
            serial_style += f" color: {self._theme.text_secondary()};"
        else:
            serial_style += " color: #888;"
        self._serial_label.setStyleSheet(serial_style)
        self._state_label = QLabel(_STATE_TEXT[self._state])
        self._state_label.setStyleSheet(self._state_style(self._state))
        header.addWidget(self._label)
        header.addWidget(self._serial_label)
        header.addStretch()
        header.addWidget(self._state_label)
        layout.addLayout(header)

        # Main image
        self._image_widget = LiveThermalWidget()
        self._image_widget.setMinimumSize(280, 210)
        layout.addWidget(self._image_widget, 1)

        # Footer: compact stats
        footer = QHBoxLayout()
        footer.setContentsMargins(4, 2, 4, 2)
        footer.setSpacing(8)
        self._fps_label = QLabel("FPS: —")
        self._sequence_label = QLabel("Seq: —")
        self._temp_label = QLabel("Temp: —")
        footer.addWidget(self._fps_label)
        footer.addWidget(self._sequence_label)
        footer.addWidget(self._temp_label)
        footer.addStretch()
        layout.addLayout(footer)

    def _state_style(self, state: LiveTileState) -> str:
        """Get stylesheet for a tile state from theme."""
        if not self._theme:
            return _STATE_STYLES.get(state, "")
        color_map = {
            LiveTileState.STARTING: self._theme.warning(),
            LiveTileState.RUNNING: self._theme.success(),
            LiveTileState.ERROR: self._theme.error(),
            LiveTileState.NOT_AVAILABLE: self._theme.disabled_text(),
        }
        color = color_map.get(state, self._theme.text())
        return f"color: {color}; font-weight: bold;"

    @pyqtSlot(object)
    def on_result(self, result: ProcessingResult) -> None:
        """Receive a ProcessingResult for this camera on the GUI thread."""
        if self._camera_id is None:
            return

        self._latest_result = result
        self._frames_received += 1

        # CRITICAL: copy the temperature buffer before retaining any display
        # data, so we never share memory with the consumer's mutable result.
        temperature_image = result.temperature_image
        if temperature_image is not None:
            temperature_image = np.asarray(temperature_image).copy()

        frame = result.frame
        self._image_widget.set_frame(temperature_image, frame)

        self._latest_sequence = frame.descriptor.sequence
        self._update_display(result)

        if self._state is LiveTileState.STARTING:
            self.set_state(LiveTileState.RUNNING)

    @pyqtSlot(str)
    def on_error(self, message: str) -> None:
        self.set_state(LiveTileState.ERROR, message)

    def _update_display(self, result: ProcessingResult) -> None:
        """Update the tile display from a processing result."""
        self._sequence_label.setText(f"Seq: {result.frame.descriptor.sequence}")

        # FPS from stats (updated separately via set_stats)
        # Temperature
        analysis = result.analysis_result
        if analysis is not None and analysis.overall_mean is not None:
            unit_symbol = _UNIT_SYMBOLS.get(getattr(analysis, "unit", None), "°C")
            self._temp_label.setText(f"{unit_symbol} {analysis.overall_mean:.1f}")
        else:
            self._temp_label.setText("Temp: —")

    def set_state(self, state: LiveTileState, message: str | None = None) -> None:
        self._state = state
        if message is not None:
            self._error_message = message
        self._state_label.setText(_STATE_TEXT[state])
        self._state_label.setStyleSheet(self._state_style(state))

        if state == LiveTileState.ERROR and message:
            self._temp_label.setText(message)
        elif state == LiveTileState.NOT_AVAILABLE:
            self._image_widget.clear()
            self._fps_label.setText("FPS: —")
            self._sequence_label.setText("Seq: —")
            self._temp_label.setText("Camera not configured")

    def set_error(self, message: str) -> None:
        self.set_state(LiveTileState.ERROR, message)
        self._temp_label.setText(message or "Camera failed")

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
            self._fps_label.setText(f"FPS: {fps:.1f}")
        if processed_frames is not None:
            self._sequence_label.setText(f"Frames: {processed_frames}")
        elif avg_processing_ms is not None:
            self._fps_label.setText(f"{avg_processing_ms:.1f} ms")
        if dropped_frames is not None and dropped_frames > 0:
            self._sequence_label.setToolTip(f"Dropped: {dropped_frames}")

    def clear(self) -> None:
        self._latest_result = None
        self._frames_received = 0
        self._latest_sequence = -1
        self._error_message = None
        self._image_widget.clear()
        self._fps_label.setText("FPS: —")
        self._sequence_label.setText("Seq: —")
        self._temp_label.setText("Temp: —")

    def set_camera(self, camera_id: str, name: str, serial: str) -> None:
        """Assign a camera to this slot."""
        self._camera_id = camera_id
        self._name = name
        self._serial = serial
        self._label.setText(name)
        self._serial_label.setText(serial)
        self.set_state(LiveTileState.STARTING)

    def clear_camera(self) -> None:
        """Remove camera assignment from this slot."""
        self._camera_id = None
        self._name = f"CAM {self._slot_index + 1:02d}"
        self._serial = ""
        self._label.setText(self._name)
        self._serial_label.setText("")
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


class LiveModeWidget(QWidget):
    """Live monitoring mode with fixed 8-camera wall.

    Always shows exactly 8 tiles in a 2x4 grid.
    Camera slots are stable - camera 1 is always top-left, camera 8 is always bottom-right.
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
        self._stats_timer: QTimer | None = None
        self._last_poll: dict[str, tuple[int, float]] = {}

        self._setup_ui()
        self._create_fixed_tiles()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Header
        header = QHBoxLayout()
        title = QLabel("LIVE MODE — 8 Camera Monitoring Wall")
        title_style = "font-size: 20px; font-weight: bold;"
        if self._theme:
            title_style += f" color: {self._theme.success()};"
        else:
            title_style += " color: #2E7D32;"
        title.setStyleSheet(title_style)
        header.addWidget(title)
        header.addStretch()
        self._summary_label = QLabel("Initializing...")
        summary_style = "font-weight: bold;"
        if self._theme:
            summary_style += f" color: {self._theme.text()};"
        self._summary_label.setStyleSheet(summary_style)
        header.addWidget(self._summary_label)
        layout.addLayout(header)

        # Fixed 8-tile grid in scroll area
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._grid_widget = QWidget()
        self._grid_layout = QGridLayout(self._grid_widget)
        self._grid_layout.setContentsMargins(4, 4, 4, 4)
        if self._theme:
            self._grid_layout.setSpacing(self._theme.live_config().get("tile_gap", 8))
        else:
            self._grid_layout.setSpacing(8)
        self._scroll.setWidget(self._grid_widget)
        layout.addWidget(self._scroll, 1)

    def _create_fixed_tiles(self) -> None:
        """Create exactly 8 fixed tiles in a 2x4 grid."""
        self._tiles.clear()
        for slot in range(FIXED_CAMERA_SLOTS):
            tile = LiveCameraTile(slot, theme_manager=self._theme)
            self._tiles.append(tile)
            row = slot // GRID_COLUMNS
            col = slot % GRID_COLUMNS
            self._grid_layout.addWidget(tile, row, col)

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
        self._set_summary("Stopped")

    def _assign_cameras_to_slots(self) -> None:
        """Assign enabled cameras to fixed slots (1-8)."""
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
            self._set_summary("No configured cameras — configure one first")
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
        not_available = sum(
            1 for t in self._tiles if t.state is LiveTileState.NOT_AVAILABLE
        )
        self._set_summary(
            f"Cameras: {configured}  Running: {running}  Failed: {failed_count}  Not Available: {not_available}"
        )

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


__all__ = ["LiveCameraTile", "LiveTileState", "LiveModeWidget", "LiveThermalWidget"]