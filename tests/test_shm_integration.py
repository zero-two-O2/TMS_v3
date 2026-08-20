"""Integration tests for shared-memory ring producer/consumer flow (Stage 7B).

Tests the complete path:
TV46LDriver -> AcquisitionWorker -> SharedMemoryRingBuffer -> Consumer
"""

from __future__ import annotations

import time
import threading
import numpy as np
import pytest

from thermal_monitor.camera.acquisition import AcquisitionWorker, AcquisitionState
from thermal_monitor.camera.driver import (
    CameraGrabTimeout,
    FrameSource,
    GrabResult,
)
from thermal_monitor.camera.model import CameraConfig, CameraIdentity, PublishResult
from thermal_monitor.camera.shm import create_ring_buffer_and_publisher
from thermal_monitor.core.frame import Frame, FrameDescriptor, FramePayload, StreamMetadata, SyncInfo, SyncStatus
from thermal_monitor.core.shm import SlotState


# ─── Test Fixtures ──────────────────────────────────────────────────────────────

def make_camera_config(**overrides) -> CameraConfig:
    defaults = {
        "identity": CameraIdentity(
            camera_id="cam_test",
            serial_number="SN12345",
            model="TV46L-1-26010003@9Hz",
            vendor="Fluke Process Instruments",
        ),
        "device_identifier": "test_device_identifier",
        "grab_timeout_ms": 100,
        "consecutive_fail_limit": 2,
        "reconnect_interval_s": 0.01,
        "reconnect_backoff_factor": 1.0,
        "max_reconnect_attempts": 3,
    }
    defaults.update(overrides)
    return CameraConfig(**defaults)


def make_thermal_frame(camera_id: str, sequence: int, width: int = 640, height: int = 480) -> Frame:
    """Create a test frame with thermal payload."""
    thermal = np.arange(sequence, sequence + width * height, dtype=np.uint16).reshape(height, width)
    thermal.setflags(write=False)

    thermal_meta = StreamMetadata(
        present=True,
        width=width,
        height=height,
        pixel_format="IR_Data",
        dtype="uint16",
        byte_count=thermal.nbytes,
        sequence=sequence,  # hardware frame_id
        timestamp=1000.0 + sequence * 0.111,
        monotonic_timestamp=100.0 + sequence * 0.111,
        hardware_timestamp=1000.0 + sequence * 0.111,
    )

    visible_meta = StreamMetadata(
        present=False,
    )

    sync = SyncInfo(status=SyncStatus.MISSING_VISIBLE)

    descriptor = FrameDescriptor(
        camera_id=camera_id,
        sequence=sequence,
        timestamp=1000.0 + sequence * 0.111,
        monotonic_timestamp=100.0 + sequence * 0.111,
        thermal=thermal_meta,
        visible=visible_meta,
        sync=sync,
        metadata={"grab_duration_s": 0.001, "packet_stats": {"packets_seen": sequence * 1000, "packets_lost": 0, "blocks_incomplete": 0}},
    )

    return Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal, visible=None))


class FakeFrameSource:
    """Scripted FrameSource for deterministic worker tests."""

    def __init__(
        self,
        grab_script=None,
        always_raise=None,
        connect_error=None,
        reopen_error=None,
        default=None,
        frame_interval_s: float = 0.1,  # Simulate ~10 FPS
    ) -> None:
        self._grab_script = list(grab_script or [])
        self._always_raise = always_raise
        self._connect_error = connect_error
        self._reopen_error = reopen_error
        self._default = default
        self._script_index = 0
        self._frame_interval_s = frame_interval_s
        self._last_grab_time = 0.0
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.grab_calls = 0
        self.reopen_calls = 0

    def connect(self) -> None:
        self.connect_calls += 1
        if self._connect_error is not None:
            raise self._connect_error

    def disconnect(self) -> None:
        self.disconnect_calls += 1

    def is_connected(self) -> bool:
        return True

    def grab(self, timeout_ms: int) -> GrabResult:
        # Simulate frame rate
        if self._frame_interval_s > 0:
            now = time.perf_counter()
            elapsed = now - self._last_grab_time
            if elapsed < self._frame_interval_s:
                time.sleep(self._frame_interval_s - elapsed)
            self._last_grab_time = time.perf_counter()

        self.grab_calls += 1
        if self._script_index < len(self._grab_script):
            item = self._grab_script[self._script_index]
            self._script_index += 1
            if isinstance(item, Exception):
                raise item
            return item
        if self._always_raise is not None:
            raise self._always_raise
        return self._default if self._default is not None else make_grab_result()

    def reopen(self) -> None:
        self.reopen_calls += 1
        if self._reopen_error is not None:
            raise self._reopen_error


