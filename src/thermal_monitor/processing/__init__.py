"""Processing domain: frame analysis."""

from thermal_monitor.processing.alarms import (
    AlarmEvaluationResult,
    AlarmEvaluator,
    AlarmStateTracker,
    NullAlarmEvaluator,
)

from thermal_monitor.processing.pipeline import (
    CalibrationProvider,
    FrameProcessor,
    FrameSource,
    ProcessingPipeline,
    ProcessingStats,
    SimpleProcessingPipeline,
    TemperatureConverter,
)

from thermal_monitor.processing.sources import (
    LiveFrameSource,
    SyntheticFrameSource,
)

from thermal_monitor.processing.temperature import (
    CPUTemperatureConverter,
    CachingCalibrationProvider,
)

__all__ = [
    "AlarmEvaluationResult",
    "AlarmEvaluator",
    "AlarmStateTracker",
    "CalibrationProvider",
    "CPUTemperatureConverter",
    "CachingCalibrationProvider",
    "FrameProcessor",
    "FrameSource",
    "LiveFrameSource",
    "NullAlarmEvaluator",
    "ProcessingPipeline",
    "ProcessingStats",
    "SimpleProcessingPipeline",
    "SyntheticFrameSource",
    "TemperatureConverter",
]