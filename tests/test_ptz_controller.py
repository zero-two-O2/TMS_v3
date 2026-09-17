"""Unit + fake-transport tests for PtzController (no server, no GUI).

A scripted in-memory transport drives deterministic status sequences so
every workflow (gating, dispatch, completion, timeout, supersede, stop,
clear, calibration) is verified without timing assumptions.
"""

from __future__ import annotations

import time

import pytest

from thermal_monitor.ptz.client import OpcUaClientConfig, OpcUaSession
from thermal_monitor.ptz.controller import (
    PtzCommandError,
    PtzController,
    PtzOperationState,
)
from thermal_monitor.ptz.errors import PtzErrorCategory
from thermal_monitor.ptz.mapping import LogicalField, SimulatorPtzMapping
from thermal_monitor.ptz.models import (
    MoveMode,
    PtzCommand,
    PtzLimits,
    PtzTolerance,
    VelocityMode,
)
from thermal_monitor.ptz.protocol import (
    OpcUaNodeError,
    OpcUaTransportError,
    PtzNodeDescriptor,
    SubscriptionHandle,
    ValueCallback,
)
from thermal_monitor.ptz.state import PlcConnectionState

PTZ = "PTZ_01"
MAPPING = SimulatorPtzMapping(ptz_ids=(PTZ, "PTZ_02"))
LIMITS = PtzLimits(
    min_pan=-170.0,
    max_pan=170.0,
    min_tilt=-90.0,
    max_tilt=90.0,
    min_velocity=0.5,
    max_velocity=60.0,
)
TOL = PtzTolerance(pan=0.1, tilt=0.1)


def node_id(field: LogicalField, ptz_id: str = PTZ) -> str:
    """Authoritative test node ID straight from the real mapping."""
    return MAPPING.resolve(field, ptz_id).node_id


def healthy_values(ptz_id: str = PTZ, **overrides) -> dict[str, object]:
    values = {
        node_id(LogicalField.ACTUAL_PAN, ptz_id): 10.0,
        node_id(LogicalField.ACTUAL_TILT, ptz_id): -5.0,
        node_id(LogicalField.MOVING, ptz_id): False,
        node_id(LogicalField.POSITION_REACHED, ptz_id): True,
        node_id(LogicalField.READY, ptz_id): True,
        node_id(LogicalField.ERROR, ptz_id): False,
        node_id(LogicalField.ERROR_CODE, ptz_id): 0,
        node_id(LogicalField.CALIBRATION_REQUIRED, ptz_id): False,
        node_id(LogicalField.CALIBRATION_ACTIVE, ptz_id): False,
        node_id(LogicalField.CALIBRATION_COMPLETE, ptz_id): True,
    }
    values.update(overrides)
    return values


class ScriptedTransport:
    """Deterministic fake: per-node value sequences, recorded writes."""

    def __init__(self, values: dict[str, object]) -> None:
        self._values = dict(values)
        self._sequences: dict[str, list[object]] = {}
        self.writes: list[tuple[str, object]] = []
        self.connected = False
        self.read_error = None
        self.write_error = None
        self.fail_nodes: set[str] = set()

    def script(self, field: LogicalField, values: list[object]) -> None:
        self._sequences[node_id(field)] = list(values)

    def connect(self, endpoint: str, timeout_s: float) -> None:
        self.connected = True

    def disconnect(self, timeout_s: float) -> None:
        self.connected = False

    @property
    def is_connected(self) -> bool:
        return self.connected

    def read(self, node: PtzNodeDescriptor, timeout_s: float) -> object:
        if self.read_error is not None:
            raise self.read_error
        if node.node_id in self.fail_nodes:
            raise OpcUaNodeError(f"injected failure {node.node_id}")
        if not self.connected:
            raise OpcUaTransportError("fake disconnected")
        seq = self._sequences.get(node.node_id)
        if seq:
            value = seq.pop(0)
            if not seq:
                del self._sequences[node.node_id]
            self._values[node.node_id] = value
            return value
        try:
            return self._values[node.node_id]
        except KeyError as exc:
            raise OpcUaNodeError(f"missing {node.node_id}") from exc

    def write(self, node: PtzNodeDescriptor, value: object, timeout_s: float) -> None:
        if self.write_error is not None:
            raise self.write_error
        if not self.connected:
            raise OpcUaTransportError("fake disconnected")
        self.writes.append((node.node_id, value))
        self._values[node.node_id] = value

    def subscribe(self, node, callback, timeout_s: float) -> SubscriptionHandle:
        return SubscriptionHandle(node=node, token="scripted")

    def unsubscribe(self, handle) -> None:
        pass


