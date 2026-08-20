"""Tests for ProcessingConsumer (Stage 7D): SHM ring -> processing pipeline.

Covers the complete consumer-side path:
Acquisition -> SharedMemoryRingBuffer -> ProcessingConsumer -> ProcessingResult

All tests use synthetic frames; no TV46L hardware or HALCON is required.
"""

from __future__ import annotations

import time
import uuid

import numpy as np
import pytest

from thermal_monitor.camera.acquisition import AcquisitionWorker, AcquisitionState
from thermal_monitor.camera.model import CameraConfig, CameraIdentity, GrabResult
from thermal_monitor.camera.shm import create_ring_buffer_and_publisher
from thermal_monitor.core.frame import (
    Frame,
    FrameDescriptor,
    FramePayload,
    StreamMetadata,
    SyncInfo,
    SyncStatus,
)
from thermal_monitor.core.models import AnalysisConfig
from thermal_monitor.processing.alarms import NullAlarmEvaluator
from thermal_monitor.processing.consumer import (
    ProcessingConsumer,
    ProcessingConsumerStats,
    create_processing_consumer,
)
from thermal_monitor.processing.pipeline import SimpleProcessingPipeline
from thermal_monitor.processing.temperature import CPUTemperatureConverter
from thermal_monitor.services.recording_consumer import create_recording_consumer
from thermal_monitor.storage.recording import RecordingWriteMetadata


# ─── Helpers ────────────────────────────────────────────────────────────────────

