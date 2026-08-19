"""
offline.reader -- RecordingReader (Stage 5C reader boundary).

Opens a recording directory, validates the manifest, exposes the index, reads
individual records, iterates frames, seeks, and verifies CRC.  It never
modifies/repairs/truncates the source recording.

Status rules (docs/architecture/v3-recording-offline.md, sections 17-18):

- manifest status WRITING / INCOMPLETE, or ``finalized_at`` absent  -> INCOMPLETE
- manifest missing / unparseable, or structural/checksum failure     -> CORRUPTED
- otherwise                                                          -> COMPLETE

A reader may read valid records from an incomplete recording, and valid
records up to the corruption point of a corrupted one.  Corrupt data is never
returned: it raises :class:`RecordingReadError`.
"""

from __future__ import annotations

import bisect
import json
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Iterator, Mapping, Sequence

import numpy as np

from thermal_monitor.core.frame import (
    Frame,
    FrameDescriptor,
    FramePayload,
    StreamMetadata,
    SyncInfo,
    SyncStatus,
)
from thermal_monitor.storage.recording.chunks import ChunkReadError, ChunkReader, ChunkRecord
from thermal_monitor.storage.recording.format import (
    CHUNKS_DIRNAME,
    FORMAT_MAJOR,
    INDEX_FILENAME,
    MANIFEST_FILENAME,
    RECORD_HEADER_SIZE,
    STATUS_COMPLETE,
    STATUS_CORRUPTED,
    STATUS_INCOMPLETE,
    STATUS_WRITING,
    FormatError,
    dtype_name_from_code,
    pixel_format_from_code,
    stream_type_from_value,
    sync_status_from_code,
)
from thermal_monitor.storage.recording.index import IndexEntry, IndexReadError, IndexReader


class RecordingStatus(str, Enum):
    """Overall recording status returned by the reader."""

    WRITING = STATUS_WRITING
    COMPLETE = STATUS_COMPLETE
    INCOMPLETE = STATUS_INCOMPLETE
    CORRUPTED = STATUS_CORRUPTED


class RecordingReadError(Exception):
    """Raised when a record cannot be read cleanly."""


class RecordingCorruptError(RecordingReadError):
    """Raised when corruption is detected (CRC, length, or structure)."""


