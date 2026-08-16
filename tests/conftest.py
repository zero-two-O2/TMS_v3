"""Shared fixtures and fakes for the camera acquisition test suite."""

from __future__ import annotations

import pytest
import numpy as np

from thermal_monitor.camera.driver import CameraGrabTimeout
from thermal_monitor.camera.model import CameraConfig, CameraIdentity, GrabResult, PublishResult
from thermal_monitor.core.frame import Frame


def make_camera_config(**overrides) -> CameraConfig:
    defaults = {
        "identity": CameraIdentity(
            camera_id="cam_test",
            serial_number="SN12345",
            model="TV46L-1-26010003@9Hz",
            vendor="Fluke Process Instruments",
        ),
        "device_identifier": "test_device_identifier",
        "grab_timeout_ms": 100,
        "consecutive_fail_limit": 2,
        "reconnect_interval_s": 0.01,
        "reconnect_backoff_factor": 1.0,
        "max_reconnect_attempts": 3,
    }
    defaults.update(overrides)
    return CameraConfig(**defaults)


def default_result(thermal_shape=(4, 4), dtype=np.uint16) -> GrabResult:
    array = np.zeros(thermal_shape, dtype=dtype)
    array.setflags(write=False)
    return GrabResult(thermal=array, thermal_format="IR_Data")


def empty_result() -> GrabResult:
    return GrabResult(thermal=None, visible=None)


class FakeFrameSource:
    """Scripted FrameSource for deterministic worker tests.

    ``grab_script`` items are returned in order; an Exception instance is
    raised.  When the script is exhausted ``default`` is returned (a plain
    valid frame unless overridden); if ``always_raise`` is set it is raised
    instead.
    """

    def __init__(
        self,
        grab_script=None,
        always_raise=None,
        connect_error=None,
        reopen_error=None,
        default=None,
    ) -> None:
        self._grab_script = list(grab_script or [])
        self._always_raise = always_raise
        self._connect_error = connect_error
        self._reopen_error = reopen_error
        self._default = default
        self._script_index = 0
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.grab_calls = 0
        self.reopen_calls = 0

    def connect(self) -> None:
        self.connect_calls += 1
        if self._connect_error is not None:
            raise self._connect_error

    def disconnect(self) -> None:
        self.disconnect_calls += 1

    def is_connected(self) -> bool:
        return True

    def grab(self, timeout_ms: int) -> GrabResult:
        self.grab_calls += 1
        if self._script_index < len(self._grab_script):
            item = self._grab_script[self._script_index]
            self._script_index += 1
            if isinstance(item, Exception):
                raise item
            return item
        if self._always_raise is not None:
            raise self._always_raise
        return self._default if self._default is not None else default_result()

    def reopen(self) -> None:
        self.reopen_calls += 1
        if self._reopen_error is not None:
            raise self._reopen_error


class FakePublisher:
    """In-memory publisher that records published frames."""

    def __init__(self, reject: bool = False) -> None:
        self._reject = reject
        self._frames: list[Frame] = []
        self._rejected = 0
        self._sequence = 0

    def publish(self, frame: Frame) -> PublishResult:
        if self._reject:
            self._rejected += 1
            return PublishResult(accepted=False, sequence=frame.descriptor.sequence, dropped=True)
        self._frames.append(frame)
        self._sequence = frame.descriptor.sequence
        return PublishResult(accepted=True, sequence=frame.descriptor.sequence, dropped=False)

    def latest(self) -> Frame | None:
        return self._frames[-1] if self._frames else None

    def reset(self) -> None:
        self._frames.clear()
        self._rejected = 0

    def close(self) -> None:
        pass

    @property
    def frames(self) -> list[Frame]:
        return self._frames

    @property
    def rejected(self) -> int:
        return self._rejected


@pytest.fixture
def camera_config():
    return make_camera_config()


@pytest.fixture
def fake_source():
    return FakeFrameSource()