def unique_camera(prefix: str) -> str:
    """Return a unique camera id to avoid shared-memory collisions."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def make_thermal_frame(camera_id: str, sequence: int, width: int = 16, height: int = 16) -> Frame:
    """Create a test frame with a known thermal payload pattern."""
    thermal = np.arange(sequence, sequence + width * height, dtype=np.uint16).reshape(height, width)
    thermal.setflags(write=False)

    thermal_meta = StreamMetadata(
        present=True,
        width=width,
        height=height,
        pixel_format="IR_Data",
        dtype="uint16",
        byte_count=thermal.nbytes,
        sequence=sequence * 1000,  # hardware frame_id
        timestamp=1000.0 + sequence * 0.111,
        monotonic_timestamp=100.0 + sequence * 0.111,
        hardware_timestamp=1000.0 + sequence * 0.111,
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


def make_analysis_config(camera_id: str) -> AnalysisConfig:
    """Analysis config with no ROIs (skips HALCON entirely)."""
    return AnalysisConfig(camera_id=camera_id)


class FixedCalibrationProvider:
    """Calibration provider returning a known LUT: temp == raw value."""

    def __init__(self, lut: np.ndarray | None = None) -> None:
        if lut is None:
            lut = np.arange(65536, dtype=np.float32)
        self._lut = lut

    def get_calibration(self, camera_id: str) -> np.ndarray:
        return self._lut


class ScriptedSource:
    """Simple continuous FrameSource for acquisition worker tests."""

    def __init__(self, frame_interval_s: float = 0.01) -> None:
        self._interval = frame_interval_s
        self._seq = 0
        self._last = 0.0
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.grab_calls = 0

    def connect(self) -> None:
        self.connect_calls += 1

    def disconnect(self) -> None:
        self.disconnect_calls += 1

    def is_connected(self) -> bool:
        return True

    def reopen(self) -> None:
        pass

    def grab(self, timeout_ms: int) -> GrabResult:
        self.grab_calls += 1
        now = time.perf_counter()
        if self._interval > 0:
            elapsed = now - self._last
            if elapsed < self._interval:
                time.sleep(self._interval - elapsed)
            self._last = time.perf_counter()

        array = np.arange(16 * 16, dtype=np.uint16).reshape(16, 16)
        array.setflags(write=False)
        self._seq += 1
        return GrabResult(
            thermal=array,
            thermal_format="IR_Data",
            hardware_timestamp=time.time(),
            frame_id=self._seq,
            packet_stats={"packets_seen": 0, "packets_lost": 0, "blocks_incomplete": 0, "blocks_discarded": 0},
            grab_started=time.perf_counter(),
            grab_completed=time.perf_counter(),
            converted_at=time.perf_counter(),
        )


class FailingPipeline(SimpleProcessingPipeline):
    """Pipeline that always raises to simulate processing failure."""

    def process_frame(self, frame: Frame):
        raise RuntimeError("simulated processing failure")


class SlowPipeline(SimpleProcessingPipeline):
    """Pipeline that sleeps per frame to simulate a slow consumer."""

    def __init__(self, config: AnalysisConfig, delay_s: float = 0.02) -> None:
        super().__init__(config)
        self._delay_s = delay_s

    def process_frame(self, frame: Frame):
        time.sleep(self._delay_s)
        return super().process_frame(frame)


def _make_camera_config(camera_id: str) -> CameraConfig:
    return CameraConfig(
        identity=CameraIdentity(
            camera_id=camera_id,
            serial_number="SN12345",
            model="TV46L-1-26010003@9Hz",
            vendor="Fluke Process Instruments",
        ),
        device_identifier="test_device_identifier",
        grab_timeout_ms=100,
        consecutive_fail_limit=2,
        reconnect_interval_s=0.01,
        reconnect_backoff_factor=1.0,
        max_reconnect_attempts=3,
    )


# ─── Tests ──────────────────────────────────────────────────────────────────────

class TestProcessingConsumerEndToEnd:
    """SHM ring -> ProcessingConsumer -> ProcessingResult."""

    def test_shm_to_processing_consumer(self):
        """End-to-end: publish frame to ring, consumer produces a ProcessingResult."""
        camera_id = unique_camera("cam_e2e")
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=8)
        results = []

        ring2, consumer = create_processing_consumer(
            camera_id=camera_id,
            analysis_config=make_analysis_config(camera_id),
            calibration_provider=FixedCalibrationProvider(),
            alarm_evaluator=NullAlarmEvaluator(),
            result_callback=results.append,
            ring_depth=8,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()
            assert consumer.is_running

            result = publisher.publish(make_thermal_frame(camera_id, 0))
            assert result.accepted is True

            assert consumer.wait_for_frames(1, timeout=3.0)
            consumer.stop()

            assert len(results) == 1
            processing = results[0]
            assert processing.frame.descriptor.sequence == 0
            assert processing.analysis_result.camera_id == camera_id
            assert processing.alarm_result is not None
            assert processing.alarm_result.frame_sequence == 0
            assert processing.processing_time_ms >= 0
            # Pipeline has a converter -> temperature image exposed
            assert processing.temperature_image is not None
            assert processing.temperature_image.shape == (16, 16)
            assert processing.temperature_image.dtype == np.float32
        finally:
            consumer.close()
            ring2.close()
            ring.close()

    def test_raw_uint16_frame_reaches_processing_unchanged(self):
        """Raw uint16 thermal data is byte-for-byte unchanged at the consumer."""
        camera_id = unique_camera("cam_raw")
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=8)
        results = []

        pipeline = SimpleProcessingPipeline(config=make_analysis_config(camera_id))
        consumer = ProcessingConsumer(
            camera_id=camera_id,
            ring_buffer=ring,
            consumer_name=f"processing_{camera_id}",
            pipeline=pipeline,
            alarm_evaluator=NullAlarmEvaluator(),
            result_callback=results.append,
        )

        try:
            consumer.start()

            original = np.arange(256, dtype=np.uint16).reshape(16, 16)
            original.setflags(write=False)
            frame = Frame(
                descriptor=make_thermal_frame(camera_id, 1).descriptor,
                payload=FramePayload(thermal=original, visible=None),
            )

            publisher.publish(frame)
            assert consumer.wait_for_frames(1, timeout=3.0)
            consumer.stop()

            received = results[0].frame.payload.thermal
            np.testing.assert_array_equal(received, original)
            assert received.dtype == np.uint16
            assert not received.flags.writeable
            # No converter configured -> no temperature image
            assert results[0].temperature_image is None
        finally:
            consumer.close()
            ring.close()

    def test_temperature_conversion_produces_expected_values(self):
        """CPU temperature conversion produces expected values (temp == raw)."""
        camera_id = unique_camera("cam_temp")
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=8)
        results = []

        ring2, consumer = create_processing_consumer(
            camera_id=camera_id,
            analysis_config=make_analysis_config(camera_id),
            calibration_provider=FixedCalibrationProvider(),
            temperature_converter=CPUTemperatureConverter(),
            alarm_evaluator=NullAlarmEvaluator(),
            result_callback=results.append,
            ring_depth=8,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()

            raw = np.arange(0, 256, dtype=np.uint16).reshape(16, 16)
            raw.setflags(write=False)
            frame = Frame(
                descriptor=make_thermal_frame(camera_id, 2).descriptor,
                payload=FramePayload(thermal=raw, visible=None),
            )
            publisher.publish(frame)

            assert consumer.wait_for_frames(1, timeout=3.0)
            consumer.stop()

            temp_image = results[0].temperature_image
            assert temp_image is not None
            # LUT maps raw -> temperature 1:1
            np.testing.assert_array_equal(temp_image, raw.astype(np.float32))
            assert temp_image[0, 0] == 0.0
            assert temp_image[3, 5] == float(raw[3, 5])
            assert temp_image[15, 15] == 255.0
        finally:
            consumer.close()
            ring2.close()
            ring.close()

    def test_multiple_frames_processed_in_sequence(self):
        """Multiple frames are processed in publish order with correct sequences."""
        camera_id = unique_camera("cam_seq")
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=16)
        results = []

        ring2, consumer = create_processing_consumer(
            camera_id=camera_id,
            analysis_config=make_analysis_config(camera_id),
            calibration_provider=FixedCalibrationProvider(),
            alarm_evaluator=NullAlarmEvaluator(),
            result_callback=results.append,
            ring_depth=16,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()

            for seq in range(10):
                result = publisher.publish(make_thermal_frame(camera_id, seq))
                assert result.accepted is True

            assert consumer.wait_for_frames(10, timeout=5.0)
            consumer.stop()

            assert len(results) == 10
            sequences = [r.frame.descriptor.sequence for r in results]
            assert sequences == list(range(10))
            # Each frame's own payload reached the pipeline
            for r in results:
                seq = r.frame.descriptor.sequence
                np.testing.assert_array_equal(
                    r.frame.payload.thermal,
                    make_thermal_frame(camera_id, seq).payload.thermal,
                )
        finally:
            consumer.close()
            ring2.close()
            ring.close()

    def test_camera_id_preserved(self):
        """Camera identity survives ring transit into processing results."""
        camera_id = unique_camera("cam_camid")
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=8)
        results = []

        ring2, consumer = create_processing_consumer(
            camera_id=camera_id,
            analysis_config=make_analysis_config(camera_id),
            calibration_provider=FixedCalibrationProvider(),
            alarm_evaluator=NullAlarmEvaluator(),
            result_callback=results.append,
            ring_depth=8,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()
            publisher.publish(make_thermal_frame(camera_id, 0))
            assert consumer.wait_for_frames(1, timeout=3.0)
            consumer.stop()

            processing = results[0]
            assert processing.frame.descriptor.camera_id == camera_id
            assert processing.analysis_result.camera_id == camera_id
            assert consumer.stats().last_camera_id == camera_id
        finally:
            consumer.close()
            ring2.close()
            ring.close()


class TestProcessingConsumerRobustness:
    """Non-blocking behavior, restart, failure isolation, stats."""

    def test_processing_consumer_does_not_block_producer(self):
        """Producer publishes without waiting for a slow processing consumer."""
        camera_id = unique_camera("cam_noblock")
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=32)
        results = []

        pipeline = SlowPipeline(config=make_analysis_config(camera_id), delay_s=0.02)
        consumer = ProcessingConsumer(
            camera_id=camera_id,
            ring_buffer=ring,
            consumer_name=f"processing_{camera_id}",
            pipeline=pipeline,
            alarm_evaluator=NullAlarmEvaluator(),
            result_callback=results.append,
        )

        try:
            consumer.start()

            # Prove the consumer can keep up initially: process 2 frames
            publisher.publish(make_thermal_frame(camera_id, 0))
            publisher.publish(make_thermal_frame(camera_id, 1))
            assert consumer.wait_for_frames(2, timeout=5.0)

            # Now burst-publish 20 frames while the consumer is slow (~0.02s/frame)
            start = time.perf_counter()
            accepted = 0
            for seq in range(2, 22):
                result = publisher.publish(make_thermal_frame(camera_id, seq))
                if result.accepted:
                    accepted += 1
            elapsed = time.perf_counter() - start

            # Producer never blocked (would take ~0.4s+ if serialized with consumer)
            assert elapsed < 0.5, f"Producer blocked for {elapsed:.3f}s"
            assert accepted == 20

            consumer.stop()
            # Slow consumer could not keep up with the burst
            assert consumer.stats().frames_processed < 22
            assert consumer.stats().frames_consumed >= 2
        finally:
            consumer.close()
            ring.close()

    def test_consumer_restart(self):
        """Consumer can stop and restart and continue processing frames."""
        camera_id = unique_camera("cam_restart")
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=8)
        results = []

        ring2, consumer = create_processing_consumer(
            camera_id=camera_id,
            analysis_config=make_analysis_config(camera_id),
            calibration_provider=FixedCalibrationProvider(),
            alarm_evaluator=NullAlarmEvaluator(),
            result_callback=results.append,
            ring_depth=8,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            # Publish frames before first start
            for seq in range(6):
                publisher.publish(make_thermal_frame(camera_id, seq))

            consumer.start()
            assert consumer.wait_for_frames(6, timeout=5.0)
            consumer.stop()
            assert not consumer.is_running
            assert consumer.stats().frames_processed == 6

            # Publish more frames after stop
            for seq in range(6, 8):
                publisher.publish(make_thermal_frame(camera_id, seq))

            # Restart and re-consume from the ring (fresh expected sequence)
            consumer.restart(timeout=3.0)
            assert consumer.is_running
            assert consumer.wait_for_frames(8, timeout=5.0)
            consumer.stop()

            # Restart re-reads frames still in the ring, so 8 more are processed
            assert consumer.stats().frames_processed == 14
            assert consumer.stats().last_sequence == 7
            sequences = [r.frame.descriptor.sequence for r in results]
            assert set(sequences) >= {6, 7}
        finally:
            consumer.close()
            ring2.close()
            ring.close()

    def test_processing_statistics_correct(self):
        """Consumer statistics reflect exactly what was consumed and processed."""
        camera_id = unique_camera("cam_stats")
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=16)
        results = []

        ring2, consumer = create_processing_consumer(
            camera_id=camera_id,
            analysis_config=make_analysis_config(camera_id),
            calibration_provider=FixedCalibrationProvider(),
            alarm_evaluator=NullAlarmEvaluator(),
            result_callback=results.append,
            ring_depth=16,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            consumer.start()
            for seq in range(5):
                publisher.publish(make_thermal_frame(camera_id, seq))
            assert consumer.wait_for_frames(5, timeout=5.0)
            consumer.stop()

            stats = consumer.stats()
            assert stats.frames_consumed == 5
            assert stats.frames_processed == 5
            assert stats.frames_failed == 0
            assert stats.errors == 0
            assert stats.last_sequence == 4
            assert stats.last_camera_id == camera_id
            assert stats.last_processed_at is not None
            assert stats.total_processing_time_ms > 0
            assert stats.average_processing_time_ms > 0
        finally:
            consumer.close()
            ring2.close()
            ring.close()

    def test_processing_failure_does_not_kill_acquisition(self):
        """A failing pipeline never stops the acquisition producer."""
        camera_id = unique_camera("cam_fail")
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=8)

        config = make_analysis_config(camera_id)
        pipeline = FailingPipeline(config)
        consumer = ProcessingConsumer(
            camera_id=camera_id,
            ring_buffer=ring,
            consumer_name=f"processing_{camera_id}",
            pipeline=pipeline,
            alarm_evaluator=NullAlarmEvaluator(),
        )

        source = ScriptedSource(frame_interval_s=0.005)
        worker = AcquisitionWorker(camera_id, source, publisher, _make_camera_config(camera_id))

        try:
            consumer.start()
            worker.start()
            assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=5.0)

            # Wait until several frames have been consumed-and-failed
            deadline = time.monotonic() + 5.0
            while consumer.stats().frames_consumed < 3 and time.monotonic() < deadline:
                time.sleep(0.01)

            assert consumer.stats().frames_consumed >= 3
            assert consumer.stats().frames_failed >= 1
            assert consumer.stats().errors >= 1

            # Acquisition is still alive and keeps publishing after failures
            assert worker.state is AcquisitionState.ACQUIRING
            published_before = worker.stats().published
            deadline = time.monotonic() + 3.0
            while worker.stats().published < published_before + 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert worker.stats().published >= published_before + 2
        finally:
            worker.stop(timeout=5.0)
            consumer.stop(timeout=3.0)
            consumer.close()
            ring.close()

    def test_recording_and_processing_independent(self, tmp_path):
        """Recording and processing consume the same SHM producer independently."""
        camera_id = unique_camera("cam_both")
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=16)

        recording_metadata = RecordingWriteMetadata(
            recording_id="rec_both",
            cameras=[camera_id],
            streams={camera_id: ["IR"]},
            camera_snapshots=[{"camera_id": camera_id}],
        )

        ring_rec, recording_consumer = create_recording_consumer(
            camera_id=camera_id,
            output_dir=tmp_path,
            recording_metadata=recording_metadata,
            ring_depth=16,
            chunk_target_bytes=64 * 1024,
            thermal_width=16,
            thermal_height=16,
        )
        ring_proc, processing_consumer = create_processing_consumer(
            camera_id=camera_id,
            analysis_config=make_analysis_config(camera_id),
            calibration_provider=FixedCalibrationProvider(),
            alarm_evaluator=NullAlarmEvaluator(),
            ring_depth=16,
            thermal_width=16,
            thermal_height=16,
        )

        try:
            recording_consumer.start()
            processing_consumer.start()

            for seq in range(6):
                result = publisher.publish(make_thermal_frame(camera_id, seq))
                assert result.accepted is True

            assert recording_consumer.wait_for_frames(6, timeout=5.0)
            assert processing_consumer.wait_for_frames(6, timeout=5.0)

            recording_consumer.stop()
            processing_consumer.stop()

            assert recording_consumer.stats().frames_written == 6
            assert processing_consumer.stats().frames_processed == 6
            assert processing_consumer.stats().last_sequence == 5
            assert processing_consumer.stats().last_camera_id == camera_id
        finally:
            recording_consumer.close()
            ring_rec.close()
            processing_consumer.close()
            ring_proc.close()
            ring.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])