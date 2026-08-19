"""Tests for recording contracts and implementations."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from thermal_monitor.core.frame import Frame, FrameDescriptor, FramePayload, StreamMetadata, SyncInfo, SyncStatus
from thermal_monitor.core.models import (
    AlarmEvent,
    AlarmSeverity,
    RecordingConfig,
    RecordingMetadata,
    RecordingState,
    RecordingTrigger,
)
from thermal_monitor.offline import OfflineFrameSource, open_offline_source
from thermal_monitor.storage.recording import (
    NullRecordingSink,
    Recorder,
    RollingFrameBuffer,
    RecordingWriteMetadata,
    RecordingWriter,
)


class TestRollingFrameBuffer:
    def test_basic_buffer(self):
        buffer = RollingFrameBuffer(max_frames=3)
        assert len(buffer) == 0

        # Add frames
        for i in range(5):
            frame = self._create_frame(i)
            buffer.add(frame)

        # Should only keep last 3
        assert len(buffer) == 3
        frames = buffer.get_all()
        assert len(frames) == 3
        assert frames[0].frame.descriptor.sequence == 2
        assert frames[1].frame.descriptor.sequence == 3
        assert frames[2].frame.descriptor.sequence == 4

    def test_clear(self):
        buffer = RollingFrameBuffer(max_frames=3)
        frame = self._create_frame(0)
        buffer.add(frame)
        buffer.clear()
        assert len(buffer) == 0

    def test_max_frames_zero_raises(self):
        with pytest.raises(ValueError):
            RollingFrameBuffer(max_frames=0)

    def _create_frame(self, sequence: int) -> Frame:
        thermal = np.zeros((10, 10), dtype=np.uint16)
        thermal.setflags(write=False)
        thermal_meta = StreamMetadata(present=True, width=10, height=10)
        descriptor = FrameDescriptor(
            camera_id="test_cam",
            sequence=sequence,
            timestamp=float(sequence),
            monotonic_timestamp=float(sequence),
            thermal=thermal_meta,
            visible=StreamMetadata(present=False),
            sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
        )
        return Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal))


class TestRecorder:
    def _create_frame(self, sequence: int, timestamp: float | None = None) -> Frame:
        thermal = np.zeros((10, 10), dtype=np.uint16)
        thermal.setflags(write=False)
        thermal_meta = StreamMetadata(present=True, width=10, height=10)
        descriptor = FrameDescriptor(
            camera_id="test_cam",
            sequence=sequence,
            timestamp=timestamp or float(sequence),
            monotonic_timestamp=timestamp or float(sequence),
            thermal=thermal_meta,
            visible=StreamMetadata(present=False),
            sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
        )
        return Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal))

    def _create_alarm_event(self, sequence: int = 0) -> AlarmEvent:
        return AlarmEvent(
            event_id=f"evt_{sequence}",
            rule_id="rule_1",
            camera_id="test_cam",
            roi_id="roi_1",
            severity=AlarmSeverity.WARNING,
            measured_value=90.0,
            threshold_value=80.0,
            timestamp=float(sequence),
            frame_sequence=sequence,
        )

    def _create_sink_factory(self):
        sinks = []

        def factory(metadata: RecordingMetadata) -> NullRecordingSink:
            sink = NullRecordingSink()
            sinks.append(sink)
            return sink

        factory.sinks = sinks
        return factory

    def test_recorder_disabled(self):
        config = RecordingConfig(camera_id="test_cam", enabled=False)
        factory = self._create_sink_factory()
        recorder = Recorder(config=config, sink_factory=factory)

        assert recorder.state == RecordingState.IDLE
        with pytest.raises(RuntimeError):
            recorder.trigger_recording(self._create_alarm_event(0), "test_cam")

    def test_recorder_arm_disarm(self):
        config = RecordingConfig(camera_id="test_cam", enabled=True, pre_alarm_seconds=1.0)
        factory = self._create_sink_factory()
        recorder = Recorder(config=config, sink_factory=factory)

        recorder.arm()
        assert recorder.state == RecordingState.ARMED

        recorder.disarm()
        assert recorder.state == RecordingState.IDLE

    def test_feed_frame_while_armed(self):
        config = RecordingConfig(camera_id="test_cam", enabled=True, pre_alarm_seconds=1.0)
        factory = self._create_sink_factory()
        recorder = Recorder(config=config, sink_factory=factory)

        recorder.arm()
        frame = self._create_frame(0)
        recorder.feed_frame(frame)
        # Buffer should have the frame

    def test_trigger_recording(self):
        config = RecordingConfig(
            camera_id="test_cam",
            enabled=True,
            pre_alarm_seconds=1.0,
            post_alarm_seconds=1.0,
        )
        factory = self._create_sink_factory()
        recorder = Recorder(config=config, sink_factory=factory)

        # Feed some pre-alarm frames
        for i in range(3):
            recorder.feed_frame(self._create_frame(i))

        # Trigger recording
        alarm = self._create_alarm_event(3)
        metadata = recorder.trigger_recording(alarm, "test_cam")

        assert metadata.recording_id.startswith("rec_")
        assert metadata.camera_id == "test_cam"
        assert metadata.trigger == RecordingTrigger.ALARM
        assert metadata.state == RecordingState.RECORDING
        assert metadata.alarm_event_id == alarm.event_id
        assert recorder.state == RecordingState.RECORDING

    def test_feed_frame_during_recording(self):
        config = RecordingConfig(
            camera_id="test_cam",
            enabled=True,
            pre_alarm_seconds=0.1,
            post_alarm_seconds=0.5,  # ~4 frames at 9fps
        )
        factory = self._create_sink_factory()
        recorder = Recorder(config=config, sink_factory=factory)

        alarm = self._create_alarm_event(0)
        recorder.trigger_recording(alarm, "test_cam")

        # Feed post-alarm frames
        for i in range(1, 5):
            recorder.feed_frame(self._create_frame(i))

        # Should auto-finalize after post-alarm frames
        assert recorder.state == RecordingState.IDLE

    def test_manual_stop_recording(self):
        config = RecordingConfig(
            camera_id="test_cam",
            enabled=True,
            pre_alarm_seconds=0.1,
            post_alarm_seconds=10.0,  # Long post-alarm
        )
        factory = self._create_sink_factory()
        recorder = Recorder(config=config, sink_factory=factory)

        alarm = self._create_alarm_event(0)
        recorder.trigger_recording(alarm, "test_cam")

        recorder.feed_frame(self._create_frame(1))
        recorder.feed_frame(self._create_frame(2))

        metadata = recorder.stop_recording()
        assert metadata is not None
        assert metadata.state == RecordingState.COMPLETED
        assert recorder.state == RecordingState.IDLE

    def test_extend_recording_on_new_alarm(self):
        config = RecordingConfig(
            camera_id="test_cam",
            enabled=True,
            pre_alarm_seconds=0.1,
            post_alarm_seconds=1.0,
        )
        factory = self._create_sink_factory()
        recorder = Recorder(config=config, sink_factory=factory)

        alarm1 = self._create_alarm_event(0)
        metadata1 = recorder.trigger_recording(alarm1, "test_cam")

        # Trigger another alarm while recording
        alarm2 = self._create_alarm_event(5)
        metadata2 = recorder.trigger_recording(alarm2, "test_cam")

        # Should return same metadata, extend post-alarm
        assert metadata2.recording_id == metadata1.recording_id


def _make_test_frame(camera_id: str, sequence: int, timestamp: float) -> Frame:
    thermal = np.zeros((10, 10), dtype=np.uint16)
    thermal.setflags(write=False)
    thermal_meta = StreamMetadata(present=True, width=10, height=10, pixel_format="IR_Data")
    descriptor = FrameDescriptor(
        camera_id=camera_id,
        sequence=sequence,
        timestamp=timestamp,
        monotonic_timestamp=timestamp,
        thermal=thermal_meta,
        visible=StreamMetadata(present=False),
        sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
    )
    return Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal))


def _default_write_metadata(recording_id: str, cameras: list[str]) -> RecordingWriteMetadata:
    return RecordingWriteMetadata(
        recording_id=recording_id,
        cameras=cameras,
        streams={cam: ["IR"] for cam in cameras},
        camera_snapshots=[{"camera_id": cam} for cam in cameras],
        roi_snapshots=[],
        ptz_snapshots=[],
        calibration_snapshots=[],
        alarm_snapshots=[],
    )


class TestNewRecordingFormat:
    def test_write_and_read_frame(self, tmp_path):
        """Test writing frames with RecordingWriter and reading with OfflineFrameSource."""
        frames = [_make_test_frame("test_cam", i, 100.0 + i) for i in range(3)]

        meta = _default_write_metadata("rec_test", ["test_cam"])
        writer = RecordingWriter(tmp_path, meta, chunk_target_bytes=64 * 1024)
        writer.open()
        for frame in frames:
            writer.write_frame(frame)
        rec_dir = writer.finalize()

        # Read back using OfflineFrameSource
        source = open_offline_source(rec_dir)
        assert len(source) == 3
        assert source.camera_id == "test_cam"

        loaded = []
        while True:
            frame = source.get_next_frame()
            if frame is None:
                break
            loaded.append(frame)

        assert len(loaded) == 3
        for i, frame in enumerate(loaded):
            assert frame.descriptor.camera_id == "test_cam"
            assert frame.descriptor.sequence == i
            assert frame.payload.thermal is not None
            assert frame.payload.thermal.shape == (10, 10)
        source.close()

    def test_multiple_cameras(self, tmp_path):
        """Test recording with multiple cameras."""
        frames = [
            _make_test_frame("cam_a", i, 100.0 + i) for i in range(2)
        ] + [
            _make_test_frame("cam_b", i, 200.0 + i) for i in range(2)
        ]

        meta = _default_write_metadata("rec_multi", ["cam_a"])
        writer = RecordingWriter(tmp_path, meta, chunk_target_bytes=64 * 1024)
        writer.open()
        for frame in frames:
            writer.write_frame(frame)
        rec_dir = writer.finalize()

        # Read all cameras
        source = open_offline_source(rec_dir)
        assert len(source) == 4
        assert set(source.camera_ids) == {"cam_a", "cam_b"}

        # Filter to cam_b
        source_cam_b = open_offline_source(rec_dir, camera_id="cam_b")
        assert len(source_cam_b) == 2
        for frame in source_cam_b:
            assert frame.descriptor.camera_id == "cam_b"
        source_cam_b.close()
        source.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])