"""ptz.models -- hardware-independent logical PTZ model (Phase 2).

Application-level contract between the PTZ UI/controller and the future
OPC UA client / PLC mapping / simulator. This module knows nothing about
OPC UA, Siemens node IDs, namespaces, DB offsets, endpoints, or the
simulator implementation.

Engineering-unit convention (documented, no conversion framework):
pan/tilt in degrees, velocity in degrees/second.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from thermal_monitor.ptz.errors import PtzValidationError


class VelocityMode(str, Enum):
    """How velocity is expressed in a logical command.

    The real PLC may use a single velocity or per-axis velocities; the
    logical command supports both so the future mapping can translate
    either way without changing the UI/controller contract.
    """

    SINGLE = "single"
    PER_AXIS = "per_axis"


class MoveMode(str, Enum):
    """Whether a command carries an absolute target or a logical offset.

    ``RELATIVE`` is a UI/controller-layer convenience only: the
    controller resolves it against the authoritative PLC actual position
    into an absolute command before touching any PLC mapping. The model
    never forces the PLC protocol to support relative commands.
    """

    ABSOLUTE = "absolute"
    RELATIVE = "relative"


def require_finite_value(name: str, value: object) -> float:
    """Validate a numeric engineering value; return it as float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PtzValidationError(f"{name} must be numeric, got {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise PtzValidationError(f"{name} must be finite, got {value!r}")
    return result


def _require_optional_finite(name: str, value: object) -> Optional[float]:
    if value is None:
        return None
    return require_finite_value(name, value)


@dataclass(frozen=True, slots=True)
class PtzLimits:
    """Configurable PTZ operating envelope.

    Every bound is optional (``None`` = unconstrained) because the real
    machine limits are currently UNKNOWN. Only configured bounds are
    enforced; nothing is silently clamped -- violations raise
    :class:`PtzValidationError`.
    """

    min_pan: Optional[float] = None
    max_pan: Optional[float] = None
    min_tilt: Optional[float] = None
    max_tilt: Optional[float] = None
    min_velocity: Optional[float] = None
    max_velocity: Optional[float] = None
    min_pan_velocity: Optional[float] = None
    max_pan_velocity: Optional[float] = None
    min_tilt_velocity: Optional[float] = None
    max_tilt_velocity: Optional[float] = None

    def __post_init__(self) -> None:
        for name in (
            "min_pan",
            "max_pan",
            "min_tilt",
            "max_tilt",
            "min_velocity",
            "max_velocity",
            "min_pan_velocity",
            "max_pan_velocity",
            "min_tilt_velocity",
            "max_tilt_velocity",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, require_finite_value(name, value))
        pairs = (
            ("min_pan", "max_pan"),
            ("min_tilt", "max_tilt"),
            ("min_velocity", "max_velocity"),
            ("min_pan_velocity", "max_pan_velocity"),
            ("min_tilt_velocity", "max_tilt_velocity"),
        )
        for low_name, high_name in pairs:
            low = getattr(self, low_name)
            high = getattr(self, high_name)
            if low is not None and high is not None and not low < high:
                raise PtzValidationError(
                    f"{low_name} ({low}) must be < {high_name} ({high})"
                )


@dataclass(frozen=True, slots=True)
class PtzTolerance:
    """Configurable position-reached tolerance in degrees.

    Per-axis fields (a single value can be expressed by setting both to
    the same number). No default real-world value is assumed; callers
    pass explicit configuration or clearly-named test fixtures.
    """

    pan: float = 0.0
    tilt: float = 0.0

    def __post_init__(self) -> None:
        pan = require_finite_value("tolerance.pan", self.pan)
        tilt = require_finite_value("tolerance.tilt", self.tilt)
        if pan < 0:
            raise PtzValidationError(f"tolerance.pan must be >= 0, got {self.pan!r}")
        if tilt < 0:
            raise PtzValidationError(f"tolerance.tilt must be >= 0, got {self.tilt!r}")


@dataclass(frozen=True, slots=True)
class PtzCommand:
    """Immutable logical movement request: "move this PTZ to this target".

    This is NOT a PLC tag write. A future Siemens/simulator mapping
    translates it into protocol-specific operations; the command itself
    never changes.

    ``request_id`` is application-level correlation only (command ->
    acknowledgement -> movement -> position reached). The PLC is not
    assumed to support it; adapters correlate locally when needed. An
    empty string means "no correlation requested".
    """

    pan: float
    tilt: float
    velocity_mode: VelocityMode = VelocityMode.SINGLE
    velocity: Optional[float] = None
    pan_velocity: Optional[float] = None
    tilt_velocity: Optional[float] = None
    move_mode: MoveMode = MoveMode.ABSOLUTE
    request_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "pan", require_finite_value("pan", self.pan))
        object.__setattr__(self, "tilt", require_finite_value("tilt", self.tilt))
        if not isinstance(self.velocity_mode, VelocityMode):
            raise PtzValidationError(
                f"velocity_mode must be a VelocityMode, got {self.velocity_mode!r}"
            )
        if not isinstance(self.move_mode, MoveMode):
            raise PtzValidationError(
                f"move_mode must be a MoveMode, got {self.move_mode!r}"
            )
        if not isinstance(self.request_id, str):
            raise PtzValidationError(
                f"request_id must be a string, got {self.request_id!r}"
            )
        object.__setattr__(
            self, "velocity", _require_optional_finite("velocity", self.velocity)
        )
        object.__setattr__(
            self,
            "pan_velocity",
            _require_optional_finite("pan_velocity", self.pan_velocity),
        )
        object.__setattr__(
            self,
            "tilt_velocity",
            _require_optional_finite("tilt_velocity", self.tilt_velocity),
        )
        if self.velocity_mode == VelocityMode.SINGLE:
            if self.velocity is None:
                raise PtzValidationError(
                    "SINGLE velocity mode requires 'velocity'"
                )
            if self.pan_velocity is not None or self.tilt_velocity is not None:
                raise PtzValidationError(
                    "SINGLE velocity mode must not set pan_velocity/tilt_velocity"
                )
        else:  # PER_AXIS
            if self.pan_velocity is None or self.tilt_velocity is None:
                raise PtzValidationError(
                    "PER_AXIS velocity mode requires pan_velocity and tilt_velocity"
                )
            if self.velocity is not None:
                raise PtzValidationError(
                    "PER_AXIS velocity mode must not set 'velocity'"
                )

    def validate(self, limits: Optional[PtzLimits] = None) -> None:
        """Enforce configured limits; raises :class:`PtzValidationError`."""
        if limits is None:
            return
        if limits.min_pan is not None and self.pan < limits.min_pan:
            raise PtzValidationError(
                f"pan {self.pan} below minimum {limits.min_pan}"
            )
        if limits.max_pan is not None and self.pan > limits.max_pan:
            raise PtzValidationError(
                f"pan {self.pan} above maximum {limits.max_pan}"
            )
        if limits.min_tilt is not None and self.tilt < limits.min_tilt:
            raise PtzValidationError(
                f"tilt {self.tilt} below minimum {limits.min_tilt}"
            )
        if limits.max_tilt is not None and self.tilt > limits.max_tilt:
            raise PtzValidationError(
                f"tilt {self.tilt} above maximum {limits.max_tilt}"
            )
        velocities: list[tuple[str, float]] = []
        if self.velocity is not None:
            velocities.append(("velocity", self.velocity))
        if self.pan_velocity is not None:
            velocities.append(("pan_velocity", self.pan_velocity))
        if self.tilt_velocity is not None:
            velocities.append(("tilt_velocity", self.tilt_velocity))
        for name, value in velocities:
            lo = getattr(limits, f"min_{name}", None)
            hi = getattr(limits, f"max_{name}", None)
            if name == "velocity":
                lo = lo if lo is not None else limits.min_velocity
                hi = hi if hi is not None else limits.max_velocity
            if lo is not None and value < lo:
                raise PtzValidationError(f"{name} {value} below minimum {lo}")
            if hi is not None and value > hi:
                raise PtzValidationError(f"{name} {value} above maximum {hi}")

    def to_absolute(self, current_pan: float, current_tilt: float) -> "PtzCommand":
        """Resolve a RELATIVE command into an absolute target.

        Absolute commands return themselves. The controller calls this
        with the authoritative PLC actual position so the PLC protocol
        itself is never required to understand relative moves.
        """
        current_pan = require_finite_value("current_pan", current_pan)
        current_tilt = require_finite_value("current_tilt", current_tilt)
        if self.move_mode == MoveMode.ABSOLUTE:
            return self
        return PtzCommand(
            pan=current_pan + self.pan,
            tilt=current_tilt + self.tilt,
            velocity_mode=self.velocity_mode,
            velocity=self.velocity,
            pan_velocity=self.pan_velocity,
            tilt_velocity=self.tilt_velocity,
            move_mode=MoveMode.ABSOLUTE,
            request_id=self.request_id,
        )


