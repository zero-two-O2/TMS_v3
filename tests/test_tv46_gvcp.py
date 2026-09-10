"""Tests for camera.tv46_gvcp: encoding, register I/O, discovery parsing.

No hardware required. The GVCPClient is exercised through an in-memory
fake UDP socket.
"""

from __future__ import annotations

import socket
import struct

import pytest

from thermal_monitor.camera.tv46_gvcp import (
    GVCPCommand,
    GVCPError,
    TV46DeviceInfo,
    broadcast_discover,
    build_request,
    discover_devices,
    local_interface_ips,
    parse_ack_header,
    parse_discovery_ack,
    GVCPClient,
)


class FakeSocket:
    """Scripted UDP socket: queued recv datagrams, recorded sends."""

    def __init__(self, recv_queue=None):
        self.recv_queue = list(recv_queue or [])
        self.sent: list[tuple[bytes, tuple]] = []
        self.timeout = None
        self.closed = False
        self.bound = None

    def settimeout(self, value):
        self.timeout = value

    def setsockopt(self, *args):
        pass

    def bind(self, addr):
        self.bound = addr

    def sendto(self, data, addr):
        self.sent.append((bytes(data), addr))
        return len(data)

    def recvfrom(self, size):
        if not self.recv_queue:
            raise socket.timeout("timed out")
        return self.recv_queue.pop(0)

    def close(self):
        self.closed = True


def _ack(req_id: int, ack_cmd: int, payload: bytes = b"", status: int = 0) -> bytes:
    header = struct.pack(">HHHH", status, ack_cmd, len(payload), req_id)
    return header + payload


def _client_with(recv_queue) -> tuple[GVCPClient, FakeSocket]:
    holder: dict = {}

    def factory():
        sock = FakeSocket(recv_queue)
        holder["sock"] = sock
        return sock

    client = GVCPClient("192.168.42.11", local_ip="192.168.42.100", socket_factory=factory)
    client.connect()
    return client, holder["sock"]


def test_build_request_header_layout():
    req = build_request(GVCPCommand.READ_REG, b"\x00\x00\x0d\x08", 0x1234)
    assert req[:2] == b"\x42\x01"
    assert struct.unpack(">H", req[2:4])[0] == GVCPCommand.READ_REG
    assert struct.unpack(">H", req[4:6])[0] == 4
    assert struct.unpack(">H", req[6:8])[0] == 0x1234


def test_parse_ack_header_round_trip():
    req = build_request(GVCPCommand.WRITE_REG, b"\x00" * 8, 7)
    req_id = struct.unpack(">H", req[6:8])[0]
    ack = _ack(req_id, GVCPCommand.WRITE_REG_ACK, b"\x00" * 4)
    status, ack_cmd, ack_id = parse_ack_header(ack)
    assert (status, ack_cmd, ack_id) == (0, GVCPCommand.WRITE_REG_ACK, req_id)


def test_parse_ack_header_rejects_short():
    with pytest.raises(ValueError):
        parse_ack_header(b"\x00\x00\x00")


def test_read_register_returns_big_endian_value():
    client, sock = _client_with([(_ack(1, GVCPCommand.READ_REG_ACK, struct.pack(">I", 10000)), ("192.168.42.11", 3956))])
    assert client.read_register(0x0D08) == 10000
    sent, addr = sock.sent[0]
    assert addr == ("192.168.42.11", 3956)
    assert struct.unpack(">H", sent[2:4])[0] == GVCPCommand.READ_REG


def test_write_register_true_on_zero_status():
    client, _ = _client_with([(_ack(1, GVCPCommand.WRITE_REG_ACK), ("192.168.42.11", 3956))])
    assert client.write_register(0x10A110, 3) is True


def test_write_register_false_on_timeout_no_raise():
    client, _ = _client_with([])
    assert client.write_register(0x10A110, 3) is False


def test_read_register_raises_on_nonzero_status():
    client, _ = _client_with([(_ack(1, GVCPCommand.READ_REG_ACK, b"\x00" * 4, status=0x8006), ("192.168.42.11", 3956))])
    with pytest.raises(GVCPError):
        client.read_register(0x10A104)


def test_pending_ack_waits_then_accepts_final():
    pending = struct.pack(">HHHHH", 0, GVCPCommand.PENDING_ACK, 2, 1, 100)
    final = _ack(1, GVCPCommand.WRITE_REG_ACK)
    client, _ = _client_with([(pending, ("192.168.42.11", 3956)), (final, ("192.168.42.11", 3956))])
    assert client.write_register(0x10A104, 1) is True


def test_read_memory_chunks_large_reads():
    first = _ack(1, GVCPCommand.READ_MEM_ACK, struct.pack(">I", 0xA0AC) + b"A" * 512)
    second = _ack(2, GVCPCommand.READ_MEM_ACK, struct.pack(">I", 0xA2AC) + b"B" * 88)
    client, _ = _client_with([(first, ("192.168.42.11", 3956)), (second, ("192.168.42.11", 3956))])
    data = client.read_memory(0xA0AC, 600)
    assert data == b"A" * 512 + b"B" * 88


def _discovery_bytes() -> bytes:
    """Synthetic DISCOVERY_ACK using the HARDWARE-MEASURED TV46L layout
    (Stage 8D: verified on .11/.13 against camera serials/MACs)."""
    header = struct.pack(">HHHH", 0, GVCPCommand.DISCOVERY_ACK, 248, 9)
    payload = bytearray(248)
    payload[10:16] = bytes([0x34, 0x08, 0xE1, 0xD8, 0xDB, 0xBE])
    payload[28:32] = bytes([192, 168, 42, 13])
    payload[72:104] = b"Fluke Process Instruments" + b"\x00" * 7
    payload[104:136] = b"TV46L-1-26010003@9Hz" + b"\x00" * 12
    payload[136:141] = b"1.0.8"
    payload[216:226] = b"HB25080011"
    return bytes(header) + bytes(payload)


