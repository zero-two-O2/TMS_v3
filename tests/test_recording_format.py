"""Tests for the Stage 5C recording binary format contract.

These assert the documented sizes, magic values and enum codes exactly.  The
binary layout is the long-lived data contract; do not change it without a
corresponding Stage 5B spec change.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from thermal_monitor.core.frame import SyncStatus
from thermal_monitor.storage.recording.format import (
    CAMERA_ID_FIELD_SIZE,
    CHUNK_END_MAGIC,
    CHUNK_HEADER_SIZE,
    CHUNK_HEADER_STRUCT,
    CHUNK_MAGIC,
    CHUNK_TRAILER_SIZE,
    CHUNK_TRAILER_STRUCT,
    CRC_SIZE,
    DEFAULT_CHUNK_TARGET_BYTES,
    FORMAT_MAJOR,
    FORMAT_MINOR,
    INDEX_ENTRY_SIZE,
    INDEX_ENTRY_STRUCT,
    INDEX_HEADER_SIZE,
    INDEX_HEADER_STRUCT,
    INDEX_MAGIC,
    RECORD_HEADER_SIZE,
    RECORD_HEADER_STRUCT,
    RECORD_MAGIC,
    RECORD_TYPE_FRAME,
    RECORD_VERSION,
    DTypeCode,
    FormatError,
    PixelFormat,
    StreamType,
    SyncStatusCode,
    dtype_from_code,
    dtype_name_from_code,
    dtype_to_code,
    pack_camera_id,
    pack_record_header,
    parse_record_header,
    pixel_format_from_code,
    pixel_format_to_code,
    record_total_size,
    sync_status_from_code,
    sync_status_to_code,
    unpack_camera_id,
    validate_format_version,
)


class TestLayoutSizes:
    def test_record_header_is_128_bytes(self):
        assert RECORD_HEADER_SIZE == 128
        assert RECORD_HEADER_STRUCT.size == 128

    def test_index_entry_is_56_bytes(self):
        assert INDEX_ENTRY_SIZE == 56
        assert INDEX_ENTRY_STRUCT.size == 56

    def test_chunk_header_is_32_bytes(self):
        assert CHUNK_HEADER_SIZE == 32
        assert CHUNK_HEADER_STRUCT.size == 32

    def test_chunk_trailer_is_16_bytes(self):
        assert CHUNK_TRAILER_SIZE == 16
        assert CHUNK_TRAILER_STRUCT.size == 16

    def test_index_header_is_32_bytes(self):
        assert INDEX_HEADER_SIZE == 32
        assert INDEX_HEADER_STRUCT.size == 32

    def test_crc_size_is_4_bytes(self):
        assert CRC_SIZE == 4

    def test_camera_id_field_fits_128_byte_header(self):
        # Fixed fields (incl. camera_id_len + 14-byte reserved2) sum to 91
        # bytes; a fixed 37-byte camera_id completes the 128-byte header
        # exactly.
        assert CAMERA_ID_FIELD_SIZE == 37
        fixed = 4 + 1 + 1 + 1 + 1 + 2 + 8 + 8 + 8 + 8 + 8 + 4 + 4 + 4 + 1 + 1 + 1 + 8 + 4 + 14
        assert fixed + CAMERA_ID_FIELD_SIZE == 128

    def test_index_entry_fields_sum_to_56(self):
        assert 4 + 1 + 1 + 8 + 8 + 4 + 8 + 8 + 4 + 4 + 4 + 2 == 56

    def test_target_chunk_size(self):
        assert DEFAULT_CHUNK_TARGET_BYTES == 64 * 1024 * 1024


class TestMagic:
    def test_record_magic(self):
        assert RECORD_MAGIC == b"TMSR"

    def test_chunk_magic(self):
        assert CHUNK_MAGIC == b"TMSR"

    def test_chunk_end_magic(self):
        assert CHUNK_END_MAGIC == b"TMSQ"

    def test_index_magic(self):
        assert INDEX_MAGIC == b"TMSI"


class TestVersionConstants:
    def test_format_version(self):
        assert (FORMAT_MAJOR, FORMAT_MINOR) == (1, 0)

    def test_record_version(self):
        assert RECORD_VERSION == 1

    def test_record_type_frame(self):
        assert RECORD_TYPE_FRAME == 0x01

    def test_validate_format_version_accepts_matching_major(self):
        validate_format_version(1, 0)
        validate_format_version(1, 1)  # older reader, newer minor: tolerated

    def test_validate_format_version_rejects_other_major(self):
        with pytest.raises(FormatError):
            validate_format_version(0, 0)
        with pytest.raises(FormatError):
            validate_format_version(2, 0)


class TestStreamTypeCodes:
    def test_ir(self):
        assert StreamType.IR == 0x01

    def test_vl(self):
        assert StreamType.VL == 0x02


class TestPixelFormatCodes:
    def test_codes(self):
        assert PixelFormat.IR_DATA == 0x0001
        assert PixelFormat.YUV422_8 == 0x0002
        assert PixelFormat.RGB8 == 0x0003
        assert PixelFormat.MONO8 == 0x0004


class TestDTypeCodes:
    def test_codes(self):
        assert DTypeCode.UINT8 == 0x01
        assert DTypeCode.UINT16 == 0x02
        assert DTypeCode.FLOAT32 == 0x03
        assert DTypeCode.UINT32 == 0x04


class TestSyncStatusCodeCodes:
    def test_codes(self):
        assert SyncStatusCode.UNKNOWN == 0x00
        assert SyncStatusCode.SYNCHRONIZED == 0x01
        assert SyncStatusCode.ACCEPTABLE == 0x02
        assert SyncStatusCode.DEGRADED == 0x03
        assert SyncStatusCode.MISSING_THERMAL == 0x04
        assert SyncStatusCode.MISSING_VISIBLE == 0x05


class TestRecordHeaderCodec:
    def _pack(self, camera_id="cam_001", **overrides):
        kwargs = dict(
            stream_type=1,
            camera_id=camera_id,
            sequence=42,
            timestamp=1755509400.5,
            monotonic=12.25,
            payload_offset=1024,
            payload_length=256,
            width=16,
            height=16,
            pixel_format=1,
            dtype_code=2,
            bits_per_channel=16,
            sync_status=5,
            sync_group_id=-1,
            metadata_len=80,
        )
        kwargs.update(overrides)
        return pack_record_header(**kwargs)

    def test_roundtrip(self):
        raw = self._pack()
        assert len(raw) == 128
        fields = parse_record_header(raw)
        assert fields["magic"] == RECORD_MAGIC
        assert fields["record_version"] == RECORD_VERSION
        assert fields["record_type"] == RECORD_TYPE_FRAME
        assert fields["stream_type"] == 1
        assert fields["camera_id"] == "cam_001"
        assert fields["sequence"] == 42
        assert fields["timestamp"] == 1755509400.5
        assert fields["monotonic"] == 12.25
        assert fields["payload_offset"] == 1024
        assert fields["payload_length"] == 256
        assert fields["width"] == 16
        assert fields["height"] == 16
        assert fields["pixel_format"] == 1
        assert fields["dtype_code"] == 2
        assert fields["bits_per_channel"] == 16
        assert fields["sync_status"] == 5
        assert fields["sync_group_id"] == -1
        assert fields["metadata_len"] == 80

    def test_camera_id_padded_and_len_preserved(self):
        raw = self._pack(camera_id="cam_longer_0123")
        fields = parse_record_header(raw)
        assert fields["camera_id_len"] == len("cam_longer_0123")
        assert fields["camera_id"] == "cam_longer_0123"
        assert fields["camera_id_len"] <= CAMERA_ID_FIELD_SIZE

    def test_camera_id_too_long_raises(self):
        with pytest.raises(FormatError):
            self._pack(camera_id="x" * (CAMERA_ID_FIELD_SIZE + 1))

    def test_bad_magic_rejected(self):
        raw = bytearray(self._pack())
        raw[0] = ord("X")
        with pytest.raises(FormatError):
            parse_record_header(bytes(raw))

    def test_bad_version_rejected(self):
        raw = bytearray(self._pack())
        raw[4] = 99
        with pytest.raises(FormatError):
            parse_record_header(bytes(raw))

    def test_short_header_rejected(self):
        with pytest.raises(FormatError):
            parse_record_header(b"\x00" * 127)

    def test_pack_camera_id_helpers(self):
        length, raw = pack_camera_id("cam_001")
        assert length == 7
        assert unpack_camera_id(length, raw) == "cam_001"
        with pytest.raises(FormatError):
            pack_camera_id("y" * 64)


class TestIndexEntryCodec:
    def test_pack_unpack_roundtrip(self):
        from thermal_monitor.storage.recording.index import pack_index_entry, unpack_index_entry

        raw = pack_index_entry(
            camera_id_ref=1,
            stream_type=2,
            flags=0,
            sequence=7,
            timestamp=1755509401.25,
            chunk_seq=3,
            offset=123456,
            payload_length=614400,
            checksum=0xDEADBEEF,
            width=640,
            height=480,
            pixel_format=2,
        )
        assert len(raw) == 56
        entry = unpack_index_entry(raw, camera_id="cam_002")
        assert entry.camera_id == "cam_002"
        assert entry.camera_id_ref == 1
        assert entry.stream_type == 2
        assert entry.sequence == 7
        assert entry.timestamp == 1755509401.25
        assert entry.chunk_seq == 3
        assert entry.offset == 123456
        assert entry.payload_length == 614400
        assert entry.checksum == 0xDEADBEEF
        assert entry.width == 640
        assert entry.height == 480
        assert entry.pixel_format == 2

    def test_wrong_size_rejected(self):
        from thermal_monitor.storage.recording.index import IndexReadError, unpack_index_entry

        with pytest.raises(IndexReadError):
            unpack_index_entry(b"\x00" * 55, camera_id="cam_001")


class TestCodecMappings:
    def test_pixel_format_to_code(self):
        assert pixel_format_to_code("IR_Data") == PixelFormat.IR_DATA
        assert pixel_format_to_code("YUV422_8") == PixelFormat.YUV422_8
        assert pixel_format_to_code("RGB8") == PixelFormat.RGB8
        assert pixel_format_to_code("Mono8") == PixelFormat.MONO8

    def test_pixel_format_from_code(self):
        assert pixel_format_from_code(1) == "IR_Data"
        assert pixel_format_from_code(2) == "YUV422_8"
        assert pixel_format_from_code(3) == "RGB8"
        assert pixel_format_from_code(4) == "Mono8"

    def test_pixel_format_unknown_raises(self):
        with pytest.raises(FormatError):
            pixel_format_to_code("bogus")
        with pytest.raises(FormatError):
            pixel_format_from_code(0xFEED)

    def test_dtype_mapping(self):
        assert dtype_to_code(np.uint8) == DTypeCode.UINT8
        assert dtype_to_code(np.uint16) == DTypeCode.UINT16
        assert dtype_to_code(np.float32) == DTypeCode.FLOAT32
        assert dtype_to_code(np.uint32) == DTypeCode.UINT32
        assert dtype_from_code(1) == np.dtype(np.uint8)
        assert dtype_from_code(2) == np.dtype(np.uint16)
        assert dtype_from_code(3) == np.dtype(np.float32)
        assert dtype_from_code(4) == np.dtype(np.uint32)
        assert dtype_name_from_code(2) == "uint16"

    def test_dtype_unsupported_raises(self):
        with pytest.raises(FormatError):
            dtype_to_code(np.float64)
        with pytest.raises(FormatError):
            dtype_from_code(99)

    def test_sync_status_mapping(self):
        assert sync_status_to_code(SyncStatus.UNKNOWN) == SyncStatusCode.UNKNOWN
        assert sync_status_to_code(SyncStatus.SYNCHRONIZED) == SyncStatusCode.SYNCHRONIZED
        assert sync_status_to_code(SyncStatus.ACCEPTABLE) == SyncStatusCode.ACCEPTABLE
        assert sync_status_to_code(SyncStatus.DEGRADED) == SyncStatusCode.DEGRADED
        assert sync_status_to_code(SyncStatus.MISSING_THERMAL) == SyncStatusCode.MISSING_THERMAL
        assert sync_status_to_code(SyncStatus.MISSING_VISIBLE) == SyncStatusCode.MISSING_VISIBLE
        assert sync_status_from_code(0) == SyncStatus.UNKNOWN
        assert sync_status_from_code(5) == SyncStatus.MISSING_VISIBLE

    def test_sync_status_unknown_raises(self):
        with pytest.raises(FormatError):
            sync_status_from_code(99)


class TestRecordArithmetic:
    def test_record_total_size(self):
        assert record_total_size(0, 0) == 128 + 0 + 0 + 4
        assert record_total_size(80, 614400) == 128 + 80 + 614400 + 4

    def test_little_endian_structures(self):
        assert RECORD_HEADER_STRUCT.format.startswith("<")
        assert INDEX_ENTRY_STRUCT.format.startswith("<")
        assert CHUNK_HEADER_STRUCT.format.startswith("<")
        assert CHUNK_TRAILER_STRUCT.format.startswith("<")
        assert INDEX_HEADER_STRUCT.format.startswith("<")