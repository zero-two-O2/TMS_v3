"""Stage 5C round-trip tests: Frame -> RecordingWriter -> disk -> RecordingReader -> Frame.

Covers single IR/VL frames, alternating streams, multiple cameras, chunk
rollover, CRC corruption, truncation, manifest status, index seek, reopen,
determinism, and the security constraint (no pickle/eval/exec).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest

from thermal_monitor.core.frame import (
    Frame,
    FrameDescriptor,
    FramePayload,
    StreamMetadata,
    SyncInfo,
    SyncStatus,
)
from thermal_monitor.offline import (
    RecordingCorruptError,
    RecordingReadError,
    RecordingReader,
    RecordingStatus,
)
from thermal_monitor.storage.recording import (
    ChunkReader,
    RecordingWriteMetadata,
    RecordingWriter,
)
from thermal_monitor.storage.recording.format import (
    MANIFEST_FILENAME,
    RECORD_HEADER_SIZE,
    STATUS_COMPLETE,
    STATUS_WRITING,
    parse_record_header,
    record_total_size,
)
from thermal_monitor.storage.recording.index import IndexReader


# --------------------------------------------------------------------------
# Frame builders
# --------------------------------------------------------------------------


def make_frame(
    camera_id: str,
    sequence: int,
    timestamp: float,
    *,
    kind: str = "ir",
    h: int = 8,
    w: int = 8,
    seed: int = 0,
    monotonic: float | None = None,
    metadata: dict | None = None,
) -> Frame:
    mono = monotonic if monotonic is not None else 1000.0 + sequence
    frame_meta = metadata if metadata is not None else {"acq": f"{camera_id}:{sequence}"}
    if kind == "ir":
        arr = (np.arange(h * w, dtype=np.uint16).reshape(h, w) + seed).astype(np.uint16)
        arr.setflags(write=False)
        stream = StreamMetadata(
            present=True,
            width=w,
            height=h,
            pixel_format="IR_Data",
            bits_per_channel=16,
            dtype="uint16",
            byte_count=arr.nbytes,
            sequence=sequence,
            timestamp=timestamp,
            monotonic_timestamp=mono,
        )
        other = StreamMetadata(present=False)
        sync = SyncInfo(status=SyncStatus.MISSING_VISIBLE)
        return Frame(
            descriptor=FrameDescriptor(
                camera_id=camera_id,
                sequence=sequence,
                timestamp=timestamp,
                monotonic_timestamp=mono,
                thermal=stream,
                visible=other,
                sync=sync,
                metadata=frame_meta,
            ),
            payload=FramePayload(thermal=arr),
        )
    arr = (np.arange(h * w * 2, dtype=np.uint8).reshape(h, w, 2) + (seed % 256)).astype(np.uint8)
    arr.setflags(write=False)
    stream = StreamMetadata(
        present=True,
        width=w,
        height=h,
        pixel_format="YUV422_8",
        bits_per_channel=8,
        dtype="uint8",
        byte_count=arr.nbytes,
        sequence=sequence,
        timestamp=timestamp,
        monotonic_timestamp=mono,
    )
    other = StreamMetadata(present=False)
    sync = SyncInfo(status=SyncStatus.MISSING_THERMAL)
    return Frame(
        descriptor=FrameDescriptor(
            camera_id=camera_id,
            sequence=sequence,
            timestamp=timestamp,
            monotonic_timestamp=mono,
            thermal=other,
            visible=stream,
            sync=sync,
            metadata=frame_meta,
        ),
        payload=FramePayload(visible=arr),
    )


def default_metadata(
    recording_id: str,
    cameras: list[str],
    *,
    streams: dict | None = None,
    created_at: str = "2026-08-18T09:30:00.000Z",
    snapshot_timestamp: str = "2026-08-18T09:30:00.000Z",
    start_time: float | None = None,
    **kw,
) -> RecordingWriteMetadata:
    if streams is None:
        streams = {cam: ["IR", "VL"] for cam in cameras}
    return RecordingWriteMetadata(
        recording_id=recording_id,
        cameras=cameras,
        streams=streams,
        created_at=created_at,
        snapshot_timestamp=snapshot_timestamp,
        start_time=start_time,
        camera_snapshots=[
            {"camera_id": cam, "identity": {"camera_id": cam, "serial_number": f"SN-{cam}"}}
            for cam in cameras
        ],
        roi_snapshots=[
            {"roi_id": "roi_1", "camera_id": "cam_001", "shape": "rectangle1", "enabled": True}
        ],
        ptz_snapshots=[{"camera_id": "cam_001", "timestamp": 1755509400.0, "pan": 0.0}],
        calibration_snapshots=[
            {"camera_id": "cam_001", "calibration_id": "cal_v3", "embedded": False}
        ],
        alarm_snapshots=[
            {"rule_id": "rule_1", "roi_id": "roi_1", "severity": "critical", "threshold": 80.0}
        ],
        **kw,
    )


def write_recording(
    tmp_path: Path,
    recording_id: str,
    cameras: list[str],
    frames: list[Frame],
    *,
    chunk_target_bytes: int = 64 * 1024,
    metadata: RecordingWriteMetadata | None = None,
    finalized_at: str | None = None,
) -> Path:
    meta = metadata or default_metadata(recording_id, cameras)
    writer = RecordingWriter(tmp_path, meta, chunk_target_bytes=chunk_target_bytes)
    writer.open()
    for frame in frames:
        writer.write_frame(frame)
    writer.finalize(finalized_at=finalized_at or "2026-08-18T09:31:00.000Z")
    return writer.recording_dir


# --------------------------------------------------------------------------
# A/B/C/D: single frames, alternating streams, multiple cameras
# --------------------------------------------------------------------------


class TestFrameRoundTrips:
    def test_single_ir_frame(self, tmp_path):
        frame = make_frame("cam_001", 5, 123.5, kind="ir", seed=11)
        rec = write_recording(tmp_path, "rec_ir", ["cam_001"], [frame])
        reader = RecordingReader(rec)
        assert reader.status == RecordingStatus.COMPLETE
        assert reader.frame_count == 1
        out = reader.read_frame(reader.entries[0])
        assert out.descriptor.camera_id == "cam_001"
        assert out.descriptor.sequence == 5
        assert out.descriptor.timestamp == 123.5
        assert out.descriptor.monotonic_timestamp == 1005.0
        assert out.payload.thermal is not None
        assert out.payload.visible is None
        np.testing.assert_array_equal(out.payload.thermal, frame.payload.thermal)
        assert out.descriptor.thermal.width == 8
        assert out.descriptor.thermal.height == 8
        assert out.descriptor.thermal.dtype == "uint16"
        assert out.descriptor.thermal.pixel_format == "IR_Data"
        assert out.descriptor.thermal.bits_per_channel == 16
        assert out.descriptor.thermal.byte_count == frame.payload.thermal.nbytes
        assert out.descriptor.sync.status == SyncStatus.MISSING_VISIBLE
        assert dict(out.descriptor.metadata) == {"acq": "cam_001:5"}

    def test_single_vl_frame(self, tmp_path):
        frame = make_frame("cam_001", 3, 200.75, kind="vl", seed=7)
        rec = write_recording(tmp_path, "rec_vl", ["cam_001"], [frame])
        reader = RecordingReader(rec)
        assert reader.status == RecordingStatus.COMPLETE
        out = reader.read_frame(reader.entries[0])
        assert out.payload.visible is not None
        assert out.payload.thermal is None
        np.testing.assert_array_equal(out.payload.visible, frame.payload.visible)
        assert out.payload.visible.shape == (8, 8, 2)
        assert out.descriptor.visible.pixel_format == "YUV422_8"
        assert out.descriptor.visible.dtype == "uint8"
        assert out.descriptor.visible.width == 8
        assert out.descriptor.visible.height == 8
        assert out.descriptor.sync.status == SyncStatus.MISSING_THERMAL

    def test_alternating_streams_remain_separate(self, tmp_path):
        frames = [
            make_frame("cam_001", i // 2, 100.0 + i * 0.5, kind="ir" if i % 2 == 0 else "vl")
            for i in range(4)
        ]
        rec = write_recording(tmp_path, "rec_alt", ["cam_001"], frames)
        reader = RecordingReader(rec)
        assert reader.frame_count == 4
        stream_types = [e.stream_type for e in reader.entries]
        assert stream_types == [1, 2, 1, 2]
        for entry in reader.entries:
            out = reader.read_frame(entry)
            if entry.stream_type == 1:
                assert out.payload.thermal is not None
                assert out.payload.visible is None
                assert out.descriptor.sequence == entry.sequence
            else:
                assert out.payload.visible is not None
                assert out.payload.thermal is None
                assert out.descriptor.sequence == entry.sequence

    def test_multiple_cameras(self, tmp_path):
        frames = [
            make_frame("cam_a", i, 100.0 + i, kind="ir")
            for i in range(3)
        ] + [
            make_frame("cam_b", i, 200.0 + i, kind="vl")
            for i in range(3)
        ]
        rec = write_recording(tmp_path, "rec_multi", ["cam_a"], frames)
        reader = RecordingReader(rec)
        assert reader.frame_count == 6
        assert sorted(reader.camera_ids) == ["cam_a", "cam_b"]
        read_back = {out.descriptor.camera_id: 0 for out in reader.iterate()}
        assert set(read_back) == {"cam_a", "cam_b"}
        # cam_b was auto-registered by the writer after creation
        assert reader.manifest["cameras"] == ["cam_a", "cam_b"]


# --------------------------------------------------------------------------
# E: chunk rollover
# --------------------------------------------------------------------------


class TestChunkRollover:
    def test_records_never_split_across_chunks(self, tmp_path):
        frames = [
            make_frame("cam_001", i, 100.0 + i, kind="ir", seed=i)
            for i in range(12)
        ]
        rec = write_recording(tmp_path, "rec_roll", ["cam_001"], frames, chunk_target_bytes=2048)
        reader = RecordingReader(rec)
        assert reader.status == RecordingStatus.COMPLETE
        assert reader.frame_count == 12
        assert reader.chunk_count > 1

        # Every record is entirely inside its chunk's valid data region.
        for entry in reader.entries:
            chunk = ChunkReader(
                rec / "chunks" / f"chunk_{entry.chunk_seq:03d}.tmsr",
                expected_chunk_seq=entry.chunk_seq,
            )
            header = parse_record_header(chunk.read_record(entry.offset).header_bytes)
            total = record_total_size(header["metadata_len"], header["payload_length"])
            assert entry.offset + total <= chunk.valid_data_end
            chunk.close()

        sequences = [out.descriptor.sequence for out in reader.iterate()]
        assert sequences == list(range(12))

    def test_chunk_trailer_present_and_valid(self, tmp_path):
        frames = [make_frame("cam_001", i, 100.0 + i) for i in range(4)]
        rec = write_recording(tmp_path, "rec_trailer", ["cam_001"], frames)
        chunk_path = rec / "chunks" / "chunk_000.tmsr"
        chunk = ChunkReader(chunk_path)
        assert chunk.has_valid_trailer
        assert chunk.trailer_record_count == 4
        assert chunk.verify_all() == 4
        chunk.close()


# --------------------------------------------------------------------------
# F: CRC corruption
# --------------------------------------------------------------------------


class TestCrcCorruption:
    def _corrupt_payload_byte(self, rec: Path, index_pos: int) -> None:
        reader = RecordingReader(rec)
        entry = reader.entries[index_pos]
        chunk_path = rec / "chunks" / f"chunk_{entry.chunk_seq:03d}.tmsr"
        data = bytearray(chunk_path.read_bytes())
        header = parse_record_header(data[entry.offset : entry.offset + RECORD_HEADER_SIZE])
        payload_start = entry.offset + RECORD_HEADER_SIZE + header["metadata_len"]
        data[payload_start] ^= 0xFF
        chunk_path.write_bytes(bytes(data))
        reader.close()

    def test_payload_byte_flip_detected(self, tmp_path):
        frames = [make_frame("cam_001", i, 100.0 + i, seed=i) for i in range(3)]
        rec = write_recording(tmp_path, "rec_crc", ["cam_001"], frames)
        self._corrupt_payload_byte(rec, index_pos=1)

        reader = RecordingReader(rec)
        # The chunk trailer CRC covers the payload, so the flip is detected
        # at open(); CRC is additionally verified per-record at read time.
        assert reader.status == RecordingStatus.CORRUPTED
        good, corrupt = reader.entries[0], reader.entries[1]
        assert reader.read_frame(good).descriptor.sequence == 0
        with pytest.raises(RecordingCorruptError):
            reader.read_frame(corrupt)
        # Full scan flags corruption.
        result = reader.verify()
        assert result.status == RecordingStatus.CORRUPTED
        assert result.records_verified == 3
        assert any("CRC" in failure for failure in result.failures)

    def test_corrupt_index_checksum_flagged_on_read(self, tmp_path):
        frames = [make_frame("cam_001", i, 100.0 + i, seed=i) for i in range(3)]
        rec = write_recording(tmp_path, "rec_idxcrc", ["cam_001"], frames)
        reader = RecordingReader(rec)
        entry = reader.entries[1]
        reader.close()
        # Rewrite the same index entry with a wrong checksum.
        chunk_path = rec / "chunks" / f"chunk_{entry.chunk_seq:03d}.tmsr"
        data = bytearray(chunk_path.read_bytes())
        header = parse_record_header(data[entry.offset : entry.offset + RECORD_HEADER_SIZE])
        payload_start = entry.offset + RECORD_HEADER_SIZE + header["metadata_len"]
        crc_pos = payload_start + entry.payload_length
        data[crc_pos] ^= 0xFF
        chunk_path.write_bytes(bytes(data))
        # Open a fresh reader so it reads the corrupted bytes (a reader that
        # held the file open before corruption may see stale buffered data).
        with pytest.raises(RecordingCorruptError):
            RecordingReader(rec).read_frame(entry)


# --------------------------------------------------------------------------
# G: truncation
# --------------------------------------------------------------------------


class TestTruncation:
    def test_finalized_then_truncated_is_corrupted(self, tmp_path):
        frames = [make_frame("cam_001", i, 100.0 + i, seed=i) for i in range(4)]
        rec = write_recording(tmp_path, "rec_trunc", ["cam_001"], frames)
        reader = RecordingReader(rec)
        entry = reader.entries[2]
        chunk_path = rec / "chunks" / "chunk_000.tmsr"
        data = chunk_path.read_bytes()
        header = parse_record_header(data[entry.offset : entry.offset + RECORD_HEADER_SIZE])
        cut_at = entry.offset + RECORD_HEADER_SIZE + header["metadata_len"] + 3
        chunk_path.write_bytes(data[:cut_at])
        reader.close()

        reader = RecordingReader(rec)
        assert reader.status == RecordingStatus.CORRUPTED
        # Valid records before the cut are still readable.
        assert reader.read_frame(reader.entries[0]).descriptor.sequence == 0
        assert reader.read_frame(reader.entries[1]).descriptor.sequence == 1
        # The truncated record and anything after it must fail, never return bad data.
        with pytest.raises(RecordingReadError):
            reader.read_frame(reader.entries[2])
        with pytest.raises(RecordingReadError):
            reader.read_frame(reader.entries[3])
        # Strict iteration surfaces the failure at the boundary.
        collected = []
        with pytest.raises(RecordingReadError):
            for out in reader.iterate():
                collected.append(out.descriptor.sequence)
        assert collected == [0, 1]
        reader.close()

    def test_interrupted_writer_is_incomplete_and_readable(self, tmp_path):
        meta = default_metadata("rec_crash", ["cam_001"])
        writer = RecordingWriter(tmp_path, meta, chunk_target_bytes=64 * 1024)
        writer.open()
        for i in range(3):
            writer.write_frame(make_frame("cam_001", i, 100.0 + i, seed=i))
        writer.abort()  # no trailer, manifest stays WRITING

        reader = RecordingReader(writer.recording_dir)
        assert reader.status == RecordingStatus.INCOMPLETE
        assert reader.frame_count == 3
        sequences = [out.descriptor.sequence for out in reader.iterate()]
        assert sequences == [0, 1, 2]

    def test_writing_manifest_is_incomplete(self, tmp_path):
        meta = default_metadata("rec_writing", ["cam_001"])
        writer = RecordingWriter(tmp_path, meta, chunk_target_bytes=64 * 1024)
        writer.open()
        writer.write_frame(make_frame("cam_001", 0, 100.0))
        writer.abort()
        reader = RecordingReader(writer.recording_dir)
        assert reader.status == RecordingStatus.INCOMPLETE


# --------------------------------------------------------------------------
# H: manifest status
# --------------------------------------------------------------------------


class TestManifestStatus:
    def test_new_recording_is_writing(self, tmp_path):
        meta = default_metadata("rec_manifest", ["cam_001"])
        writer = RecordingWriter(tmp_path, meta, chunk_target_bytes=64 * 1024)
        writer.open()
        manifest = json.loads((writer.recording_dir / MANIFEST_FILENAME).read_text("utf-8"))
        assert manifest["status"] == STATUS_WRITING
        assert manifest["finalized_at"] is None
        writer.abort()

    def test_finalized_recording_is_complete(self, tmp_path):
        rec = write_recording(
            tmp_path,
            "rec_manifest2",
            ["cam_001"],
            [make_frame("cam_001", 0, 100.0)],
            finalized_at="2026-08-18T09:31:00.000Z",
        )
        manifest = json.loads((rec / MANIFEST_FILENAME).read_text("utf-8"))
        assert manifest["status"] == STATUS_COMPLETE
        assert manifest["finalized_at"] == "2026-08-18T09:31:00.000Z"
        assert manifest["frame_count"] == 1
        assert manifest["index_count"] == 1
        assert manifest["chunk_count"] == 1
        reader = RecordingReader(rec)
        assert reader.status == RecordingStatus.COMPLETE


# --------------------------------------------------------------------------
# I: index seek
# --------------------------------------------------------------------------


class TestIndexSeek:
    def _build(self, tmp_path) -> tuple[Path, list[Frame]]:
        frames = []
        for i in range(6):
            frames.append(make_frame("cam_001", i, 100.0 + i, kind="ir", seed=i))
            frames.append(make_frame("cam_001", i, 100.0 + i + 0.3, kind="vl", seed=i))
        rec = write_recording(tmp_path, "rec_seek", ["cam_001"], frames)
        return rec, frames

    def test_timestamp_seek(self, tmp_path):
        rec, _ = self._build(tmp_path)
        reader = RecordingReader(rec)
        # entries sorted chronologically: IR0(100.0), VL0(100.3), IR1(101.0), ...
        assert reader.seek_by_timestamp(100.0) == 0
        assert reader.seek_by_timestamp(100.2) == 1
        assert reader.seek_by_timestamp(100.5) == 2
        assert reader.seek_by_timestamp(105.0) == 10
        entry = reader.entry_at(reader.seek_by_timestamp(101.5))
        assert entry.timestamp == 102.0
        reader.close()

    def test_timestamp_seek_with_stream_filter(self, tmp_path):
        rec, _ = self._build(tmp_path)
        reader = RecordingReader(rec)
        # First VL record at/after 100.0 is VL0 at position 1.
        pos = reader.seek_by_timestamp(100.0, stream_type=2)
        assert pos == 1
        assert reader.entry_at(pos).stream_type == 2
        reader.close()

    def test_sequence_seek(self, tmp_path):
        rec, _ = self._build(tmp_path)
        reader = RecordingReader(rec)
        pos = reader.seek_by_sequence("cam_001", 1, 2)  # IR sequence 2
        assert pos == 4
        assert reader.entry_at(pos).stream_type == 1
        assert reader.entry_at(pos).sequence == 2
        pos = reader.seek_by_sequence("cam_001", 2, 3)  # VL sequence 3
        assert pos == 7
        assert reader.entry_at(pos).stream_type == 2
        assert reader.entry_at(pos).sequence == 3
        # Missing sequence -> end sentinel.
        assert reader.seek_by_sequence("cam_001", 1, 99) == len(reader.entries)
        reader.close()

    def test_index_reader_filters(self, tmp_path):
        rec, _ = self._build(tmp_path)
        index = IndexReader(rec / "index.bin")
        index.load(["cam_001"])
        ir = index.filter_stream(1)
        vl = index.filter_stream(2)
        assert len(ir) == 6
        assert len(vl) == 6
        assert len(index.filter_camera("cam_001")) == 12
        # Entries with timestamp >= 102.0: IR2/VL2..IR5/VL5 = 8.
        assert len(index.filter_camera_timestamp("cam_001", 102.0)) == 8
        # Entries with timestamp in [102.0, 103.0]: IR2, VL2, IR3 = 3.
        assert len(index.filter_camera_timestamp("cam_001", 102.0, 103.0)) == 3
        assert len(ir) == len([e for e in index if e.stream_type == 1])


# --------------------------------------------------------------------------
# J: reopen completed recording
# --------------------------------------------------------------------------


class TestReopen:
    def test_reopen_completed_recording(self, tmp_path):
        frames = [make_frame("cam_001", i, 100.0 + i, kind="ir", seed=i) for i in range(5)]
        rec = write_recording(tmp_path, "rec_reopen", ["cam_001"], frames)

        for _ in range(2):
            reader = RecordingReader(rec)
            assert reader.status == RecordingStatus.COMPLETE
            assert reader.frame_count == 5
            sequences = [out.descriptor.sequence for out in reader.iterate()]
            assert sequences == [0, 1, 2, 3, 4]
            reader.close()

    def test_missing_manifest_is_corrupted(self, tmp_path):
        rec = write_recording(
            tmp_path,
            "rec_nomanifest",
            ["cam_001"],
            [make_frame("cam_001", 0, 100.0)],
        )
        (rec / MANIFEST_FILENAME).unlink()
        reader = RecordingReader(rec)
        assert reader.status == RecordingStatus.CORRUPTED


# --------------------------------------------------------------------------
# K: determinism
# --------------------------------------------------------------------------


class TestDeterminism:
    def test_identical_input_produces_identical_bytes(self, tmp_path):
        frames = []
        for i in range(4):
            frames.append(make_frame("cam_001", i, 100.0 + i, kind="ir", seed=i))
            frames.append(make_frame("cam_001", i, 100.0 + i + 0.3, kind="vl", seed=i))

        out_a = tmp_path / "a"
        out_b = tmp_path / "b"
        meta_a = default_metadata(
            "rec_det", ["cam_001"], created_at="2026-08-18T09:30:00.000Z",
            snapshot_timestamp="2026-08-18T09:30:00.000Z", start_time=100.0,
        )
        meta_b = default_metadata(
            "rec_det", ["cam_001"], created_at="2026-08-18T09:30:00.000Z",
            snapshot_timestamp="2026-08-18T09:30:00.000Z", start_time=100.0,
        )
        writer_a = RecordingWriter(out_a, meta_a, chunk_target_bytes=64 * 1024)
        writer_b = RecordingWriter(out_b, meta_b, chunk_target_bytes=64 * 1024)
        writer_a.open()
        writer_b.open()
        for frame in frames:
            writer_a.write_frame(frame)
            writer_b.write_frame(frame)
        writer_a.finalize(finalized_at="2026-08-18T09:31:00.000Z")
        writer_b.finalize(finalized_at="2026-08-18T09:31:00.000Z")

        files_a = {p.relative_to(writer_a.recording_dir): p for p in writer_a.recording_dir.rglob("*") if p.is_file()}
        files_b = {p.relative_to(writer_b.recording_dir): p for p in writer_b.recording_dir.rglob("*") if p.is_file()}
        assert set(files_a) == set(files_b)
        for rel in files_a:
            assert files_a[rel].read_bytes() == files_b[rel].read_bytes(), (
                f"{rel} differs between identical recordings"
            )


# --------------------------------------------------------------------------
# Security / safety
# --------------------------------------------------------------------------


class TestSecurity:
    @pytest.mark.parametrize(
        "path",
        [
            "src/thermal_monitor/storage/recording/format.py",
            "src/thermal_monitor/storage/recording/chunks.py",
            "src/thermal_monitor/storage/recording/index.py",
            "src/thermal_monitor/storage/recording/writer.py",
            "src/thermal_monitor/offline/reader.py",
        ],
    )
    def test_no_pickle_eval_exec(self, path):
        import os

        repo_root = Path(os.path.dirname(__file__)).parent
        source = (repo_root / path).read_text("utf-8")
        # Detect actual usage, not the word appearing in "no pickle" docs.
        forbidden = (
            r"\bimport\s+pickle\b",
            r"\bfrom\s+pickle\b",
            r"\bpickle\.",
            r"\beval\s*\(",
            r"\bexec\s*\(",
        )
        for pattern in forbidden:
            assert re.search(pattern, source) is None, f"{pattern} found in {path}"

    def test_manifest_and_snapshots_are_json(self, tmp_path):
        rec = write_recording(
            tmp_path,
            "rec_json",
            ["cam_001"],
            [make_frame("cam_001", 0, 100.0)],
        )
        for rel in ("manifest.json", "config/cameras.json", "config/rois.json", "config/ptz.json",
                    "config/calibration.json", "config/alarm.json", "events/alarms.json"):
            text = (rec / rel).read_text("utf-8")
            json.loads(text)  # must parse as JSON, not pickle
        assert "pickle" not in (rec / "config" / "cameras.json").read_text("utf-8")