@dataclass(frozen=True, slots=True)
class RecordingRecord:
    """A fully decoded, CRC-verified record plus its reconstructed Frame."""

    camera_id: str
    stream_type: int
    sequence: int
    timestamp: float
    monotonic: float
    payload_offset: int
    payload_length: int
    width: int
    height: int
    pixel_format: str
    dtype: str
    bits_per_channel: int
    sync_status: int
    sync_group_id: int
    chunk_seq: int
    record_offset: int
    metadata: Mapping[str, object]
    frame: Frame


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Result of a full CRC verification pass."""

    status: RecordingStatus
    records_verified: int
    failures: tuple[str, ...] = ()


class RecordingReader:
    """Reads a recording directory (manifest + index + chunks)."""

    def __init__(
        self,
        recording_dir: str | Path,
        *,
        tolerant: bool = False,
        strict_open: bool = False,
    ) -> None:
        self._dir = Path(recording_dir)
        self._tolerant = tolerant
        self._chunks_dir = self._dir / CHUNKS_DIRNAME
        self._manifest: dict | None = None
        self._status = RecordingStatus.CORRUPTED
        self._manifest_error: str | None = None
        self._index: IndexReader | None = None
        self._chunk_readers: dict[int, ChunkReader] = {}
        self._entries: list[IndexEntry] = []
        self._sorted_entries: list[IndexEntry] = []
        self._timestamps: list[float] = []
        self._seq_map: dict[tuple[str, int], list[tuple[int, int]]] = defaultdict(list)
        self._open()

    # ------------------------------------------------------------------
    # Open / status
    # ------------------------------------------------------------------

    @property
    def recording_dir(self) -> Path:
        return self._dir

    @property
    def status(self) -> RecordingStatus:
        return self._status

    @property
    def manifest(self) -> dict:
        return self._manifest or {}

    @property
    def recording_id(self) -> str:
        return str(self.manifest.get("recording_id") or self._dir.name)

    @property
    def camera_ids(self) -> list[str]:
        return list(self.manifest.get("cameras") or [])

    @property
    def frame_count(self) -> int:
        return len(self._entries)

    @property
    def chunk_count(self) -> int:
        return int(self.manifest.get("chunk_count") or 0)

    @property
    def entries(self) -> list[IndexEntry]:
        """Playback entries (chronologically sorted)."""
        return list(self._entries)

    def _open(self) -> None:
        self._load_manifest()
        self._load_chunks()
        base_status = self._base_status()
        self._index = self._try_load_index()

        if base_status == RecordingStatus.INCOMPLETE:
            self._status = RecordingStatus.INCOMPLETE
            self._load_entries_scanned()
            return

        if base_status == RecordingStatus.CORRUPTED:
            self._status = RecordingStatus.CORRUPTED
            if self._index is not None:
                self._set_playback_entries(self._index.entries)
            else:
                self._load_entries_scanned()
            return

        # COMPLETE path: index is authoritative; validate structure.  On
        # failure the recording is CORRUPTED but the index entries are kept so
        # reads/iteration surface the corruption at the boundary.
        valid = (
            self._index is not None
            and self._validate_entries_in_bounds(self._index.entries)
            and (
                self.manifest.get("frame_count") is None
                or len(self._index.entries) == self.manifest.get("frame_count")
            )
            and all(chunk.has_valid_trailer for chunk in self._chunk_readers.values())
        )
        if not valid:
            self._status = RecordingStatus.CORRUPTED
            if self._index is not None:
                self._set_playback_entries(self._index.entries)
            else:
                self._load_entries_scanned()
            return
        self._status = RecordingStatus.COMPLETE
        self._set_playback_entries(self._index.entries)

    def _load_manifest(self) -> None:
        path = self._dir / MANIFEST_FILENAME
        if not path.exists():
            self._manifest_error = "manifest.json missing"
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                document = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            self._manifest_error = f"manifest.json unparseable: {exc}"
            return
        if not isinstance(document, dict):
            self._manifest_error = "manifest.json is not a JSON object"
            return
        if "status" not in document:
            self._manifest_error = "manifest.json has no status field"
            return
        version = document.get("format_version")
        if not isinstance(version, dict) or version.get("major") != FORMAT_MAJOR:
            self._manifest_error = (
                f"unsupported format version {version!r} (reader major {FORMAT_MAJOR})"
            )
            return
        self._manifest = document

    def _base_status(self) -> RecordingStatus:
        if self._manifest is None:
            return RecordingStatus.CORRUPTED
        status = self.manifest.get("status")
        if status == STATUS_CORRUPTED:
            return RecordingStatus.CORRUPTED
        if status == STATUS_WRITING or status == STATUS_INCOMPLETE:
            return RecordingStatus.INCOMPLETE
        if status == STATUS_COMPLETE:
            if self.manifest.get("finalized_at") is None:
                return RecordingStatus.INCOMPLETE
            return RecordingStatus.COMPLETE
        return RecordingStatus.CORRUPTED

    def _load_chunks(self) -> None:
        if not self._chunks_dir.exists():
            return
        for path in sorted(self._chunks_dir.glob(f"chunk_*.tmsr")):
            try:
                chunk = ChunkReader(path)
            except (ChunkReadError, OSError):
                continue
            self._chunk_readers[chunk.chunk_seq] = chunk

    def _try_load_index(self) -> IndexReader | None:
        path = self._dir / INDEX_FILENAME
        if not path.exists():
            return None
        reader = IndexReader(path)
        try:
            reader.load(self.manifest.get("cameras") or [])
        except (IndexReadError, OSError):
            return None
        return reader

    def _validate_entries_in_bounds(self, entries: Sequence[IndexEntry]) -> bool:
        for entry in entries:
            chunk = self._get_chunk(entry.chunk_seq)
            if chunk is None:
                return False
            if entry.offset + RECORD_HEADER_SIZE > chunk.valid_data_end:
                return False
            if entry.payload_length > chunk.valid_data_end - (entry.offset + RECORD_HEADER_SIZE):
                return False
        return True

    def _load_entries_scanned(self) -> None:
        """Best-effort enumeration of valid records for INCOMPLETE/CORRUPTED recordings."""
        entries: list[IndexEntry] = []
        for chunk_seq in sorted(self._chunk_readers):
            chunk = self._chunk_readers[chunk_seq]
            try:
                for record in chunk.records():
                    header = record.header
                    entry = IndexEntry(
                        camera_id=header["camera_id"],
                        camera_id_ref=-1,
                        stream_type=header["stream_type"],
                        flags=0,
                        sequence=header["sequence"],
                        timestamp=header["timestamp"],
                        chunk_seq=chunk_seq,
                        offset=record.offset,
                        payload_length=header["payload_length"],
                        checksum=record.stored_crc,
                        width=header["width"],
                        height=header["height"],
                        pixel_format=header["pixel_format"],
                    )
                    entries.append(entry)
            except ChunkReadError:
                break  # truncated boundary reached; remaining records are incomplete
        self._set_playback_entries(entries)

    def _set_playback_entries(self, entries: Sequence[IndexEntry]) -> None:
        self._entries = sorted(entries, key=lambda e: (e.timestamp, e.sequence, e.stream_type))
        self._sorted_entries = self._entries
        self._timestamps = [e.timestamp for e in self._entries]
        self._seq_map.clear()
        for pos, entry in enumerate(self._entries):
            self._seq_map[(entry.camera_id, entry.stream_type)].append((entry.sequence, pos))
        for seq_list in self._seq_map.values():
            seq_list.sort()

    # ------------------------------------------------------------------
    # Chunk access
    # ------------------------------------------------------------------

    def _get_chunk(self, chunk_seq: int) -> ChunkReader | None:
        return self._chunk_readers.get(chunk_seq)

    def close(self) -> None:
        for chunk in self._chunk_readers.values():
            chunk.close()
        self._chunk_readers.clear()

    def __enter__(self) -> "RecordingReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Record / frame reads
    # ------------------------------------------------------------------

    def _read_record_bytes(self, entry: IndexEntry) -> ChunkRecord:
        chunk = self._get_chunk(entry.chunk_seq)
        if chunk is None:
            raise RecordingReadError(f"Chunk {entry.chunk_seq} is not available")
        if entry.offset < 0:
            raise RecordingReadError(f"Invalid record offset {entry.offset}")
        try:
            record = chunk.read_record(entry.offset)
        except ChunkReadError as exc:
            raise RecordingReadError(
                f"Record at chunk {entry.chunk_seq} offset {entry.offset} is unreadable: {exc}"
            ) from None
        if entry.payload_length != len(record.payload_bytes):
            raise RecordingCorruptError(
                f"Index payload_length {entry.payload_length} does not match "
                f"record payload {len(record.payload_bytes)} bytes"
            )
        if entry.checksum != record.stored_crc:
            raise RecordingCorruptError(
                f"Index checksum {entry.checksum:#010x} does not match record CRC "
                f"{record.stored_crc:#010x} at chunk {entry.chunk_seq} offset {entry.offset}"
            )
        if not record.verify_crc():
            raise RecordingCorruptError(
                f"Record CRC mismatch at chunk {entry.chunk_seq} offset {entry.offset}"
            )
        return record

    def read_record(self, entry: IndexEntry) -> RecordingRecord:
        """Read and fully validate one record; returns decoded data + Frame."""
        record = self._read_record_bytes(entry)
        return self._decode_record(entry, record)

    def read_frame(self, entry: IndexEntry) -> Frame:
        """Read one record and reconstruct its V3 Frame (CRC-verified)."""
        return self.read_record(entry).frame

    def read_entry_at(self, position: int) -> Frame:
        if not 0 <= position < len(self._entries):
            raise RecordingReadError(f"Index position {position} is out of range")
        return self.read_frame(self._entries[position])

    def _decode_record(self, entry: IndexEntry, record: ChunkRecord) -> RecordingRecord:
        header = record.header
        try:
            metadata = json.loads(record.metadata_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecordingCorruptError(
                f"Record metadata at chunk {entry.chunk_seq} offset {entry.offset} is not valid JSON: {exc}"
            ) from None
        stream_meta = metadata.get("stream_meta") if isinstance(metadata, dict) else None
        if not isinstance(stream_meta, dict):
            raise RecordingCorruptError("Record metadata has no stream_meta object")

        dtype_name = dtype_name_from_code(header["dtype_code"])
        tail_dtype = stream_meta.get("dtype")
        if tail_dtype is not None and np.dtype(tail_dtype).name != np.dtype(dtype_name).name:
            raise RecordingCorruptError(
                f"Record dtype {tail_dtype!r} does not match dtype_code "
                f"{header['dtype_code']} ({dtype_name})"
            )
        dtype = np.dtype(dtype_name)
        width, height = header["width"], header["height"]
        raw_shape = stream_meta.get("array_shape")
        if isinstance(raw_shape, (list, tuple)) and raw_shape:
            shape = [int(s) for s in raw_shape]
            if len(shape) < 2 or shape[0] != height or shape[1] != width:
                raise RecordingCorruptError(
                    f"Record array shape {shape} is inconsistent with header "
                    f"{width}x{height}"
                )
        else:
            shape = [height, width]
        expected_bytes = 1
        for dim in shape:
            expected_bytes *= dim
        expected_bytes *= dtype.itemsize
        if expected_bytes != len(record.payload_bytes):
            raise RecordingCorruptError(
                f"Payload length {len(record.payload_bytes)} does not match "
                f"{shape} {dtype} ({expected_bytes} bytes)"
            )
        try:
            pixel_format = pixel_format_from_code(header["pixel_format"])
        except FormatError as exc:
            raise RecordingCorruptError(str(exc)) from None

        array = np.frombuffer(record.payload_bytes, dtype=dtype).reshape(shape)
        array.setflags(write=False)

        stream_type = header["stream_type"]
        is_ir = stream_type == 1
        frame_sequence = header["sequence"]
        original_monotonic = stream_meta.get("monotonic_timestamp")
        descriptor_monotonic = (
            original_monotonic if original_monotonic is not None else header["monotonic"]
        )
        sync_status_from_code(header["sync_status"])  # validate the code is known

        if is_ir:
            thermal_meta = StreamMetadata(
                present=True,
                width=width,
                height=height,
                pixel_format=pixel_format,
                bits_per_channel=header["bits_per_channel"],
                dtype=dtype_name,
                byte_count=len(record.payload_bytes),
                sequence=stream_meta.get("sequence") or frame_sequence,
                timestamp=stream_meta.get("timestamp") or header["timestamp"],
                monotonic_timestamp=stream_meta.get("monotonic_timestamp") or header["monotonic"],
                hardware_timestamp=stream_meta.get("hardware_timestamp"),
            )
            visible_meta = StreamMetadata(present=False)
            reconstructed_sync = SyncStatus.MISSING_VISIBLE
            payload = FramePayload(thermal=array, visible=None)
        else:
            thermal_meta = StreamMetadata(present=False)
            visible_meta = StreamMetadata(
                present=True,
                width=width,
                height=height,
                pixel_format=pixel_format,
                bits_per_channel=header["bits_per_channel"],
                dtype=dtype_name,
                byte_count=len(record.payload_bytes),
                sequence=stream_meta.get("sequence") or frame_sequence,
                timestamp=stream_meta.get("timestamp") or header["timestamp"],
                monotonic_timestamp=stream_meta.get("monotonic_timestamp") or header["monotonic"],
                hardware_timestamp=stream_meta.get("hardware_timestamp"),
            )
            reconstructed_sync = SyncStatus.MISSING_THERMAL
            payload = FramePayload(thermal=None, visible=array)

        # A single physical record reconstructs to one stream, so the pair
        # status cannot be synchronized; missing is the truthful state.
        sync = SyncInfo(status=reconstructed_sync, time_delta=metadata.get("sync_time_delta"))

        acquisition_metadata = metadata.get("acquisition_metadata")
        descriptor = FrameDescriptor(
            camera_id=header["camera_id"],
            sequence=frame_sequence,
            timestamp=header["timestamp"],
            monotonic_timestamp=descriptor_monotonic,
            thermal=thermal_meta,
            visible=visible_meta,
            sync=sync,
            metadata=MappingProxyType(
                dict(acquisition_metadata) if isinstance(acquisition_metadata, dict) else {}
            ),
        )
        frame = Frame(descriptor=descriptor, payload=payload)

        return RecordingRecord(
            camera_id=header["camera_id"],
            stream_type=stream_type,
            sequence=frame_sequence,
            timestamp=header["timestamp"],
            monotonic=header["monotonic"],
            payload_offset=header["payload_offset"],
            payload_length=header["payload_length"],
            width=width,
            height=height,
            pixel_format=pixel_format,
            dtype=dtype_name,
            bits_per_channel=header["bits_per_channel"],
            sync_status=header["sync_status"],
            sync_group_id=header["sync_group_id"],
            chunk_seq=entry.chunk_seq,
            record_offset=entry.offset,
            metadata=MappingProxyType(dict(metadata)),
            frame=frame,
        )

    # ------------------------------------------------------------------
    # Iteration
    # ------------------------------------------------------------------

    def iterate(self) -> Iterator[Frame]:
        """Yield frames chronologically.

        For INCOMPLETE recordings, yields every valid frame up to the
        truncation boundary and stops.  For CORRUPTED recordings, yields valid
        frames and raises :class:`RecordingReadError` at the first corrupted
        record (or skips it when the reader is ``tolerant``).
        """
        for entry in self._entries:
            try:
                yield self.read_frame(entry)
            except RecordingReadError:
                if self._tolerant:
                    continue
                if self._status == RecordingStatus.INCOMPLETE:
                    break
                raise

    def verify(self) -> VerifyResult:
        """Full-CRC verification pass over every record in the recording."""
        verified = 0
        failures: list[str] = []
        for chunk_seq in sorted(self._chunk_readers):
            chunk = self._chunk_readers[chunk_seq]
            try:
                record_iter = chunk.records()
                while True:
                    try:
                        record = next(record_iter)
                    except StopIteration:
                        break
                    verified += 1
                    if not record.verify_crc():
                        failures.append(
                            f"CRC mismatch at chunk {chunk_seq} offset {record.offset}"
                        )
            except ChunkReadError as exc:
                failures.append(str(exc))
        if failures:
            return VerifyResult(
                status=RecordingStatus.CORRUPTED,
                records_verified=verified,
                failures=tuple(failures),
            )
        return VerifyResult(status=self._status, records_verified=verified)

    # ------------------------------------------------------------------
    # Seeks (binary search)
    # ------------------------------------------------------------------

    def seek_by_timestamp(self, timestamp: float, stream_type: int | None = None) -> int:
        """Return the chronological position of the first record at/after ``timestamp``."""
        pos = bisect.bisect_left(self._timestamps, timestamp)
        if stream_type is not None:
            while pos < len(self._entries) and self._entries[pos].stream_type != stream_type:
                pos += 1
        return pos

    def seek_by_sequence(self, camera_id: str, stream_type: int, sequence: int) -> int:
        """Return the chronological position of ``(camera, stream, sequence)``."""
        seq_list = self._seq_map.get((camera_id, stream_type))
        if not seq_list:
            return len(self._entries)
        seqs = [s for s, _ in seq_list]
        pos = bisect.bisect_left(seqs, sequence)
        if pos < len(seqs) and seqs[pos] == sequence:
            return seq_list[pos][1]
        return len(self._entries)

    def entry_at(self, position: int) -> IndexEntry:
        if not 0 <= position < len(self._entries):
            raise RecordingReadError(f"Index position {position} is out of range")
        return self._entries[position]