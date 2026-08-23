"""
ui.modes.observer -- Observer mode widget (live multi-camera monitoring).

Displays the live thermal stream for every enabled camera simultaneously.
Each camera gets its own independent :class:`CameraTile`; tiles consume
``ProcessingResult`` objects produced by the ProcessingConsumer (bridged to
the GUI thread by the per-camera ``ObserverService``) and show:

- live thermal image (temperature image when available, raw thermal otherwise)
- frame information (camera id, sequence, timestamp)
- overall temperature statistics
- active alarm state
- processing status / FPS

The widget only talks to the ``ObserverService`` (one per camera) and the
``CameraRuntimeService`` lifecycle controller; it never touches the camera
driver, HALCON, the acquisition loop, the shared-memory ring, or recording.
"""

from __future__ import annotations

from enum import Enum

from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QImage, QPainter, QColor, QFont
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QScrollArea,
    QGroupBox,
    QFormLayout,
    QLabel,
)

import numpy as np

from thermal_monitor.core.models import AnalysisConfig, TemperatureUnit
from thermal_monitor.processing import ProcessingResult
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.observer import ObserverService
from thermal_monitor.services.runtime import CameraRuntimeService
from thermal_monitor.ui.modes.observer_image import LiveThermalWidget


_UNIT_SYMBOLS = {
    TemperatureUnit.CELSIUS: "°C",
    TemperatureUnit.FAHRENHEIT: "°F",
    TemperatureUnit.KELVIN: "K",
}


class CameraTileState(str, Enum):
    """High-level lifecycle state shown on a camera tile."""

    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"
    STOPPED = "stopped"


_STATE_STYLES = {
    CameraTileState.STARTING: "color: #FFA000; font-weight: bold;",
    CameraTileState.RUNNING: "color: #2E7D32; font-weight: bold;",
    CameraTileState.ERROR: "color: #D32F2F; font-weight: bold;",
    CameraTileState.STOPPED: "color: #757575; font-weight: bold;",
}

_STATE_TEXT = {
    CameraTileState.STARTING: "STARTING",
    CameraTileState.RUNNING: "RUNNING",
    CameraTileState.ERROR: "ERROR",
    CameraTileState.STOPPED: "STOPPED",
}


def _columns_for_camera_count(count: int) -> int:
    """Responsive column policy for the observer grid (1..8 cameras)."""
    if count <= 1:
        return 1
    if count == 2:
        return 2
    if count <= 4:
        return 2
    if count <= 6:
        return 3
    return 4


