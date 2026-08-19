"""
offline.source -- OfflineFrameSource for replaying recordings as Frame streams.

The OfflineFrameSource reconstructs V3 Frame objects from physical recording
records (one record per IR or VL stream). It does not merge IR/VL records;
each physical record becomes a Frame with the corresponding payload present.

Architecture:
- storage owns recording persistence (writer, format, chunks, index)
- offline owns reading/replay of recordings (RecordingReader, OfflineFrameSource)
- processing owns processing algorithms (pipeline, temperature, ROI, alarms)
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterator, Mapping, Optional, Sequence

import numpy as np

from thermal_monitor.core.frame import (
    Frame,
    FrameDescriptor,
    FramePayload,
    StreamMetadata,
    SyncInfo,
    SyncStatus,
)
from thermal_monitor.offline.reader import (
    RecordingReader,
    RecordingRecord,
    RecordingStatus,
    RecordingReadError,
    RecordingCorruptError,
)
from thermal_monitor.processing.pipeline import FrameSource
from thermal_monitor.storage.recording.format import StreamType
from thermal_monitor.storage.recording.index import IndexEntry


class StreamFilter(str, Enum):
    """Stream type filter for offline source."""

    ALL = "all"
    IR = "ir"
    VL = "vl"


@dataclass(frozen=True, slots=True)
class OfflineFrameSourceConfig:
    """Configuration for OfflineFrameSource."""

    camera_id: str | None = None  # None = all cameras
    stream_filter: StreamFilter = StreamFilter.ALL
    start_timestamp: float | None = None
    end_timestamp: float | None = None
    start_sequence: int | None = None
    tolerant: bool = False  # Skip corrupt records instead of raising


class OfflineFrameSource:
    """FrameSource for offline playback of recorded frames.

    Reads frames from a recording directory via RecordingReader and provides
    sequential or random access. Each physical record (IR or VL) reconstructs
    to a separate Frame with the corresponding payload.

    The source preserves the V3 Frame contract exactly:
    - camera_id, sequence, timestamps, dimensions, dtype, pixel format
    - raw payload as read-only NumPy arrays
    - acquisition metadata, synchronization metadata
    - sync_group_id where available (UNKNOWN/MISSING when not available)
    """

    def __init__(
        self,
        recording_dir: str | Path,
        *,
        config: OfflineFrameSourceConfig | None = None,
    ) -> None:
        self._recording_dir = Path(recording_dir)
        self._config = config or OfflineFrameSourceConfig()
        self._reader: RecordingReader | None = None
        self._filtered_entries: list[IndexEntry] = []
        self._current_index = 0
        self._opened = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open the recording for playback."""
        if self._opened:
            return
        self._reader = RecordingReader(self._recording_dir, tolerant=self._config.tolerant)
        self._build_filtered_entries()
        self._current_index = 0
        self._opened = True

    def close(self) -> None:
        """Close the recording."""
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        self._filtered_entries.clear()
        self._current_index = 0
        self._opened = False

    def __enter__(self) -> "OfflineFrameSource":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._opened

    @property
    def recording_dir(self) -> Path:
        return self._recording_dir

    @property
    def status(self) -> RecordingStatus:
        if self._reader is None:
            return RecordingStatus.CORRUPTED
        return self._reader.status

    @property
    def camera_ids(self) -> list[str]:
        if self._reader is None:
            return []
        return self._reader.camera_ids

    # ------------------------------------------------------------------
    # FrameSource protocol
    # ------------------------------------------------------------------

    def get_next_frame(self) -> Frame | None:
        """Get the next frame in playback order."""
        if not self._opened:
            raise RuntimeError("OfflineFrameSource not open; call open() first")
        if self._current_index >= len(self._filtered_entries):
            return None
        frame = self._read_frame_at(self._current_index)
        self._current_index += 1
        return frame

    def get_latest_frame(self) -> Frame | None:
        """Get the most recent frame without advancing."""
        if not self._opened:
            raise RuntimeError("OfflineFrameSource not open; call open() first")
        if not self._filtered_entries:
            return None
        return self._read_frame_at(len(self._filtered_entries) - 1)

    def seek(self, sequence: int) -> bool:
        """Seek to a specific frame sequence (for the current camera filter).

        Returns True if seek was successful.
        """
        if not self._opened:
            raise RuntimeError("OfflineFrameSource not open; call open() first")
        if self._reader is None:
            return False

        # Filter entries by sequence
        for i, entry in enumerate(self._filtered_entries):
            if entry.sequence == sequence:
                self._current_index = i
                return True
        return False

    def seek_to_index(self, index: int) -> bool:
        """Seek to a specific index in the filtered entry list."""
        if not self._opened:
            raise RuntimeError("OfflineFrameSource not open; call open() first")
        if 0 <= index < len(self._filtered_entries):
            self._current_index = index
            return True
        return False

    def first(self) -> Frame | None:
        """Seek to first frame and return it."""
        if not self._opened:
            raise RuntimeError("OfflineFrameSource not open; call open() first")
        if not self._filtered_entries:
            return None
        self._current_index = 0
        return self._read_frame_at(0)

    def current(self) -> Frame | None:
        """Return the current frame without advancing."""
        if not self._opened:
            raise RuntimeError("OfflineFrameSource not open; call open() first")
        if not self._filtered_entries or self._current_index >= len(self._filtered_entries):
            return None
        return self._read_frame_at(self._current_index)

    def seek_timestamp(self, timestamp: float, stream_type: int | None = None) -> bool:
        """Seek to the first frame at or after the given timestamp.

        Args:
            timestamp: Target timestamp (absolute, epoch seconds)
            stream_type: Optional stream filter (1=IR, 2=VL)

        Returns:
            True if seek was successful, False if timestamp is past the end.
        """
        if not self._opened:
            raise RuntimeError("OfflineFrameSource not open; call open() first")
        if self._reader is None:
            return False

        pos = self._reader.seek_by_timestamp(timestamp, stream_type)
        if pos < len(self._filtered_entries):
            # Map from reader position to filtered position
            target_entry = self._reader.entry_at(pos)
            # Find the matching entry in filtered list
            for i, entry in enumerate(self._filtered_entries):
                if (entry.chunk_seq == target_entry.chunk_seq and
                    entry.offset == target_entry.offset and
                    entry.stream_type == target_entry.stream_type):
                    self._current_index = i
                    return True
        return False

    def seek_sequence(self, camera_id: str, stream_type: int, sequence: int) -> bool:
        """Seek to a specific (camera, stream, sequence) combination.

        Returns True if found.
        """
        if not self._opened:
            raise RuntimeError("OfflineFrameSource not open; call open() first")
        if self._reader is None:
            return False

        pos = self._reader.seek_by_sequence(camera_id, stream_type, sequence)
        if pos < len(self._filtered_entries):
            target_entry = self._reader.entry_at(pos)
            for i, entry in enumerate(self._filtered_entries):
                if (entry.chunk_seq == target_entry.chunk_seq and
                    entry.offset == target_entry.offset and
                    entry.stream_type == target_entry.stream_type):
                    self._current_index = i
                    return True
        return False

    @property
    def camera_id(self) -> str:
        """Primary camera ID this source produces frames for.

        Returns the configured camera_id, or the first camera in the recording
        if not filtered.
        """
        if self._config.camera_id is not None:
            return self._config.camera_id
        if self._reader is not None:
            cams = self._reader.camera_ids
            if cams:
                return cams[0]
        return "unknown"

    @property
    def is_live(self) -> bool:
        return False

    # ------------------------------------------------------------------
    # Iteration and length
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._filtered_entries)

    def __iter__(self) -> Iterator[Frame]:
        """Iterate over all frames in playback order."""
        if not self._opened:
            raise RuntimeError("OfflineFrameSource not open; call open() first")
        for i in range(len(self._filtered_entries)):
            yield self._read_frame_at(i)

    def reset(self) -> None:
        """Reset playback to the beginning."""
        self._current_index = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_filtered_entries(self) -> None:
        """Build the filtered entry list based on configuration."""
        if self._reader is None:
            return

        entries = self._reader.entries

        # Filter by camera
        if self._config.camera_id is not None:
            entries = [e for e in entries if e.camera_id == self._config.camera_id]

        # Filter by stream type
        if self._config.stream_filter == StreamFilter.IR:
            entries = [e for e in entries if e.stream_type == StreamType.IR]
        elif self._config.stream_filter == StreamFilter.VL:
            entries = [e for e in entries if e.stream_type == StreamType.VL]

        # Filter by timestamp range
        if self._config.start_timestamp is not None:
            entries = [e for e in entries if e.timestamp >= self._config.start_timestamp]
        if self._config.end_timestamp is not None:
            entries = [e for e in entries if e.timestamp <= self._config.end_timestamp]

        # Filter by sequence (start from)
        if self._config.start_sequence is not None:
            entries = [e for e in entries if e.sequence >= self._config.start_sequence]

        self._filtered_entries = entries

    def _read_frame_at(self, index: int) -> Frame:
        """Read and reconstruct a Frame at the given filtered index."""
        if self._reader is None:
            raise RuntimeError("Reader not initialized")
        if not 0 <= index < len(self._filtered_entries):
            raise IndexError(f"Index {index} out of range ({len(self._filtered_entries)})")

        entry = self._filtered_entries[index]
        return self._reader.read_frame(entry)


# ----------------------------------------------------------------------
# Convenience factory functions
# ----------------------------------------------------------------------


def open_offline_source(
    recording_dir: str | Path,
    *,
    camera_id: str | None = None,
    stream_filter: StreamFilter = StreamFilter.ALL,
    start_timestamp: float | None = None,
    end_timestamp: float | None = None,
    start_sequence: int | None = None,
    tolerant: bool = False,
) -> OfflineFrameSource:
    """Convenience function to create and open an OfflineFrameSource.

    Args:
        recording_dir: Path to the recording directory.
        camera_id: Filter to a specific camera (None = all).
        stream_filter: Filter by stream type (ALL, IR, VL).
        start_timestamp: Only include frames at or after this timestamp.
        end_timestamp: Only include frames at or before this timestamp.
        start_sequence: Only include frames with sequence >= this value.
        tolerant: Skip corrupt records instead of raising.

    Returns:
        Opened OfflineFrameSource ready for playback.
    """
    config = OfflineFrameSourceConfig(
        camera_id=camera_id,
        stream_filter=stream_filter,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        start_sequence=start_sequence,
        tolerant=tolerant,
    )
    source = OfflineFrameSource(recording_dir, config=config)
    source.open()
    return source