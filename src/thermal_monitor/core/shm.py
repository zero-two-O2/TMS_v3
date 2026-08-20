"""
Shared-memory ring buffer for frame transport (ADR-003 Phase 1).

Implements an in-process shared-memory ring buffer using
multiprocessing.shared_memory.SharedMemory. Single-producer, multi-consumer
with per-consumer independent position tracking, pinning for tear-free reads,
and non-blocking publication.
"""

from __future__ import annotations

import logging
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from multiprocessing.shared_memory import SharedMemory
    from thermal_monitor.core.frame import Frame, FrameDescriptor, FramePayload, StreamMetadata, SyncInfo, SyncStatus


logger = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────────────────

MAGIC = b"TMS_RING"
LAYOUT_MAJOR = 1
LAYOUT_MINOR = 0
HEADER_ALIGN = 64
SLOT_ALIGN = 64

# Slot states
class SlotState(IntEnum):
    EMPTY = 0
    WRITING = 1
    PUBLISHED = 2
    INVALID = 3

# ─── Data Specifications ────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PayloadSpec:
    """Specification for one payload stream (thermal or visible)."""
    width: int
    height: int
    dtype: np.dtype
    bytes_per_frame: int

    @property
    def aligned_bytes(self) -> int:
        return (self.bytes_per_frame + HEADER_ALIGN - 1) & ~(HEADER_ALIGN - 1)


@dataclass(frozen=True, slots=True)
class RingConfig:
    """Configuration for creating a ring buffer."""
    camera_id: str
    thermal_spec: PayloadSpec
    visible_spec: PayloadSpec | None
    depth: int

    def shm_name(self) -> str:
        return f"TMS_{self.camera_id}_frames"

    def slot_size(self) -> int:
        thermal_bytes = self.thermal_spec.aligned_bytes
        visible_bytes = self.visible_spec.aligned_bytes if self.visible_spec else 0
        descriptor_bytes = 4096  # fixed descriptor region
        slot_header_bytes = 256  # fixed slot header
        return (slot_header_bytes + descriptor_bytes + thermal_bytes + visible_bytes + SLOT_ALIGN - 1) & ~(SLOT_ALIGN - 1)

    def total_size(self) -> int:
        slot_sz = self.slot_size()
        header_sz = self.header_size()
        return header_sz + slot_sz * self.depth

    def header_size(self) -> int:
        return (1024 + HEADER_ALIGN - 1) & ~(HEADER_ALIGN - 1)


# ─── Binary Layout Helpers ──────────────────────────────────────────────────

def _align_offset(offset: int, alignment: int) -> int:
    return (offset + alignment - 1) & ~(alignment - 1)


@dataclass(frozen=True, slots=True)
class RingLayout:
    """Precomputed byte offsets for the ring buffer layout."""
    header_offset: int
    header_size: int
    slot_offsets: list[int]
    slot_size: int
    slot_header_offset: int
    slot_header_size: int
    descriptor_offset: int
    descriptor_size: int
    thermal_offset: int
    thermal_size: int
    visible_offset: int
    visible_size: int

    @classmethod
    def from_config(cls, config: RingConfig) -> RingLayout:
        header_size = config.header_size()
        slot_size = config.slot_size()
        thermal_bytes = config.thermal_spec.aligned_bytes
        visible_bytes = config.visible_spec.aligned_bytes if config.visible_spec else 0

        # Header at start
        header_offset = 0

        # First slot starts after header
        first_slot = _align_offset(header_size, SLOT_ALIGN)

        # Slot internal layout
        slot_header_offset = 0
        slot_header_size = 256
        descriptor_offset = _align_offset(slot_header_size, 8)
        descriptor_size = 4096
        thermal_offset = _align_offset(descriptor_offset + descriptor_size, HEADER_ALIGN)
        thermal_size = thermal_bytes
        visible_offset = _align_offset(thermal_offset + thermal_size, HEADER_ALIGN)
        visible_size = visible_bytes

        # Verify slot size matches
        computed_slot_size = _align_offset(visible_offset + visible_size, SLOT_ALIGN)
        assert computed_slot_size == slot_size, f"Slot size mismatch: {computed_slot_size} != {slot_size}"

        slot_offsets = [first_slot + i * slot_size for i in range(config.depth)]

        return cls(
            header_offset=header_offset,
            header_size=header_size,
            slot_offsets=slot_offsets,
            slot_size=slot_size,
            slot_header_offset=slot_header_offset,
            slot_header_size=slot_header_size,
            descriptor_offset=descriptor_offset,
            descriptor_size=descriptor_size,
            thermal_offset=thermal_offset,
            thermal_size=thermal_size,
            visible_offset=visible_offset,
            visible_size=visible_size,
        )

    def slot_base(self, index: int) -> int:
        return self.slot_offsets[index]

    def slot_header_addr(self, index: int) -> int:
        return self.slot_base(index) + self.slot_header_offset

    def descriptor_addr(self, index: int) -> int:
        return self.slot_base(index) + self.descriptor_offset

    def thermal_addr(self, index: int) -> int:
        return self.slot_base(index) + self.thermal_offset

    def visible_addr(self, index: int) -> int:
        return self.slot_base(index) + self.visible_offset


# ─── Ring Header (control block) ────────────────────────────────────────────

# Header format: magic(8) + layout_major(2) + layout_minor(2) + camera_id(64) +
# depth(4) + slot_size(8) + producer_sequence(8) + producer_head_slot(4) +
# owner_pid(4) + created_monotonic(8) + closed(1) + reserved(31) = ~140 bytes
# Padded to 1024 bytes for future expansion.

_RING_HEADER_FORMAT = "<8sHH64sQQQiId?"  # ~100 bytes (head_slot as signed int, created as double)
_RING_HEADER_SIZE = struct.calcsize(_RING_HEADER_FORMAT)


