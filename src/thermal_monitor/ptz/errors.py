"""ptz.errors -- structured PTZ error representation (Phase 2: model only).

Hardware-independent. No OPC UA, no Siemens, no GUI imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PtzErrorCategory(str, Enum):
    """Source/category of a PTZ failure.

    Lets the controller/UI distinguish a PLC communication failure
    from a PTZ operational error from an invalid command without
    parsing message text.
    """

    COMMUNICATION = "communication"
    COMMAND_REJECTED = "command_rejected"
    LIMIT = "limit"
    MOVEMENT_TIMEOUT = "movement_timeout"
    CALIBRATION = "calibration"
    PLC = "plc"
    PTZ = "ptz"
    UNKNOWN = "unknown"


class PtzValidationError(ValueError):
    """Raised when a logical PTZ command or value fails model validation.

    Follows the TMS_v3 convention of raising ``ValueError`` from frozen
    dataclass ``__post_init__`` (see ``core.models.camera``).
    """


class PtzStateError(RuntimeError):
    """Raised when a PTZ state transition is not allowed."""


@dataclass(frozen=True, slots=True)
class PtzError:
    """Immutable structured PTZ error snapshot."""

    code: str
    message: str
    category: PtzErrorCategory = PtzErrorCategory.UNKNOWN

    def __post_init__(self) -> None:
        if not self.code or not self.code.strip():
            raise PtzValidationError("PtzError.code is required")
        if not self.message or not self.message.strip():
            raise PtzValidationError("PtzError.message is required")


__all__ = [
    "PtzError",
    "PtzErrorCategory",
    "PtzStateError",
    "PtzValidationError",
]
