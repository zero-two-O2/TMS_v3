"""Tests for the Phase 3 OPC UA session (fake transport only).

No live server, no asyncua, no GUI. Covers connect/read/write/subscribe
lifecycles, timeouts, mapping resolution, velocity translation, error
translation, reconnect, and bounded shutdown.
"""

from __future__ import annotations

import threading
import time

import pytest

from thermal_monitor.ptz.client import (
    OpcUaClientConfig,
    OpcUaSession,
    coerce_bool,
    coerce_float,
    command_to_fields,
    translate_error,
)
from thermal_monitor.ptz.errors import (
    PtzErrorCategory,
    PtzValidationError,
)
from thermal_monitor.ptz.mapping import (
    LogicalField,
    PtzMappingError,
    SiemensPtzMapping,
    SimulatorPtzMapping,
)
from thermal_monitor.ptz.models import (
    MoveMode,
    PtzCommand,
    PtzLimits,
    VelocityMode,
)
from thermal_monitor.ptz.protocol import (
    OpcUaNodeError,
    OpcUaTimeoutError,
    OpcUaTransportError,
    PtzNodeDescriptor,
    SubscriptionHandle,
    ValueCallback,
)
from thermal_monitor.ptz.state import PlcConnectionState


TEST_LIMITS = PtzLimits(
    min_pan=-170.0,
    max_pan=170.0,
    min_tilt=-90.0,
    max_tilt=90.0,
    min_velocity=0.5,
    max_velocity=60.0,
)


