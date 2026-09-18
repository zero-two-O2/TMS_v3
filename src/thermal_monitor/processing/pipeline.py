"""
processing.pipeline -- Processing pipeline contracts and base implementations.

Defines the FrameSource protocol and ProcessingPipeline abstraction.
The pipeline operates on the V3 Frame contract and produces AnalysisResults.
"""

from __future__ import annotations

import threading
import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional, Protocol, Sequence

import numpy as np

from thermal_monitor.core.frame import Frame
from thermal_monitor.core.models import (
    AnalysisConfig,
    AnalysisResult,
    ROIConfig,
    ROIStatistics,
    TemperatureUnit,
)
from thermal_monitor.core.roi_resolver import CachedROIResolver, resolve_rois
from thermal_monitor.processing.halcon import HalconROIAdapter, process_rois_with_halcon

logger = logging.getLogger(__name__)


class FrameSource(Protocol):
    """Protocol for frame sources (live, offline, synthetic).

    Any frame source must be able to provide frames in sequence.
    """

    def get_next_frame(self) -> Frame | None:
        """Get the next frame, or None if no frame is available."""
        ...

    def get_latest_frame(self) -> Frame | None:
        """Get the most recent frame without advancing."""
        ...

    def seek(self, sequence: int) -> bool:
        """Seek to a specific frame sequence (for offline sources).

        Returns True if seek was successful.
        """
        ...

    @property
    def camera_id(self) -> str:
        """Camera ID this source produces frames for."""
        ...

    @property
    def is_live(self) -> bool:
        """Whether this is a live source."""
        ...


@dataclass(frozen=True, slots=True)
class ProcessingStats:
    """Processing pipeline statistics."""

    frames_processed: int = 0
    frames_dropped: int = 0
    total_processing_time_ms: float = 0.0
    average_processing_time_ms: float = 0.0
    last_frame_sequence: int | None = None
    last_processed_at: float | None = None
    errors: int = 0


class FrameProcessor(Protocol):
    """Protocol for a frame processor that produces AnalysisResult from Frame."""

    def process(self, frame: Frame, config: AnalysisConfig) -> AnalysisResult:
        """Process a frame and return analysis results."""
        ...

    def get_stats(self) -> ProcessingStats:
        """Get processing statistics."""
        ...


class ProcessingPipeline(ABC):
    """Abstract processing pipeline.

    Consumes Frames from a FrameSource and produces AnalysisResults.
    The pipeline is source-agnostic: it works with live, offline, or synthetic frames.
    """

    def __init__(self, config: AnalysisConfig) -> None:
        self._config = config
        self._stats = ProcessingStats()

    @property
    def config(self) -> AnalysisConfig:
        return self._config

    @property
    def stats(self) -> ProcessingStats:
        return self._stats

    @abstractmethod
    def process_frame(self, frame: Frame) -> AnalysisResult:
        """Process a single frame.

        Args:
            frame: Input frame from any FrameSource.

        Returns:
            AnalysisResult with per-ROI statistics and metadata.
        """
        ...

    def process_frames(self, frames: Sequence[Frame]) -> list[AnalysisResult]:
        """Process multiple frames sequentially."""
        results = []
        for frame in frames:
            results.append(self.process_frame(frame))
        return results

    def update_config(self, config: AnalysisConfig) -> None:
        """Update the analysis configuration."""
        self._config = config


class CalibrationProvider(Protocol):
    """Protocol for providing calibration data."""

    def get_calibration(self, camera_id: str) -> np.ndarray | None:
        """Get calibration data for a camera.

        Returns a calibration array or None if not available.
        """
        ...


class TemperatureConverter(Protocol):
    """Protocol for converting raw thermal data to temperature."""

    def raw_to_temperature(
        self,
        raw_data: np.ndarray,
        calibration: np.ndarray | None,
        emissivity: float,
        ambient_temp: float,
        distance: float,
        humidity: float,
        reflected_temp: float,
        camera_id: str | None = None,
    ) -> np.ndarray:
        """Convert raw thermal data to temperature values.

        Parameters
        ----------
        camera_id : str | None
            Optional camera identifier. When provided and calibration is None,
            implementations with a calibration_provider should attempt to fetch
            camera-specific calibration.
        """
        ...


