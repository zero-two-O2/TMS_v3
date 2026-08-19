"""
storage.recording.format -- Persistent recording binary format (Stage 5B/5C).

Authoritative binary contract for the V3 recording container.  Everything
here is explicit little-endian binary layout: constants, enum codes and
``struct`` definitions.  There is deliberately **no pickle**, no NumPy
object serialization and no Python class identity anywhere in the format.

Layout summary (all little-endian, LE):

    Record header       128 bytes fixed
      magic             "TMSR"
      record_version    1
      record_type       0x01 = frame record
      stream_type       0x01 IR | 0x02 VL
      camera_id         fixed 37 bytes, NUL padded (UTF-8)
      ...
    Record trailer      4 bytes CRC32(header + metadata + payload)
    Chunk header        32 bytes fixed ("TMSR")
    Chunk trailer       16 bytes fixed ("TMSQ" + count + chunk CRC)
    Index header        32 bytes fixed ("TMSI")
    Index entry         56 bytes fixed

Reference: docs/architecture/v3-recording-offline.md, Appendix A.

Corrections applied to the written spec (reported and approved before
implementation):

1. The spec declares a fixed 128-byte record header but lists a
   variable-length ``camera_id``.  The fixed header fields (including
   ``camera_id_len`` and a 14-byte ``reserved2`` tail) sum to 91 bytes, so
   ``camera_id`` is stored as a fixed 37-byte NUL-padded field inside the
   128-byte header.  ``camera_id_len`` is kept and validated <= 37.
2. The spec declares a fixed 56-byte index entry but the listed fields
   (including a 6-byte ``reserved``) sum to 62 bytes.  The 6-byte
   ``reserved`` field is dropped; the documented 56-byte layout is kept.
"""

from __future__ import annotations

import struct
import zlib
from enum import IntEnum

import numpy as np

from thermal_monitor.core.frame import SyncStatus

# --------------------------------------------------------------------------
# Format identity
# --------------------------------------------------------------------------

FORMAT_MAJOR = 1
FORMAT_MINOR = 0

RECORD_MAGIC = b"TMSR"
CHUNK_MAGIC = b"TMSR"
CHUNK_END_MAGIC = b"TMSQ"
INDEX_MAGIC = b"TMSI"

RECORD_VERSION = 1
RECORD_TYPE_FRAME = 0x01

# --------------------------------------------------------------------------
# Sizes (bytes)
# --------------------------------------------------------------------------

RECORD_HEADER_SIZE = 128
CAMERA_ID_FIELD_SIZE = 37  # fixed slot inside the 128-byte record header
CRC_SIZE = 4
CHUNK_HEADER_SIZE = 32
CHUNK_TRAILER_SIZE = 16
INDEX_HEADER_SIZE = 32
INDEX_ENTRY_SIZE = 56

# Target chunk payload size.  Chunks roll over at this bound; a single record
# larger than the target is still written whole (records never split).
DEFAULT_CHUNK_TARGET_BYTES = 64 * 1024 * 1024  # 64 MiB

# Filenames / directory names inside a recording
MANIFEST_FILENAME = "manifest.json"
INDEX_FILENAME = "index.bin"
CHUNKS_DIRNAME = "chunks"
CONFIG_DIRNAME = "config"
EVENTS_DIRNAME = "events"
CHUNK_EXTENSION = ".tmsr"
CONFIG_CAMERAS = "cameras.json"
CONFIG_ROIS = "rois.json"
CONFIG_PTZ = "ptz.json"
CONFIG_CALIBRATION = "calibration.json"
CONFIG_ALARM = "alarm.json"
EVENTS_ALARMS = "alarms.json"

# Manifest statuses
STATUS_WRITING = "WRITING"
STATUS_COMPLETE = "COMPLETE"
STATUS_INCOMPLETE = "INCOMPLETE"
STATUS_CORRUPTED = "CORRUPTED"

# --------------------------------------------------------------------------
# Enums (binary codes)
# --------------------------------------------------------------------------


