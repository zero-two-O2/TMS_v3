"""
camera.tv46_gvsp -- pure-Python GigE Vision streaming protocol (GVSP) receiver.

Stage 8B production port of the PROVEN direct path from
``referance/tv46_standalone/thermal_monitor/acquisition/``:

* ``tv46_fusion.py`` -- GVSP header layout, single-current-block reassembly,
  latest-frame-only policy, receiver counters
* ``tv46_direct_gvsp.py`` -- Combined payload layout constants

Deliberately NOT ported: display conversion (``ir_to_display`` /
``yuyv_to_rgb`` percentile stretching and colour conversion belong to the UI
render path, never to acquisition).  This module returns RAW sensor bytes.

Datagram layout (big-endian header, 8 bytes)::

    status BE16 | block_id BE16 | packet_info BE32 | payload...

``packet_info``: bits 31-24 = format (1=leader, 2=trailer, 3=payload,
4=all-in), bits 23-0 = packet_id (ordering within the block).
``is_last`` is true for trailer / all-in formats.

Reassembly policy (matches the proven implementation):

* exactly one in-progress block (the newest forward-moving ``block_id``);
  an older/stale ``block_id`` is ignored, a newer one abandons the current
  block and counts it as incomplete;
* latest-frame-only completion queue (depth 1): if a completed frame is
  overwritten before consumption it counts as incomplete (dropped);
* no packet resend is ever requested;
* a block with no completion within ``block_timeout_s`` is abandoned and
  counted as a timeout (incomplete).

Combined TV46L payload (little-endian IR!)::

    20-byte leader | 640x480 uint16 LE IR | 640x480 YUYV VL [+ 4-byte trailer]
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GVSP wire constants
# ---------------------------------------------------------------------------

FORMAT_LEADER = 1
FORMAT_TRAILER = 2
FORMAT_PAYLOAD = 3
FORMAT_ALL_IN = 4
_LAST_FORMATS = (FORMAT_TRAILER, FORMAT_ALL_IN)

# ---------------------------------------------------------------------------
# Combined payload layout (proven direct-path values -- do not change silently)
# ---------------------------------------------------------------------------

IR_WIDTH = 640
IR_HEIGHT = 480
#: Raw IR bytes: 640 * 480 * 2 (uint16 little-endian).
IR_BYTES = IR_WIDTH * IR_HEIGHT * 2
#: Raw VL bytes: 640 * 480 YUYV (2 bytes/pixel).
VL_BYTES = IR_WIDTH * IR_HEIGHT * 2
#: Canonical VL plane geometry as parsed (YUYV bytes, one row = 1280 uint8).
#: Stage 8D: this is the SHM/payload contract for VL (width=1280, height=480,
#: dtype=uint8) so the ring round-trips the exact parsed array shape.
VL_WIDTH = 1280
VL_HEIGHT = 480
#: VL pixel format label used in StreamMetadata/GrabResult. This matches the
#: recording format vocabulary ("YUV422_8"); the packing within the family
#: is YUYV (YUY2) -- see VL_PACKING. One label end-to-end, no format change.
VL_PIXEL_FORMAT = "YUV422_8"
#: Byte packing of the VL plane (needed by any future YUV->RGB conversion;
#: kept separate so pixel_format stays within the recording vocabulary).
VL_PACKING = "YUYV"
#: GVSP leader prefix inside each reassembled block.
LEADER_BYTES = 20
#: Optional GVSP trailer suffix inside each reassembled block.
TRAILER_BYTES = 4
COMBINED_BYTES = LEADER_BYTES + IR_BYTES + VL_BYTES
COMBINED_WITH_TRAILER_BYTES = COMBINED_BYTES + TRAILER_BYTES


def parse_gvsp_header(data: bytes | memoryview) -> tuple[int, int, int, int, bool]:
    """Parse an 8-byte GVSP datagram header.

    Returns ``(status, block_id, packet_format, packet_id, is_last)``.

    Raises:
        ValueError: if fewer than 8 bytes are available.
    """
    if len(data) < 8:
        raise ValueError(f"GVSP datagram too short: {len(data)} bytes")
    status = (data[0] << 8) | data[1]
    block_id = (data[2] << 8) | data[3]
    packet_info = (data[4] << 24) | (data[5] << 16) | (data[6] << 8) | data[7]
    packet_format = (packet_info >> 24) & 0xFF
    packet_id = packet_info & 0x00FFFFFF
    return status, block_id, packet_format, packet_id, packet_format in _LAST_FORMATS


@dataclass
class CombinedFrame:
    """One parsed Combined payload: RAW sensor arrays (owned copies).

    ``ir`` is 640x480 ``uint16`` little-endian sensor data. ``vl`` is the raw
    640x480 YUYV byte plane reshaped to ``(480, 1280)`` uint8 -- raw, NOT
    colour converted. Stage 8B.1 uses only ``ir``; ``vl`` is parsed (so its
    bytes are validated) but not published into the V3 ring yet.
    """

    block_id: int
    ir: np.ndarray
    vl: np.ndarray
    received_at: float


def parse_combined_payload(payload: bytes | memoryview, block_id: int) -> CombinedFrame:
    """Split one reassembled Combined block into RAW IR + RAW VL arrays.

    Accepts the payload with or without the 4-byte trailer. The 20-byte
    leader is skipped (frame-counter extraction from the leader is a known
    unproven area -- ``block_id`` from the GVSP header is authoritative).

    Raises:
        ValueError: if the payload size matches neither expected layout.
    """
    size = len(payload)
    if size not in (COMBINED_BYTES, COMBINED_WITH_TRAILER_BYTES):
        raise ValueError(
            f"Combined payload size {size} not in "
            f"({COMBINED_BYTES}, {COMBINED_WITH_TRAILER_BYTES})"
        )
    raw = payload[:COMBINED_BYTES]
    ir = (
        np.frombuffer(raw[LEADER_BYTES : LEADER_BYTES + IR_BYTES], dtype="<u2")
        .reshape(IR_HEIGHT, IR_WIDTH)
        .copy()
    )
    vl = (
        np.frombuffer(
            raw[LEADER_BYTES + IR_BYTES : LEADER_BYTES + IR_BYTES + VL_BYTES],
            dtype=np.uint8,
        )
        .reshape(IR_HEIGHT, IR_WIDTH * 2)
        .copy()
    )
    return CombinedFrame(
        block_id=int(block_id), ir=ir, vl=vl, received_at=time.time()
    )


class GVSPBlock:
    """Reassembly state for one GVSP block (frame)."""

    def __init__(self, block_id: int) -> None:
        self.block_id = block_id
        self.packets: dict[int, bytes] = {}
        self.expected_packets: Optional[int] = None
        self.received_count = 0
        self.is_complete = False
        # Monotonic clock: wall-clock adjustments must not expire blocks.
        # (Reference used time.time(); monotonic is strictly safer here.)
        self.first_packet_time = time.monotonic()

    def add_packet(self, block_id: int, packet_id: int, payload, is_last: bool) -> bool:
        """Store one packet. Returns False for wrong-block or duplicates."""
        if block_id != self.block_id or packet_id in self.packets:
            return False
        self.packets[packet_id] = bytes(payload)
        self.received_count += 1
        if is_last:
            self.expected_packets = packet_id + 1
        if self.expected_packets is not None and self.received_count >= self.expected_packets:
            self.is_complete = True
        return True

    def get_missing_packets(self) -> list[int]:
        """Packet ids expected but not yet received (empty if unknown)."""
        if self.expected_packets is None:
            return []
        return [i for i in range(self.expected_packets) if i not in self.packets]

    def reassemble(self) -> Optional[bytes]:
        """Concatenate payloads in packet order, or None if incomplete."""
        if not self.is_complete or self.expected_packets is None:
            return None
        try:
            return b"".join(self.packets[i] for i in range(self.expected_packets))
        except KeyError:
            return None


class GVSPReceiver:
    """UDP GVSP receiver with single-current-block reassembly.

    Runs a daemon receive thread writing into a reusable 9000-byte buffer
    (``recvfrom_into`` -- no per-datagram allocation). Completed blocks are
    handed out newest-first via :meth:`get_block_with_id`; at most one
    completed block is retained (latest-frame-only).
    """

    def __init__(
        self,
        local_ip: str = "0.0.0.0",
        port: int = 0,
        buffer_size: int = 16 * 1024 * 1024,
        block_timeout_s: float = 1.0,
    ) -> None:
        self.local_ip = local_ip
        self.port = port
        self.buffer_size = buffer_size
        self.block_timeout_s = block_timeout_s
        self._socket: Optional[socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._current_block: Optional[GVSPBlock] = None
        self._current_block_id: Optional[int] = None
        self._completed: list[tuple[int, bytes]] = []
        self._completed_lock = threading.Lock()
        self._block_lock = threading.Lock()
        self._receive_buffer = bytearray(9000)
        self._last_cleanup = time.monotonic()
        self._last_packet_id: dict[int, int] = {}
        # Counters (Stage 8B.5 measurement surface).
        self.packets_received = 0
        self.packets_dropped = 0
        self.blocks_started = 0
        self.blocks_completed = 0
        self.blocks_incomplete = 0
        self.block_timeouts = 0
        self.duplicate_packets = 0
        self.out_of_order_packets = 0
        self.bytes_received = 0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> int:
        """Bind the UDP socket and start the receive thread. Returns the
        bound (ephemeral) port so the camera can be pointed at it (SCP0)."""
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.buffer_size)
        self._socket.bind((self.local_ip, self.port))
        self.buffer_size = self._socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        self._socket.settimeout(0.01)
        self.port = self._socket.getsockname()[1]
        self._running = True
        self._thread = threading.Thread(
            target=self._receive_loop, name="gvsp-receiver", daemon=True
        )
        self._thread.start()
        logger.info("GVSP receiver started on %s:%d", self.local_ip, self.port)
        return self.port

    def stop(self) -> None:
        """Stop the receive thread and close the socket (idempotent)."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    # -- receive path -------------------------------------------------------

    def _receive_loop(self) -> None:
        while self._running:
            try:
                received, _ = self._socket.recvfrom_into(self._receive_buffer)
                self._process_buffer(received)
                if time.monotonic() - self._last_cleanup >= 0.25:
                    self._cleanup_old_blocks()
                    self._last_cleanup = time.monotonic()
            except socket.timeout:
                self._cleanup_old_blocks()
                continue
            except OSError:
                if self._running:
                    logger.exception("GVSP socket error")
                break
            except Exception:
                # A malformed datagram must never kill the receiver thread.
                self.packets_dropped += 1
                logger.exception("GVSP packet processing error")

    def _process_buffer(self, length: int) -> None:
        self.packets_received += 1
        self.bytes_received += length
        if length < 8:
            self.packets_dropped += 1
            return
        view = memoryview(self._receive_buffer)[:length]
        try:
            _, block_id, _, packet_id, is_last = parse_gvsp_header(view)
        except ValueError:
            self.packets_dropped += 1
            return
        last = self._last_packet_id.get(block_id, -1)
        if packet_id < last:
            self.out_of_order_packets += 1
        self._last_packet_id[block_id] = packet_id

        if self._current_block_id is None:
            self._current_block_id = block_id
            self._current_block = GVSPBlock(block_id)
            self.blocks_started += 1
        elif block_id != self._current_block_id:
            distance = (block_id - self._current_block_id) & 0xFFFF
            if not 0 < distance < 0x8000:
                return  # stale retransmit / wraparound ambiguity: ignore
            self.blocks_incomplete += 1
            self._current_block_id = block_id
            self._current_block = GVSPBlock(block_id)
            self.blocks_started += 1
        block = self._current_block
        if block is None:
            return
        if not block.add_packet(block_id, packet_id, view[8:], is_last):
            self.duplicate_packets += 1
            return
        if block.is_complete:
            reassembled = block.reassemble()
            if reassembled is not None:
                self.blocks_completed += 1
                with self._completed_lock:
                    if self._completed:
                        # Latest-frame-only: drop the unconsumed frame.
                        self._completed.pop(0)
                        self.blocks_incomplete += 1
                    self._completed.append((block_id, reassembled))
            self._current_block = None
            self._current_block_id = None
            self._last_packet_id.pop(block_id, None)

    def _cleanup_old_blocks(self) -> None:
        now = time.monotonic()
        with self._block_lock:
            block = self._current_block
            if (
                block is not None
                and now - block.first_packet_time >= self.block_timeout_s
            ):
                self.block_timeouts += 1
                self.blocks_incomplete += 1
                logger.warning(
                    "Block %d timed out, missing packets: %s",
                    block.block_id,
                    block.get_missing_packets(),
                )
                self._last_packet_id.pop(block.block_id, None)
                self._current_block = None
                self._current_block_id = None

    # -- consumer API ---------------------------------------------------------

    def get_block_with_id(self, timeout: float = 5.0) -> Optional[tuple[int, bytes]]:
        """Return the latest completed ``(block_id, payload)``, or None on
        timeout. Never synthesises frames -- None means no camera data."""
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            with self._completed_lock:
                if self._completed:
                    block_id, payload = self._completed.pop(0)
                    return int(block_id), payload
            time.sleep(0.001)
        return None

    def get_stats(self) -> dict:
        """Snapshot of receiver counters for FPS/loss measurement."""
        with self._block_lock:
            active = 1 if self._current_block is not None else 0
        with self._completed_lock:
            completed = len(self._completed)
        return {
            "active_blocks": active,
            "completed_blocks": completed,
            "port": self.port,
            "packets_received": self.packets_received,
            "packets_dropped": self.packets_dropped,
            "blocks_started": self.blocks_started,
            "blocks_completed": self.blocks_completed,
            "blocks_incomplete": self.blocks_incomplete,
            "block_timeouts": self.block_timeouts,
            "duplicate_packets": self.duplicate_packets,
            "out_of_order_packets": self.out_of_order_packets,
            "bytes_received": self.bytes_received,
            "rcvbuf": self.buffer_size,
        }


__all__ = [
    "COMBINED_BYTES",
    "COMBINED_WITH_TRAILER_BYTES",
    "FORMAT_ALL_IN",
    "FORMAT_LEADER",
    "FORMAT_PAYLOAD",
    "FORMAT_TRAILER",
    "GVSPBlock",
    "GVSPReceiver",
    "CombinedFrame",
    "IR_BYTES",
    "IR_HEIGHT",
    "IR_WIDTH",
    "LEADER_BYTES",
    "TRAILER_BYTES",
    "VL_BYTES",
    "VL_HEIGHT",
    "VL_PACKING",
    "VL_PIXEL_FORMAT",
    "VL_WIDTH",
    "parse_combined_payload",
    "parse_gvsp_header",
]
