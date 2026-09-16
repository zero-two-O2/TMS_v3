"""ptz.state -- logical PTZ state representation (Phase 2: model only).

Explicit enums plus an immutable authoritative status snapshot. The
model describes state; the future controller owns async transitions.

PLC connection and PTZ availability are independent: the camera may
stay connected while its PTZ communication is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from thermal_monitor.ptz.errors import PtzError, PtzStateError, PtzValidationError
from thermal_monitor.ptz.models import require_finite_value


class PlcConnectionState(str, Enum):
    """OPC UA session state toward the PLC (one session, many PTZs)."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    COMMUNICATION_LOST = "communication_lost"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class PtzMovementState(str, Enum):
    """Authoritative movement state reported by the PLC/simulator.

    "Command accepted" never implies "position reached": only
    ``POSITION_REACHED`` with matching actual position does.
    """

    IDLE = "idle"
    MOVING = "moving"
    POSITION_REACHED = "position_reached"
    ERROR = "error"


class CalibrationState(str, Enum):
    """Dedicated calibration state. Represents state only -- the real
    Siemens calibration sequence is unknown and lives in a later phase."""

    NOT_REQUIRED = "not_required"
    REQUIRED = "required"
    ACTIVE = "active"
    COMPLETE = "complete"
    FAILED = "failed"


_PLC_TRANSITIONS: dict[PlcConnectionState, frozenset[PlcConnectionState]] = {
    PlcConnectionState.DISCONNECTED: frozenset(
        {PlcConnectionState.CONNECTING, PlcConnectionState.DISCONNECTED}
    ),
    PlcConnectionState.CONNECTING: frozenset(
        {
            PlcConnectionState.CONNECTED,
            PlcConnectionState.DISCONNECTED,
            PlcConnectionState.ERROR,
            PlcConnectionState.COMMUNICATION_LOST,
        }
    ),
    PlcConnectionState.CONNECTED: frozenset(
        {
            PlcConnectionState.COMMUNICATION_LOST,
            PlcConnectionState.DISCONNECTED,
            PlcConnectionState.ERROR,
        }
    ),
    PlcConnectionState.COMMUNICATION_LOST: frozenset(
        {
            PlcConnectionState.RECONNECTING,
            PlcConnectionState.DISCONNECTED,
            PlcConnectionState.ERROR,
        }
    ),
    PlcConnectionState.RECONNECTING: frozenset(
        {
            PlcConnectionState.CONNECTED,
            PlcConnectionState.DISCONNECTED,
            PlcConnectionState.ERROR,
            PlcConnectionState.COMMUNICATION_LOST,
        }
    ),
    PlcConnectionState.ERROR: frozenset(
        {
            PlcConnectionState.DISCONNECTED,
            PlcConnectionState.CONNECTING,
            PlcConnectionState.RECONNECTING,
        }
    ),
}

_MOVEMENT_TRANSITIONS: dict[PtzMovementState, frozenset[PtzMovementState]] = {
    PtzMovementState.IDLE: frozenset(
        {PtzMovementState.MOVING, PtzMovementState.IDLE, PtzMovementState.ERROR}
    ),
    PtzMovementState.MOVING: frozenset(
        {
            PtzMovementState.POSITION_REACHED,
            PtzMovementState.IDLE,
            PtzMovementState.MOVING,
            PtzMovementState.ERROR,
        }
    ),
    PtzMovementState.POSITION_REACHED: frozenset(
        {
            PtzMovementState.IDLE,
            PtzMovementState.MOVING,
            PtzMovementState.POSITION_REACHED,
            PtzMovementState.ERROR,
        }
    ),
    PtzMovementState.ERROR: frozenset(
        {PtzMovementState.IDLE, PtzMovementState.ERROR}
    ),
}


def allowed_plc_transition(
    current: PlcConnectionState, target: PlcConnectionState
) -> bool:
    """True when ``current -> target`` is an explicit PLC transition."""
    if current == target:
        return True
    return target in _PLC_TRANSITIONS.get(current, frozenset())


def allowed_movement_transition(
    current: PtzMovementState, target: PtzMovementState
) -> bool:
    """True when ``current -> target`` is an explicit movement transition."""
    if current == target:
        return True
    return target in _MOVEMENT_TRANSITIONS.get(current, frozenset())


def check_plc_transition(
    current: PlcConnectionState, target: PlcConnectionState
) -> None:
    """Raise :class:`PtzStateError` when the PLC transition is illegal."""
    if not allowed_plc_transition(current, target):
        raise PtzStateError(
            f"Illegal PLC transition {current.value} -> {target.value}"
        )


def check_movement_transition(
    current: PtzMovementState, target: PtzMovementState
) -> None:
    """Raise :class:`PtzStateError` when the movement transition is illegal."""
    if not allowed_movement_transition(current, target):
        raise PtzStateError(
            f"Illegal movement transition {current.value} -> {target.value}"
        )


@dataclass(frozen=True, slots=True)
class PtzStatus:
    """Immutable authoritative PTZ status snapshot.

    No UI fields (no button/label/visibility state). ``moving`` mirrors
    ``movement`` for convenient gating; both are set from the same
    authoritative source.
    """

    plc_state: PlcConnectionState = PlcConnectionState.DISCONNECTED
    ptz_available: bool = False
    communication_ok: bool = False
    ready: bool = False
    movement: PtzMovementState = PtzMovementState.IDLE
    actual_pan: float = 0.0
    actual_tilt: float = 0.0
    calibration: CalibrationState = CalibrationState.NOT_REQUIRED
    error: Optional[PtzError] = None

    def __post_init__(self) -> None:
        if not isinstance(self.plc_state, PlcConnectionState):
            raise PtzValidationError(
                f"plc_state must be a PlcConnectionState, got {self.plc_state!r}"
            )
        if not isinstance(self.movement, PtzMovementState):
            raise PtzValidationError(
                f"movement must be a PtzMovementState, got {self.movement!r}"
            )
        if not isinstance(self.calibration, CalibrationState):
            raise PtzValidationError(
                f"calibration must be a CalibrationState, got {self.calibration!r}"
            )
        if not isinstance(self.error, (PtzError, type(None))):
            raise PtzValidationError(
                f"error must be a PtzError or None, got {self.error!r}"
            )
        object.__setattr__(self, "actual_pan", require_finite_value("actual_pan", self.actual_pan))
        object.__setattr__(
            self, "actual_tilt", require_finite_value("actual_tilt", self.actual_tilt)
        )

    @property
    def moving(self) -> bool:
        """True while the authoritative state reports motion."""
        return self.movement == PtzMovementState.MOVING

    @property
    def position_reached(self) -> bool:
        """True only on authoritative POSITION_REACHED (never on send)."""
        return self.movement == PtzMovementState.POSITION_REACHED

    @property
    def plc_connected(self) -> bool:
        return self.plc_state == PlcConnectionState.CONNECTED

    @property
    def accepts_commands(self) -> bool:
        """Gating predicate for the future controller: PLC connected,
        PTZ available, communication healthy, ready, and no calibration
        or error owning the axis."""
        return (
            self.plc_connected
            and self.ptz_available
            and self.communication_ok
            and self.ready
            and self.movement != PtzMovementState.ERROR
            and self.calibration != CalibrationState.ACTIVE
            and self.error is None
        )


__all__ = [
    "CalibrationState",
    "PlcConnectionState",
    "PtzMovementState",
    "PtzStatus",
    "allowed_movement_transition",
    "allowed_plc_transition",
    "check_movement_transition",
    "check_plc_transition",
]