def make_grab_result(width: int = 16, height: int = 16) -> GrabResult:
    """Create a minimal GrabResult for testing."""
    array = np.arange(width * height, dtype=np.uint16).reshape(height, width)
    array.setflags(write=False)
    return GrabResult(
        thermal=array,
        thermal_format="IR_Data",
        hardware_timestamp=None,
        frame_id=None,
        packet_stats={"packets_seen": 0, "packets_lost": 0, "blocks_incomplete": 0, "blocks_discarded": 0},
        grab_started=time.perf_counter(),
        grab_completed=time.perf_counter(),
        converted_at=time.perf_counter(),
    )


# ─── Integration Tests ──────────────────────────────────────────────────────────

class TestSharedMemoryRingIntegration:
    """Test IR producer -> shared-memory ring -> consumer flow."""

    def test_basic_producer_consumer(self):
        """Test basic frame publication and consumption through ring."""
        camera_id = "cam_test"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=4)

        try:
            # Producer publishes frame
            frame = make_thermal_frame(camera_id, 0, 16, 16)
            result = publisher.publish(frame)
            assert result.accepted is True
            assert result.sequence == 0

            # Consumer reads frame (attach to same ring)
            ring2, consumer = __import__('thermal_monitor.camera.shm', fromlist=['attach_ring_buffer_and_consumer']).attach_ring_buffer_and_consumer(
                camera_id, "test_consumer", width=16, height=16, depth=4
            )
            try:
                view = consumer.latest()
                assert view is not None
                assert view.descriptor.sequence == 0
                assert view.descriptor.camera_id == camera_id
                assert view.payload.thermal is not None
                assert view.payload.thermal.shape == (16, 16)
                assert view.payload.thermal.dtype == np.uint16
                assert not view.payload.thermal.flags.writeable

                # Verify hardware metadata preserved
                assert view.descriptor.thermal.sequence == 0
                assert view.descriptor.thermal.hardware_timestamp is not None
            finally:
                consumer.close()
                # Do NOT close ring2 - it's the same shared memory as ring
        finally:
            ring.close()  # Only producer closes the ring

    def test_multiple_frames_sequence(self):
        """Test multiple frames in sequence with correct ordering."""
        camera_id = "cam_seq"
        # Use depth > number of frames to avoid wraparound
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=16)

        try:
            # Publish 10 frames
            for seq in range(10):
                frame = make_thermal_frame(camera_id, seq, 16, 16)
                result = publisher.publish(frame)
                assert result.accepted is True
                assert result.sequence == seq

            # Consumer reads sequentially
            ring2, consumer = __import__('thermal_monitor.camera.shm', fromlist=['attach_ring_buffer_and_consumer']).attach_ring_buffer_and_consumer(
                camera_id, "seq_consumer", width=16, height=16, depth=16
            )
            try:
                for expected in range(10):
                    view = consumer.next(expected)
                    assert view is not None, f"Frame {expected} not found"
                    assert view.descriptor.sequence == expected
                    assert view.payload.thermal[0, 0] == expected  # Data matches

                stats = consumer.stats()
                assert stats.consumed == 10
                assert stats.overwritten == 0
                assert stats.gaps == 0
            finally:
                consumer.close()
        finally:
            ring.close()

    def test_ring_wraparound(self):
        """Test ring buffer wraparound with small depth."""
        camera_id = "cam_wrap"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=4)

        try:
            # Publish more frames than depth
            for seq in range(12):
                frame = make_thermal_frame(camera_id, seq, 16, 16)
                result = publisher.publish(frame)
                assert result.accepted is True

            # Consumer should see last 4 frames (8, 9, 10, 11)
            ring2, consumer = __import__('thermal_monitor.camera.shm', fromlist=['attach_ring_buffer_and_consumer']).attach_ring_buffer_and_consumer(
                camera_id, "wrap_consumer", width=16, height=16, depth=4
            )
            try:
                # Latest should be frame 11
                view = consumer.latest()
                assert view is not None
                assert view.descriptor.sequence == 11

                # Earlier frames overwritten
                view = consumer.next(0)
                assert view is None  # Overwritten

                stats = consumer.stats()
                assert stats.overwritten > 0
            finally:
                consumer.close()
        finally:
            ring.close()

    def test_producer_faster_than_consumer(self):
        """Test producer continues when consumer is slow."""
        camera_id = "cam_fast"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=4)

        try:
            ring2, slow_consumer = __import__('thermal_monitor.camera.shm', fromlist=['attach_ring_buffer_and_consumer']).attach_ring_buffer_and_consumer(
                camera_id, "slow_consumer", width=16, height=16, depth=4
            )
            ring3, fast_consumer = __import__('thermal_monitor.camera.shm', fromlist=['attach_ring_buffer_and_consumer']).attach_ring_buffer_and_consumer(
                camera_id, "fast_consumer", width=16, height=16, depth=4
            )

            try:
                # Slow consumer pins first frame
                publisher.publish(make_thermal_frame(camera_id, 0, 16, 16))
                view = slow_consumer.latest()
                assert view is not None
                pinned = slow_consumer.pin(view)

                # Producer should continue publishing to other slots
                for seq in range(1, 6):
                    result = publisher.publish(make_thermal_frame(camera_id, seq, 16, 16))
                    # Some may be dropped if all other slots fill up

                # Fast consumer can still read latest
                view = fast_consumer.latest()
                assert view is not None
                assert view.descriptor.sequence >= 1

                slow_consumer.release(pinned)
            finally:
                slow_consumer.close()
                fast_consumer.close()
                # Do NOT close ring2, ring3
        finally:
            ring.close()

    def test_dropped_frame_accounting(self):
        """Test explicit dropped-frame accounting when ring is full."""
        camera_id = "cam_drop"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=2)

        try:
            ring2, consumer1 = __import__('thermal_monitor.camera.shm', fromlist=['attach_ring_buffer_and_consumer']).attach_ring_buffer_and_consumer(
                camera_id, "consumer1", width=16, height=16, depth=2
            )
            ring3, consumer2 = __import__('thermal_monitor.camera.shm', fromlist=['attach_ring_buffer_and_consumer']).attach_ring_buffer_and_consumer(
                camera_id, "consumer2", width=16, height=16, depth=2
            )

            try:
                # Fill all slots and pin them
                publisher.publish(make_thermal_frame(camera_id, 0, 16, 16))
                view0 = consumer1.latest()
                pinned0 = consumer1.pin(view0)

                publisher.publish(make_thermal_frame(camera_id, 1, 16, 16))
                view1 = consumer2.latest()
                pinned1 = consumer2.pin(view1)

                # Next publish should be dropped
                frame = make_thermal_frame(camera_id, 2, 16, 16)
                result = publisher.publish(frame)
                assert result.accepted is False
                assert result.dropped is True
                assert result.sequence == 2

                # Release one pin
                consumer1.release(pinned0)

                # Now publish should succeed
                result = publisher.publish(make_thermal_frame(camera_id, 2, 16, 16))
                assert result.accepted is True

            finally:
                consumer1.close()
                consumer2.close()
                # Do NOT close ring2, ring3
        finally:
            ring.close()

    def test_consumer_restart(self):
        """Test consumer can restart and re-read from ring."""
        camera_id = "cam_restart"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=8)

        try:
            # Publish frames
            for seq in range(5):
                publisher.publish(make_thermal_frame(camera_id, seq, 16, 16))

            # First consumer reads some
            ring2, consumer1 = __import__('thermal_monitor.camera.shm', fromlist=['attach_ring_buffer_and_consumer']).attach_ring_buffer_and_consumer(
                camera_id, "consumer1", width=16, height=16, depth=8
            )
            try:
                for seq in range(3):
                    view = consumer1.next(seq)
                    assert view is not None
                consumer1.close()
            finally:
                # Do NOT close ring2
                pass

            # Second consumer reads all (new attach to same ring)
            ring3, consumer2 = __import__('thermal_monitor.camera.shm', fromlist=['attach_ring_buffer_and_consumer']).attach_ring_buffer_and_consumer(
                camera_id, "consumer2", width=16, height=16, depth=8
            )
            try:
                for seq in range(5):
                    view = consumer2.next(seq)
                    assert view is not None
                    assert view.descriptor.sequence == seq
                consumer2.close()
            finally:
                pass  # Do not close ring3
        finally:
            ring.close()

    def test_multi_camera_isolation(self):
        """Test multiple camera rings are isolated."""
        # Use unique camera IDs to avoid shared memory collisions across test runs
        import uuid
        unique_suffix = uuid.uuid4().hex[:8]
        camera_id_a = f"cam_a_{unique_suffix}"
        camera_id_b = f"cam_b_{unique_suffix}"
        ring_a, pub_a = create_ring_buffer_and_publisher(camera_id_a, width=16, height=16, depth=4)
        ring_b, pub_b = create_ring_buffer_and_publisher(camera_id_b, width=16, height=16, depth=4)

        try:
            pub_a.publish(make_thermal_frame(camera_id_a, 0, 16, 16))
            pub_b.publish(make_thermal_frame(camera_id_b, 100, 16, 16))  # Different sequence

            ring_a2, consumer_a = __import__('thermal_monitor.camera.shm', fromlist=['attach_ring_buffer_and_consumer']).attach_ring_buffer_and_consumer(
                camera_id_a, "consumer_a", width=16, height=16, depth=4
            )
            ring_b2, consumer_b = __import__('thermal_monitor.camera.shm', fromlist=['attach_ring_buffer_and_consumer']).attach_ring_buffer_and_consumer(
                camera_id_b, "consumer_b", width=16, height=16, depth=4
            )

            try:
                view_a = consumer_a.latest()
                view_b = consumer_b.latest()
                assert view_a.descriptor.camera_id == camera_id_a
                assert view_b.descriptor.camera_id == camera_id_b
                assert view_a.descriptor.sequence == 0
                assert view_b.descriptor.sequence == 100
            finally:
                consumer_a.close()
                consumer_b.close()
                # Do NOT close ring_a2, ring_b2
        finally:
            ring_a.close()
            ring_b.close()

    def test_hardware_metadata_survives_ring(self):
        """Test hardware frame_id, timestamp survive ring transit.
        
        Note: packet_stats is stored in frame metadata but not yet encoded
        in the ring buffer descriptor format. This test verifies the
        core hardware metadata that IS preserved.
        """
        camera_id = "cam_hw"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=4)

        try:
            # Frame with full hardware metadata - create with correct values from start
            thermal = np.arange(42, 42 + 16 * 16, dtype=np.uint16).reshape(16, 16)
            thermal.setflags(write=False)

            thermal_meta = StreamMetadata(
                present=True,
                width=16,
                height=16,
                pixel_format="IR_Data",
                dtype="uint16",
                byte_count=thermal.nbytes,
                sequence=1000,  # hardware frame_id
                timestamp=1234567890.123,
                monotonic_timestamp=100.0,
                hardware_timestamp=1234567890.123,
            )
            visible_meta = StreamMetadata(present=False)
            sync = SyncInfo(status=SyncStatus.MISSING_VISIBLE)
            descriptor = FrameDescriptor(
                camera_id=camera_id,
                sequence=42,
                timestamp=1234567890.123,
                monotonic_timestamp=100.0,
                thermal=thermal_meta,
                visible=visible_meta,
                sync=sync,
                metadata={"grab_duration_s": 0.001},  # Only timing fields survive ring
            )
            frame = Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal, visible=None))

            publisher.publish(frame)

            ring2, consumer = __import__('thermal_monitor.camera.shm', fromlist=['attach_ring_buffer_and_consumer']).attach_ring_buffer_and_consumer(
                camera_id, "hw_consumer", width=16, height=16, depth=4
            )
            try:
                view = consumer.latest()
                assert view is not None

                # Hardware metadata preserved
                assert view.descriptor.thermal.sequence == 1000
                assert abs(view.descriptor.thermal.hardware_timestamp - 1234567890.123) < 0.001

                # Timing metadata preserved
                metadata = view.descriptor.metadata
                assert "grab_duration_s" in metadata
                assert abs(metadata["grab_duration_s"] - 0.001) < 0.001

            finally:
                consumer.close()
        finally:
            ring.close()

    def test_raw_uint16_data_unchanged(self):
        """Test raw uint16 data remains unchanged through ring."""
        camera_id = "cam_data"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=4)

        try:
            # Create frame with known pattern
            thermal = np.arange(256, dtype=np.uint16).reshape(16, 16)
            thermal.setflags(write=False)

            thermal_meta = StreamMetadata(
                present=True, width=16, height=16, pixel_format="IR_Data",
                dtype="uint16", byte_count=thermal.nbytes,
                sequence=1, timestamp=1000.0, monotonic_timestamp=100.0,
                hardware_timestamp=1000.0,
            )
            visible_meta = StreamMetadata(present=False)
            sync = SyncInfo(status=SyncStatus.MISSING_VISIBLE)
            descriptor = FrameDescriptor(
                camera_id=camera_id, sequence=1, timestamp=1000.0,
                monotonic_timestamp=100.0, thermal=thermal_meta, visible=visible_meta,
                sync=sync, metadata={},
            )
            frame = Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal, visible=None))

            publisher.publish(frame)

            ring2, consumer = __import__('thermal_monitor.camera.shm', fromlist=['attach_ring_buffer_and_consumer']).attach_ring_buffer_and_consumer(
                camera_id, "data_consumer", width=16, height=16, depth=4
            )
            try:
                view = consumer.latest()
                assert view is not None

                # Data should match exactly
                received = view.payload.thermal
                np.testing.assert_array_equal(received, thermal)
                assert received.dtype == np.uint16
                assert not received.flags.writeable

            finally:
                consumer.close()
        finally:
            ring.close()

    def test_producer_never_blocks_on_consumer(self):
        """Test producer never blocks, even when consumer is slow."""
        camera_id = "cam_noblock"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=4)

        try:
            ring2, consumer = __import__('thermal_monitor.camera.shm', fromlist=['attach_ring_buffer_and_consumer']).attach_ring_buffer_and_consumer(
                camera_id, "slow", width=16, height=16, depth=4
            )

            try:
                # Pin first slot
                publisher.publish(make_thermal_frame(camera_id, 0, 16, 16))
                view = consumer.latest()
                pinned0 = consumer.pin(view)

                # Pin second slot
                publisher.publish(make_thermal_frame(camera_id, 1, 16, 16))
                view = consumer.latest()
                # Can't pin again with same consumer - use latest_pinned instead
                # Just verify producer doesn't block
                start = time.perf_counter()
                result = publisher.publish(make_thermal_frame(camera_id, 99, 16, 16))
                elapsed = time.perf_counter() - start

                assert result.accepted is True
                assert elapsed < 0.1, f"Producer blocked for {elapsed:.3f}s"

                consumer.release(pinned0)
            finally:
                consumer.close()
        finally:
            ring.close()


