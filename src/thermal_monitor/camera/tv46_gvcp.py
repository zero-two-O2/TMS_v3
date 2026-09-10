"""
camera.tv46_gvcp -- pure-Python GigE Vision control protocol (GVCP) client.

Stage 8B production port of the PROVEN direct path from
``referance/tv46_standalone/thermal_monitor/acquisition/``:

* ``tv46_direct_gvsp.py`` -- register map, connection sequence values
* ``tv46_fusion.py`` -- GVCP wire encoding (header, PENDING_ACK handling)

Deliberately NOT ported: the legacy ``TV46FusionCamera`` XML-fetch path
(``get_xml_url`` / ``fetch_device_xml`` / ``parse_xml_features`` and all
candidate-register fallbacks).  The direct path uses fixed, hardware-proven
register addresses below.

Wire format (big-endian)::

    GVCP request:  0x42 0x01 | command BE16 | length BE16 | req_id BE16 | payload
    GVCP ack:      status BE16 | ack_cmd BE16 | length BE16 | req_id BE16 | payload

All multi-byte register values are big-endian on the wire.  The IR image
payload itself (GVSP) is little-endian -- see :mod:`tv46_gvsp`.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Transport constants (proven direct-path values -- do not change silently)
# ---------------------------------------------------------------------------

#: UDP port the TV46L listens on for GVCP control traffic.
GVCP_PORT = 3956

#: GVCP magic bytes at the start of every request header.
GVCP_MAGIC = (0x42, 0x01)

# ---------------------------------------------------------------------------
# Proven TV46L register map (hardware-verified, see Stage 8A audit)
# ---------------------------------------------------------------------------

#: Control-channel privilege register; write ``CCP_KEY`` to take control.
REG_CCP = 0x0A00
#: Value written to REG_CCP to acquire GVCP control (direct path).
CCP_KEY = 2

#: Heartbeat timeout register; camera drops control if quiet for longer.
REG_HEARTBEAT_TIMEOUT = 0x0938
#: Heartbeat timeout written at connect (ms). Proven direct-path value.
HEARTBEAT_TIMEOUT_MS = 30000

#: Stream destination IP (SCDA0) and stream port (SCP0).
REG_SCDA0 = 0x0D18
REG_SCP0 = 0x0D00
#: GVSP packet size register. Proven value is 1500 -- forcing 8192 degrades
#: the stream to ~2.5 fps or stops it. The driver keeps the current value.
REG_SCPS0 = 0x0D04
SCPS0_KEEP_VALUE = 1500

#: Packet-delay (GevSCPD) pacing register. Proven value 10000 ticks.
REG_PACKET_DELAY = 0x0D08
PACKET_DELAY_TICKS = 10000

#: Combined IR+VL stream selector. Proven value 3.
REG_FUSION_SELECTOR = 0x10A110
FUSION_COMBINED_VALUE = 3

#: Acquisition start (write-only command register; reads fail -- expected).
REG_ACQUISITION_START = 0x10A104

#: Frame-rate target register (write-only).
REG_FRAME_RATE = 0x20A154
FRAME_RATE_FPS = 9

#: Manual NUC / fine-offset command (write-only). Value 11 executes the
#: fine-offset correction immediately. This is the production NUC command
#: for the custom GVCP/GVSP path (Stage 8G final).
REG_NUC_COMMAND = 0x20A134
NUC_EXECUTE_FINE_OFFSETS = 11

#: Focus motor position registers (GenICam FLK_TI_ControlFeatures base
#: 0x20A114, focus block offset 9*4). Set is WO integer mm; current/min/max
#: are RO readback in mm.
REG_FOCUS_SET = 0x20A138
REG_FOCUS_CURRENT = 0x20A13C
REG_FOCUS_MIN = 0x20A140
REG_FOCUS_MAX = 0x20A144
#: Motor settle poll budget after a focus write (s).
FOCUS_SETTLE_TIMEOUT_S = 5.0

#: Device temperature telemetry (FLK_TI_InfoRegisters base 0xA000 + 172),
#: read-only float32 big-endian, degrees C. Display telemetry only.
REG_DEVICE_TEMP_CURRENT = 0xA0AC
DEVICE_TEMP_POLL_S = 15.0


class GVCPCommand(IntEnum):
    """GVCP command / acknowledgement identifiers used by this driver."""

    DISCOVERY = 0x0002
    DISCOVERY_ACK = 0x0003
    READ_REG = 0x0080
    READ_REG_ACK = 0x0081
    WRITE_REG = 0x0082
    WRITE_REG_ACK = 0x0083
    READ_MEM = 0x0084
    READ_MEM_ACK = 0x0085
    PENDING_ACK = 0x0089


@dataclass
class TV46DeviceInfo:
    """Identity block returned by GVCP discovery for one camera."""

    ip_address: str = ""
    mac_address: str = ""
    serial_number: str = ""
    model_name: str = ""
    manufacturer_name: str = ""
    device_version: str = ""
    user_defined_name: str = ""


def build_request(command: int, payload: bytes, req_id: int) -> bytes:
    """Encode one GVCP request datagram (pure function, unit-testable)."""
    header = struct.pack(
        ">BBHHH", GVCP_MAGIC[0], GVCP_MAGIC[1], command, len(payload), req_id & 0xFFFF
    )
    return header + bytes(payload)


def parse_ack_header(data: bytes) -> tuple[int, int, int]:
    """Decode a GVCP ack header into ``(status, ack_cmd, req_id)``.

    Raises:
        ValueError: if the datagram is shorter than the 8-byte header.
    """
    if len(data) < 8:
        raise ValueError(f"GVCP ack too short: {len(data)} bytes")
    status = struct.unpack(">H", data[0:2])[0]
    ack_cmd = struct.unpack(">H", data[2:4])[0]
    req_id = struct.unpack(">H", data[6:8])[0]
    return status, ack_cmd, req_id


def parse_discovery_ack(data: bytes, sender_ip: str = "") -> TV46DeviceInfo:
    """Parse a DISCOVERY_ACK datagram into :class:`TV46DeviceInfo`.

    Field offsets are HARDWARE-MEASURED on the TV46L fleet (Stage 8D,
    verified against serials/MACs on .11 and .13 -- e.g. serial
    ``HB25080011`` at payload[216:248]). They supersede the ported
    standalone guesses.

    Raises:
        ValueError: if the datagram is shorter than the GigE discovery
            payload (248 bytes total).
        RuntimeError: if the device reports a non-zero status.
    """
    if len(data) < 248:
        raise ValueError(f"Discovery ack too short: {len(data)}")
    status = struct.unpack(">H", data[0:2])[0]
    if status != 0:
        raise RuntimeError(f"Discovery failed: status={status:#06x}")
    payload = data[8:]

    def _field(offset: int, size: int = 32) -> str:
        raw = payload[offset : offset + size]
        return raw.split(b"\x00")[0].decode("ascii", errors="ignore").strip()

    mac = ":".join(f"{b:02x}" for b in payload[10:16])
    current_ip = ".".join(str(b) for b in payload[28:32])
    manufacturer = _field(72)
    model = _field(104)
    device_version = _field(136)
    serial = _field(216)
    # User-name offset is not established on this fleet -- never fabricate.
    user_name = ""
    return TV46DeviceInfo(
        ip_address=sender_ip or current_ip,
        mac_address=mac,
        serial_number=serial,
        model_name=model,
        manufacturer_name=manufacturer,
        device_version=device_version,
        user_defined_name=user_name,
    )


class GVCPError(RuntimeError):
    """A GVCP transaction failed (timeout, status, or ack mismatch)."""


class GVCPClient:
    """Minimal GVCP control client for one TV46L camera.

    Owns exactly one UDP socket bound to ``local_ip`` (ephemeral port).
    All transactions are serialised with an RLock so the heartbeat thread
    and the acquisition thread can share one client safely.

    ``socket_factory`` is injectable for unit tests (must return an object
    with the ``socket.socket`` UDP subset used here).
    """

    def __init__(
        self,
        camera_ip: str,
        local_ip: str = "0.0.0.0",
        port: int = GVCP_PORT,
        timeout: float = 2.0,
        socket_factory: Optional[Callable[[], socket.socket]] = None,
    ) -> None:
        self.camera_ip = camera_ip
        self.port = port
        self.timeout = timeout
        self._local_ip = local_ip
        self._socket_factory = socket_factory
        self._socket: Optional[socket.socket] = None
        self._req_id = 0
        self._lock = threading.RLock()

    # -- lifecycle ------------------------------------------------------

    def connect(self) -> bool:
        """Create and bind the UDP control socket."""
        if self._socket_factory is not None:
            self._socket = self._socket_factory()
        else:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.settimeout(self.timeout)
        self._socket.bind((self._local_ip, 0))
        return True

    def close(self) -> None:
        """Close the control socket (idempotent)."""
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    @property
    def is_connected(self) -> bool:
        """True while the control socket is open."""
        return self._socket is not None

    # -- low-level transactions ------------------------------------------

    def _next_req_id(self) -> int:
        self._req_id = (self._req_id + 1) & 0xFFFF
        return self._req_id

    def _send_recv(self, data: bytes, expected_ack: Optional[int] = None) -> bytes:
        """Send one request and wait for its acknowledgement.

        Handles PENDING_ACK per GigE Vision: the device may reply PENDING
        with a wait hint before the final ack arrives.
        """
        if self._socket is None:
            raise GVCPError("Not connected")
        req_id = struct.unpack(">H", data[6:8])[0] if len(data) >= 8 else None
        with self._lock:
            self._socket.sendto(data, (self.camera_ip, self.port))
            deadline = time.time() + self.timeout + 2.0
            while True:
                remaining = max(0.5, deadline - time.time())
                self._socket.settimeout(remaining)
                try:
                    resp, _ = self._socket.recvfrom(8192)
                except socket.timeout as exc:
                    raise GVCPError("GVCP timeout waiting for ack") from exc
                status, ack_cmd, ack_id = parse_ack_header(resp)
                if ack_cmd == GVCPCommand.PENDING_ACK:
                    pending_ms = 500
                    if len(resp) >= 10:
                        try:
                            pending_ms = struct.unpack(">H", resp[8:10])[0] or 500
                        except struct.error:
                            pass
                    time.sleep(min(max(pending_ms, 100), 5000) / 1000.0)
                    continue
                if expected_ack is not None and ack_cmd != expected_ack:
                    raise GVCPError(
                        f"Expected ack {expected_ack:#06x}, got {ack_cmd:#06x}"
                    )
                if status != 0:
                    raise GVCPError(
                        f"GVCP command failed: status={status:#06x} "
                        f"ack={ack_cmd:#06x} req={req_id}"
                    )
                if req_id is not None and ack_id != req_id:
                    logger.debug("GVCP ack id mismatch req=%s ack=%s", req_id, ack_id)
                return resp

    # -- register access --------------------------------------------------

    def read_register(self, address: int) -> int:
        """Read one 32-bit control register (big-endian on the wire)."""
        payload = struct.pack(">I", address)
        cmd = build_request(GVCPCommand.READ_REG, payload, self._next_req_id())
        resp = self._send_recv(cmd, GVCPCommand.READ_REG_ACK)
        if len(resp) < 12:
            raise GVCPError(f"READ_REG ack too short: {len(resp)}")
        return struct.unpack(">I", resp[8:12])[0]

    def write_register(self, address: int, value: int) -> bool:
        """Write one 32-bit control register. Returns False (no raise) on
        transport failure so callers can implement retry/fallback policy."""
        payload = struct.pack(">II", address, value & 0xFFFFFFFF)
        cmd = build_request(GVCPCommand.WRITE_REG, payload, self._next_req_id())
        try:
            resp = self._send_recv(cmd, GVCPCommand.WRITE_REG_ACK)
            return struct.unpack(">H", resp[0:2])[0] == 0
        except GVCPError:
            return False

    def read_memory(self, address: int, length: int) -> bytes:
        """Read ``length`` bytes from device memory, chunked to 512 B per
        transaction (GigE Vision device limit)."""
        if length > 512:
            out = bytearray()
            for offset in range(0, length, 512):
                out.extend(self.read_memory(address + offset, min(512, length - offset)))
            return bytes(out)
        payload = struct.pack(">II", address, length)
        cmd = build_request(GVCPCommand.READ_MEM, payload, self._next_req_id())
        resp = self._send_recv(cmd, GVCPCommand.READ_MEM_ACK)
        if len(resp) < 12:
            raise GVCPError(f"READ_MEM ack too short: {len(resp)}")
        data = resp[12 : 12 + length]
        if len(data) < length:
            raise GVCPError(
                f"READ_MEM short data: expected {length}, got {len(data)}"
            )
        return data

    # -- camera operations ------------------------------------------------

    def control_switchover(self, key: int = CCP_KEY) -> bool:
        """Take GVCP control-channel privilege (CCP)."""
        return self.write_register(REG_CCP, key)

    def release_control(self) -> bool:
        """Release GVCP control-channel privilege (best-effort)."""
        try:
            return self.write_register(REG_CCP, 0)
        except GVCPError:
            return False

    def heartbeat_ping(self) -> bool:
        """Keep-alive read of the heartbeat register. Returns False (never
        raises) so the heartbeat thread can never kill acquisition."""
        try:
            self.read_register(REG_HEARTBEAT_TIMEOUT)
            return True
        except Exception:
            return False

    def discover_once(self) -> TV46DeviceInfo:
        """Unicast DISCOVERY handshake against the configured camera IP."""
        if self._socket is None:
            raise GVCPError("Not connected")
        cmd = build_request(GVCPCommand.DISCOVERY, b"", self._next_req_id())
        with self._lock:
            self._socket.sendto(cmd, (self.camera_ip, self.port))
            resp, addr = self._socket.recvfrom(4096)
        return parse_discovery_ack(resp, addr[0])


def local_interface_ips() -> list[str]:
    """Enumerate local IPv4 interface addresses (no hardcoding).

    Used to select the interface(s) for GVCP broadcast discovery and to
    report which local interface each camera was seen on.
    """
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def routed_local_ip(target_ip: str) -> str:
    """Return the local address that routes to ``target_ip`` (no traffic)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((target_ip, GVCP_PORT))
        return probe.getsockname()[0]
    finally:
        probe.close()


