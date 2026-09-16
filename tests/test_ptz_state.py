"""Tests for the Phase 2 PTZ state/error model (status/movement/calibration).

Covers spec items N, O, P, Q, R, S.
"""

from __future__ import annotations

import dataclasses

import pytest

from thermal_monitor.ptz.errors import (
    PtzError,
    PtzErrorCategory,
    PtzStateError,
    PtzValidationError,
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


class TestValidStatus:
    def test_default_status(self):
        status = PtzStatus()
        assert status.plc_state == PlcConnectionState.DISCONNECTED
        assert status.moving is False
        assert status.position_reached is False
        assert status.accepts_commands is False

    def test_ready_status_accepts_commands(self):
        status = PtzStatus(
            plc_state=PlcConnectionState.CONNECTED,
            ptz_available=True,
            communication_ok=True,
            ready=True,
            movement=PtzMovementState.IDLE,
        )
        assert status.accepts_commands is True

    def test_camera_connected_ptz_unavailable_is_representable(self):
        """Camera and PTZ lifecycles are independent."""
        status = PtzStatus(
            plc_state=PlcConnectionState.CONNECTED,
            ptz_available=False,
            communication_ok=False,
            ready=False,
        )
        assert status.plc_connected is True
        assert status.accepts_commands is False

    def test_status_is_frozen(self):
        status = PtzStatus()
        with pytest.raises(dataclasses.FrozenInstanceError):
            status.ready = True  # type: ignore[misc]

    def test_non_finite_actual_rejected(self):
        with pytest.raises(PtzValidationError):
            PtzStatus(actual_pan=float("nan"))


class TestMovementStates:
    def test_moving_derived(self):
        assert PtzStatus(movement=PtzMovementState.MOVING).moving is True
        assert PtzStatus(movement=PtzMovementState.IDLE).moving is False

    def test_position_reached_derived(self):
        reached = PtzStatus(movement=PtzMovementState.POSITION_REACHED)
        assert reached.position_reached is True
        assert reached.moving is False

    def test_command_accepted_is_not_position_reached(self):
        """A fresh command is MOVING/IDLE, never POSITION_REACHED."""
        assert PtzStatus(movement=PtzMovementState.MOVING).position_reached is False
        assert PtzStatus(movement=PtzMovementState.IDLE).position_reached is False

    def test_idle_to_moving_allowed(self):
        assert allowed_movement_transition(
            PtzMovementState.IDLE, PtzMovementState.MOVING
        ) is True

    def test_moving_to_reached_allowed(self):
        assert allowed_movement_transition(
            PtzMovementState.MOVING, PtzMovementState.POSITION_REACHED
        ) is True

    def test_idle_to_reached_rejected(self):
        """Skipping MOVING is illegal: reached requires motion first."""
        assert allowed_movement_transition(
            PtzMovementState.IDLE, PtzMovementState.POSITION_REACHED
        ) is False
        with pytest.raises(PtzStateError):
            check_movement_transition(
                PtzMovementState.IDLE, PtzMovementState.POSITION_REACHED
            )

    def test_error_recovers_to_idle(self):
        assert allowed_movement_transition(
            PtzMovementState.ERROR, PtzMovementState.IDLE
        ) is True


class TestConnectionStates:
    def test_connect_sequence(self):
        assert allowed_plc_transition(
            PlcConnectionState.DISCONNECTED, PlcConnectionState.CONNECTING
        ) is True
        assert allowed_plc_transition(
            PlcConnectionState.CONNECTING, PlcConnectionState.CONNECTED
        ) is True

    def test_communication_lost_to_reconnect(self):
        assert allowed_plc_transition(
            PlcConnectionState.CONNECTED, PlcConnectionState.COMMUNICATION_LOST
        ) is True
        assert allowed_plc_transition(
            PlcConnectionState.COMMUNICATION_LOST, PlcConnectionState.RECONNECTING
        ) is True
        assert allowed_plc_transition(
            PlcConnectionState.RECONNECTING, PlcConnectionState.CONNECTED
        ) is True

    def test_connected_to_connecting_rejected(self):
        assert allowed_plc_transition(
            PlcConnectionState.CONNECTED, PlcConnectionState.CONNECTING
        ) is False
        with pytest.raises(PtzStateError):
            check_plc_transition(
                PlcConnectionState.CONNECTED, PlcConnectionState.CONNECTING
            )

    def test_error_has_bounded_exit(self):
        assert allowed_plc_transition(
            PlcConnectionState.ERROR, PlcConnectionState.DISCONNECTED
        ) is True


class TestCalibrationStates:
    def test_all_states_constructible(self):
        for state in CalibrationState:
            status = PtzStatus(calibration=state)
            assert status.calibration == state

    def test_active_calibration_blocks_commands(self):
        status = PtzStatus(
            plc_state=PlcConnectionState.CONNECTED,
            ptz_available=True,
            communication_ok=True,
            ready=True,
            calibration=CalibrationState.ACTIVE,
        )
        assert status.accepts_commands is False

    def test_complete_calibration_allows_commands(self):
        status = PtzStatus(
            plc_state=PlcConnectionState.CONNECTED,
            ptz_available=True,
            communication_ok=True,
            ready=True,
            calibration=CalibrationState.COMPLETE,
        )
        assert status.accepts_commands is True


class TestErrors:
    def test_communication_vs_operational_vs_invalid(self):
        comm = PtzError(
            code="OPC-UA-TIMEOUT", message="no PLC response",
            category=PtzErrorCategory.COMMUNICATION,
        )
        assert comm.category == PtzErrorCategory.COMMUNICATION
        op = PtzError(
            code="PTZ-DRIVE-FAULT", message="drive fault",
            category=PtzErrorCategory.PTZ,
        )
        assert op.category == PtzErrorCategory.PTZ
        rejected = PtzError(
            code="CMD-RANGE", message="pan out of range",
            category=PtzErrorCategory.COMMAND_REJECTED,
        )
        assert rejected.category == PtzErrorCategory.COMMAND_REJECTED

    def test_error_requires_code_and_message(self):
        with pytest.raises(PtzValidationError):
            PtzError(code="", message="x")
        with pytest.raises(PtzValidationError):
            PtzError(code="E1", message="  ")

    def test_error_blocks_commands(self):
        status = PtzStatus(
            plc_state=PlcConnectionState.CONNECTED,
            ptz_available=True,
            communication_ok=True,
            ready=True,
            error=PtzError(
                code="PTZ-DRIVE-FAULT", message="drive fault",
                category=PtzErrorCategory.PTZ,
            ),
        )
        assert status.accepts_commands is False