class TestAcquisitionWorkerWithSharedMemory:
    """Test AcquisitionWorker using SharedMemoryPublisher."""

    def test_worker_with_shm_publisher(self):
        """Test AcquisitionWorker publishes to shared-memory ring."""
        config = make_camera_config()
        camera_id = config.identity.camera_id
        source = FakeFrameSource()
        
        # Create ring and publisher - test owns the ring
        ring, publisher = create_ring_buffer_and_publisher(
            camera_id, width=16, height=16, depth=8
        )

        worker = AcquisitionWorker(camera_id, source, publisher, config)

        try:
            worker.start()
            assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=5.0)

            # Wait for frames - attach consumer to same ring
            ring2, consumer = __import__('thermal_monitor.camera.shm', fromlist=['attach_ring_buffer_and_consumer']).attach_ring_buffer_and_consumer(
                camera_id, "test_consumer", width=16, height=16, depth=8
            )
            try:
                deadline = time.monotonic() + 3.0
                frames_seen = 0
                while frames_seen < 5 and time.monotonic() < deadline:
                    view = consumer.next(frames_seen)
                    if view:
                        frames_seen += 1
                    time.sleep(0.01)

                assert frames_seen >= 5

                stats = worker.stats()
                assert stats.published >= 5
                assert stats.frames_received >= 5

            finally:
                consumer.close()
                # Do NOT close ring2
        finally:
            worker.stop(timeout=5.0)
            # Worker stops and calls publisher.close() which closes producer
            # Test owns the ring, so close it here
            ring.close()

    def test_worker_reconnect_preserves_ring(self):
        """Test worker reconnect doesn't break ring connection."""
        config = make_camera_config(reconnect_interval_s=0.01)
        camera_id = config.identity.camera_id + "_reconnect"
        timeout = CameraGrabTimeout("timeout")
        # Provide many valid frames after the initial timeouts
        valid_frames = [make_grab_result() for _ in range(20)]
        source = FakeFrameSource(grab_script=[timeout, timeout, timeout] + valid_frames, frame_interval_s=0.05)
        ring, publisher = create_ring_buffer_and_publisher(
            camera_id, width=16, height=16, depth=8
        )

        worker = AcquisitionWorker(camera_id, source, publisher, config)

        try:
            worker.start()
            assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=5.0)

            # Wait for reconnect
            deadline = time.monotonic() + 5.0
            while worker.stats().reconnect_count == 0 and time.monotonic() < deadline:
                time.sleep(0.01)

            assert worker.stats().reconnect_count >= 1

            # Wait for frames to be received after reconnect
            deadline = time.monotonic() + 3.0
            while worker.stats().frames_received == 0 and time.monotonic() < deadline:
                time.sleep(0.01)

            assert worker.stats().frames_received > 0
        finally:
            worker.stop(timeout=5.0)
            ring.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])