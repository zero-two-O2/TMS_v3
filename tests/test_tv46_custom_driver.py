"""Tests for camera.tv46_custom.CustomTV46LDriver (Stage 8G final, dual-feed).

No hardware required. GVCP and GVSP are faked in-memory; the driver is
exercised through the real FrameSource/AcquisitionWorker contract surface.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from thermal_monitor.camera.acquisition import AcquisitionWorker, InProcessLatestPublisher
from thermal_monitor.camera.source import (
    CameraConnectionError,
    CameraGrabError,
    CameraGrabTimeout,
)
from thermal_monitor.camera.model import CameraConfig, CameraIdentity
from thermal_monitor.camera.tv46_custom import CustomTV46LDriver
from thermal_monitor.camera.tv46_gvcp import GVCPCommand
from thermal_monitor.camera.tv46_gvsp import COMBINED_BYTES, IR_BYTES, LEADER_BYTES


def make_config(**overrides) -> CameraConfig:
    defaults = {
        "identity": CameraIdentity(camera_id="cam_custom_1", serial_number="SN-C1"),
        "device_identifier": "custom",
        "ip_address": "192.168.42.11",
    }
    defaults.update(overrides)
    return CameraConfig(**defaults)


class FakeGVCP:
    """Records register writes; serves canned readbacks."""

    def __init__(self, registers=None, fail_writes=()):
        self.registers = dict(registers or {})
        self.writes: list[tuple[int, int]] = []
        self.fail_writes = set(fail_writes)
        self.memory: dict[int, bytes] = {}
        self.closed = False

    def connect(self):
        return True

    def close(self):
        self.closed = True

    def control_switchover(self, key=2):
        self.writes.append((0x0A00, key))
        return True

    def read_register(self, address):
        return self.registers.get(address, 0)

    def write_register(self, address, value):
        if address in self.fail_writes:
            return False
        self.writes.append((address, value))
        self.registers[address] = value
        # Focus hardware mirror: writing SET moves the CURRENT readback
        # (motor positioning; exact match here, offsets covered separately).
        if address == 0x20A138:
            self.registers[0x20A13C] = value
        return True

    def read_memory(self, address, length):
        return self.memory.get(address, b"\x00" * length)

    def heartbeat_ping(self):
        return True


def _default_registers() -> dict:
    return {
        0x0D08: 10000,   # packet delay
        0x0D18: 0xC0A82A0B,  # SCDA0 == driver config ip 192.168.42.11
        0x0D00: 54321,   # SCP0
        0x0D04: 1500,    # SCPS0
        0x10A110: 3,     # fusion
        0x20A140: 150,   # focus min
        0x20A144: 1000000,  # focus max
        0x20A13C: 1000,  # focus current
    }


class FakeReceiver:
    """Pre-loaded completed blocks for deterministic grab tests."""

    def __init__(self, blocks=()):
        self._blocks = list(blocks)
        self.started = False
        self.stopped = False
        self.port = 50001

    def start(self):
        self.started = True
        return self.port

    def stop(self):
        self.stopped = True

    def get_block_with_id(self, timeout=5.0):
        if self._blocks:
            return self._blocks.pop(0)
        return None

    def get_stats(self):
        return {
            "packets_received": 100,
            "packets_dropped": 0,
            "duplicate_packets": 0,
            "blocks_incomplete": 0,
            "block_timeouts": 0,
            "blocks_completed": 1,
        }


def _ir_payload(first_value: int = 0x1234) -> bytes:
    payload = bytearray(COMBINED_BYTES)
    payload[LEADER_BYTES : LEADER_BYTES + 2] = struct.pack("<H", first_value)
    return bytes(payload)


def _driver(gvcp=None, blocks=()) -> tuple[CustomTV46LDriver, FakeGVCP, FakeReceiver]:
    gvcp = gvcp or FakeGVCP(registers=_default_registers())
    receiver = FakeReceiver(blocks=list(blocks))
    driver = CustomTV46LDriver(
        make_config(),
        camera_ip="192.168.42.11",
        local_ip="192.168.42.100",
        gvcp_factory=lambda: gvcp,
        receiver_factory=lambda: receiver,
    )
    return driver, gvcp, receiver


# -- connection lifecycle -------------------------------------------------


def test_construct_requires_camera_ip():
    cfg = make_config(ip_address="")
    with pytest.raises(ValueError):
        CustomTV46LDriver(cfg, camera_ip="")


def test_connect_runs_proven_bring_up_sequence():
    driver, gvcp, receiver = _driver()
    driver.connect()
    assert driver.is_connected() is True
    assert receiver.started is True
    written = dict(gvcp.writes)
    assert written[0x0A00] == 2            # CCP switchover
    assert written[0x0938] == 30000       # heartbeat
    assert written[0x0D08] == 10000       # packet delay
    assert written[0x10A110] == 3         # fusion combined
    assert written[0x10A104] == 1         # acquisition start
    driver.disconnect()


def test_connect_fails_without_control():
    gvcp = FakeGVCP(registers=_default_registers())
    gvcp.control_switchover = lambda key=2: False
    driver, _, _ = _driver(gvcp=gvcp)
    with pytest.raises(CameraConnectionError):
        driver.connect()


def test_connect_fails_on_packet_delay_mismatch_without_write():
    gvcp = FakeGVCP(registers=_default_registers(), fail_writes={0x0D08})
    driver, _, _ = _driver(gvcp=gvcp)
    with pytest.raises(CameraConnectionError):
        driver.connect()


def test_disconnect_is_idempotent_and_releases():
    driver, gvcp, receiver = _driver()
    driver.connect()
    driver.disconnect()
    driver.disconnect()
    assert driver.is_connected() is False
    assert receiver.stopped is True
    assert gvcp.closed is True


def test_reopen_reconnects_and_requires_first_frame():
    driver, _, receiver = _driver(blocks=[(9, _ir_payload())])
    driver.connect()
    receiver._blocks.append((10, _ir_payload()))
    driver.reopen()
    assert driver.is_connected() is True
    driver.disconnect()


# -- grab / FrameSource conformance ----------------------------------------


def test_grab_returns_raw_mono16_ir_read_only():
    driver, _, _ = _driver(blocks=[(41, _ir_payload(0x1234))])
    driver.connect()
    result = driver.grab(500)
    assert result.thermal is not None
    assert result.thermal.shape == (480, 640)
    assert result.thermal.dtype == np.uint16
    assert int(result.thermal[0, 0]) == 0x1234
    assert result.thermal.flags.writeable is False  # frame-contract immutability
    # Stage 8D dual-feed: same-block raw VL (YUYV), read-only, same frame ID.
    assert result.visible is not None
    assert result.visible.shape == (480, 1280)
    assert result.visible.dtype == np.uint8
    assert result.visible.flags.writeable is False
    assert result.visible_format == "YUV422_8"
    assert result.frame_id == 41
    assert result.hardware_timestamp is None
    assert result.packet_stats is not None
    driver.disconnect()


def test_grab_timeout_raises_grab_timeout_never_synthesises():
    driver, _, _ = _driver(blocks=[])
    driver.connect()
    with pytest.raises(CameraGrabTimeout):
        driver.grab(50)
    driver.disconnect()


def test_grab_malformed_payload_raises_grab_error():
    driver, _, _ = _driver(blocks=[(1, b"short")])
    driver.connect()
    with pytest.raises(CameraGrabError):
        driver.grab(500)
    driver.disconnect()


def test_grab_requires_connect():
    driver, _, _ = _driver()
    with pytest.raises(CameraConnectionError):
        driver.grab(100)


def test_block_id_wrap_extends_epoch():
    driver, _, _ = _driver(blocks=[(0xFFFF, _ir_payload()), (0x0000, _ir_payload())])
    driver.connect()
    first = driver.grab(500)
    second = driver.grab(500)
    assert first.frame_id == 0xFFFF
    assert second.frame_id == (1 << 16) | 0x0000
    assert second.frame_id > first.frame_id
    driver.disconnect()


def test_validate_registers_all_pass():
    driver, _, _ = _driver()
    driver.connect()
    result = driver.validate_registers(expected_scda_ip="192.168.42.100")
    assert result.all_passed is True
    assert {c.name for c in result.checks} == {"SCDA", "SCP", "FUSION"}
    driver.disconnect()


def test_worker_integration_streams_and_stops():
    """Custom driver through the UNCHANGED AcquisitionWorker (Stage 8B.1)."""
    blocks = [(i, _ir_payload(1000 + i)) for i in range(5)]
    driver, _, _ = _driver(blocks=blocks)
    publisher = InProcessLatestPublisher()
    worker = AcquisitionWorker("cam_custom_1", driver, publisher, make_config())
    worker.start()
    import time

    deadline = time.time() + 5.0
    while publisher.latest() is None and time.time() < deadline:
        time.sleep(0.02)
    # Snapshot BEFORE stop: worker shutdown closes the publisher (clears latest).
    frame = publisher.latest()
    published = worker.stats().published
    worker.stop()
    assert frame is not None
    assert published >= 1
    assert frame.payload.thermal is not None
    assert frame.payload.thermal.shape == (480, 640)
    # Stage 8D: worker carries the same-block VL with SYNCHRONIZED status.
    assert frame.payload.visible is not None
    assert frame.payload.visible.shape == (480, 1280)
    assert frame.descriptor.visible.present is True
    assert frame.descriptor.visible.sequence == frame.descriptor.thermal.sequence
    assert frame.descriptor.sync.status.value == "synchronized"


# -- NUC --------------------------------------------------------------------


def test_perform_nuc_sends_one_step_command():
    driver, gvcp, _ = _driver()
    driver.connect()
    driver.perform_nuc()
    assert (0x20A134, 11) in gvcp.writes
    driver.disconnect()


def test_perform_nuc_requires_streaming():
    driver, _, _ = _driver()
    with pytest.raises(CameraConnectionError):
        driver.perform_nuc()


def test_perform_nuc_rejected_command_raises():
    gvcp = FakeGVCP(registers=_default_registers(), fail_writes={0x20A134})
    driver, _, _ = _driver(gvcp=gvcp)
    driver.connect()
    with pytest.raises(CameraGrabError):
        driver.perform_nuc()
    driver.disconnect()


# -- focus -------------------------------------------------------------------


def test_focus_limits_and_readback():
    driver, _, _ = _driver()
    driver.connect()
    assert driver.get_focus_limits() == (150, 1000000)
    assert driver.get_focus_mm() == 1000
    driver.disconnect()


def test_focus_set_writes_and_returns_readback():
    driver, gvcp, _ = _driver()
    driver.connect()
    readback = driver.set_focus_mm(2500, settle_timeout=0.0)
    assert (0x20A138, 2500) in gvcp.writes
    assert readback == 2500
    driver.disconnect()


def test_focus_out_of_range_rejected_never_clamped():
    driver, gvcp, _ = _driver()
    driver.connect()
    with pytest.raises(ValueError):
        driver.set_focus_mm(99999999, settle_timeout=0.0)
    with pytest.raises(ValueError):
        driver.set_focus_mm(0, settle_timeout=0.0)
    assert all(addr != 0x20A138 for addr, _ in gvcp.writes)
    driver.disconnect()


def test_focus_rejects_non_integer():
    driver, _, _ = _driver()
    driver.connect()
    with pytest.raises(ValueError):
        driver.set_focus_mm(True, settle_timeout=0.0)  # bool is not valid mm
    driver.disconnect()


def test_focus_requires_control():
    driver, _, _ = _driver()
    with pytest.raises(CameraConnectionError):
        driver.set_focus_mm(1000)


# -- telemetry ----------------------------------------------------------------


def test_device_temperature_decodes_big_endian_float():
    gvcp = FakeGVCP(registers=_default_registers())
    gvcp.memory[0xA0AC] = struct.pack(">f", 42.5)
    driver, _, _ = _driver(gvcp=gvcp)
    driver.connect()
    assert driver.get_device_temperature_c() == pytest.approx(42.5)
    driver.disconnect()


def test_device_temperature_never_raises():
    driver, _, _ = _driver()
    assert driver.get_device_temperature_c() is None  # no control
    driver.connect()
    gvcp = driver._gvcp
    gvcp.memory[0xA0AC] = b"\x00\x01"  # short payload
    assert driver.get_device_temperature_c() is None
    gvcp.memory[0xA0AC] = struct.pack(">f", 999.0)  # out of range
    assert driver.get_device_temperature_c() is None
    driver.disconnect()


def test_verify_stream_config_restores_packet_delay_only():
    driver, gvcp, _ = _driver()
    driver.connect()
    gvcp.registers[0x0D08] = 0  # simulate NUC disturbance
    actual = driver.verify_stream_config(10000)
    assert gvcp.registers[0x0D08] == 10000
    assert actual["fusion_selector"] == 3
    driver.disconnect()


def test_get_status_exposes_counters():
    driver, _, _ = _driver()
    driver.connect()
    status = driver.get_status()
    assert status["camera_ip"] == "192.168.42.11"
    assert status["streaming"] is True
    assert "packets_received" in status
    driver.disconnect()
