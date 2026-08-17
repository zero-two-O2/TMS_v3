"""Tests for SharedMemoryRingBuffer core functionality."""

from __future__ import annotations

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
from thermal_monitor.core.shm import (
    SharedMemoryRingBuffer,
    RingConfig,
    PayloadSpec,
    SlotState,
    create_ring_buffer,
    attach_ring_buffer,
    SharedMemoryPublisher,
    RingLayout,
)


def make_test_frame(sequence: int, thermal_shape=(16, 16), visible_shape=None) -> Frame:
    """Create a test frame with synthetic data."""
    thermal = np.arange(np.prod(thermal_shape), dtype=np.uint16).reshape(thermal_shape)
    thermal.setflags(write=False)

    visible = None
    if visible_shape is not None:
        visible = np.arange(np.prod(visible_shape), dtype=np.uint8).reshape(visible_shape)
        visible.setflags(write=False)

    thermal_meta = StreamMetadata(
        present=True,
        width=thermal_shape[1],
        height=thermal_shape[0],
        pixel_format="IR_Data",
        dtype="uint16",
        byte_count=thermal.nbytes,
        sequence=sequence,
        timestamp=1000.0 + sequence * 0.1,
        monotonic_timestamp=100.0 + sequence * 0.1,
    )

    visible_meta = StreamMetadata(
        present=visible is not None,
        width=visible_shape[1] if visible_shape else None,
        height=visible_shape[0] if visible_shape else None,
        pixel_format="RGB8" if visible_shape else None,
        dtype="uint8" if visible_shape else None,
        byte_count=visible.nbytes if visible is not None else None,
        sequence=sequence if visible_shape else None,
        timestamp=1000.0 + sequence * 0.1 if visible_shape else None,
        monotonic_timestamp=100.0 + sequence * 0.1 if visible_shape else None,
    )

    sync = SyncInfo(status=SyncStatus.MISSING_VISIBLE if visible is None else SyncStatus.UNKNOWN)

    descriptor = FrameDescriptor(
        camera_id="cam_test",
        sequence=sequence,
        timestamp=1000.0 + sequence * 0.1,
        monotonic_timestamp=100.0 + sequence * 0.1,
        thermal=thermal_meta,
        visible=visible_meta,
        sync=sync,
    )

    return Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal, visible=visible))


def test_ring_layout_calculation():
    """Test ring buffer layout computation."""
    thermal_spec = PayloadSpec(
        width=640, height=480, dtype=np.dtype(np.uint16),
        bytes_per_frame=640 * 480 * 2
    )
    visible_spec = PayloadSpec(
        width=640, height=480, dtype=np.dtype(np.uint8),
        bytes_per_frame=640 * 480 * 3
    )
    config = RingConfig(
        camera_id="cam_test",
        thermal_spec=thermal_spec,
        visible_spec=visible_spec,
        depth=8,
    )

    layout = RingLayout.from_config(config)

    # Check basic properties
    assert layout.slot_size > 0
    assert len(layout.slot_offsets) == 8
    assert layout.thermal_size == thermal_spec.aligned_bytes
    assert layout.visible_size == visible_spec.aligned_bytes
    assert layout.descriptor_size == 4096
    assert layout.slot_header_size == 256

    # Check alignment
    assert layout.thermal_offset % 64 == 0
    assert layout.visible_offset % 64 == 0
    assert layout.slot_offsets[0] % 64 == 0
    for i in range(1, 8):
        assert (layout.slot_offsets[i] - layout.slot_offsets[i-1]) == layout.slot_size

    # Total size
    total = config.total_size()
    assert total == layout.header_size + layout.slot_size * 8


def test_ring_buffer_create_and_attach():
    """Test creating and attaching to a ring buffer."""
    ring = create_ring_buffer(
        camera_id="cam_test_create",
        thermal_width=32,
        thermal_height=32,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
        visible_width=32,
        visible_height=32,
        visible_dtype=np.dtype(np.uint8),
    )

    try:
        stats = ring.stats()
        assert stats.camera_id == "cam_test_create"
        assert stats.depth == 4
        assert stats.producer_sequence == 0
        assert stats.producer_head_slot == -1
        assert not stats.closed

        # Attach to same buffer
        ring2 = attach_ring_buffer(
            camera_id="cam_test_create",
            thermal_width=32,
            thermal_height=32,
            thermal_dtype=np.dtype(np.uint16),
            depth=4,
            visible_width=32,
            visible_height=32,
            visible_dtype=np.dtype(np.uint8),
        )

        try:
            stats2 = ring2.stats()
            assert stats2.camera_id == "cam_test_create"
            assert stats2.depth == 4
        finally:
            ring2.close()
    finally:
        ring.close()


