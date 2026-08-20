"""
processing.consumer -- Shared-memory ring consumer that runs the processing pipeline.

Consumes frames from a SharedMemoryRingBuffer and runs them through the
existing processing pipeline (CPU temperature conversion, ROI statistics,
alarm evaluation). Runs independently of the acquisition producer in its own
thread and never blocks the producer. Uses the existing TemperatureConverter,
CalibrationProvider, ProcessingPipeline and AlarmEvaluator contracts unchanged.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from thermal_monitor.core.frame import Frame
from thermal_monitor.core.models import AnalysisConfig
from thermal_monitor.core.shm import Consumer, SharedMemoryRingBuffer, RingConfig, PayloadSpec
from thermal_monitor.processing.alarms import AlarmEvaluator
from thermal_monitor.processing.pipeline import (
    CalibrationProvider,
    ProcessingPipeline,
    ProcessingStats,
    SimpleProcessingPipeline,
    TemperatureConverter,
)
from thermal_monitor.processing.temperature import CPUTemperatureConverter
from thermal_monitor.processing.worker import ProcessingResult

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProcessingConsumerStats:
    """Statistics for the processing consumer."""

    frames_consumed: int = 0
    frames_processed: int = 0
    frames_failed: int = 0
    ring_overwritten: int = 0
    ring_gaps: int = 0
    ring_stale: int = 0
    ring_invalid: int = 0
    errors: int = 0
    last_sequence: int = -1
    last_camera_id: str | None = None
    last_processed_at: float | None = None
    total_processing_time_ms: float = 0.0
    average_processing_time_ms: float = 0.0
    last_error: str | None = None

    def copy(self) -> "ProcessingConsumerStats":
        """Return a copy of the current stats."""
        return ProcessingConsumerStats(
            frames_consumed=self.frames_consumed,
            frames_processed=self.frames_processed,
            frames_failed=self.frames_failed,
            ring_overwritten=self.ring_overwritten,
            ring_gaps=self.ring_gaps,
            ring_stale=self.ring_stale,
            ring_invalid=self.ring_invalid,
            errors=self.errors,
            last_sequence=self.last_sequence,
            last_camera_id=self.last_camera_id,
            last_processed_at=self.last_processed_at,
            total_processing_time_ms=self.total_processing_time_ms,
            average_processing_time_ms=self.average_processing_time_ms,
            last_error=self.last_error,
        )


class ProcessingConsumer:
    """Consumes frames from a shared-memory ring and runs the processing pipeline.

    Each processed frame produces a :class:`ProcessingResult` delivered to the
    configured result callback. Runs in its own thread and does not block the
    acquisition producer. Processing failures on individual frames are recorded
    in the statistics and never terminate the consumer loop.
    """

    def __init__(
        self,
        camera_id: str,
        ring_buffer: SharedMemoryRingBuffer,
        consumer_name: str,
        pipeline: ProcessingPipeline,
        alarm_evaluator: AlarmEvaluator | None = None,
        result_callback: Callable[[ProcessingResult], None] | None = None,
    ) -> None:
        self._camera_id = camera_id
        self._consumer = ring_buffer.consumer(consumer_name)
        self._pipeline = pipeline
        self._alarm_evaluator = alarm_evaluator
        self._result_callback = result_callback

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._stats = ProcessingConsumerStats()
        self._stats_lock = threading.Lock()

    def start(self, result_callback: Callable[[ProcessingResult], None] | None = None) -> None:
        """Start the processing consumer thread."""
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("ProcessingConsumer already running")

        if result_callback is not None:
            self._result_callback = result_callback

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"ProcessingConsumer-{self._camera_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info("ProcessingConsumer started for camera %s", self._camera_id)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the processing consumer thread."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("Camera %s: processing consumer thread did not stop within %.1f s", self._camera_id, timeout)
        self._thread = None
        logger.info("ProcessingConsumer stopped for camera %s", self._camera_id)

    def restart(self, timeout: float = 5.0) -> None:
        """Stop and start the processing consumer with the same consumer name."""
        self.stop(timeout=timeout)
        self.start()

    @property
    def is_running(self) -> bool:
        """Whether the consumer thread is currently running."""
        return self._thread is not None and self._thread.is_alive()

    def stats(self) -> ProcessingConsumerStats:
        """Get current consumer statistics."""
        with self._stats_lock:
            return self._stats.copy()

    @property
    def pipeline_stats(self) -> ProcessingStats:
        """Statistics from the underlying processing pipeline."""
        return self._pipeline.stats

    def wait_for_frames(self, count: int, timeout: float = 5.0) -> bool:
        """Wait until at least `count` frames have been processed."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._stats_lock:
                if self._stats.frames_processed >= count:
                    return True
            time.sleep(0.01)
        return False

    def close(self) -> None:
        """Clean up resources."""
        if self._thread is not None and self._thread.is_alive():
            self.stop(timeout=2.0)
        try:
            self._consumer.close()
        except Exception:
            pass

    def _run(self) -> None:
        """Main consumer loop."""
        logger.debug("ProcessingConsumer %s: entering run loop", self._camera_id)

        expected_sequence = 0
        first_frame = True

        while not self._stop_event.is_set():
            try:
                pinned_view = self._consumer.next_pinned(expected_sequence)

                if pinned_view is not None:
                    frame = pinned_view.view.copy()
                    try:
                        self._process_frame(frame)
                    finally:
                        try:
                            self._consumer.release(pinned_view)
                        except Exception as exc:
                            logger.warning("Camera %s: failed to release pinned view: %s", self._camera_id, exc)

                    expected_sequence = frame.descriptor.sequence + 1
                    first_frame = False

                else:
                    # Frame not available - check consumer stats for drops
                    consumer_stats = self._consumer.stats()
                    with self._stats_lock:
                        if consumer_stats.overwritten > self._stats.ring_overwritten:
                            self._stats.ring_overwritten = consumer_stats.overwritten
                        if consumer_stats.gaps > self._stats.ring_gaps:
                            self._stats.ring_gaps = consumer_stats.gaps
                        if consumer_stats.stale > self._stats.ring_stale:
                            self._stats.ring_stale = consumer_stats.stale
                        if consumer_stats.invalid > self._stats.ring_invalid:
                            self._stats.ring_invalid = consumer_stats.invalid

                    # If we haven't seen any frames yet, try latest_pinned() to catch up
                    if first_frame:
                        latest_pinned = self._consumer.latest_pinned()
                        if latest_pinned is not None:
                            frame = latest_pinned.view.copy()
                            try:
                                self._process_frame(frame)
                            finally:
                                try:
                                    self._consumer.release(latest_pinned)
                                except Exception as exc:
                                    logger.warning("Camera %s: failed to release pinned view: %s", self._camera_id, exc)

                            expected_sequence = frame.descriptor.sequence + 1
                            first_frame = False

                    # Small sleep to avoid busy-waiting when frames not yet available
                    time.sleep(0.001)

            except Exception as exc:
                logger.exception("Camera %s: consumer loop error: %s", self._camera_id, exc)
                with self._stats_lock:
                    self._stats.errors += 1
                    self._stats.last_error = str(exc)
                time.sleep(0.1)  # Back off on error

        logger.debug("ProcessingConsumer %s: run loop exited", self._camera_id)

    def _process_frame(self, frame: Frame) -> None:
        """Run the processing pipeline on one durable frame copy."""
        start_time = time.perf_counter()
        try:
            analysis_result = self._pipeline.process_frame(frame)

            alarm_result = None
            if self._alarm_evaluator is not None:
                alarm_result = self._alarm_evaluator.evaluate(analysis_result)

            processing_time_ms = (time.perf_counter() - start_time) * 1000

            # Expose the CPU-converted temperature image for display/diagnostics.
            # The pipeline is the single conversion point (no duplicate LUT lookup).
            temperature_image = None
            if getattr(self._pipeline, "temperature_converter", None) is not None:
                temperature_image = getattr(self._pipeline, "last_temperature_image", None)

            result = ProcessingResult(
                frame=frame,
                analysis_result=analysis_result,
                alarm_result=alarm_result,
                processing_time_ms=processing_time_ms,
                temperature_image=temperature_image,
            )

            with self._stats_lock:
                self._stats.frames_processed += 1
                self._stats.last_sequence = frame.descriptor.sequence
                self._stats.last_camera_id = frame.descriptor.camera_id
                self._stats.last_processed_at = time.time()
                self._stats.total_processing_time_ms += processing_time_ms
                self._stats.average_processing_time_ms = (
                    self._stats.total_processing_time_ms / self._stats.frames_processed
                )

            if self._result_callback is not None:
                try:
                    self._result_callback(result)
                except Exception as exc:
                    with self._stats_lock:
                        self._stats.errors += 1
                        self._stats.last_error = f"result callback failed: {exc}"
                    logger.exception("Camera %s: result callback failed: %s", self._camera_id, exc)

        except Exception as exc:
            with self._stats_lock:
                self._stats.frames_failed += 1
                self._stats.errors += 1
                self._stats.last_error = str(exc)
            logger.exception("Camera %s: frame processing failed: %s", self._camera_id, exc)
        finally:
            with self._stats_lock:
                self._stats.frames_consumed += 1