class StreamType(IntEnum):
    """Physical acquisition stream of one frame record."""

    IR = 0x01
    VL = 0x02


class RecordType(IntEnum):
    """Record payload type."""

    FRAME = RECORD_TYPE_FRAME


class PixelFormat(IntEnum):
    """Pixel format codes.  0x0000 is reserved for unknown on read."""

    UNKNOWN = 0x0000
    IR_DATA = 0x0001
    YUV422_8 = 0x0002
    RGB8 = 0x0003
    MONO8 = 0x0004


class DTypeCode(IntEnum):
    """dtype codes for raw payloads."""

    UINT8 = 0x01
    UINT16 = 0x02
    FLOAT32 = 0x03
    UINT32 = 0x04


class SyncStatusCode(IntEnum):
    """Sync status codes mirroring :class:`thermal_monitor.core.frame.SyncStatus`."""

    UNKNOWN = 0x00
    SYNCHRONIZED = 0x01
    ACCEPTABLE = 0x02
    DEGRADED = 0x03
    MISSING_THERMAL = 0x04
    MISSING_VISIBLE = 0x05


# --------------------------------------------------------------------------
# Codec mappings
# --------------------------------------------------------------------------

_PIXEL_FORMAT_TO_CODE = {
    "IR_Data": PixelFormat.IR_DATA,
    "YUV422_8": PixelFormat.YUV422_8,
    "RGB8": PixelFormat.RGB8,
    "Mono8": PixelFormat.MONO8,
}

_PIXEL_FORMAT_CODE_TO_NAME = {code.value: name for name, code in _PIXEL_FORMAT_TO_CODE.items()}

_DTYPE_TO_CODE = {
    "uint8": DTypeCode.UINT8,
    "uint16": DTypeCode.UINT16,
    "float32": DTypeCode.FLOAT32,
    "uint32": DTypeCode.UINT32,
}

_DTYPE_CODE_TO_DTYPE = {
    DTypeCode.UINT8: np.dtype(np.uint8),
    DTypeCode.UINT16: np.dtype(np.uint16),
    DTypeCode.FLOAT32: np.dtype(np.float32),
    DTypeCode.UINT32: np.dtype(np.uint32),
}

_DTYPE_CODE_TO_NAME = {code.value: np.dtype(dtype).name for code, dtype in _DTYPE_CODE_TO_DTYPE.items()}

_SYNC_STATUS_TO_CODE = {
    SyncStatus.UNKNOWN: SyncStatusCode.UNKNOWN,
    SyncStatus.SYNCHRONIZED: SyncStatusCode.SYNCHRONIZED,
    SyncStatus.ACCEPTABLE: SyncStatusCode.ACCEPTABLE,
    SyncStatus.DEGRADED: SyncStatusCode.DEGRADED,
    SyncStatus.MISSING_THERMAL: SyncStatusCode.MISSING_THERMAL,
    SyncStatus.MISSING_VISIBLE: SyncStatusCode.MISSING_VISIBLE,
}

_SYNC_STATUS_CODE_TO_STATUS = {code.value: status for status, code in _SYNC_STATUS_TO_CODE.items()}


class FormatError(ValueError):
    """Raised for any violation of the documented binary contract."""


def validate_format_version(major: int, minor: int) -> None:
    """Reject recordings whose major format version differs from ours.

    Minor version differences are tolerated (additive evolution): an older
    reader ignores unknown optional fields, per the Stage 5B versioning rules.
    """
    if major != FORMAT_MAJOR:
        raise FormatError(
            f"Recording format major {major} is incompatible with reader format major {FORMAT_MAJOR}"
        )
    if minor > FORMAT_MINOR:
        # Forward-compatible minor: unknown optional fields may exist.
        pass


def stream_type_from_value(value: str) -> StreamType:
    if value.upper() == "IR":
        return StreamType.IR
    if value.upper() == "VL":
        return StreamType.VL
    raise FormatError(f"Unknown stream type: {value!r}")


