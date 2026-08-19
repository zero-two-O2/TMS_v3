"""
storage.recording.chunks -- Append-only chunk files of frame records.

Each chunk file is ``chunk_NNN.tmsr``: a fixed 32-byte header, a sequence of
self-describing records (header + metadata + payload + CRC), and an optional
16-byte trailer.  A chunk with a missing/invalid trailer is truncated; its
complete records are still readable, but the recording must be reported as
incomplete.

A writer never reports a record as committed before its header, metadata,
payload and CRC have all been written.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from thermal_monitor.storage.recording.format import (
    CHUNK_END_MAGIC,
    CHUNK_HEADER_SIZE,
    CHUNK_HEADER_STRUCT,
    CHUNK_MAGIC,
    CHUNK_TRAILER_SIZE,
    CHUNK_TRAILER_STRUCT,
    CRC_SIZE,
    DEFAULT_CHUNK_TARGET_BYTES,
    FORMAT_MAJOR,
    RECORD_HEADER_SIZE,
    RECORD_MAGIC,
    FormatError,
    compute_record_crc,
    parse_record_header,
    record_total_size,
)


class ChunkReadError(FormatError):
    """Structural or checksum failure while reading a chunk."""

    def __init__(self, message: str, *, reason: str = "structural", chunk_seq: int | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.chunk_seq = chunk_seq


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """One raw record read from a chunk (bytes not yet validated for CRC)."""

    chunk_seq: int
    offset: int
    header_bytes: bytes
    metadata_bytes: bytes
    payload_bytes: bytes
    stored_crc: int

    @property
    def total_size(self) -> int:
        return len(self.header_bytes) + len(self.metadata_bytes) + len(self.payload_bytes) + CRC_SIZE

    @property
    def header(self) -> dict:
        return parse_record_header(self.header_bytes)

    def verify_crc(self) -> bool:
        return (
            compute_record_crc(self.header_bytes, self.metadata_bytes, self.payload_bytes)
            == self.stored_crc
        )


class ChunkWriter:
    """Append-only writer for one chunk file."""

    def __init__(
        self,
        path: str | Path,
        chunk_seq: int,
        target_bytes: int = DEFAULT_CHUNK_TARGET_BYTES,
    ) -> None:
        if chunk_seq < 0:
            raise ValueError("chunk_seq must be >= 0")
        if target_bytes < CHUNK_TRAILER_SIZE + RECORD_HEADER_SIZE + CRC_SIZE:
            raise ValueError("target_bytes is too small to hold a single record")
        self._path = Path(path)
        self._chunk_seq = chunk_seq
        self._target_bytes = target_bytes
        self._file: BinaryIO | None = None
        self._record_count = 0
        self._chunk_crc = 0
        self._closed = False
        self._open()

    # -- properties -------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def chunk_seq(self) -> int:
        return self._chunk_seq

    @property
    def record_count(self) -> int:
        return self._record_count

    @property
    def position(self) -> int:
        """Current write position (byte offset of the next record)."""
        if self._file is None:
            raise RuntimeError("chunk not open")
        return self._file.tell()

    @property
    def closed(self) -> bool:
        return self._closed

    # -- writing ----------------------------------------------------------

    def _open(self) -> None:
        self._file = open(self._path, "wb")
        header = CHUNK_HEADER_STRUCT.pack(
            CHUNK_MAGIC,
            FORMAT_MAJOR,
            self._chunk_seq,
            0,  # record_count (informational; the trailer holds the authoritative count)
            b"\x00" * 10,  # reserved
        )
        self._file.write(header)
        self._chunk_crc = zlib.crc32(header)

    def ensure_capacity(self, record_size: int) -> bool:
        """Return True if ``record_size`` bytes fit in this chunk.

        The chunk holds the 32-byte header plus records plus the 16-byte
        trailer; rollover happens before a record would exceed the target.
        """
        if self._closed:
            raise RuntimeError("chunk is closed")
        record_region = self.position - CHUNK_HEADER_SIZE
        return record_region + record_size + CHUNK_TRAILER_SIZE <= self._target_bytes

    def write_record(self, record_bytes: bytes) -> None:
        """Append one complete record (header + metadata + payload + CRC)."""
        if self._closed:
            raise RuntimeError("chunk is closed")
        if self._file is None:
            raise RuntimeError("chunk not open")
        if len(record_bytes) < RECORD_HEADER_SIZE + CRC_SIZE:
            raise ValueError("record_bytes is too small to be a valid record")
        self._file.write(record_bytes)
        self._record_count += 1
        self._chunk_crc = zlib.crc32(record_bytes, self._chunk_crc)

    def finalize(self) -> None:
        """Write the chunk trailer and close the file."""
        if self._closed:
            raise RuntimeError("chunk is already closed")
        if self._file is None:
            raise RuntimeError("chunk not open")
        trailer = CHUNK_TRAILER_STRUCT.pack(CHUNK_END_MAGIC, self._record_count, self._chunk_crc)
        self._file.write(trailer)
        self._file.flush()
        self._file.close()
        self._file = None
        self._closed = True

    def close_without_trailer(self) -> None:
        """Close the file without writing a trailer (crash/interrupt path)."""
        if self._closed:
            return
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None
        self._closed = True


class ChunkReader:
    """Reader for one chunk file with boundary and trailer validation."""

    def __init__(self, path: str | Path, expected_chunk_seq: int | None = None) -> None:
        self._path = Path(path)
        self._file: BinaryIO | None = None
        self._file_size = self._path.stat().st_size
        if self._file_size < CHUNK_HEADER_SIZE:
            raise ChunkReadError(
                f"Chunk {self._path.name} is too small ({self._file_size} bytes)",
                reason="truncated",
            )
        self._file = open(self._path, "rb")
        self._header = self._read_chunk_header()
        if expected_chunk_seq is not None and self._header["chunk_seq"] != expected_chunk_seq:
            raise ChunkReadError(
                f"Chunk {self._path.name} has seq {self._header['chunk_seq']}, expected {expected_chunk_seq}",
                reason="bad_header",
                chunk_seq=self._header["chunk_seq"],
            )
        self._trailer = self._read_chunk_trailer()

    # -- construction helpers ---------------------------------------------

    def _read(self, offset: int, length: int) -> bytes:
        if self._file is None:
            raise ChunkReadError("chunk not open", reason="structural")
        self._file.seek(offset)
        data = self._file.read(length)
        if len(data) != length:
            raise ChunkReadError(
                f"Unexpected EOF in {self._path.name} at offset {offset}", reason="truncated"
            )
        return data

    def _read_chunk_header(self) -> dict:
        raw = self._read(0, CHUNK_HEADER_SIZE)
        magic, version, chunk_seq, record_count, reserved = CHUNK_HEADER_STRUCT.unpack(raw)
        if magic != CHUNK_MAGIC:
            raise ChunkReadError(
                f"Bad chunk magic {magic!r} in {self._path.name}", reason="magic"
            )
        if version != FORMAT_MAJOR:
            raise ChunkReadError(
                f"Unsupported chunk version {version} in {self._path.name}",
                reason="version",
                chunk_seq=chunk_seq,
            )
        return {
            "magic": magic,
            "version": version,
            "chunk_seq": chunk_seq,
            "record_count": record_count,
            "reserved": reserved,
        }

    def _read_chunk_trailer(self) -> dict | None:
        """Return trailer info if a valid trailer is present, else None."""
        if self._file_size < CHUNK_HEADER_SIZE + CHUNK_TRAILER_SIZE:
            return None
        raw = self._read(self._file_size - CHUNK_TRAILER_SIZE, CHUNK_TRAILER_SIZE)
        end_magic, record_count, chunk_crc = CHUNK_TRAILER_STRUCT.unpack(raw)
        if end_magic != CHUNK_END_MAGIC:
            return None
        data = self._read(0, self._file_size - CHUNK_TRAILER_SIZE)
        actual = zlib.crc32(data) & 0xFFFFFFFF
        if actual != chunk_crc:
            return None
        return {"end_magic": end_magic, "record_count": record_count, "chunk_crc": chunk_crc}

    # -- properties -------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def chunk_seq(self) -> int:
        return self._header["chunk_seq"]

    @property
    def version(self) -> int:
        return self._header["version"]

    @property
    def header_record_count(self) -> int:
        return self._header["record_count"]

    @property
    def file_size(self) -> int:
        return self._file_size

    @property
    def has_valid_trailer(self) -> bool:
        return self._trailer is not None

    @property
    def trailer_record_count(self) -> int | None:
        return self._trailer["record_count"] if self._trailer is not None else None

    @property
    def valid_data_end(self) -> int:
        """Byte offset where record data ends (before the trailer, if present)."""
        if self._trailer is not None:
            return self._file_size - CHUNK_TRAILER_SIZE
        return self._file_size

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> "ChunkReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- reading ----------------------------------------------------------

    def read_record(self, offset: int) -> ChunkRecord:
        """Read one record starting at ``offset``, validating boundaries only."""
        end = self.valid_data_end
        if offset < CHUNK_HEADER_SIZE or offset + RECORD_HEADER_SIZE > end:
            raise ChunkReadError(
                f"Record offset {offset} is outside the valid data region of {self._path.name}",
                reason="truncated",
                chunk_seq=self.chunk_seq,
            )
        header_bytes = self._read(offset, RECORD_HEADER_SIZE)
        try:
            header = parse_record_header(header_bytes)
        except FormatError as exc:
            raise ChunkReadError(
                f"Invalid record header at offset {offset} in {self._path.name}: {exc}",
                reason="bad_header",
                chunk_seq=self.chunk_seq,
            ) from None
        metadata_len = header["metadata_len"]
        payload_length = header["payload_length"]
        total = record_total_size(metadata_len, payload_length)
        if offset + total > end:
            raise ChunkReadError(
                f"Record at offset {offset} in {self._path.name} extends past the valid data end",
                reason="truncated",
                chunk_seq=self.chunk_seq,
            )
        meta_start = offset + RECORD_HEADER_SIZE
        metadata_bytes = self._read(meta_start, metadata_len)
        payload_bytes = self._read(meta_start + metadata_len, payload_length)
        crc_raw = self._read(meta_start + metadata_len + payload_length, CRC_SIZE)
        stored_crc = int.from_bytes(crc_raw, "little")
        return ChunkRecord(
            chunk_seq=self.chunk_seq,
            offset=offset,
            header_bytes=header_bytes,
            metadata_bytes=metadata_bytes,
            payload_bytes=payload_bytes,
            stored_crc=stored_crc,
        )

    def records(self) -> Iterator[ChunkRecord]:
        """Yield every complete record in the chunk, oldest first.

        Raises :class:`ChunkReadError` (reason="truncated") if the chunk ends
        inside a record -- after all preceding complete records were yielded.
        """
        pos = CHUNK_HEADER_SIZE
        end = self.valid_data_end
        while pos < end:
            if end - pos < RECORD_HEADER_SIZE:
                raise ChunkReadError(
                    f"Chunk {self._path.name} ends with {end - pos} trailing bytes "
                    "inside a record header",
                    reason="truncated",
                    chunk_seq=self.chunk_seq,
                )
            record = self.read_record(pos)
            yield record
            pos += record.total_size
        if pos != end:
            raise ChunkReadError(
                f"Chunk {self._path.name} record boundaries do not align with the valid data end",
                reason="truncated",
                chunk_seq=self.chunk_seq,
            )

    def verify_all(self) -> int:
        """Full-CRC pass over the chunk; returns the number of records verified.

        Raises :class:`ChunkReadError` (reason="crc") on the first mismatch.
        """
        count = 0
        for record in self.records():
            if not record.verify_crc():
                raise ChunkReadError(
                    f"CRC mismatch at offset {record.offset} in {self._path.name}",
                    reason="crc",
                    chunk_seq=self.chunk_seq,
                )
            count += 1
        return count