def broadcast_discover(
    local_ip: str = "0.0.0.0",
    broadcast: str = "255.255.255.255",
    port: int = GVCP_PORT,
    timeout: float = 2.0,
    socket_factory: Optional[Callable[[], socket.socket]] = None,
) -> list[tuple[TV46DeviceInfo, str]]:
    """Broadcast one DISCOVERY and collect ``(info, local_ip)`` answers.

    ``socket_factory`` injects the UDP socket for unit tests. Duplicate
    responders (same serial, else same IP) are suppressed, keeping the
    first answer. Malformed acks never abort the sweep.
    """
    sock = socket_factory() if socket_factory is not None else socket.socket(
        socket.AF_INET, socket.SOCK_DGRAM
    )
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)
        sock.bind((local_ip, 0))
        sock.sendto(build_request(GVCPCommand.DISCOVERY, b"", 1), (broadcast, port))
        found: list[tuple[TV46DeviceInfo, str]] = []
        seen: set[str] = set()
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                resp, addr = sock.recvfrom(4096)
            except socket.timeout:
                break
            try:
                info = parse_discovery_ack(resp, addr[0])
            except (ValueError, RuntimeError):
                continue
            key = info.serial_number or info.ip_address
            if not key or key in seen:
                continue
            seen.add(key)
            found.append((info, local_ip))
        return found
    finally:
        sock.close()