def make_controller(
    transport: ScriptedTransport, **kwargs
) -> tuple[PtzController, OpcUaSession, SimulatorPtzMapping]:
    mapping = SimulatorPtzMapping(ptz_ids=(PTZ,))
    session = OpcUaSession(
        OpcUaClientConfig(endpoint="fake", read_timeout_s=1.0, write_timeout_s=1.0),
        transport,
    )
    session.connect()
    controller = PtzController(
        PTZ,
        session,
        mapping,
        limits=LIMITS,
        tolerance=TOL,
        poll_interval_s=0.005,
        **kwargs,
    )
    return controller, session, mapping


def written_fields(transport: ScriptedTransport) -> dict[str, object]:
    return {nid: value for nid, value in transport.writes}


class TestGating:
    def test_not_connected_rejected(self):
        transport = ScriptedTransport(healthy_values())
        controller, session, _ = make_controller(transport)
        session.disconnect()
        with pytest.raises(PtzCommandError) as exc_info:
            controller.move_absolute(20.0, 0.0, velocity=10.0, wait=False)
        assert exc_info.value.error.category == PtzErrorCategory.COMMUNICATION

    def test_latched_error_rejected(self):
        transport = ScriptedTransport(
            healthy_values(
                **{
                    node_id(LogicalField.ERROR): True,
                    node_id(LogicalField.ERROR_CODE): 100,
                    node_id(LogicalField.READY): False,
                }
            )
        )
        controller, _, _ = make_controller(transport)
        with pytest.raises(PtzCommandError) as exc_info:
            controller.move_absolute(20.0, 0.0, velocity=10.0, wait=False)
        assert exc_info.value.error.category == PtzErrorCategory.PTZ

    def test_calibration_active_rejected(self):
        transport = ScriptedTransport(
            healthy_values(
                **{node_id(LogicalField.CALIBRATION_ACTIVE): True}
            )
        )
        controller, _, _ = make_controller(transport)
        with pytest.raises(PtzCommandError) as exc_info:
            controller.move_absolute(20.0, 0.0, velocity=10.0, wait=False)
        assert exc_info.value.error.category == PtzErrorCategory.CALIBRATION

    def test_not_ready_rejected(self):
        transport = ScriptedTransport(
            healthy_values(**{node_id(LogicalField.READY): False})
        )
        controller, _, _ = make_controller(transport)
        with pytest.raises(PtzCommandError) as exc_info:
            controller.move_absolute(20.0, 0.0, velocity=10.0, wait=False)
        assert "not-ready" in exc_info.value.error.code


class TestValidation:
    def test_out_of_range_rejected_without_clamp(self):
        controller, _, _ = make_controller(ScriptedTransport(healthy_values()))
        with pytest.raises(PtzCommandError) as exc_info:
            controller.move_absolute(999.0, 0.0, velocity=10.0, wait=False)
        assert exc_info.value.error.category == PtzErrorCategory.COMMAND_REJECTED

    def test_bool_rejected(self):
        controller, _, _ = make_controller(ScriptedTransport(healthy_values()))
        with pytest.raises(PtzCommandError):
            controller.move_absolute(True, 0.0, velocity=10.0, wait=False)  # type: ignore[arg-type]

    def test_relative_command_rejected_at_boundary(self):
        controller, _, _ = make_controller(ScriptedTransport(healthy_values()))
        with pytest.raises(PtzCommandError) as exc_info:
            controller.dispatch(
                PtzCommand(
                    pan=1.0,
                    tilt=0.0,
                    velocity=10.0,
                    move_mode=MoveMode.RELATIVE,
                ),
                wait=False,
            )
        assert exc_info.value.error.category == PtzErrorCategory.COMMAND_REJECTED


class TestRelative:
    def test_resolves_against_authoritative_actual(self):
        controller, _, _ = make_controller(ScriptedTransport(healthy_values()))
        op = controller.move_relative(5.0, -5.0, velocity=10.0, wait=False)
        assert op.target_pan == pytest.approx(15.0)  # actual 10.0 + 5
        assert op.target_tilt == pytest.approx(-10.0)  # actual -5.0 - 5

    def test_standard_increments(self):
        controller, _, _ = make_controller(ScriptedTransport(healthy_values()))
        for inc in (1.0, 5.0, 10.0, -1.0, -5.0, -10.0):
            op = controller.move_relative(inc, 0.0, velocity=10.0, wait=False)
            assert op.target_pan == pytest.approx(10.0 + inc)

    def test_unavailable_actual_rejected(self):
        transport = ScriptedTransport(healthy_values())
        controller, session, _ = make_controller(transport)
        session.disconnect()
        with pytest.raises(PtzCommandError) as exc_info:
            controller.move_relative(5.0, 0.0, velocity=10.0, wait=False)
        assert exc_info.value.error.category == PtzErrorCategory.COMMUNICATION

    def test_relative_result_out_of_limits_rejected(self):
        controller, _, _ = make_controller(ScriptedTransport(healthy_values()))
        with pytest.raises(PtzCommandError):
            controller.move_relative(500.0, 0.0, velocity=10.0, wait=False)


