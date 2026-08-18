"""Core domain models package.

Exports grouped models for camera, inspection, and system domains.
"""

from thermal_monitor.core.models.camera import (
    CameraConfig,
    CameraConnectionState,
    CameraIdentity,
    CameraInfo,
    CameraStatus,
    PTZConfig,
    PTZLimits,
    PTZMode,
    PTZPosition,
)

from thermal_monitor.core.models.inspection import (
    AlarmCondition,
    AlarmEvent,
    AlarmRule,
    AlarmSeverity,
    AnalysisConfig,
    AnalysisResult,
    PositionROIAssociation,
    ROIConfig,
    ROIGeometry,
    ROIStatistics,
    ROIShape,
    ROIType,
    TemperatureLimits,
    TemperatureUnit,
)

from thermal_monitor.core.models.system import (
    ApplicationState,
    RecordingConfig,
    RecordingMetadata,
    RecordingState,
    RecordingTrigger,
    SystemConfig,
    SystemStatus,
)

__all__ = [
    # Camera
    "CameraConnectionState",
    "CameraConfig",
    "CameraIdentity",
    "CameraInfo",
    "CameraStatus",
    "PTZConfig",
    "PTZLimits",
    "PTZMode",
    "PTZPosition",
    # Inspection
    "AlarmCondition",
    "AlarmEvent",
    "AlarmRule",
    "AlarmSeverity",
    "AnalysisConfig",
    "AnalysisResult",
    "PositionROIAssociation",
    "ROIConfig",
    "ROIGeometry",
    "ROIStatistics",
    "ROIShape",
    "ROIType",
    "TemperatureLimits",
    "TemperatureUnit",
    # System
    "ApplicationState",
    "RecordingConfig",
    "RecordingMetadata",
    "RecordingState",
    "RecordingTrigger",
    "SystemConfig",
    "SystemStatus",
]