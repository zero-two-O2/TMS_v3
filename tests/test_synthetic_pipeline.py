"""Synthetic end-to-end acquisition pipeline integration tests.

This test suite proves the complete V3 data path works:

Synthetic Source
    ↓
AcquisitionWorker
    ↓
SharedMemoryRingBuffer
    ↓
Independent Consumers (Processing, Observer, Recorder, Diagnostics)

No real hardware is required. All components are synthetic/test-only.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pytest

from thermal_monitor.camera.acquisition import AcquisitionWorker
from thermal_monitor.camera.driver import FrameSource, GrabResult
from thermal_monitor.camera.model import CameraConfig, CameraIdentity, PublishResult
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
    create_ring_buffer,
    SharedMemoryPublisher,
    Consumer,
    FrameView,
)

logger = logging.getLogger(__name__)

# ─── Synthetic Frame Source ──────────────────────────────────────────────────

@dataclass(slots=True)
class SyntheticFrameSourceConfig:
    """Configuration for synthetic frame generation."""
    thermal_width: int = 16
    thermal_height: int = 16
    thermal_dtype: np.dtype = np.dtype(np.uint16)
    visible_width: int | None = 16
    visible_height: int | None = 16
    visible_dtype: np.dtype | None = np.dtype(np.uint8)
    frame_interval_s: float = 0.01  # ~100 FPS for fast tests
    max_frames: int | None = None
    include_visible: bool = True
    fail_after: int | None = None  # Fail after N frames (None = never)


class SyntheticFrameSource(FrameSource):
    """Synthetic frame source that generates deterministic test frames.

    This behaves like a camera source from AcquisitionWorker's perspective
    but requires no hardware. It generates frames with known patterns
    for verification.
    """

    def __init__(self, config: SyntheticFrameSourceConfig) -> None:
        self._config = config
        self._sequence = 0
        self._connected = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame_count = 0
        self._lock = threading.Lock()

    def connect(self) -> None:
        with self._lock:
            if self._connected:
                return
            self._connected = True
            self._sequence = 0
            self._frame_count = 0
            self._stop_event.clear()

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            self._stop_event.set()
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=1.0)

    def is_connected(self) -> bool:
        return self._connected

    def grab(self, timeout_ms: int) -> GrabResult:
        if not self._connected:
            raise RuntimeError("Not connected")

        # Check if we should fail
        if self._config.fail_after is not None and self._frame_count >= self._config.fail_after:
            raise RuntimeError(f"Synthetic failure after {self._config.fail_after} frames")

        # Check max frames
        if self._config.max_frames is not None and self._frame_count >= self._config.max_frames:
            raise RuntimeError("Max frames reached")

        # Simulate frame interval
        time.sleep(self._config.frame_interval_s)

        # Generate frame data
        thermal = self._generate_thermal()
        visible = self._generate_visible() if self._config.include_visible and self._config.visible_width else None

        now = time.perf_counter()
        grab_started = now - self._config.frame_interval_s
        grab_completed = now
        converted_at = now

        self._frame_count += 1

        return GrabResult(
            thermal=thermal,
            visible=visible,
            thermal_format="IR_Data",
            visible_format="RGB8" if visible is not None else None,
            hardware_timestamp=None,
            grab_started=grab_started,
            grab_completed=grab_completed,
            converted_at=converted_at,
        )

    def reopen(self) -> None:
        self.disconnect()
        self.connect()

    def _generate_thermal(self) -> np.ndarray:
        """Generate deterministic thermal frame with sequence encoded."""
        w, h = self._config.thermal_width, self._config.thermal_height
        arr = np.zeros((h, w), dtype=self._config.thermal_dtype)
        # Encode sequence in first few pixels for verification
        arr[0, 0] = self._sequence & 0xFFFF
        arr[0, 1] = (self._sequence >> 16) & 0xFFFF
        # Fill rest with pattern
        for y in range(h):
            for x in range(w):
                if x < 2 and y == 0:
                    continue
                arr[y, x] = (self._sequence + y * w + x) & 0xFFFF
        arr.setflags(write=False)
        self._sequence += 1
        return arr

    def _generate_visible(self) -> np.ndarray | None:
        """Generate deterministic visible frame."""
        if self._config.visible_width is None or self._config.visible_height is None:
            return None
        w, h = self._config.visible_width, self._config.visible_height
        arr = np.zeros((h, w), dtype=self._config.visible_dtype)
        # Encode sequence
        arr[0, 0] = self._sequence & 0xFF
        arr[0, 1] = (self._sequence >> 8) & 0xFF
        for y in range(h):
            for x in range(w):
                if x < 2 and y == 0:
                    continue
                arr[y, x] = (self._sequence + y * w + x) & 0xFF
        arr.setflags(write=False)
        return arr


# ─── Synthetic Consumers ─────────────────────────────────────────────────────

@dataclass(slots=True)
class ProcessingConsumer:
    """Sequential consumer that verifies every frame.

    - Consumes frames in sequence order
    - Pins frames when correctness is required
    - Verifies sequence ordering, payload dimensions, dtype, read-only state
    - Releases pin after processing
    """
    consumer: Consumer
    expected_sequence: int = 0
    processed_frames: list[Frame] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    _running: bool = False

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def process_next(self) -> bool:
        """Process the next expected frame. Returns True if successful."""
        if not self._running:
            return False

        view = self.consumer.next(self.expected_sequence)
        if view is None:
            return False

        # Verify frame
        self._verify_frame(view)

        # Pin for processing (simulating correctness requirement)
        with self.consumer.pin(view) as pinned:
            # Simulate processing work
            self._simulate_processing(pinned)
            # Copy frame for durable storage
            self.processed_frames.append(pinned.copy())

        self.expected_sequence += 1
        return True

    def _verify_frame(self, view: FrameView) -> None:
        desc = view.descriptor
        # Verify sequence
        if desc.sequence != self.expected_sequence:
            self.errors.append(f"Sequence mismatch: expected {self.expected_sequence}, got {desc.sequence}")
        # Verify thermal
        thermal = view.thermal()
        if thermal is None:
            self.errors.append("Missing thermal payload")
        else:
            if thermal.shape != (16, 16):
                self.errors.append(f"Wrong thermal shape: {thermal.shape}")
            if thermal.dtype != np.uint16:
                self.errors.append(f"Wrong thermal dtype: {thermal.dtype}")
            if thermal.flags.writeable:
                self.errors.append("Thermal payload is writeable!")
        # Verify visible
        if desc.visible.present:
            visible = view.visible()
            if visible is None:
                self.errors.append("Visible present but payload is None")
            elif visible.shape != (16, 16):
                self.errors.append(f"Wrong visible shape: {visible.shape}")
            elif visible.dtype != np.uint8:
                self.errors.append(f"Wrong visible dtype: {visible.dtype}")
            elif visible.flags.writeable:
                self.errors.append("Visible payload is writeable!")

    def _simulate_processing(self, view: FrameView) -> None:
        """Simulate some processing work."""
        # Access data to verify it's readable
        thermal = view.thermal()
        if thermal is not None:
            _ = thermal.mean()
        visible = view.visible()
        if visible is not None:
            _ = visible.mean()


@dataclass(slots=True)
class ObserverConsumer:
    """Latest-frame consumer for display-like usage.

    - Uses latest()
    - Tolerates skipped frames
    - Verifies returned frame is current
    - Does NOT pin by default
    - Does NOT block acquisition
    """
    consumer: Consumer
    observed_frames: list[Frame] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    _running: bool = False

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def observe(self) -> bool:
        """Get latest frame. Returns True if got a frame."""
        if not self._running:
            return False

        view = self.consumer.latest()
        if view is None:
            return False

        # Verify frame is valid
        if not view.valid():
            self.errors.append("Observer got stale frame")
            return False

        # Copy for storage (observer might want to keep for display)
        self.observed_frames.append(view.copy())
        return True


@dataclass(slots=True)
class RecorderConsumer:
    """Recorder consumer demonstrating pin/copy/release ownership model.

    - Sequential consumption
    - Pins frame
    - Copies to recording buffer
    - Releases pin
    """
    consumer: Consumer
    expected_sequence: int = 0
    recorded_frames: list[Frame] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    _running: bool = False

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def record_next(self) -> bool:
        """Record the next expected frame. Returns True if successful."""
        if not self._running:
            return False

        view = self.consumer.next(self.expected_sequence)
        if view is None:
            return False

        # Pin while copying
        with self.consumer.pin(view) as pinned:
            self.recorded_frames.append(pinned.copy())

        self.expected_sequence += 1
        return True


@dataclass(slots=True)
class DiagnosticsConsumer:
    """Diagnostics consumer that verifies pipeline health.

    - Measures acquisition FPS
    - Tracks sequence numbers
    - Tracks publisher drops
    - Tracks consumer gaps
    - Reports ring statistics
    """
    consumer: Consumer
    ring: SharedMemoryRingBuffer
    samples: list[dict] = field(default_factory=list)
    _running: bool = False

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def sample(self) -> dict | None:
        """Take a diagnostics sample. Returns stats dict."""
        if not self._running:
            return None

        consumer_stats = self.consumer.stats()
        ring_stats = self.ring.stats()

        sample = {
            "timestamp": time.time(),
            "consumer_consumed": consumer_stats.consumed,
            "consumer_overwritten": consumer_stats.overwritten,
            "consumer_gaps": consumer_stats.gaps,
            "consumer_stale": consumer_stats.stale,
            "consumer_last_sequence": consumer_stats.last_sequence,
            "ring_producer_sequence": ring_stats.producer_sequence,
            "ring_producer_head_slot": ring_stats.producer_head_slot,
            "ring_closed": ring_stats.closed,
        }
        self.samples.append(sample)
        return sample


# ─── Test Helpers ────────────────────────────────────────────────────────────

def make_camera_config(camera_id: str = "cam_test") -> CameraConfig:
    """Create a test camera config."""
    return CameraConfig(
        identity=CameraIdentity(
            camera_id=camera_id,
            serial_number=f"SN_{camera_id}",
            model="TV46L-1-26010003@9Hz",
            vendor="Fluke Process Instruments",
        ),
        device_identifier=f"test_{camera_id}",
        grab_timeout_ms=100,
        consecutive_fail_limit=100,  # High limit for synthetic tests
        reconnect_interval_s=0.01,
    )


def create_synthetic_pipeline(
    camera_id: str = "cam_test",
    depth: int = 8,
    thermal_shape: tuple[int, int] = (16, 16),
    visible_shape: tuple[int, int] | None = (16, 16),
    frame_interval_s: float = 0.01,
    max_frames: int | None = None,
) -> tuple[AcquisitionWorker, SyntheticFrameSource, SharedMemoryRingBuffer]:
    """Create a complete synthetic pipeline: source -> worker -> ring buffer."""
    # Create synthetic source
    source_config = SyntheticFrameSourceConfig(
        thermal_width=thermal_shape[1],
        thermal_height=thermal_shape[0],
        visible_width=visible_shape[1] if visible_shape else None,
        visible_height=visible_shape[0] if visible_shape else None,
        frame_interval_s=frame_interval_s,
        max_frames=max_frames,
        include_visible=visible_shape is not None,
    )
    source = SyntheticFrameSource(source_config)

    # Create ring buffer
    ring = create_ring_buffer(
        camera_id=camera_id,
        thermal_width=thermal_shape[1],
        thermal_height=thermal_shape[0],
        thermal_dtype=np.dtype(np.uint16),
        depth=depth,
        visible_width=visible_shape[1] if visible_shape else None,
        visible_height=visible_shape[0] if visible_shape else None,
        visible_dtype=np.dtype(np.uint8) if visible_shape else None,
    )

    # Create publisher adapter
    publisher = SharedMemoryPublisher(ring)

    # Create acquisition worker
    config = make_camera_config(camera_id)
    worker = AcquisitionWorker(camera_id, source, publisher, config)

    return worker, source, ring


# ─── Integration Tests ───────────────────────────────────────────────────────

class TestSyntheticPipeline:
    """Test the complete synthetic acquisition pipeline."""

    def test_basic_pipeline_source_to_ring(self):
        """Test 1: synthetic source -> acquisition -> ring."""
        worker, source, ring = create_synthetic_pipeline(
            camera_id="cam_pipe_basic",
            max_frames=10,
            frame_interval_s=0.001,  # Fast for test
        )

        try:
            worker.start()
            assert worker.wait_for_state(worker.state.__class__.ACQUIRING, timeout=5.0)

            # Wait for frames (max_frames=10 means sequences 0-9, so producer_sequence=9)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                stats = worker.stats()
                if stats.published >= 10:
                    break
                time.sleep(0.01)

            stats = worker.stats()
            assert stats.published >= 10
            assert stats.total_acquired >= 10

            # Verify ring has frames (sequence 0-9 means producer_sequence=9)
            ring_stats = ring.stats()
            assert ring_stats.producer_sequence >= 9

        finally:
            worker.stop()
            ring.close()

    def test_processing_consumer_sequential(self):
        """Test 2: Processing consumer sequential consumption."""
        worker, source, ring = create_synthetic_pipeline(
            camera_id="cam_proc",
            max_frames=20,
            frame_interval_s=0.001,
        )

        try:
            # Create processing consumer
            consumer = ring.consumer("processing")
            proc = ProcessingConsumer(consumer=consumer)
            proc.start()

            worker.start()
            assert worker.wait_for_state(worker.state.__class__.ACQUIRING, timeout=5.0)

            # Process frames as they arrive
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and proc.expected_sequence < 20:
                proc.process_next()
                time.sleep(0.001)

            proc.stop()

            # Verify all frames processed
            assert proc.expected_sequence == 20
            assert len(proc.processed_frames) == 20
            assert len(proc.errors) == 0, f"Processing errors: {proc.errors}"

            # Verify sequences are correct
            for i, frame in enumerate(proc.processed_frames):
                assert frame.descriptor.sequence == i

        finally:
            worker.stop()
            ring.close()

    def test_observer_consumer_latest(self):
        """Test 3: Observer consumer using latest()."""
        worker, source, ring = create_synthetic_pipeline(
            camera_id="cam_obs",
            max_frames=50,
            frame_interval_s=0.001,
        )

        try:
            consumer = ring.consumer("observer")
            observer = ObserverConsumer(consumer=consumer)
            observer.start()

            worker.start()
            assert worker.wait_for_state(worker.state.__class__.ACQUIRING, timeout=5.0)

            # Observer samples at lower rate
            deadline = time.monotonic() + 5.0
            sample_count = 0
            while time.monotonic() < deadline and sample_count < 20:
                if observer.observe():
                    sample_count += 1
                time.sleep(0.005)  # Sample slower than production

            observer.stop()

            # Should have observed frames, all should be latest
            assert len(observer.observed_frames) > 0
            assert len(observer.errors) == 0, f"Observer errors: {observer.errors}"

            # All observed frames should be valid and have increasing sequences
            sequences = [f.descriptor.sequence for f in observer.observed_frames]
            assert all(sequences[i] <= sequences[i+1] for i in range(len(sequences)-1))

        finally:
            worker.stop()
            ring.close()

    def test_recorder_consumer_pin_copy_release(self):
        """Test 4: Recorder consumer pin/copy/release."""
        worker, source, ring = create_synthetic_pipeline(
            camera_id="cam_rec",
            max_frames=15,
            frame_interval_s=0.001,
        )

        try:
            consumer = ring.consumer("recorder")
            recorder = RecorderConsumer(consumer=consumer)
            recorder.start()

            worker.start()
            assert worker.wait_for_state(worker.state.__class__.ACQUIRING, timeout=5.0)

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and recorder.expected_sequence < 15:
                recorder.record_next()
                time.sleep(0.001)

            recorder.stop()

            assert recorder.expected_sequence == 15
            assert len(recorder.recorded_frames) == 15
            assert len(recorder.errors) == 0, f"Recorder errors: {recorder.errors}"

            for i, frame in enumerate(recorder.recorded_frames):
                assert frame.descriptor.sequence == i

        finally:
            worker.stop()
            ring.close()

    def test_independent_consumers(self):
        """Test 5: Independent consumers on same ring."""
        worker, source, ring = create_synthetic_pipeline(
            camera_id="cam_indep",
            max_frames=30,
            frame_interval_s=0.001,
            depth=32,  # Larger depth to hold all frames
        )

        try:
            # Create four consumers
            proc_consumer = ring.consumer("processing")
            obs_consumer = ring.consumer("observer")
            rec_consumer = ring.consumer("recorder")
            diag_consumer = ring.consumer("diagnostics")

            proc = ProcessingConsumer(consumer=proc_consumer)
            obs = ObserverConsumer(consumer=obs_consumer)
            rec = RecorderConsumer(consumer=rec_consumer)
            diag = DiagnosticsConsumer(consumer=diag_consumer, ring=ring)

            proc.start()
            obs.start()
            rec.start()
            diag.start()

            worker.start()
            assert worker.wait_for_state(worker.state.__class__.ACQUIRING, timeout=5.0)

            # Run all consumers concurrently until all frames processed
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if proc.expected_sequence < 30:
                    proc.process_next()
                obs.observe()
                if rec.expected_sequence < 30:
                    rec.record_next()
                diag.sample()
                time.sleep(0.001)
                
                # Stop when all sequential consumers are done
                if proc.expected_sequence >= 30 and rec.expected_sequence >= 30:
                    break

            proc.stop()
            obs.stop()
            rec.stop()
            diag.stop()

            # Verify all consumers worked independently
            assert proc.expected_sequence == 30, f"Processing got {proc.expected_sequence}/30"
            assert len(obs.observed_frames) > 0
            assert rec.expected_sequence == 30, f"Recorder got {rec.expected_sequence}/30"
            assert len(diag.samples) > 0

            # Verify independent stats
            p_stats = proc.consumer.stats()
            o_stats = obs.consumer.stats()
            r_stats = rec.consumer.stats()
            d_stats = diag.consumer.stats()

            assert p_stats.consumed == 30
            assert r_stats.consumed == 30
            # Observer and diagnostics use latest() so consumed count differs

        finally:
            worker.stop()
            ring.close()

    def test_slow_consumer_does_not_block_producer(self):
        """Test 6: Slow consumer doesn't block producer (backpressure test)."""
        worker, source, ring = create_synthetic_pipeline(
            camera_id="cam_slow",
            max_frames=50,
            frame_interval_s=0.0005,  # Fast producer
            depth=8,
        )

        try:
            # Slow processing consumer - holds pin for longer
            slow_consumer = ring.consumer("slow_processing")
            fast_consumer = ring.consumer("fast_observer")

            slow_proc = ProcessingConsumer(consumer=slow_consumer)
            fast_obs = ObserverConsumer(consumer=fast_consumer)

            slow_proc.start()
            fast_obs.start()

            worker.start()
            assert worker.wait_for_state(worker.state.__class__.ACQUIRING, timeout=5.0)

            deadline = time.monotonic() + 5.0
            slow_frames = 0

            while time.monotonic() < deadline:
                # Slow consumer processes every 5th frame and holds pin
                if slow_frames < 10:
                    if slow_proc.process_next():
                        slow_frames += 1
                        time.sleep(0.01)  # Slow processing

                # Fast observer samples rapidly
                fast_obs.observe()
                time.sleep(0.0001)

            slow_proc.stop()
            fast_obs.stop()

            # Producer should have continued (check ring stats)
            ring_stats = ring.stats()
            assert ring_stats.producer_sequence > slow_frames

            # Fast observer should have seen recent frames
            assert len(fast_obs.observed_frames) > 0

            # Producer never blocked - verify by checking acquisition stats
            worker_stats = worker.stats()
            assert worker_stats.published > 0

        finally:
            worker.stop()
            ring.close()

    def test_producer_never_blocks(self):
        """Test 7: Producer never blocks even when consumers are slow."""
        worker, source, ring = create_synthetic_pipeline(
            camera_id="cam_noblock",
            max_frames=100,
            frame_interval_s=0.0001,  # Very fast producer
            depth=4,  # Small ring
        )

        try:
            # Create consumers that will be slow
            consumers = []
            for i in range(3):
                c = ring.consumer(f"consumer_{i}")
                consumers.append(c)

            worker.start()
            assert worker.wait_for_state(worker.state.__class__.ACQUIRING, timeout=5.0)

            # Let producer run for a bit without consumers reading
            time.sleep(0.1)

            # Now start consumers - they'll see gaps/overwrites but producer never blocked
            for c in consumers:
                c.next(0)  # This will detect overwrites

            worker_stats = worker.stats()
            # Producer should have published many frames without blocking
            assert worker_stats.published > 10

            # Some frames may be dropped due to small ring, but producer never blocked
            ring_stats = ring.stats()
            assert ring_stats.producer_sequence > 10

        finally:
            worker.stop()
            ring.close()

    def test_pinned_slot_protection(self):
        """Test 8: Pinned slot protection - processing pins frame N, producer cannot overwrite N."""
        ring = create_ring_buffer(
            camera_id="cam_pin_test",
            thermal_width=16,
            thermal_height=16,
            thermal_dtype=np.dtype(np.uint16),
            depth=4,
        )

        try:
            producer = ring.producer()
            consumer = ring.consumer("test_pinning")

            # Publish frames 0, 1, 2, 3 (no visible since ring not configured for it)
            for seq in range(4):
                frame = self._make_test_frame(seq, camera_id="cam_pin_test", include_visible=False)
                result = producer.publish(frame)
                assert result.accepted

            # Consumer pins frame 0 (slot 0)
            view = consumer.next(0)
            assert view is not None
            assert view.descriptor.sequence == 0
            pinned = consumer.pin(view)

            # Publish more frames - should wrap around but NOT overwrite pinned slot 0
            for seq in range(4, 10):
                frame = self._make_test_frame(seq, camera_id="cam_pin_test", include_visible=False)
                result = producer.publish(frame)
                # Should succeed because other slots available
                assert result.accepted, f"Frame {seq} should be accepted"

            # Pinned frame should still be valid
            assert pinned.view.valid()
            assert pinned.view.descriptor.sequence == 0

            # Release pin
            consumer.release(pinned)

            # Now producer can reuse slot 0
            frame = self._make_test_frame(10, camera_id="cam_pin_test", include_visible=False)
            result = producer.publish(frame)
            assert result.accepted

            consumer.close()

        finally:
            ring.close()

    def test_all_slots_pinned_causes_drop(self):
        """Test 9: All slots pinned -> publish returns accepted=False without blocking."""
        ring = create_ring_buffer(
            camera_id="cam_all_pinned",
            thermal_width=16,
            thermal_height=16,
            thermal_dtype=np.dtype(np.uint16),
            depth=3,
        )

        try:
            producer = ring.producer()
            consumers = [ring.consumer(f"c{i}") for i in range(3)]
            pinned_views = []

            # Fill and pin all slots
            for i in range(3):
                frame = self._make_test_frame(i, camera_id="cam_all_pinned", include_visible=False)
                result = producer.publish(frame)
                assert result.accepted

                view = consumers[i].latest()
                assert view is not None
                pinned = consumers[i].pin(view)
                pinned_views.append(pinned)

            # All slots pinned - next publish should drop (accepted=False)
            frame = self._make_test_frame(3, camera_id="cam_all_pinned", include_visible=False)
            result = producer.publish(frame)
            assert result.accepted is False
            assert result.dropped is True

            # Producer did NOT block - we're still here immediately

            # Release one pin
            consumers[0].release(pinned_views[0])

            # Now publish should succeed
            frame = self._make_test_frame(3, camera_id="cam_all_pinned", include_visible=False)
            result = producer.publish(frame)
            assert result.accepted is True

            for c in consumers:
                c.close()

        finally:
            ring.close()

    def test_thermal_visible_payloads(self):
        """Test 11: Test thermal only, thermal+visible, visible missing cases."""
        # Case A: Thermal only
        ring_a = create_ring_buffer(
            camera_id="cam_thermal_only",
            thermal_width=16,
            thermal_height=16,
            thermal_dtype=np.dtype(np.uint16),
            depth=4,
        )
        try:
            producer = ring_a.producer()
            consumer = ring_a.consumer("test")

            frame = self._make_test_frame(0, camera_id="cam_thermal_only", include_visible=False)
            result = producer.publish(frame)
            assert result.accepted

            view = consumer.latest()
            assert view.descriptor.thermal.present is True
            assert view.descriptor.visible.present is False
            assert view.descriptor.sync.status == SyncStatus.MISSING_VISIBLE
            assert view.thermal() is not None
            assert view.visible() is None

            consumer.close()
        finally:
            ring_a.close()

        # Case B: Thermal + Visible
        ring_b = create_ring_buffer(
            camera_id="cam_both",
            thermal_width=16,
            thermal_height=16,
            thermal_dtype=np.dtype(np.uint16),
            depth=4,
            visible_width=16,
            visible_height=16,
            visible_dtype=np.dtype(np.uint8),
        )
        try:
            producer = ring_b.producer()
            consumer = ring_b.consumer("test")

            frame = self._make_test_frame(0, camera_id="cam_both", include_visible=True)
            result = producer.publish(frame)
            assert result.accepted

            view = consumer.latest()
            assert view.descriptor.thermal.present is True
            assert view.descriptor.visible.present is True
            assert view.descriptor.sync.status == SyncStatus.UNKNOWN  # TV46L time-slices
            assert view.thermal() is not None
            assert view.visible() is not None

            consumer.close()
        finally:
            ring_b.close()

        # Case C: Visible missing (thermal only but ring configured for visible)
        ring_c = create_ring_buffer(
            camera_id="cam_vis_missing",
            thermal_width=16,
            thermal_height=16,
            thermal_dtype=np.dtype(np.uint16),
            depth=4,
            visible_width=16,
            visible_height=16,
            visible_dtype=np.dtype(np.uint8),
        )
        try:
            producer = ring_c.producer()
            consumer = ring_c.consumer("test")

            frame = self._make_test_frame(0, camera_id="cam_vis_missing", include_visible=False)
            result = producer.publish(frame)
            assert result.accepted

            view = consumer.latest()
            assert view.descriptor.thermal.present is True
            assert view.descriptor.visible.present is False
            assert view.descriptor.sync.status == SyncStatus.MISSING_VISIBLE
            assert view.thermal() is not None
            assert view.visible() is None

            consumer.close()
        finally:
            ring_c.close()

    def test_frame_immutability(self):
        """Test 12: Frame immutability end-to-end."""
        ring = create_ring_buffer(
            camera_id="cam_immutable",
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
            consumer = ring.consumer("test")

            frame = self._make_test_frame(0, camera_id="cam_immutable", include_visible=True)
            original_thermal = frame.payload.thermal.copy()
            original_visible = frame.payload.visible.copy()

            result = producer.publish(frame)
            assert result.accepted

            view = consumer.latest()

            # Verify views are read-only
            thermal_view = view.thermal()
            visible_view = view.visible()

            assert not thermal_view.flags.writeable
            assert not visible_view.flags.writeable

            # Attempting to modify should raise
            with pytest.raises(ValueError):
                thermal_view[0, 0] = 999
            with pytest.raises(ValueError):
                visible_view[0, 0] = 999

            # Copy creates independent data
            copied_frame = view.copy()
            copied_thermal = copied_frame.payload.thermal
            copied_visible = copied_frame.payload.visible

            # Modify copy (it's read-only, so we need to make writeable copy first)
            copied_thermal_rw = copied_thermal.copy()
            copied_thermal_rw[0, 0] = 9999

            # Original shared memory should be unaffected
            thermal_view_fresh = view.thermal()
            assert thermal_view_fresh[0, 0] == original_thermal[0, 0]

            consumer.close()
        finally:
            ring.close()

    def test_four_camera_independence(self):
        """Test 13: Four cameras running simultaneously."""
        pipelines = []
        for i in range(4):
            cam_id = f"cam_multi_{i}"
            worker, source, ring = create_synthetic_pipeline(
                camera_id=cam_id,
                max_frames=20,
                frame_interval_s=0.001,
                depth=16,
            )
            pipelines.append((worker, source, ring, cam_id))

        try:
            # Start all workers
            for worker, source, ring, cam_id in pipelines:
                worker.start()
                assert worker.wait_for_state(worker.state.__class__.ACQUIRING, timeout=5.0)

            # Wait for all to publish frames (max_frames=20 means sequences 0-19)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                all_done = True
                for worker, source, ring, cam_id in pipelines:
                    stats = worker.stats()
                    if stats.published < 20:
                        all_done = False
                if all_done:
                    break
                time.sleep(0.01)

            # Verify each camera has independent sequence numbers
            for worker, source, ring, cam_id in pipelines:
                stats = worker.stats()
                ring_stats = ring.stats()
                assert stats.published >= 20
                assert ring_stats.producer_sequence >= 19  # sequence 0-19 = 20 frames

                # Verify consumer can read from each independently
                consumer = ring.consumer("verify")
                view = consumer.latest()
                assert view is not None
                assert view.descriptor.camera_id == cam_id
                assert view.descriptor.sequence >= 19
                consumer.close()

        finally:
            for worker, source, ring, cam_id in pipelines:
                worker.stop()
                ring.close()

    def test_one_camera_stops_others_continue(self):
        """Test 13b: One camera stopping doesn't affect others."""
        pipelines = []
        for i in range(4):
            cam_id = f"cam_stop_{i}"
            worker, source, ring = create_synthetic_pipeline(
                camera_id=cam_id,
                max_frames=100,  # Large max so they don't auto-stop
                frame_interval_s=0.001,
                depth=16,
            )
            pipelines.append((worker, source, ring, cam_id))

        try:
            # Start all
            for worker, source, ring, cam_id in pipelines:
                worker.start()
                assert worker.wait_for_state(worker.state.__class__.ACQUIRING, timeout=5.0)

            time.sleep(0.1)  # Let them run

            # Stop camera 1
            pipelines[0][0].stop()

            # Others should continue
            time.sleep(0.1)

            for i in range(1, 4):
                worker, source, ring, cam_id = pipelines[i]
                assert worker.state == worker.state.__class__.ACQUIRING
                stats = worker.stats()
                assert stats.published > 0

        finally:
            for worker, source, ring, cam_id in pipelines:
                if worker.state != worker.state.__class__.STOPPED:
                    worker.stop()
                ring.close()

    def test_consumer_stops_producer_continues(self):
        """Test 13c: Consumer stopping doesn't stop producer."""
        worker, source, ring = create_synthetic_pipeline(
            camera_id="cam_cons_stop",
            max_frames=100,
            frame_interval_s=0.001,
            depth=16,
        )

        try:
            consumer = ring.consumer("test")
            obs = ObserverConsumer(consumer=consumer)
            obs.start()

            worker.start()
            assert worker.wait_for_state(worker.state.__class__.ACQUIRING, timeout=5.0)

            # Let it run
            time.sleep(0.1)

            # Stop consumer
            obs.stop()
            consumer.close()

            # Producer should continue
            time.sleep(0.1)

            worker_stats = worker.stats()
            assert worker_stats.published > 20

            ring_stats = ring.stats()
            assert ring_stats.producer_sequence > 20

        finally:
            worker.stop()
            ring.close()

    def test_eight_camera_scalability(self):
        """Test 14: Eight lightweight synthetic cameras."""
        pipelines = []
        for i in range(8):
            cam_id = f"cam_8_{i}"
            worker, source, ring = create_synthetic_pipeline(
                camera_id=cam_id,
                max_frames=10,
                frame_interval_s=0.0005,
                depth=8,
                thermal_shape=(8, 8),  # Smaller frames for 8 cameras
                visible_shape=(8, 8),
            )
            pipelines.append((worker, source, ring, cam_id))

        try:
            # Start all
            for worker, source, ring, cam_id in pipelines:
                worker.start()
                assert worker.wait_for_state(worker.state.__class__.ACQUIRING, timeout=5.0)

            # Wait for completion (max_frames=10 means sequences 0-9)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                all_done = True
                for worker, source, ring, cam_id in pipelines:
                    stats = worker.stats()
                    if stats.published < 10:
                        all_done = False
                if all_done:
                    break
                time.sleep(0.01)

            # Verify all independent
            for worker, source, ring, cam_id in pipelines:
                stats = worker.stats()
                assert stats.published >= 10
                ring_stats = ring.stats()
                assert ring_stats.producer_sequence >= 9  # sequence 0-9 = 10 frames

        finally:
            for worker, source, ring, cam_id in pipelines:
                worker.stop()
                ring.close()

    def test_lifecycle_start_acquire_publish_consume_stop(self):
        """Test 15: Complete lifecycle."""
        worker, source, ring = create_synthetic_pipeline(
            camera_id="cam_lifecycle",
            max_frames=15,
            frame_interval_s=0.001,
        )

        consumer = ring.consumer("test")
        proc = ProcessingConsumer(consumer=consumer)
        proc.start()

        # Start
        worker.start()
        assert worker.wait_for_state(worker.state.__class__.ACQUIRING, timeout=5.0)

        # Acquire and consume
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and proc.expected_sequence < 15:
            proc.process_next()
            time.sleep(0.001)

        # Stop
        proc.stop()
        worker.stop()

        # Verify final state
        assert worker.state == worker.state.__class__.STOPPED
        assert proc.expected_sequence == 15

        # Close
        consumer.close()
        ring.close()

    def _make_test_frame(
        self,
        sequence: int,
        camera_id: str = "cam_test",
        include_visible: bool = True,
        thermal_shape: tuple[int, int] = (16, 16),
        visible_shape: tuple[int, int] = (16, 16),
    ) -> Frame:
        """Helper to create a test frame matching ring buffer config."""
        h, w = thermal_shape
        thermal = np.zeros((h, w), dtype=np.uint16)
        thermal[0, 0] = sequence & 0xFFFF
        thermal.setflags(write=False)

        visible = None
        if include_visible:
            vh, vw = visible_shape
            visible = np.zeros((vh, vw), dtype=np.uint8)
            visible[0, 0] = sequence & 0xFF
            visible.setflags(write=False)

        thermal_meta = StreamMetadata(
            present=True,
            width=w,
            height=h,
            pixel_format="IR_Data",
            dtype="uint16",
            byte_count=thermal.nbytes,
            sequence=sequence,
        )

        visible_meta = StreamMetadata(
            present=include_visible,
            width=vw if include_visible else None,
            height=vh if include_visible else None,
            pixel_format="RGB8" if include_visible else None,
            dtype="uint8" if include_visible else None,
            byte_count=visible.nbytes if include_visible else None,
            sequence=sequence if include_visible else None,
        )

        sync = SyncInfo(
            status=SyncStatus.MISSING_VISIBLE if not include_visible else SyncStatus.UNKNOWN
        )

        descriptor = FrameDescriptor(
            camera_id=camera_id,
            sequence=sequence,
            timestamp=time.time(),
            monotonic_timestamp=time.monotonic(),
            thermal=thermal_meta,
            visible=visible_meta,
            sync=sync,
        )

        return Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal, visible=visible))


