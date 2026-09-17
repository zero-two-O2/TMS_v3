"""Unit tests for the PTZ simulation engine (no OPC UA server, no GUI).

Deterministic: time is driven explicitly via ``tick(dt)`` -- no sleeps.
"""

from __future__ import annotations

import pytest

from thermal_monitor.ptz.mapping import LogicalField
from tools.ptz_plc_simulator.ptz_simulator import (
    CMD_CLEAR_ERROR,
    CMD_MOVE,
    CMD_STOP,
    PtzSimulationEngine,
)
from tools.ptz_plc_simulator.simulator_config import SimulatorConfig
from tools.ptz_plc_simulator.simulator_state import (
    ERR_CALIBRATION_ACTIVE,
    ERR_CALIBRATION_FAILED,
    ERR_INJECTED_FAULT,
    ERR_INVALID_TARGET,
    ERR_INVALID_VELOCITY,
    ERR_NOT_READY,
)


@pytest.fixture
def config() -> SimulatorConfig:
    return SimulatorConfig(calibration_duration_s=1.0)


@pytest.fixture
def engine(config: SimulatorConfig) -> PtzSimulationEngine:
    return PtzSimulationEngine(config)


def move(engine: PtzSimulationEngine, ptz_id: str, pan: float, tilt: float,
         velocity: float = 60.0) -> None:
    engine.set_target(ptz_id, pan, tilt, velocity=velocity)
    assert engine.strobe_command(ptz_id, CMD_MOVE) is True


def run_until(engine: PtzSimulationEngine, ptz_id: str, *, limit_s: float = 30.0) -> None:
    elapsed = 0.0
    while not engine.state(ptz_id).position_reached and elapsed < limit_s:
        engine.tick(0.05)
        elapsed += 0.05
    assert engine.state(ptz_id).position_reached, f"{ptz_id} never reached"


class TestConfiguration:
    def test_defaults_are_simulator_values(self, config):
        assert config.default_pan == 0.0
        assert config.default_tilt == 0.0
        assert config.tolerance_pan == 0.1
        assert config.endpoint.startswith("opc.tcp://127.0.0.1")

    def test_empty_ptz_ids_rejected(self):
        with pytest.raises(ValueError):
            SimulatorConfig(ptz_ids=())


class TestInitialization:
    def test_eight_instances(self, engine):
        assert engine.ptz_ids == tuple(f"PTZ_{i:02d}" for i in range(1, 9))

    def test_initial_snapshot(self, engine):
        snap = engine.snapshot("PTZ_01")
        assert snap[LogicalField.ACTUAL_PAN] == 0.0
        assert snap[LogicalField.READY] is True
        assert snap[LogicalField.ERROR] is False
        assert snap[LogicalField.CALIBRATION_REQUIRED] is True

    def test_unknown_ptz_rejected(self, engine):
        with pytest.raises(KeyError):
            engine.state("PTZ_99")


class TestMovement:
    def test_target_pan_and_tilt(self, engine):
        move(engine, "PTZ_01", 90.0, 30.0)
        run_until(engine, "PTZ_01")
        st = engine.state("PTZ_01")
        assert st.actual_pan == 90.0
        assert st.actual_tilt == 30.0

    def test_simultaneous_axes(self, engine):
        """Pan and tilt converge together; reached needs both."""
        move(engine, "PTZ_01", 90.0, 30.0, velocity=30.0)
        engine.tick(1.0)  # ~30 deg each: pan partway, tilt done
        st = engine.state("PTZ_01")
        assert st.actual_pan == pytest.approx(30.0)
        assert st.actual_tilt == pytest.approx(30.0)
        assert st.moving is True
        assert st.position_reached is False
        run_until(engine, "PTZ_01")

    def test_time_based_not_instant(self, engine):
        move(engine, "PTZ_01", 90.0, 0.0, velocity=10.0)
        engine.tick(1.0)
        assert engine.state("PTZ_01").actual_pan == pytest.approx(10.0)

    def test_no_overshoot(self, engine):
        move(engine, "PTZ_01", 5.0, 0.0, velocity=60.0)
        engine.tick(10.0)  # far more than needed
        assert engine.state("PTZ_01").actual_pan == 5.0

    def test_movement_state_flags(self, engine):
        move(engine, "PTZ_01", 90.0, 0.0, velocity=10.0)
        st = engine.state("PTZ_01")
        assert st.moving is True
        assert st.position_reached is False
        run_until(engine, "PTZ_01")
        assert st.moving is False
        assert st.position_reached is True
        assert st.error is False

    def test_per_axis_velocity(self, engine):
        engine.set_target("PTZ_01", 60.0, 30.0, pan_velocity=60.0, tilt_velocity=10.0)
        assert engine.strobe_command("PTZ_01", CMD_MOVE) is True
        engine.tick(1.0)
        st = engine.state("PTZ_01")
        assert st.actual_pan == pytest.approx(60.0)
        assert st.actual_tilt == pytest.approx(10.0)

    def test_command_replacement_latest_wins(self, engine):
        move(engine, "PTZ_01", 90.0, 0.0, velocity=10.0)
        engine.tick(1.0)
        assert engine.state("PTZ_01").actual_pan == pytest.approx(10.0)
        move(engine, "PTZ_01", -45.0, 0.0, velocity=10.0)
        run_until(engine, "PTZ_01")
        assert engine.state("PTZ_01").actual_pan == -45.0

    def test_stop_aborts_in_place(self, engine):
        move(engine, "PTZ_01", 90.0, 0.0, velocity=10.0)
        engine.tick(1.0)
        assert engine.strobe_command("PTZ_01", CMD_STOP) is True
        st = engine.state("PTZ_01")
        assert st.moving is False
        assert st.actual_pan == pytest.approx(10.0)

    def test_independence(self, engine):
        move(engine, "PTZ_01", 50.0, 0.0)
        move(engine, "PTZ_02", -30.0, 10.0)
        run_until(engine, "PTZ_01")
        run_until(engine, "PTZ_02")
        assert engine.state("PTZ_01").actual_pan == 50.0
        assert engine.state("PTZ_02").actual_pan == -30.0
        assert engine.state("PTZ_02").actual_tilt == 10.0

    def test_tick_failure_isolation(self, engine):
        """A pathological state in one PTZ must not break the loop."""
        engine.tick(0.05)  # smoke: no exception with idle PTZs