class SimpleProcessingPipeline(ProcessingPipeline):
    """Simple reference implementation of the processing pipeline.

    Uses the proven HALCON ROI statistics path for Rectangle1,
    and HALCON-ready implementations for other geometries.
    """

    def __init__(
        self,
        config: AnalysisConfig,
        calibration_provider: CalibrationProvider | None = None,
        temperature_converter: TemperatureConverter | None = None,
        halcon_adapter: HalconROIAdapter | None = None,
        roi_resolver: CachedROIResolver | None = None,
    ) -> None:
        super().__init__(config)
        self._calibration_provider = calibration_provider
        self._temperature_converter = temperature_converter
        self._roi_resolver = roi_resolver or CachedROIResolver()
        self._halcon_adapter = halcon_adapter or HalconROIAdapter()
        self._last_temperature_image: np.ndarray | None = None
        # Active PTZ position override (Phase 8 retargeting). Acquisition
        # frames never carry position_id, so the pipeline falls back to
        # this tuple when frame metadata lacks one. Swapped atomically
        # under a lock; each frame snapshots it once, so a retarget can
        # never mix old/new context within a single frame. The generation
        # is stamped onto every produced result for stale-result guards.
        self._position_lock = threading.Lock()
        self._active_position: tuple[str, int] = ("default", 0)

    def set_active_position(
        self, position_id: str, generation: int | None = None
    ) -> int:
        """Publish a new active position context. Returns its generation.

        With ``generation=None`` the generation bumps (normal retarget).
        Pass an explicit generation only to re-apply a known-good context
        (e.g. after an observer restart) without invalidating in-flight
        results that already carry it.
        """
        if not position_id:
            raise ValueError("position_id is required")
        with self._position_lock:
            if generation is None:
                generation = self._active_position[1] + 1
            self._active_position = (position_id, generation)
            return generation

    @property
    def active_position(self) -> tuple[str, int]:
        """Current ``(position_id, context_generation)`` snapshot."""
        with self._position_lock:
            return self._active_position

    @property
    def calibration_provider(self) -> CalibrationProvider | None:
        return self._calibration_provider

    @property
    def temperature_converter(self) -> TemperatureConverter | None:
        return self._temperature_converter

    @property
    def roi_resolver(self) -> CachedROIResolver:
        return self._roi_resolver

    @property
    def last_temperature_image(self) -> np.ndarray | None:
        """Temperature image (float32 °C) computed for the most recent frame.

        None when no temperature converter is configured or the most recent
        frame carried no thermal payload.
        """
        return self._last_temperature_image

    def process_frame(self, frame: Frame) -> AnalysisResult:
        """Process a frame using the configured ROIs and HALCON statistics."""
        start_time = time.perf_counter()
        self._last_temperature_image = None

        # Get applicable ROIs for the current frame's position.
        # Snapshot the active context once: acquisition frames are
        # position-agnostic, so the override applies; a retarget racing
        # this frame cannot mix contexts mid-frame.
        with self._position_lock:
            override_position_id, context_generation = self._active_position
        position_id = (
            frame.descriptor.metadata.get("position_id")
            or override_position_id
            or "default"
        )
        result_context = MappingProxyType(
            {"position_id": position_id, "context_generation": context_generation}
        )
        rois = self._roi_resolver.resolve(self._config, frame.descriptor.camera_id, position_id)

        roi_results: dict[str, ROIStatistics] = {}

        # Get thermal data
        thermal_data = frame.payload.thermal
        if thermal_data is None:
            # No thermal data - return empty result
            return AnalysisResult(
                camera_id=frame.descriptor.camera_id,
                frame_sequence=frame.descriptor.sequence,
                frame_timestamp=frame.descriptor.timestamp,
                roi_results=MappingProxyType({}),
                processing_time_ms=(time.perf_counter() - start_time) * 1000,
                metadata=result_context,
            )

        # Convert raw to temperature if converter available
        temperature_data = thermal_data
        if self.temperature_converter is not None:
            calibration = None
            if self.calibration_provider is not None:
                calibration = self.calibration_provider.get_calibration(frame.descriptor.camera_id)

            if calibration is None:
                logger.error(
                    "No calibration available for camera %s; refusing to interpret "
                    "Mono16 detector values as Celsius",
                    frame.descriptor.camera_id,
                )
                raise RuntimeError(
                    f"No valid calibration available for camera {frame.descriptor.camera_id}"
                )

            temperature_data = self.temperature_converter.raw_to_temperature(
                raw_data=thermal_data,
                calibration=calibration,
                emissivity=self._config.default_emissivity,
                ambient_temp=self._config.ambient_temperature,
                distance=self._config.distance,
                humidity=self._config.humidity,
                reflected_temp=self._config.reflected_temperature,
                camera_id=frame.descriptor.camera_id,
            )
            self._last_temperature_image = temperature_data
            finite = np.isfinite(temperature_data)
            logger.debug(
                "Thermal frame camera_id=%s size=%sx%s pixel_type=%s raw_min=%s raw_max=%s "
                "temperature_min=%s temperature_max=%s calibration=loaded emissivity=%.3f ambient=%.2f",
                frame.descriptor.camera_id,
                thermal_data.shape[1],
                thermal_data.shape[0],
                thermal_data.dtype,
                int(np.min(thermal_data)),
                int(np.max(thermal_data)),
                float(np.min(temperature_data[finite])) if np.any(finite) else None,
                float(np.max(temperature_data[finite])) if np.any(finite) else None,
                self._config.default_emissivity,
                self._config.ambient_temperature,
            )

        # Process ROIs using the worker-owned cached batched HALCON path.
        # The frame's context snapshot (taken above) travels with the call
        # so regions are built, cached, and reused per context; a retarget
        # racing this frame cannot mix old/new regions mid-frame.
        if rois:
            stats_list = process_rois_with_halcon(
                rois=rois,
                temperature_image=temperature_data,
                adapter=self._halcon_adapter,
                camera_id=frame.descriptor.camera_id,
                position_id=position_id,
                context_generation=context_generation,
            )

            for stat in stats_list:
                roi_results[stat.roi_id] = stat

        # Post-processing generation guard: a position retarget (or ROI
        # resolver invalidation) may have landed while HALCON was
        # running. Publishing those stats would mix contexts, and
        # feeding them to alarms could false-trigger, so discard the
        # ROI results and flag the frame instead. frames_dropped is the
        # existing discard diagnostic.
        with self._position_lock:
            live_position_id, live_generation = self._active_position
        stale_discarded = (
            live_position_id != position_id or live_generation != context_generation
        )

        # Compute overall statistics (valid measurements only: an
        # explicitly invalid ROI must not poison frame aggregates).
        overall_min: float | None = None
        overall_max: float | None = None
        overall_sum = 0.0
        overall_count = 0

        for stat in roi_results.values():
            if not stat.valid:
                continue
            if overall_min is None or stat.min_temp < overall_min:
                overall_min = stat.min_temp
            if overall_max is None or stat.max_temp > overall_max:
                overall_max = stat.max_temp
            # Use mean * pixel_count approximation since we don't have pixel_count
            overall_sum += stat.mean_temp
            overall_count += 1

        overall_mean = overall_sum / overall_count if overall_count > 0 else None

        processing_time_ms = (time.perf_counter() - start_time) * 1000

        if stale_discarded:
            logger.warning(
                "Camera %s: discarding ROI results for superseded context "
                "(position=%s generation=%s)",
                frame.descriptor.camera_id, position_id, context_generation,
            )
            roi_results = {}
            overall_min = overall_max = overall_mean = None
            result_context = MappingProxyType({
                "position_id": position_id,
                "context_generation": context_generation,
                "stale_discarded": True,
            })

        result = AnalysisResult(
            camera_id=frame.descriptor.camera_id,
            frame_sequence=frame.descriptor.sequence,
            frame_timestamp=frame.descriptor.timestamp,
            roi_results=MappingProxyType(roi_results),
            overall_min=overall_min,
            overall_max=overall_max,
            overall_mean=overall_mean,
            unit=self._config.unit,
            processing_time_ms=processing_time_ms,
            metadata=result_context,
        )

        # Update stats
        self._stats = ProcessingStats(
            frames_processed=self._stats.frames_processed + 1,
            frames_dropped=self._stats.frames_dropped + (1 if stale_discarded else 0),
            total_processing_time_ms=self._stats.total_processing_time_ms + processing_time_ms,
            average_processing_time_ms=(
                self._stats.total_processing_time_ms + processing_time_ms
            ) / (self._stats.frames_processed + 1),
            last_frame_sequence=frame.descriptor.sequence,
            last_processed_at=time.time(),
            errors=self._stats.errors,
        )

        return result


class NullFrameProcessor:
    """Null processor that returns empty results (for testing)."""

    def process(self, frame: Frame, config: AnalysisConfig) -> AnalysisResult:
        return AnalysisResult(
            camera_id=frame.descriptor.camera_id,
            frame_sequence=frame.descriptor.sequence,
            frame_timestamp=frame.descriptor.timestamp,
            roi_results=MappingProxyType({}),
            processing_time_ms=0.0,
        )

    def get_stats(self) -> ProcessingStats:
        return ProcessingStats()
