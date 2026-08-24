"""
Configuration models for TMS V3.

Typed configuration sections with validation.
All models use dataclasses with explicit validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    name: str = "Thermal Monitoring System V3"
    version: str = "3.0.0"
    default_mode: str = "CONFIGURATION"
    start_maximized: bool = True

    def __post_init__(self) -> None:
        if self.default_mode not in ("LAUNCHER", "CONFIGURATION", "LIVE", "OFFLINE"):
            raise ValueError(f"default_mode must be one of LAUNCHER, CONFIGURATION, LIVE, OFFLINE; got {self.default_mode!r}")


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    enabled: bool = False
    type: str = "sqlserver"
    driver: str = "ODBC Driver 17 for SQL Server"
    host: str = ""
    port: int = 1433
    name: str = ""
    trusted_connection: bool = True
    username: str = ""
    password_env: str = "TMS_DB_PASSWORD"
    connection_timeout: int = 30
    command_timeout: int = 30
    trust_server_certificate: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError(f"database.port must be between 1 and 65535; got {self.port}")
        if self.connection_timeout < 0:
            raise ValueError(f"database.connection_timeout must be >= 0; got {self.connection_timeout}")
        if self.command_timeout < 0:
            raise ValueError(f"database.command_timeout must be >= 0; got {self.command_timeout}")


@dataclass(frozen=True, slots=True)
class CameraDiscoveryConfig:
    enabled: bool = True
    startup_scan: bool = True
    halcon_interface: str = "GigEVision2"
    attempts: int = 3
    retry_delay_s: float = 3.0
    interval_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError(f"cameras.discovery.attempts must be >= 1; got {self.attempts}")
        if self.retry_delay_s < 0:
            raise ValueError(f"cameras.discovery.retry_delay_s must be >= 0; got {self.retry_delay_s}")
        if self.interval_seconds < 0:
            raise ValueError(f"cameras.discovery.interval_seconds must be >= 0; got {self.interval_seconds}")


@dataclass(frozen=True, slots=True)
class CameraAcquisitionConfig:
    target_fps: int = 9
    grab_timeout_ms: int = 2000
    socket_buffer_size: int = 1048576
    num_buffers: int = 8
    stream_source_thermal: str = "IR_Data"
    thermal_bits_per_channel: int = 16
    stream_source_visible: Optional[str] = None
    visible_bits_per_channel: int = -1

    def __post_init__(self) -> None:
        if self.target_fps <= 0:
            raise ValueError(f"cameras.acquisition.target_fps must be > 0; got {self.target_fps}")
        if self.grab_timeout_ms <= 0:
            raise ValueError(f"cameras.acquisition.grab_timeout_ms must be > 0; got {self.grab_timeout_ms}")
        if self.socket_buffer_size <= 0:
            raise ValueError(f"cameras.acquisition.socket_buffer_size must be > 0; got {self.socket_buffer_size}")
        if self.num_buffers <= 0:
            raise ValueError(f"cameras.acquisition.num_buffers must be > 0; got {self.num_buffers}")


@dataclass(frozen=True, slots=True)
class CameraRecoveryConfig:
    consecutive_fail_limit: int = 3
    reconnect_interval_s: float = 3.0
    reconnect_backoff_factor: float = 2.0
    max_reconnect_attempts: int = 10

    def __post_init__(self) -> None:
        if self.consecutive_fail_limit <= 0:
            raise ValueError(f"cameras.recovery.consecutive_fail_limit must be > 0; got {self.consecutive_fail_limit}")
        if self.reconnect_interval_s < 0:
            raise ValueError(f"cameras.recovery.reconnect_interval_s must be >= 0; got {self.reconnect_interval_s}")
        if self.reconnect_backoff_factor < 1.0:
            raise ValueError(f"cameras.recovery.reconnect_backoff_factor must be >= 1.0; got {self.reconnect_backoff_factor}")
        if self.max_reconnect_attempts <= 0:
            raise ValueError(f"cameras.recovery.max_reconnect_attempts must be > 0; got {self.max_reconnect_attempts}")


@dataclass(frozen=True, slots=True)
class CameraConnectionConfig:
    device_identifier: str = "default"
    ip_mode: str = "auto"

    def __post_init__(self) -> None:
        if self.ip_mode not in ("auto", "static"):
            raise ValueError(f"cameras.connection.ip_mode must be 'auto' or 'static'; got {self.ip_mode!r}")


@dataclass(frozen=True, slots=True)
class CameraStartupConfig:
    acquire_timeout_s: float = 10.0

    def __post_init__(self) -> None:
        if self.acquire_timeout_s <= 0:
            raise ValueError(f"cameras.startup.acquire_timeout_s must be > 0; got {self.acquire_timeout_s}")


@dataclass(frozen=True, slots=True)
class CameraMappingConfig:
    camera_id: str
    serial_number: str
    enabled: bool = True
    name: str = ""
    target_fps: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.camera_id:
            raise ValueError("cameras.mapping[].camera_id is required")
        if not self.serial_number:
            raise ValueError("cameras.mapping[].serial_number is required")
        if self.target_fps is not None and self.target_fps <= 0:
            raise ValueError(f"cameras.mapping[].target_fps must be > 0; got {self.target_fps}")


@dataclass(frozen=True, slots=True)
class CamerasConfig:
    discovery: CameraDiscoveryConfig = field(default_factory=CameraDiscoveryConfig)
    acquisition: CameraAcquisitionConfig = field(default_factory=CameraAcquisitionConfig)
    recovery: CameraRecoveryConfig = field(default_factory=CameraRecoveryConfig)
    connection: CameraConnectionConfig = field(default_factory=CameraConnectionConfig)
    startup: CameraStartupConfig = field(default_factory=CameraStartupConfig)
    mapping: list[CameraMappingConfig] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SystemConfig:
    max_cameras: int = 8
    auto_save_config: bool = True
    shutdown_timeout_s: float = 5.0

    def __post_init__(self) -> None:
        if self.max_cameras < 1:
            raise ValueError(f"system.max_cameras must be >= 1; got {self.max_cameras}")
        if self.shutdown_timeout_s <= 0:
            raise ValueError(f"system.shutdown_timeout_s must be > 0; got {self.shutdown_timeout_s}")


@dataclass(frozen=True, slots=True)
class StorageConfig:
    root: str = "data"
    recordings: str = "recordings"
    snapshots: str = "snapshots"
    logs: str = "logs"
    calibration: str = "calibration"
    exports: str = "exports"
    database_backups: str = "database"
    offline: str = "offline"

    def resolve_root(self, app_root: Path) -> Path:
        root = Path(self.root)
        if not root.is_absolute():
            return (app_root / root).resolve()
        return root

    def resolve_subpath(self, app_root: Path, subpath: str) -> Path:
        p = Path(subpath)
        if p.is_absolute():
            return p
        return self.resolve_root(app_root) / subpath


@dataclass(frozen=True, slots=True)
class RecordingConfig:
    enabled: bool = True
    default_fps: float = 9.0
    pre_alarm_seconds: float = 10.0
    post_alarm_seconds: float = 30.0
    max_duration_seconds: float = 300.0
    max_file_size_mb: int = 500
    chunk_target_bytes: int = 67108864
    compression_enabled: bool = False
    naming_pattern: str = "rec_{camera_id}_{timestamp_ms}"

    def __post_init__(self) -> None:
        if self.default_fps <= 0:
            raise ValueError(f"recording.default_fps must be > 0; got {self.default_fps}")
        if self.pre_alarm_seconds < 0:
            raise ValueError(f"recording.pre_alarm_seconds must be >= 0; got {self.pre_alarm_seconds}")
        if self.post_alarm_seconds < 0:
            raise ValueError(f"recording.post_alarm_seconds must be >= 0; got {self.post_alarm_seconds}")
        if self.max_duration_seconds <= 0:
            raise ValueError(f"recording.max_duration_seconds must be > 0; got {self.max_duration_seconds}")
        if self.max_file_size_mb <= 0:
            raise ValueError(f"recording.max_file_size_mb must be > 0; got {self.max_file_size_mb}")
        if self.chunk_target_bytes <= 0:
            raise ValueError(f"recording.chunk_target_bytes must be > 0; got {self.chunk_target_bytes}")


@dataclass(frozen=True, slots=True)
class CalibrationConfig:
    default_file: str = "calibration/calibration_blob.txt"


@dataclass(frozen=True, slots=True)
class ProcessingConfig:
    enabled: bool = True
    interval_ms: int = 100
    default_fps: float = 9.0
    gpu_enabled: bool = False

    def __post_init__(self) -> None:
        if self.interval_ms <= 0:
            raise ValueError(f"processing.interval_ms must be > 0; got {self.interval_ms}")
        if self.default_fps <= 0:
            raise ValueError(f"processing.default_fps must be > 0; got {self.default_fps}")


@dataclass(frozen=True, slots=True)
class AlarmsConfig:
    evaluation_enabled: bool = True
    cooldown_seconds: float = 5.0
    max_history: int = 10000
    default_enabled: bool = True

    def __post_init__(self) -> None:
        if self.cooldown_seconds < 0:
            raise ValueError(f"alarms.cooldown_seconds must be >= 0; got {self.cooldown_seconds}")
        if self.max_history < 0:
            raise ValueError(f"alarms.max_history must be >= 0; got {self.max_history}")


@dataclass(frozen=True, slots=True)
class PTZLimitsConfig:
    min_pan: float = -170.0
    max_pan: float = 170.0
    min_tilt: float = -90.0
    max_tilt: float = 90.0
    min_zoom: float = 1.0
    max_zoom: float = 30.0

    def __post_init__(self) -> None:
        if self.min_pan >= self.max_pan:
            raise ValueError(f"ptz.limits.min_pan ({self.min_pan}) must be < max_pan ({self.max_pan})")
        if self.min_tilt >= self.max_tilt:
            raise ValueError(f"ptz.limits.min_tilt ({self.min_tilt}) must be < max_tilt ({self.max_tilt})")
        if self.min_zoom >= self.max_zoom:
            raise ValueError(f"ptz.limits.min_zoom ({self.min_zoom}) must be < max_zoom ({self.max_zoom})")


@dataclass(frozen=True, slots=True)
class PTZDefaultPositionConfig:
    pan: float = 0.0
    tilt: float = 0.0
    zoom: float = 1.0


@dataclass(frozen=True, slots=True)
class PTZSpeedsConfig:
    pan: float = 10.0
    tilt: float = 10.0
    zoom: float = 5.0


@dataclass(frozen=True, slots=True)
class PTZConfig:
    enabled: bool = True
    limits: PTZLimitsConfig = field(default_factory=PTZLimitsConfig)
    default_position: PTZDefaultPositionConfig = field(default_factory=PTZDefaultPositionConfig)
    speeds: PTZSpeedsConfig = field(default_factory=PTZSpeedsConfig)


@dataclass(frozen=True, slots=True)
class ROIDefaultsConfig:
    RECTANGLE1: dict = field(default_factory=lambda: {"y1": 0.0, "x1": 0.0, "y2": 100.0, "x2": 100.0})
    RECTANGLE2: dict = field(default_factory=lambda: {"center_y": 0.0, "center_x": 0.0, "phi": 0.0, "length1": 50.0, "length2": 50.0})
    CIRCLE: dict = field(default_factory=lambda: {"center_y": 0.0, "center_x": 0.0, "radius": 50.0})
    ELLIPSE: dict = field(default_factory=lambda: {"center_y": 0.0, "center_x": 0.0, "phi": 0.0, "radius1": 50.0, "radius2": 30.0})
    POLYGON: dict = field(default_factory=lambda: {"points": [[0.0, 0.0], [100.0, 0.0], [50.0, 100.0]]})


@dataclass(frozen=True, slots=True)
class ROIConfig:
    defaults: ROIDefaultsConfig = field(default_factory=ROIDefaultsConfig)


@dataclass(frozen=True, slots=True)
class OfflinePlaybackConfig:
    default_speed: float = 1.0
    speed_min: float = 0.1
    speed_max: float = 10.0

    def __post_init__(self) -> None:
        if self.speed_min <= 0:
            raise ValueError(f"offline.playback.speed_min must be > 0; got {self.speed_min}")
        if self.speed_max <= self.speed_min:
            raise ValueError(f"offline.playback.speed_max ({self.speed_max}) must be > speed_min ({self.speed_min})")


@dataclass(frozen=True, slots=True)
class OfflineConfig:
    playback: OfflinePlaybackConfig = field(default_factory=OfflinePlaybackConfig)
    storage_path: str = ""


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str = "INFO"
    file_path: str = ""
    format: str = "[%(asctime)s] [%(levelname)s] %(message)s"
    date_format: str = "%Y-%m-%d %H:%M:%S"
    max_size_mb: int = 20
    backup_count: int = 5

    def __post_init__(self) -> None:
        valid_levels = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
        if self.level not in valid_levels:
            raise ValueError(f"logging.level must be one of {valid_levels}; got {self.level!r}")
        if self.max_size_mb <= 0:
            raise ValueError(f"logging.max_size_mb must be > 0; got {self.max_size_mb}")
        if self.backup_count < 0:
            raise ValueError(f"logging.backup_count must be >= 0; got {self.backup_count}")


@dataclass(frozen=True, slots=True)
class UIColorsConfig:
    primary: str = "#2E7D32"
    primary_hover: str = "#388E3C"
    primary_pressed: str = "#1B5E20"
    primary_disabled_bg: str = "#A5D6A7"
    primary_disabled_text: str = "#E8F5E9"
    secondary: str = "#1976D2"
    secondary_hover: str = "#1E88E5"
    secondary_pressed: str = "#0D47A1"
    secondary_disabled_bg: str = "#90CAF9"
    secondary_disabled_text: str = "#E3F2FD"
    accent: str = "#7B1FA2"
    accent_hover: str = "#8E24AA"
    accent_pressed: str = "#4A148C"
    warning: str = "#FFA000"
    danger: str = "#D32F2F"
    disabled: str = "#757575"
    text_primary: str = "#212121"
    text_secondary: str = "#888888"
    text_muted: str = "#666666"
    title: str = "#2196F3"
    background: str = "#FFFFFF"
    panel: str = "#F5F5F5"
    border: str = "#E0E0E0"
    alarm: str = "#D32F2F"
    success: str = "#2E7D32"
    info: str = "#1976D2"


@dataclass(frozen=True, slots=True)
class UIWindowsConfig:
    start_maximized: bool = True
    launcher_min_width: int = 1000
    launcher_min_height: int = 700
    live_min_width: int = 640
    live_min_height: int = 480
    config_min_width: int = 1000
    config_min_height: int = 700
    config_image_min_width: int = 480
    config_image_min_height: int = 360
    offline_min_width: int = 640
    offline_min_height: int = 480
    observer_image_min_width: int = 320
    observer_image_min_height: int = 240
    observer_image_small_width: int = 240
    observer_image_small_height: int = 180


@dataclass(frozen=True, slots=True)
class UILiveConfig:
    tile_gap: int = 0
    columns: int = 4
    rows: int = 2
    show_camera_name: bool = True
    show_serial: bool = True
    show_fps: bool = True
    show_temperature: bool = True


@dataclass(frozen=True, slots=True)
class UIDisplayConfig:
    default_palette: str = "temperature"
    default_zoom: str = "Fit to Window"
    auto_range: bool = True
    min_temperature: float = -20.0
    max_temperature: float = 1200.0


@dataclass(frozen=True, slots=True)
class UIConfig:
    theme: str = "light"
    colors: UIColorsConfig = field(default_factory=UIColorsConfig)
    windows: UIWindowsConfig = field(default_factory=UIWindowsConfig)
    live: UILiveConfig = field(default_factory=UILiveConfig)
    display: UIDisplayConfig = field(default_factory=UIDisplayConfig)

    def __post_init__(self) -> None:
        if self.theme not in ("light", "dark", "system"):
            raise ValueError(f"ui.theme must be 'light', 'dark', or 'system'; got {self.theme!r}")


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    bind_address: str = "0.0.0.0"
    http_port: int = 8080

    def __post_init__(self) -> None:
        if not 1 <= self.http_port <= 65535:
            raise ValueError(f"network.http_port must be between 1 and 65535; got {self.http_port}")


@dataclass(frozen=True, slots=True)
class HALCONConfig:
    grab_timeout_error_code: int = 5322
    first_frame_timeout_ms: int = 5000


@dataclass(frozen=True, slots=True)
class AppConfig:
    application: ApplicationConfig = field(default_factory=ApplicationConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    cameras: CamerasConfig = field(default_factory=CamerasConfig)
    system: SystemConfig = field(default_factory=SystemConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    alarms: AlarmsConfig = field(default_factory=AlarmsConfig)
    ptz: PTZConfig = field(default_factory=PTZConfig)
    roi: ROIConfig = field(default_factory=ROIConfig)
    offline: OfflineConfig = field(default_factory=OfflineConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    halcon: HALCONConfig = field(default_factory=HALCONConfig)