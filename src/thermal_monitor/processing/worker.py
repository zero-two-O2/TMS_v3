"""
processing.worker -- Background processing worker for offline analysis.

Provides a QThread-based worker that processes frames through the analysis
pipeline without blocking the UI thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

try:
    from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot
    _HAS_PYQT6 = True
except ImportError:
    QObject = object
    QThread = object
    pyqtSignal = lambda *args, **kwargs: None
    pyqtSlot = lambda *args, **kwargs: (lambda f: f)
    _HAS_PYQT6 = False

from thermal_monitor.core.frame import Frame
from thermal_monitor.core.models import AnalysisConfig, AnalysisResult
from thermal_monitor.processing.alarms import AlarmEvaluator, AlarmEvaluationResult
from thermal_monitor.processing.pipeline import (
    ProcessingPipeline,
    SimpleProcessingPipeline,
    TemperatureConverter,
    CalibrationProvider,
)
from thermal_monitor.processing.halcon import HalconROIAdapter


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    """Result of processing a single frame."""

    frame: Frame
    analysis_result: AnalysisResult
    alarm_result: AlarmEvaluationResult | None
    processing_time_ms: float
    temperature_image: np.ndarray | None = None


class ProcessingWorker(QObject if _HAS_PYQT6 else object):
    """
    Worker for offline frame processing in a background thread.

    Processes frames through the full analysis pipeline (calibration,
    ROI statistics, alarm evaluation) and emits results via signals.

    Usage:
        worker = ProcessingWorker(pipeline, alarm_evaluator)
        worker.moveToThread(thread)
        worker.result_ready.connect(on_result)
        thread.start()
        worker.process_frame(frame)
    """

    # Emitted when a frame has been processed
    result_ready = pyqtSignal(object) if _HAS_PYQT6 else None  # ProcessingResult

    # Emitted when an error occurs during processing
    error_occurred = pyqtSignal(str) if _HAS_PYQT6 else None

    # Emitted when the worker has finished processing (after stop() called)
    finished = pyqtSignal() if _HAS_PYQT6 else None

    def __init__(
        self,
        pipeline: ProcessingPipeline,
        alarm_evaluator: AlarmEvaluator | None = None,
    ) -> None:
        if _HAS_PYQT6:
            super().__init__()
        self._pipeline = pipeline
        self._alarm_evaluator = alarm_evaluator
        self._running = False
        self._current_frame: Frame | None = None

    @pyqtSlot() if _HAS_PYQT6 else lambda f: f
    def start(self) -> None:
        """Start the worker."""
        self._running = True

    @pyqtSlot() if _HAS_PYQT6 else lambda f: f
    def stop(self) -> None:
        """Stop the worker."""
        self._running = False
        if _HAS_PYQT6 and self.finished:
            self.finished.emit()

    @pyqtSlot(object) if _HAS_PYQT6 else lambda f: f
    def process_frame(self, frame: Frame) -> None:
        """
        Process a single frame.

        This slot is called from the UI thread (via queued connection)
        and runs in the worker's thread.
        """
        if not self._running or frame is None:
            return

        self._current_frame = frame

        try:
            start_time = time.perf_counter()

            # Run analysis pipeline
            analysis_result = self._pipeline.process_frame(frame)

            # Run alarm evaluation
            alarm_result = None
            if self._alarm_evaluator is not None:
                alarm_result = self._alarm_evaluator.evaluate(analysis_result)

            processing_time = (time.perf_counter() - start_time) * 1000

            # Emit result
            result = ProcessingResult(
                frame=frame,
                analysis_result=analysis_result,
                alarm_result=alarm_result,
                processing_time_ms=processing_time,
            )
            if _HAS_PYQT6 and self.result_ready:
                self.result_ready.emit(result)

        except Exception as e:
            if _HAS_PYQT6 and self.error_occurred:
                self.error_occurred.emit(f"Processing error: {e}")

    @property
    def is_running(self) -> bool:
        return self._running


def create_processing_worker(
    analysis_config: AnalysisConfig,
    calibration_provider: CalibrationProvider | None = None,
    temperature_converter: TemperatureConverter | None = None,
    halcon_adapter: HalconROIAdapter | None = None,
    alarm_evaluator: AlarmEvaluator | None = None,
) -> tuple[ProcessingWorker, QThread]:
    """
    Factory function to create a configured ProcessingWorker and its thread.

    Returns:
        Tuple of (worker, thread). The worker is already moved to the thread.
        Call thread.start() to begin processing.
    """
    if not _HAS_PYQT6:
        raise RuntimeError("PyQt6 is required for create_processing_worker. Install with 'pip install thermal-monitoring-system[ui]'")

    pipeline = SimpleProcessingPipeline(
        config=analysis_config,
        calibration_provider=calibration_provider,
        temperature_converter=temperature_converter,
        halcon_adapter=halcon_adapter,
    )

    if alarm_evaluator is None:
        alarm_evaluator = AlarmEvaluator(config=analysis_config)

    worker = ProcessingWorker(pipeline=pipeline, alarm_evaluator=alarm_evaluator)
    thread = QThread()
    worker.moveToThread(thread)

    # Auto-start worker when thread starts
    thread.started.connect(worker.start)

    # Clean up thread when worker finishes
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)

    return worker, thread