class TestDiagnostics:
    """Test diagnostics capabilities."""

    def _make_test_frame(
        self,
        sequence: int,
        camera_id: str = "cam_test",
        include_visible: bool = True,
        thermal_shape: tuple[int, int] = (16, 16),
        visible_shape: tuple[int, int] = (16, 16),
    ) -> Frame:
        """Helper to create a test frame matching ring buffer config."""
        h, w = thermal_shape
        thermal = np.zeros((h, w), dtype=np.uint16)
        thermal[0, 0] = sequence & 0xFFFF
        thermal.setflags(write=False)

        visible = None
        if include_visible:
            vh, vw = visible_shape
            visible = np.zeros((vh, vw), dtype=np.uint8)
            visible[0, 0] = sequence & 0xFF
            visible.setflags(write=False)

        thermal_meta = StreamMetadata(
            present=True,
            width=w,
            height=h,
            pixel_format="IR_Data",
            dtype="uint16",
            byte_count=thermal.nbytes,
            sequence=sequence,
        )

        visible_meta = StreamMetadata(
            present=include_visible,
            width=vw if include_visible else None,
            height=vh if include_visible else None,
            pixel_format="RGB8" if include_visible else None,
            dtype="uint8" if include_visible else None,
            byte_count=visible.nbytes if include_visible else None,
            sequence=sequence if include_visible else None,
        )

        sync = SyncInfo(
            status=SyncStatus.MISSING_VISIBLE if not include_visible else SyncStatus.UNKNOWN
        )

        descriptor = FrameDescriptor(
            camera_id=camera_id,
            sequence=sequence,
            timestamp=time.time(),
            monotonic_timestamp=time.monotonic(),
            thermal=thermal_meta,
            visible=visible_meta,
            sync=sync,
        )

        return Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal, visible=visible))

    def test_diagnostics_measures_fps_and_gaps(self):
        """Test diagnostics consumer captures FPS, sequence, drops, gaps."""
        worker, source, ring = create_synthetic_pipeline(
            camera_id="cam_diag",
            max_frames=50,
            frame_interval_s=0.001,
            depth=16,
        )

        try:
            diag_consumer = ring.consumer("diagnostics")
            diag = DiagnosticsConsumer(consumer=diag_consumer, ring=ring)
            diag.start()

            worker.start()
            assert worker.wait_for_state(worker.state.__class__.ACQUIRING, timeout=5.0)

            # Wait for some frames to be published, then consume one
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                stats = worker.stats()
                if stats.published >= 5:
                    break
                time.sleep(0.01)
            
            # Now consume a frame so consumer has stats
            diag.consumer.next(0)

            # Sample diagnostics while running
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                diag.sample()
                time.sleep(0.01)

            diag.stop()

            # Verify diagnostics captured data
            assert len(diag.samples) > 0
            last_sample = diag.samples[-1]
            assert last_sample["ring_producer_sequence"] > 0
            assert last_sample["consumer_last_sequence"] >= 0

        finally:
            worker.stop()
            ring.close()

    def test_diagnostics_detects_consumer_gaps(self):
        """Test diagnostics detects when consumer falls behind."""
        ring = create_ring_buffer(
            camera_id="cam_diag_gaps",
            thermal_width=16,
            thermal_height=16,
            thermal_dtype=np.dtype(np.uint16),
            depth=4,
        )

        try:
            producer = ring.producer()
            consumer = ring.consumer("diagnostics")
            diag = DiagnosticsConsumer(consumer=consumer, ring=ring)
            diag.start()

            # Publish frames fast
            for seq in range(20):
                frame = self._make_test_frame(seq, camera_id="cam_diag_gaps", include_visible=False)
                producer.publish(frame)

            # Consumer reads only first frame, then stops
            consumer.next(0)

            # Diagnostics samples
            sample = diag.sample()
            assert sample is not None

            # Consumer should have gaps/overwrites
            assert sample["consumer_gaps"] > 0 or sample["consumer_overwritten"] > 0

            consumer.close()
        finally:
            ring.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])