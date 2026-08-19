"""
storage.recording.index -- Compact append-only binary frame index.

``index.bin`` is a fixed 56-byte-entry array: per-record (camera_id_ref,
stream, sequence, timestamp, chunk_seq, offset, payload_length, checksum,
width, height, pixel_format).  Camera id strings are stored once in a
dictionary (the manifest ``cameras`` list); entries reference them by index.

The on-disk order is append order.  The reader builds a sorted view
(primary: timestamp, secondary: sequence, tertiary: stream_type) in memory
and performs **binary search** over it -- there is no O(1) seek.

The index contains no raw payload bytes and no metadata JSON.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Sequence

from thermal_monitor.storage.recording.format import (
    FORMAT_MAJOR,
    INDEX_ENTRY_SIZE,
    INDEX_ENTRY_STRUCT,
    INDEX_HEADER_SIZE,
    INDEX_HEADER_STRUCT,
    INDEX_MAGIC,
    FormatError,
)


class IndexReadError(FormatError):
    """Raised when the index file violates the documented layout."""


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """One decoded index entry (56 bytes on disk)."""

    camera_id: str
    camera_id_ref: int
    stream_type: int
    flags: int
    sequence: int
    timestamp: float
    chunk_seq: int
    offset: int
    payload_length: int
    checksum: int
    width: int
    height: int
    pixel_format: int


def pack_index_entry(
    *,
    camera_id_ref: int,
    stream_type: int,
    flags: int,
    sequence: int,
    timestamp: float,
    chunk_seq: int,
    offset: int,
    payload_length: int,
    checksum: int,
    width: int,
    height: int,
    pixel_format: int,
) -> bytes:
    return INDEX_ENTRY_STRUCT.pack(
        camera_id_ref,
        stream_type,
        flags,
        sequence,
        timestamp,
        chunk_seq,
        offset,
        payload_length,
        checksum,
        width,
        height,
        pixel_format,
    )


def unpack_index_entry(raw: bytes, camera_id: str) -> IndexEntry:
    if len(raw) != INDEX_ENTRY_SIZE:
        raise IndexReadError(f"Index entry is {len(raw)} bytes, expected {INDEX_ENTRY_SIZE}")
    (
        camera_id_ref,
        stream_type,
        flags,
        sequence,
        timestamp,
        chunk_seq,
        offset,
        payload_length,
        checksum,
        width,
        height,
        pixel_format,
    ) = INDEX_ENTRY_STRUCT.unpack(raw)
    return IndexEntry(
        camera_id=camera_id,
        camera_id_ref=camera_id_ref,
        stream_type=stream_type,
        flags=flags,
        sequence=sequence,
        timestamp=timestamp,
        chunk_seq=chunk_seq,
        offset=offset,
        payload_length=payload_length,
        checksum=checksum,
        width=width,
        height=height,
        pixel_format=pixel_format,
    )


def _index_header(count: int) -> bytes:
    return INDEX_HEADER_STRUCT.pack(
        INDEX_MAGIC, FORMAT_MAJOR, count, INDEX_ENTRY_SIZE, b"\x00" * 16
    )


class IndexWriter:
    """Append-only writer for ``index.bin``."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._file: BinaryIO | None = None
        self._count = 0
        self._camera_list: list[str] = []
        self._camera_ref: dict[str, int] = {}

    def open(self) -> None:
        if self._file is not None:
            raise RuntimeError("index already open")
        self._file = open(self._path, "wb")
        self._file.write(_index_header(0))

    @property
    def path(self) -> Path:
        return self._path

    @property
    def count(self) -> int:
        return self._count

    @property
    def camera_list(self) -> list[str]:
        """Camera id dictionary in reference-index order (matches the manifest)."""
        return list(self._camera_list)

    def camera_ref(self, camera_id: str) -> int:
        ref = self._camera_ref.get(camera_id)
        if ref is None:
            ref = len(self._camera_list)
            self._camera_ref[camera_id] = ref
            self._camera_list.append(camera_id)
        return ref

    def append(self, *, camera_id: str, **fields) -> None:
        """Append one 56-byte entry.  ``fields`` are the entry fields (see pack_index_entry)."""
        if self._file is None:
            raise RuntimeError("index not open")
        ref = self.camera_ref(camera_id)
        self._file.write(pack_index_entry(camera_id_ref=ref, **fields))
        self._count += 1

    def finalize(self) -> None:
        """Rewrite the header with the final record count and close."""
        if self._file is None:
            raise RuntimeError("index not open")
        self._file.seek(0)
        self._file.write(_index_header(self._count))
        self._file.flush()
        self._file.close()
        self._file = None


