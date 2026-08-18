"""
core.models.camera -- Camera domain models.

Application-level camera models, independent of acquisition implementation.
These models represent the stable configuration and state that the rest of
the application uses. They do not contain HALCON, GVSP, or driver-specific
details.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional

import numpy as np


class CameraConnectionState(str, Enum):
    """High-level camera connection state visible to the application."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ACQUIRING = "acquiring"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class PTZMode(str, Enum):
    """PTZ operation mode."""

    MANUAL = "manual"
    PRESET = "preset"
    SCAN = "scan"
    LOCKED = "locked"


@dataclass(frozen=True, slots=True)
class CameraIdentity:
    """Stable identity of a physical camera.

    The camera_id is the permanent identity (serial-based).
    IP address is only a connection endpoint and must not be treated as identity.
    """

    camera_id: str
    serial_number: str
    model: str = ""
    vendor: str = ""
    firmware: str = ""
    user_name: str = ""

    def __post_init__(self) -> None:
        if not self.camera_id:
            raise ValueError("camera_id is required")
        if not self.serial_number:
            raise ValueError("serial_number is required")


@dataclass(frozen=True, slots=True)
class PTZPosition:
    """PTZ position (pan, tilt, zoom) with optional name/preset."""

    pan: float = 0.0
    tilt: float = 0.0
    zoom: float = 1.0
    name: str = ""
    preset_id: int | None = None

    def __post_init__(self) -> None:
        if not (-360.0 <= self.pan <= 360.0):
            raise ValueError(f"pan must be in [-360, 360], got {self.pan}")
        if not (-90.0 <= self.tilt <= 90.0):
            raise ValueError(f"tilt must be in [-90, 90], got {self.tilt}")
        if not (1.0 <= self.zoom <= 100.0):
            raise ValueError(f"zoom must be in [1, 100], got {self.zoom}")


@dataclass(frozen=True, slots=True)
class PTZLimits:
    """PTZ mechanical/configured limits."""

    min_pan: float = -170.0
    max_pan: float = 170.0
    min_tilt: float = -90.0
    max_tilt: float = 90.0
    min_zoom: float = 1.0
    max_zoom: float = 30.0

    def __post_init__(self) -> None:
        if self.min_pan >= self.max_pan:
            raise ValueError("min_pan must be < max_pan")
        if self.min_tilt >= self.max_tilt:
            raise ValueError("min_tilt must be < max_tilt")
        if self.min_zoom >= self.max_zoom:
            raise ValueError("min_zoom must be < max_zoom")

    def contains(self, position: PTZPosition) -> bool:
        return (
            self.min_pan <= position.pan <= self.max_pan
            and self.min_tilt <= position.tilt <= self.max_tilt
            and self.min_zoom <= position.zoom <= self.max_zoom
        )

    def clamp(self, position: PTZPosition) -> PTZPosition:
        return PTZPosition(
            pan=max(self.min_pan, min(self.max_pan, position.pan)),
            tilt=max(self.min_tilt, min(self.max_tilt, position.tilt)),
            zoom=max(self.min_zoom, min(self.max_zoom, position.zoom)),
            name=position.name,
            preset_id=position.preset_id,
        )


@dataclass(frozen=True, slots=True)
class PTZConfig:
    """PTZ configuration for a camera."""

    limits: PTZLimits = field(default_factory=PTZLimits)
    default_position: PTZPosition = field(default_factory=PTZPosition)
    preset_positions: Mapping[int, PTZPosition] = field(default_factory=dict)
    mode: PTZMode = PTZMode.MANUAL
    speed_pan: float = 10.0
    speed_tilt: float = 10.0
    speed_zoom: float = 5.0

    def get_preset(self, preset_id: int) -> PTZPosition | None:
        return self.preset_positions.get(preset_id)

    def with_preset(self, preset_id: int, position: PTZPosition) -> PTZConfig:
        new_presets = dict(self.preset_positions)
        new_presets[preset_id] = position
        return PTZConfig(
            limits=self.limits,
            default_position=self.default_position,
            preset_positions=MappingProxyType(new_presets),
            mode=self.mode,
            speed_pan=self.speed_pan,
            speed_tilt=self.speed_tilt,
            speed_zoom=self.speed_zoom,
        )


@dataclass(frozen=True, slots=True)
class CameraConfig:
    """Application-level camera configuration.

    This is the stable configuration used by the application layer.
    Acquisition-specific tuning (HALCON/GVSP) lives in camera.model.CameraConfig.
    """

    identity: CameraIdentity
    name: str = ""
    description: str = ""
    enabled: bool = True

    # Stream configuration
    thermal_enabled: bool = True
    visible_enabled: bool = False

    # PTZ
    ptz_config: PTZConfig = field(default_factory=PTZConfig)

    # Metadata
    tags: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    metadata: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class CameraStatus:
    """Runtime camera status visible to the application."""

    camera_id: str
    connection_state: CameraConnectionState = CameraConnectionState.DISCONNECTED
    current_position: PTZPosition = field(default_factory=PTZPosition)
    target_position: PTZPosition | None = None
    is_moving: bool = False
    last_frame_sequence: int = 0
    last_frame_timestamp: float = 0.0
    fps: float = 0.0
    error_message: str | None = None
    metadata: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def is_connected(self) -> bool:
        return self.connection_state in (
            CameraConnectionState.CONNECTED,
            CameraConnectionState.ACQUIRING,
            CameraConnectionState.DEGRADED,
        )

    @property
    def is_acquiring(self) -> bool:
        return self.connection_state == CameraConnectionState.ACQUIRING

    @property
    def is_healthy(self) -> bool:
        return self.connection_state in (
            CameraConnectionState.CONNECTED,
            CameraConnectionState.ACQUIRING,
        )


@dataclass(frozen=True, slots=True)
class CameraInfo:
    """Complete camera information combining config and status."""

    config: CameraConfig
    status: CameraStatus

    @property
    def camera_id(self) -> str:
        return self.config.identity.camera_id