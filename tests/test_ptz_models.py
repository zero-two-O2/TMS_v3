"""Tests for the Phase 2 logical PTZ model (command/limits/tolerance/binding).

Covers spec items A-M, Q, V, W. Values are clearly-named test fixtures
(TEST_LIMITS); nothing here represents real machine limits.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from thermal_monitor.ptz.errors import PtzValidationError
from thermal_monitor.ptz.models import (
    MoveMode,
    PtzCommand,
    PtzLimits,
    PtzStationBinding,
    PtzTolerance,
    VelocityMode,
    within_tolerance,
)


TEST_LIMITS = PtzLimits(
    min_pan=-170.0,
    max_pan=170.0,
    min_tilt=-90.0,
    max_tilt=90.0,
    min_velocity=0.5,
    max_velocity=60.0,
    min_pan_velocity=0.5,
    max_pan_velocity=60.0,
    min_tilt_velocity=0.5,
    max_tilt_velocity=60.0,
)


class TestValidCommand:
    def test_single_velocity_command(self):
        cmd = PtzCommand(
            pan=35.0, tilt=-12.0, velocity_mode=VelocityMode.SINGLE,
            velocity=10.0, request_id="req-1",
        )
        assert cmd.pan == 35.0
        assert cmd.tilt == -12.0
        assert cmd.velocity == 10.0
        assert cmd.move_mode == MoveMode.ABSOLUTE
        cmd.validate(TEST_LIMITS)  # must not raise

    def test_per_axis_command(self):
        cmd = PtzCommand(
            pan=35.0, tilt=-12.0, velocity_mode=VelocityMode.PER_AXIS,
            pan_velocity=10.0, tilt_velocity=8.0,
        )
        assert cmd.pan_velocity == 10.0
        assert cmd.tilt_velocity == 8.0
        cmd.validate(TEST_LIMITS)


class TestInvalidCoordinates:
    def test_non_numeric_pan_rejected(self):
        with pytest.raises(PtzValidationError):
            PtzCommand(pan="35", tilt=0.0, velocity=10.0)

    def test_non_numeric_tilt_rejected(self):
        with pytest.raises(PtzValidationError):
            PtzCommand(pan=0.0, tilt=None, velocity=10.0)

    def test_bool_rejected(self):
        with pytest.raises(PtzValidationError):
            PtzCommand(pan=True, tilt=0.0, velocity=10.0)

    def test_nan_pan_rejected(self):
        with pytest.raises(PtzValidationError):
            PtzCommand(pan=math.nan, tilt=0.0, velocity=10.0)

    def test_inf_tilt_rejected(self):
        with pytest.raises(PtzValidationError):
            PtzCommand(pan=0.0, tilt=math.inf, velocity=10.0)

    def test_inf_velocity_rejected(self):
        with pytest.raises(PtzValidationError):
            PtzCommand(pan=0.0, tilt=0.0, velocity=math.inf)


class TestVelocityModes:
    def test_single_requires_velocity(self):
        with pytest.raises(PtzValidationError):
            PtzCommand(pan=0.0, tilt=0.0, velocity_mode=VelocityMode.SINGLE)

    def test_single_rejects_per_axis_fields(self):
        with pytest.raises(PtzValidationError):
            PtzCommand(
                pan=0.0, tilt=0.0, velocity_mode=VelocityMode.SINGLE,
                velocity=10.0, pan_velocity=10.0,
            )

    def test_per_axis_requires_both_axes(self):
        with pytest.raises(PtzValidationError):
            PtzCommand(
                pan=0.0, tilt=0.0, velocity_mode=VelocityMode.PER_AXIS,
                pan_velocity=10.0,
            )

    def test_per_axis_rejects_single_velocity(self):
        with pytest.raises(PtzValidationError):
            PtzCommand(
                pan=0.0, tilt=0.0, velocity_mode=VelocityMode.PER_AXIS,
                velocity=10.0, pan_velocity=10.0, tilt_velocity=8.0,
            )

    def test_velocity_below_minimum_rejected(self):
        cmd = PtzCommand(pan=0.0, tilt=0.0, velocity=0.1)
        with pytest.raises(PtzValidationError):
            cmd.validate(TEST_LIMITS)

    def test_velocity_above_maximum_rejected(self):
        cmd = PtzCommand(pan=0.0, tilt=0.0, velocity=999.0)
        with pytest.raises(PtzValidationError):
            cmd.validate(TEST_LIMITS)


class TestLimits:
    def test_out_of_range_pan(self):
        cmd = PtzCommand(pan=180.0, tilt=0.0, velocity=10.0)
        with pytest.raises(PtzValidationError):
            cmd.validate(TEST_LIMITS)

    def test_out_of_range_tilt(self):
        cmd = PtzCommand(pan=0.0, tilt=-91.0, velocity=10.0)
        with pytest.raises(PtzValidationError):
            cmd.validate(TEST_LIMITS)

    def test_boundary_values_accepted(self):
        cmd = PtzCommand(pan=170.0, tilt=-90.0, velocity=0.5)
        cmd.validate(TEST_LIMITS)

    def test_no_limits_means_no_range_check(self):
        cmd = PtzCommand(pan=5000.0, tilt=-5000.0, velocity=10.0)
        cmd.validate(None)
        cmd.validate(PtzLimits())

    def test_inverted_limits_rejected(self):
        with pytest.raises(PtzValidationError):
            PtzLimits(min_pan=10.0, max_pan=-10.0)

    def test_no_silent_clamping(self):
        """Validation reports; it never rewrites the command."""
        cmd = PtzCommand(pan=180.0, tilt=0.0, velocity=10.0)
        with pytest.raises(PtzValidationError):
            cmd.validate(TEST_LIMITS)
        assert cmd.pan == 180.0


class TestImmutability:
    def test_command_is_frozen(self):
        cmd = PtzCommand(pan=1.0, tilt=2.0, velocity=3.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            cmd.pan = 99.0  # type: ignore[misc]

    def test_limits_are_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            TEST_LIMITS.min_pan = 0.0  # type: ignore[misc]


class TestRelativeResolution:
    def test_absolute_returns_self(self):
        cmd = PtzCommand(pan=35.0, tilt=-12.0, velocity=10.0)
        assert cmd.to_absolute(20.0, 5.0) is cmd

    def test_relative_resolves_against_authoritative_position(self):
        cmd = PtzCommand(
            pan=5.0, tilt=-2.0, velocity=10.0, move_mode=MoveMode.RELATIVE
        )
        resolved = cmd.to_absolute(20.0, 5.0)
        assert resolved.move_mode == MoveMode.ABSOLUTE
        assert resolved.pan == pytest.approx(25.0)
        assert resolved.tilt == pytest.approx(3.0)
        assert resolved.velocity == 10.0


class TestTolerance:
    def test_negative_tolerance_rejected(self):
        with pytest.raises(PtzValidationError):
            PtzTolerance(pan=-0.1, tilt=0.1)

    def test_nan_tolerance_rejected(self):
        with pytest.raises(PtzValidationError):
            PtzTolerance(pan=math.nan, tilt=0.1)

    def test_within_tolerance(self):
        tol = PtzTolerance(pan=0.1, tilt=0.1)
        assert within_tolerance(35.05, -12.05, 35.0, -12.0, tol) is True
        assert within_tolerance(35.5, -12.0, 35.0, -12.0, tol) is False

    def test_asymmetric_tolerance(self):
        tol = PtzTolerance(pan=0.5, tilt=0.05)
        assert within_tolerance(35.4, -12.0, 35.0, -12.0, tol) is True
        assert within_tolerance(35.0, -12.06, 35.0, -12.0, tol) is False


class TestStationBinding:
    def test_valid_binding(self):
        binding = PtzStationBinding(camera_id="cam_HB25100002", ptz_id="PTZ_01")
        assert binding.camera_id == "cam_HB25100002"
        assert binding.ptz_id == "PTZ_01"

    def test_empty_camera_id_rejected(self):
        with pytest.raises(PtzValidationError):
            PtzStationBinding(camera_id="", ptz_id="PTZ_01")

    def test_empty_ptz_id_rejected(self):
        with pytest.raises(PtzValidationError):
            PtzStationBinding(camera_id="cam_A", ptz_id="  ")

    def test_binding_is_frozen(self):
        binding = PtzStationBinding(camera_id="cam_A", ptz_id="PTZ_01")
        with pytest.raises(dataclasses.FrozenInstanceError):
            binding.ptz_id = "PTZ_02"  # type: ignore[misc]

    def test_binding_carries_no_hardware_fields(self):
        """Association is identity only: no node IDs, addresses, or IPs."""
        assert set(dataclasses.asdict(PtzStationBinding(
            camera_id="c", ptz_id="p")).keys()) == {"camera_id", "ptz_id"}


class TestRequestId:
    def test_request_id_optional(self):
        cmd = PtzCommand(pan=0.0, tilt=0.0, velocity=10.0)
        assert cmd.request_id == ""

    def test_request_id_preserved(self):
        cmd = PtzCommand(pan=0.0, tilt=0.0, velocity=10.0, request_id="cmd-42")
        assert cmd.request_id == "cmd-42"

    def test_request_id_survives_relative_resolution(self):
        cmd = PtzCommand(
            pan=1.0, tilt=1.0, velocity=10.0,
            move_mode=MoveMode.RELATIVE, request_id="cmd-43",
        )
        assert cmd.to_absolute(0.0, 0.0).request_id == "cmd-43"

    def test_non_string_request_id_rejected(self):
        with pytest.raises(PtzValidationError):
            PtzCommand(pan=0.0, tilt=0.0, velocity=10.0, request_id=42)  # type: ignore[arg-type]
