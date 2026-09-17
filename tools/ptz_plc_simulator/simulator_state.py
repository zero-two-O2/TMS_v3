"""Mutable per-PTZ simulator state.

Deliberately separate from the immutable Phase 2 ``PtzStatus``: this is
the simulator's internal working state. Status snapshots for OPC UA are
derived from it; nothing here leaks into production code.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SimulatorPtzState:
    """Mutable working state for one simulated PTZ instance."""

    ptz_id: str
    # Commanded target (degrees).
    target_pan: float = 0.0
    target_tilt: float = 0.0
    # Simulated actual position (degrees).
    actual_pan: float = 0.0
    actual_tilt: float = 0.0
    # Active velocity (deg/s). ``velocity`` is the SINGLE-mode value;
    # per-axis values are used in PER_AXIS mode.
    velocity: float = 10.0
    pan_velocity: float = 10.0
    tilt_velocity: float = 10.0
    use_per_axis_velocity: bool = False
    # Status flags mirrored to OPC UA.
    moving: bool = False
    position_reached: bool = True
    ready: bool = True
    enabled: bool = True
    error: bool = False
    error_code: int = 0
    # Calibration flags mirrored to OPC UA.
    calibration_required: bool = True
    calibration_active: bool = False
    calibration_complete: bool = False
    calibration_elapsed_s: float = 0.0
    # Deterministic test hooks (all False in normal operation).
    freeze_motion: bool = False  # accept command, never converge (timeout)
    fail_next_calibration: bool = False
    # Last rejection diagnostic for COMMAND_REJECTED visibility.
    last_rejection: str = ""


# Simulator error codes. Development-only; NOT Siemens codes.
NO_ERROR = 0
ERR_INVALID_TARGET = 1
ERR_INVALID_VELOCITY = 2
ERR_NOT_READY = 3
ERR_CALIBRATION_ACTIVE = 4
ERR_INJECTED_FAULT = 100
ERR_CALIBRATION_FAILED = 101


@dataclass(slots=True)
class SimulatorSnapshot:
    """Read-only view of one PTZ used for assertions/logging."""

    ptz_id: str
    fields: dict = field(default_factory=dict)


__all__ = [
    "ERR_CALIBRATION_ACTIVE",
    "ERR_CALIBRATION_FAILED",
    "ERR_INJECTED_FAULT",
    "ERR_INVALID_TARGET",
    "ERR_INVALID_VELOCITY",
    "ERR_NOT_READY",
    "NO_ERROR",
    "SimulatorPtzState",
    "SimulatorSnapshot",
]