class TestVelocityFields:
    def test_single_writes_and_mirrors(self):
        transport = ScriptedTransport(healthy_values())
        controller, _, _ = make_controller(transport)
        controller.move_absolute(20.0, 0.0, velocity=10.0, wait=False)
        fields = written_fields(transport)
        assert fields[node_id(LogicalField.TARGET_PAN)] == 20.0
        assert fields[node_id(LogicalField.VELOCITY)] == 10.0
        assert fields[node_id(LogicalField.PAN_VELOCITY)] == 10.0
        assert fields[node_id(LogicalField.TILT_VELOCITY)] == 10.0
        assert fields[node_id(LogicalField.COMMAND)] == 1  # MOVE strobe

    def test_per_axis_writes(self):
        transport = ScriptedTransport(healthy_values())
        controller, _, _ = make_controller(transport)
        controller.move_absolute(
            20.0,
            0.0,
            velocity_mode=VelocityMode.PER_AXIS,
            pan_velocity=12.0,
            tilt_velocity=8.0,
            wait=False,
        )
        fields = written_fields(transport)
        assert fields[node_id(LogicalField.PAN_VELOCITY)] == 12.0
        assert fields[node_id(LogicalField.TILT_VELOCITY)] == 8.0
        assert node_id(LogicalField.VELOCITY) not in fields


class TestCompletion:
    def test_accepted_to_reached(self):
        transport = ScriptedTransport(healthy_values())
        # After dispatch: MOVING True, then reached with actuals on target.
        transport.script(LogicalField.MOVING, [True, True, False])
        transport.script(
            LogicalField.POSITION_REACHED, [False, False, True]
        )
        transport.script(
            LogicalField.ACTUAL_PAN, [12.0, 18.0, 20.0]
        )
        controller, _, _ = make_controller(transport)
        op = controller.move_absolute(20.0, -5.0, velocity=10.0, wait=True)
        assert op.state == PtzOperationState.REACHED
        assert op.actual_pan == pytest.approx(20.0)
        assert op.terminal is True
        assert op.elapsed_s >= 0.0

    def test_wait_false_then_wait(self):
        transport = ScriptedTransport(healthy_values())
        transport.script(LogicalField.MOVING, [True, False])
        transport.script(LogicalField.POSITION_REACHED, [False, True])
        transport.script(LogicalField.ACTUAL_PAN, [15.0, 20.0])
        controller, _, _ = make_controller(transport)
        op = controller.move_absolute(20.0, -5.0, velocity=10.0, wait=False)
        assert op.state == PtzOperationState.ACKNOWLEDGED
        final = controller.wait_for_operation(op.operation_id, timeout_s=5.0)
        assert final.state == PtzOperationState.REACHED

    def test_unknown_operation(self):
        controller, _, _ = make_controller(ScriptedTransport(healthy_values()))
        with pytest.raises(PtzCommandError):
            controller.wait_for_operation("ptzop-999999", timeout_s=1.0)

    def test_timeout(self):
        transport = ScriptedTransport(
            healthy_values(
                **{
                    node_id(LogicalField.MOVING): True,
                    node_id(LogicalField.POSITION_REACHED): False,
                }
            )
        )
        controller, _, _ = make_controller(transport, move_timeout_s=5.0)
        op = controller.move_absolute(20.0, -5.0, velocity=10.0, wait=True, timeout_s=0.05)
        assert op.state == PtzOperationState.FAILED
        assert op.error is not None
        assert op.error.category == PtzErrorCategory.MOVEMENT_TIMEOUT

    def test_ptz_error_during_move(self):
        transport = ScriptedTransport(healthy_values())
        transport.script(LogicalField.MOVING, [True, False])
        transport.script(LogicalField.ERROR, [False, True])
        transport.script(LogicalField.ERROR_CODE, [0, 100])
        controller, _, _ = make_controller(transport)
        op = controller.move_absolute(20.0, -5.0, velocity=10.0, wait=True)
        assert op.state == PtzOperationState.FAILED
        assert op.error is not None
        assert op.error.category == PtzErrorCategory.PTZ

    def test_connection_loss_during_move(self):
        transport = ScriptedTransport(healthy_values())
        controller, session, _ = make_controller(transport)
        transport.script(LogicalField.MOVING, [True])
        op = controller.move_absolute(20.0, -5.0, velocity=10.0, wait=False)

        def _kill_and_wait():
            session.disconnect()
            return controller.wait_for_operation(op.operation_id, timeout_s=5.0)

        import threading

        result: list = []
        worker = threading.Thread(
            target=lambda: result.append(_kill_and_wait()), daemon=True
        )
        worker.start()
        time.sleep(0.05)
        # Reads now fail (disconnected) while session is DISCONNECTED.
        worker.join(timeout=5.0)
        assert result and result[0].state == PtzOperationState.FAILED
        assert result[0].error is not None

    def test_supersede_latest_wins(self):
        transport = ScriptedTransport(
            healthy_values(
                **{
                    node_id(LogicalField.MOVING): True,
                    node_id(LogicalField.POSITION_REACHED): False,
                }
            )
        )
        controller, _, _ = make_controller(transport, move_timeout_s=5.0)
        first = controller.move_absolute(20.0, 0.0, velocity=10.0, wait=False)
        second = controller.move_absolute(30.0, 0.0, velocity=10.0, wait=False)
        assert controller.get_operation(first.operation_id).state == (
            PtzOperationState.CANCELLED
        )
        assert second.target_pan == pytest.approx(30.0)


