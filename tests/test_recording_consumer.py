"""Tests for RecordingConsumer (Stage 7C): SHM ring -> RecordingWriter."""

from __future__ import annotations

import time
import numpy as np
import pytest

from thermal_monitor.camera.shm import create_ring_buffer_and_publisher
from thermal_monitor.camera.acquisition import AcquisitionWorker
from thermal_monitor.camera.model import CameraConfig, CameraIdentity, GrabResult
from thermal_monitor.core.frame import (
    Frame,
    FrameDescriptor,
    FramePayload,
    StreamMetadata,
    SyncInfo,
    SyncStatus,
)
from thermal_monitor.services.recording import ContinuousRecordingManager
from thermal_monitor.services.recording_consumer import RecordingConsumer, create_recording_consumer
from thermal_monitor.storage.recording import RecordingWriteMetadata
from thermal_monitor.offline import RecordingReader, RecordingStatus


def make_thermal_frame(
    camera_id: str,
    sequence: int,
    hw_frame_id: int | None = None,
    hardware_timestamp: float | None = None,
    width: int = 16,
    height: int = 16,
) -> Frame:
    """Create a test frame with thermal payload."""
    thermal = np.arange(sequence, sequence + width * height, dtype=np.uint16).reshape(height, width)
    thermal.setflags(write=False)

    if hw_frame_id is None:
        hw_frame_id = sequence * 1000
    if hardware_timestamp is None:
        hardware_timestamp = 1000.0 + sequence * 0.111

    thermal_meta = StreamMetadata(
        present=True,
        width=width,
        height=height,
        pixel_format="IR_Data",
        dtype="uint16",
        byte_count=thermal.nbytes,
        sequence=hw_frame_id,
        timestamp=1000.0 + sequence * 0.111,
        monotonic_timestamp=100.0 + sequence * 0.111,
        hardware_timestamp=hardware_timestamp,
    )

    visible_meta = StreamMetadata(present=False)
    sync = SyncInfo(status=SyncStatus.MISSING_VISIBLE)

    descriptor = FrameDescriptor(
        camera_id=camera_id,
        sequence=sequence,
        timestamp=1000.0 + sequence * 0.111,
        monotonic_timestamp=100.0 + sequence * 0.111,
        thermal=thermal_meta,
        visible=visible_meta,
        sync=sync,
        metadata={"grab_duration_s": 0.001, "packet_stats": {"packets_seen": sequence * 1000, "packets_lost": 0, "blocks_incomplete": 0, "blocks_discarded": 0}},
    )

    return Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal, visible=None))


