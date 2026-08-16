"""Tests for the camera driver: timeout detection and config-driven behavior.

These tests do NOT require HALCON; they exercise pure logic that does not
touch the HALCON runtime.  Hardware-dependent tests are deliberately not
included (no HALCON in the CI/test environment).
"""

from __future__ import annotations

import pytest

from thermal_monitor.camera.driver import TV46LDriver
from thermal_monitor.camera.model import CameraConfig, CameraIdentity


def make_config(**overrides) -> CameraConfig:
    defaults = {
        "identity": CameraIdentity(camera_id="cam_1", serial_number="SN1"),
        "device_identifier": "test_dev",
    }
    defaults.update(overrides)
    return CameraConfig(**defaults)


class _ErrorCode:
    """Exception with HALCON-style error_code attribute."""


class FakeTimeoutError(Exception):
    error_code = 5322


class FakeNonTimeoutError(Exception):
    error_code = 3001


def test_grab_timeout_detection_by_error_code():
    assert TV46LDriver._is_grab_timeout(FakeTimeoutError("grab timeout"))


def test_non_timeout_halcon_error_is_not_timeout():
    assert not TV46LDriver._is_grab_timeout(FakeNonTimeoutError("some error"))


def test_timeout_detection_message_fallback():
    class MsgOnlyError(Exception):
        pass

    assert TV46LDriver._is_grab_timeout(MsgOnlyError("grab_image_async grab timeout after 500 ms"))


def test_non_timeout_message_is_not_timeout():
    class MsgOnlyError(Exception):
        pass

    assert not TV46LDriver._is_grab_timeout(MsgOnlyError("unexpected bus error"))


def test_driver_requires_halcon_only_at_connect():
    """Constructing a driver must not import HALCON."""
    driver = TV46LDriver(make_config())
    assert driver.is_connected() is False


def test_config_carries_v2_proven_tuning_defaults():
    cfg = make_config()
    assert cfg.grab_timeout_ms == 500
    assert cfg.socket_buffer_size == 1048576
    assert cfg.num_buffers == 8
    assert cfg.frame_rate == 9
    assert cfg.stream_source_thermal == "IR_Data"
    assert cfg.thermal_bits_per_channel == 16