@dataclass(slots=True)
class RingHeader:
    """Mutable ring buffer header (control block)."""
    magic: bytes = MAGIC
    layout_major: int = LAYOUT_MAJOR
    layout_minor: int = LAYOUT_MINOR
    camera_id: str = ""
    depth: int = 0
    slot_size: int = 0
    producer_sequence: int = 0
    producer_head_slot: int = -1
    owner_pid: int = 0
    created_monotonic: float = 0.0
    closed: bool = False

    def pack(self) -> bytes:
        cam_bytes = self.camera_id.encode("ascii")[:64].ljust(64, b"\x00")
        return struct.pack(
            _RING_HEADER_FORMAT,
            self.magic,
            self.layout_major,
            self.layout_minor,
            cam_bytes,
            self.depth,
            self.slot_size,
            self.producer_sequence,
            self.producer_head_slot,
            self.owner_pid,
            self.created_monotonic,
            self.closed,
        )

    @classmethod
    def unpack(cls, data: bytes) -> RingHeader:
        magic, major, minor, cam_bytes, depth, slot_size, prod_seq, head_slot, pid, created, closed = struct.unpack(_RING_HEADER_FORMAT, data)
        camera_id = cam_bytes.decode("ascii").rstrip("\x00")
        return cls(
            magic=magic,
            layout_major=major,
            layout_minor=minor,
            camera_id=camera_id,
            depth=depth,
            slot_size=slot_size,
            producer_sequence=prod_seq,
            producer_head_slot=head_slot,
            owner_pid=pid,
            created_monotonic=created,
            closed=closed,
        )

    def validate(self, expected_camera_id: str, expected_depth: int, expected_slot_size: int) -> None:
        if self.magic != MAGIC:
            raise ValueError(f"Invalid magic: {self.magic!r} != {MAGIC!r}")
        if self.layout_major != LAYOUT_MAJOR or self.layout_minor != LAYOUT_MINOR:
            raise ValueError(f"Layout version mismatch: {self.layout_major}.{self.layout_minor} != {LAYOUT_MAJOR}.{LAYOUT_MINOR}")
        if self.camera_id != expected_camera_id:
            raise ValueError(f"Camera ID mismatch: {self.camera_id} != {expected_camera_id}")
        if self.depth != expected_depth:
            raise ValueError(f"Depth mismatch: {self.depth} != {expected_depth}")
        if self.slot_size != expected_slot_size:
            raise ValueError(f"Slot size mismatch: {self.slot_size} != {expected_slot_size}")
        if self.closed:
            raise ValueError("Ring buffer is closed")


# ─── Slot Header ────────────────────────────────────────────────────────────

# Slot header format:
# state(1) + generation(8) + sequence(8) + descriptor_len(4) + flags(4) + reserved(231) = 256 bytes
_SLOT_HEADER_FORMAT = "<BQQII231x"
_SLOT_HEADER_SIZE = struct.calcsize(_SLOT_HEADER_FORMAT)


@dataclass(slots=True)
class SlotHeader:
    """Mutable slot header."""
    state: SlotState = SlotState.EMPTY
    generation: int = 0
    sequence: int = 0
    descriptor_len: int = 0
    flags: int = 0

    def pack(self) -> bytes:
        return struct.pack(
            _SLOT_HEADER_FORMAT,
            self.state.value,
            self.generation,
            self.sequence,
            self.descriptor_len,
            self.flags,
        )

    @classmethod
    def unpack(cls, data: bytes) -> SlotHeader:
        state_val, generation, sequence, desc_len, flags = struct.unpack(_SLOT_HEADER_FORMAT, data)
        return cls(
            state=SlotState(state_val),
            generation=generation,
            sequence=sequence,
            descriptor_len=desc_len,
            flags=flags,
        )


# ─── Descriptor Encoding ────────────────────────────────────────────────────

# Binary descriptor format (versioned, fixed-size 4096 bytes):
# version_major(2) + version_minor(2) + camera_id(64) + sequence(8) +
# timestamp(8) + monotonic_timestamp(8) +
# thermal_present(1) + thermal_width(4) + thermal_height(4) + thermal_pixel_format(32) +
# thermal_bits_per_channel(2) + thermal_dtype(16) + thermal_byte_count(8) +
# thermal_sequence(8) + thermal_timestamp(8) + thermal_mono_timestamp(8) + thermal_hw_timestamp(8) +
# visible_present(1) + visible_width(4) + visible_height(4) + visible_pixel_format(32) +
# visible_bits_per_channel(2) + visible_dtype(16) + visible_byte_count(8) +
# visible_sequence(8) + visible_timestamp(8) + visible_mono_timestamp(8) + visible_hw_timestamp(8) +
# sync_status(1) + sync_time_delta(8) +
# grab_started(8) + grab_completed(8) + converted_at(8) + grab_duration_s(8) +
# packet_stats_present(1) + packets_seen(8) + packets_lost(8) + blocks_incomplete(8) + blocks_discarded(8) +
# payload_thermal_offset(8) + payload_thermal_size(8) + payload_visible_offset(8) + payload_visible_size(8) +
# frame_valid(1) + reserved(pad to 4096)
#
# Total well under 4096 bytes.

_DESC_VERSION_MAJOR = 1
_DESC_VERSION_MINOR = 0
_DESC_FORMAT = (
    "<HH"           # version major, minor
    "64s"           # camera_id
    "Q"             # sequence
    "dd"            # timestamp, monotonic_timestamp
    "?"             # thermal_present
    "II"            # thermal_width, thermal_height
    "32s"           # thermal_pixel_format
    "H"             # thermal_bits_per_channel
    "16s"           # thermal_dtype
    "Q"             # thermal_byte_count
    "Q"             # thermal_sequence
    "d"             # thermal_timestamp
    "d"             # thermal_mono_timestamp
    "d"             # thermal_hw_timestamp
    "?"             # visible_present
    "II"            # visible_width, visible_height
    "32s"           # visible_pixel_format
    "H"             # visible_bits_per_channel
    "16s"           # visible_dtype
    "Q"             # visible_byte_count
    "Q"             # visible_sequence
    "d"             # visible_timestamp
    "d"             # visible_mono_timestamp
    "d"             # visible_hw_timestamp
    "B"             # sync_status (as int)
    "d"             # sync_time_delta
    "dddd"          # grab_started, grab_completed, converted_at, grab_duration_s
    "?"             # packet statistics present
    "QQQQ"          # packet statistics
    "QQQQ"          # thermal_offset, thermal_size, visible_offset, visible_size
    "?"             # frame_valid
)
_DESC_SIZE = struct.calcsize(_DESC_FORMAT)
assert _DESC_SIZE <= 4096, f"Descriptor size {_DESC_SIZE} exceeds 4096"


