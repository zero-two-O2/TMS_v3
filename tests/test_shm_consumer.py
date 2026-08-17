"""Tests for SharedMemoryRingBuffer consumer functionality."""

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
    create_ring_buffer,
    Consumer,
    FrameView,
    PinnedView,
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


def test_consumer_latest_empty_ring():
    """Test latest() on empty ring returns None."""
    ring = create_ring_buffer(
        camera_id="cam_test_empty_latest",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        consumer = ring.consumer("test_empty")
        view = consumer.latest()
        assert view is None
        consumer.close()
    finally:
        ring.close()


def test_consumer_latest_after_publish():
    """Test latest() returns most recent frame."""
    ring = create_ring_buffer(
        camera_id="cam_test_latest_after",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_latest_after")

        producer.publish(make_test_frame(0))
        view = consumer.latest()
        assert view is not None
        assert view.descriptor.sequence == 0

        producer.publish(make_test_frame(1))
        view = consumer.latest()
        assert view.descriptor.sequence == 1

        producer.publish(make_test_frame(2))
        view = consumer.latest()
        assert view.descriptor.sequence == 2

        consumer.close()
    finally:
        ring.close()


def test_consumer_latest_skips_intermediate():
    """Test latest() skipping frames doesn't count as loss."""
    ring = create_ring_buffer(
        camera_id="cam_test_latest_skip",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=8,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_skip")

        # Publish 10 frames
        for seq in range(10):
            producer.publish(make_test_frame(seq))

        # Latest should be frame 9
        view = consumer.latest()
        assert view.descriptor.sequence == 9

        # Stats should show 1 consumed (the latest), not 10
        stats = consumer.stats()
        assert stats.consumed == 1
        assert stats.last_sequence == 9
        # Gaps/overwritten should be 0 for latest() consumer
        assert stats.overwritten == 0
        assert stats.gaps == 0

        consumer.close()
    finally:
        ring.close()


def test_consumer_next_sequential_basic():
    """Test next() consumes frames in order."""
    ring = create_ring_buffer(
        camera_id="cam_test_next_basic",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=8,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_next_basic")

        for seq in range(5):
            producer.publish(make_test_frame(seq))

        for expected in range(5):
            view = consumer.next(expected)
            assert view is not None
            assert view.descriptor.sequence == expected
            assert view.valid()

        stats = consumer.stats()
        assert stats.consumed == 5
        assert stats.last_sequence == 4
        assert stats.overwritten == 0
        assert stats.gaps == 0

        consumer.close()
    finally:
        ring.close()


def test_consumer_next_without_argument():
    """Test next() without argument uses expected_sequence."""
    ring = create_ring_buffer(
        camera_id="cam_test_next_no_arg",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=8,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_next_no_arg")

        for seq in range(3):
            producer.publish(make_test_frame(seq))

        # Call next() without argument
        view = consumer.next()
        assert view.descriptor.sequence == 0

        view = consumer.next()
        assert view.descriptor.sequence == 1

        view = consumer.next()
        assert view.descriptor.sequence == 2

        consumer.close()
    finally:
        ring.close()


def test_consumer_next_overwritten_detection():
    """Test next() detects overwritten frames."""
    ring = create_ring_buffer(
        camera_id="cam_test_next_overwrite",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=3,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_next_overwrite")

        # Fill ring
        for seq in range(3):
            producer.publish(make_test_frame(seq))

        # Overwrite all slots
        for seq in range(3, 6):
            producer.publish(make_test_frame(seq))

        # Consumer expects 0, but it's gone
        view = consumer.next(0)
        assert view is None

        stats = consumer.stats()
        # Should detect overwritten frames
        assert stats.overwritten > 0
        # Expected sequence should advance past producer head
        assert stats.last_sequence == -1  # Not updated on overwrite

        consumer.close()
    finally:
        ring.close()


def test_consumer_next_future_frame():
    """Test next() for frame not yet published."""
    ring = create_ring_buffer(
        camera_id="cam_test_next_future",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_next_future")

        producer.publish(make_test_frame(0))

        # Ask for frame 5
        view = consumer.next(5)
        assert view is None

        stats = consumer.stats()
        assert stats.gaps >= 1

        consumer.close()
    finally:
        ring.close()


def test_consumer_next_after_gap_reanchors():
    """Test next() re-anchors after detecting gap."""
    ring = create_ring_buffer(
        camera_id="cam_test_next_reanchor",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_next_reanchor")

        # Publish 0, 1
        producer.publish(make_test_frame(0))
        producer.publish(make_test_frame(1))

        # Consumer reads 0
        view = consumer.next(0)
        assert view.descriptor.sequence == 0

        # Producer publishes 2, 3, 4, 5 (overwrites 0, 1)
        for seq in range(2, 6):
            producer.publish(make_test_frame(seq))

        # Consumer asks for 1 - overwritten
        view = consumer.next(1)
        assert view is None

        # Next call should re-anchor to latest
        view = consumer.next()
        assert view is not None
        assert view.descriptor.sequence == 5  # Latest

        consumer.close()
    finally:
        ring.close()


def test_consumer_pin_prevents_reuse():
    """Test that pinning a slot prevents producer reuse of that specific slot."""
    ring = create_ring_buffer(
        camera_id="cam_test_pin_prevent",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=3,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_pin_prevent")

        producer.publish(make_test_frame(0))
        view = consumer.latest()
        pinned = consumer.pin(view)

        # Fill the other slots
        producer.publish(make_test_frame(1))  # slot 1
        producer.publish(make_test_frame(2))  # slot 2

        # Ring is now full: slot 0 (pinned), slot 1 (published), slot 2 (published)
        # Next publish should reuse slot 1 (not pinned)
        result = producer.publish(make_test_frame(3))
        assert result.accepted is True
        assert result.sequence == 3

        # Slot 0 should still be pinned and valid
        assert pinned.view.valid()

        # Publish more to fill up again
        producer.publish(make_test_frame(4))  # reuses slot 2
        producer.publish(make_test_frame(5))  # reuses slot 1

        # Now all slots are published but slot 0 is still pinned
        # Next publish should drop (all slots either pinned or would overwrite pinned)
        # Actually, producer will reuse slot 1 or 2 since they're not pinned
        result = producer.publish(make_test_frame(6))
        assert result.accepted is True

        consumer.release(pinned)
        consumer.close()
    finally:
        ring.close()


def test_consumer_pin_all_slots_causes_drop():
    """Test that pinning all slots causes publish to drop."""
    ring = create_ring_buffer(
        camera_id="cam_test_pin_all",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=2,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_pin_all")

        # Fill and pin both slots
        producer.publish(make_test_frame(0))
        view0 = consumer.latest()
        pinned0 = consumer.pin(view0)

        producer.publish(make_test_frame(1))
        # Need a second consumer to pin the second slot
        consumer2 = ring.consumer("test_pin_all_2")
        view1 = consumer2.latest()
        pinned1 = consumer2.pin(view1)

        # All slots pinned - publish should drop
        result = producer.publish(make_test_frame(2))
        assert result.accepted is False
        assert result.dropped is True

        consumer.release(pinned0)
        consumer2.release(pinned1)
        consumer2.close()
        consumer.close()
    finally:
        ring.close()


def test_consumer_double_pin_raises():
    """Test that pinning twice without release raises."""
    ring = create_ring_buffer(
        camera_id="cam_test_double_pin",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_double_pin")

        producer.publish(make_test_frame(0))
        view = consumer.latest()
        pinned = consumer.pin(view)

        # Try to pin again
        with pytest.raises(RuntimeError, match="already has a pinned slot"):
            consumer.pin(view)

        consumer.release(pinned)
        consumer.close()
    finally:
        ring.close()


def test_consumer_release_wrong_view_raises():
    """Test releasing a view that doesn't match current pin raises."""
    ring = create_ring_buffer(
        camera_id="cam_test_wrong_release",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_wrong_release")

        producer.publish(make_test_frame(0))
        view = consumer.latest()
        pinned = consumer.pin(view)

        # Create a fake pinned view with different slot
        fake_view = FrameView(
            descriptor=view.descriptor,
            payload=view.payload,
            slot_index=999,  # Different slot
            generation=view.generation,
            ring=ring,
        )
        fake_pinned = PinnedView(fake_view, consumer)

        with pytest.raises(RuntimeError, match="does not match current pin"):
            consumer.release(fake_pinned)

        consumer.release(pinned)
        consumer.close()
    finally:
        ring.close()


def test_consumer_pin_context_manager():
    """Test pin as context manager auto-releases."""
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

        # Should be released after context
        assert consumer._pinned_slot is None

        consumer.close()
    finally:
        ring.close()


def test_consumer_pin_context_manager_exception():
    """Test pin context manager releases on exception."""
    ring = create_ring_buffer(
        camera_id="cam_test_pin_cm_exc",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_pin_cm_exc")

        producer.publish(make_test_frame(0))
        view = consumer.latest()

        try:
            with consumer.pin(view):
                raise ValueError("test exception")
        except ValueError:
            pass

        # Should be released despite exception
        assert consumer._pinned_slot is None

        consumer.close()
    finally:
        ring.close()


def test_consumer_stats_independent():
    """Test each consumer has independent statistics."""
    ring = create_ring_buffer(
        camera_id="cam_test_stats_indep",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=8,
    )

    try:
        producer = ring.producer()
        consumer1 = ring.consumer("consumer1")
        consumer2 = ring.consumer("consumer2")

        # Publish 5 frames
        for seq in range(5):
            producer.publish(make_test_frame(seq))

        # Consumer1 reads all
        for seq in range(5):
            consumer1.next(seq)

        # Consumer2 reads only latest
        consumer2.latest()

        stats1 = consumer1.stats()
        stats2 = consumer2.stats()

        assert stats1.consumed == 5
        assert stats2.consumed == 1

        consumer1.close()
        consumer2.close()
    finally:
        ring.close()


def test_consumer_close_releases_pin():
    """Test closing consumer releases any pinned slot."""
    ring = create_ring_buffer(
        camera_id="cam_test_close_releases",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=3,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_close_releases")

        producer.publish(make_test_frame(0))
        view = consumer.latest()
        pinned = consumer.pin(view)

        # Close consumer (should release pin)
        consumer.close()

        # Producer should now be able to reuse the slot
        result = producer.publish(make_test_frame(1))
        assert result.accepted is True

    finally:
        ring.close()


def test_consumer_after_close_raises():
    """Test operations on closed consumer raise."""
    ring = create_ring_buffer(
        camera_id="cam_test_closed_consumer",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        consumer = ring.consumer("test_closed")
        consumer.close()

        with pytest.raises(RuntimeError, match="Consumer is closed"):
            consumer.latest()

        with pytest.raises(RuntimeError, match="Consumer is closed"):
            consumer.next(0)

    finally:
        ring.close()


def test_frame_view_thermal_view():
    """Test FrameView.thermal() returns correct array."""
    ring = create_ring_buffer(
        camera_id="cam_test_fv_thermal",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_fv_thermal")

        frame = make_test_frame(42, thermal_shape=(16, 16))
        producer.publish(frame)

        view = consumer.latest()
        thermal = view.thermal()

        assert thermal is not None
        assert thermal.shape == (16, 16)
        assert thermal.dtype == np.uint16
        assert not thermal.flags.writeable
        # Check data matches
        np.testing.assert_array_equal(thermal, frame.payload.thermal)

        consumer.close()
    finally:
        ring.close()


def test_frame_view_visible_view():
    """Test FrameView.visible() returns correct array."""
    ring = create_ring_buffer(
        camera_id="cam_test_fv_visible",
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
        consumer = ring.consumer("test_fv_visible")

        frame = make_test_frame(42, thermal_shape=(16, 16), visible_shape=(16, 16))
        producer.publish(frame)

        view = consumer.latest()
        visible = view.visible()

        assert visible is not None
        assert visible.shape == (16, 16)
        assert visible.dtype == np.uint8
        assert not visible.flags.writeable

        consumer.close()
    finally:
        ring.close()


def test_frame_view_descriptor_access():
    """Test FrameView.descriptor provides full metadata."""
    ring = create_ring_buffer(
        camera_id="cam_test_fv_desc",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_fv_desc")

        frame = make_test_frame(99, thermal_shape=(16, 16))
        # Override camera_id to match ring
        frame = Frame(
            descriptor=FrameDescriptor(
                camera_id="cam_test_fv_desc",
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

        view = consumer.latest()
        desc = view.descriptor

        assert desc.camera_id == "cam_test_fv_desc"
        assert desc.sequence == 99
        assert desc.thermal.present is True
        assert desc.thermal.width == 16
        assert desc.thermal.height == 16
        assert desc.thermal.dtype == "uint16"
        assert desc.visible.present is False
        assert desc.sync.status == SyncStatus.MISSING_VISIBLE

        consumer.close()
    finally:
        ring.close()


def test_frame_view_copy_creates_independent_frame():
    """Test FrameView.copy() creates fully independent Frame."""
    ring = create_ring_buffer(
        camera_id="cam_test_fv_copy",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_fv_copy")

        frame = make_test_frame(123, thermal_shape=(16, 16))
        # Override camera_id to match ring
        frame = Frame(
            descriptor=FrameDescriptor(
                camera_id="cam_test_fv_copy",
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

        view = consumer.latest()
        copied = view.copy()

        # Should be a proper Frame
        assert isinstance(copied, Frame)
        assert copied.descriptor.sequence == 123
        assert copied.payload.thermal is not None
        assert copied.payload.thermal.flags.writeable is False
        assert copied.payload.thermal.shape == (16, 16)

        # Data should match
        np.testing.assert_array_equal(copied.payload.thermal, view.thermal())

        # Modifying the copy's data should not affect shared memory
        # (copy is read-only, but we can verify it's a different array)
        assert copied.payload.thermal is not view.thermal()

        consumer.close()
    finally:
        ring.close()


def test_frame_view_valid_after_reuse():
    """Test FrameView.valid() returns False after slot reuse."""
    ring = create_ring_buffer(
        camera_id="cam_test_fv_valid_reuse",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=2,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_fv_valid_reuse")

        producer.publish(make_test_frame(0))
        view = consumer.latest()
        assert view.valid()

        # Overwrite the slot
        producer.publish(make_test_frame(1))
        producer.publish(make_test_frame(2))  # Reuses slot 0

        assert not view.valid()

        consumer.close()
    finally:
        ring.close()


def test_frame_view_valid_after_close():
    """Test FrameView.valid() after ring close."""
    ring = create_ring_buffer(
        camera_id="cam_test_fv_valid_close",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_fv_valid_close")

        producer.publish(make_test_frame(0))
        view = consumer.latest()
        assert view.valid()

        ring.close()

        # View should be invalid after close
        assert not view.valid()

    finally:
        pass  # Ring already closed


def test_multiple_consumers_same_ring():
    """Test multiple consumers operating on same ring independently."""
    ring = create_ring_buffer(
        camera_id="cam_test_multi_cons",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=32,  # Larger depth to hold all frames
    )

    try:
        producer = ring.producer()

        # Create 4 consumers like the spec: Processing, Observer, Recorder, Diagnostics
        processing = ring.consumer("processing")
        observer = ring.consumer("observer")
        recorder = ring.consumer("recorder")
        diagnostics = ring.consumer("diagnostics")

        # Producer: 100 FPS test (simulate with 20 frames, depth 32 holds all)
        for seq in range(20):
            frame = make_test_frame(seq)
            # Override camera_id to match ring
            frame = Frame(
                descriptor=FrameDescriptor(
                    camera_id="cam_test_multi_cons",
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

        # Processing: consumes every frame
        for seq in range(20):
            view = processing.next(seq)
            assert view is not None
            assert view.descriptor.sequence == seq

        # Observer: latest()
        view = observer.latest()
        assert view.descriptor.sequence == 19

        # Recorder: sequential
        for seq in range(20):
            view = recorder.next(seq)
            assert view is not None

        # Diagnostics: periodic latest()
        for _ in range(10):
            view = diagnostics.latest()
            assert view is not None
            assert view.descriptor.sequence == 19

        # Stats
        p_stats = processing.stats()
        o_stats = observer.stats()
        r_stats = recorder.stats()
        d_stats = diagnostics.stats()

        assert p_stats.consumed == 20
        assert o_stats.consumed == 1
        assert r_stats.consumed == 20
        assert d_stats.consumed == 10

        # One consumer slow doesn't stop others
        # (Processing already consumed all, but producer kept going)

        processing.close()
        observer.close()
        recorder.close()
        diagnostics.close()
    finally:
        ring.close()


def test_consumer_pinned_view_access():
    """Test accessing data through PinnedView."""
    ring = create_ring_buffer(
        camera_id="cam_test_pinned_access",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_pinned_access")

        producer.publish(make_test_frame(0))
        view = consumer.latest()

        with consumer.pin(view) as pinned:
            # Access through pinned view
            thermal = pinned.thermal()
            assert thermal is not None
            assert thermal.shape == (16, 16)

            desc = pinned.descriptor
            assert desc.sequence == 0

        consumer.close()
    finally:
        ring.close()


def test_consumer_generation_validation():
    """Test consumer validates generation on read."""
    ring = create_ring_buffer(
        camera_id="cam_test_gen_val",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=4,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_gen_val")

        producer.publish(make_test_frame(0))
        view = consumer.latest()
        assert view.generation > 0

        # Manually check generation matches
        assert view.valid()

        consumer.close()
    finally:
        ring.close()


def test_consumer_stale_detection():
    """Test consumer detects stale view (generation mismatch)."""
    ring = create_ring_buffer(
        camera_id="cam_test_stale",
        thermal_width=16,
        thermal_height=16,
        thermal_dtype=np.dtype(np.uint16),
        depth=2,
    )

    try:
        producer = ring.producer()
        consumer = ring.consumer("test_stale")

        producer.publish(make_test_frame(0))
        view = consumer.latest()

        # Overwrite slot
        producer.publish(make_test_frame(1))
        producer.publish(make_test_frame(2))

        # View is now stale
        assert not view.valid()

        # next() should detect and count as stale
        view2 = consumer.next(0)  # This slot was reused
        assert view2 is None

        stats = consumer.stats()
        # stale or overwritten should be incremented
        assert stats.stale > 0 or stats.overwritten > 0

        consumer.close()
    finally:
        ring.close()