"""
ui.modes.offline -- Offline mode widget.

Provides the UI for offline playback:
- Recording selector
- Camera selector
- Stream selector (IR/VL)
- Frame navigation (first, prev, play, pause, next, last, seek)
- Timestamp display
- Sequence display
- Playback controls with speeds (0.25x, 0.5x, 1x, 2x, 4x)
- ROI overlay
- Analysis results
- Alarm state
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSlot, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QGroupBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QSlider,
    QSpinBox,
    QDoubleSpinBox,
    QCheckBox,
    QTreeWidget,
    QTreeWidgetItem,
    QTextEdit,
    QFileDialog,
    QMessageBox,
    QProgressBar,
    QScrollArea,
)
from PyQt6.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QFont

import numpy as np

from thermal_monitor.core.modes import ApplicationMode
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.offline import OfflineService, OfflineSession
from thermal_monitor.offline import StreamFilter, OfflineFrameSourceConfig
from thermal_monitor.core.models import AnalysisConfig
from thermal_monitor.processing import (
    SimpleProcessingPipeline,
    AlarmEvaluator,
    ProcessingWorker,
    ProcessingResult,
    create_processing_worker,
)


class OfflineModeWidget(QWidget):
    """Main widget for Offline mode."""

    # Signal to request frame processing in worker thread
    _process_frame_requested = pyqtSignal(object)

    def __init__(
        self,
        offline_service: OfflineService,
        config_service: ConfigurationService,
        mode_service: ModeService,
        database: Optional[object] = None,
    ) -> None:
        super().__init__()

        self._offline_service = offline_service
        self._config_service = config_service
        self._mode_service = mode_service
        self._database = database

        self._current_session: OfflineSession | None = None
        self._playback_timer = QTimer()
        self._playback_timer.timeout.connect(self._on_playback_tick)
        self._playback_speed = 1.0
        self._target_fps = 9.0  # Default, updated from recording

        # Worker thread for processing
        self._worker: ProcessingWorker | None = None
        self._worker_thread: QThread | None = None
        self._worker_busy = False

        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Top: Recording controls
        recording_group = QGroupBox("Recording")
        recording_layout = QVBoxLayout(recording_group)

        # Recording selector
        selector_layout = QHBoxLayout()
        self._recording_combo = QComboBox()
        self._recording_combo.setMinimumWidth(300)
        self._recording_combo.currentIndexChanged.connect(self._on_recording_selected)
        self._open_btn = QPushButton("Open Recording...")
        self._open_btn.clicked.connect(self.open_recording_dialog)
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.close_recording)
        self._close_btn.setEnabled(False)
        selector_layout.addWidget(QLabel("Recording:"))
        selector_layout.addWidget(self._recording_combo, 1)
        selector_layout.addWidget(self._open_btn)
        selector_layout.addWidget(self._close_btn)
        recording_layout.addLayout(selector_layout)

        # Stream and camera filters
        filter_layout = QHBoxLayout()
        self._camera_combo = QComboBox()
        self._camera_combo.addItem("All Cameras", None)
        self._camera_combo.currentIndexChanged.connect(self._on_camera_filter_changed)
        self._stream_combo = QComboBox()
        self._stream_combo.addItems(["All Streams", "IR Only", "VL Only"])
        self._stream_combo.setCurrentIndex(0)
        self._stream_combo.currentIndexChanged.connect(self._on_stream_filter_changed)
        filter_layout.addWidget(QLabel("Camera:"))
        filter_layout.addWidget(self._camera_combo)
        filter_layout.addWidget(QLabel("Stream:"))
        filter_layout.addWidget(self._stream_combo)
        filter_layout.addStretch()
        recording_layout.addLayout(filter_layout)

        layout.addWidget(recording_group)

        # Main content area: Image view | Analysis/Info panel
        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(main_splitter, 1)

        # Left: Image display
        self._image_widget = OfflineImageWidget()
        main_splitter.addWidget(self._image_widget)

        # Right: Analysis and info panels
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Frame info
        info_group = QGroupBox("Frame Information")
        info_layout = QFormLayout(info_group)
        self._frame_index_label = QLabel("0 / 0")
        self._sequence_label = QLabel("—")
        self._timestamp_label = QLabel("—")
        self._camera_label = QLabel("—")
        self._stream_label = QLabel("—")
        self._sync_label = QLabel("—")
        info_layout.addRow("Frame:", self._frame_index_label)
        info_layout.addRow("Sequence:", self._sequence_label)
        info_layout.addRow("Timestamp:", self._timestamp_label)
        info_layout.addRow("Camera:", self._camera_label)
        info_layout.addRow("Stream:", self._stream_label)
        info_layout.addRow("Sync:", self._sync_label)
        right_layout.addWidget(info_group)

        # Playback controls
        playback_group = QGroupBox("Playback Controls")
        playback_layout = QVBoxLayout(playback_group)

        # Speed control
        speed_layout = QHBoxLayout()
        self._speed_combo = QComboBox()
        self._speed_combo.addItems(["0.25x", "0.5x", "1x", "2x", "4x"])
        self._speed_combo.setCurrentIndex(2)  # 1x
        self._speed_combo.currentTextChanged.connect(self._on_speed_changed)
        speed_layout.addWidget(QLabel("Speed:"))
        speed_layout.addWidget(self._speed_combo)
        speed_layout.addStretch()
        playback_layout.addLayout(speed_layout)

        # Navigation buttons
        nav_layout = QHBoxLayout()
        self._first_btn = QPushButton("⏮ First")
        self._first_btn.clicked.connect(self._go_first)
        self._prev_btn = QPushButton("◀ Prev")
        self._prev_btn.clicked.connect(self._go_prev)
        self._play_btn = QPushButton("▶ Play")
        self._play_btn.clicked.connect(self._toggle_play)
        self._pause_btn = QPushButton("⏸ Pause")
        self._pause_btn.clicked.connect(self._toggle_pause)
        self._pause_btn.setEnabled(False)
        self._next_btn = QPushButton("Next ▶")
        self._next_btn.clicked.connect(self._go_next)
        self._last_btn = QPushButton("Last ⏭")
        self._last_btn.clicked.connect(self._go_last)
        nav_layout.addWidget(self._first_btn)
        nav_layout.addWidget(self._prev_btn)
        nav_layout.addWidget(self._play_btn)
        nav_layout.addWidget(self._pause_btn)
        nav_layout.addWidget(self._next_btn)
        nav_layout.addWidget(self._last_btn)
        playback_layout.addLayout(nav_layout)

        # Seek slider
        seek_layout = QHBoxLayout()
        self._seek_slider = QSlider(Qt.Orientation.Horizontal)
        self._seek_slider.setRange(0, 0)
        self._seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self._seek_slider.sliderReleased.connect(self._on_seek_released)
        self._seek_slider.valueChanged.connect(self._on_seek_value_changed)
        self._seek_label = QLabel("0.0%")
        seek_layout.addWidget(self._seek_slider, 1)
        seek_layout.addWidget(self._seek_label)
        playback_layout.addLayout(seek_layout)

        right_layout.addWidget(playback_group)

        # Analysis results
        analysis_group = QGroupBox("Analysis Results")
        analysis_layout = QVBoxLayout(analysis_group)

        self._analysis_tree = QTreeWidget()
        self._analysis_tree.setHeaderLabels(["ROI", "Mean", "Std Dev", "Min", "Max", "Range"])
        self._analysis_tree.setColumnWidth(0, 150)
        self._analysis_tree.setColumnWidth(1, 80)
        self._analysis_tree.setColumnWidth(2, 80)
        self._analysis_tree.setColumnWidth(3, 80)
        self._analysis_tree.setColumnWidth(4, 80)
        self._analysis_tree.setColumnWidth(5, 80)
        analysis_layout.addWidget(self._analysis_tree)

        # Overall stats
        self._overall_label = QLabel("Overall: —")
        analysis_layout.addWidget(self._overall_label)

        right_layout.addWidget(analysis_group, 1)

        # Alarm panel
        alarm_group = QGroupBox("Alarms")
        alarm_layout = QVBoxLayout(alarm_group)

        self._alarm_tree = QTreeWidget()
        self._alarm_tree.setHeaderLabels(["ROI", "Severity", "Condition", "Threshold", "Measured", "Time"])
        self._alarm_tree.setColumnWidth(0, 120)
        self._alarm_tree.setColumnWidth(1, 80)
        self._alarm_tree.setColumnWidth(2, 100)
        self._alarm_tree.setColumnWidth(3, 100)
        self._alarm_tree.setColumnWidth(4, 100)
        self._alarm_tree.setColumnWidth(5, 150)
        alarm_layout.addWidget(self._alarm_tree)

        # Alarm legend
        alarm_legend = QLabel(
            "● Recorded Alarm  ○ Current Offline Analysis Alarm\n"
            "Recorded alarms are from the original recording. "
            "Current alarms are re-evaluated during playback."
        )
        alarm_legend.setStyleSheet("color: gray; font-size: 10px;")
        alarm_layout.addWidget(alarm_legend)

        right_layout.addWidget(alarm_group, 1)

        main_splitter.addWidget(right_widget)
        main_splitter.setSizes([800, 400])

        # Bottom: Processing status
        self._status_bar = QWidget()
        status_layout = QHBoxLayout(self._status_bar)
        status_layout.setContentsMargins(4, 4, 4, 4)
        self._processing_time_label = QLabel("Processing: — ms")
        self._fps_label = QLabel("FPS: —")
        status_layout.addWidget(self._processing_time_label)
        status_layout.addWidget(self._fps_label)
        status_layout.addStretch()
        layout.addWidget(self._status_bar)

        # Disable playback controls initially
        self._set_playback_enabled(False)

    def _connect_signals(self) -> None:
        # Connect worker signals
        self._process_frame_requested.connect(self._process_frame_in_worker)

    def _set_playback_enabled(self, enabled: bool) -> None:
        """Enable/disable playback controls."""
        self._first_btn.setEnabled(enabled)
        self._prev_btn.setEnabled(enabled)
        self._play_btn.setEnabled(enabled)
        self._pause_btn.setEnabled(enabled and self._current_session and self._current_session.is_playing)
        self._next_btn.setEnabled(enabled)
        self._last_btn.setEnabled(enabled)
        self._seek_slider.setEnabled(enabled)
        self._speed_combo.setEnabled(enabled)

    def open_recording_dialog(self) -> None:
        """Open file dialog to select a recording directory."""
        dir_path = QFileDialog.getExistingDirectory(
            self,
            "Open Recording Directory",
            str(Path.home()),
            QFileDialog.Option.ShowDirsOnly,
        )
        if dir_path:
            self._load_recording(dir_path)

    def _load_recording(self, recording_dir: str) -> None:
        """Load a recording directory."""
        # Close existing session and worker
        self._cleanup_worker()

        if self._current_session:
            self._offline_service.remove_session(self._current_session.session_id)
            self._current_session = None

        # Get first camera from recording to determine analysis config
        try:
            from thermal_monitor.offline import open_offline_source
            temp_source = open_offline_source(recording_dir)
            camera_ids = temp_source.camera_ids
            temp_source.close()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to open recording: {e}")
            return

        if not camera_ids:
            QMessageBox.warning(self, "Empty Recording", "No cameras found in recording.")
            return

        # Use first camera's analysis config, or create default
        camera_id = camera_ids[0]
        analysis_config = self._config_service.get_analysis_config(camera_id)
        if not analysis_config:
            from thermal_monitor.core.models import TemperatureUnit
            analysis_config = AnalysisConfig(
                camera_id=camera_id,
                unit=TemperatureUnit.CELSIUS,
            )

        # Create session
        session_id = f"session_{int(time.time())}"
        self._current_session = self._offline_service.create_session(
            session_id=session_id,
            camera_id=camera_id if len(camera_ids) == 1 else None,
            recording_dir=recording_dir,
            analysis_config=analysis_config,
        )
        self._offline_service.add_session_callback(session_id, self._on_session_changed)

        # Create processing worker in background thread
        self._worker, self._worker_thread = create_processing_worker(
            analysis_config=analysis_config,
        )
        self._worker.result_ready.connect(self._on_processing_result)
        self._worker.error_occurred.connect(self._on_processing_error)
        self._worker_thread.start()

        # Update UI
        self._recording_combo.clear()
        self._recording_combo.addItem(Path(recording_dir).name, recording_dir)
        self._camera_combo.clear()
        self._camera_combo.addItem("All Cameras", None)
        for cam in camera_ids:
            self._camera_combo.addItem(cam, cam)
        self._close_btn.setEnabled(True)
        self._set_playback_enabled(True)

        # Update seek slider range
        self._seek_slider.setRange(0, len(self._current_session.source) - 1)
        self._update_frame_info()

        # Process first frame
        self._request_frame_processing()

    def _cleanup_worker(self) -> None:
        """Stop and clean up the processing worker."""
        if self._worker:
            self._worker.stop()
            self._worker.result_ready.disconnect(self._on_processing_result)
            self._worker.error_occurred.disconnect(self._on_processing_error)
            self._worker = None
        if self._worker_thread:
            if self._worker_thread.isRunning():
                self._worker_thread.quit()
                self._worker_thread.wait(1000)
            self._worker_thread = None
        self._worker_busy = False

    def close_recording(self) -> None:
        """Close the current recording."""
        self._playback_timer.stop()
        self._cleanup_worker()

        if self._current_session:
            self._offline_service.remove_session_callback(
                self._current_session.session_id, self._on_session_changed
            )
            self._offline_service.remove_session(self._current_session.session_id)
            self._current_session = None

        self._recording_combo.clear()
        self._camera_combo.clear()
        self._camera_combo.addItem("All Cameras", None)
        self._close_btn.setEnabled(False)
        self._set_playback_enabled(False)
        self._image_widget.clear()
        self._clear_analysis()
        self._clear_alarms()
        self._frame_index_label.setText("0 / 0")
        self._sequence_label.setText("—")
        self._timestamp_label.setText("—")
        self._camera_label.setText("—")
        self._stream_label.setText("—")
        self._sync_label.setText("—")

    def _on_recording_selected(self, index: int) -> None:
        if index >= 0:
            recording_dir = self._recording_combo.itemData(index)
            if recording_dir and recording_dir != getattr(self._current_session, 'source', None):
                self._load_recording(recording_dir)

    def _on_camera_filter_changed(self, index: int) -> None:
        """Handle camera filter change - recreate session with new filter."""
        if not self._current_session:
            return

        camera_id = self._camera_combo.itemData(index)
        recording_dir = str(self._current_session.source.recording_dir)

        # Recreate session with new camera filter
        analysis_config = self._config_service.get_analysis_config(camera_id) if camera_id else self._current_session.analysis_config

        self._cleanup_worker()
        self._offline_service.remove_session(self._current_session.session_id)

        session_id = f"session_{int(time.time())}"
        self._current_session = self._offline_service.create_session(
            session_id=session_id,
            camera_id=camera_id,
            recording_dir=recording_dir,
            analysis_config=analysis_config,
        )
        self._offline_service.add_session_callback(session_id, self._on_session_changed)

        # Create new worker with updated config
        self._worker, self._worker_thread = create_processing_worker(
            analysis_config=analysis_config,
        )
        self._worker.result_ready.connect(self._on_processing_result)
        self._worker.error_occurred.connect(self._on_processing_error)
        self._worker_thread.start()

        self._seek_slider.setRange(0, len(self._current_session.source) - 1)
        self._update_frame_info()
        self._request_frame_processing()

    def _on_stream_filter_changed(self, index: int) -> None:
        """Handle stream filter change."""
        if not self._current_session:
            return

        stream_filter = StreamFilter.ALL
        if index == 1:
            stream_filter = StreamFilter.IR
        elif index == 2:
            stream_filter = StreamFilter.VL

        # Need to recreate source with new filter - for now just update current source
        # This is a limitation; full recreation would be better
        self._current_session.source._config.stream_filter = stream_filter
        self._current_session.source._build_filtered_entries()
        self._current_session.source._current_index = 0

        self._seek_slider.setRange(0, len(self._current_session.source) - 1)
        self._update_frame_info()
        self._request_frame_processing()

    def _on_speed_changed(self, text: str) -> None:
        """Handle playback speed change."""
        speed_map = {"0.25x": 0.25, "0.5x": 0.5, "1x": 1.0, "2x": 2.0, "4x": 4.0}
        self._playback_speed = speed_map.get(text, 1.0)
        if self._current_session:
            self._offline_service.set_playback_speed(self._current_session.session_id, self._playback_speed)

    def _go_first(self) -> None:
        if self._current_session and self._current_session.source.first():
            self._current_session.current_frame_index = 0
            self._update_frame_info()
            self._request_frame_processing()

    def _go_prev(self) -> None:
        if self._current_session and self._current_session.current_frame_index > 0:
            self._current_session.current_frame_index -= 1
            self._current_session.source.seek_to_index(self._current_session.current_frame_index)
            self._update_frame_info()
            self._request_frame_processing()

    def _go_next(self) -> None:
        if self._current_session:
            frame = self._current_session.source.get_next_frame()
            if frame:
                self._current_session.current_frame_index += 1
                self._update_frame_info()
                self._request_frame_processing()

    def _go_last(self) -> None:
        if self._current_session:
            last_idx = len(self._current_session.source) - 1
            if self._current_session.source.seek_to_index(last_idx):
                self._current_session.current_frame_index = last_idx
                self._update_frame_info()
                self._request_frame_processing()

    def _toggle_play(self) -> None:
        if self._current_session:
            self._current_session.is_playing = True
            self._offline_service.play(self._current_session.session_id)
            self._play_btn.setEnabled(False)
            self._pause_btn.setEnabled(True)
            self._start_playback_timer()

    def _toggle_pause(self) -> None:
        if self._current_session:
            self._current_session.is_playing = False
            self._offline_service.pause(self._current_session.session_id)
            self._play_btn.setEnabled(True)
            self._pause_btn.setEnabled(False)
            self._playback_timer.stop()

    def _start_playback_timer(self) -> None:
        """Start the playback timer based on frame rate and speed."""
        interval_ms = int(1000 / (self._target_fps * self._playback_speed))
        self._playback_timer.start(max(10, interval_ms))

    def _on_playback_tick(self) -> None:
        """Playback timer tick - advance to next frame."""
        if self._current_session and self._current_session.is_playing:
            frame = self._current_session.source.get_next_frame()
            if frame:
                self._current_session.current_frame_index += 1
                self._update_frame_info()
                self._request_frame_processing()
            else:
                # End of recording
                self._toggle_pause()

    def _on_seek_pressed(self) -> None:
        """Slider pressed - pause playback."""
        self._was_playing = self._current_session.is_playing if self._current_session else False
        if self._was_playing:
            self._toggle_pause()

    def _on_seek_released(self) -> None:
        """Slider released - seek to position and resume if was playing."""
        if self._current_session:
            index = self._seek_slider.value()
            if self._current_session.source.seek_to_index(index):
                self._current_session.current_frame_index = index
                self._update_frame_info()
                self._request_frame_processing()

            if self._was_playing:
                self._toggle_play()

    def _on_seek_value_changed(self, value: int) -> None:
        """Slider value changed - update seek label."""
        if self._current_session:
            total = len(self._current_session.source)
            if total > 0:
                percent = (value / total) * 100
                self._seek_label.setText(f"{percent:.1f}%")

    def _request_frame_processing(self) -> None:
        """Request frame processing in worker thread (bounded: drop if busy)."""
        if not self._current_session or not self._worker or self._worker_busy:
            return

        frame = self._current_session.source.current()
        if not frame:
            return

        self._worker_busy = True
        self._process_frame_requested.emit(frame)

    @pyqtSlot(object)
    def _process_frame_in_worker(self, frame) -> None:
        """Slot called in worker thread to process frame."""
        if self._worker and self._worker.is_running:
            self._worker.process_frame(frame)

    @pyqtSlot(object)
    def _on_processing_result(self, result: ProcessingResult) -> None:
        """Handle processing result from worker (runs on UI thread via queued connection)."""
        self._worker_busy = False

        # Update displays
        self._image_widget.set_frame(result.frame)
        self._update_analysis_display(result.analysis_result)
        self._update_alarm_display(result.alarm_result, result.analysis_result)
        self._processing_time_label.setText(f"Processing: {result.processing_time_ms:.1f} ms")

    @pyqtSlot(str)
    def _on_processing_error(self, error: str) -> None:
        """Handle processing error from worker."""
        self._worker_busy = False
        # Could show error in status bar
        self._processing_time_label.setText(f"Error: {error}")

    def _update_frame_info(self) -> None:
        """Update frame information display."""
        if not self._current_session:
            return

        frame = self._current_session.source.current()
        if not frame:
            return

        total = len(self._current_session.source)
        self._frame_index_label.setText(f"{self._current_session.current_frame_index + 1} / {total}")
        self._sequence_label.setText(str(frame.descriptor.sequence))
        self._timestamp_label.setText(f"{frame.descriptor.timestamp:.3f}")
        self._camera_label.setText(frame.descriptor.camera_id)

        stream_type = "IR" if frame.payload.thermal is not None else "VL"
        self._stream_label.setText(stream_type)

        sync_status = frame.descriptor.sync.status.name
        self._sync_label.setText(sync_status)

        # Update seek slider
        self._seek_slider.blockSignals(True)
        self._seek_slider.setValue(self._current_session.current_frame_index)
        self._seek_slider.blockSignals(False)

    def _update_analysis_display(self, result) -> None:
        """Update analysis results tree."""
        self._analysis_tree.clear()

        for roi_id, stat in result.roi_results.items():
            item = QTreeWidgetItem([
                f"{stat.roi_name} ({roi_id})",
                f"{stat.mean_temp:.2f}",
                f"{stat.deviation:.2f}",
                f"{stat.min_temp:.2f}",
                f"{stat.max_temp:.2f}",
                f"{stat.range_temp:.2f}",
            ])
            self._analysis_tree.addTopLevelItem(item)

        # Overall stats
        if result.overall_min is not None:
            self._overall_label.setText(
                f"Overall: Min={result.overall_min:.2f}, "
                f"Max={result.overall_max:.2f}, "
                f"Mean={result.overall_mean:.2f} °C"
            )
        else:
            self._overall_label.setText("Overall: No thermal data")

    def _update_alarm_display(self, alarm_result, analysis_result) -> None:
        """Update alarm display with both recorded and current analysis alarms."""
        self._alarm_tree.clear()

        # Show recorded alarms from frame metadata (if any)
        # For now, show current analysis alarms
        if alarm_result and alarm_result.active_alarms:
            for alarm in alarm_result.active_alarms:
                roi_name = "Unknown"
                for roi_id, stat in analysis_result.roi_results.items():
                    if roi_id == alarm.roi_id:
                        roi_name = stat.roi_name
                        break

                item = QTreeWidgetItem([
                    roi_name,
                    alarm.severity.value.upper(),
                    alarm.rule_id,
                    f"{alarm.threshold_value:.1f}",
                    f"{alarm.measured_value:.1f}",
                    time.strftime("%H:%M:%S", time.localtime(alarm.timestamp)),
                ])
                # Mark as current analysis alarm (different color)
                item.setForeground(1, QColor("red"))
                self._alarm_tree.addTopLevelItem(item)

    def _clear_analysis(self) -> None:
        self._analysis_tree.clear()
        self._overall_label.setText("Overall: —")

    def _clear_alarms(self) -> None:
        self._alarm_tree.clear()

    def _on_session_changed(self, session: OfflineSession, event: str) -> None:
        """Handle session changes from OfflineService."""
        if event == "created":
            pass  # Already handled in _load_recording
        elif event == "changed":
            self._update_frame_info()

    def on_mode_activated(self) -> None:
        """Called when offline mode becomes active."""
        pass

    def closeEvent(self, event) -> None:
        self._playback_timer.stop()
        self._cleanup_worker()
        if self._current_session:
            self._offline_service.remove_session_callback(
                self._current_session.session_id, self._on_session_changed
            )
            self._offline_service.remove_session(self._current_session.session_id)
        super().closeEvent(event)


class OfflineImageWidget(QWidget):
    """Widget for displaying thermal/visible images with ROI overlays."""

    def __init__(self) -> None:
        super().__init__()
        self._frame = None
        self._display_image: QImage | None = None
        self._rois: list[dict] = []
        self.setMinimumSize(640, 480)

    def set_frame(self, frame) -> None:
        """Set a new frame and update display."""
        self._frame = frame
        self._update_display_image()
        self.update()

    def _update_display_image(self) -> None:
        """Convert frame thermal data to display image."""
        if not self._frame or self._frame.payload.thermal is None:
            self._display_image = None
            return

        thermal = self._frame.payload.thermal
        # Normalize to 0-255 for display
        finite = np.isfinite(thermal)
        if not np.any(finite):
            self._display_image = QImage(thermal.shape[1], thermal.shape[0], QImage.Format.Format_Grayscale8)
            self._display_image.fill(0)
            return

        values = thermal[finite]
        min_val = float(values.min())
        max_val = float(values.max())
        if max_val <= min_val:
            max_val = min_val + 1.0

        normalized = np.clip((thermal - min_val) / (max_val - min_val), 0.0, 1.0)
        normalized[~finite] = 0.0
        display_arr = (normalized * 255.0).astype(np.uint8)

        h, w = display_arr.shape
        self._display_image = QImage(
            display_arr.data, w, h, display_arr.strides[0], QImage.Format.Format_Grayscale8
        ).copy()

    def set_rois(self, rois: list[dict]) -> None:
        """Set ROI overlays to display."""
        self._rois = rois
        self.update()

    def clear(self) -> None:
        self._frame = None
        self._display_image = None
        self._rois = []
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(30, 30, 30))

        if self._display_image:
            # Scale image to fit widget while maintaining aspect ratio
            scaled = self._display_image.scaled(
                self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawImage(x, y, scaled)

            # Draw ROI overlays
            if self._rois and self._frame and self._frame.payload.thermal is not None:
                self._draw_roi_overlays(painter, x, y, scaled.width(), scaled.height())
        else:
            painter.setPen(QColor(128, 128, 128))
            painter.setFont(QFont("Segoe UI", 14))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No frame loaded")

    def _draw_roi_overlays(self, painter: QPainter, offset_x: int, offset_y: int, display_w: int, display_h: int) -> None:
        """Draw ROI overlays on the image."""
        if not self._frame or self._frame.payload.thermal is None:
            return

        thermal = self._frame.payload.thermal
        img_h, img_w = thermal.shape

        scale_x = display_w / img_w
        scale_y = display_h / img_h

        for roi in self._rois:
            geometry = roi.get("geometry")
            if not geometry:
                continue

            shape = geometry.shape
            params = geometry.parameters

            painter.setPen(QPen(QColor("springgreen"), 2))
            painter.setFont(QFont("Segoe UI", 10))

            if shape.name == "RECTANGLE1":
                y1 = params["y1"] * scale_y + offset_y
                x1 = params["x1"] * scale_x + offset_x
                y2 = params["y2"] * scale_y + offset_y
                x2 = params["x2"] * scale_x + offset_x
                painter.drawRect(int(x1), int(y1), int(x2 - x1), int(y2 - y1))
                painter.drawText(int(x1), int(y1) - 5, roi.get("name", "ROI"))

            elif shape.name == "CIRCLE":
                cy = params["center_y"] * scale_y + offset_y
                cx = params["center_x"] * scale_x + offset_x
                r = params["radius"] * min(scale_x, scale_y)
                painter.drawEllipse(int(cx - r), int(cy - r), int(2 * r), int(2 * r))
                painter.drawText(int(cx - r), int(cy - r) - 5, roi.get("name", "ROI"))

            # Add other shapes as needed