def encode_descriptor(
    frame: Frame,
    layout: RingLayout,
    slot_index: int,
) -> bytes:
    """Encode a Frame into the binary descriptor format."""
    desc = frame.descriptor
    thermal = desc.thermal
    visible = desc.visible
    sync = desc.sync

    cam_bytes = desc.camera_id.encode("ascii")[:64].ljust(64, b"\x00")

    def enc_str(s: str | None, length: int) -> bytes:
        return (s or "").encode("ascii")[:length].ljust(length, b"\x00")

    thermal_pix_fmt = enc_str(thermal.pixel_format, 32)
    thermal_dtype = enc_str(thermal.dtype, 16)
    visible_pix_fmt = enc_str(visible.pixel_format, 32)
    visible_dtype = enc_str(visible.dtype, 16)

    # Payload offsets within the slot
    thermal_offset = layout.thermal_offset
    thermal_size = frame.payload.thermal.nbytes if frame.payload.thermal is not None else 0
    visible_offset = layout.visible_offset
    visible_size = frame.payload.visible.nbytes if frame.payload.visible is not None else 0

    # Thermal stream sequence (use frame sequence if not separate)
    thermal_seq = thermal.sequence if thermal.sequence is not None else desc.sequence
    thermal_ts = thermal.timestamp if thermal.timestamp is not None else desc.timestamp
    thermal_mono = thermal.monotonic_timestamp if thermal.monotonic_timestamp is not None else desc.monotonic_timestamp
    thermal_hw = thermal.hardware_timestamp if thermal.hardware_timestamp is not None else 0.0

    visible_seq = visible.sequence if visible.sequence is not None else desc.sequence
    visible_ts = visible.timestamp if visible.timestamp is not None else desc.timestamp
    visible_mono = visible.monotonic_timestamp if visible.monotonic_timestamp is not None else desc.monotonic_timestamp
    visible_hw = visible.hardware_timestamp if visible.hardware_timestamp is not None else 0.0

    sync_delta = sync.time_delta if sync.time_delta is not None else 0.0

    # SyncStatus is str,Enum; map to integer for binary format
    _SYNC_STATUS_MAP = {
        "synchronized": 0,
        "acceptable": 1,
        "degraded": 2,
        "missing_thermal": 3,
        "missing_visible": 4,
        "unknown": 5,
    }
    sync_status_val = _SYNC_STATUS_MAP.get(sync.status.value, 5)

    metadata = desc.metadata
    grab_started = metadata.get("grab_started", 0.0)
    grab_completed = metadata.get("grab_completed", 0.0)
    converted_at = metadata.get("converted_at", 0.0)
    grab_duration = metadata.get("grab_duration_s", 0.0)
    packet_stats = metadata.get("packet_stats")
    packet_stats_present = isinstance(packet_stats, dict)
    if not packet_stats_present:
        packet_stats = {}

    return struct.pack(
        _DESC_FORMAT,
        _DESC_VERSION_MAJOR,
        _DESC_VERSION_MINOR,
        cam_bytes,
        desc.sequence,
        desc.timestamp,
        desc.monotonic_timestamp,
        thermal.present,
        thermal.width or 0,
        thermal.height or 0,
        thermal_pix_fmt,
        thermal.bits_per_channel or 0,
        thermal_dtype,
        thermal.byte_count or 0,
        thermal_seq,
        thermal_ts,
        thermal_mono,
        thermal_hw,
        visible.present,
        visible.width or 0,
        visible.height or 0,
        visible_pix_fmt,
        visible.bits_per_channel or 0,
        visible_dtype,
        visible.byte_count or 0,
        visible_seq,
        visible_ts if visible_ts is not None else 0.0,
        visible_mono if visible_mono is not None else 0.0,
        visible_hw,
        sync_status_val,
        sync_delta,
        grab_started,
        grab_completed,
        converted_at,
        grab_duration,
        packet_stats_present,
        int(packet_stats.get("packets_seen", 0)),
        int(packet_stats.get("packets_lost", 0)),
        int(packet_stats.get("blocks_incomplete", 0)),
        int(packet_stats.get("blocks_discarded", 0)),
        thermal_offset,
        thermal_size,
        visible_offset,
        visible_size,
        True,  # frame_valid
    )


def decode_descriptor(data: bytes) -> dict:
    """Decode binary descriptor into a dict for FrameDescriptor reconstruction."""
    vals = struct.unpack(_DESC_FORMAT, data[:_DESC_SIZE])
    it = iter(vals)
    version_major = next(it)
    version_minor = next(it)
    camera_id = next(it).decode("ascii").rstrip("\x00")
    sequence = next(it)
    timestamp = next(it)
    monotonic_timestamp = next(it)
    thermal_present = next(it)
    thermal_width = next(it)
    thermal_height = next(it)
    thermal_pixel_format = next(it).decode("ascii").rstrip("\x00")
    thermal_bits_per_channel = next(it)
    thermal_dtype = next(it).decode("ascii").rstrip("\x00")
    thermal_byte_count = next(it)
    thermal_sequence = next(it)
    thermal_timestamp = next(it)
    thermal_mono_timestamp = next(it)
    thermal_hw_timestamp = next(it)
    visible_present = next(it)
    visible_width = next(it)
    visible_height = next(it)
    visible_pixel_format = next(it).decode("ascii").rstrip("\x00")
    visible_bits_per_channel = next(it)
    visible_dtype = next(it).decode("ascii").rstrip("\x00")
    visible_byte_count = next(it)
    visible_sequence = next(it)
    visible_timestamp = next(it)
    visible_mono_timestamp = next(it)
    visible_hw_timestamp = next(it)
    sync_status_val = next(it)
    sync_time_delta = next(it)

    # Import here to avoid circular import
    from thermal_monitor.core.frame import StreamMetadata, SyncInfo, SyncStatus

    # Map integer sync status back to enum
    _SYNC_STATUS_REV_MAP = {
        0: SyncStatus.SYNCHRONIZED,
        1: SyncStatus.ACCEPTABLE,
        2: SyncStatus.DEGRADED,
        3: SyncStatus.MISSING_THERMAL,
        4: SyncStatus.MISSING_VISIBLE,
        5: SyncStatus.UNKNOWN,
    }
    sync_status = _SYNC_STATUS_REV_MAP.get(sync_status_val, SyncStatus.UNKNOWN)

    grab_started = next(it)
    grab_completed = next(it)
    converted_at = next(it)
    grab_duration_s = next(it)
    packet_stats_present = next(it)
    packets_seen = next(it)
    packets_lost = next(it)
    blocks_incomplete = next(it)
    blocks_discarded = next(it)
    thermal_offset = next(it)
    thermal_size = next(it)
    visible_offset = next(it)
    visible_size = next(it)
    frame_valid = next(it)

    if version_major != _DESC_VERSION_MAJOR:
        raise ValueError(f"Descriptor version mismatch: {version_major}.{version_minor} != {_DESC_VERSION_MAJOR}.{_DESC_VERSION_MINOR}")

    # Import here to avoid circular import
    from thermal_monitor.core.frame import StreamMetadata, SyncInfo, SyncStatus

    thermal_meta = StreamMetadata(
        present=bool(thermal_present),
        width=thermal_width if thermal_present else None,
        height=thermal_height if thermal_present else None,
        pixel_format=thermal_pixel_format if thermal_present else None,
        bits_per_channel=thermal_bits_per_channel if thermal_present else None,
        dtype=thermal_dtype if thermal_present else None,
        byte_count=thermal_byte_count if thermal_present else None,
        sequence=thermal_sequence if thermal_present else None,
        timestamp=thermal_timestamp if thermal_present else None,
        monotonic_timestamp=thermal_mono_timestamp if thermal_present else None,
        hardware_timestamp=thermal_hw_timestamp if thermal_present and thermal_hw_timestamp != 0.0 else None,
    )

    visible_meta = StreamMetadata(
        present=bool(visible_present),
        width=visible_width if visible_present else None,
        height=visible_height if visible_present else None,
        pixel_format=visible_pixel_format if visible_present else None,
        bits_per_channel=visible_bits_per_channel if visible_present else None,
        dtype=visible_dtype if visible_present else None,
        byte_count=visible_byte_count if visible_present else None,
        sequence=visible_sequence if visible_present else None,
        timestamp=visible_timestamp if visible_present and visible_timestamp != 0.0 else None,
        monotonic_timestamp=visible_mono_timestamp if visible_present and visible_mono_timestamp != 0.0 else None,
        hardware_timestamp=visible_hw_timestamp if visible_present and visible_hw_timestamp != 0.0 else None,
    )

    sync = SyncInfo(
        status=sync_status,
        time_delta=sync_time_delta if sync_time_delta != 0.0 else None,
    )

    metadata = {
        "grab_started": grab_started,
        "grab_completed": grab_completed,
        "converted_at": converted_at,
        "grab_duration_s": grab_duration_s,
    }
    if packet_stats_present:
        metadata["packet_stats"] = {
            "packets_seen": packets_seen,
            "packets_lost": packets_lost,
            "blocks_incomplete": blocks_incomplete,
            "blocks_discarded": blocks_discarded,
        }

    return {
        "camera_id": camera_id,
        "sequence": sequence,
        "timestamp": timestamp,
        "monotonic_timestamp": monotonic_timestamp,
        "thermal": thermal_meta,
        "visible": visible_meta,
        "sync": sync,
        "metadata": metadata,
        "thermal_offset": thermal_offset,
        "thermal_size": thermal_size,
        "visible_offset": visible_offset,
        "visible_size": visible_size,
        "frame_valid": bool(frame_valid),
    }


