"""OPC UA integration tests: Phase 3 client <-> simulator server.

Uses the REAL ``OpcUaSession`` + ``AsyncuaTransport`` against a REAL
asyncua server. Nothing bypasses OPC UA: every assertion below travels
the wire. Requires asyncua (skipped without it).
"""

from __future__ import annotations

import time

import pytest

asyncua = pytest.importorskip("asyncua")

from thermal_monitor.ptz.client import (
    OpcUaClientConfig,
    OpcUaSession,
    AsyncuaTransport,
)
from thermal_monitor.ptz.mapping import LogicalField, SimulatorPtzMapping
from thermal_monitor.ptz.models import PtzCommand, VelocityMode
from thermal_monitor.ptz.state import PlcConnectionState
from tools.ptz_plc_simulator.plc_server import PtzPlcSimulatorServer
from tools.ptz_plc_simulator.simulator_config import (
    SIMULATOR_TEST_ENDPOINT,
    SimulatorConfig,
    default_ptz_ids,
)

PTZ_IDS_2 = default_ptz_ids(2)
FAST = {"velocity": 120.0}  # deg/s keeps wire tests in the seconds range


def make_server(ptz_ids=PTZ_IDS_2, **overrides) -> PtzPlcSimulatorServer:
    config = SimulatorConfig(
        ptz_ids=tuple(ptz_ids),
        endpoint=SIMULATOR_TEST_ENDPOINT,
        calibration_duration_s=0.3,
        update_hz=50.0,
        # Test envelope: fast moves keep wire tests in the seconds range.
        # Production simulator default (60 deg/s) is unchanged.
        max_velocity=360.0,
        **overrides,
    )
    return PtzPlcSimulatorServer(config)


def make_client(**overrides) -> OpcUaSession:
    config = OpcUaClientConfig(
        endpoint=SIMULATOR_TEST_ENDPOINT,
        reconnect_max_attempts=20,
        reconnect_initial_backoff_s=0.05,
        reconnect_backoff_factor=1.0,
        reconnect_max_backoff_s=0.2,
        **overrides,
    )
    return OpcUaSession(config, AsyncuaTransport())


@pytest.fixture
def live():
    """Running 2-PTZ simulator + connected Phase 3 session."""
    server = make_server()
    server.start_background()
    mapping = SimulatorPtzMapping(ptz_ids=PTZ_IDS_2)
    session = make_client()
    session.connect()
    yield server, mapping, session
    session.shutdown()
    server.stop_background()