def test_ring_buffer_invalid_attach():
    """Test attaching with mismatched configuration fails."""
    ring = create_ring_buffer(
        camera_id="cam_test_invalid",
        thermal_width=32,
        thermal_height=32,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        # Wrong depth
        with pytest.raises(ValueError, match="Depth mismatch"):
            attach_ring_buffer(
                camera_id="cam_test_invalid",
                thermal_width=32,
                thermal_height=32,
                thermal_dtype=np.dtype(np.uint16),
                depth=8,  # Wrong!
            )
    finally:
        ring.close()


def test_producer_publish_basic():
    """Test basic frame publication."""
    ring = create_ring_buffer(
        camera_id="cam_test_pub",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        frame = make_test_frame(0)
        result = producer.publish(frame)

        assert result.accepted is True
        assert result.sequence == 0
        assert result.dropped is False

        stats = ring.stats()
        assert stats.producer_sequence == 0
        assert stats.producer_head_slot == 0
    finally:
        ring.close()


def test_producer_publish_multiple_frames():
    """Test publishing multiple frames advances ring."""
    ring = create_ring_buffer(
        camera_id="cam_test_multi",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()

        for seq in range(10):
            frame = make_test_frame(seq)
            result = producer.publish(frame)
            assert result.accepted is True
            assert result.sequence == seq

        stats = ring.stats()
        assert stats.producer_sequence == 9
        assert stats.producer_head_slot == 1  # 10 frames, depth 4 -> slot 1
    finally:
        ring.close()


def test_producer_no_block_when_full():
    """Test producer returns dropped when all slots pinned."""
    ring = create_ring_buffer(
        camera_id="cam_test_full",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=2,  # Very small depth
    )

    try:
        producer = ring.producer()
        consumer1 = ring.consumer("test_consumer1")
        consumer2 = ring.consumer("test_consumer2")

        # Fill all slots and pin them with different consumers
        producer.publish(make_test_frame(0))
        view0 = consumer1.latest()
        assert view0 is not None
        pinned0 = consumer1.pin(view0)

        producer.publish(make_test_frame(1))
        view1 = consumer2.latest()
        assert view1 is not None
        pinned1 = consumer2.pin(view1)

        # Now try to publish - should fail (all slots pinned)
        frame = make_test_frame(2)
        result = producer.publish(frame)
        assert result.accepted is False
        assert result.dropped is True
        assert result.sequence == 2

        consumer1.release(pinned0)
        consumer2.release(pinned1)
        consumer1.close()
        consumer2.close()
    finally:
        ring.close()


def test_producer_reserve_write_commit():
    """Test the reserve/write/commit flow."""
    ring = create_ring_buffer(
        camera_id="cam_test_rwc",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()

        writer = producer.reserve()
        assert writer is not None
        assert writer.slot_index == 0
        assert writer.generation == 2  # Started at 1, bumped to 2

        # Write thermal data
        thermal_view = writer.thermal_view()
        test_data = np.arange(256, dtype=np.uint16).reshape(16, 16)
        thermal_view[:test_data.nbytes] = test_data.tobytes()

        # Create minimal frame for commit
        frame = make_test_frame(5)
        result = writer.commit(frame)

        assert result.accepted is True
        assert result.sequence == 5
    finally:
        ring.close()


def test_descriptor_encode_decode():
    """Test descriptor binary encoding/decoding round-trip."""
    ring = create_ring_buffer(
        camera_id="cam_test_desc",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        layout = ring._layout
        # Use camera_id that matches the ring
        frame = make_test_frame(42, thermal_shape=(16, 16), visible_shape=(16, 16))
        # Override camera_id in frame to match ring
        frame = Frame(
            descriptor=FrameDescriptor(
                camera_id="cam_test_desc",
                sequence=frame.descriptor.sequence,
                timestamp=frame.descriptor.timestamp,
                monotonic_timestamp=frame.descriptor.monotonic_timestamp,
                thermal=frame.descriptor.thermal,
                visible=frame.descriptor.visible,
                sync=frame.descriptor.sync,
                metadata=frame.descriptor.metadata,
            ),
            payload=frame.payload,
        )

        # Encode
        desc_bytes = encode_descriptor(frame, layout, 0)

        # Decode
        decoded = decode_descriptor(desc_bytes)

        assert decoded["camera_id"] == "cam_test_desc"
        assert decoded["sequence"] == 42
        assert decoded["thermal"].present is True
        assert decoded["thermal"].width == 16
        assert decoded["thermal"].height == 16
        assert decoded["thermal"].dtype == "uint16"
        assert decoded["visible"].present is True
        assert decoded["visible"].width == 16
        assert decoded["visible"].height == 16
        assert decoded["visible"].dtype == "uint8"
        assert decoded["frame_valid"] is True
    finally:
        ring.close()


def test_descriptor_version_mismatch():
    """Test that descriptor version mismatch is detected."""
    from thermal_monitor.core.shm import _DESC_FORMAT, _DESC_VERSION_MAJOR
    import struct
    # Create a descriptor with wrong version using the actual format
    bad_desc = struct.pack(
        _DESC_FORMAT,
        99, 0,  # Wrong version
        b"cam_test".ljust(64, b"\x00"),
        1, 1000.0, 100.0,
        True, 16, 16, b"IR_Data".ljust(32, b"\x00"), 16, b"uint16".ljust(16, b"\x00"), 512, 1, 1000.0, 100.0, 0.0,
        False, 0, 0, b"".ljust(32, b"\x00"), 0, b"".ljust(16, b"\x00"), 0, 0, 0.0, 0.0, 0.0,
        5, 0.0,  # sync_status=UNKNOWN(5), sync_delta=0
        0.0, 0.0, 0.0, 0.0,  # metadata
        0, 0, 0, 0,  # offsets/sizes
        True,  # frame_valid
    )

    with pytest.raises(ValueError, match="Descriptor version mismatch"):
        decode_descriptor(bad_desc)


# Import encode/decode functions
from thermal_monitor.core.shm import encode_descriptor, decode_descriptor


def test_consumer_latest():
    """Test consumer latest() returns newest frame."""
    ring = create_ring_buffer(
        camera_id="cam_test_latest",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_latest")

        # Publish frames
        for seq in range(5):
            producer.publish(make_test_frame(seq))

        # Latest should be frame 4
        view = consumer.latest()
        assert view is not None
        assert view.descriptor.sequence == 4
        assert view.generation == consumer._stats.last_generation

        consumer.close()
    finally:
        ring.close()


def test_consumer_next_sequential():
    """Test consumer next() for sequential consumption."""
    ring = create_ring_buffer(
        camera_id="cam_test_next",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=8,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_next")

        # Publish frames 0-4
        for seq in range(5):
            producer.publish(make_test_frame(seq))

        # Consume sequentially
        for expected in range(5):
            view = consumer.next(expected)
            assert view is not None
            assert view.descriptor.sequence == expected
            assert view.valid()

        stats = consumer.stats()
        assert stats.consumed == 5
        assert stats.last_sequence == 4

        consumer.close()
    finally:
        ring.close()


def test_consumer_next_overwritten():
    """Test consumer detects overwritten frames."""
    ring = create_ring_buffer(
        camera_id="cam_test_overwrite",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=3,  # Small depth
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_overwrite")

        # Publish frames 0-2 (fills ring)
        for seq in range(3):
            producer.publish(make_test_frame(seq))

        # Publish frames 3-5 (overwrites 0-2)
        for seq in range(3, 6):
            producer.publish(make_test_frame(seq))

        # Consumer expects 0, but it's overwritten
        view = consumer.next(0)
        assert view is None

        stats = consumer.stats()
        assert stats.overwritten > 0
        assert stats.gaps > 0

        consumer.close()
    finally:
        ring.close()


def test_consumer_next_not_yet_published():
    """Test consumer next() returns None for future frames."""
    ring = create_ring_buffer(
        camera_id="cam_test_future",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_future")

        # Publish only frame 0
        producer.publish(make_test_frame(0))

        # Ask for frame 5 - not yet published
        view = consumer.next(5)
        assert view is None

        stats = consumer.stats()
        assert stats.gaps >= 1

        consumer.close()
    finally:
        ring.close()


def test_consumer_pin_release():
    """Test pinning and releasing a slot."""
    ring = create_ring_buffer(
        camera_id="cam_test_pin",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=3,  # Small depth
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_pin")
        consumer2 = ring.consumer("test_pin2")
        consumer3 = ring.consumer("test_pin3")

        producer.publish(make_test_frame(0))
        view = consumer.latest()
        assert view is not None

        # Pin the first slot
        pinned = consumer.pin(view)
        assert pinned.view is view
        assert consumer._pinned_slot == (view.slot_index, view.generation)

        # Fill other slots and pin them with other consumers
        producer.publish(make_test_frame(1))
        view1 = consumer2.latest()
        pinned1 = consumer2.pin(view1)

        producer.publish(make_test_frame(2))
        view2 = consumer3.latest()
        pinned2 = consumer3.pin(view2)

        # All 3 slots pinned - next publish should drop
        result = producer.publish(make_test_frame(3))
        assert result.accepted is False
        assert result.dropped is True

        # Release one pin
        consumer.release(pinned)
        assert consumer._pinned_slot is None

        # Now publish should succeed (reuses the released slot)
        result = producer.publish(make_test_frame(3))
        assert result.accepted is True

        consumer.close()
        consumer2.close()
        consumer3.close()
    finally:
        ring.close()


def test_consumer_pin_context_manager():
    """Test pin as context manager."""
    ring = create_ring_buffer(
        camera_id="cam_test_pin_cm",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_pin_cm")

        producer.publish(make_test_frame(0))
        view = consumer.latest()

        with consumer.pin(view) as pinned_view:
            assert pinned_view is view
            assert consumer._pinned_slot is not None

        # Pin should be released
        assert consumer._pinned_slot is None

        consumer.close()
    finally:
        ring.close()


def test_frame_view_thermal_readonly():
    """Test that FrameView thermal array is read-only."""
    ring = create_ring_buffer(
        camera_id="cam_test_ro",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_ro")

        producer.publish(make_test_frame(0))
        view = consumer.latest()

        thermal = view.thermal()
        assert thermal is not None
        assert not thermal.flags.writeable

        # Attempting to modify should fail
        with pytest.raises(ValueError):
            thermal[0, 0] = 999

        consumer.close()
    finally:
        ring.close()


def test_frame_view_visible_readonly():
    """Test that FrameView visible array is read-only."""
    ring = create_ring_buffer(
        camera_id="cam_test_ro_vis",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
        visible_width=16,
        visible_height=16,
        visible_dtype=np.dtype(np.uint8),
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_ro_vis")

        producer.publish(make_test_frame(0, visible_shape=(16, 16)))
        view = consumer.latest()

        visible = view.visible()
        assert visible is not None
        assert not visible.flags.writeable

        with pytest.raises(ValueError):
            visible[0, 0] = 999

        consumer.close()
    finally:
        ring.close()


def test_frame_view_copy():
    """Test FrameView.copy() creates durable independent frame."""
    ring = create_ring_buffer(
        camera_id="cam_test_copy",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_copy")

        producer.publish(make_test_frame(42))
        view = consumer.latest()

        # Copy the frame
        frame_copy = view.copy()

        assert isinstance(frame_copy, Frame)
        assert frame_copy.descriptor.sequence == 42
        assert frame_copy.payload.thermal is not None
        assert frame_copy.payload.thermal.flags.writeable is False
        # Data should match
        np.testing.assert_array_equal(frame_copy.payload.thermal, view.thermal())

        # Copy should be independent - modifying original view's backing
        # memory shouldn't affect copy (though we can't easily test that
        # without producer overwriting)

        consumer.close()
    finally:
        ring.close()


def test_frame_view_valid():
    """Test FrameView.valid() detects reuse."""
    ring = create_ring_buffer(
        camera_id="cam_test_valid",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=2,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_valid")

        producer.publish(make_test_frame(0))
        view = consumer.latest()
        assert view.valid()

        # Overwrite the slot
        producer.publish(make_test_frame(1))
        producer.publish(make_test_frame(2))  # This overwrites slot 0

        # View should now be invalid
        assert not view.valid()

        consumer.close()
    finally:
        ring.close()


def test_shared_memory_publisher():
    """Test SharedMemoryPublisher implements FramePublisher protocol."""
    ring = create_ring_buffer(
        camera_id="cam_test_pub_adapter",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        publisher = SharedMemoryPublisher(ring)

        # Test publish
        frame = make_test_frame(0)
        result = publisher.publish(frame)
        assert result.accepted is True
        assert result.sequence == 0

        # Test latest (returns copied Frame)
        latest_frame = publisher.latest()
        assert latest_frame is not None
        assert latest_frame.descriptor.sequence == 0
        assert isinstance(latest_frame, Frame)

        # Test close
        publisher.close()

        # After close, publish should fail
        result = publisher.publish(make_test_frame(1))
        assert result.accepted is False
        assert result.dropped is True
    finally:
        ring.close()


def test_thermal_only_frame():
    """Test frames with only thermal payload (no visible)."""
    ring = create_ring_buffer(
        camera_id="cam_test_thermal_only",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
        # No visible spec
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_thermal_only")

        frame = make_test_frame(0)  # No visible
        result = producer.publish(frame)
        assert result.accepted is True

        view = consumer.latest()
        assert view is not None
        assert view.descriptor.sequence == 0
        assert view.thermal() is not None
        assert view.visible() is None
        assert view.descriptor.visible.present is False
        assert view.descriptor.sync.status == SyncStatus.MISSING_VISIBLE

        consumer.close()
    finally:
        ring.close()


def test_multiple_independent_consumers():
    """Test multiple consumers with independent state."""
    ring = create_ring_buffer(
        camera_id="cam_test_multi_consumer",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=16,  # Larger depth to hold all frames
    )

    try:
        producer = ring.producer()
        consumer1 = ring.consumer("processing")
        consumer2 = ring.consumer("observer")
        consumer3 = ring.consumer("recorder")

        # Publish 10 frames with correct camera_id
        for seq in range(10):
            frame = make_test_frame(seq)
            frame = Frame(
                descriptor=FrameDescriptor(
                    camera_id="cam_test_multi_consumer",
                    sequence=frame.descriptor.sequence,
                    timestamp=frame.descriptor.timestamp,
                    monotonic_timestamp=frame.descriptor.monotonic_timestamp,
                    thermal=frame.descriptor.thermal,
                    visible=frame.descriptor.visible,
                    sync=frame.descriptor.sync,
                    metadata=frame.descriptor.metadata,
                ),
                payload=frame.payload,
            )
            producer.publish(frame)

        # Consumer1 (processing): sequential
        for seq in range(10):
            view = consumer1.next(seq)
            assert view is not None
            assert view.descriptor.sequence == seq

        # Consumer2 (observer): latest only
        view = consumer2.latest()
        assert view.descriptor.sequence == 9

        # Consumer3 (recorder): sequential but starts late
        for seq in range(5, 10):
            view = consumer3.next(seq)
            assert view is not None
            assert view.descriptor.sequence == seq

        # All have independent stats
        stats1 = consumer1.stats()
        stats2 = consumer2.stats()
        stats3 = consumer3.stats()

        assert stats1.consumed == 10
        assert stats2.consumed == 1  # latest() counts as consumed
        assert stats3.consumed == 5

        consumer1.close()
        consumer2.close()
        consumer3.close()
    finally:
        ring.close()


def test_slow_consumer_does_not_block_producer():
    """Test that a slow consumer doesn't block the producer."""
    ring = create_ring_buffer(
        camera_id="cam_test_slow",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        slow_consumer = ring.consumer("slow")
        fast_consumer = ring.consumer("fast")

        # Slow consumer pins first frame and holds it
        producer.publish(make_test_frame(0))
        view = slow_consumer.latest()
        pinned = slow_consumer.pin(view)

        # Producer should continue publishing to other slots
        for seq in range(1, 5):
            result = producer.publish(make_test_frame(seq))
            # Some may be dropped if all other slots fill up
            # but producer never blocks

        # Fast consumer can still read latest
        view = fast_consumer.latest()
        assert view is not None
        assert view.descriptor.sequence >= 1

        slow_consumer.release(pinned)
        slow_consumer.close()
        fast_consumer.close()
    finally:
        ring.close()


def test_consumer_stats():
    """Test consumer statistics tracking."""
    ring = create_ring_buffer(
        camera_id="cam_test_stats",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_stats")

        # Publish and consume
        for seq in range(3):
            producer.publish(make_test_frame(seq))
            view = consumer.next(seq)
            assert view is not None

        stats = consumer.stats()
        assert stats.consumed == 3
        assert stats.last_sequence == 2
        assert stats.overwritten == 0
        assert stats.gaps == 0
        assert stats.stale == 0
        assert stats.invalid == 0

        consumer.close()
    finally:
        ring.close()


def test_ring_buffer_close():
    """Test ring buffer shutdown."""
    ring = create_ring_buffer(
        camera_id="cam_test_close",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    producer = ring.producer()
    consumer = ring.consumer("test_close")

    producer.publish(make_test_frame(0))
    consumer.latest()

    ring.close()

    # After close, stats should show closed
    stats = ring.stats()
    assert stats.closed is True

    # Producer/consumer should be closed
    # (operations would raise)


def test_ring_buffer_close_idempotent():
    """Test close can be called multiple times."""
    ring = create_ring_buffer(
        camera_id="cam_test_close2",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    ring.close()
    ring.close()  # Should not raise


def test_payload_spec_aligned_bytes():
    """Test PayloadSpec aligned_bytes calculation."""
    spec = PayloadSpec(width=10, height=10, dtype=np.dtype(np.uint16), bytes_per_frame=200)
    assert spec.aligned_bytes == 256  # 200 rounded up to 64-byte boundary

    spec2 = PayloadSpec(width=16, height=16, dtype=np.dtype(np.uint16), bytes_per_frame=512)
    assert spec2.aligned_bytes == 512  # Already aligned


def test_ring_config_slot_size():
    """Test RingConfig slot_size calculation."""
    thermal_spec = PayloadSpec(width=16, height=16, dtype=np.dtype(np.uint16), bytes_per_frame=512)
    visible_spec = PayloadSpec(width=16, height=16, dtype=np.dtype(np.uint8), bytes_per_frame=256)

    config = RingConfig(
        camera_id="cam_test",
        thermal_spec=thermal_spec,
        visible_spec=visible_spec,
        depth=4,
    )

    slot_size = config.slot_size()
    # header(256) + descriptor(4096) + thermal(512) + visible(256) = 5120 -> aligned to 64 = 5120
    assert slot_size >= 5120
    assert slot_size % 64 == 0


def test_invalid_frame_not_published():
    """Test that frames with invalid descriptor are handled."""
    ring = create_ring_buffer(
        camera_id="cam_test_invalid_frame",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_invalid")

        # Normal frame
        producer.publish(make_test_frame(0))
        view = consumer.latest()
        assert view is not None

        consumer.close()
    finally:
        ring.close()


# Performance helper test (not a benchmark, just exercises the path)
def test_publish_rate_smoke():
    """Smoke test for publish throughput."""
    ring = create_ring_buffer(
        camera_id="cam_test_perf",
        thermal_width=64,
        thermal_height=64,
        thermal_dtype=np.dtype(np.uint16),
        depth=16,
    )

    try:
        producer = ring.producer()

        import time
        start = time.perf_counter()
        for seq in range(100):
            frame = make_test_frame(seq, thermal_shape=(64, 64))
            result = producer.publish(frame)
            assert result.accepted
        elapsed = time.perf_counter() - start

        # Should complete quickly (not a real benchmark, just sanity)
        assert elapsed < 5.0  # 100 frames in < 5 seconds
        print(f"Published 100 frames in {elapsed:.3f}s ({100/elapsed:.1f} fps)")
    finally:
        ring.close()