"""Application services package.

Service implementations are loaded on demand so lightweight services such as
camera discovery do not pull in the processing and acquisition dependency
graph merely because this package is imported.
"""

from importlib import import_module


_EXPORTS = {
    "AnalysisService": ("thermal_monitor.services.analysis", "AnalysisService"),
    "AlarmService": ("thermal_monitor.services.alarm", "AlarmService"),
    "ConfigurationService": ("thermal_monitor.services.configuration", "ConfigurationService"),
    "CameraDiscoveryError": ("thermal_monitor.services.discovery", "CameraDiscoveryError"),
    "CameraDiscoveryService": ("thermal_monitor.services.discovery", "CameraDiscoveryService"),
    "DiscoveredCamera": ("thermal_monitor.services.discovery", "DiscoveredCamera"),
    "ModeService": ("thermal_monitor.services.mode", "ModeService"),
    "OfflineService": ("thermal_monitor.services.offline", "OfflineService"),
    "OfflineSession": ("thermal_monitor.services.offline", "OfflineSession"),
    "ContinuousRecordingManager": ("thermal_monitor.services.recording", "ContinuousRecordingManager"),
    "RecordingConsumer": ("thermal_monitor.services.recording", "RecordingConsumer"),
    "RecordingConsumerStats": ("thermal_monitor.services.recording", "RecordingConsumerStats"),
    "RecordingService": ("thermal_monitor.services.recording", "RecordingService"),
    "create_recording_consumer": ("thermal_monitor.services.recording", "create_recording_consumer"),
    "CameraRuntime": ("thermal_monitor.services.runtime", "CameraRuntime"),
    "CameraRuntimeError": ("thermal_monitor.services.runtime", "CameraRuntimeError"),
    "CameraRuntimeService": ("thermal_monitor.services.runtime", "CameraRuntimeService"),
    "build_driver_config": ("thermal_monitor.services.runtime", "build_driver_config"),
}


def __getattr__(name: str):
    """Load a service export only when it is requested."""
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value

__all__ = [
    "AlarmService",
    "AnalysisService",
    "CameraRuntime",
    "CameraRuntimeError",
    "CameraRuntimeService",
    "ConfigurationService",
    "CameraDiscoveryError",
    "CameraDiscoveryService",
    "DiscoveredCamera",
    "ContinuousRecordingManager",
    "ModeService",
    "OfflineService",
    "OfflineSession",
    "RecordingConsumer",
    "RecordingConsumerStats",
    "RecordingService",
    "build_driver_config",
    "create_recording_consumer",
]
