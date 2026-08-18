"""
processing.pipeline -- Processing pipeline contracts and base implementations.

Defines the FrameSource protocol and ProcessingPipeline abstraction.
The pipeline operates on the V3 Frame contract and produces AnalysisResults.
"""

from __future__ import annotations

import time
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
from thermal_monitor.processing.halcon import HalconROIAdapter, process_rois_with_halcon
from thermal_monitor.processing.roi_resolver import ROIResolver
from thermal_monitor.storage.database import Database


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
    ) -> np.ndarray:
        """Convert raw thermal data to temperature values."""
        ...


class SimpleProcessingPipeline(ProcessingPipeline):
    """Simple reference implementation of the processing pipeline.

    Uses the proven HALCON ROI statistics path for Rectangle1,
    and HALCON-ready implementations for other geometries.
    """

    def __init__(
        self,
        config: AnalysisConfig,
        database: Database,
        calibration_provider: CalibrationProvider | None = None,
        temperature_converter: TemperatureConverter | None = None,
        halcon_adapter: HalconROIAdapter | None = None,
    ) -> None:
        super().__init__(config)
        self._calibration_provider = calibration_provider
        self._temperature_converter = temperature_converter
        self._roi_resolver = ROIResolver(database)
        self._halcon_adapter = halcon_adapter or HalconROIAdapter()

    @property
    def calibration_provider(self) -> CalibrationProvider | None:
        return self._calibration_provider

    @property
    def temperature_converter(self) -> TemperatureConverter | None:
        return self._temperature_converter

    @property
    def roi_resolver(self) -> ROIResolver:
        return self._roi_resolver

    def process_frame(self, frame: Frame) -> AnalysisResult:
        """Process a frame using the configured ROIs and HALCON statistics."""
        start_time = time.perf_counter()

        # Get applicable ROIs for the current frame's position
        position_id = frame.descriptor.metadata.get("position_id", "default")
        rois = self._roi_resolver.resolve(frame.descriptor.camera_id, position_id)

        # Fall back to config if resolver returns empty (e.g., no database)
        if not rois:
            rois = self._config.get_rois_for_position(position_id)

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
            )

        # Convert raw to temperature if converter available
        temperature_data = thermal_data
        if self.temperature_converter is not None:
            calibration = None
            if self.calibration_provider is not None:
                calibration = self.calibration_provider.get_calibration(frame.descriptor.camera_id)

            temperature_data = self.temperature_converter.raw_to_temperature(
                raw_data=thermal_data,
                calibration=calibration,
                emissivity=self._config.default_emissivity,
                ambient_temp=self._config.ambient_temperature,
                distance=self._config.distance,
                humidity=self._config.humidity,
                reflected_temp=self._config.reflected_temperature,
            )

        # Process ROIs using HALCON adapter
        if rois:
            stats_list = process_rois_with_halcon(
                rois=rois,
                temperature_image=temperature_data,
                adapter=self._halcon_adapter,
            )

            for stat in stats_list:
                roi_results[stat.roi_id] = stat

        # Compute overall statistics
        overall_min: float | None = None
        overall_max: float | None = None
        overall_sum = 0.0
        overall_count = 0

        for stat in roi_results.values():
            if overall_min is None or stat.min_temp < overall_min:
                overall_min = stat.min_temp
            if overall_max is None or stat.max_temp > overall_max:
                overall_max = stat.max_temp
            # Use mean * pixel_count approximation since we don't have pixel_count
            overall_sum += stat.mean_temp
            overall_count += 1

        overall_mean = overall_sum / overall_count if overall_count > 0 else None

        processing_time_ms = (time.perf_counter() - start_time) * 1000

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
        )

        # Update stats
        self._stats = ProcessingStats(
            frames_processed=self._stats.frames_processed + 1,
            frames_dropped=self._stats.frames_dropped,
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