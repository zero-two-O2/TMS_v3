"""
storage.recording -- Persistent recording container (Stage 5C).

Owns the binary recording format (writer, chunk store, index) that stores
raw acquisition data independently of pickle, HALCON, SHM layout, SQL
Server and Python class serialization.
"""

from thermal_monitor.storage.recording.format import (
    CAMERA_ID_FIELD_SIZE,
    CHUNK_HEADER_SIZE,
    CHUNK_TRAILER_SIZE,
    CHUNK_END_MAGIC,
    CHUNK_MAGIC,
    CRC_SIZE,
    DEFAULT_CHUNK_TARGET_BYTES,
    FORMAT_MAJOR,
    FORMAT_MINOR,
    INDEX_ENTRY_SIZE,
    INDEX_MAGIC,
    INDEX_HEADER_SIZE,
    RECORD_HEADER_SIZE,
    RECORD_MAGIC,
    DTypeCode,
    PixelFormat,
    StreamType,
    SyncStatusCode,
)
from thermal_monitor.storage.recording.chunks import ChunkReadError, ChunkReader, ChunkWriter
from thermal_monitor.storage.recording.index import (
    IndexEntry,
    IndexReadError,
    IndexReader,
    IndexWriter,
)
from thermal_monitor.storage.recording.writer import (
    RecordingWriteMetadata,
    RecordingWriter,
)

# Legacy Stage 5A recorder/sinks live in the ``storage.recording`` module,
# which this package shadows; load it under a distinct name and re-export so
# existing imports keep working.  It uses pickle and remains legacy -- the
# Stage 5C binary writer above does not.
import importlib.util
import sys as _sys
from pathlib import Path as _Path

_LEGACY_RECORDING_MODULE = _Path(__file__).resolve().parent.parent / "recording.py"
_legacy_spec = importlib.util.spec_from_file_location(
    "thermal_monitor.storage.recording_legacy", _LEGACY_RECORDING_MODULE
)
_legacy_mod = importlib.util.module_from_spec(_legacy_spec)
_sys.modules["thermal_monitor.storage.recording_legacy"] = _legacy_mod
_legacy_spec.loader.exec_module(_legacy_mod)

FileRecordingSink = _legacy_mod.FileRecordingSink
NullRecordingSink = _legacy_mod.NullRecordingSink
Recorder = _legacy_mod.Recorder
RollingFrameBuffer = _legacy_mod.RollingFrameBuffer

__all__ = [
    "CAMERA_ID_FIELD_SIZE",
    "CHUNK_HEADER_SIZE",
    "CHUNK_TRAILER_SIZE",
    "CHUNK_END_MAGIC",
    "CHUNK_MAGIC",
    "CRC_SIZE",
    "DEFAULT_CHUNK_TARGET_BYTES",
    "FORMAT_MAJOR",
    "FORMAT_MINOR",
    "INDEX_ENTRY_SIZE",
    "INDEX_MAGIC",
    "INDEX_HEADER_SIZE",
    "RECORD_HEADER_SIZE",
    "RECORD_MAGIC",
    "DTypeCode",
    "PixelFormat",
    "StreamType",
    "SyncStatusCode",
    "ChunkReadError",
    "ChunkReader",
    "ChunkWriter",
    "IndexEntry",
    "IndexReadError",
    "IndexReader",
    "IndexWriter",
    "RecordingWriteMetadata",
    "RecordingWriter",
    "FileRecordingSink",
    "NullRecordingSink",
    "Recorder",
    "RollingFrameBuffer",
]