# ─── SlotWriter ─────────────────────────────────────────────────────────────

@dataclass(slots=True)
class SlotWriter:
    """Write handle for a reserved ring buffer slot."""
    ring: "SharedMemoryRingBuffer"
    slot_index: int
    generation: int
    layout: RingLayout
    shm_buf: memoryview

    _committed: bool = field(default=False, init=False)

    def thermal_view(self) -> memoryview:
        """Get writeable view of thermal payload region."""
        offset = self.layout.thermal_addr(self.slot_index)
        size = self.layout.thermal_size
        return self.shm_buf[offset:offset + size]

    def visible_view(self) -> memoryview:
        """Get writeable view of visible payload region."""
        offset = self.layout.visible_addr(self.slot_index)
        size = self.layout.visible_size
        return self.shm_buf[offset:offset + size]

    def descriptor_view(self) -> memoryview:
        """Get writeable view of descriptor region."""
        offset = self.layout.descriptor_addr(self.slot_index)
        return self.shm_buf[offset:offset + self.layout.descriptor_size]

    def commit(self, frame: Frame) -> "PublishResult":
        """Write descriptor and mark slot as PUBLISHED."""
        if self._committed:
            raise RuntimeError("SlotWriter already committed")

        # Write descriptor
        desc_bytes = encode_descriptor(frame, self.layout, self.slot_index)
        desc_view = self.descriptor_view()
        desc_view[:len(desc_bytes)] = desc_bytes

        # Update slot header: mark PUBLISHED
        with self.ring._lock:
            header = SlotHeader.unpack(self._slot_header_bytes())
            header.state = SlotState.PUBLISHED
            header.sequence = frame.descriptor.sequence
            header.descriptor_len = len(desc_bytes)
            self._write_slot_header(header)

            # Update ring header
            ring_header = self.ring._read_header()
            ring_header.producer_sequence = frame.descriptor.sequence
            ring_header.producer_head_slot = self.slot_index
            self.ring._write_header(ring_header)

        self._committed = True
        return PublishResult(
            accepted=True,
            sequence=frame.descriptor.sequence,
            dropped=False,
            overwritten_sequence=self._overwritten_sequence,
        )

    @property
    def _overwritten_sequence(self) -> int | None:
        # The slot may have previously held a PUBLISHED frame
        # We don't track this directly; the producer can infer from its state
        return None

    def _slot_header_bytes(self) -> bytes:
        offset = self.layout.slot_header_addr(self.slot_index)
        return bytes(self.shm_buf[offset:offset + self.layout.slot_header_size])

    def _write_slot_header(self, header: SlotHeader) -> None:
        offset = self.layout.slot_header_addr(self.slot_index)
        self.shm_buf[offset:offset + self.layout.slot_header_size] = header.pack()


# ─── PublishResult (re-export compatible with camera.model) ─────────────────

from thermal_monitor.camera.model import PublishResult as _PublishResult

# Re-export for convenience
PublishResult = _PublishResult


# ─── SharedMemoryRingBuffer ─────────────────────────────────────────────────

