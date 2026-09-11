"""
Configuration manager for TMS V3.

Loads, validates, and provides access to application configuration.
Supports YAML configuration files, environment variable overrides,
and path resolution for deployment scenarios.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml

from thermal_monitor.config.models import (
    AppConfig,
    ApplicationConfig,
    DatabaseConfig,
    CameraMappingConfig,
    CamerasConfig,
    CameraAcquisitionConfig,
    CameraRecoveryConfig,
    CameraConnectionConfig,
    CameraDiscoveryConfig,
    CameraStartupConfig,
    CalibrationConfig,
    AlarmsConfig,
    LoggingConfig,
    NetworkConfig,
    OfflineConfig,
    OfflinePlaybackConfig,
    ProcessingConfig,
    PTZConfig,
    PTZLimitsConfig,
    PTZDefaultPositionConfig,
    PTZSpeedsConfig,
    RecordingConfig,
    ROIConfig,
    ROIDefaultsConfig,
    StorageConfig,
    SystemConfig,
    UIConfig,
    UIColorsConfig,
    UIDisplayConfig,
    UILiveConfig,
    UIWindowsConfig,
)


class ConfigurationError(Exception):
    """Raised when configuration is invalid or cannot be loaded."""

    pass


def _get_env(key: str, default: Any = None) -> Any:
    """Get environment variable with optional default."""
    value = os.environ.get(key)
    if value is None:
        return default
    return value


def _parse_bool(value: Any) -> bool:
    """Parse string to boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "on")
    return bool(value)


