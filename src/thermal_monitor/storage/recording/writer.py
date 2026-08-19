"""
storage.recording.writer -- RecordingWriter (Stage 5C).

Creates a recording directory, writes the manifest (WRITING -> COMPLETE),
writes immutable config snapshots, appends physical IR/VL frame records into
chunks, appends the binary index, and seals the manifest atomically.

The writer operates on the V3 Frame contract plus explicit metadata.  It has
no dependency on SQL, the camera driver, HALCON, SHM layout or Python serialization.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import IO, BinaryIO, Mapping, Sequence

import numpy as np

from thermal_monitor.core.frame import Frame
from thermal_monitor.storage.recording.chunks import ChunkWriter
from thermal_monitor.storage.recording.format import (
    CHUNKS_DIRNAME,
    CONFIG_ALARM,
    CONFIG_CALIBRATION,
    CONFIG_CAMERAS,
    CONFIG_DIRNAME,
    CONFIG_PTZ,
    CONFIG_ROIS,
    DEFAULT_CHUNK_TARGET_BYTES,
    EVENTS_ALARMS,
    EVENTS_DIRNAME,
    FORMAT_MAJOR,
    FORMAT_MINOR,
    INDEX_FILENAME,
    MANIFEST_FILENAME,
    RECORD_HEADER_SIZE,
    STATUS_COMPLETE,
    STATUS_WRITING,
    FormatError,
    compute_record_crc,
    dtype_to_code,
    pack_record_header,
    pixel_format_to_code,
    record_total_size,
    sync_status_to_code,
)
from thermal_monitor.storage.recording.index import IndexEntry, IndexWriter

# --------------------------------------------------------------------------
# JSON helpers (explicit data only -- no pickle anywhere)
# --------------------------------------------------------------------------


def _jsonable(value: object) -> object:
    """Convert an arbitrary value into JSON-safe explicit data."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _dump_json(path: Path, document: object) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def _write_atomic_json(path: Path, document: object) -> None:
    tmp = path.with_name(path.name + ".tmp")
    _dump_json(tmp, document)
    os.replace(tmp, path)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Recording metadata (explicit, JSON-able)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordingWriteMetadata:
    """Explicit metadata for creating a recording.

    All fields are JSON-able explicit data; no Python object identity is
    stored in the recording.
    """

    recording_id: str
    cameras: Sequence[str]
    streams: Mapping[str, Sequence[str]] = field(default_factory=lambda: MappingProxyType({}))
    trigger: str = "manual"  # MANUAL | ALARM | SCHEDULED | DIAGNOSTIC
    trigger_alarm_id: str | None = None
    created_at: str | None = None  # ISO-8601; auto-generated when None
    application_name: str = "Thermal Monitoring System V3"
    application_version: str = "3.0.0"
    start_time: float | None = None  # recording-relative base (first frame if None)
    pre_alarm_seconds: float = 0.0
    post_alarm_seconds: float = 0.0

    # Immutable config snapshots (explicit dicts / dataclasses -> JSON)
    camera_snapshots: Sequence[Mapping[str, object]] = ()
    roi_snapshots: Sequence[Mapping[str, object]] = ()
    ptz_snapshots: Sequence[Mapping[str, object]] = ()
    calibration_snapshots: Sequence[Mapping[str, object]] = ()
    alarm_snapshots: Sequence[Mapping[str, object]] = ()
    snapshot_timestamp: str | None = None  # ISO-8601 for snapshot wrappers
    snapshot_source: str = "file"

    def __post_init__(self) -> None:
        if not self.recording_id:
            raise ValueError("recording_id is required")
        if not self.cameras:
            raise ValueError("cameras must contain at least one camera")


# --------------------------------------------------------------------------
# RecordingWriter
# --------------------------------------------------------------------------