class IndexReader:
    """Reader over ``index.bin`` with binary-search seeks."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._entries: list[IndexEntry] = []
        self._timestamps: list[float] = []
        self._seq_map: dict[tuple[str, int], list[tuple[int, int]]] = defaultdict(list)
        self._camera_list: list[str] = []
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def camera_list(self) -> list[str]:
        return list(self._camera_list)

    @property
    def entries(self) -> list[IndexEntry]:
        """Chronologically sorted entries (timestamp, sequence, stream_type)."""
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[IndexEntry]:
        return iter(self._entries)

    def load(self, camera_list: Sequence[str] | None = None) -> None:
        """Read and decode the index, resolving camera ids via the dictionary.

        ``camera_list`` is the manifest ``cameras`` list; refs index into it.
        """
        raw_header = self._read(0, INDEX_HEADER_SIZE)
        magic, version, count, entry_size, reserved = INDEX_HEADER_STRUCT.unpack(raw_header)
        if magic != INDEX_MAGIC:
            raise IndexReadError(f"Bad index magic {magic!r} in {self._path.name}")
        if version != FORMAT_MAJOR:
            raise IndexReadError(f"Unsupported index version {version} in {self._path.name}")
        if entry_size != INDEX_ENTRY_SIZE:
            raise IndexReadError(
                f"Index entry size {entry_size} does not match expected {INDEX_ENTRY_SIZE}"
            )
        if count < 0:
            raise IndexReadError(f"Negative index record count {count}")
        self._camera_list = list(camera_list or [])

        raw_entries = self._read(INDEX_HEADER_SIZE, count * INDEX_ENTRY_SIZE)
        entries: list[IndexEntry] = []
        for i in range(count):
            start = i * INDEX_ENTRY_SIZE
            camera_id = self._resolve_camera(ref=unpack_index_entry(
                raw_entries[start : start + INDEX_ENTRY_SIZE], camera_id=""
            ).camera_id_ref)
            entries.append(
                unpack_index_entry(raw_entries[start : start + INDEX_ENTRY_SIZE], camera_id=camera_id)
            )
        self._entries = sorted(
            entries,
            key=lambda e: (e.timestamp, e.sequence, e.stream_type),
        )
        self._timestamps = [e.timestamp for e in self._entries]
        self._seq_map.clear()
        for pos, entry in enumerate(self._entries):
            self._seq_map[(entry.camera_id, entry.stream_type)].append((entry.sequence, pos))
        for seq_list in self._seq_map.values():
            seq_list.sort()
        self._loaded = True

    def _resolve_camera(self, ref: int) -> str:
        if 0 <= ref < len(self._camera_list):
            return self._camera_list[ref]
        return f"cam_ref_{ref}"

    def _read(self, offset: int, length: int) -> bytes:
        with open(self._path, "rb") as f:
            f.seek(offset)
            data = f.read(length)
        if len(data) != length:
            raise IndexReadError(f"Index file ends early at offset {offset}")
        return data

    # -- seeks / filters ---------------------------------------------------

    def seek_by_timestamp(self, timestamp: float, stream_type: int | None = None) -> int:
        """Return the sorted position of the first entry at/after ``timestamp``.

        Uses binary search over the timestamp-sorted array.  If a stream
        filter is given, scans forward from that position for the first
        matching stream (O(log n) + scan).
        """
        if not self._loaded:
            raise RuntimeError("index not loaded")
        pos = bisect.bisect_left(self._timestamps, timestamp)
        if stream_type is None:
            return pos
        while pos < len(self._entries):
            if self._entries[pos].stream_type == stream_type:
                return pos
            pos += 1
        return len(self._entries)

    def seek_by_sequence(self, camera_id: str, stream_type: int, sequence: int) -> int:
        """Return the sorted position of the entry for ``(camera, stream, sequence)``.

        Uses binary search over the per-(camera, stream) sequence list.
        Returns ``len(entries)`` when not found.
        """
        if not self._loaded:
            raise RuntimeError("index not loaded")
        seq_list = self._seq_map.get((camera_id, stream_type))
        if not seq_list:
            return len(self._entries)
        seqs = [s for s, _ in seq_list]
        pos = bisect.bisect_left(seqs, sequence)
        if pos < len(seqs) and seqs[pos] == sequence:
            return seq_list[pos][1]
        return len(self._entries)

    def filter_camera(self, camera_id: str) -> list[IndexEntry]:
        return [e for e in self._entries if e.camera_id == camera_id]

    def filter_stream(self, stream_type: int) -> list[IndexEntry]:
        return [e for e in self._entries if e.stream_type == stream_type]

    def filter_camera_timestamp(
        self, camera_id: str, start: float, end: float | None = None
    ) -> list[IndexEntry]:
        result = []
        for e in self._entries:
            if e.camera_id != camera_id:
                continue
            if e.timestamp < start:
                continue
            if end is not None and e.timestamp > end:
                continue
            result.append(e)
        return result