"""ptz.mapping -- logical-field to OPC UA node mapping (Phase 3).

The mapping knows WHAT node; the session knows HOW to communicate; the
future controller knows WHAT the value means. UI/controller code must
never contain raw node ID strings -- everything node-specific lives here.

Two implementations:

* :class:`SimulatorPtzMapping` -- proposed ``urn:tms:ptz:sim`` nodes for
  the Phase 4 simulator. Explicitly NOT Siemens tags.
* :class:`SiemensPtzMapping` -- production placeholder. Rejects every
  resolution until real PLC documentation provides the tag table.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping

from thermal_monitor.ptz.errors import PtzValidationError
from thermal_monitor.ptz.protocol import PtzNodeDescriptor


class LogicalField(str, Enum):
    """Hardware-independent PTZ concepts addressable through a mapping."""

    # Write (command path)
    TARGET_PAN = "target_pan"
    TARGET_TILT = "target_tilt"
    VELOCITY = "velocity"
    PAN_VELOCITY = "pan_velocity"
    TILT_VELOCITY = "tilt_velocity"
    COMMAND = "command"
    CALIBRATION_REQUEST = "calibration_request"
    # Read / subscribe (status path)
    ACTUAL_PAN = "actual_pan"
    ACTUAL_TILT = "actual_tilt"
    MOVING = "moving"
    POSITION_REACHED = "position_reached"
    READY = "ready"
    ERROR = "error"
    ERROR_CODE = "error_code"
    CALIBRATION_REQUIRED = "calibration_required"
    CALIBRATION_ACTIVE = "calibration_active"
    CALIBRATION_COMPLETE = "calibration_complete"


class NodeDataType(str, Enum):
    """Controlled OPC UA datatype vocabulary at the mapping boundary."""

    FLOAT = "float"
    BOOL = "bool"
    INT = "int"
    STRING = "string"


class NodeAccess(str, Enum):
    """Access direction of a mapped node."""

    READ = "read"
    WRITE = "write"
    READ_WRITE = "read_write"


class PtzMappingError(RuntimeError):
    """Unknown PTZ ID, unknown field, or unconfigured mapping."""


class PtzMapping:
    """Abstract logical-field -> node resolution for one PLC scope."""

    @property
    def known_ptz_ids(self) -> tuple[str, ...]:
        """PTZ instances addressable through this mapping."""
        raise NotImplementedError

    def available_fields(self, ptz_id: str) -> frozenset[LogicalField]:
        """Logical fields this mapping can resolve for ``ptz_id``.

        Lets callers declare availability instead of assuming every
        field exists in the real PLC.
        """
        raise NotImplementedError

    def resolve(self, field: LogicalField, ptz_id: str) -> PtzNodeDescriptor:
        """Return the node descriptor for ``field`` on ``ptz_id``.

        Raises :class:`PtzMappingError` for unknown IDs/fields.
        """
        raise NotImplementedError


_SIMULATOR_NAMESPACE = "urn:tms:ptz:sim"

# Proposed SIMULATOR nodes only -- NOT real Siemens PLC tags.
_SIMULATOR_FIELDS: Mapping[LogicalField, tuple[str, NodeDataType, NodeAccess]] = {
    LogicalField.TARGET_PAN: ("TargetPan", NodeDataType.FLOAT, NodeAccess.WRITE),
    LogicalField.TARGET_TILT: ("TargetTilt", NodeDataType.FLOAT, NodeAccess.WRITE),
    LogicalField.VELOCITY: ("Velocity", NodeDataType.FLOAT, NodeAccess.WRITE),
    LogicalField.PAN_VELOCITY: ("PanVelocity", NodeDataType.FLOAT, NodeAccess.WRITE),
    LogicalField.TILT_VELOCITY: ("TiltVelocity", NodeDataType.FLOAT, NodeAccess.WRITE),
    LogicalField.COMMAND: ("Command", NodeDataType.INT, NodeAccess.WRITE),
    LogicalField.CALIBRATION_REQUEST: (
        "CalibrationRequest",
        NodeDataType.BOOL,
        NodeAccess.WRITE,
    ),
    LogicalField.ACTUAL_PAN: ("ActualPan", NodeDataType.FLOAT, NodeAccess.READ),
    LogicalField.ACTUAL_TILT: ("ActualTilt", NodeDataType.FLOAT, NodeAccess.READ),
    LogicalField.MOVING: ("Moving", NodeDataType.BOOL, NodeAccess.READ),
    LogicalField.POSITION_REACHED: (
        "PositionReached",
        NodeDataType.BOOL,
        NodeAccess.READ,
    ),
    LogicalField.READY: ("Ready", NodeDataType.BOOL, NodeAccess.READ),
    LogicalField.ERROR: ("Error", NodeDataType.BOOL, NodeAccess.READ),
    LogicalField.ERROR_CODE: ("ErrorCode", NodeDataType.INT, NodeAccess.READ),
    LogicalField.CALIBRATION_REQUIRED: (
        "CalibrationRequired",
        NodeDataType.BOOL,
        NodeAccess.READ,
    ),
    LogicalField.CALIBRATION_ACTIVE: (
        "CalibrationActive",
        NodeDataType.BOOL,
        NodeAccess.READ,
    ),
    LogicalField.CALIBRATION_COMPLETE: (
        "CalibrationComplete",
        NodeDataType.BOOL,
        NodeAccess.READ,
    ),
}


@dataclass(frozen=True, slots=True)
class SimulatorPtzMapping(PtzMapping):
    """Proposed mapping for the Phase 4 development simulator.

    Node IDs take the deterministic form
    ``ns=<index>;s=<ptz_id>.<Name>`` under the simulator namespace
    ``urn:tms:ptz:sim`` (e.g. ``ns=2;s=PTZ_01.TargetPan``). These are
    development-only identifiers -- not Siemens tags.
    """

    ptz_ids: tuple[str, ...] = ()
    namespace_uri: str = _SIMULATOR_NAMESPACE
    namespace_index: int = 2

    def __post_init__(self) -> None:
        if not self.namespace_uri or not self.namespace_uri.strip():
            raise PtzValidationError("namespace_uri is required")
        for ptz_id in self.ptz_ids:
            if not ptz_id or not ptz_id.strip():
                raise PtzValidationError("ptz_ids must not contain blank IDs")

    @property
    def known_ptz_ids(self) -> tuple[str, ...]:
        return tuple(self.ptz_ids)

    def available_fields(self, ptz_id: str) -> frozenset[LogicalField]:
        self._require_ptz(ptz_id)
        return frozenset(_SIMULATOR_FIELDS.keys())

    def resolve(self, field: LogicalField, ptz_id: str) -> PtzNodeDescriptor:
        self._require_ptz(ptz_id)
        try:
            name, datatype, access = _SIMULATOR_FIELDS[field]
        except KeyError as exc:
            raise PtzMappingError(f"Unknown logical field {field!r}") from exc
        return PtzNodeDescriptor(
            node_id=f"ns={self.namespace_index};s={ptz_id}.{name}",
            datatype=datatype.value,
            access=access.value,
            logical_name=f"{ptz_id}.{field.value}",
        )

    def _require_ptz(self, ptz_id: str) -> None:
        if ptz_id not in self.ptz_ids:
            raise PtzMappingError(
                f"Unknown PTZ ID {ptz_id!r} (known: {list(self.ptz_ids)})"
            )


class SiemensPtzMapping(PtzMapping):
    """Production placeholder for the real Siemens CPU 1510SP-1 PN.

    Requires the real PLC tag documentation, which is currently
    unavailable. Every resolution fails with a clear configuration
    error -- it never returns guessed node IDs.
    """

    def __init__(self, ptz_ids: Iterable[str] = ()) -> None:
        self._ptz_ids = tuple(ptz_ids)

    @property
    def known_ptz_ids(self) -> tuple[str, ...]:
        return self._ptz_ids

    def available_fields(self, ptz_id: str) -> frozenset[LogicalField]:
        self._require_configured(ptz_id)
        return frozenset()

    def resolve(self, field: LogicalField, ptz_id: str) -> PtzNodeDescriptor:
        self._require_configured(ptz_id)
        raise PtzMappingError(
            "Siemens PTZ mapping is not configured: real PLC tag "
            f"documentation is required to resolve {field.value!r} "
            f"for {ptz_id!r}."
        )

    def _require_configured(self, ptz_id: str) -> None:
        if ptz_id not in self._ptz_ids:
            raise PtzMappingError(
                f"Unknown PTZ ID {ptz_id!r} (known: {list(self._ptz_ids)})"
            )


__all__ = [
    "LogicalField",
    "NodeAccess",
    "NodeDataType",
    "PtzMapping",
    "PtzMappingError",
    "SiemensPtzMapping",
    "SimulatorPtzMapping",
]
