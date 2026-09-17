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
from thermal_monitor.ptz.mapping import (
    LogicalField,
    NodeAccess,
    NodeDataType,
    PtzMapping,
    PtzMappingError,
    SiemensPtzMapping,
    SimulatorPtzMapping,
)
from thermal_monitor.ptz.protocol import (
    EventCallback,
    OpcUaNodeError,
    OpcUaTimeoutError,
    OpcUaTransport,
    OpcUaTransportError,
    PtzNodeDescriptor,
    SubscriptionHandle,
    TransportEvent,
    ValueCallback,
)
from thermal_monitor.ptz.client import (
    AsyncuaTransport,
    ConnectionCallback,
    OpcUaClientConfig,
    OpcUaSecurity,
    OpcUaSession,
    coerce_bool,
    coerce_float,
    command_to_fields,
    translate_error,
)
from thermal_monitor.ptz.controller import (
    CommandStrobe,
    PtzCommandError,
    PtzController,
    PtzOperation,
    PtzOperationState,
)
from thermal_monitor.ptz.service import (
    PtzService,
    PtzServiceConfig,
    StatusListener,
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
    "AsyncuaTransport",
    "CalibrationState",
    "CommandStrobe",
    "ConnectionCallback",
    "EventCallback",
    "LogicalField",
    "MoveMode",
    "NodeAccess",
    "NodeDataType",
    "OpcUaClientConfig",
    "OpcUaNodeError",
    "OpcUaSecurity",
    "OpcUaSession",
    "OpcUaTimeoutError",
    "OpcUaTransport",
    "OpcUaTransportError",
    "PlcConnectionState",
    "PtzCommand",
    "PtzCommandError",
    "PtzController",
    "PtzError",
    "PtzErrorCategory",
    "PtzLimits",
    "PtzMapping",
    "PtzMappingError",
    "PtzMovementState",
    "PtzOperation",
    "PtzOperationState",
    "PtzService",
    "PtzServiceConfig",
    "PtzStateError",
    "PtzStationBinding",
    "PtzStatus",
    "PtzTolerance",
    "PtzValidationError",
    "PtzNodeDescriptor",
    "SiemensPtzMapping",
    "SimulatorPtzMapping",
    "StatusListener",
    "SubscriptionHandle",
    "TransportEvent",
    "ValueCallback",
    "VelocityMode",
    "allowed_movement_transition",
    "allowed_plc_transition",
    "check_movement_transition",
    "check_plc_transition",
    "coerce_bool",
    "coerce_float",
    "command_to_fields",
    "translate_error",
    "within_tolerance",
]