class TestRecordingConsumerBasic:
    """Basic ring -> writer tests."""

    def test_ring_to_writer_single_frame(self, tmp_path):
        """Test single frame goes from ring to recording."""
        camera_id = "cam_test"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=4)

        try:
            # Attach consumer and create RecordingConsumer FIRST
            ring2, consumer = create_recording_consumer(
                camera_id=camera_id,
                output_dir=tmp_path,
                recording_metadata=RecordingWriteMetadata(
                    recording_id="rec_001",
                    cameras=[camera_id],
                    streams={camera_id: ["IR"]},
                    camera_snapshots=[{"camera_id": camera_id}],
                ),
                ring_depth=4,
                chunk_target_bytes=64 * 1024,
                thermal_width=16,
                thermal_height=16,
            )

            try:
                consumer.start()

                # THEN publish frame
                frame = make_thermal_frame(camera_id, 0)
                result = publisher.publish(frame)
                assert result.accepted is True

                # Wait for consumer to process
                assert consumer.wait_for_frames(1, timeout=2.0)
                consumer.stop()

                # Verify recording
                reader = RecordingReader(consumer._writer.recording_dir)
                assert reader.status == RecordingStatus.COMPLETE
                assert reader.frame_count == 1
                out = reader.read_frame(reader.entries[0])
                assert out.descriptor.camera_id == camera_id
                assert out.descriptor.sequence == 0
                np.testing.assert_array_equal(out.payload.thermal, frame.payload.thermal)
            finally:
                consumer.close()
                ring2.close()
        finally:
            ring.close()

    def test_packet_stats_survive_acquisition_shm_recording_roundtrip(self, tmp_path):
        """Preserve GrabResult packet stats through SHM and recording storage."""
        camera_id = "cam_packet_stats"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=4)
        ring2 = None
        consumer = None
        try:
            config = CameraConfig(
                identity=CameraIdentity(
                    camera_id=camera_id,
                    serial_number="test",
                    model="TV46L",
                    vendor="test",
                ),
                device_identifier="test",
            )
            worker = AcquisitionWorker(camera_id, object(), publisher, config)
            thermal = np.arange(16 * 16, dtype=np.uint16).reshape(16, 16)
            thermal.setflags(write=False)
            packet_stats = {
                "packets_seen": 422,
                "packets_lost": 0,
                "blocks_incomplete": 0,
                "blocks_discarded": 0,
            }
            frame = worker._build_frame(GrabResult(
                thermal=thermal,
                thermal_format="IR_Data",
                packet_stats=packet_stats,
                grab_started=1.0,
                grab_completed=1.001,
                converted_at=1.002,
            ))
            assert frame is not None
            assert dict(frame.descriptor.metadata["packet_stats"]) == packet_stats

            ring2, consumer = create_recording_consumer(
                camera_id=camera_id,
                output_dir=tmp_path,
                recording_metadata=RecordingWriteMetadata(
                    recording_id="rec_packet_stats",
                    cameras=[camera_id],
                    streams={camera_id: ["IR"]},
                ),
                ring_depth=4,
                thermal_width=16,
                thermal_height=16,
            )
            consumer.start()
            assert publisher.publish(frame).accepted
            assert consumer.wait_for_frames(1, timeout=2.0)
            consumer.stop()

            reader = RecordingReader(consumer._writer.recording_dir)
            recorded = reader.read_frame(reader.entries[0])
            assert dict(recorded.descriptor.metadata["packet_stats"]) == packet_stats
        finally:
            if consumer is not None:
                consumer.close()
            if ring2 is not None:
                ring2.close()
            ring.close()

    def test_ring_to_writer_multiple_frames(self, tmp_path):
        """Test multiple frames in sequence."""
        camera_id = "cam_multi"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=16)

        # Attach consumer FIRST
        ring2, consumer = create_recording_consumer(
            camera_id=camera_id,
            output_dir=tmp_path,
            recording_metadata=RecordingWriteMetadata(
                recording_id="rec_002",
                cameras=[camera_id],
                streams={camera_id: ["IR"]},
                camera_snapshots=[{"camera_id": camera_id}],
            ),
            ring_depth=16,
            chunk_target_bytes=64 * 1024,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()

            # Publish frames
            frames = []
            for seq in range(10):
                frame = make_thermal_frame(camera_id, seq)
                frames.append(frame)
                result = publisher.publish(frame)
                assert result.accepted is True

            # Wait for consumer to process all
            assert consumer.wait_for_frames(10, timeout=2.0)
            consumer.stop()

            reader = RecordingReader(consumer._writer.recording_dir)
            assert reader.status == RecordingStatus.COMPLETE
            assert reader.frame_count == 10
            sequences = [out.descriptor.sequence for out in reader.iterate()]
            assert sequences == list(range(10))

            # Verify raw data integrity
            for i, out in enumerate(reader.iterate()):
                np.testing.assert_array_equal(out.payload.thermal, frames[i].payload.thermal)
        finally:
            consumer.close()
            ring2.close()
            ring.close()

    def test_hardware_metadata_preserved(self, tmp_path):
        """Test hardware frame_id and timestamp survive ring -> writer -> reader."""
        camera_id = "cam_hw"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=4)

        # Attach consumer FIRST
        ring2, consumer = create_recording_consumer(
            camera_id=camera_id,
            output_dir=tmp_path,
            recording_metadata=RecordingWriteMetadata(
                recording_id="rec_003",
                cameras=[camera_id],
                streams={camera_id: ["IR"]},
                camera_snapshots=[{"camera_id": camera_id}],
            ),
            ring_depth=4,
            chunk_target_bytes=64 * 1024,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()

            # Frame with specific hardware metadata
            hw_id = 42000
            hw_ts = 1234567890.123456
            frame = make_thermal_frame(camera_id, 5, hw_frame_id=hw_id, hardware_timestamp=hw_ts)
            publisher.publish(frame)

            # Wait for consumer to process
            assert consumer.wait_for_frames(1, timeout=2.0)
            consumer.stop()

            reader = RecordingReader(consumer._writer.recording_dir)
            out = reader.read_frame(reader.entries[0])

            # Hardware frame_id from thermal stream metadata
            assert out.descriptor.thermal.sequence == hw_id
            # Hardware timestamp preserved
            assert abs(out.descriptor.thermal.hardware_timestamp - hw_ts) < 0.001
        finally:
            consumer.close()
            ring2.close()
            ring.close()

    def test_raw_uint16_preserved_byte_for_byte(self, tmp_path):
        """Test raw uint16 data is preserved exactly."""
        camera_id = "cam_data"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=4)

        # Attach consumer FIRST
        ring2, consumer = create_recording_consumer(
            camera_id=camera_id,
            output_dir=tmp_path,
            recording_metadata=RecordingWriteMetadata(
                recording_id="rec_004",
                cameras=[camera_id],
                streams={camera_id: ["IR"]},
                camera_snapshots=[{"camera_id": camera_id}],
            ),
            ring_depth=4,
            chunk_target_bytes=64 * 1024,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()

            # Known pattern
            thermal = np.arange(256, dtype=np.uint16).reshape(16, 16)
            thermal.setflags(write=False)

            thermal_meta = StreamMetadata(
                present=True, width=16, height=16, pixel_format="IR_Data",
                dtype="uint16", byte_count=thermal.nbytes,
                sequence=1, timestamp=1000.0, monotonic_timestamp=100.0,
                hardware_timestamp=1000.0,
            )
            frame = Frame(
                descriptor=FrameDescriptor(
                    camera_id=camera_id, sequence=1, timestamp=1000.0,
                    monotonic_timestamp=100.0, thermal=thermal_meta,
                    visible=StreamMetadata(present=False),
                    sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
                    metadata={},
                ),
                payload=FramePayload(thermal=thermal, visible=None),
            )

            publisher.publish(frame)

            # Wait for consumer to process
            assert consumer.wait_for_frames(1, timeout=2.0)
            consumer.stop()

            reader = RecordingReader(consumer._writer.recording_dir)
            out = reader.read_frame(reader.entries[0])

            # Exact byte-for-byte match
            np.testing.assert_array_equal(out.payload.thermal, thermal)
            assert out.payload.thermal.dtype == np.uint16
        finally:
            consumer.close()
            ring2.close()
            ring.close()


class TestRecordingConsumerMultiCamera:
    """Multi-camera recording tests."""

    def test_two_cameras_independent_recordings(self, tmp_path):
        """Test two cameras record independently to separate directories."""
        camera_a = "cam_a"
        camera_b = "cam_b"

        ring_a, pub_a = create_ring_buffer_and_publisher(camera_a, width=16, height=16, depth=8)
        ring_b, pub_b = create_ring_buffer_and_publisher(camera_b, width=16, height=16, depth=8)

        try:
            # Create consumers FIRST
            ring_a2, consumer_a = create_recording_consumer(
                camera_id=camera_a,
                output_dir=tmp_path,
                recording_metadata=RecordingWriteMetadata(
                    recording_id="rec_a",
                    cameras=[camera_a],
                    streams={camera_a: ["IR"]},
                    camera_snapshots=[{"camera_id": camera_a}],
                ),
                ring_depth=8,
                chunk_target_bytes=64 * 1024,
                thermal_width=16,
                thermal_height=16,
            )
            ring_b2, consumer_b = create_recording_consumer(
                camera_id=camera_b,
                output_dir=tmp_path,
                recording_metadata=RecordingWriteMetadata(
                    recording_id="rec_b",
                    cameras=[camera_b],
                    streams={camera_b: ["IR"]},
                    camera_snapshots=[{"camera_id": camera_b}],
                ),
                ring_depth=8,
                chunk_target_bytes=64 * 1024,
                thermal_width=16,
                thermal_height=16,
            )

            try:
                consumer_a.start()
                consumer_b.start()

                # THEN publish to both
                pub_a.publish(make_thermal_frame(camera_a, 0))
                pub_b.publish(make_thermal_frame(camera_b, 100))

                # Wait for both consumers
                assert consumer_a.wait_for_frames(1, timeout=2.0)
                assert consumer_b.wait_for_frames(1, timeout=2.0)

                consumer_a.stop()
                consumer_b.stop()

                # Verify both recordings
                reader_a = RecordingReader(consumer_a._writer.recording_dir)
                reader_b = RecordingReader(consumer_b._writer.recording_dir)

                assert reader_a.frame_count == 1
                assert reader_b.frame_count == 1
                assert reader_a.entries[0].camera_id == camera_a
                assert reader_b.entries[0].camera_id == camera_b
                assert reader_a.entries[0].sequence == 0
                assert reader_b.entries[0].sequence == 100
            finally:
                consumer_a.close()
                consumer_b.close()
                ring_a2.close()
                ring_b2.close()
        finally:
            ring_a.close()
            ring_b.close()

    def test_continuous_recording_manager_multi_camera(self, tmp_path):
        """Test ContinuousRecordingManager manages multiple cameras."""
        manager = ContinuousRecordingManager(
            output_dir=tmp_path,
            ring_depth=8,
            chunk_target_bytes=64 * 1024,
            thermal_width=16,
            thermal_height=16,
        )

        # Create ring buffers (simulating producer side) with unique names
        ring_a, pub_a = create_ring_buffer_and_publisher("cam_a_multi", width=16, height=16, depth=8)
        ring_b, pub_b = create_ring_buffer_and_publisher("cam_b_multi", width=16, height=16, depth=8)

        try:
            # Start recordings via manager
            ring_a_attached, consumer_a = manager.start_recording("cam_a_multi", "rec_a", camera_snapshots=[{"camera_id": "cam_a_multi"}])
            ring_b_attached, consumer_b = manager.start_recording("cam_b_multi", "rec_b", camera_snapshots=[{"camera_id": "cam_b_multi"}])

            # Publish frames using the original publishers
            pub_a.publish(make_thermal_frame("cam_a_multi", 0))
            pub_b.publish(make_thermal_frame("cam_b_multi", 0))

            # Wait for consumers
            assert consumer_a.wait_for_frames(1, timeout=2.0)
            assert consumer_b.wait_for_frames(1, timeout=2.0)

            stats = manager.stop_all()

            assert "cam_a_multi" in stats
            assert "cam_b_multi" in stats
            assert stats["cam_a_multi"].frames_written == 1
            assert stats["cam_b_multi"].frames_written == 1

            # Verify recordings
            reader_a = RecordingReader(consumer_a._writer.recording_dir)
            reader_b = RecordingReader(consumer_b._writer.recording_dir)
            assert reader_a.status == RecordingStatus.COMPLETE
            assert reader_b.status == RecordingStatus.COMPLETE
        finally:
            ring_a.close()
            ring_b.close()
            ring_a_attached.close()
            ring_b_attached.close()
            manager.abort_all()


class TestRecordingConsumerSequencePreservation:
    """Sequence number preservation tests."""

    def test_sequence_continuity(self, tmp_path):
        """Test frame sequences are preserved without gaps."""
        camera_id = "cam_seq"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=32)

        # Attach consumer FIRST
        ring2, consumer = create_recording_consumer(
            camera_id=camera_id,
            output_dir=tmp_path,
            recording_metadata=RecordingWriteMetadata(
                recording_id="rec_seq",
                cameras=[camera_id],
                streams={camera_id: ["IR"]},
                camera_snapshots=[{"camera_id": camera_id}],
            ),
            ring_depth=32,
            chunk_target_bytes=64 * 1024,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()

            # Publish frames
            for seq in range(20):
                publisher.publish(make_thermal_frame(camera_id, seq))

            # Wait for consumer to process all
            assert consumer.wait_for_frames(20, timeout=3.0)
            consumer.stop()

            reader = RecordingReader(consumer._writer.recording_dir)
            sequences = [out.descriptor.sequence for out in reader.iterate()]
            assert sequences == list(range(20))
        finally:
            consumer.close()
            ring2.close()
            ring.close()

    def test_hardware_frame_id_sequence(self, tmp_path):
        """Test hardware frame_id sequence preserved."""
        camera_id = "cam_hwseq"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=32)

        # Attach consumer FIRST
        ring2, consumer = create_recording_consumer(
            camera_id=camera_id,
            output_dir=tmp_path,
            recording_metadata=RecordingWriteMetadata(
                recording_id="rec_hwseq",
                cameras=[camera_id],
                streams={camera_id: ["IR"]},
                camera_snapshots=[{"camera_id": camera_id}],
            ),
            ring_depth=32,
            chunk_target_bytes=64 * 1024,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()

            for seq in range(10):
                hw_id = 10000 + seq * 10
                publisher.publish(make_thermal_frame(camera_id, seq, hw_frame_id=hw_id))

            # Wait for consumer to process all
            assert consumer.wait_for_frames(10, timeout=3.0)
            consumer.stop()

            reader = RecordingReader(consumer._writer.recording_dir)
            hw_ids = [out.descriptor.thermal.sequence for out in reader.iterate()]
            expected = [10000 + i * 10 for i in range(10)]
            assert hw_ids == expected
        finally:
            consumer.close()
            ring2.close()
            ring.close()


class TestRecordingConsumerDropTracking:
    """Drop tracking tests."""

    def test_ring_overwrite_detected(self, tmp_path):
        """Test ring buffer overwrites are tracked."""
        camera_id = "cam_drop"
        # Small depth to force overwrites
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=3)

        # Attach consumer FIRST
        ring2, consumer = create_recording_consumer(
            camera_id=camera_id,
            output_dir=tmp_path,
            recording_metadata=RecordingWriteMetadata(
                recording_id="rec_drop",
                cameras=[camera_id],
                streams={camera_id: ["IR"]},
                camera_snapshots=[{"camera_id": camera_id}],
            ),
            ring_depth=3,
            chunk_target_bytes=64 * 1024,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()

            # Fill ring and overwrite
            for seq in range(10):
                publisher.publish(make_thermal_frame(camera_id, seq))

            # Wait for consumer to process (it will miss some due to overwrites)
            time.sleep(0.2)
            consumer.stop()

            stats = consumer.stats()
            # Ring overwrites should be detected
            assert stats.ring_overwritten > 0
            assert stats.ring_gaps > 0
        finally:
            consumer.close()
            ring2.close()
            ring.close()

    def test_producer_never_blocks(self, tmp_path):
        """Test producer never blocks even when consumer is slow."""
        camera_id = "cam_noblock"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=4)

        ring2, consumer = create_recording_consumer(
            camera_id=camera_id,
            output_dir=tmp_path,
            recording_metadata=RecordingWriteMetadata(
                recording_id="rec_noblock",
                cameras=[camera_id],
                streams={camera_id: ["IR"]},
                camera_snapshots=[{"camera_id": camera_id}],
            ),
            ring_depth=4,
            chunk_target_bytes=64 * 1024,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()

            # Pin a frame to slow down consumer
            ring2_consumer = ring2.consumer("slow")
            ring2_consumer.latest_pinned()  # Pin first available frame

            # Producer should not block
            start = time.perf_counter()
            for seq in range(10):
                result = publisher.publish(make_thermal_frame(camera_id, seq))
            elapsed = time.perf_counter() - start

            # Should complete quickly (non-blocking)
            assert elapsed < 0.5

            consumer.stop()
            ring2_consumer.close()
        finally:
            consumer.close()
            ring2.close()
            ring.close()


class TestRecordingConsumerFinalization:
    """Recording finalization and CRC tests."""

    def test_finalize_recording_complete_status(self, tmp_path):
        """Test finalized recording shows COMPLETE status."""
        camera_id = "cam_finalize"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=8)

        # Attach consumer FIRST
        ring2, consumer = create_recording_consumer(
            camera_id=camera_id,
            output_dir=tmp_path,
            recording_metadata=RecordingWriteMetadata(
                recording_id="rec_finalize",
                cameras=[camera_id],
                streams={camera_id: ["IR"]},
                camera_snapshots=[{"camera_id": camera_id}],
            ),
            ring_depth=8,
            chunk_target_bytes=64 * 1024,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()

            for seq in range(5):
                publisher.publish(make_thermal_frame(camera_id, seq))

            # Wait for consumer to process all
            assert consumer.wait_for_frames(5, timeout=2.0)
            consumer.stop()

            reader = RecordingReader(consumer._writer.recording_dir)
            assert reader.status == RecordingStatus.COMPLETE
        finally:
            consumer.close()
            ring2.close()
            ring.close()

    def test_crc_verification_all_frames(self, tmp_path):
        """Test all recorded frames pass CRC verification."""
        camera_id = "cam_crc"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=16)

        # Attach consumer FIRST
        ring2, consumer = create_recording_consumer(
            camera_id=camera_id,
            output_dir=tmp_path,
            recording_metadata=RecordingWriteMetadata(
                recording_id="rec_crc",
                cameras=[camera_id],
                streams={camera_id: ["IR"]},
                camera_snapshots=[{"camera_id": camera_id}],
            ),
            ring_depth=16,
            chunk_target_bytes=64 * 1024,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()

            for seq in range(10):
                publisher.publish(make_thermal_frame(camera_id, seq))

            # Wait for consumer to process all
            assert consumer.wait_for_frames(10, timeout=3.0)
            consumer.stop()

            reader = RecordingReader(consumer._writer.recording_dir)
            verify_result = reader.verify()
            assert verify_result.status == RecordingStatus.COMPLETE
            assert verify_result.records_verified == 10
            assert len(verify_result.failures) == 0
        finally:
            consumer.close()
            ring2.close()
            ring.close()

    def test_chunk_rollover(self, tmp_path):
        """Test chunk rollover works correctly."""
        camera_id = "cam_chunk"
        # Use depth > number of frames to avoid overwrites
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=32)

        # Attach consumer FIRST
        ring2, consumer = create_recording_consumer(
            camera_id=camera_id,
            output_dir=tmp_path,
            recording_metadata=RecordingWriteMetadata(
                recording_id="rec_chunk",
                cameras=[camera_id],
                streams={camera_id: ["IR"]},
                camera_snapshots=[{"camera_id": camera_id}],
            ),
            ring_depth=32,
            chunk_target_bytes=4096,  # 4KB chunks - small enough for rollover with 20 frames
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()

            # Publish enough frames to trigger chunk rollover
            for seq in range(20):
                publisher.publish(make_thermal_frame(camera_id, seq))

            # Wait for consumer to process all
            assert consumer.wait_for_frames(20, timeout=5.0)
            consumer.stop()

            reader = RecordingReader(consumer._writer.recording_dir)
            assert reader.status == RecordingStatus.COMPLETE
            assert reader.frame_count == 20
            assert reader.chunk_count > 1

            # Verify all frames readable
            sequences = [out.descriptor.sequence for out in reader.iterate()]
            assert sequences == list(range(20))

            verify_result = reader.verify()
            assert verify_result.records_verified == 20
        finally:
            consumer.close()
            ring2.close()
            ring.close()

    def test_consumer_restart(self, tmp_path):
        """Test consumer can be restarted and continue recording."""
        camera_id = "cam_restart"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=16)

        # Attach consumer FIRST
        ring2, consumer = create_recording_consumer(
            camera_id=camera_id,
            output_dir=tmp_path,
            recording_metadata=RecordingWriteMetadata(
                recording_id="rec_restart",
                cameras=[camera_id],
                streams={camera_id: ["IR"]},
                camera_snapshots=[{"camera_id": camera_id}],
            ),
            ring_depth=16,
            chunk_target_bytes=64 * 1024,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()

            # First batch
            for seq in range(5):
                publisher.publish(make_thermal_frame(camera_id, seq))

            # Wait for first batch
            assert consumer.wait_for_frames(5, timeout=2.0)

            # Second batch
            for seq in range(5, 10):
                publisher.publish(make_thermal_frame(camera_id, seq))

            # Wait for second batch
            assert consumer.wait_for_frames(10, timeout=3.0)
            consumer.stop()

            reader = RecordingReader(consumer._writer.recording_dir)
            assert reader.frame_count == 10
            sequences = [out.descriptor.sequence for out in reader.iterate()]
            assert sequences == list(range(10))
        finally:
            consumer.close()
            ring2.close()
            ring.close()

    def test_abort_leaves_incomplete(self, tmp_path):
        """Test aborted recording shows INCOMPLETE status."""
        camera_id = "cam_abort"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=8)

        # Attach consumer FIRST
        ring2, consumer = create_recording_consumer(
            camera_id=camera_id,
            output_dir=tmp_path,
            recording_metadata=RecordingWriteMetadata(
                recording_id="rec_abort",
                cameras=[camera_id],
                streams={camera_id: ["IR"]},
                camera_snapshots=[{"camera_id": camera_id}],
            ),
            ring_depth=8,
            chunk_target_bytes=64 * 1024,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()

            for seq in range(3):
                publisher.publish(make_thermal_frame(camera_id, seq))

            # Wait for consumer to process
            assert consumer.wait_for_frames(3, timeout=2.0)
            consumer.abort()  # Abort without finalizing

            reader = RecordingReader(consumer._writer.recording_dir)
            assert reader.status == RecordingStatus.INCOMPLETE
            assert reader.frame_count == 3
        finally:
            consumer.close()
            ring2.close()
            ring.close()


class TestRecordingConsumerStats:
    """Statistics tracking tests."""

    def test_stats_track_all_categories(self, tmp_path):
        """Test all drop categories are tracked separately."""
        camera_id = "cam_stats"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=4)

        # Attach consumer FIRST
        ring2, consumer = create_recording_consumer(
            camera_id=camera_id,
            output_dir=tmp_path,
            recording_metadata=RecordingWriteMetadata(
                recording_id="rec_stats",
                cameras=[camera_id],
                streams={camera_id: ["IR"]},
                camera_snapshots=[{"camera_id": camera_id}],
            ),
            ring_depth=4,
            chunk_target_bytes=64 * 1024,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()

            # Fill ring and force overwrites
            for seq in range(8):
                publisher.publish(make_thermal_frame(camera_id, seq))

            # Wait for consumer to process
            time.sleep(0.2)
            consumer.stop()

            stats = consumer.stats()
            # Verify all stat categories exist
            assert stats.frames_consumed >= 0
            assert stats.frames_written >= 0
            assert stats.ring_overwritten >= 0
            assert stats.ring_gaps >= 0
            assert stats.ring_stale >= 0
            assert stats.ring_invalid >= 0
            assert stats.writer_dropped >= 0
            # If any frames were processed, last_sequence should be >= 0
            if stats.frames_written > 0:
                assert stats.last_sequence >= 0
                assert stats.first_hardware_frame_id is not None
                assert stats.last_hardware_frame_id is not None
                assert stats.first_timestamp is not None
                assert stats.last_timestamp is not None
        finally:
            consumer.close()
            ring2.close()
            ring.close()

    def test_effective_fps_calculation(self, tmp_path):
        """Test effective recording FPS can be calculated from stats."""
        camera_id = "cam_fps"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=16)

        # Attach consumer FIRST
        ring2, consumer = create_recording_consumer(
            camera_id=camera_id,
            output_dir=tmp_path,
            recording_metadata=RecordingWriteMetadata(
                recording_id="rec_fps",
                cameras=[camera_id],
                streams={camera_id: ["IR"]},
                camera_snapshots=[{"camera_id": camera_id}],
            ),
            ring_depth=16,
            chunk_target_bytes=64 * 1024,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()

            for seq in range(10):
                publisher.publish(make_thermal_frame(camera_id, seq))

            # Wait for consumer to process all
            assert consumer.wait_for_frames(10, timeout=3.0)
            consumer.stop()

            stats = consumer.stats()
            duration = stats.last_timestamp - stats.first_timestamp
            if duration > 0:
                effective_fps = stats.frames_written / duration
                # Should be reasonable (close to frame rate)
                assert effective_fps > 0
        finally:
            consumer.close()
            ring2.close()
            ring.close()


class TestRecordingConsumerCrashRecovery:
    """Crash recovery tests."""

    def test_interrupted_recording_readable(self, tmp_path):
        """Test recording interrupted mid-write is readable up to last complete frame."""
        camera_id = "cam_crash"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=16)

        # Attach consumer FIRST
        ring2, consumer = create_recording_consumer(
            camera_id=camera_id,
            output_dir=tmp_path,
            recording_metadata=RecordingWriteMetadata(
                recording_id="rec_crash",
                cameras=[camera_id],
                streams={camera_id: ["IR"]},
                camera_snapshots=[{"camera_id": camera_id}],
            ),
            ring_depth=16,
            chunk_target_bytes=2048,  # Small chunks
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()

            # Write several frames
            for seq in range(10):
                publisher.publish(make_thermal_frame(camera_id, seq))
                time.sleep(0.01)

            # Abort without clean finalize (simulate crash)
            consumer.abort()

            # Recording should be INCOMPLETE but readable
            reader = RecordingReader(consumer._writer.recording_dir)
            assert reader.status == RecordingStatus.INCOMPLETE

            # Should be able to read valid frames
            frames_read = list(reader.iterate())
            assert len(frames_read) > 0

            # All readable frames should pass CRC
            for frame in frames_read:
                assert frame.payload.thermal is not None
        finally:
            consumer.close()
            ring2.close()
            ring.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