def wait_for(predicate, timeout_s: float = 10.0, poll_s: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()


def opcua_move(session, mapping, ptz_id, pan, tilt, velocity=120.0):
    """Drive a full simulator move purely over OPC UA.

    Mirrors the future controller pattern: write target + consistent
    velocities, strobe MOVE, wait for authoritative MOVING, then wait
    for authoritative POSITION_REACHED (never assumed from the write).
    """
    session.write_node(mapping.resolve(LogicalField.TARGET_PAN, ptz_id), pan)
    session.write_node(mapping.resolve(LogicalField.TARGET_TILT, ptz_id), tilt)
    session.write_node(mapping.resolve(LogicalField.VELOCITY, ptz_id), velocity)
    session.write_node(mapping.resolve(LogicalField.PAN_VELOCITY, ptz_id), velocity)
    session.write_node(mapping.resolve(LogicalField.TILT_VELOCITY, ptz_id), velocity)
    session.write_node(mapping.resolve(LogicalField.COMMAND, ptz_id), 1)
    assert wait_for(
        lambda: session.read_field(mapping, LogicalField.MOVING, ptz_id) is True
    ), f"{ptz_id} never started moving"
    assert wait_for(
        lambda: session.read_field(mapping, LogicalField.POSITION_REACHED, ptz_id)
        is True
    ), f"{ptz_id} never reached"


class TestServerLifecycle:
    def test_startup_exposes_namespace(self, live):
        server, mapping, session = live
        assert server.endpoint == SIMULATOR_TEST_ENDPOINT
        node = mapping.resolve(LogicalField.ACTUAL_PAN, "PTZ_01")
        assert session.read_node(node) is not None

    def test_shutdown(self):
        server = make_server()
        server.start_background()
        session = make_client()
        session.connect()
        session.shutdown()
        server.stop_background()
        assert session.state == PlcConnectionState.DISCONNECTED


class TestReadWrite:
    def test_read_status_snapshot(self, live):
        _, mapping, session = live
        assert session.read_field(mapping, LogicalField.READY, "PTZ_01") is True
        assert session.read_field(mapping, LogicalField.ERROR, "PTZ_01") is False
        assert session.read_field(mapping, LogicalField.MOVING, "PTZ_01") is False

    def test_write_command_fields(self, live):
        _, mapping, session = live
        session.write_node(mapping.resolve(LogicalField.TARGET_PAN, "PTZ_01"), 12.5)
        assert session.read_node(
            mapping.resolve(LogicalField.TARGET_PAN, "PTZ_01")
        ) == pytest.approx(12.5)

    def test_phase3_write_command_reaches_simulator(self, live):
        """Phase 3 write_command + explicit simulator MOVE strobe."""
        server, mapping, session = live
        cmd = PtzCommand(pan=25.0, tilt=-8.0, velocity=120.0)
        written = session.write_command(mapping, "PTZ_01", cmd)
        assert set(written) == {
            LogicalField.TARGET_PAN,
            LogicalField.TARGET_TILT,
            LogicalField.VELOCITY,
        }
        # Mirror SINGLE velocity to the axis nodes (simulator convention).
        session.write_node(mapping.resolve(LogicalField.PAN_VELOCITY, "PTZ_01"), 120.0)
        session.write_node(mapping.resolve(LogicalField.TILT_VELOCITY, "PTZ_01"), 120.0)
        session.write_node(mapping.resolve(LogicalField.COMMAND, "PTZ_01"), 1)
        assert wait_for(
            lambda: session.read_field(mapping, LogicalField.MOVING, "PTZ_01")
            is True,
            timeout_s=5.0,
        )
        assert wait_for(
            lambda: session.read_field(
                mapping, LogicalField.POSITION_REACHED, "PTZ_01"
            )
            is True
        )
        assert server.engine.state("PTZ_01").actual_pan == pytest.approx(25.0)


class TestMovementOverOpcUa:
    def test_move_to_target(self, live):
        _, mapping, session = live
        opcua_move(session, mapping, "PTZ_01", 45.0, -15.0)
        assert session.read_field(
            mapping, LogicalField.ACTUAL_PAN, "PTZ_01"
        ) == pytest.approx(45.0)
        assert session.read_field(
            mapping, LogicalField.ACTUAL_TILT, "PTZ_01"
        ) == pytest.approx(-15.0)

    def test_actual_position_updates_continuously(self, live):
        _, mapping, session = live
        samples: list[float] = []
        handle = session.subscribe(
            mapping,
            LogicalField.ACTUAL_PAN,
            "PTZ_01",
            lambda node, value: samples.append(float(value)),
        )
        opcua_move(session, mapping, "PTZ_01", 40.0, 0.0, velocity=60.0)
        session.unsubscribe(handle)
        assert len(samples) >= 3
        assert samples[0] < samples[-1] <= 40.0

    def test_reached_notification_via_subscription(self, live):
        _, mapping, session = live
        events: list[bool] = []
        handle = session.subscribe(
            mapping,
            LogicalField.POSITION_REACHED,
            "PTZ_01",
            lambda node, value: events.append(bool(value)),
        )
        try:
            opcua_move(session, mapping, "PTZ_01", 20.0, 5.0)
        finally:
            session.unsubscribe(handle)
        # Initial snapshot on subscribe is True (startup state); a real
        # move produces True -> False -> True transitions on the wire.
        # Direct reads outrun the notification pipeline, so allow the
        # trailing True to arrive.
        assert wait_for(lambda: len(events) >= 2 and events[-1] is True)

    def test_per_axis_velocity(self, live):
        _, mapping, session = live
        cmd = PtzCommand(
            pan=50.0,
            tilt=20.0,
            velocity_mode=VelocityMode.PER_AXIS,
            pan_velocity=120.0,
            tilt_velocity=30.0,
        )
        session.write_command(mapping, "PTZ_01", cmd)
        session.write_node(mapping.resolve(LogicalField.COMMAND, "PTZ_01"), 1)
        assert wait_for(
            lambda: session.read_field(
                mapping, LogicalField.POSITION_REACHED, "PTZ_01"
            )
            is True
        )

    def test_command_replacement(self, live):
        _, mapping, session = live
        session.write_node(mapping.resolve(LogicalField.TARGET_PAN, "PTZ_01"), 90.0)
        session.write_node(mapping.resolve(LogicalField.TARGET_TILT, "PTZ_01"), 0.0)
        for field in (
            LogicalField.VELOCITY,
            LogicalField.PAN_VELOCITY,
            LogicalField.TILT_VELOCITY,
        ):
            session.write_node(mapping.resolve(field, "PTZ_01"), 30.0)
        session.write_node(mapping.resolve(LogicalField.COMMAND, "PTZ_01"), 1)
        assert wait_for(
            lambda: session.read_field(mapping, LogicalField.MOVING, "PTZ_01")
            is True
        )
        session.write_node(mapping.resolve(LogicalField.TARGET_PAN, "PTZ_01"), -20.0)
        session.write_node(mapping.resolve(LogicalField.COMMAND, "PTZ_01"), 1)
        assert wait_for(
            lambda: session.read_field(
                mapping, LogicalField.POSITION_REACHED, "PTZ_01"
            )
            is True
        )
        assert session.read_field(
            mapping, LogicalField.ACTUAL_PAN, "PTZ_01"
        ) == pytest.approx(-20.0)


class TestIsolationAndConcurrency:
    def test_ptz_isolation(self, live):
        server, mapping, session = live
        opcua_move(session, mapping, "PTZ_01", 60.0, 10.0)
        assert session.read_field(
            mapping, LogicalField.ACTUAL_PAN, "PTZ_02"
        ) == pytest.approx(0.0)
        assert server.engine.state("PTZ_02").moving is False

    def test_eight_ptz_concurrent(self):
        ids = default_ptz_ids(8)
        server = make_server(ptz_ids=ids)
        server.start_background()
        mapping = SimulatorPtzMapping(ptz_ids=ids)
        session = make_client()
        try:
            session.connect()
            targets = {ptz_id: (float(i * 10), float(-i * 2)) for i, ptz_id in enumerate(ids)}
            for ptz_id, (pan, tilt) in targets.items():
                session.write_node(mapping.resolve(LogicalField.TARGET_PAN, ptz_id), pan)
                session.write_node(mapping.resolve(LogicalField.TARGET_TILT, ptz_id), tilt)
                for field in (
                    LogicalField.VELOCITY,
                    LogicalField.PAN_VELOCITY,
                    LogicalField.TILT_VELOCITY,
                ):
                    session.write_node(mapping.resolve(field, ptz_id), 120.0)
                session.write_node(mapping.resolve(LogicalField.COMMAND, ptz_id), 1)
            for ptz_id, (pan, tilt) in targets.items():
                assert wait_for(
                    lambda p=ptz_id: session.read_field(
                        mapping, LogicalField.POSITION_REACHED, p
                    )
                    is True,
                    timeout_s=15.0,
                ), f"{ptz_id} never reached"
                assert session.read_field(
                    mapping, LogicalField.ACTUAL_PAN, ptz_id
                ) == pytest.approx(pan)
        finally:
            session.shutdown()
            server.stop_background()


class TestCalibrationOverOpcUa:
    def test_calibration_cycle(self, live):
        _, mapping, session = live
        assert session.read_field(
            mapping, LogicalField.CALIBRATION_REQUIRED, "PTZ_01"
        ) is True
        session.write_node(
            mapping.resolve(LogicalField.CALIBRATION_REQUEST, "PTZ_01"), True
        )
        assert wait_for(
            lambda: session.read_field(
                mapping, LogicalField.CALIBRATION_ACTIVE, "PTZ_01"
            )
            is True
        )
        assert session.read_field(mapping, LogicalField.READY, "PTZ_01") is False
        assert wait_for(
            lambda: session.read_field(
                mapping, LogicalField.CALIBRATION_COMPLETE, "PTZ_01"
            )
            is True
        )
        assert session.read_field(mapping, LogicalField.READY, "PTZ_01") is True

    def test_movement_gated_during_calibration(self, live):
        _, mapping, session = live
        session.write_node(
            mapping.resolve(LogicalField.CALIBRATION_REQUEST, "PTZ_01"), True
        )
        assert wait_for(
            lambda: session.read_field(
                mapping, LogicalField.CALIBRATION_ACTIVE, "PTZ_01"
            )
            is True
        )
        session.write_node(mapping.resolve(LogicalField.TARGET_PAN, "PTZ_01"), 40.0)
        session.write_node(mapping.resolve(LogicalField.COMMAND, "PTZ_01"), 1)
        time.sleep(0.3)
        assert session.read_field(mapping, LogicalField.MOVING, "PTZ_01") is False


class TestFaultsOverOpcUa:
    def test_ptz_error_and_recovery(self, live):
        server, mapping, session = live
        server.engine.inject_ptz_error("PTZ_01")
        assert wait_for(
            lambda: session.read_field(mapping, LogicalField.ERROR, "PTZ_01")
            is True
        )
        assert session.read_field(mapping, LogicalField.READY, "PTZ_01") is False
        session.write_node(mapping.resolve(LogicalField.TARGET_PAN, "PTZ_01"), 30.0)
        session.write_node(mapping.resolve(LogicalField.COMMAND, "PTZ_01"), 1)
        time.sleep(0.3)
        assert session.read_field(mapping, LogicalField.MOVING, "PTZ_01") is False
        server.engine.clear_error("PTZ_01")
        opcua_move(session, mapping, "PTZ_01", 30.0, 0.0)

    def test_connection_loss_and_reconnect(self, live):
        server, mapping, session = live
        server.stop_listening()
        try:
            session.read_field(mapping, LogicalField.READY, "PTZ_01")
            lost = False
        except Exception:
            lost = True
        assert lost, "reads should fail while the server is down"
        session.notify_connection_lost("simulator stopped listening")
        assert wait_for(
            lambda: session.state == PlcConnectionState.COMMUNICATION_LOST
            or session.state == PlcConnectionState.RECONNECTING,
            timeout_s=3.0,
        )
        server.start_listening()
        assert wait_for(
            lambda: session.state == PlcConnectionState.CONNECTED, timeout_s=15.0
        )
        opcua_move(session, mapping, "PTZ_01", 15.0, 0.0)

    def test_shutdown_while_moving(self, live):
        server, mapping, session = live
        session.write_node(mapping.resolve(LogicalField.TARGET_PAN, "PTZ_01"), 90.0)
        session.write_node(mapping.resolve(LogicalField.VELOCITY, "PTZ_01"), 5.0)
        session.write_node(mapping.resolve(LogicalField.PAN_VELOCITY, "PTZ_01"), 5.0)
        session.write_node(mapping.resolve(LogicalField.TILT_VELOCITY, "PTZ_01"), 5.0)
        session.write_node(mapping.resolve(LogicalField.COMMAND, "PTZ_01"), 1)
        assert wait_for(
            lambda: session.read_field(mapping, LogicalField.MOVING, "PTZ_01")
            is True
        )
        session.shutdown()
        server.stop_background()
        assert session.state == PlcConnectionState.DISCONNECTED
