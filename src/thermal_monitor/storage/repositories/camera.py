"""
storage.repositories.camera -- Camera repository implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from thermal_monitor.core.models import CameraConfig, CameraIdentity, PTZConfig, PTZLimits, PTZPosition
from thermal_monitor.storage.database import Database
from thermal_monitor.storage.repositories.base import BaseRepository, RepositoryResult


@dataclass
class CameraRow:
    """Database row for camera table."""

    id: int
    camera_id: str
    serial_number: str
    model: str
    vendor: str
    firmware: str
    user_name: str
    name: str
    description: str
    enabled: bool
    thermal_enabled: bool
    visible_enabled: bool
    device_identifier: str
    ip_address: str
    frame_rate: int
    grab_timeout_ms: int
    socket_buffer_size: int
    num_buffers: int
    stream_source_thermal: str
    thermal_bits_per_channel: int
    stream_source_visible: str | None
    visible_bits_per_channel: int
    consecutive_fail_limit: int
    reconnect_interval_s: float
    reconnect_backoff_factor: float
    max_reconnect_attempts: int
    # PTZ fields
    ptz_min_pan: float
    ptz_max_pan: float
    ptz_min_tilt: float
    ptz_max_tilt: float
    ptz_min_zoom: float
    ptz_max_zoom: float
    ptz_default_pan: float
    ptz_default_tilt: float
    ptz_default_zoom: float
    ptz_mode: str
    ptz_speed_pan: float
    ptz_speed_tilt: float
    ptz_speed_zoom: float


class CameraRepository(BaseRepository[CameraConfig]):
    """Repository for camera configurations."""

    def __init__(self, database: Database) -> None:
        super().__init__(database, "cameras")

    def _get_columns(self) -> list[str]:
        return [
            "camera_id", "serial_number", "model", "vendor", "firmware", "user_name",
            "name", "description", "enabled",
            "thermal_enabled", "visible_enabled",
            "device_identifier", "ip_address",
            "frame_rate", "grab_timeout_ms", "socket_buffer_size", "num_buffers",
            "stream_source_thermal", "thermal_bits_per_channel",
            "stream_source_visible", "visible_bits_per_channel",
            "consecutive_fail_limit", "reconnect_interval_s",
            "reconnect_backoff_factor", "max_reconnect_attempts",
            "ptz_min_pan", "ptz_max_pan", "ptz_min_tilt", "ptz_max_tilt",
            "ptz_min_zoom", "ptz_max_zoom",
            "ptz_default_pan", "ptz_default_tilt", "ptz_default_zoom",
            "ptz_mode", "ptz_speed_pan", "ptz_speed_tilt", "ptz_speed_zoom",
        ]

    def _to_entity(self, row: tuple) -> CameraConfig:
        """Convert database row to CameraConfig."""
        r = CameraRow(*row)

        identity = CameraIdentity(
            camera_id=r.camera_id,
            serial_number=r.serial_number,
            model=r.model,
            vendor=r.vendor,
            firmware=r.firmware,
            user_name=r.user_name,
        )

        ptz_limits = PTZLimits(
            min_pan=r.ptz_min_pan,
            max_pan=r.ptz_max_pan,
            min_tilt=r.ptz_min_tilt,
            max_tilt=r.ptz_max_tilt,
            min_zoom=r.ptz_min_zoom,
            max_zoom=r.ptz_max_zoom,
        )

        ptz_config = PTZConfig(
            limits=ptz_limits,
            default_position=PTZPosition(
                pan=r.ptz_default_pan,
                tilt=r.ptz_default_tilt,
                zoom=r.ptz_default_zoom,
            ),
            mode=r.ptz_mode,
            speed_pan=r.ptz_speed_pan,
            speed_tilt=r.ptz_speed_tilt,
            speed_zoom=r.ptz_speed_zoom,
        )

        return CameraConfig(
            identity=identity,
            name=r.name,
            description=r.description,
            enabled=r.enabled,
            thermal_enabled=r.thermal_enabled,
            visible_enabled=r.visible_enabled,
            device_identifier=r.device_identifier,
            ip_address=r.ip_address,
            frame_rate=r.frame_rate,
            grab_timeout_ms=r.grab_timeout_ms,
            socket_buffer_size=r.socket_buffer_size,
            num_buffers=r.num_buffers,
            stream_source_thermal=r.stream_source_thermal,
            thermal_bits_per_channel=r.thermal_bits_per_channel,
            stream_source_visible=r.stream_source_visible,
            visible_bits_per_channel=r.visible_bits_per_channel,
            consecutive_fail_limit=r.consecutive_fail_limit,
            reconnect_interval_s=r.reconnect_interval_s,
            reconnect_backoff_factor=r.reconnect_backoff_factor,
            max_reconnect_attempts=r.max_reconnect_attempts,
            ptz_config=ptz_config,
        )

    def _to_params(self, entity: CameraConfig) -> tuple:
        """Convert CameraConfig to SQL parameters."""
        return (
            entity.identity.camera_id,
            entity.identity.serial_number,
            entity.identity.model,
            entity.identity.vendor,
            entity.identity.firmware,
            entity.identity.user_name,
            entity.name,
            entity.description,
            entity.enabled,
            entity.thermal_enabled,
            entity.visible_enabled,
            entity.device_identifier,
            entity.ip_address,
            entity.frame_rate if hasattr(entity, 'frame_rate') else 9,
            entity.grab_timeout_ms if hasattr(entity, 'grab_timeout_ms') else 500,
            entity.socket_buffer_size if hasattr(entity, 'socket_buffer_size') else 1048576,
            entity.num_buffers if hasattr(entity, 'num_buffers') else 8,
            entity.stream_source_thermal if hasattr(entity, 'stream_source_thermal') else "IR_Data",
            entity.thermal_bits_per_channel if hasattr(entity, 'thermal_bits_per_channel') else 16,
            entity.stream_source_visible if hasattr(entity, 'stream_source_visible') else None,
            entity.visible_bits_per_channel if hasattr(entity, 'visible_bits_per_channel') else -1,
            entity.consecutive_fail_limit if hasattr(entity, 'consecutive_fail_limit') else 3,
            entity.reconnect_interval_s if hasattr(entity, 'reconnect_interval_s') else 3.0,
            entity.reconnect_backoff_factor if hasattr(entity, 'reconnect_backoff_factor') else 2.0,
            entity.max_reconnect_attempts if hasattr(entity, 'max_reconnect_attempts') else 10,
            entity.ptz_config.limits.min_pan,
            entity.ptz_config.limits.max_pan,
            entity.ptz_config.limits.min_tilt,
            entity.ptz_config.limits.max_tilt,
            entity.ptz_config.limits.min_zoom,
            entity.ptz_config.limits.max_zoom,
            entity.ptz_config.default_position.pan,
            entity.ptz_config.default_position.tilt,
            entity.ptz_config.default_position.zoom,
            entity.ptz_config.mode.value if hasattr(entity.ptz_config.mode, 'value') else str(entity.ptz_config.mode),
            entity.ptz_config.speed_pan,
            entity.ptz_config.speed_tilt,
            entity.ptz_config.speed_zoom,
        )

    def find_by_camera_id(self, camera_id: str) -> RepositoryResult[CameraConfig | None]:
        """Find camera by camera_id."""
        return self.find_all("camera_id = ?", (camera_id,))

    def find_enabled(self) -> RepositoryResult[list[CameraConfig]]:
        """Find all enabled cameras."""
        return self.find_all("enabled = 1")