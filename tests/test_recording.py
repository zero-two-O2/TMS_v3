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
from thermal_monitor.processing.sources import OfflineFrameSource
from thermal_monitor.storage.recording import (
    FileRecordingSink,
    NullRecordingSink,
    Recorder,
    RollingFrameBuffer,
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


class TestFileRecordingSink:
    def test_open_close(self, tmp_path):
        sink = FileRecordingSink(tmp_path)
        metadata = RecordingMetadata(
            recording_id="test_rec",
            camera_id="test_cam",
            trigger=RecordingTrigger.ALARM,
            state=RecordingState.RECORDING,
            start_timestamp=1.0,
            start_sequence=0,
        )
        sink.open(metadata)
        assert sink.get_file_path() is not None
        sink.close()

    def test_write_frame(self, tmp_path):
        import numpy as np
        sink = FileRecordingSink(tmp_path)
        metadata = RecordingMetadata(
            recording_id="test_rec",
            camera_id="test_cam",
            trigger=RecordingTrigger.ALARM,
            state=RecordingState.RECORDING,
            start_timestamp=1.0,
            start_sequence=0,
        )
        sink.open(metadata)

        thermal = np.zeros((10, 10), dtype=np.uint16)
        thermal.setflags(write=False)
        thermal_meta = StreamMetadata(present=True, width=10, height=10)
        descriptor = FrameDescriptor(
            camera_id="test_cam",
            sequence=0,
            timestamp=1.0,
            monotonic_timestamp=1.0,
            thermal=thermal_meta,
            visible=StreamMetadata(present=False),
            sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
        )
        frame = Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal))

        sink.write_frame(frame)
        sink.close()

        # File should exist
        assert Path(sink.get_file_path()).exists()

        # Test loading from file
        offline_source = OfflineFrameSource.from_recording_file(sink.get_file_path())
        assert len(offline_source) == 1
        assert offline_source.camera_id == "test_cam"
        loaded_frame = offline_source.get_next_frame()
        assert loaded_frame is not None
        assert loaded_frame.descriptor.camera_id == "test_cam"
        assert loaded_frame.descriptor.sequence == 0
        assert loaded_frame.payload.thermal is not None
        assert loaded_frame.payload.thermal.shape == (10, 10)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])