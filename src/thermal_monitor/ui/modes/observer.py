"""
ui.modes.observer -- Observer mode widget (live monitoring).

Displays the live thermal stream for the monitored camera.  Consumes
ProcessingResult objects produced by the ProcessingConsumer (bridged to the
GUI thread by ObserverService) and shows:

- live thermal image (temperature image when available, raw thermal otherwise)
- frame information (camera id, sequence, timestamp)
- overall temperature statistics
- active alarm state
- processing status

The widget only talks to the ObserverService; it never touches the camera
driver, HALCON, the acquisition loop, the shared-memory ring, or recording.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QImage, QPainter, QColor, QFont
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
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

_UNIT_SYMBOLS = {
    TemperatureUnit.CELSIUS: "°C",
    TemperatureUnit.FAHRENHEIT: "°F",
    TemperatureUnit.KELVIN: "K",
}


class ObserverModeWidget(QWidget):
    """Live camera monitoring for Observer mode.

    Owns the lifecycle of the supplied ObserverService: entering the mode
    starts monitoring the first configured camera, leaving the mode stops it.
    """

    def __init__(
        self,
        mode_service: ModeService,
        config_service: ConfigurationService,
        observer_service: ObserverService | None = None,
    ) -> None:
        super().__init__()
        self._mode_service = mode_service
        self._config_service = config_service
        self._observer_service = observer_service

        self._latest_result: ProcessingResult | None = None
        self._frames_received = 0

        self._setup_ui()

        if observer_service is not None:
            observer_service.result_ready.connect(self._on_result)
            observer_service.error_occurred.connect(self._on_error)

    # ─── UI construction ───────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # Header
        header = QHBoxLayout()
        title = QLabel("OBSERVER MODE")
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #2196F3;")
        header.addWidget(title)
        header.addStretch()
        self._status_label = QLabel("Idle")
        self._status_label.setStyleSheet("font-weight: bold;")
        header.addWidget(self._status_label)
        layout.addLayout(header)

        # Main splitter: image | info panels
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)

        self._image_widget = LiveThermalWidget()
        splitter.addWidget(self._image_widget)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        frame_group = QGroupBox("Frame Information")
        frame_form = QFormLayout(frame_group)
        self._camera_label = QLabel("—")
        self._sequence_label = QLabel("—")
        self._timestamp_label = QLabel("—")
        frame_form.addRow("Camera:", self._camera_label)
        frame_form.addRow("Sequence:", self._sequence_label)
        frame_form.addRow("Timestamp:", self._timestamp_label)
        right_layout.addWidget(frame_group)

        temp_group = QGroupBox("Temperature")
        temp_form = QFormLayout(temp_group)
        self._temp_label = QLabel("—")
        self._temp_min = QLabel("—")
        self._temp_max = QLabel("—")
        self._temp_mean = QLabel("—")
        temp_form.addRow("Unit:", self._temp_label)
        temp_form.addRow("Min:", self._temp_min)
        temp_form.addRow("Max:", self._temp_max)
        temp_form.addRow("Mean:", self._temp_mean)
        right_layout.addWidget(temp_group)

        alarm_group = QGroupBox("Alarms")
        alarm_form = QFormLayout(alarm_group)
        self._alarm_count_label = QLabel("0")
        self._alarm_label = QLabel("No active alarms")
        self._alarm_label.setWordWrap(True)
        alarm_form.addRow("Active:", self._alarm_count_label)
        alarm_form.addRow("", self._alarm_label)
        right_layout.addWidget(alarm_group)

        proc_group = QGroupBox("Processing")
        proc_form = QFormLayout(proc_group)
        self._frames_label = QLabel("0")
        self._proc_time_label = QLabel("—")
        self._avg_time_label = QLabel("—")
        proc_form.addRow("Frames:", self._frames_label)
        proc_form.addRow("Last:", self._proc_time_label)
        proc_form.addRow("Avg:", self._avg_time_label)
        right_layout.addWidget(proc_group)

        right_layout.addStretch()
        splitter.addWidget(right)
        splitter.setSizes([900, 360])

    # ─── Lifecycle ─────────────────────────────────────────────────────────

    def on_mode_activated(self) -> None:
        """Start live monitoring when Observer mode becomes active."""
        if self._observer_service is None:
            self._set_status("Observer service not available")
            return
        if self._observer_service.is_running:
            return

        camera = self._first_configured_camera()
        if camera is None:
            self._set_status("No configured cameras — configure one first")
            return

        camera_id = camera.identity.camera_id
        analysis = self._config_service.get_analysis_config(camera_id)
        if analysis is None:
            analysis = AnalysisConfig(camera_id=camera_id)

        try:
            self._observer_service.start(camera_id, analysis_config=analysis)
            self._set_status(f"Observing {camera_id}…")
        except Exception as exc:
            self._set_status(f"Failed to start: {exc}")

    def on_mode_deactivated(self) -> None:
        """Stop live monitoring when leaving Observer mode."""
        if self._observer_service is not None:
            self._observer_service.stop()
        self._set_status("Stopped")

    def _first_configured_camera(self):
        for config in self._config_service.get_all_camera_configs():
            if getattr(config, "enabled", True) and getattr(config, "thermal_enabled", True):
                return config
        return None

    # ─── Result handling ───────────────────────────────────────────────────

    @pyqtSlot(object)
    def _on_result(self, result: ProcessingResult) -> None:
        """Receive a ProcessingResult on the GUI thread (queued connection)."""
        self._latest_result = result
        self._frames_received += 1

        frame = result.frame
        self._image_widget.set_frame(result.temperature_image, frame)
        self._camera_label.setText(frame.descriptor.camera_id)
        self._sequence_label.setText(str(frame.descriptor.sequence))
        self._timestamp_label.setText(f"{frame.descriptor.timestamp:.3f}")
        self._update_temperature(result.analysis_result)
        self._update_alarms(result.alarm_result, frame.descriptor.camera_id)
        self._update_processing(result)
        self._set_status(f"Live — {frame.descriptor.camera_id} seq {frame.descriptor.sequence}")

    @pyqtSlot(str)
    def _on_error(self, message: str) -> None:
        self._set_status(f"Error: {message}")

    def _update_temperature(self, analysis_result) -> None:
        if analysis_result is None:
            self._temp_label.setText("—")
            self._temp_min.setText("—")
            self._temp_max.setText("—")
            self._temp_mean.setText("—")
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
            self._temp_mean.setText("—")
            return

        def fmt(value):
            return "—" if value is None else f"{value:.1f}"

        self._temp_min.setText(fmt(analysis_result.overall_min))
        self._temp_max.setText(fmt(analysis_result.overall_max))
        self._temp_mean.setText(fmt(analysis_result.overall_mean))

    def _update_alarms(self, alarm_result, camera_id: str) -> None:
        if alarm_result is None:
            self._alarm_count_label.setText("0")
            self._alarm_label.setText("No alarm evaluation")
            self._alarm_label.setStyleSheet("color: #666;")
            return

        active = tuple(alarm_result.active_alarms or ())
        self._alarm_count_label.setText(str(len(active)))
        if not active:
            self._alarm_label.setText("No active alarms")
            self._alarm_label.setStyleSheet("color: #666;")
            return

        rules = {}
        analysis = self._config_service.get_analysis_config(camera_id)
        if analysis is not None:
            rules = analysis.alarm_rules

        lines = []
        for rule_id in active:
            rule = rules.get(rule_id)
            if rule is not None:
                lines.append(f"{rule_id} → {rule.roi_id} ({rule.severity.value})")
            else:
                lines.append(rule_id)
        self._alarm_label.setText("\n".join(lines))
        self._alarm_label.setStyleSheet("color: #D32F2F; font-weight: bold;")

    def _update_processing(self, result: ProcessingResult) -> None:
        self._frames_label.setText(str(self._frames_received))
        self._proc_time_label.setText(f"{result.processing_time_ms:.1f} ms")
        stats = self._observer_service.stats() if self._observer_service else None
        if stats is not None and stats.frames_processed > 0:
            self._avg_time_label.setText(f"{stats.average_processing_time_ms:.1f} ms")
        else:
            self._avg_time_label.setText("—")

    def _set_status(self, text: str) -> None:
        self._status_label.setText(text)

    def closeEvent(self, event) -> None:
        if self._observer_service is not None:
            self._observer_service.stop()
        super().closeEvent(event)


class LiveThermalWidget(QWidget):
    """Displays the live thermal image (temperature image or raw thermal).

    The source temperature array is copied into a fresh uint8 display buffer
    before the QImage is constructed, so the widget never retains a reference
    to the shared processing result array.  A later mutation of the source
    cannot affect the displayed pixels.
    """

    def __init__(self) -> None:
        super().__init__()
        self._temperature_image: np.ndarray | None = None
        self._raw_thermal: np.ndarray | None = None
        self._display_image: QImage | None = None
        self._display_array: np.ndarray | None = None
        self.setMinimumSize(320, 240)

    @property
    def display_array(self) -> np.ndarray | None:
        """The copied uint8 display buffer (test/inspection hook)."""
        return self._display_array

    def set_frame(self, temperature_image: np.ndarray | None, frame) -> None:
        """Update the displayed image from a processed frame."""
        self._temperature_image = temperature_image
        self._raw_thermal = frame.payload.thermal if frame is not None else None
        self._rebuild_display()
        self.update()

    def clear(self) -> None:
        self._temperature_image = None
        self._raw_thermal = None
        self._display_image = None
        self._display_array = None
        self.update()

    def _rebuild_display(self) -> None:
        src = self._temperature_image
        if src is None:
            src = self._raw_thermal
        if src is None:
            self._display_image = None
            self._display_array = None
            return

        src = np.asarray(src)
        if src.ndim != 2:
            self._display_image = None
            self._display_array = None
            return

        finite = np.isfinite(src)
        if not np.any(finite):
            display = np.zeros(src.shape, dtype=np.uint8)
        else:
            lo = float(src[finite].min())
            hi = float(src[finite].max())
            if hi <= lo:
                hi = lo + 1.0
            normalized = np.clip((src - lo) / (hi - lo), 0.0, 1.0)
            normalized[~finite] = 0.0
            display = (normalized * 255.0).astype(np.uint8)

        self._display_array = display  # fresh array, no view of the source
        h, w = display.shape
        self._display_image = QImage(
            display.data, w, h, display.strides[0], QImage.Format.Format_Grayscale8
        ).copy()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(20, 20, 20))

        if self._display_image is not None:
            scaled = self._display_image.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawImage(x, y, scaled)
        else:
            painter.setPen(QColor(150, 150, 150))
            painter.setFont(QFont("Segoe UI", 12))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No live data")