class SharedMemoryRingBuffer:
    """Shared-memory ring buffer for one camera.

    Owns a single named shared memory segment containing the ring header
    and all slots. Single producer, multiple independent consumers.
    """

    def __init__(
        self,
        config: RingConfig,
        layout: RingLayout,
        shm: "SharedMemory",
        owns_shm: bool = True,
    ) -> None:
        self._config = config
        self._layout = layout
        self._shm = shm
        self._owns_shm = owns_shm
        self._lock = threading.Lock()
        self._closed = False
        self._producer: Producer | None = None
        self._consumers: dict[str, Consumer] = {}

    @classmethod
    def create(cls, config: RingConfig) -> "SharedMemoryRingBuffer":
        """Create a new ring buffer (producer side)."""
        from multiprocessing.shared_memory import SharedMemory

        layout = RingLayout.from_config(config)
        total_size = config.total_size()

        # Create new shared memory segment
        shm = SharedMemory(name=config.shm_name(), create=True, size=total_size)

        # Initialize header
        ring = cls(config, layout, shm, owns_shm=True)
        header = RingHeader(
            camera_id=config.camera_id,
            depth=config.depth,
            slot_size=config.slot_size(),
            owner_pid=os.getpid(),
            created_monotonic=time.monotonic(),
        )
        ring._write_header(header)

        # Initialize all slots to EMPTY
        for i in range(config.depth):
            slot_header = SlotHeader(state=SlotState.EMPTY, generation=1)
            ring._write_slot_header(i, slot_header)

        return ring

    @classmethod
    def attach(cls, config: RingConfig) -> "SharedMemoryRingBuffer":
        """Attach to an existing ring buffer (consumer side)."""
        from multiprocessing.shared_memory import SharedMemory

        layout = RingLayout.from_config(config)
        shm = SharedMemory(name=config.shm_name(), create=False)
        ring = cls(config, layout, shm, owns_shm=False)

        # Validate header
        header = ring._read_header()
        header.validate(config.camera_id, config.depth, config.slot_size())

        return ring

    def producer(self) -> "Producer":
        """Get the producer handle (single producer per ring)."""
        with self._lock:
            if self._producer is not None:
                raise RuntimeError("Producer already exists")
            self._producer = Producer(self)
            return self._producer

    def consumer(self, name: str) -> "Consumer":
        """Create a new independent consumer."""
        with self._lock:
            if name in self._consumers:
                raise ValueError(f"Consumer '{name}' already exists")
            consumer = Consumer(self, name)
            self._consumers[name] = consumer
            return consumer

    def close(self) -> None:
        """Close the ring buffer and release resources.

        Only the segment owner (``owns_shm=True``) seals the shared header.
        An attached consumer-side ring merely releases its own handle; the
        header is owned by the producer and must not be marked closed while
        a live producer may still publish.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True

            # Mark header as closed only if we own the segment.
            if self._owns_shm:
                try:
                    header = self._read_header()
                    header.closed = True
                    self._write_header(header)
                except Exception:
                    pass

            # Close producer
            if self._producer:
                self._producer.close()

            # Close all consumers
            for consumer in self._consumers.values():
                consumer.close()

            # Close shared memory handle
            try:
                self._shm.close()
            except Exception:
                pass

            # Unlink if we own it (Windows: no-op, but kept for symmetry)
            if self._owns_shm:
                try:
                    self._shm.unlink()
                except Exception:
                    pass

    def stats(self) -> "RingBufferStats":
        """Get ring buffer statistics."""
        if self._shm is None or self._shm.buf is None:
            # Ring is closed, return cached/known values
            return RingBufferStats(
                camera_id=self._config.camera_id,
                depth=self._config.depth,
                slot_size=self._config.slot_size(),
                total_size=self._config.total_size(),
                producer_sequence=0,
                producer_head_slot=-1,
                closed=True,
                owner_pid=0,
            )
        header = self._read_header()
        return RingBufferStats(
            camera_id=self._config.camera_id,
            depth=self._config.depth,
            slot_size=self._config.slot_size(),
            total_size=self._config.total_size(),
            producer_sequence=header.producer_sequence,
            producer_head_slot=header.producer_head_slot,
            closed=header.closed,
            owner_pid=header.owner_pid,
        )

    # Internal helpers
    def _read_header(self) -> RingHeader:
        buf = self._shm.buf
        header_data = bytes(buf[self._layout.header_offset:self._layout.header_offset + _RING_HEADER_SIZE])
        return RingHeader.unpack(header_data)

    def _write_header(self, header: RingHeader) -> None:
        buf = self._shm.buf
        buf[self._layout.header_offset:self._layout.header_offset + _RING_HEADER_SIZE] = header.pack()

    def _read_slot_header(self, index: int) -> SlotHeader:
        buf = self._shm.buf
        offset = self._layout.slot_header_addr(index)
        data = bytes(buf[offset:offset + self._layout.slot_header_size])
        return SlotHeader.unpack(data)

    def _write_slot_header(self, index: int, header: SlotHeader) -> None:
        buf = self._shm.buf
        offset = self._layout.slot_header_addr(index)
        buf[offset:offset + self._layout.slot_header_size] = header.pack()

    def _slot_state(self, index: int) -> SlotState:
        return self._read_slot_header(index).state

    def _shm_buf(self) -> memoryview:
        return self._shm.buf


# ─── Producer ───────────────────────────────────────────────────────────────

class Producer:
    """Single producer for a ring buffer. Non-blocking, never waits for consumers."""

    def __init__(self, ring: SharedMemoryRingBuffer) -> None:
        self._ring = ring
        self._layout = ring._layout
        self._closed = False

    def reserve(self) -> SlotWriter | None:
        """Reserve a slot for writing. Returns None if no reusable slot available."""
        if self._closed:
            raise RuntimeError("Producer is closed")

        with self._ring._lock:
            header = self._ring._read_header()
            if header.closed:
                raise RuntimeError("Ring buffer is closed")

            depth = self._ring._config.depth
            start_slot = (header.producer_head_slot + 1) % depth

            # Search for a reusable slot (EMPTY or PUBLISHED but not pinned)
            for i in range(depth):
                slot_idx = (start_slot + i) % depth
                slot_header = self._ring._read_slot_header(slot_idx)

                if slot_header.state == SlotState.EMPTY:
                    # Reusable: bump generation and mark WRITING
                    new_gen = slot_header.generation + 1
                    slot_header.state = SlotState.WRITING
                    slot_header.generation = new_gen
                    slot_header.sequence = 0
                    slot_header.descriptor_len = 0
                    slot_header.flags = 0
                    self._ring._write_slot_header(slot_idx, slot_header)
                    return SlotWriter(
                        ring=self._ring,
                        slot_index=slot_idx,
                        generation=new_gen,
                        layout=self._layout,
                        shm_buf=self._ring._shm_buf(),
                    )

                elif slot_header.state == SlotState.PUBLISHED:
                    # Check if slot is pinned (has readers)
                    # In Phase 1, we track pin count in slot_header.flags (lower 16 bits)
                    pin_count = slot_header.flags & 0xFFFF
                    if pin_count == 0:
                        # Not pinned, can reuse
                        new_gen = slot_header.generation + 1
                        slot_header.state = SlotState.WRITING
                        slot_header.generation = new_gen
                        slot_header.sequence = 0
                        slot_header.descriptor_len = 0
                        slot_header.flags = 0
                        self._ring._write_slot_header(slot_idx, slot_header)
                        return SlotWriter(
                            ring=self._ring,
                            slot_index=slot_idx,
                            generation=new_gen,
                            layout=self._layout,
                            shm_buf=self._ring._shm_buf(),
                        )
                    # Else pinned, continue searching

                # WRITING or INVALID or pinned PUBLISHED - skip

            # No reusable slot found
            return None

    def publish(self, frame: "Frame") -> PublishResult:
        """Convenience method: reserve, write payloads, commit."""
        writer = self.reserve()
        if writer is None:
            return PublishResult(
                accepted=False,
                sequence=frame.descriptor.sequence,
                dropped=True,
                overwritten_sequence=None,
            )

        # Write thermal payload (direct buffer copy via memoryview cast, avoids intermediate bytes allocation)
        thermal_arr = frame.payload.thermal
        if thermal_arr is not None:
            thermal_view = writer.thermal_view()
            thermal_view[:thermal_arr.nbytes] = memoryview(thermal_arr).cast("B")

        # Write visible payload
        visible_arr = frame.payload.visible
        if visible_arr is not None:
            visible_view = writer.visible_view()
            visible_view[:visible_arr.nbytes] = memoryview(visible_arr).cast("B")

        return writer.commit(frame)

    def close(self) -> None:
        self._closed = True


# ─── Consumer ───────────────────────────────────────────────────────────────

@dataclass(slots=True)
class ConsumerStats:
    """Per-consumer statistics."""
    consumed: int = 0
    overwritten: int = 0
    gaps: int = 0
    stale: int = 0
    invalid: int = 0
    last_sequence: int = -1
    last_generation: int = 0


@dataclass(slots=True)
class RingBufferStats:
    """Ring buffer global statistics."""
    camera_id: str
    depth: int
    slot_size: int
    total_size: int
    producer_sequence: int
    producer_head_slot: int
    closed: bool
    owner_pid: int


class Consumer:
    """Independent consumer with its own position state."""

    def __init__(self, ring: SharedMemoryRingBuffer, name: str) -> None:
        self._ring = ring
        self._name = name
        self._layout = ring._layout
        self._shm_buf = ring._shm_buf()
        self._stats = ConsumerStats()
        self._expected_sequence = 0
        self._closed = False
        self._pinned_slot: tuple[int, int] | None = None  # (slot_index, generation)

    def latest(self) -> "FrameView | None":
        """Return the newest published frame (snapshot semantics)."""
        if self._closed:
            raise RuntimeError("Consumer is closed")

        with self._ring._lock:
            header = self._ring._read_header()
            if header.closed:
                return None

            head_slot = header.producer_head_slot
            if head_slot < 0:
                return None

            slot_header = self._ring._read_slot_header(head_slot)
            if slot_header.state != SlotState.PUBLISHED:
                return None

            # Validate generation
            view = self._make_frame_view(head_slot, slot_header.generation)
            if view and view.valid():
                self._stats.consumed += 1
                self._stats.last_sequence = slot_header.sequence
                self._stats.last_generation = slot_header.generation
                self._expected_sequence = slot_header.sequence + 1
            return view

    def next(self, expected_sequence: int | None = None) -> "FrameView | None":
        """Return the frame with the given sequence, or the next expected."""
        if self._closed:
            raise RuntimeError("Consumer is closed")

        if expected_sequence is None:
            expected_sequence = self._expected_sequence

        with self._ring._lock:
            header = self._ring._read_header()
            if header.closed:
                return None

            # Search for the expected sequence
            depth = self._ring._config.depth
            for i in range(depth):
                slot_idx = (header.producer_head_slot - i) % depth
                if slot_idx < 0:
                    continue
                slot_header = self._ring._read_slot_header(slot_idx)
                if slot_header.state != SlotState.PUBLISHED:
                    continue
                if slot_header.sequence == expected_sequence:
                    # Found it
                    view = self._make_frame_view(slot_idx, slot_header.generation)
                    if view and view.valid():
                        self._stats.consumed += 1
                        self._stats.last_sequence = expected_sequence
                        self._stats.last_generation = slot_header.generation
                        self._expected_sequence = expected_sequence + 1
                        return view
                    elif view:
                        self._stats.stale += 1
                        return None

            # Not found - check if overwritten or not yet published
            if header.producer_sequence >= expected_sequence:
                # Producer has passed this sequence - it was overwritten
                gap = header.producer_sequence - expected_sequence + 1
                self._stats.overwritten += gap
                self._stats.gaps += gap
                # Re-anchor to latest published sequence
                self._expected_sequence = header.producer_sequence
            else:
                # Not yet published
                self._stats.gaps += 1

            return None

    def latest_pinned(self) -> "PinnedView | None":
        """Atomically get latest frame and pin it. Prevents pinning race."""
        if self._closed:
            raise RuntimeError("Consumer is closed")
        if self._pinned_slot is not None:
            raise RuntimeError("Consumer already has a pinned slot; release it first")

        with self._ring._lock:
            header = self._ring._read_header()
            if header.closed:
                return None

            head_slot = header.producer_head_slot
            if head_slot < 0:
                return None

            slot_header = self._ring._read_slot_header(head_slot)
            if slot_header.state != SlotState.PUBLISHED:
                return None

            # Increment pin count before creating view
            pin_count = (slot_header.flags & 0xFFFF) + 1
            slot_header.flags = (slot_header.flags & ~0xFFFF) | pin_count
            self._ring._write_slot_header(head_slot, slot_header)

            view = self._make_frame_view(head_slot, slot_header.generation)
            if view and view.valid():
                self._stats.consumed += 1
                self._stats.last_sequence = slot_header.sequence
                self._stats.last_generation = slot_header.generation
                self._expected_sequence = slot_header.sequence + 1
                self._pinned_slot = (view.slot_index, view.generation)
                return PinnedView(view, self)
            elif view:
                # View stale, decrement pin count
                pin_count = (slot_header.flags & 0xFFFF) - 1
                if pin_count < 0:
                    pin_count = 0
                slot_header.flags = (slot_header.flags & ~0xFFFF) | pin_count
                self._ring._write_slot_header(head_slot, slot_header)
                self._stats.stale += 1
                return None
            return None

    def next_pinned(self, expected_sequence: int | None = None) -> "PinnedView | None":
        """Atomically get expected frame and pin it. Prevents pinning race."""
        if self._closed:
            raise RuntimeError("Consumer is closed")
        if self._pinned_slot is not None:
            raise RuntimeError("Consumer already has a pinned slot; release it first")

        if expected_sequence is None:
            expected_sequence = self._expected_sequence

        with self._ring._lock:
            header = self._ring._read_header()
            if header.closed:
                return None

            depth = self._ring._config.depth
            for i in range(depth):
                slot_idx = (header.producer_head_slot - i) % depth
                if slot_idx < 0:
                    continue
                slot_header = self._ring._read_slot_header(slot_idx)
                if slot_header.state != SlotState.PUBLISHED:
                    continue
                if slot_header.sequence == expected_sequence:
                    # Found it - increment pin count before creating view
                    pin_count = (slot_header.flags & 0xFFFF) + 1
                    slot_header.flags = (slot_header.flags & ~0xFFFF) | pin_count
                    self._ring._write_slot_header(slot_idx, slot_header)

                    view = self._make_frame_view(slot_idx, slot_header.generation)
                    if view and view.valid():
                        self._stats.consumed += 1
                        self._stats.last_sequence = expected_sequence
                        self._stats.last_generation = slot_header.generation
                        self._expected_sequence = expected_sequence + 1
                        self._pinned_slot = (view.slot_index, view.generation)
                        return PinnedView(view, self)
                    elif view:
                        # View stale, decrement pin count
                        pin_count = (slot_header.flags & 0xFFFF) - 1
                        if pin_count < 0:
                            pin_count = 0
                        slot_header.flags = (slot_header.flags & ~0xFFFF) | pin_count
                        self._ring._write_slot_header(slot_idx, slot_header)
                        self._stats.stale += 1
                        return None
                    return None

            # Not found - check if overwritten or not yet published
            if header.producer_sequence >= expected_sequence:
                gap = header.producer_sequence - expected_sequence + 1
                self._stats.overwritten += gap
                self._stats.gaps += gap
                self._expected_sequence = header.producer_sequence
            else:
                self._stats.gaps += 1

            return None

    def pin(self, view: "FrameView") -> "PinnedView":
        """Pin a slot to prevent producer reuse during processing."""
        if self._closed:
            raise RuntimeError("Consumer is closed")
        if self._pinned_slot is not None:
            raise RuntimeError("Consumer already has a pinned slot; release it first")

        # Increment pin count in slot header
        with self._ring._lock:
            slot_header = self._ring._read_slot_header(view.slot_index)
            if slot_header.generation != view.generation:
                raise RuntimeError("View generation mismatch; slot was reused")
            if slot_header.state != SlotState.PUBLISHED:
                raise RuntimeError("Slot no longer published")

            # Increment pin count (stored in lower 16 bits of flags)
            pin_count = (slot_header.flags & 0xFFFF) + 1
            slot_header.flags = (slot_header.flags & ~0xFFFF) | pin_count
            self._ring._write_slot_header(view.slot_index, slot_header)

        self._pinned_slot = (view.slot_index, view.generation)
        return PinnedView(view, self)

    def release(self, pinned: "PinnedView") -> None:
        """Release a pinned slot."""
        if self._pinned_slot != (pinned.view.slot_index, pinned.view.generation):
            raise RuntimeError("Pinned view does not match current pin")

        with self._ring._lock:
            slot_header = self._ring._read_slot_header(pinned.view.slot_index)
            pin_count = (slot_header.flags & 0xFFFF) - 1
            if pin_count < 0:
                pin_count = 0
            slot_header.flags = (slot_header.flags & ~0xFFFF) | pin_count
            self._ring._write_slot_header(pinned.view.slot_index, slot_header)

        self._pinned_slot = None

    def stats(self) -> ConsumerStats:
        return ConsumerStats(
            consumed=self._stats.consumed,
            overwritten=self._stats.overwritten,
            gaps=self._stats.gaps,
            stale=self._stats.stale,
            invalid=self._stats.invalid,
            last_sequence=self._stats.last_sequence,
            last_generation=self._stats.last_generation,
        )

    def close(self) -> None:
        self._closed = True
        # Release any pinned slot
        if self._pinned_slot is not None:
            slot_idx, gen = self._pinned_slot
            with self._ring._lock:
                slot_header = self._ring._read_slot_header(slot_idx)
                pin_count = (slot_header.flags & 0xFFFF) - 1
                if pin_count < 0:
                    pin_count = 0
                slot_header.flags = (slot_header.flags & ~0xFFFF) | pin_count
                self._ring._write_slot_header(slot_idx, slot_header)
            self._pinned_slot = None

    def _make_frame_view(self, slot_index: int, generation: int) -> "FrameView | None":
        slot_header = self._ring._read_slot_header(slot_index)
        if slot_header.state != SlotState.PUBLISHED:
            return None
        if slot_header.generation != generation:
            return None

        # Read descriptor
        desc_offset = self._layout.descriptor_addr(slot_index)
        desc_data = bytes(self._shm_buf[desc_offset:desc_offset + self._layout.descriptor_size])
        try:
            desc_dict = decode_descriptor(desc_data)
        except Exception as e:
            logger.warning("Consumer %s: descriptor decode failed: %s", self._name, e)
            self._stats.invalid += 1
            return None

        if not desc_dict.get("frame_valid", False):
            self._stats.invalid += 1
            return None

        # Create numpy views
        thermal_arr = None
        if desc_dict["thermal_size"] > 0:
            thermal_offset = self._layout.thermal_addr(slot_index)
            thermal_bytes = self._shm_buf[thermal_offset:thermal_offset + desc_dict["thermal_size"]]
            thermal_dtype = np.dtype(desc_dict["thermal"].dtype) if desc_dict["thermal"].dtype else np.uint16
            thermal_arr = np.frombuffer(thermal_bytes, dtype=thermal_dtype).reshape(
                desc_dict["thermal"].height, desc_dict["thermal"].width
            )
            thermal_arr.setflags(write=False)

        visible_arr = None
        if desc_dict["visible_size"] > 0:
            visible_offset = self._layout.visible_addr(slot_index)
            visible_bytes = self._shm_buf[visible_offset:visible_offset + desc_dict["visible_size"]]
            v_meta = desc_dict["visible"]
            # Compute channels from byte_count
            itemsize = np.dtype(v_meta.dtype).itemsize if v_meta.dtype else 1
            expected_bytes = v_meta.width * v_meta.height * itemsize
            channels = max(1, v_meta.byte_count // expected_bytes) if expected_bytes > 0 else 1
            if channels == 1:
                visible_arr = np.frombuffer(visible_bytes, dtype=v_meta.dtype).reshape(
                    v_meta.height, v_meta.width
                )
            else:
                visible_arr = np.frombuffer(visible_bytes, dtype=v_meta.dtype).reshape(
                    v_meta.height, v_meta.width, channels
                )
            visible_arr.setflags(write=False)

        # Reconstruct FrameDescriptor
        from thermal_monitor.core.frame import FrameDescriptor, FramePayload, MappingProxyType
        descriptor = FrameDescriptor(
            camera_id=desc_dict["camera_id"],
            sequence=desc_dict["sequence"],
            timestamp=desc_dict["timestamp"],
            monotonic_timestamp=desc_dict["monotonic_timestamp"],
            thermal=desc_dict["thermal"],
            visible=desc_dict["visible"],
            sync=desc_dict["sync"],
            metadata=MappingProxyType(desc_dict["metadata"]),
        )
        payload = FramePayload(thermal=thermal_arr, visible=visible_arr)

        return FrameView(
            descriptor=descriptor,
            payload=payload,
            slot_index=slot_index,
            generation=generation,
            ring=self._ring,
        )


# ─── PinnedView ─────────────────────────────────────────────────────────────

@dataclass(slots=True)
class PinnedView:
    """A pinned FrameView that prevents slot reuse."""
    view: "FrameView"
    consumer: Consumer

    def __enter__(self) -> "FrameView":
        return self.view

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.consumer.release(self)


# ─── FrameView ──────────────────────────────────────────────────────────────

@dataclass(slots=True)
class FrameView:
    """Transient view of a frame in shared memory.

    LIFETIME CONTRACT: FrameView is NOT a durable frame.
    It is a transient shared-memory view, valid only until the producer
    reuses its slot. Unpinned views are best-effort; a consumer that must
    not read torn data pins for the duration of access, or copies into
    consumer-owned memory before processing.
    """
    descriptor: "FrameDescriptor"
    payload: "FramePayload"
    slot_index: int
    generation: int
    ring: SharedMemoryRingBuffer

    def thermal(self) -> np.ndarray | None:
        """Read-only thermal payload view. Raises if view is stale or ring closed."""
        if not self.valid():
            raise RuntimeError("FrameView is no longer valid (slot reused or ring closed)")
        return self.payload.thermal

    def visible(self) -> np.ndarray | None:
        """Read-only visible payload view. Raises if view is stale or ring closed."""
        if not self.valid():
            raise RuntimeError("FrameView is no longer valid (slot reused or ring closed)")
        return self.payload.visible

    def valid(self) -> bool:
        """Check if the view is still valid (generation matches)."""
        try:
            slot_header = self.ring._read_slot_header(self.slot_index)
            return slot_header.state == SlotState.PUBLISHED and slot_header.generation == self.generation
        except Exception:
            # Ring closed or other error
            return False

    def copy(self) -> "Frame":
        """Create a durable deep copy of this frame."""
        from thermal_monitor.core.frame import Frame, FramePayload
        thermal_copy = None
        visible_copy = None
        if self.payload.thermal is not None:
            thermal_copy = self.payload.thermal.copy()
            thermal_copy.setflags(write=False)
        if self.payload.visible is not None:
            visible_copy = self.payload.visible.copy()
            visible_copy.setflags(write=False)
        return Frame(
            descriptor=self.descriptor,
            payload=FramePayload(thermal=thermal_copy, visible=visible_copy),
        )


# ─── Publisher Adapter ──────────────────────────────────────────────────────

class SharedMemoryPublisher:
    """FramePublisher implementation using SharedMemoryRingBuffer.

    Implements the FramePublisher protocol so AcquisitionWorker can use
    the shared-memory transport without changes.
    """

    def __init__(self, ring: SharedMemoryRingBuffer) -> None:
        self._ring = ring
        self._producer = ring.producer()
        self._closed = False

    def publish(self, frame: "Frame") -> PublishResult:
        if self._closed:
            return PublishResult(accepted=False, sequence=frame.descriptor.sequence, dropped=True)
        return self._producer.publish(frame)

    def latest(self) -> "Frame | None":
        """Development convenience - returns a copied Frame."""
        # Create a temporary consumer to get latest
        consumer = self._ring.consumer("_latest_temp")
        try:
            view = consumer.latest()
            if view:
                return view.copy()
            return None
        finally:
            consumer.close()

    def reset(self) -> None:
        # Not meaningful for ring buffer; would require producer cooperation
        pass

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._producer.close()


# ─── Convenience factory ────────────────────────────────────────────────────

def create_ring_buffer(
    camera_id: str,
    thermal_width: int,
    thermal_height: int,
    thermal_dtype: np.dtype,
    depth: int,
    visible_width: int | None = None,
    visible_height: int | None = None,
    visible_dtype: np.dtype | None = None,
) -> SharedMemoryRingBuffer:
    """Create a ring buffer with the given specifications."""
    thermal_spec = PayloadSpec(
        width=thermal_width,
        height=thermal_height,
        dtype=thermal_dtype,
        bytes_per_frame=thermal_width * thermal_height * thermal_dtype.itemsize,
    )

    visible_spec = None
    if visible_width is not None and visible_height is not None and visible_dtype is not None:
        visible_spec = PayloadSpec(
            width=visible_width,
            height=visible_height,
            dtype=visible_dtype,
            bytes_per_frame=visible_width * visible_height * visible_dtype.itemsize,
        )

    config = RingConfig(
        camera_id=camera_id,
        thermal_spec=thermal_spec,
        visible_spec=visible_spec,
        depth=depth,
    )

    return SharedMemoryRingBuffer.create(config)


def attach_ring_buffer(
    camera_id: str,
    thermal_width: int,
    thermal_height: int,
    thermal_dtype: np.dtype,
    depth: int,
    visible_width: int | None = None,
    visible_height: int | None = None,
    visible_dtype: np.dtype | None = None,
) -> SharedMemoryRingBuffer:
    """Attach to an existing ring buffer."""
    thermal_spec = PayloadSpec(
        width=thermal_width,
        height=thermal_height,
        dtype=thermal_dtype,
        bytes_per_frame=thermal_width * thermal_height * thermal_dtype.itemsize,
    )

    visible_spec = None
    if visible_width is not None and visible_height is not None and visible_dtype is not None:
        visible_spec = PayloadSpec(
            width=visible_width,
            height=visible_height,
            dtype=visible_dtype,
            bytes_per_frame=visible_width * visible_height * visible_dtype.itemsize,
        )

    config = RingConfig(
        camera_id=camera_id,
        thermal_spec=thermal_spec,
        visible_spec=visible_spec,
        depth=depth,
    )

    return SharedMemoryRingBuffer.attach(config)