def pixel_format_to_code(name: str) -> PixelFormat:
    try:
        return _PIXEL_FORMAT_TO_CODE[name]
    except KeyError:
        raise FormatError(f"Unsupported pixel format: {name!r}") from None


def pixel_format_from_code(code: int) -> str:
    try:
        return _PIXEL_FORMAT_CODE_TO_NAME[int(code)]
    except KeyError:
        raise FormatError(f"Unknown pixel format code: {int(code):#06x}") from None


def dtype_to_code(dtype) -> DTypeCode:
    name = np.dtype(dtype).name
    try:
        return _DTYPE_TO_CODE[name]
    except KeyError:
        raise FormatError(f"Unsupported dtype: {name!r}") from None


def dtype_from_code(code: int) -> np.dtype:
    try:
        return _DTYPE_CODE_TO_DTYPE[DTypeCode(int(code))]
    except (KeyError, ValueError):
        raise FormatError(f"Unknown dtype code: {int(code)}") from None


def dtype_name_from_code(code: int) -> str:
    try:
        return _DTYPE_CODE_TO_NAME[int(code)]
    except KeyError:
        raise FormatError(f"Unknown dtype code: {int(code)}") from None


def sync_status_to_code(status: SyncStatus) -> SyncStatusCode:
    try:
        return _SYNC_STATUS_TO_CODE[status]
    except KeyError:
        raise FormatError(f"Unsupported sync status: {status!r}") from None


def sync_status_from_code(code: int) -> SyncStatus:
    try:
        return _SYNC_STATUS_CODE_TO_STATUS[int(code)]
    except KeyError:
        raise FormatError(f"Unknown sync status code: {int(code)}") from None


# --------------------------------------------------------------------------
# Fixed struct layouts (little-endian)
# --------------------------------------------------------------------------

RECORD_HEADER_STRUCT = struct.Struct(
    "<4sBBBBH37sQddQQIIIBBBqI14s"
)

CHUNK_HEADER_STRUCT = struct.Struct("<4sHQQ10s")
CHUNK_TRAILER_STRUCT = struct.Struct("<4sQI")
INDEX_HEADER_STRUCT = struct.Struct("<4sHQH16s")
INDEX_ENTRY_STRUCT = struct.Struct("<IBBQdIQQIIIH")


def validate_layouts() -> None:
    """Assert the documented binary sizes hold exactly."""
    if RECORD_HEADER_STRUCT.size != RECORD_HEADER_SIZE:
        raise FormatError(
            f"Record header struct is {RECORD_HEADER_STRUCT.size} bytes, expected {RECORD_HEADER_SIZE}"
        )
    if CHUNK_HEADER_STRUCT.size != CHUNK_HEADER_SIZE:
        raise FormatError(
            f"Chunk header struct is {CHUNK_HEADER_STRUCT.size} bytes, expected {CHUNK_HEADER_SIZE}"
        )
    if CHUNK_TRAILER_STRUCT.size != CHUNK_TRAILER_SIZE:
        raise FormatError(
            f"Chunk trailer struct is {CHUNK_TRAILER_STRUCT.size} bytes, expected {CHUNK_TRAILER_SIZE}"
        )
    if INDEX_HEADER_STRUCT.size != INDEX_HEADER_SIZE:
        raise FormatError(
            f"Index header struct is {INDEX_HEADER_STRUCT.size} bytes, expected {INDEX_HEADER_SIZE}"
        )
    if INDEX_ENTRY_STRUCT.size != INDEX_ENTRY_SIZE:
        raise FormatError(
            f"Index entry struct is {INDEX_ENTRY_STRUCT.size} bytes, expected {INDEX_ENTRY_SIZE}"
        )


validate_layouts()


def pack_camera_id(camera_id: str) -> tuple[int, bytes]:
    """Return ``(camera_id_len, camera_id_bytes)`` for the fixed 37-byte field."""
    data = camera_id.encode("utf-8")
    if len(data) > CAMERA_ID_FIELD_SIZE:
        raise FormatError(
            f"camera_id {camera_id!r} is {len(data)} bytes, exceeds the {CAMERA_ID_FIELD_SIZE}-byte header field"
        )
    return len(data), data


