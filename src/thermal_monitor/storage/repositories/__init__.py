"""Storage repositories package."""

from thermal_monitor.storage.repositories.base import BaseRepository, RepositoryResult
from thermal_monitor.storage.repositories.camera import CameraRepository
from thermal_monitor.storage.repositories.roi import (
    AlarmRuleRepository,
    AnalysisConfigRepository,
    PositionROIRepository,
    ROIRepository,
)
from thermal_monitor.storage.repositories.alarm import AlarmEventRepository
from thermal_monitor.storage.repositories.recording import RecordingRepository
from thermal_monitor.storage.repositories.system import (
    SystemConfigRepository,
    RecordingConfigRepository,
)

__all__ = [
    "AlarmEventRepository",
    "AlarmRuleRepository",
    "AnalysisConfigRepository",
    "BaseRepository",
    "CameraRepository",
    "PositionROIRepository",
    "RecordingConfigRepository",
    "RecordingRepository",
    "RepositoryResult",
    "ROIRepository",
    "SystemConfigRepository",
]