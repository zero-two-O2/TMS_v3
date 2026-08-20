"""
core.models.system -- System-level domain models.

Application configuration, system status, recording metadata.
Independent of HALCON, PyQt, and SQL Server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional

from thermal_monitor.core.modes import ApplicationMode


class RecordingState(str, Enum):
    """Recording lifecycle state."""

    IDLE = "idle"
    ARMED = "armed"          # Waiting for alarm trigger
    RECORDING = "recording"  # Actively recording
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    ERROR = "error"


class RecordingTrigger(str, Enum):
    """What triggered the recording."""

    MANUAL = "manual"
    ALARM = "alarm"
    SCHEDULED = "scheduled"
    DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True, slots=True)
class RecordingMetadata:
    """Metadata for a recording session."""

    recording_id: str
    camera_id: str
    trigger: RecordingTrigger = RecordingTrigger.MANUAL
    state: RecordingState = RecordingState.IDLE
    start_timestamp: float = 0.0
    end_timestamp: float | None = None
    start_sequence: int = 0
    end_sequence: int | None = None
    pre_alarm_frames: int = 0
    post_alarm_frames: int = 0
    alarm_event_id: str | None = None
    position_id: str | None = None
    roi_config_hash: str | None = None
    file_path: str | None = None
    file_size_bytes: int = 0
    frame_count: int = 0
    duration_seconds: float = 0.0
    metadata: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not self.recording_id:
            raise ValueError("recording_id is required")
        if not self.camera_id:
            raise ValueError("camera_id is required")

    @property
    def is_active(self) -> bool:
        return self.state in (RecordingState.ARMED, RecordingState.RECORDING, RecordingState.FINALIZING)

    @property
    def is_complete(self) -> bool:
        return self.state == RecordingState.COMPLETED


@dataclass(frozen=True, slots=True)
class RecordingConfig:
    """Configuration for alarm-triggered recording."""

    camera_id: str
    enabled: bool = True
    pre_alarm_seconds: float = 10.0
    post_alarm_seconds: float = 30.0
    max_duration_seconds: float = 300.0
    max_file_size_mb: int = 500
    storage_path: str = ""
    compression_enabled: bool = False
    metadata: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not self.camera_id:
            raise ValueError("camera_id is required")
        if self.pre_alarm_seconds < 0:
            raise ValueError("pre_alarm_seconds must be >= 0")
        if self.post_alarm_seconds < 0:
            raise ValueError("post_alarm_seconds must be >= 0")


@dataclass(frozen=True, slots=True)
class SystemConfig:
    """Application-wide system configuration."""

    application_name: str = "Thermal Monitoring System V3"
    version: str = "3.0.0"

    # Mode
    default_mode: ApplicationMode = ApplicationMode.CONFIGURATION

    # Camera discovery/management
    max_cameras: int = 8
    camera_discovery_enabled: bool = True
    camera_discovery_interval_seconds: float = 30.0

    # Processing
    processing_enabled: bool = True
    processing_interval_ms: int = 100  # Minimum time between processing runs

    # Alarms
    alarm_evaluation_enabled: bool = True
    alarm_cooldown_seconds: float = 5.0
    max_alarm_history: int = 10000

    # Recording
    recording_enabled: bool = True
    default_recording_config: RecordingConfig | None = None

    # Storage
    database_connection_string: str = ""
    recording_storage_path: str = ""

    # Database (SQL Server connection form fields)
    database_server: str = ""
    database_name: str = ""
    database_trusted: bool = True
    database_username: str = ""
    database_password: str = ""
    auto_save_config: bool = True

    # Offline
    offline_storage_path: str = ""

    # Logging
    log_level: str = "INFO"
    log_file_path: str = ""
    log_max_size_mb: int = 100
    log_backup_count: int = 5

    # Network
    bind_address: str = "0.0.0.0"
    http_port: int = 8080

    # Metadata
    metadata: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if self.max_cameras <= 0:
            raise ValueError("max_cameras must be > 0")
        if self.processing_interval_ms <= 0:
            raise ValueError("processing_interval_ms must be > 0")
        if self.alarm_cooldown_seconds < 0:
            raise ValueError("alarm_cooldown_seconds must be >= 0")


@dataclass(frozen=True, slots=True)
class SystemStatus:
    """Runtime system status."""

    mode: ApplicationMode = ApplicationMode.CONFIGURATION
    uptime_seconds: float = 0.0
    camera_count: int = 0
    active_camera_count: int = 0
    acquiring_camera_count: int = 0
    alarm_count: int = 0
    active_alarm_count: int = 0
    recording_count: int = 0
    active_recording_count: int = 0
    storage_free_bytes: int = 0
    storage_total_bytes: int = 0
    memory_usage_mb: float = 0.0
    cpu_usage_percent: float = 0.0
    last_error: str | None = None
    metadata: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def is_healthy(self) -> bool:
        return self.last_error is None and self.active_camera_count > 0

    @property
    def storage_usage_percent(self) -> float:
        if self.storage_total_bytes == 0:
            return 0.0
        used = self.storage_total_bytes - self.storage_free_bytes
        return (used / self.storage_total_bytes) * 100.0


@dataclass(frozen=True, slots=True)
class ApplicationState:
    """Complete application state combining config, mode, and status."""

    config: SystemConfig
    mode: ApplicationMode
    status: SystemStatus

    @property
    def is_configuration_mode(self) -> bool:
        return self.mode == ApplicationMode.CONFIGURATION

    @property
    def is_observer_mode(self) -> bool:
        return self.mode == ApplicationMode.OBSERVER

    @property
    def is_offline_mode(self) -> bool:
        return self.mode == ApplicationMode.OFFLINE