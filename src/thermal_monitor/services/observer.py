"""
services.observer -- Observer mode live monitoring service.

Owns the consumer-side live monitoring path for Observer mode:

    SharedMemoryRing (written by AcquisitionWorker)
        -> ProcessingConsumer (processing thread)
        -> result_ready signal (GUI thread, queued)

The service attaches to the producer-owned ring buffer that the acquisition
worker writes to, runs the existing ProcessingConsumer unchanged, and bridges
every ProcessingResult to the GUI thread with a Qt queued signal.  It never
touches the TV46L driver, HALCON, the acquisition loop, or the recording writer.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

try:
    from PyQt6.QtCore import QObject, pyqtSignal
    _HAS_PYQT6 = True
except ImportError:
    QObject = object
    pyqtSignal = lambda *args, **kwargs: None
    _HAS_PYQT6 = False

from thermal_monitor.core.models import AnalysisConfig
from thermal_monitor.processing import (
    ProcessingConsumer,
    ProcessingResult,
    create_processing_consumer,
)
from thermal_monitor.processing.alarms import AlarmEvaluator
from thermal_monitor.processing.pipeline import CalibrationProvider, TemperatureConverter

logger = logging.getLogger(__name__)


class ObserverService(QObject if _HAS_PYQT6 else object):
    """Bridges ProcessingConsumer results to the GUI thread for Observer mode.

    Lives in the GUI thread.  ``start`` attaches to the camera's shared-memory
    ring buffer (producer-owned) and runs a ProcessingConsumer in its own
    thread.  Each ProcessingResult is forwarded through :attr:`result_ready`;
    the consumer thread emits it and the GUI thread receives it via a queued
    connection, so UI code never runs in the processing thread.

    No TV46L driver, HALCON, acquisition loop, or recording path is touched:
    the service is purely a consumer-side bridge.
    """

    result_ready = pyqtSignal(object) if _HAS_PYQT6 else None  # ProcessingResult
    error_occurred = pyqtSignal(str) if _HAS_PYQT6 else None

    def __init__(self) -> None:
        if _HAS_PYQT6:
            super().__init__()
        self._camera_id: str | None = None
        self._ring = None
        self._consumer: ProcessingConsumer | None = None

    @property
    def camera_id(self) -> str | None:
        return self._camera_id

    @property
    def is_running(self) -> bool:
        return self._consumer is not None and self._consumer.is_running

    def start(
        self,
        camera_id: str,
        analysis_config: AnalysisConfig | None = None,
        *,
        consumer_name: str | None = None,
        alarm_evaluator: AlarmEvaluator | None = None,
        calibration_provider: CalibrationProvider | None = None,
        temperature_converter: TemperatureConverter | None = None,
        ring_depth: int = 32,
        thermal_width: int = 640,
        thermal_height: int = 480,
        thermal_dtype: np.dtype = np.dtype(np.uint16),
    ) -> None:
        """Start observing one camera.

        Attaches to the shared-memory ring that the producer
        (AcquisitionWorker) already created for ``camera_id`` and runs a
        ProcessingConsumer over it.  Ring geometry must match the producer.

        Raises:
            ValueError: if no analysis_config is provided.
            RuntimeError: if the producer ring cannot be attached.
        """
        if self.is_running:
            logger.warning("ObserverService already running for %s", self._camera_id)
            return
        if analysis_config is None:
            raise ValueError("analysis_config is required to start observer monitoring")

        self.stop(timeout=1.0)

        self._camera_id = camera_id
        try:
            ring, consumer = create_processing_consumer(
                camera_id=camera_id,
                analysis_config=analysis_config,
                consumer_name=consumer_name or f"observer_{camera_id}",
                alarm_evaluator=alarm_evaluator,
                calibration_provider=calibration_provider,
                temperature_converter=temperature_converter,
                result_callback=self._forward_result,
                ring_depth=ring_depth,
                thermal_width=thermal_width,
                thermal_height=thermal_height,
                thermal_dtype=thermal_dtype,
            )
        except Exception as exc:
            self._camera_id = None
            if _HAS_PYQT6 and self.error_occurred:
                self.error_occurred.emit(str(exc))
            raise RuntimeError(
                f"Cannot attach to ring for camera {camera_id}: {exc}"
            ) from exc

        self._ring = ring
        self._consumer = consumer
        consumer.start()
        logger.info("Observer monitoring started for camera %s", camera_id)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the consumer and release the attached ring."""
        consumer, ring, camera_id = self._consumer, self._ring, self._camera_id
        self._consumer = None
        self._ring = None
        self._camera_id = None

        if consumer is not None:
            try:
                consumer.stop(timeout=timeout)
            except Exception:
                logger.exception("Observer consumer stop failed")
            try:
                consumer.close()
            except Exception:
                pass
        if ring is not None:
            try:
                ring.close()
            except Exception:
                pass
        if camera_id:
            logger.info("Observer monitoring stopped for camera %s", camera_id)

    def close(self) -> None:
        """Stop monitoring and release resources."""
        self.stop()

    def stats(self) -> Any:
        """Current ProcessingConsumerStats, or None when not running."""
        if self._consumer is None:
            return None
        return self._consumer.stats()

    def _forward_result(self, result: ProcessingResult) -> None:
        """Called from the consumer thread; forwards to the GUI thread."""
        try:
            if _HAS_PYQT6 and self.result_ready:
                self.result_ready.emit(result)
        except Exception:
            logger.exception("Failed to forward ProcessingResult to GUI")


__all__ = ["ObserverService"]