def within_tolerance(
    actual_pan: float,
    actual_tilt: float,
    target_pan: float,
    target_tilt: float,
    tolerance: PtzTolerance,
) -> bool:
    """Pure position-reached predicate used by controller/simulator alike."""
    actual_pan = require_finite_value("actual_pan", actual_pan)
    actual_tilt = require_finite_value("actual_tilt", actual_tilt)
    target_pan = require_finite_value("target_pan", target_pan)
    target_tilt = require_finite_value("target_tilt", target_tilt)
    return abs(actual_pan - target_pan) <= tolerance.pan and abs(
        actual_tilt - target_tilt
    ) <= tolerance.tilt


@dataclass(frozen=True, slots=True)
class PtzStationBinding:
    """Logical association: ``camera_id`` -> ``ptz_id``.

    Identity only -- no OPC UA node IDs, PLC addresses, or IP
    assumptions. The ``ptz_id`` namespaces saved positions so camera A
    can never accidentally address camera B's PTZ or positions.
    """

    camera_id: str
    ptz_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.camera_id, str) or not self.camera_id.strip():
            raise PtzValidationError("camera_id is required")
        if not isinstance(self.ptz_id, str) or not self.ptz_id.strip():
            raise PtzValidationError("ptz_id is required")


__all__ = [
    "MoveMode",
    "PtzCommand",
    "PtzLimits",
    "PtzStationBinding",
    "PtzTolerance",
    "VelocityMode",
    "within_tolerance",
    "require_finite_value",
    "PtzValidationError",
]