def unpack_camera_id(length: int, raw: bytes) -> str:
    if length > CAMERA_ID_FIELD_SIZE:
        raise FormatError(f"Invalid camera_id_len {length} > {CAMERA_ID_FIELD_SIZE}")
    return raw[:length].decode("utf-8")


def record_total_size(metadata_len: int, payload_length: int) -> int:
    """Total on-disk size of one record: header + metadata + payload + CRC."""
    return RECORD_HEADER_SIZE + metadata_len + payload_length + CRC_SIZE


def pack_record_header(
    *,
    stream_type: int,
    camera_id: str,
    sequence: int,
    timestamp: float,
    monotonic: float,
    payload_offset: int,
    payload_length: int,
    width: int,
    height: int,
    pixel_format: int,
    dtype_code: int,
    bits_per_channel: int,
    sync_status: int,
    sync_group_id: int,
    metadata_len: int,
) -> bytes:
    """Pack the fixed 128-byte record header (little-endian)."""
    camera_len, camera_raw = pack_camera_id(camera_id)
    return RECORD_HEADER_STRUCT.pack(
        RECORD_MAGIC,
        RECORD_VERSION,
        RECORD_TYPE_FRAME,
        stream_type,
        0,  # reserved
        camera_len,
        camera_raw,
        sequence,
        timestamp,
        monotonic,
        payload_offset,
        payload_length,
        width,
        height,
        pixel_format,
        dtype_code,
        bits_per_channel,
        sync_status,
        sync_group_id,
        metadata_len,
        b"\x00" * 14,  # reserved2
    )


def parse_record_header(header_bytes: bytes) -> dict:
    """Unpack a record header into a dict of named fields."""
    if len(header_bytes) != RECORD_HEADER_SIZE:
        raise FormatError(
            f"Record header is {len(header_bytes)} bytes, expected {RECORD_HEADER_SIZE}"
        )
    (
        magic,
        record_version,
        record_type,
        stream_type,
        reserved,
        camera_id_len,
        camera_id_raw,
        sequence,
        timestamp,
        monotonic,
        payload_offset,
        payload_length,
        width,
        height,
        pixel_format,
        dtype_code,
        bits_per_channel,
        sync_status,
        sync_group_id,
        metadata_len,
        reserved2,
    ) = RECORD_HEADER_STRUCT.unpack(header_bytes)
    if magic != RECORD_MAGIC:
        raise FormatError(f"Bad record magic {magic!r}, expected {RECORD_MAGIC!r}")
    if record_version != RECORD_VERSION:
        raise FormatError(f"Unsupported record version {record_version}, expected {RECORD_VERSION}")
    if record_type != RECORD_TYPE_FRAME:
        raise FormatError(f"Unsupported record type {record_type:#04x}")
    return {
        "magic": magic,
        "record_version": record_version,
        "record_type": record_type,
        "stream_type": stream_type,
        "reserved": reserved,
        "camera_id_len": camera_id_len,
        "camera_id": unpack_camera_id(camera_id_len, camera_id_raw),
        "sequence": sequence,
        "timestamp": timestamp,
        "monotonic": monotonic,
        "payload_offset": payload_offset,
        "payload_length": payload_length,
        "width": width,
        "height": height,
        "pixel_format": pixel_format,
        "dtype_code": dtype_code,
        "bits_per_channel": bits_per_channel,
        "sync_status": sync_status,
        "sync_group_id": sync_group_id,
        "metadata_len": metadata_len,
        "reserved2": reserved2,
    }


def compute_record_crc(header_bytes: bytes, metadata_bytes: bytes, payload_bytes: bytes) -> int:
    """CRC32 (zlib) over header + metadata + payload.  Stored as record trailer."""
    crc = zlib.crc32(header_bytes)
    crc = zlib.crc32(metadata_bytes, crc)
    crc = zlib.crc32(payload_bytes, crc)
    return crc & 0xFFFFFFFF