class FakeTransport:
    """In-memory OpcUaTransport with scripted failures and delays."""

    def __init__(
        self,
        *,
        values: dict[str, object] | None = None,
        connect_failures: int = 0,
        read_error: Exception | None = None,
        write_error: Exception | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self._values = dict(values or {})
        self._connect_failures = connect_failures
        self._read_error = read_error
        self._write_error = write_error
        self._delay = delay_s
        self._connected = False
        self._subscribers: dict[str, tuple[PtzNodeDescriptor, ValueCallback]] = {}
        self._counter = 0
        self.connect_calls = 0
        self.writes: list[tuple[str, object]] = []
        self.disconnect_calls = 0

    def _maybe_timeout(self, timeout_s: float) -> None:
        if self._delay > timeout_s:
            time.sleep(min(self._delay, timeout_s) + 0.01)
            raise OpcUaTimeoutError(f"fake timeout after {timeout_s}s")
        if self._delay:
            time.sleep(self._delay)

    def connect(self, endpoint: str, timeout_s: float) -> None:
        self.connect_calls += 1
        self._maybe_timeout(timeout_s)
        if self.connect_calls <= self._connect_failures:
            raise OpcUaTransportError("fake connect refused")
        if not endpoint:
            raise OpcUaTransportError("empty endpoint")
        self._connected = True

    def disconnect(self, timeout_s: float) -> None:
        self.disconnect_calls += 1
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def read(self, node: PtzNodeDescriptor, timeout_s: float) -> object:
        self._maybe_timeout(timeout_s)
        if self._read_error is not None:
            raise self._read_error
        if node.node_id not in self._values:
            raise OpcUaNodeError(f"node {node.node_id} unavailable")
        return self._values[node.node_id]

    def write(self, node: PtzNodeDescriptor, value: object, timeout_s: float) -> None:
        self._maybe_timeout(timeout_s)
        if self._write_error is not None:
            raise self._write_error
        self.writes.append((node.node_id, value))
        self._values[node.node_id] = value

    def subscribe(
        self, node: PtzNodeDescriptor, callback: ValueCallback, timeout_s: float
    ) -> SubscriptionHandle:
        self._maybe_timeout(timeout_s)
        self._counter += 1
        handle = SubscriptionHandle(node=node, token=f"fake-{self._counter}")
        self._subscribers[handle.token] = (node, callback)
        return handle

    def unsubscribe(self, handle: SubscriptionHandle) -> None:
        self._subscribers.pop(handle.token, None)

    def fire(self, token: str, value: object) -> None:
        node, callback = self._subscribers[token]
        callback(node, value)


def make_config(**overrides) -> OpcUaClientConfig:
    defaults = {
        "endpoint": "opc.tcp://127.0.0.1:4840",
        "connect_timeout_s": 1.0,
        "read_timeout_s": 1.0,
        "write_timeout_s": 1.0,
        "subscribe_timeout_s": 1.0,
        "reconnect_max_attempts": 3,
        "reconnect_initial_backoff_s": 0.01,
        "reconnect_backoff_factor": 1.0,
        "reconnect_max_backoff_s": 0.05,
    }
    defaults.update(overrides)
    return OpcUaClientConfig(**defaults)


def make_session(transport, **overrides):
    events: list[tuple[PlcConnectionState, str]] = []
    errors: list = []
    session = OpcUaSession(
        make_config(**overrides),
        transport,
        on_connection_changed=lambda s, d: events.append((s, d)),
        on_error=errors.append,
    )
    return session, events, errors


@pytest.fixture
def sim_mapping() -> SimulatorPtzMapping:
    return SimulatorPtzMapping(ptz_ids=("PTZ_01", "PTZ_02"))


def wait_for(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestConnect:
    def test_connect_success(self):
        session, events, _ = make_session(FakeTransport())
        session.connect()
        assert session.state == PlcConnectionState.CONNECTED
        assert events[0][0] == PlcConnectionState.CONNECTING
        assert events[-1][0] == PlcConnectionState.CONNECTED
        session.shutdown()

    def test_connect_failure_reports_error_state(self):
        session, events, errors = make_session(FakeTransport(connect_failures=99))
        with pytest.raises(OpcUaTransportError):
            session.connect()
        assert session.state == PlcConnectionState.ERROR
        assert errors and errors[0].category == PtzErrorCategory.COMMUNICATION

    def test_connect_timeout(self):
        session, _, errors = make_session(FakeTransport(delay_s=5.0))
        with pytest.raises(OpcUaTransportError):
            session.connect()
        assert errors and errors[0].category == PtzErrorCategory.COMMUNICATION
        session.shutdown()

    def test_missing_endpoint_rejected(self):
        session, _, _ = make_session(
            FakeTransport(), endpoint=""
        )
        with pytest.raises(PtzMappingError):
            session.connect()

    def test_disconnect(self):
        transport = FakeTransport()
        session, events, _ = make_session(transport)
        session.connect()
        session.disconnect()
        assert session.state == PlcConnectionState.DISCONNECTED
        assert transport.disconnect_calls == 1


class TestReadWrite:
    def test_read_success(self, sim_mapping):
        node_id = "ns=2;s=PTZ_01.ActualPan"
        session, _, _ = make_session(FakeTransport(values={node_id: 35.0}))
        session.connect()
        value = session.read_field(sim_mapping, LogicalField.ACTUAL_PAN, "PTZ_01")
        assert coerce_float(value, "actual_pan") == 35.0
        session.shutdown()

    def test_read_missing_node(self, sim_mapping):
        session, _, errors = make_session(FakeTransport(values={}))
        session.connect()
        with pytest.raises(OpcUaNodeError):
            session.read_field(sim_mapping, LogicalField.ACTUAL_PAN, "PTZ_01")
        assert errors and errors[-1].category == PtzErrorCategory.PLC
        session.shutdown()

    def test_read_timeout(self, sim_mapping):
        transport = FakeTransport()
        session, _, _ = make_session(transport)
        session.connect()
        transport._delay = 5.0
        with pytest.raises(OpcUaTimeoutError):
            session.read_field(sim_mapping, LogicalField.ACTUAL_PAN, "PTZ_01")
        session.shutdown()

    def test_write_success_records_value(self, sim_mapping):
        transport = FakeTransport()
        session, _, _ = make_session(transport)
        session.connect()
        session.write_node(
            sim_mapping.resolve(LogicalField.TARGET_PAN, "PTZ_01"), 35.0
        )
        assert transport.writes == [("ns=2;s=PTZ_01.TargetPan", 35.0)]
        session.shutdown()

    def test_write_failure(self, sim_mapping):
        session, _, errors = make_session(
            FakeTransport(write_error=OpcUaTransportError("rejected"))
        )
        session.connect()
        with pytest.raises(OpcUaTransportError):
            session.write_node(
                sim_mapping.resolve(LogicalField.TARGET_PAN, "PTZ_01"), 35.0
            )
        assert errors
        session.shutdown()

    def test_write_timeout(self, sim_mapping):
        transport = FakeTransport()
        session, _, _ = make_session(transport)
        session.connect()
        transport._delay = 5.0
        with pytest.raises(OpcUaTimeoutError):
            session.write_node(
                sim_mapping.resolve(LogicalField.TARGET_PAN, "PTZ_01"), 35.0
            )
        session.shutdown()


class TestSubscriptions:
    def test_value_change_callback(self, sim_mapping):
        received: list[tuple[str, object]] = []
        session, _, _ = make_session(FakeTransport())
        session.connect()
        handle = session.subscribe(
            sim_mapping,
            LogicalField.ACTUAL_PAN,
            "PTZ_01",
            lambda node, value: received.append((node.node_id, value)),
        )
        session._transport.fire(handle.token, 36.5)
        assert received == [("ns=2;s=PTZ_01.ActualPan", 36.5)]
        session.shutdown()

    def test_unsubscribe(self, sim_mapping):
        session, _, _ = make_session(FakeTransport())
        session.connect()
        handle = session.subscribe(
            sim_mapping, LogicalField.MOVING, "PTZ_01", lambda n, v: None
        )
        session.unsubscribe(handle)
        assert handle.token not in session._transport._subscribers
        session.shutdown()


class TestReconnect:
    def test_connection_loss_reconnects(self):
        transport = FakeTransport()
        session, events, _ = make_session(transport)
        session.connect()
        session.notify_connection_lost("cable pulled")
        assert wait_for(
            lambda: session.state == PlcConnectionState.CONNECTED
        ), events
        states = [s for s, _ in events]
        assert PlcConnectionState.COMMUNICATION_LOST in states
        assert PlcConnectionState.RECONNECTING in states
        session.shutdown()

    def test_reconnect_failure_exhausts_to_error(self):
        transport = FakeTransport()
        session, _, errors = make_session(
            transport, reconnect_max_attempts=2
        )
        session.connect()
        transport._connect_failures = 99
        session.notify_connection_lost("gone")
        assert wait_for(
            lambda: session.state == PlcConnectionState.ERROR
        )
        assert any("exhausted" in e.message for e in errors)
        session.shutdown()

    def test_subscriptions_restored_after_reconnect(self, sim_mapping):
        transport = FakeTransport()
        session, _, _ = make_session(transport)
        session.connect()
        received: list = []
        session.subscribe(
            sim_mapping, LogicalField.MOVING, "PTZ_01",
            lambda n, v: received.append(v),
        )
        before = transport.connect_calls
        session.notify_connection_lost("blip")
        assert wait_for(lambda: session.state == PlcConnectionState.CONNECTED)
        assert transport.connect_calls > before
        assert len(session._subscriptions) == 1
        session.shutdown()


class TestCommandTranslation:
    def test_single_velocity_write(self, sim_mapping):
        transport = FakeTransport()
        session, _, _ = make_session(transport)
        session.connect()
        cmd = PtzCommand(pan=35.0, tilt=-12.0, velocity=10.0)
        written = session.write_command(sim_mapping, "PTZ_01", cmd, TEST_LIMITS)
        assert written == {
            LogicalField.TARGET_PAN: 35.0,
            LogicalField.TARGET_TILT: -12.0,
            LogicalField.VELOCITY: 10.0,
        }
        ids = [node_id for node_id, _ in transport.writes]
        assert ids == [
            "ns=2;s=PTZ_01.TargetPan",
            "ns=2;s=PTZ_01.TargetTilt",
            "ns=2;s=PTZ_01.Velocity",
        ]
        session.shutdown()

    def test_per_axis_velocity_write(self, sim_mapping):
        transport = FakeTransport()
        session, _, _ = make_session(transport)
        session.connect()
        cmd = PtzCommand(
            pan=35.0, tilt=-12.0, velocity_mode=VelocityMode.PER_AXIS,
            pan_velocity=10.0, tilt_velocity=8.0,
        )
        written = session.write_command(sim_mapping, "PTZ_01", cmd)
        assert written[LogicalField.PAN_VELOCITY] == 10.0
        assert written[LogicalField.TILT_VELOCITY] == 8.0
        assert LogicalField.VELOCITY not in written
        session.shutdown()

    def test_relative_command_rejected_at_boundary(self, sim_mapping):
        session, _, _ = make_session(FakeTransport())
        session.connect()
        cmd = PtzCommand(
            pan=5.0, tilt=0.0, velocity=10.0, move_mode=MoveMode.RELATIVE
        )
        with pytest.raises(PtzValidationError):
            command_to_fields(cmd)
        with pytest.raises(PtzValidationError):
            session.write_command(sim_mapping, "PTZ_01", cmd)
        session.shutdown()

    def test_out_of_limits_command_rejected(self, sim_mapping):
        session, _, _ = make_session(FakeTransport())
        session.connect()
        cmd = PtzCommand(pan=180.0, tilt=0.0, velocity=10.0)
        with pytest.raises(PtzValidationError):
            session.write_command(sim_mapping, "PTZ_01", cmd, TEST_LIMITS)
        session.shutdown()

    def test_unknown_ptz_rejected(self, sim_mapping):
        session, _, _ = make_session(FakeTransport())
        session.connect()
        cmd = PtzCommand(pan=0.0, tilt=0.0, velocity=10.0)
        with pytest.raises(PtzMappingError):
            session.write_command(sim_mapping, "PTZ_99", cmd)
        session.shutdown()


class TestErrorTranslation:
    def test_timeout_maps_to_communication(self):
        error = translate_error(OpcUaTimeoutError("slow"), "read")
        assert error.category == PtzErrorCategory.COMMUNICATION

    def test_node_error_maps_to_plc(self):
        error = translate_error(OpcUaNodeError("bad"), "read")
        assert error.category == PtzErrorCategory.PLC

    def test_validation_maps_to_command_rejected(self):
        error = translate_error(PtzValidationError("bad"), "cmd")
        assert error.category == PtzErrorCategory.COMMAND_REJECTED

    def test_unknown_maps_to_unknown(self):
        error = translate_error(RuntimeError("weird"), "x")
        assert error.category == PtzErrorCategory.UNKNOWN

    def test_raw_exceptions_never_escape_as_contract(self, sim_mapping):
        """Session raises transport errors; PtzError goes to on_error."""
        session, _, errors = make_session(
            FakeTransport(read_error=OpcUaNodeError("bad status"))
        )
        session.connect()
        with pytest.raises(OpcUaNodeError):
            session.read_field(sim_mapping, LogicalField.READY, "PTZ_01")
        assert errors and isinstance(errors[0].code, str)
        session.shutdown()


class TestDatatypeCoercion:
    def test_float_from_int(self):
        assert coerce_float(35, "pan") == 35.0

    def test_bool_rejected_as_float(self):
        with pytest.raises(OpcUaNodeError):
            coerce_float(True, "pan")

    def test_none_rejected(self):
        with pytest.raises(OpcUaNodeError):
            coerce_float(None, "pan")

    def test_bool_from_int_flag(self):
        assert coerce_bool(1, "moving") is True
        assert coerce_bool(0, "moving") is False

    def test_bool_rejects_garbage(self):
        with pytest.raises(OpcUaNodeError):
            coerce_bool("yes", "moving")


class TestShutdown:
    def test_shutdown_while_connected(self):
        transport = FakeTransport()
        session, _, _ = make_session(transport)
        session.connect()
        session.shutdown(timeout_s=2.0)
        assert session.state == PlcConnectionState.DISCONNECTED
        assert transport.disconnect_calls >= 1

    def test_shutdown_during_reconnect(self):
        transport = FakeTransport()
        session, _, _ = make_session(
            transport, reconnect_max_attempts=100,
            reconnect_max_backoff_s=0.05,
        )
        session.connect()
        transport._connect_failures = 99
        session.notify_connection_lost("gone")
        time.sleep(0.05)
        session.shutdown(timeout_s=2.0)
        assert session.state == PlcConnectionState.DISCONNECTED
        thread = session._reconnect_thread
        assert thread is None or not thread.is_alive()

    def test_shutdown_with_pending_operation(self, sim_mapping):
        transport = FakeTransport(delay_s=0.2)
        session, _, _ = make_session(transport)
        session.connect()
        outcome: list[str] = []

        def _reader() -> None:
            try:
                session.read_field(sim_mapping, LogicalField.ACTUAL_PAN, "PTZ_01")
                outcome.append("done")
            except Exception:
                outcome.append("failed")

        worker = threading.Thread(target=_reader, daemon=True)
        worker.start()
        time.sleep(0.05)
        session.shutdown(timeout_s=2.0)
        worker.join(timeout=2.0)
        assert session.state == PlcConnectionState.DISCONNECTED
        assert outcome == ["done"] or outcome == ["failed"]

    def test_shutdown_is_idempotent(self):
        session, _, _ = make_session(FakeTransport())
        session.shutdown()
        session.shutdown()
        assert session.state == PlcConnectionState.DISCONNECTED


class TestGuiIndependence:
    def test_ptz_package_importable_without_qt(self):
        import sys

        qt_before = {name for name in sys.modules if name.startswith("PyQt")}
        import thermal_monitor.ptz  # noqa: F401

        qt_after = {name for name in sys.modules if name.startswith("PyQt")}
        assert qt_after == qt_before

    def test_siemens_mapping_never_returns_guessed_nodes(self):
        mapping = SiemensPtzMapping(ptz_ids=("PTZ_01",))
        with pytest.raises(PtzMappingError, match="not configured"):
            mapping.resolve(LogicalField.TARGET_PAN, "PTZ_01")