class CameraTile(QWidget):
    """Independent live thermal tile for one camera.

    Owns the rendering of a single camera's stream. It copies the incoming
    temperature image before retaining any display buffer, so it never shares
    memory with the mutable processing result owned by the ProcessingConsumer.
    """

    def __init__(self, camera_id: str, name: str = "", serial: str = "") -> None:
        super().__init__()
        self._camera_id = camera_id
        self._name = name or camera_id
        self._serial = serial

        self._latest_result: ProcessingResult | None = None
        self._frames_received = 0
        self._latest_sequence = -1
        self._state = CameraTileState.STARTING
        self._error_message: str | None = None

        self._setup_ui()

    # ─── UI construction ───────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Header: name + serial + state
        header = QHBoxLayout()
        header.setSpacing(6)
        self._name_label = QLabel(self._name)
        self._name_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        self._serial_label = QLabel(self._serial)
        self._serial_label.setStyleSheet("color: #888; font-size: 11px;")
        self._state_label = QLabel(_STATE_TEXT[self._state])
        self._state_label.setStyleSheet(_STATE_STYLES[self._state])
        header.addWidget(self._name_label)
        header.addWidget(self._serial_label)
        header.addStretch()
        header.addWidget(self._state_label)
        layout.addLayout(header)

        # Main image
        self._image_widget = LiveThermalWidget()
        self._image_widget.setMinimumSize(240, 180)
        layout.addWidget(self._image_widget, 1)

        # Footer: stats
        footer = QFormLayout()
        footer.setContentsMargins(4, 2, 4, 2)
        footer.setSpacing(2)
        self._fps_label = QLabel("—")
        self._sequence_label = QLabel("—")
        self._temp_label = QLabel("—")
        self._temp_min = QLabel("—")
        self._temp_max = QLabel("—")
        self._proc_label = QLabel("—")
        self._alarm_label = QLabel("No alarm evaluation")
        self._alarm_label.setWordWrap(True)
        footer.addRow("FPS:", self._fps_label)
        footer.addRow("Seq:", self._sequence_label)
        footer.addRow("Temp:", self._temp_label)
        footer.addRow("Min:", self._temp_min)
        footer.addRow("Max:", self._temp_max)
        footer.addRow("Proc:", self._proc_label)
        footer.addRow("Alarm:", self._alarm_label)
        layout.addLayout(footer)

    # ─── Result handling ───────────────────────────────────────────────────

    @pyqtSlot(object)
    def on_result(self, result: ProcessingResult) -> None:
        """Receive a ProcessingResult for this camera on the GUI thread."""
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
        self._sequence_label.setText(str(frame.descriptor.sequence))
        self._update_temperature(result.analysis_result)
        self._update_alarms(result.alarm_result)
        self._proc_label.setText(f"{result.processing_time_ms:.1f} ms")

        if self._state is CameraTileState.STARTING:
            self.set_state(CameraTileState.RUNNING)

    @pyqtSlot(str)
    def on_error(self, message: str) -> None:
        self.set_error(message)

    def _update_temperature(self, analysis_result) -> None:
        if analysis_result is None:
            self._temp_label.setText("—")
            self._temp_min.setText("—")
            self._temp_max.setText("—")
            return

        unit_symbol = _UNIT_SYMBOLS.get(getattr(analysis_result, "unit", None), "")
        self._temp_label.setText(unit_symbol or "—")

        if (
            analysis_result.overall_min is None
            and analysis_result.overall_max is None
            and analysis_result.overall_mean is None
        ):
            self._temp_min.setText("No thermal data")
            self._temp_max.setText("—")
            return

        def fmt(value):
            return "—" if value is None else f"{value:.1f}"

        if analysis_result.overall_mean is not None:
            self._temp_label.setText(f"{unit_symbol} {analysis_result.overall_mean:.1f}")
        self._temp_min.setText(fmt(analysis_result.overall_min))
        self._temp_max.setText(fmt(analysis_result.overall_max))

    def _update_alarms(self, alarm_result) -> None:
        if alarm_result is None:
            self._alarm_label.setText("No alarm evaluation")
            self._alarm_label.setStyleSheet("color: #666;")
            return
        active = tuple(alarm_result.active_alarms or ())
        if not active:
            self._alarm_label.setText("No active alarms")
            self._alarm_label.setStyleSheet("color: #666;")
            return
        self._alarm_label.setText(", ".join(active))
        self._alarm_label.setStyleSheet("color: #D32F2F; font-weight: bold;")

    # ─── State / stats ─────────────────────────────────────────────────────

    def set_state(self, state: CameraTileState, message: str | None = None) -> None:
        self._state = state
        if message is not None:
            self._error_message = message
        self._state_label.setText(_STATE_TEXT[state])
        self._state_label.setStyleSheet(_STATE_STYLES[state])

    def set_error(self, message: str) -> None:
        self.set_state(CameraTileState.ERROR, message)
        self._alarm_label.setText(message or "Camera failed")
        self._alarm_label.setStyleSheet("color: #D32F2F; font-weight: bold;")

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
            self._fps_label.setText(f"{fps:.1f}")
        if processed_frames is not None:
            self._proc_label.setText(f"{processed_frames} f / {avg_processing_ms:.1f} ms" if avg_processing_ms is not None else f"{processed_frames} f")
        elif avg_processing_ms is not None:
            self._proc_label.setText(f"{avg_processing_ms:.1f} ms")
        if dropped_frames is not None and dropped_frames > 0:
            self._sequence_label.setToolTip(f"Dropped: {dropped_frames}")

    def clear(self) -> None:
        self._latest_result = None
        self._frames_received = 0
        self._latest_sequence = -1
        self._error_message = None
        self._image_widget.clear()
        self._fps_label.setText("—")
        self._sequence_label.setText("—")
        self._temp_label.setText("—")
        self._temp_min.setText("—")
        self._temp_max.setText("—")
        self._proc_label.setText("—")
        self._alarm_label.setText("No alarm evaluation")
        self._alarm_label.setStyleSheet("color: #666;")

    # ─── Inspection hooks (tests) ──────────────────────────────────────────

    @property
    def camera_id(self) -> str:
        return self._camera_id

    @property
    def state(self) -> CameraTileState:
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


