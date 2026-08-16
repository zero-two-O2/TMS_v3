"""
camera.model -- data models for the camera acquisition domain.

Contains only data definitions.  No hardware, threading or HALCON calls
belong here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np


class AcquisitionState(str, Enum):
    """Lifecycle state of one camera acquisition worker."""

    CREATED = "created"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ACQUIRING = "acquiring"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CameraIdentity:
    """Stable identity of one physical camera.

    The camera_id is the permanent identity (serial based, per V2:
    ``cam_{serial}``).  The IP address is only the connection endpoint and
    must not be treated as identity.
    """

    camera_id: str
    serial_number: str
    model: str = ""
    vendor: str = ""
    firmware: str = ""
    user_name: str = ""


@dataclass(frozen=True, slots=True)
class CameraConfig:
    """Validated configuration for connecting to and running one camera."""

    identity: CameraIdentity

    # Connection
    device_identifier: str = ""
    ip_address: str = ""

    # HALCON / GigE tuning (V2-validated values)
    frame_rate: int = 9
    grab_timeout_ms: int = 500
    socket_buffer_size: int = 1048576
    num_buffers: int = 8

    # Stream selection
    stream_source_thermal: str = "IR_Data"
    thermal_bits_per_channel: int = 16
    stream_source_visible: str | None = None
    visible_bits_per_channel: int = -1

    # Recovery
    consecutive_fail_limit: int = 3
    reconnect_interval_s: float = 3.0
    reconnect_backoff_factor: float = 2.0
    max_reconnect_attempts: int = 10


@dataclass(frozen=True, slots=True)
class GrabResult:
    """One acquisition from the driver.

    thermal/visible are raw immutable NumPy arrays, or None when that
    stream was not acquired for this frame.  Timing is in seconds using
    ``time.perf_counter()`` so the acquisition worker can measure latency
    without clock jumps.
    """

    thermal: np.ndarray | None = None
    visible: np.ndarray | None = None
    thermal_format: str | None = None
    visible_format: str | None = None
    hardware_timestamp: float | None = None
    grab_started: float = 0.0
    grab_completed: float = 0.0
    converted_at: float = 0.0


@dataclass(frozen=True, slots=True)
class AcquisitionStats:
    """Snapshotted performance counters for one acquisition worker."""

    state: AcquisitionState = AcquisitionState.CREATED
    total_acquired: int = 0
    published: int = 0
    dropped: int = 0
    sequence_gaps: int = 0
    consecutive_failures: int = 0
    reconnect_count: int = 0
    last_grab_duration_s: float = 0.0
    current_fps: float = 0.0
    average_fps: float = 0.0
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Result of a frame publication attempt.

    This is the boundary between acquisition and transport.  A future
    shared-memory ring buffer will return richer information (slot index,
    overwritten sequence, per-consumer state) without changing the worker.
    """

    accepted: bool
    """Whether the transport accepted the frame."""

    sequence: int
    """The sequence number that was published (or would have been)."""

    dropped: bool = False
    """True if the frame was dropped due to buffer full."""

    overwritten_sequence: int | None = None
    """If a frame was overwritten, the sequence number of the lost frame."""