class TestStopClear:
    def test_stop(self):
        transport = ScriptedTransport(
            healthy_values(**{node_id(LogicalField.MOVING): True})
        )
        transport.script(LogicalField.MOVING, [True, False])
        controller, _, _ = make_controller(transport)
        op = controller.stop(timeout_s=5.0)
        assert op.state == PtzOperationState.CANCELLED
        assert written_fields(transport)[node_id(LogicalField.COMMAND)] == 2

    def test_clear_error(self):
        transport = ScriptedTransport(
            healthy_values(
                **{
                    node_id(LogicalField.ERROR): True,
                    node_id(LogicalField.ERROR_CODE): 100,
                }
            )
        )
        transport.script(LogicalField.ERROR, [True, False])
        controller, _, _ = make_controller(transport)
        status = controller.clear_error(timeout_s=5.0)
        assert status.error is None
        assert written_fields(transport)[node_id(LogicalField.COMMAND)] == 3


class TestCalibrationUnit:
    def test_request_and_complete(self):
        transport = ScriptedTransport(healthy_values())
        transport.script(
            LogicalField.CALIBRATION_ACTIVE, [True, True, False]
        )
        transport.script(
            LogicalField.CALIBRATION_COMPLETE, [False, False, True]
        )
        controller, _, _ = make_controller(transport)
        status = controller.request_calibration(
            wait=True, timeout_s=5.0
        )
        assert status.calibration.name == "COMPLETE"
        assert written_fields(transport)[
            node_id(LogicalField.CALIBRATION_REQUEST)
        ] is True

    def test_calibration_while_moving_rejected(self):
        transport = ScriptedTransport(
            healthy_values(**{node_id(LogicalField.MOVING): True})
        )
        controller, _, _ = make_controller(transport)
        with pytest.raises(PtzCommandError) as exc_info:
            controller.request_calibration(wait=False)
        assert exc_info.value.error.category == PtzErrorCategory.CALIBRATION

    def test_calibration_failure(self):
        transport = ScriptedTransport(healthy_values())
        transport.script(LogicalField.CALIBRATION_ACTIVE, [True, False])
        transport.script(LogicalField.ERROR, [False, True])
        transport.script(LogicalField.ERROR_CODE, [0, 101])
        controller, _, _ = make_controller(transport)
        with pytest.raises(PtzCommandError) as exc_info:
            controller.request_calibration(wait=True, timeout_s=5.0)
        assert exc_info.value.error.category == PtzErrorCategory.CALIBRATION


class TestSessionFailures:
    def test_write_failure(self):
        transport = ScriptedTransport(healthy_values())
        transport.write_error = OpcUaTransportError("write refused")
        controller, _, _ = make_controller(transport)
        with pytest.raises(PtzCommandError):
            controller.move_absolute(20.0, 0.0, velocity=10.0, wait=False)

    def test_degraded_status_without_throw(self):
        transport = ScriptedTransport(healthy_values())
        controller, session, _ = make_controller(transport)
        session.disconnect()
        status = controller.read_status()
        assert status.communication_ok is False
        assert status.plc_state == PlcConnectionState.DISCONNECTED

    def test_invalid_controller_config(self):
        transport = ScriptedTransport(healthy_values())
        mapping = SimulatorPtzMapping(ptz_ids=(PTZ,))
        session = OpcUaSession(OpcUaClientConfig(endpoint="fake"), transport)
        with pytest.raises(ValueError):
            PtzController(PTZ, session, mapping, poll_interval_s=0)
        with pytest.raises(ValueError):
            PtzController("", session, mapping)
