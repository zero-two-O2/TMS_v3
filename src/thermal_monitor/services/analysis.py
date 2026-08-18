"""
services.analysis -- Analysis service for coordinating frame processing.
"""

from __future__ import annotations

import dataclasses
from typing import Callable, Optional

from thermal_monitor.core.frame import Frame
from thermal_monitor.core.models import (
    AnalysisConfig,
    AnalysisResult,
    CameraConfig,
)
from thermal_monitor.processing import (
    FrameSource,
    ProcessingPipeline,
    SimpleProcessingPipeline,
)


@dataclass
class AnalysisService:
    """Application-level service for coordinating frame analysis.

    Manages processing pipelines for each camera and coordinates
    frame processing with the current mode and configuration.
    """

    processing_pipelines: dict[str, ProcessingPipeline] = dataclasses.field(default_factory=dict)
    frame_sources: dict[str, FrameSource] = dataclasses.field(default_factory=dict)
    _result_callbacks: dict[str, list[Callable[[AnalysisResult], None]]] = dataclasses.field(default_factory=dict)
    _error_callbacks: dict[str, list[Callable[[Exception], None]]] = dataclasses.field(default_factory=dict)

    def __init__(self) -> None:
        pass

    def get_pipeline(self, camera_id: str) -> ProcessingPipeline | None:
        return self.processing_pipelines.get(camera_id)

    def get_all_pipelines(self) -> list[ProcessingPipeline]:
        return list(self.processing_pipelines.values())

    def set_pipeline(self, camera_id: str, pipeline: ProcessingPipeline) -> None:
        self.processing_pipelines[camera_id] = pipeline

    def create_simple_pipeline(self, config: AnalysisConfig) -> SimpleProcessingPipeline:
        pipeline = SimpleProcessingPipeline(config=config)
        self.processing_pipelines[config.camera_id] = pipeline
        return pipeline

    def remove_pipeline(self, camera_id: str) -> bool:
        if camera_id in self.processing_pipelines:
            del self.processing_pipelines[camera_id]
            return True
        return False

    def set_frame_source(self, camera_id: str, source: FrameSource) -> None:
        self.frame_sources[camera_id] = source

    def get_frame_source(self, camera_id: str) -> FrameSource | None:
        return self.frame_sources.get(camera_id)

    def process_frame(self, frame: Frame, config: AnalysisConfig) -> AnalysisResult:
        """Process a single frame using the pipeline for its camera."""
        pipeline = self.processing_pipelines.get(frame.descriptor.camera_id)
        if pipeline is None:
            pipeline = SimpleProcessingPipeline(config=config)
            self.processing_pipelines[frame.descriptor.camera_id] = pipeline

        result = pipeline.process_frame(frame)
        self._notify_result(result)
        return result

    def process_frame_with_config(self, frame: Frame) -> AnalysisResult:
        """Process a frame using the stored analysis config for its camera."""
        config = self.get_analysis_config(frame.descriptor.camera_id)
        if config is None:
            raise ValueError(f"No analysis config for camera {frame.descriptor.camera_id}")
        return self.process_frame(frame, config)

    def get_analysis_config(self, camera_id: str) -> AnalysisConfig | None:
        """Get analysis config from the pipeline."""
        pipeline = self.processing_pipelines.get(camera_id)
        if pipeline is not None:
            return pipeline.config
        return None

    def add_result_callback(self, camera_id: str, callback: Callable[[AnalysisResult], None]) -> None:
        if camera_id not in self._result_callbacks:
            self._result_callbacks[camera_id] = []
        self._result_callbacks[camera_id].append(callback)

    def remove_result_callback(self, camera_id: str, callback: Callable[[AnalysisResult], None]) -> None:
        if camera_id in self._result_callbacks:
            try:
                self._result_callbacks[camera_id].remove(callback)
            except ValueError:
                pass

    def add_error_callback(self, camera_id: str, callback: Callable[[Exception], None]) -> None:
        if camera_id not in self._error_callbacks:
            self._error_callbacks[camera_id] = []
        self._error_callbacks[camera_id].append(callback)

    def remove_error_callback(self, camera_id: str, callback: Callable[[Exception], None]) -> None:
        if camera_id in self._error_callbacks:
            try:
                self._error_callbacks[camera_id].remove(callback)
            except ValueError:
                pass

    def _notify_result(self, result: AnalysisResult) -> None:
        callbacks = self._result_callbacks.get(result.camera_id, [])
        for cb in callbacks:
            try:
                cb(result)
            except Exception as e:
                self._notify_error(result.camera_id, e)

    def _notify_error(self, camera_id: str, error: Exception) -> None:
        callbacks = self._error_callbacks.get(camera_id, [])
        for cb in callbacks:
            try:
                cb(error)
            except Exception:
                pass

    def get_pipeline_stats(self, camera_id: str) -> dict:
        pipeline = self.processing_pipelines.get(camera_id)
        if pipeline is not None:
            return {
                "frames_processed": pipeline.stats.frames_processed,
                "frames_dropped": pipeline.stats.frames_dropped,
                "average_processing_time_ms": pipeline.stats.average_processing_time_ms,
            }
        return {}

    def get_all_stats(self) -> dict[str, dict]:
        return {cam_id: self.get_pipeline_stats(cam_id) for cam_id in self.processing_pipelines}