class RecordingWriter:
    """Writes one recording directory from V3 frames plus explicit metadata.

    Lifecycle: ``open()`` -> ``write_frame()``... -> ``finalize()``.

    On ``open`` the manifest is written with ``status = WRITING``.  Only
    ``finalize()`` rewrites it to ``status = COMPLETE`` with ``finalized_at``.
    If the writer is abandoned (context-manager exit with an exception, or a
    crash), the manifest remains ``WRITING`` and the recording is detectable
    as INCOMPLETE by any reader.
    """

    def __init__(
        self,
        output_dir: str | Path,
        metadata: RecordingWriteMetadata,
        *,
        chunk_target_bytes: int = DEFAULT_CHUNK_TARGET_BYTES,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._metadata = metadata
        self._chunk_target_bytes = chunk_target_bytes

        self._recording_dir: Path | None = None
        self._chunks_dir: Path | None = None
        self._chunk_writer: ChunkWriter | None = None
        self._chunk_seq = 0
        self._index_writer: IndexWriter | None = None
        self._frame_count = 0
        self._chunk_count = 0
        self._start_time: float | None = None
        self._end_time: float | None = None
        self._base_time: float | None = metadata.start_time
        self._camera_list: list[str] = list(metadata.cameras)
        self._camera_set = set(self._camera_list)
        self._opened = False
        self._finalized = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def recording_dir(self) -> Path:
        if self._recording_dir is None:
            raise RuntimeError("recording has not been opened")
        return self._recording_dir

    @property
    def is_open(self) -> bool:
        return self._opened

    @property
    def is_finalized(self) -> bool:
        return self._finalized

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def __enter__(self) -> "RecordingWriter":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.finalize()
        else:
            self.abort()

    def open(self) -> None:
        if self._opened:
            raise RuntimeError("recording already open")
        if self._recording_dir is not None:
            raise RuntimeError("recording already created")
        metadata = self._metadata

        self._recording_dir = self._output_dir / metadata.recording_id
        if self._recording_dir.exists():
            raise FileExistsError(f"Recording directory already exists: {self._recording_dir}")
        self._chunks_dir = self._recording_dir / CHUNKS_DIRNAME
        config_dir = self._recording_dir / CONFIG_DIRNAME
        events_dir = self._recording_dir / EVENTS_DIRNAME
        for directory in (self._recording_dir, self._chunks_dir, config_dir, events_dir):
            directory.mkdir(parents=True)

        created_at = metadata.created_at or _utc_now_iso()
        self._created_at = created_at

        # Config snapshots are written once and never rewritten (immutable).
        self._write_snapshot(config_dir / CONFIG_CAMERAS, metadata.camera_snapshots)
        self._write_snapshot(config_dir / CONFIG_ROIS, metadata.roi_snapshots)
        self._write_snapshot(config_dir / CONFIG_PTZ, metadata.ptz_snapshots)
        self._write_snapshot(config_dir / CONFIG_CALIBRATION, metadata.calibration_snapshots)
        self._write_snapshot(config_dir / CONFIG_ALARM, metadata.alarm_snapshots)
        _dump_json(
            events_dir / EVENTS_ALARMS,
            {"format_version": {"major": FORMAT_MAJOR, "minor": FORMAT_MINOR}, "items": []},
        )

        self._write_manifest(status=STATUS_WRITING, finalized_at=None)

        # Index camera dictionary seeded from the manifest camera list so
        # camera_id_ref ordering always matches the manifest.
        self._index_writer = IndexWriter(self._recording_dir / INDEX_FILENAME)
        self._index_writer.open()
        for camera_id in self._camera_list:
            self._index_writer.camera_ref(camera_id)

        self._new_chunk()
        self._opened = True

    def _new_chunk(self) -> None:
        assert self._chunks_dir is not None
        path = self._chunks_dir / f"chunk_{self._chunk_seq:03d}.tmsr"
        self._chunk_writer = ChunkWriter(path, self._chunk_seq, target_bytes=self._chunk_target_bytes)
        self._chunk_seq += 1
        self._chunk_count += 1

    def _write_snapshot(self, path: Path, items: Sequence[Mapping[str, object]]) -> None:
        document = {
            "snapshot_timestamp": self._metadata.snapshot_timestamp
            or self._metadata.created_at
            or self._created_at,
            "source": self._metadata.snapshot_source,
            "schema_version": 1,
            "items": [_jsonable(item) for item in items],
        }
        _dump_json(path, document)

    def _manifest_document(self, status: str, finalized_at: str | None) -> dict:
        metadata = self._metadata
        streams = {
            camera_id: {stream_name: True for stream_name in sorted(names)}
            for camera_id, names in metadata.streams.items()
        }
        if self._start_time is not None and self._end_time is not None:
            duration = max(0.0, self._end_time - self._start_time)
        else:
            duration = 0.0
        return {
            "format_version": {"major": FORMAT_MAJOR, "minor": FORMAT_MINOR},
            "recording_id": metadata.recording_id,
            "created_at": self._created_at,
            "application": {
                "name": metadata.application_name,
                "version": metadata.application_version,
            },
            "status": status,
            "finalized_at": finalized_at,
            "trigger": metadata.trigger,
            "trigger_alarm_id": metadata.trigger_alarm_id,
            "cameras": list(self._camera_list),
            "streams": streams,
            "start_time": self._start_time,
            "end_time": self._end_time,
            "frame_count": self._frame_count,
            "duration_seconds": duration,
            "pre_alarm_seconds": metadata.pre_alarm_seconds,
            "post_alarm_seconds": metadata.post_alarm_seconds,
            "chunk_count": self._chunk_count,
            "index_count": self._frame_count,
            "sync_groups": 0,
            "sql_metadata": {
                "recording_id": metadata.recording_id,
                "path": self._recording_dir.name,
                "status": status,
            },
        }

    def _write_manifest(self, status: str, finalized_at: str | None) -> None:
        assert self._recording_dir is not None
        document = self._manifest_document(status, finalized_at)
        _write_atomic_json(self._recording_dir / MANIFEST_FILENAME, document)

    # -- frame writing -----------------------------------------------------

    def write_frame(
        self,
        frame: Frame,
        *,
        position_id: str | None = None,
        sync_group_id: int | None = None,
    ) -> list[IndexEntry]:
        """Write one physical record per stream present in the frame.

        IR and VL payloads are never merged into a single record.  Returns the
        index entries appended for the frame (one per physical stream).
        """
        if not self._opened:
            raise RuntimeError("recording is not open")
        if self._finalized:
            raise RuntimeError("recording is already finalized")

        if self._base_time is None:
            self._base_time = frame.descriptor.timestamp

        if not self._camera_registered(frame.descriptor.camera_id):
            self._register_camera(frame.descriptor.camera_id)

        entries: list[IndexEntry] = []
        thermal = frame.payload.thermal
        visible = frame.payload.visible
        if thermal is not None:
            if not frame.descriptor.thermal.present:
                raise FormatError("thermal payload present but thermal.present is False")
            entries.append(
                self._write_stream_record(
                    frame=frame,
                    stream_meta=frame.descriptor.thermal,
                    array=thermal,
                    stream_type=1,
                    position_id=position_id,
                    sync_group_id=sync_group_id,
                )
            )
        if visible is not None:
            if not frame.descriptor.visible.present:
                raise FormatError("visible payload present but visible.present is False")
            entries.append(
                self._write_stream_record(
                    frame=frame,
                    stream_meta=frame.descriptor.visible,
                    array=visible,
                    stream_type=2,
                    position_id=position_id,
                    sync_group_id=sync_group_id,
                )
            )
        return entries

    def _camera_registered(self, camera_id: str) -> bool:
        return camera_id in self._camera_set

    def _register_camera(self, camera_id: str) -> None:
        self._camera_set.add(camera_id)
        self._camera_list.append(camera_id)
        assert self._index_writer is not None
        self._index_writer.camera_ref(camera_id)

    def _write_stream_record(
        self,
        *,
        frame: Frame,
        stream_meta,
        array: np.ndarray,
        stream_type: int,
        position_id: str | None,
        sync_group_id: int | None,
    ) -> IndexEntry:
        assert self._base_time is not None
        assert self._chunk_writer is not None
        assert self._index_writer is not None

        payload = array.tobytes()
        dtype_code = int(dtype_to_code(array.dtype))
        pixel_format = pixel_format_to_code(stream_meta.pixel_format or "")

        # Dimensions: prefer the descriptor metadata, validate against the array.
        if stream_meta.width is not None and stream_meta.height is not None:
            if (stream_meta.width, stream_meta.height) != (array.shape[1], array.shape[0]):
                raise FormatError(
                    f"stream dimensions {(stream_meta.width, stream_meta.height)} "
                    f"do not match payload shape {array.shape}"
                )
            width, height = stream_meta.width, stream_meta.height
        else:
            height, width = array.shape[0], array.shape[1]

        if stream_meta.bits_per_channel is not None:
            bits_per_channel = stream_meta.bits_per_channel
        else:
            bits_per_channel = array.dtype.itemsize * 8

        relative = frame.descriptor.timestamp - self._base_time
        metadata_dict = {
            "stream_meta": {
                "sequence": stream_meta.sequence,
                "timestamp": stream_meta.timestamp,
                "monotonic_timestamp": stream_meta.monotonic_timestamp,
                "hardware_timestamp": stream_meta.hardware_timestamp,
                "dtype": str(array.dtype),
                "array_shape": list(array.shape),
            },
            "recording_relative_timestamp": relative,
            "sync_time_delta": frame.descriptor.sync.time_delta,
            "position_id": position_id,
            "acquisition_metadata": _jsonable(dict(frame.descriptor.metadata)),
        }
        metadata_bytes = (
            json.dumps(metadata_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            .encode("utf-8")
        )

        record_size = record_total_size(len(metadata_bytes), len(payload))
        if not self._chunk_writer.ensure_capacity(record_size):
            self._chunk_writer.finalize()
            self._new_chunk()

        chunk_seq = self._chunk_writer.chunk_seq
        record_offset = self._chunk_writer.position
        payload_offset = record_offset + RECORD_HEADER_SIZE + len(metadata_bytes)

        header = pack_record_header(
            stream_type=stream_type,
            camera_id=frame.descriptor.camera_id,
            sequence=frame.descriptor.sequence,
            timestamp=frame.descriptor.timestamp,
            monotonic=relative,
            payload_offset=payload_offset,
            payload_length=len(payload),
            width=width,
            height=height,
            pixel_format=int(pixel_format),
            dtype_code=dtype_code,
            bits_per_channel=bits_per_channel,
            sync_status=int(sync_status_to_code(frame.descriptor.sync.status)),
            sync_group_id=-1 if sync_group_id is None else sync_group_id,
            metadata_len=len(metadata_bytes),
        )
        checksum = compute_record_crc(header, metadata_bytes, payload)
        self._chunk_writer.write_record(
            header + metadata_bytes + payload + checksum.to_bytes(4, "little")
        )

        timestamp = frame.descriptor.timestamp
        if self._start_time is None or timestamp < self._start_time:
            self._start_time = timestamp
        if self._end_time is None or timestamp > self._end_time:
            self._end_time = timestamp
        self._frame_count += 1

        self._index_writer.append(
            camera_id=frame.descriptor.camera_id,
            stream_type=stream_type,
            flags=0,
            sequence=frame.descriptor.sequence,
            timestamp=timestamp,
            chunk_seq=chunk_seq,
            offset=record_offset,
            payload_length=len(payload),
            checksum=checksum,
            width=width,
            height=height,
            pixel_format=int(pixel_format),
        )
        return IndexEntry(
            camera_id=frame.descriptor.camera_id,
            camera_id_ref=self._index_writer.camera_ref(frame.descriptor.camera_id),
            stream_type=stream_type,
            flags=0,
            sequence=frame.descriptor.sequence,
            timestamp=timestamp,
            chunk_seq=chunk_seq,
            offset=record_offset,
            payload_length=len(payload),
            checksum=checksum,
            width=width,
            height=height,
            pixel_format=int(pixel_format),
        )

    # -- finalization ------------------------------------------------------

    def finalize(self, finalized_at: str | None = None) -> Path:
        """Seal the recording: close chunks, finalize index, mark COMPLETE.

        ``finalized_at`` is an optional ISO-8601 timestamp (auto-generated
        when None) that lets callers control determinism.
        """
        if self._finalized:
            return self.recording_dir
        if not self._opened:
            raise RuntimeError("recording is not open")

        if self._chunk_writer is not None and not self._chunk_writer.closed:
            self._chunk_writer.finalize()
        assert self._index_writer is not None
        self._index_writer.finalize()

        self._write_manifest(status=STATUS_COMPLETE, finalized_at=finalized_at or _utc_now_iso())
        self._opened = False
        self._finalized = True
        return self.recording_dir

    def abort(self) -> None:
        """Abandon the recording without sealing it (stays WRITING / INCOMPLETE)."""
        if self._finalized:
            return
        if self._chunk_writer is not None and not self._chunk_writer.closed:
            self._chunk_writer.close_without_trailer()
        if self._index_writer is not None:
            try:
                self._index_writer.finalize()
            except RuntimeError:
                pass
        self._opened = False