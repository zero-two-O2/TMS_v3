"""ptz -- hardware-independent logical PTZ model (Phase 2).

Stable application-level contract between the PTZ UI/controller and
the future OPC UA client / PLC mapping / simulator. No OPC UA, no
Siemens, no simulator, no GUI imports anywhere in this package.
"""

from thermal_monitor.ptz.errors import (
    PtzError,
    PtzErrorCategory,
    PtzStateError,
    PtzValidationError,
)
from thermal_monitor.ptz.models import (
    MoveMode,
    PtzCommand,
    PtzLimits,
    PtzStationBinding,
    PtzTolerance,
    VelocityMode,
    within_tolerance,
)
from thermal_monitor.ptz.state import (
    CalibrationState,
    PlcConnectionState,
    PtzMovementState,
    PtzStatus,
    allowed_movement_transition,
    allowed_plc_transition,
    check_movement_transition,
    check_plc_transition,
)

__all__ = [
    "CalibrationState",
    "MoveMode",
    "PlcConnectionState",
    "PtzCommand",
    "PtzError",
    "PtzErrorCategory",
    "PtzLimits",
    "PtzMovementState",
    "PtzStateError",
    "PtzStationBinding",
    "PtzStatus",
    "PtzTolerance",
    "PtzValidationError",
    "VelocityMode",
    "allowed_movement_transition",
    "allowed_plc_transition",
    "check_movement_transition",
    "check_plc_transition",
    "within_tolerance",
]