def test_parse_discovery_ack_fields():
    info = parse_discovery_ack(_discovery_bytes(), "192.168.42.13")
    assert info.ip_address == "192.168.42.13"
    # 6-octet device MAC.
    assert info.mac_address == "34:08:e1:d8:db:be"
    assert info.serial_number == "HB25080011"
    assert info.model_name == "TV46L-1-26010003@9Hz"
    assert info.manufacturer_name == "Fluke Process Instruments"
    assert info.device_version == "1.0.8"
    # User-name offset is unestablished: never fabricated.
    assert info.user_defined_name == ""


def test_parse_discovery_ack_rejects_short():
    with pytest.raises(ValueError):
        parse_discovery_ack(b"\x00" * 100)


def test_control_switchover_writes_ccp_key_2():
    client, sock = _client_with([(_ack(1, GVCPCommand.WRITE_REG_ACK), ("192.168.42.11", 3956))])
    assert client.control_switchover(key=2) is True
    sent, _ = sock.sent[0]
    addr, value = struct.unpack(">II", sent[8:16])
    assert (addr, value) == (0x0A00, 2)


def test_heartbeat_ping_never_raises():
    client, _ = _client_with([])
    assert client.heartbeat_ping() is False  # timeout -> False, not raise


def test_close_is_idempotent():
    client, sock = _client_with([])
    client.close()
    client.close()
    assert sock.closed is True
    assert client.is_connected is False


def _broadcast_socket_factory(recv_queue):
    holder: dict = {}

    def factory():
        sock = FakeSocket(recv_queue)
        holder["sock"] = sock
        return sock

    factory.holder = holder  # type: ignore[attr-defined]
    return factory


def test_broadcast_discover_collects_with_local_ip():
    ack = _discovery_bytes()
    factory = _broadcast_socket_factory([(ack, ("192.168.42.13", 3956))])
    found = broadcast_discover(
        local_ip="192.168.42.100", timeout=1.0, socket_factory=factory
    )
    assert len(found) == 1
    info, local = found[0]
    assert info.serial_number == "HB25080011"
    assert local == "192.168.42.100"
    sock = factory.holder["sock"]
    assert sock.bound == ("192.168.42.100", 0)


def test_broadcast_discover_dedupes_and_ignores_garbage():
    ack = _discovery_bytes()
    factory = _broadcast_socket_factory(
        [
            (ack, ("192.168.42.13", 3956)),
            (ack, ("192.168.42.13", 3956)),  # duplicate serial
            (b"\x00\x01", ("192.168.42.99", 3956)),  # malformed
        ]
    )
    found = broadcast_discover(local_ip="192.168.42.100", timeout=1.0, socket_factory=factory)
    assert len(found) == 1
    assert found[0][0].serial_number == "HB25080011"


def test_local_interface_ips_returns_list():
    ips = local_interface_ips()
    assert isinstance(ips, list)
    assert all(not ip.startswith("127.") for ip in ips)


def _client_with_options(recv_queue, **kwargs) -> tuple[GVCPClient, FakeSocket]:
    holder: dict = {}

    def factory():
        sock = FakeSocket(recv_queue)
        holder["sock"] = sock
        return sock

    client = GVCPClient(
        "192.168.42.11", local_ip="192.168.42.100", socket_factory=factory, **kwargs
    )
    client.connect()
    return client, holder["sock"]


def test_pending_storm_fails_loudly_bounded():
    """A camera that PENDINGs forever must fail loudly, never hang.

    Regression: _send_recv previously looped unboundedly on repeated
    PENDING_ACK, wedging focus/NUC/heartbeat (GVSP kept streaming).
    """
    import time

    pendings = [
        (struct.pack(">HHHHH", 0, GVCPCommand.PENDING_ACK, 2, 1, 100), ("192.168.42.11", 3956))
        for _ in range(200)
    ]
    client, _ = _client_with_options(pendings, timeout=0.05, max_pending_wait_s=0.3)
    started = time.monotonic()
    with pytest.raises(GVCPError, match="PENDING"):
        client.read_register(0x20A13C)
    assert time.monotonic() - started < 5.0


def test_stale_req_id_datagram_discarded():
    """A late ack from an earlier transaction must not be accepted."""
    stale = _ack(999, GVCPCommand.READ_REG_ACK, struct.pack(">I", 0xDEAD))
    fresh = _ack(1, GVCPCommand.READ_REG_ACK, struct.pack(">I", 1000))
    client, _ = _client_with(
        [(stale, ("192.168.42.11", 3956)), (fresh, ("192.168.42.11", 3956))]
    )
    assert client.read_register(0x20A13C) == 1000


def test_short_datagram_discarded():
    """Garbage datagrams must not kill (or fake) a transaction."""
    fresh = _ack(1, GVCPCommand.READ_REG_ACK, struct.pack(">I", 2000))
    client, _ = _client_with(
        [(b"\x00\x01", ("192.168.42.11", 3956)), (fresh, ("192.168.42.11", 3956))]
    )
    assert client.read_register(0x20A140) == 2000


def test_timeout_error_carries_request_context():
    """Timeouts must identify camera + request instead of failing silently."""
    client, _ = _client_with_options([], timeout=0.05)
    with pytest.raises(GVCPError, match="192.168.42.11"):
        client.read_register(0x20A13C)
    try:
        client.read_register(0x20A13C)
    except GVCPError as exc:
        assert "req=2" in str(exc)  # req_id keeps incrementing
    else:
        raise AssertionError("expected GVCPError")