def discover_devices(
    local_ip: str = "0.0.0.0",
    broadcast: str = "255.255.255.255",
    port: int = GVCP_PORT,
    timeout: float = 2.0,
) -> list[TV46DeviceInfo]:
    """Broadcast GVCP discovery and collect all responding cameras.

    Stage 8B.11 hardening target: this helper is implemented and unit
    tested here but NOT yet wired into production discovery -- HALCON
    discovery remains authoritative until hardware validation passes.
    Duplicate responders (same serial) are suppressed.
    """
    return [
        info
        for info, _local in broadcast_discover(
            local_ip=local_ip, broadcast=broadcast, port=port, timeout=timeout
        )
    ]


__all__ = [
    "ACQUISITION_START_VALUE",
    "CCP_KEY",
    "DEVICE_TEMP_POLL_S",
    "FOCUS_SETTLE_TIMEOUT_S",
    "FRAME_RATE_FPS",
    "FUSION_COMBINED_VALUE",
    "GVCPCommand",
    "GVCPError",
    "GVCP_PORT",
    "GVCPClient",
    "HEARTBEAT_TIMEOUT_MS",
    "NUC_EXECUTE_FINE_OFFSETS",
    "PACKET_DELAY_TICKS",
    "REG_ACQUISITION_START",
    "REG_CCP",
    "REG_DEVICE_TEMP_CURRENT",
    "REG_FOCUS_CURRENT",
    "REG_FOCUS_MAX",
    "REG_FOCUS_MIN",
    "REG_FOCUS_SET",
    "REG_FRAME_RATE",
    "REG_FUSION_SELECTOR",
    "REG_HEARTBEAT_TIMEOUT",
    "REG_NUC_COMMAND",
    "REG_PACKET_DELAY",
    "REG_SCDA0",
    "REG_SCP0",
    "REG_SCPS0",
    "SCPS0_KEEP_VALUE",
    "TV46DeviceInfo",
    "broadcast_discover",
    "build_request",
    "discover_devices",
    "local_interface_ips",
    "parse_ack_header",
    "parse_discovery_ack",
    "routed_local_ip",
]

#: Value written to REG_ACQUISITION_START to begin streaming.
ACQUISITION_START_VALUE = 1
