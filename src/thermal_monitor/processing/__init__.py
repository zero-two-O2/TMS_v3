"""Processing domain: frame analysis."""

from thermal_monitor.processing.alarms import (
    AlarmEvaluationResult,
    AlarmEvaluator,
    AlarmStateTracker,
    NullAlarmEvaluator,
)

from thermal_monitor.processing.consumer import (
    ProcessingConsumer,
    ProcessingConsumerStats,
    create_processing_consumer,
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

from thermal_monitor.processing.worker import (
    ProcessingResult,
    ProcessingWorker,
    create_processing_worker,
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
    "ProcessingConsumer",
    "ProcessingConsumerStats",
    "ProcessingPipeline",
    "ProcessingResult",
    "ProcessingStats",
    "ProcessingWorker",
    "SimpleProcessingPipeline",
    "SyntheticFrameSource",
    "TemperatureConverter",
    "create_processing_consumer",
    "create_processing_worker",
]