def create_processing_consumer(
    camera_id: str,
    analysis_config: AnalysisConfig,
    consumer_name: str | None = None,
    alarm_evaluator: AlarmEvaluator | None = None,
    calibration_provider: CalibrationProvider | None = None,
    temperature_converter: TemperatureConverter | None = None,
    halcon_adapter=None,
    result_callback: Callable[[ProcessingResult], None] | None = None,
    ring_depth: int = 32,
    thermal_width: int = 640,
    thermal_height: int = 480,
    thermal_dtype: np.dtype = np.dtype(np.uint16),
) -> tuple[SharedMemoryRingBuffer, ProcessingConsumer]:
    """Factory to attach to an existing ring buffer and create a ProcessingConsumer.

    This is the consumer-side factory. The ring buffer must already exist
    (created by the producer via create_ring_buffer_and_publisher).

    Args:
        camera_id: Camera identifier
        analysis_config: Analysis configuration (ROIs, unit, alarm rules)
        consumer_name: Unique consumer name (default: ``processing_<camera_id>``)
        alarm_evaluator: Optional alarm evaluator; defaults to AlarmEvaluator
        calibration_provider: Calibration provider for temperature conversion
        temperature_converter: Temperature converter (default CPUTemperatureConverter)
        halcon_adapter: Optional HALCON ROI adapter (skipped when no ROIs)
        result_callback: Optional callback receiving each ProcessingResult
        ring_depth: Ring buffer depth (must match producer)
        thermal_width: Thermal frame width (must match producer)
        thermal_height: Thermal frame height (must match producer)
        thermal_dtype: Thermal frame dtype (must match producer)

    Returns:
        Tuple of (ring_buffer, processing_consumer). The ring_buffer must be
        closed by the caller when all consumers are done.
    """
    config = RingConfig(
        camera_id=camera_id,
        thermal_spec=PayloadSpec(
            width=thermal_width,
            height=thermal_height,
            dtype=thermal_dtype,
            bytes_per_frame=thermal_width * thermal_height * thermal_dtype.itemsize,
        ),
        visible_spec=None,  # IR-only for now
        depth=ring_depth,
    )

    ring = SharedMemoryRingBuffer.attach(config)

    if temperature_converter is None:
        temperature_converter = CPUTemperatureConverter(calibration_provider)

    pipeline = SimpleProcessingPipeline(
        config=analysis_config,
        calibration_provider=calibration_provider,
        temperature_converter=temperature_converter,
        halcon_adapter=halcon_adapter,
    )

    if alarm_evaluator is None:
        alarm_evaluator = AlarmEvaluator(config=analysis_config)

    consumer = ProcessingConsumer(
        camera_id=camera_id,
        ring_buffer=ring,
        consumer_name=consumer_name or f"processing_{camera_id}",
        pipeline=pipeline,
        alarm_evaluator=alarm_evaluator,
        result_callback=result_callback,
    )

    return ring, consumer


__all__ = [
    "ProcessingConsumer",
    "ProcessingConsumerStats",
    "create_processing_consumer",
]