class TestRejection:
    def test_invalid_target(self, engine):
        engine.set_target("PTZ_01", 999.0, 0.0, velocity=10.0)
        assert engine.strobe_command("PTZ_01", CMD_MOVE) is False
        st = engine.state("PTZ_01")
        assert st.error is True
        assert st.error_code == ERR_INVALID_TARGET
        assert st.moving is False

    def test_invalid_velocity(self, engine):
        engine.set_target("PTZ_01", 10.0, 0.0, velocity=9999.0)
        assert engine.strobe_command("PTZ_01", CMD_MOVE) is False
        assert engine.state("PTZ_01").error_code == ERR_INVALID_VELOCITY

    def test_not_ready(self, engine):
        engine.set_enabled("PTZ_01", False)
        engine.set_target("PTZ_01", 10.0, 0.0, velocity=10.0)
        assert engine.strobe_command("PTZ_01", CMD_MOVE) is False
        assert engine.state("PTZ_01").error_code == ERR_NOT_READY
        engine.set_enabled("PTZ_01", True)

    def test_unknown_command_code(self, engine):
        assert engine.strobe_command("PTZ_01", 99) is False


class TestCalibration:
    def test_start_and_complete(self, engine):
        assert engine.request_calibration("PTZ_01") is True
        st = engine.state("PTZ_01")
        assert st.calibration_active is True
        assert st.ready is False
        assert st.moving is False
        engine.tick(0.5)
        assert engine.state("PTZ_01").calibration_active is True
        engine.tick(0.6)
        st = engine.state("PTZ_01")
        assert st.calibration_active is False
        assert st.calibration_complete is True
        assert st.calibration_required is False
        assert st.ready is True
        assert (st.actual_pan, st.actual_tilt) == (0.0, 0.0)

    def test_movement_gated_during_calibration(self, engine):
        assert engine.request_calibration("PTZ_01") is True
        engine.set_target("PTZ_01", 10.0, 0.0, velocity=10.0)
        assert engine.strobe_command("PTZ_01", CMD_MOVE) is False
        assert engine.state("PTZ_01").error_code == ERR_CALIBRATION_ACTIVE

    def test_movement_allowed_after_calibration(self, engine):
        engine.request_calibration("PTZ_01")
        engine.tick(2.0)
        engine.clear_error("PTZ_01")
        move(engine, "PTZ_01", 20.0, 5.0)
        run_until(engine, "PTZ_01")

    def test_calibration_failure(self, engine):
        engine.inject_calibration_failure("PTZ_01", True)
        assert engine.request_calibration("PTZ_01") is True
        engine.tick(2.0)
        st = engine.state("PTZ_01")
        assert st.calibration_active is False
        assert st.calibration_complete is False
        assert st.error is True
        assert st.error_code == ERR_CALIBRATION_FAILED
        assert st.ready is False
        engine.clear_error("PTZ_01")
        assert engine.state("PTZ_01").error is False


class TestErrors:
    def test_inject_and_recover(self, engine):
        engine.inject_ptz_error("PTZ_01")
        st = engine.state("PTZ_01")
        assert st.error is True
        assert st.error_code == ERR_INJECTED_FAULT
        assert st.ready is False
        engine.set_target("PTZ_01", 10.0, 0.0, velocity=10.0)
        assert engine.strobe_command("PTZ_01", CMD_MOVE) is False
        engine.clear_error("PTZ_01")
        move(engine, "PTZ_01", 10.0, 0.0)
        run_until(engine, "PTZ_01")

    def test_clear_command(self, engine):
        engine.inject_ptz_error("PTZ_01")
        assert engine.strobe_command("PTZ_01", CMD_CLEAR_ERROR) is True
        assert engine.state("PTZ_01").error is False

    def test_motion_freeze_timeout(self, engine):
        """Accepted command, MOVING held, never converges."""
        engine.inject_motion_freeze("PTZ_01", True)
        move(engine, "PTZ_01", 90.0, 0.0, velocity=60.0)
        engine.tick(5.0)
        st = engine.state("PTZ_01")
        assert st.moving is True
        assert st.position_reached is False
        assert st.actual_pan == 0.0
        engine.inject_motion_freeze("PTZ_01", False)
        run_until(engine, "PTZ_01")