def _parse_int(value: Any, default: int) -> int:
    """Parse value to int."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_float(value: Any, default: float) -> float:
    """Parse value to float."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class ConfigurationManager:
    """
    Central configuration manager for TMS V3.

    Loads configuration from config.yaml, applies environment variable overrides,
    validates configuration, and resolves paths for deployment scenarios.

    Precedence (highest to lowest):
    1. Environment variables (explicit mapping only)
    2. config.yaml
    3. Built-in defaults
    """

    _ENV_PREFIX = "TMS_"

    def __init__(
        self,
        config_path: Path | str | None = None,
        app_root: Path | str | None = None,
        create_default: bool = True,
    ) -> None:
        """
        Initialize configuration manager.

        Args:
            config_path: Explicit path to config.yaml. If None, auto-detect.
            app_root: Application root directory. If None, auto-detect.
            create_default: Whether to create default config.yaml if missing (dev mode).
        """
        self._app_root = self._resolve_app_root(app_root)
        self._config_path = self._resolve_config_path(config_path)
        self._is_dev = self._detect_dev_mode()
        self._config: AppConfig | None = None
        self._load_config(create_default)

    def _resolve_app_root(self, app_root: Path | str | None) -> Path:
        """Resolve the application root directory."""
        if app_root is not None:
            return Path(app_root).resolve()

        # Frozen executable (PyInstaller, cx_Freeze, etc.)
        if getattr(sys, "frozen", False):
            return Path(sys.executable).parent.resolve()

        # Development: assume this file is at src/thermal_monitor/config/manager.py
        return Path(__file__).parent.parent.parent.parent.resolve()

    def _resolve_config_path(self, config_path: Path | str | None) -> Path:
        """Resolve the configuration file path."""
        if config_path is not None:
            return Path(config_path).resolve()

        # Standard location: <app_root>/config/config.yaml
        return (self._app_root / "config" / "config.yaml").resolve()

    def _detect_dev_mode(self) -> bool:
        """Detect if running in development mode."""
        # Check if running from source tree (not frozen)
        if getattr(sys, "frozen", False):
            return False
        # Check if config.yaml exists in standard location
        return (self._app_root / "config" / "config.yaml").exists()

    def _load_config(self, create_default: bool) -> None:
        """Load configuration from file and apply overrides."""
        raw_config = self._load_yaml(create_default)
        raw_config = self._apply_env_overrides(raw_config)
        try:
            self._config = self._build_config(raw_config)
        except ValueError as e:
            raise ConfigurationError(str(e)) from e
        self._validate_config(self._config)

    def _load_yaml(self, create_default: bool) -> dict[str, Any]:
        """Load YAML configuration file."""
        if not self._config_path.exists():
            if create_default and self._is_dev:
                self._create_default_config()
                return self._load_yaml(False)  # Recurse once after creation
            elif not create_default or not self._is_dev:
                # Production: use empty dict, rely on defaults
                return {}
            else:
                raise ConfigurationError(
                    f"Configuration file not found: {self._config_path}. "
                    "Set TMS_CONFIG_PATH or create config/config.yaml"
                )

        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                return data if data is not None else {}
        except yaml.YAMLError as e:
            raise ConfigurationError(f"Invalid YAML in {self._config_path}: {e}") from e

    def _create_default_config(self) -> None:
        """Create a default configuration file for development."""
        default_config = self._get_default_config_dict()
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._config_path, "w", encoding="utf-8") as f:
            yaml.dump(default_config, f, default_flow_style=False, sort_keys=False)

    def _get_default_config_dict(self) -> dict[str, Any]:
        """Get default configuration as dictionary."""
        return {
            "application": {
                "name": "Thermal Monitoring System V3",
                "version": "3.0.0",
                "default_mode": "CONFIGURATION",
                "start_maximized": True,
            },
            "database": {
                "enabled": False,
                "type": "sqlserver",
                "driver": "ODBC Driver 17 for SQL Server",
                "host": "",
                "port": 1433,
                "name": "",
                "trusted_connection": True,
                "username": "",
                "password_env": "TMS_DB_PASSWORD",
                "connection_timeout": 30,
                "command_timeout": 30,
                "trust_server_certificate": True,
            },
            "cameras": {
                "discovery": {
                    "enabled": True,
                    "startup_scan": True,
                    "halcon_interface": "GigEVision2",
                    "attempts": 3,
                    "retry_delay_s": 3.0,
                    "interval_seconds": 30.0,
                    "backend": "gvcp",
                    "static_ips": [],
                },
                "acquisition": {
                    "target_fps": 9,
                    "grab_timeout_ms": 500,
                    "socket_buffer_size": 1048576,
                    "num_buffers": 8,
                    "stream_source_thermal": "IR_Data",
                    "thermal_bits_per_channel": 16,
                    "stream_source_visible": None,
                    "visible_bits_per_channel": -1,
                    "backend": "custom",
                },
                "recovery": {
                    "consecutive_fail_limit": 3,
                    "reconnect_interval_s": 3.0,
                    "reconnect_backoff_factor": 2.0,
                    "max_reconnect_attempts": 10,
                },
                "connection": {
                    "device_identifier": "default",
                    "ip_mode": "auto",
                },
                "startup": {
                    "acquire_timeout_s": 10.0,
                },
                "mapping": [],
            },
            "system": {
                "max_cameras": 8,
                "auto_save_config": True,
                "shutdown_timeout_s": 5.0,
            },
            "storage": {
                "root": "data",
                "recordings": "recordings",
                "snapshots": "snapshots",
                "logs": "logs",
                "calibration": "calibration",
                "exports": "exports",
                "database_backups": "database",
                "offline": "offline",
            },
            "recording": {
                "enabled": True,
                "default_fps": 9.0,
                "pre_alarm_seconds": 10.0,
                "post_alarm_seconds": 30.0,
                "max_duration_seconds": 300.0,
                "max_file_size_mb": 500,
                "chunk_target_bytes": 67108864,
                "compression_enabled": False,
                "naming_pattern": "rec_{camera_id}_{timestamp_ms}",
            },
            "calibration": {
                "default_file": "calibration/calibration_blob.txt",
            },
            "processing": {
                "enabled": True,
                "interval_ms": 100,
                "default_fps": 9.0,
                "gpu_enabled": False,
            },
            "alarms": {
                "evaluation_enabled": True,
                "cooldown_seconds": 5.0,
                "max_history": 10000,
                "default_enabled": True,
            },
            "ptz": {
                "enabled": True,
                "limits": {
                    "min_pan": -170.0,
                    "max_pan": 170.0,
                    "min_tilt": -90.0,
                    "max_tilt": 90.0,
                    "min_zoom": 1.0,
                    "max_zoom": 30.0,
                },
                "default_position": {
                    "pan": 0.0,
                    "tilt": 0.0,
                    "zoom": 1.0,
                },
                "speeds": {
                    "pan": 10.0,
                    "tilt": 10.0,
                    "zoom": 5.0,
                },
            },
            "roi": {
                "defaults": {
                    "RECTANGLE1": {"y1": 0.0, "x1": 0.0, "y2": 100.0, "x2": 100.0},
                    "RECTANGLE2": {"center_y": 0.0, "center_x": 0.0, "phi": 0.0, "length1": 50.0, "length2": 50.0},
                    "CIRCLE": {"center_y": 0.0, "center_x": 0.0, "radius": 50.0},
                    "ELLIPSE": {"center_y": 0.0, "center_x": 0.0, "phi": 0.0, "radius1": 50.0, "radius2": 30.0},
                    "POLYGON": {"points": [[0.0, 0.0], [100.0, 0.0], [50.0, 100.0]]},
                }
            },
            "offline": {
                "playback": {
                    "default_speed": 1.0,
                    "speed_min": 0.1,
                    "speed_max": 10.0,
                },
                "storage_path": "",
            },
            "logging": {
                "level": "INFO",
                "file_path": "",
                "format": "[%(asctime)s] [%(levelname)s] %(message)s",
                "date_format": "%Y-%m-%d %H:%M:%S",
                "max_size_mb": 20,
                "backup_count": 5,
            },
            "ui": {
                "theme": "industrial_dark",
                "colors": {
                    "primary": "#2E7D32",
                    "primary_hover": "#388E3C",
                    "primary_pressed": "#1B5E20",
                    "primary_disabled_bg": "#A5D6A7",
                    "primary_disabled_text": "#E8F5E9",
                    "secondary": "#1976D2",
                    "secondary_hover": "#1E88E5",
                    "secondary_pressed": "#0D47A1",
                    "secondary_disabled_bg": "#90CAF9",
                    "secondary_disabled_text": "#E3F2FD",
                    "accent": "#7B1FA2",
                    "accent_hover": "#8E24AA",
                    "accent_pressed": "#4A148C",
                    "warning": "#FFA000",
                    "danger": "#D32F2F",
                    "disabled": "#757575",
                    "text_primary": "#212121",
                    "text_secondary": "#888888",
                    "text_muted": "#666666",
                    "title": "#2196F3",
                    "background": "#FFFFFF",
                    "panel": "#F5F5F5",
                    "border": "#E0E0E0",
                    "alarm": "#D32F2F",
                    "success": "#2E7D32",
                    "info": "#1976D2",
                },
                "windows": {
                    "start_maximized": True,
                    "launcher_min_width": 1000,
                    "launcher_min_height": 700,
                    "live_min_width": 640,
                    "live_min_height": 480,
                    "config_min_width": 1000,
                    "config_min_height": 700,
                    "config_image_min_width": 480,
                    "config_image_min_height": 360,
                    "offline_min_width": 640,
                    "offline_min_height": 480,
                    "observer_image_min_width": 320,
                    "observer_image_min_height": 240,
                    "observer_image_small_width": 240,
                    "observer_image_small_height": 180,
                },
                "live": {
                    "tile_gap": 0,
                    "columns": 4,
                    "rows": 2,
                    "show_camera_name": True,
                    "show_serial": True,
                    "show_fps": True,
                    "show_temperature": True,
                },
                "display": {
                    "default_palette": "temperature",
                    "default_zoom": "Fit to Window",
                    "auto_range": True,
                    "min_temperature": -20.0,
                    "max_temperature": 1200.0,
                },
            },
            "network": {
                "bind_address": "0.0.0.0",
                "http_port": 8080,
            },
        }

    def _apply_env_overrides(self, config: dict[str, Any]) -> dict[str, Any]:
        """Apply environment variable overrides to configuration."""
        # Database password from env
        db_password = _get_env("TMS_DB_PASSWORD")
        if db_password is not None:
            config.setdefault("database", {})["password_env"] = "TMS_DB_PASSWORD"
            # Store the actual password in a special key for resolution later
            config["database"]["_env_password"] = db_password

        # Log level from env
        log_level = _get_env("TMS_LOG_LEVEL")
        if log_level is not None:
            config.setdefault("logging", {})["level"] = log_level

        # Storage root from env
        storage_root = _get_env("TMS_STORAGE_ROOT")
        if storage_root is not None:
            config.setdefault("storage", {})["root"] = storage_root

        # Config file path from env
        config_path = _get_env("TMS_CONFIG_PATH")
        if config_path is not None:
            self._config_path = Path(config_path).resolve()

        return config

    def _build_config(self, raw: dict[str, Any]) -> AppConfig:
        """Build typed configuration from raw dictionary."""
        # Application
        app_raw = raw.get("application", {})
        application = ApplicationConfig(
            name=app_raw.get("name", "Thermal Monitoring System V3"),
            version=app_raw.get("version", "3.0.0"),
            default_mode=app_raw.get("default_mode", "CONFIGURATION"),
            start_maximized=app_raw.get("start_maximized", True),
        )

        # Database
        db_raw = raw.get("database", {})
        database = DatabaseConfig(
            enabled=db_raw.get("enabled", False),
            type=db_raw.get("type", "sqlserver"),
            driver=db_raw.get("driver", "ODBC Driver 17 for SQL Server"),
            host=db_raw.get("host", ""),
            port=_parse_int(db_raw.get("port"), 1433),
            name=db_raw.get("name", ""),
            trusted_connection=db_raw.get("trusted_connection", True),
            username=db_raw.get("username", ""),
            password_env=db_raw.get("password_env", "TMS_DB_PASSWORD"),
            connection_timeout=_parse_int(db_raw.get("connection_timeout"), 30),
            command_timeout=_parse_int(db_raw.get("command_timeout"), 30),
            trust_server_certificate=db_raw.get("trust_server_certificate", True),
        )

        # Cameras
        cam_raw = raw.get("cameras", {})

        disc_raw = cam_raw.get("discovery", {})
        static_ips = disc_raw.get("static_ips", [])
        if not isinstance(static_ips, list):
            raise ConfigurationError("cameras.discovery.static_ips must be a list of IP strings")
        discovery = CameraDiscoveryConfig(
            enabled=disc_raw.get("enabled", True),
            startup_scan=disc_raw.get("startup_scan", True),
            halcon_interface=disc_raw.get("halcon_interface", "GigEVision2"),
            attempts=_parse_int(disc_raw.get("attempts"), 3),
            retry_delay_s=_parse_float(disc_raw.get("retry_delay_s"), 3.0),
            interval_seconds=_parse_float(disc_raw.get("interval_seconds"), 30.0),
            backend=str(disc_raw.get("backend", "gvcp")),
            static_ips=[str(ip) for ip in static_ips],
        )

        acq_raw = cam_raw.get("acquisition", {})
        acquisition = CameraAcquisitionConfig(
            target_fps=_parse_int(acq_raw.get("target_fps"), 9),
            grab_timeout_ms=_parse_int(acq_raw.get("grab_timeout_ms"), 500),
            socket_buffer_size=_parse_int(acq_raw.get("socket_buffer_size"), 1048576),
            num_buffers=_parse_int(acq_raw.get("num_buffers"), 8),
            stream_source_thermal=acq_raw.get("stream_source_thermal", "IR_Data"),
            thermal_bits_per_channel=_parse_int(acq_raw.get("thermal_bits_per_channel"), 16),
            stream_source_visible=acq_raw.get("stream_source_visible"),
            visible_bits_per_channel=_parse_int(acq_raw.get("visible_bits_per_channel"), -1),
            backend=str(acq_raw.get("backend", "custom")),
        )

        rec_raw = cam_raw.get("recovery", {})
        recovery = CameraRecoveryConfig(
            consecutive_fail_limit=_parse_int(rec_raw.get("consecutive_fail_limit"), 3),
            reconnect_interval_s=_parse_float(rec_raw.get("reconnect_interval_s"), 3.0),
            reconnect_backoff_factor=_parse_float(rec_raw.get("reconnect_backoff_factor"), 2.0),
            max_reconnect_attempts=_parse_int(rec_raw.get("max_reconnect_attempts"), 10),
        )

        conn_raw = cam_raw.get("connection", {})
        connection = CameraConnectionConfig(
            device_identifier=conn_raw.get("device_identifier", "default"),
            ip_mode=conn_raw.get("ip_mode", "auto"),
        )

        start_raw = cam_raw.get("startup", {})
        startup = CameraStartupConfig(
            acquire_timeout_s=_parse_float(start_raw.get("acquire_timeout_s"), 10.0),
        )

        mapping_raw = cam_raw.get("mapping", [])
        mapping = []
        for m in mapping_raw:
            if isinstance(m, dict):
                mapping.append(CameraMappingConfig(
                    camera_id=m.get("camera_id", ""),
                    serial_number=m.get("serial_number", ""),
                    enabled=m.get("enabled", True),
                    name=m.get("name", ""),
                    target_fps=m.get("target_fps"),
                ))

        cameras = CamerasConfig(
            discovery=discovery,
            acquisition=acquisition,
            recovery=recovery,
            connection=connection,
            startup=startup,
            mapping=mapping,
        )

        # System
        sys_raw = raw.get("system", {})
        system = SystemConfig(
            max_cameras=_parse_int(sys_raw.get("max_cameras"), 8),
            auto_save_config=sys_raw.get("auto_save_config", True),
            shutdown_timeout_s=_parse_float(sys_raw.get("shutdown_timeout_s"), 5.0),
        )

        # Storage
        storage_raw = raw.get("storage", {})
        storage = StorageConfig(
            root=storage_raw.get("root", "data"),
            recordings=storage_raw.get("recordings", "recordings"),
            snapshots=storage_raw.get("snapshots", "snapshots"),
            logs=storage_raw.get("logs", "logs"),
            calibration=storage_raw.get("calibration", "calibration"),
            exports=storage_raw.get("exports", "exports"),
            database_backups=storage_raw.get("database_backups", "database"),
            offline=storage_raw.get("offline", "offline"),
        )

        # Recording
        rec_raw = raw.get("recording", {})
        recording = RecordingConfig(
            enabled=rec_raw.get("enabled", True),
            default_fps=_parse_float(rec_raw.get("default_fps"), 9.0),
            pre_alarm_seconds=_parse_float(rec_raw.get("pre_alarm_seconds"), 10.0),
            post_alarm_seconds=_parse_float(rec_raw.get("post_alarm_seconds"), 30.0),
            max_duration_seconds=_parse_float(rec_raw.get("max_duration_seconds"), 300.0),
            max_file_size_mb=_parse_int(rec_raw.get("max_file_size_mb"), 500),
            chunk_target_bytes=_parse_int(rec_raw.get("chunk_target_bytes"), 67108864),
            compression_enabled=rec_raw.get("compression_enabled", False),
            naming_pattern=rec_raw.get("naming_pattern", "rec_{camera_id}_{timestamp_ms}"),
        )

        # Calibration
        cal_raw = raw.get("calibration", {})
        calibration = CalibrationConfig(
            default_file=cal_raw.get("default_file", "calibration/calibration_blob.txt"),
        )

        # Processing
        proc_raw = raw.get("processing", {})
        processing = ProcessingConfig(
            enabled=proc_raw.get("enabled", True),
            interval_ms=_parse_int(proc_raw.get("interval_ms"), 100),
            default_fps=_parse_float(proc_raw.get("default_fps"), 9.0),
            gpu_enabled=proc_raw.get("gpu_enabled", False),
        )

        # Alarms
        alarm_raw = raw.get("alarms", {})
        alarms = AlarmsConfig(
            evaluation_enabled=alarm_raw.get("evaluation_enabled", True),
            cooldown_seconds=_parse_float(alarm_raw.get("cooldown_seconds"), 5.0),
            max_history=_parse_int(alarm_raw.get("max_history"), 10000),
            default_enabled=alarm_raw.get("default_enabled", True),
        )

        # PTZ
        ptz_raw = raw.get("ptz", {})
        limits_raw = ptz_raw.get("limits", {})
        limits = PTZLimitsConfig(
            min_pan=_parse_float(limits_raw.get("min_pan"), -170.0),
            max_pan=_parse_float(limits_raw.get("max_pan"), 170.0),
            min_tilt=_parse_float(limits_raw.get("min_tilt"), -90.0),
            max_tilt=_parse_float(limits_raw.get("max_tilt"), 90.0),
            min_zoom=_parse_float(limits_raw.get("min_zoom"), 1.0),
            max_zoom=_parse_float(limits_raw.get("max_zoom"), 30.0),
        )
        default_pos_raw = ptz_raw.get("default_position", {})
        default_position = PTZDefaultPositionConfig(
            pan=_parse_float(default_pos_raw.get("pan"), 0.0),
            tilt=_parse_float(default_pos_raw.get("tilt"), 0.0),
            zoom=_parse_float(default_pos_raw.get("zoom"), 1.0),
        )
        speeds_raw = ptz_raw.get("speeds", {})
        speeds = PTZSpeedsConfig(
            pan=_parse_float(speeds_raw.get("pan"), 10.0),
            tilt=_parse_float(speeds_raw.get("tilt"), 10.0),
            zoom=_parse_float(speeds_raw.get("zoom"), 5.0),
        )
        ptz = PTZConfig(
            enabled=ptz_raw.get("enabled", True),
            limits=limits,
            default_position=default_position,
            speeds=speeds,
        )

        # ROI
        roi_raw = raw.get("roi", {})
        defaults_raw = roi_raw.get("defaults", {})
        defaults = ROIDefaultsConfig(
            RECTANGLE1=defaults_raw.get("RECTANGLE1", {"y1": 0.0, "x1": 0.0, "y2": 100.0, "x2": 100.0}),
            RECTANGLE2=defaults_raw.get("RECTANGLE2", {"center_y": 0.0, "center_x": 0.0, "phi": 0.0, "length1": 50.0, "length2": 50.0}),
            CIRCLE=defaults_raw.get("CIRCLE", {"center_y": 0.0, "center_x": 0.0, "radius": 50.0}),
            ELLIPSE=defaults_raw.get("ELLIPSE", {"center_y": 0.0, "center_x": 0.0, "phi": 0.0, "radius1": 50.0, "radius2": 30.0}),
            POLYGON=defaults_raw.get("POLYGON", {"points": [[0.0, 0.0], [100.0, 0.0], [50.0, 100.0]]}),
        )
        roi = ROIConfig(defaults=defaults)

        # Offline
        off_raw = raw.get("offline", {})
        playback_raw = off_raw.get("playback", {})
        playback = OfflinePlaybackConfig(
            default_speed=_parse_float(playback_raw.get("default_speed"), 1.0),
            speed_min=_parse_float(playback_raw.get("speed_min"), 0.1),
            speed_max=_parse_float(playback_raw.get("speed_max"), 10.0),
        )
        offline = OfflineConfig(
            playback=playback,
            storage_path=off_raw.get("storage_path", ""),
        )

        # Logging
        log_raw = raw.get("logging", {})
        logging = LoggingConfig(
            level=log_raw.get("level", "INFO"),
            file_path=log_raw.get("file_path", ""),
            format=log_raw.get("format", "[%(asctime)s] [%(levelname)s] %(message)s"),
            date_format=log_raw.get("date_format", "%Y-%m-%d %H:%M:%S"),
            max_size_mb=_parse_int(log_raw.get("max_size_mb"), 20),
            backup_count=_parse_int(log_raw.get("backup_count"), 5),
        )

        # UI
        ui_raw = raw.get("ui", {})
        colors_raw = ui_raw.get("colors", {})
        colors = UIColorsConfig(
            primary=colors_raw.get("primary", "#2E7D32"),
            primary_hover=colors_raw.get("primary_hover", "#388E3C"),
            primary_pressed=colors_raw.get("primary_pressed", "#1B5E20"),
            primary_disabled_bg=colors_raw.get("primary_disabled_bg", "#A5D6A7"),
            primary_disabled_text=colors_raw.get("primary_disabled_text", "#E8F5E9"),
            secondary=colors_raw.get("secondary", "#1976D2"),
            secondary_hover=colors_raw.get("secondary_hover", "#1E88E5"),
            secondary_pressed=colors_raw.get("secondary_pressed", "#0D47A1"),
            secondary_disabled_bg=colors_raw.get("secondary_disabled_bg", "#90CAF9"),
            secondary_disabled_text=colors_raw.get("secondary_disabled_text", "#E3F2FD"),
            accent=colors_raw.get("accent", "#7B1FA2"),
            accent_hover=colors_raw.get("accent_hover", "#8E24AA"),
            accent_pressed=colors_raw.get("accent_pressed", "#4A148C"),
            warning=colors_raw.get("warning", "#FFA000"),
            danger=colors_raw.get("danger", "#D32F2F"),
            disabled=colors_raw.get("disabled", "#757575"),
            text_primary=colors_raw.get("text_primary", "#212121"),
            text_secondary=colors_raw.get("text_secondary", "#888888"),
            text_muted=colors_raw.get("text_muted", "#666666"),
            title=colors_raw.get("title", "#2196F3"),
            background=colors_raw.get("background", "#FFFFFF"),
            panel=colors_raw.get("panel", "#F5F5F5"),
            border=colors_raw.get("border", "#E0E0E0"),
            alarm=colors_raw.get("alarm", "#D32F2F"),
            success=colors_raw.get("success", "#2E7D32"),
            info=colors_raw.get("info", "#1976D2"),
        )
        windows_raw = ui_raw.get("windows", {})
        windows = UIWindowsConfig(
            start_maximized=windows_raw.get("start_maximized", True),
            launcher_min_width=_parse_int(windows_raw.get("launcher_min_width"), 1000),
            launcher_min_height=_parse_int(windows_raw.get("launcher_min_height"), 700),
            live_min_width=_parse_int(windows_raw.get("live_min_width"), 640),
            live_min_height=_parse_int(windows_raw.get("live_min_height"), 480),
            config_min_width=_parse_int(windows_raw.get("config_min_width"), 1000),
            config_min_height=_parse_int(windows_raw.get("config_min_height"), 700),
            config_image_min_width=_parse_int(windows_raw.get("config_image_min_width"), 480),
            config_image_min_height=_parse_int(windows_raw.get("config_image_min_height"), 360),
            offline_min_width=_parse_int(windows_raw.get("offline_min_width"), 640),
            offline_min_height=_parse_int(windows_raw.get("offline_min_height"), 480),
            observer_image_min_width=_parse_int(windows_raw.get("observer_image_min_width"), 320),
            observer_image_min_height=_parse_int(windows_raw.get("observer_image_min_height"), 240),
            observer_image_small_width=_parse_int(windows_raw.get("observer_image_small_width"), 240),
            observer_image_small_height=_parse_int(windows_raw.get("observer_image_small_height"), 180),
        )
        live_raw = ui_raw.get("live", {})
        live = UILiveConfig(
            tile_gap=_parse_int(live_raw.get("tile_gap"), 0),
            columns=_parse_int(live_raw.get("columns"), 4),
            rows=_parse_int(live_raw.get("rows"), 2),
            show_camera_name=live_raw.get("show_camera_name", True),
            show_serial=live_raw.get("show_serial", True),
            show_fps=live_raw.get("show_fps", True),
            show_temperature=live_raw.get("show_temperature", True),
        )
        display_raw = ui_raw.get("display", {})
        display = UIDisplayConfig(
            default_palette=display_raw.get("default_palette", "temperature"),
            default_zoom=display_raw.get("default_zoom", "Fit to Window"),
            auto_range=display_raw.get("auto_range", True),
            min_temperature=_parse_float(display_raw.get("min_temperature"), -20.0),
            max_temperature=_parse_float(display_raw.get("max_temperature"), 1200.0),
        )
        ui = UIConfig(
            theme=ui_raw.get("theme", "industrial_dark"),
            colors=colors,
            windows=windows,
            live=live,
            display=display,
        )

        # Network
        net_raw = raw.get("network", {})
        network = NetworkConfig(
            bind_address=net_raw.get("bind_address", "0.0.0.0"),
            http_port=_parse_int(net_raw.get("http_port"), 8080),
        )

        return AppConfig(
            application=application,
            database=database,
            cameras=cameras,
            system=system,
            storage=storage,
            recording=recording,
            calibration=calibration,
            processing=processing,
            alarms=alarms,
            ptz=ptz,
            roi=roi,
            offline=offline,
            logging=logging,
            ui=ui,
            network=network,
        )

    def _validate_config(self, config: AppConfig) -> None:
        """Validate the complete configuration."""
        # Validation is done in __post_init__ of each model
        # Additional cross-section validation can go here
        pass

    def get_config(self) -> AppConfig:
        """Get the complete validated configuration."""
        if self._config is None:
            raise ConfigurationError("Configuration not loaded")
        return self._config

    def get_database_password(self) -> str | None:
        """Get database password from environment variable."""
        env_name = self._config.database.password_env if self._config else "TMS_DB_PASSWORD"
        return _get_env(env_name)

    def resolve_storage_root(self) -> Path:
        """Resolve the storage root path."""
        if self._config is None:
            raise ConfigurationError("Configuration not loaded")
        return self._config.storage.resolve_root(self._app_root)

    def resolve_storage_path(self, subpath: str) -> Path:
        """Resolve a storage subpath."""
        if self._config is None:
            raise ConfigurationError("Configuration not loaded")
        return self._config.storage.resolve_subpath(self._app_root, subpath)

    def resolve_calibration_path(self) -> Path:
        """Resolve the calibration file path."""
        if self._config is None:
            raise ConfigurationError("Configuration not loaded")
        return self.resolve_storage_path(self._config.calibration.default_file)

    def resolve_log_path(self) -> Path | None:
        """Resolve the log file path."""
        if self._config is None:
            raise ConfigurationError("Configuration not loaded")
        log_path = self._config.logging.file_path
        if not log_path:
            return None
        return self.resolve_storage_path(log_path)

    @property
    def config_path(self) -> Path:
        """Get the configuration file path."""
        return self._config_path

    @property
    def app_root(self) -> Path:
        """Get the application root directory."""
        return self._app_root

    @property
    def is_dev(self) -> bool:
        """Check if running in development mode."""
        return self._is_dev


def create_config_manager(
    config_path: Path | str | None = None,
    app_root: Path | str | None = None,
    create_default: bool = True,
) -> ConfigurationManager:
    """Factory function to create a ConfigurationManager."""
    return ConfigurationManager(config_path, app_root, create_default)