class ObserverModeWidget(QWidget):
    """Live multi-camera monitoring for Observer mode.

    Owns the lifecycle of the per-camera runtimes: entering the mode starts the
    producer (camera acquisition) and the observer for every *enabled* camera
    through ``CameraRuntimeService`` and creates one :class:`CameraTile` per
    camera.  Leaving the mode stops every runtime.  The widget only talks to
    the lifecycle service; it never touches the camera driver, HALCON, the
    acquisition loop, the shared-memory ring, or recording.

    A legacy single-``ObserverService`` may be injected for display-only tests,
    in which case exactly one tile is created and bound to that service.
    """

    def __init__(
        self,
        mode_service: ModeService,
        config_service: ConfigurationService,
        observer_service: ObserverService | None = None,
        *,
        runtime_service: CameraRuntimeService | None = None,
        stats_interval_ms: int = 1000,
    ) -> None:
        super().__init__()
        self._mode_service = mode_service
        self._config_service = config_service
        self._runtime_service = runtime_service
        self._legacy_observer = observer_service
        self._stats_interval_ms = stats_interval_ms

        self._tiles: dict[str, CameraTile] = {}
        self._tile_order: list[str] = []
        self._stats_timer: QTimer | None = None
        self._last_poll: dict[str, tuple[int, float]] = {}

        self._setup_ui()

        if observer_service is not None:
            self._legacy_observer = observer_service

    @property
    def _observer_service(self) -> ObserverService | None:
        """The active legacy ObserverService (display-only tests)."""
        if self._runtime_service is not None:
            return None
        return self._legacy_observer

    # ─── UI construction ───────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        header = QHBoxLayout()
        title = QLabel("OBSERVER MODE")
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #2196F3;")
        header.addWidget(title)
        header.addStretch()
        self._summary_label = QLabel("Idle")
        self._summary_label.setStyleSheet("font-weight: bold;")
        header.addWidget(self._summary_label)
        layout.addLayout(header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._grid_widget = QWidget()
        self._grid_layout = QGridLayout(self._grid_widget)
        self._grid_layout.setContentsMargins(4, 4, 4, 4)
        self._grid_layout.setSpacing(6)
        self._scroll.setWidget(self._grid_widget)
        layout.addWidget(self._scroll, 1)

        self._empty_label = QLabel("No enabled cameras running.")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet("color: #888; font-size: 14px;")
        self._empty_label.setVisible(True)
        self._grid_layout.addWidget(self._empty_label, 0, 0)

    # ─── Lifecycle ─────────────────────────────────────────────────────────

    def on_mode_activated(self) -> None:
        """Start live monitoring when Observer mode becomes active."""
        if self._runtime_service is not None:
            self._start_via_runtime()
        elif self._legacy_observer is not None:
            self._start_via_legacy()
        else:
            self._set_summary("Observer service not available")

    def on_mode_deactivated(self) -> None:
        """Stop live monitoring and tear down tiles when leaving Observer mode."""
        self._stop_stats_timer()
        self._disconnect_all_tiles()
        if self._runtime_service is not None:
            for camera_id in list(self._tiles.keys()):
                try:
                    self._runtime_service.stop_camera(camera_id)
                except Exception:
                    logger = __import__("logging").getLogger(__name__)
                    logger.exception("Failed to stop camera %s on mode exit", camera_id)
        elif self._legacy_observer is not None:
            try:
                self._legacy_observer.stop()
            except Exception:
                pass
        self._clear_tiles()
        self._set_summary("Stopped")

    def _start_via_runtime(self) -> None:
        """Start every enabled camera through the lifecycle service."""
        enabled = self._enabled_cameras()
        if not enabled:
            self._show_empty_state()
            self._set_summary("Cameras: 0  Running: 0  Failed: 0")
            return

        failed: list[str] = []
        for config in enabled:
            camera_id = config.identity.camera_id
            try:
                if not self._runtime_service.is_camera_running(camera_id):
                    self._runtime_service.start_camera(config)
            except Exception as exc:
                tile = self._add_tile(camera_id, config)
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
                tile = self._add_tile(camera_id, config)
                tile.set_error(str(exc))
                failed.append(camera_id)
                # Camera acquisition keeps running; only observer failed.
                continue

            tile = self._add_tile(camera_id, config)
            tile.set_state(CameraTileState.STARTING)
            observer.result_ready.connect(tile.on_result, Qt.ConnectionType.QueuedConnection)
            observer.error_occurred.connect(tile.on_error, Qt.ConnectionType.QueuedConnection)

        self._relayout_grid()
        self._update_summary(failed)
        self._start_stats_timer()

    def _start_via_legacy(self) -> None:
        """Start the legacy injected ObserverService (display-only tests)."""
        camera = self._first_configured_camera()
        if camera is None:
            self._show_empty_state()
            self._set_summary("No configured cameras — configure one first")
            return
        camera_id = camera.identity.camera_id
        analysis = self._config_service.get_analysis_config(camera_id)
        if analysis is None:
            analysis = AnalysisConfig(camera_id=camera_id)

        # Create and wire the tile first so the signal bridge is always live
        # (an injected display-only observer may already be producing results).
        tile = self._add_tile(camera_id, camera)
        tile.set_state(CameraTileState.STARTING)
        self._legacy_observer.result_ready.connect(tile.on_result, Qt.ConnectionType.QueuedConnection)
        self._legacy_observer.error_occurred.connect(tile.on_error, Qt.ConnectionType.QueuedConnection)
        self._relayout_grid()

        try:
            self._legacy_observer.start(camera_id, analysis_config=analysis)
            self._set_summary("Cameras: 1  Running: 1  Failed: 0")
        except Exception as exc:
            tile.set_error(str(exc))
            self._set_summary("Cameras: 1  Running: 0  Failed: 1")
        self._start_stats_timer()

    # ─── Tile management ───────────────────────────────────────────────────

    def _add_tile(self, camera_id: str, config) -> CameraTile:
        serial = config.identity.serial_number if config is not None else ""
        name = config.name if config is not None else camera_id
        tile = CameraTile(camera_id, name=name, serial=serial)
        self._tiles[camera_id] = tile
        if camera_id not in self._tile_order:
            self._tile_order.append(camera_id)
        return tile

    def _disconnect_all_tiles(self) -> None:
        if self._runtime_service is not None:
            for camera_id, tile in self._tiles.items():
                observer = self._runtime_service.observer_service(camera_id)
                if observer is not None:
                    self._safe_disconnect(observer.result_ready, tile.on_result)
                    self._safe_disconnect(observer.error_occurred, tile.on_error)
        elif self._legacy_observer is not None:
            for tile in self._tiles.values():
                self._safe_disconnect(self._legacy_observer.result_ready, tile.on_result)
                self._safe_disconnect(self._legacy_observer.error_occurred, tile.on_error)

    @staticmethod
    def _safe_disconnect(signal, slot) -> None:
        try:
            signal.disconnect(slot)
        except Exception:
            pass

    def _clear_tiles(self) -> None:
        for camera_id in list(self._tiles.keys()):
            tile = self._tiles.pop(camera_id)
            tile.deleteLater()
        self._tile_order.clear()
        self._last_poll.clear()
        self._relayout_grid()

    def _relayout_grid(self) -> None:
        # Remove every widget from the grid (keep the empty label reference).
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            if item.widget() is not None and item.widget() is not self._empty_label:
                pass
        if not self._tiles:
            self._empty_label.setVisible(True)
            self._grid_layout.addWidget(self._empty_label, 0, 0)
            return
        self._empty_label.setVisible(False)
        columns = _columns_for_camera_count(len(self._tiles))
        for index, camera_id in enumerate(self._tile_order):
            tile = self._tiles.get(camera_id)
            if tile is None:
                continue
            row, col = divmod(index, columns)
            self._grid_layout.addWidget(tile, row, col)

    def _show_empty_state(self) -> None:
        self._clear_tiles()
        self._relayout_grid()

    # ─── Stats polling ─────────────────────────────────────────────────────

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
            for camera_id, tile in self._tiles.items():
                if tile.state is CameraTileState.ERROR:
                    continue
                observer = self._runtime_service.observer_service(camera_id)
                obs_stats = observer.stats() if observer is not None else None
                cam_stats = self._runtime_service.camera_stats(camera_id)
                self._apply_stats(tile, camera_id, obs_stats, cam_stats)
        elif self._legacy_observer is not None:
            for tile in self._tiles.values():
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

    # ─── Helpers ──────────────────────────────────────────────────────────

    def _enabled_cameras(self) -> list:
        result = []
        for config in self._config_service.get_all_camera_configs():
            if getattr(config, "enabled", True) and getattr(config, "thermal_enabled", True):
                result.append(config)
        return result

    def _first_configured_camera(self):
        enabled = self._enabled_cameras()
        return enabled[0] if enabled else None

    def _update_summary(self, failed: list[str] | None = None) -> None:
        configured = len(self._config_service.get_all_camera_configs())
        running = sum(
            1 for t in self._tiles.values() if t.state is not CameraTileState.ERROR
        )
        if failed is None:
            failed_count = sum(
                1 for t in self._tiles.values() if t.state is CameraTileState.ERROR
            )
        else:
            failed_count = len(failed)
        self._set_summary(
            f"Cameras: {configured}  Running: {running}  Failed: {failed_count}"
        )

    def _set_summary(self, text: str) -> None:
        self._summary_label.setText(text)

    def closeEvent(self, event) -> None:
        self.on_mode_deactivated()
        super().closeEvent(event)


__all__ = [
    "CameraTile",
    "CameraTileState",
    "LiveThermalWidget",
    "ObserverModeWidget",
]
