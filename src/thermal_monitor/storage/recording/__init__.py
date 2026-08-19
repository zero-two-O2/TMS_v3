"""
storage.recording -- Persistent recording container (Stage 5C).

Owns the binary recording format (writer, chunk store, index) that stores
raw acquisition data independently of pickle, HALCON, SHM layout, SQL
Server and Python class serialization.
"""

from .format import (
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
from .chunks import ChunkReadError, ChunkReader, ChunkWriter
from .index import (
    IndexEntry,
    IndexReadError,
    IndexReader,
    IndexWriter,
)
from .writer import (
    RecordingWriteMetadata,
    RecordingWriter,
)

# Legacy Stage 5A recorder/sinks (pickle-based) - kept for backward compatibility
# These are defined in storage.recording.recording module (recording.py)
from .recording import (
    FileRecordingSink,
    NullRecordingSink,
    Recorder,
    RollingFrameBuffer,
)

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