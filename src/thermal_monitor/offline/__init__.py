"""
Offline domain: saved raw data as a frame source.

Provides OfflineFrameSource for replaying recordings as Frame streams,
and RecordingReader for low-level recording access.
"""

from thermal_monitor.offline.reader import (
    RecordingCorruptError,
    RecordingReadError,
    RecordingReader,
    RecordingRecord,
    RecordingStatus,
    VerifyResult,
)
from thermal_monitor.offline.source import (
    OfflineFrameSource,
    OfflineFrameSourceConfig,
    StreamFilter,
    open_offline_source,
)

__all__ = [
    "RecordingCorruptError",
    "RecordingReadError",
    "RecordingReader",
    "RecordingRecord",
    "RecordingStatus",
    "VerifyResult",
    "OfflineFrameSource",
    "OfflineFrameSourceConfig",
    "StreamFilter",
    "open_offline_source",
]