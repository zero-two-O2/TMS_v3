"""
storage.recording -- Recording contracts and implementations.

Defines the recording abstraction for alarm-triggered raw data recording.
The recorder is independent from acquisition and storage layers.
"""

from __future__ import annotations

import dataclasses
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Optional, Protocol

import numpy as np

from thermal_monitor.core.frame import Frame
from thermal_monitor.core.models import (
    AlarmEvent,
    RecordingConfig,
    RecordingMetadata,
    RecordingState,
    RecordingTrigger,
)


class FrameRecorder(Protocol):
    """Protocol for recording frames to storage."""

    def start_recording(self, metadata: RecordingMetadata) -> None:
        """Start a new recording session."""
        ...

    def write_frame(self, frame: Frame) -> None:
        """Write a frame to the current recording."""
        ...

    def stop_recording(self) -> RecordingMetadata:
        """Stop the current recording and return final metadata."""
        ...

    @property
    def is_recording(self) -> bool:
        """Whether currently recording."""
        ...


class RecordingSink(ABC):
    """Abstract sink for recorded frame data.

    Implementations can write to files, databases, network streams, etc.
    """

    @abstractmethod
    def open(self, metadata: RecordingMetadata) -> None:
        """Open the sink for writing."""
        ...

    @abstractmethod
    def write_frame(self, frame: Frame) -> None:
        """Write a frame to the sink."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Close the sink and finalize."""
        ...

    @abstractmethod
    def get_file_path(self) -> str | None:
        """Get the output file path if applicable."""
        ...


@dataclass(frozen=True, slots=True)
class FrameRecord:
    """A single frame record for storage.

    Contains all data needed to reconstruct the frame later.
    """

    frame: Frame
    position_id: str | None = None
    roi_config_hash: str | None = None


class RollingFrameBuffer:
    """Rolling buffer for pre-alarm frame history.

    Maintains a fixed-size buffer of recent frames for pre-alarm capture.
    """

    def __init__(self, max_frames: int) -> None:
        if max_frames <= 0:
            raise ValueError("max_frames must be > 0")
        self._max_frames = max_frames
        self._frames: list[FrameRecord] = []

    def add(self, frame: Frame, position_id: str | None = None, roi_config_hash: str | None = None) -> None:
        """Add a frame to the buffer."""
        record = FrameRecord(frame=frame, position_id=position_id, roi_config_hash=roi_config_hash)
        self._frames.append(record)
        if len(self._frames) > self._max_frames:
            self._frames.pop(0)

    def get_all(self) -> list[FrameRecord]:
        """Get all frames in the buffer (oldest first)."""
        return list(self._frames)

    def clear(self) -> None:
        """Clear the buffer."""
        self._frames.clear()

    def __len__(self) -> int:
        return len(self._frames)


class Recorder:
    """Alarm-triggered recorder with pre/post-alarm capture.

    The recorder maintains a rolling buffer of recent frames. When an alarm
    triggers, it saves the pre-alarm frames, the alarm frame, and continues
    recording post-alarm frames until the configured duration is reached.
    """

    def __init__(
        self,
        config: RecordingConfig,
        sink_factory: callable,  # Callable[[RecordingMetadata], RecordingSink]
    ) -> None:
        self._config = config
        self._sink_factory = sink_factory

        # Calculate buffer sizes from config
        fps = 9.0  # Default, should be configurable per camera
        pre_frames = max(1, int(config.pre_alarm_seconds * fps))
        self._pre_alarm_buffer = RollingFrameBuffer(max_frames=pre_frames)

        self._state = RecordingState.IDLE
        self._current_metadata: RecordingMetadata | None = None
        self._sink: RecordingSink | None = None
        self._frames_recorded = 0
        self._post_alarm_frames_remaining = 0
        self._recording_id: str | None = None

    @property
    def state(self) -> RecordingState:
        return self._state

    @property
    def is_recording(self) -> bool:
        return self._state == RecordingState.RECORDING

    @property
    def is_armed(self) -> bool:
        return self._state == RecordingState.ARMED

    def feed_frame(
        self,
        frame: Frame,
        position_id: str | None = None,
        roi_config_hash: str | None = None,
    ) -> None:
        """Feed a frame to the recorder.

        This should be called for every frame from the live source.
        """
        if self._state == RecordingState.RECORDING:
            self._write_frame(frame)
            self._post_alarm_frames_remaining -= 1
            if self._post_alarm_frames_remaining <= 0:
                self._finalize_recording()
        elif self._state == RecordingState.ARMED:
            # Just buffer for pre-alarm
            self._pre_alarm_buffer.add(frame, position_id, roi_config_hash)
        elif self._state == RecordingState.IDLE and self._config.enabled:
            # Continuously buffer for potential alarm
            self._pre_alarm_buffer.add(frame, position_id, roi_config_hash)

    def trigger_recording(
        self,
        alarm_event: AlarmEvent,
        camera_id: str,
        position_id: str | None = None,
        roi_config_hash: str | None = None,
    ) -> RecordingMetadata:
        """Trigger recording due to an alarm event.

        Args:
            alarm_event: The alarm event that triggered recording.
            camera_id: Camera ID.
            position_id: Current PTZ position ID.
            roi_config_hash: Hash of current ROI configuration.

        Returns:
            RecordingMetadata for the started recording.
        """
        if not self._config.enabled:
            raise RuntimeError("Recording is disabled")

        if self._state == RecordingState.RECORDING:
            # Already recording - extend post-alarm if needed
            fps = 9.0
            additional_frames = int(self._config.post_alarm_seconds * fps)
            self._post_alarm_frames_remaining = max(self._post_alarm_frames_remaining, additional_frames)
            return self._current_metadata

        # Create recording metadata
        self._recording_id = f"rec_{uuid.uuid4().hex[:12]}"
        fps = 9.0
        pre_frames = int(self._config.pre_alarm_seconds * fps)
        post_frames = int(self._config.post_alarm_seconds * fps)

        self._current_metadata = RecordingMetadata(
            recording_id=self._recording_id,
            camera_id=camera_id,
            trigger=RecordingTrigger.ALARM,
            state=RecordingState.RECORDING,
            start_timestamp=alarm_event.timestamp,
            start_sequence=alarm_event.frame_sequence,
            pre_alarm_frames=pre_frames,
            post_alarm_frames=post_frames,
            alarm_event_id=alarm_event.event_id,
            position_id=position_id,
            roi_config_hash=roi_config_hash,
        )

        # Create and open sink
        self._sink = self._sink_factory(self._current_metadata)
        self._sink.open(self._current_metadata)

        # Write pre-alarm frames
        pre_alarm_frames = self._pre_alarm_buffer.get_all()
        for record in pre_alarm_frames:
            self._sink.write_frame(record.frame)

        # Write the alarm frame (current frame would be fed next via feed_frame)
        # The alarm frame itself will be written on next feed_frame call

        self._state = RecordingState.RECORDING
        self._frames_recorded = len(pre_alarm_frames)
        self._post_alarm_frames_remaining = post_frames

        return self._current_metadata

    def _write_frame(self, frame: Frame) -> None:
        if self._sink is not None:
            self._sink.write_frame(frame)
            self._frames_recorded += 1

    def _finalize_recording(self) -> None:
        if self._sink is not None:
            self._sink.close()
            file_path = self._sink.get_file_path()
            if self._current_metadata is not None:
                self._current_metadata = dataclasses.replace(
                    self._current_metadata,
                    state=RecordingState.COMPLETED,
                    end_timestamp=self._current_metadata.start_timestamp + (self._frames_recorded / 9.0),
                    end_sequence=self._current_metadata.start_sequence + self._frames_recorded,
                    file_path=file_path,
                    frame_count=self._frames_recorded,
                    duration_seconds=self._frames_recorded / 9.0,
                )

        self._state = RecordingState.IDLE
        self._sink = None
        self._post_alarm_frames_remaining = 0

    def stop_recording(self) -> RecordingMetadata | None:
        """Manually stop the current recording."""
        if self._state == RecordingState.RECORDING:
            self._finalize_recording()
            return self._current_metadata
        return None

    def arm(self) -> None:
        """Arm the recorder (start buffering for potential alarm)."""
        if self._config.enabled:
            self._state = RecordingState.ARMED
            self._pre_alarm_buffer.clear()

    def disarm(self) -> None:
        """Disarm the recorder."""
        if self._state == RecordingState.ARMED:
            self._state = RecordingState.IDLE

    def get_current_metadata(self) -> RecordingMetadata | None:
        return self._current_metadata


class FileRecordingSink(RecordingSink):
    """File-based recording sink using a simple binary format.

    Format:
    - Header: magic, version, metadata JSON
    - Frames: sequence of [frame_length (4 bytes), frame_data]
    """

    MAGIC = b"TMS3REC"
    VERSION = 1

    def __init__(self, base_path: str | Path) -> None:
        self._base_path = Path(base_path)
        self._file: BinaryIO | None = None
        self._file_path: Path | None = None
        self._metadata: RecordingMetadata | None = None

    def open(self, metadata: RecordingMetadata) -> None:
        self._metadata = metadata
        self._base_path.mkdir(parents=True, exist_ok=True)
        self._file_path = self._base_path / f"{metadata.recording_id}.tmsrec"
        self._file = open(self._file_path, "wb")

        # Write header
        import json
        header = {
            "magic": self.MAGIC.decode(),
            "version": self.VERSION,
            "metadata": self._serialize_metadata(metadata),
        }
        header_json = json.dumps(header).encode("utf-8")
        self._file.write(len(header_json).to_bytes(4, "little"))
        self._file.write(header_json)

    def write_frame(self, frame: Frame) -> None:
        if self._file is None:
            raise RuntimeError("Sink not open")

        # Serialize frame to binary
        frame_data = self._serialize_frame(frame)
        self._file.write(len(frame_data).to_bytes(4, "little"))
        self._file.write(frame_data)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def get_file_path(self) -> str | None:
        return str(self._file_path) if self._file_path else None

    def _serialize_metadata(self, metadata: RecordingMetadata) -> dict:
        return {
            "recording_id": metadata.recording_id,
            "camera_id": metadata.camera_id,
            "trigger": metadata.trigger.value,
            "state": metadata.state.value,
            "start_timestamp": metadata.start_timestamp,
            "end_timestamp": metadata.end_timestamp,
            "start_sequence": metadata.start_sequence,
            "end_sequence": metadata.end_sequence,
            "pre_alarm_frames": metadata.pre_alarm_frames,
            "post_alarm_frames": metadata.post_alarm_frames,
            "alarm_event_id": metadata.alarm_event_id,
            "position_id": metadata.position_id,
            "roi_config_hash": metadata.roi_config_hash,
        }

    def _serialize_frame(self, frame: Frame) -> bytes:
        import json
        import pickle

        # Serialize descriptor
        descriptor_data = {
            "camera_id": frame.descriptor.camera_id,
            "sequence": frame.descriptor.sequence,
            "timestamp": frame.descriptor.timestamp,
            "monotonic_timestamp": frame.descriptor.monotonic_timestamp,
            "thermal": {
                "present": frame.descriptor.thermal.present,
                "width": frame.descriptor.thermal.width,
                "height": frame.descriptor.thermal.height,
                "pixel_format": frame.descriptor.thermal.pixel_format,
                "bits_per_channel": frame.descriptor.thermal.bits_per_channel,
                "dtype": frame.descriptor.thermal.dtype,
                "byte_count": frame.descriptor.thermal.byte_count,
                "sequence": frame.descriptor.thermal.sequence,
                "timestamp": frame.descriptor.thermal.timestamp,
                "monotonic_timestamp": frame.descriptor.thermal.monotonic_timestamp,
                "hardware_timestamp": frame.descriptor.thermal.hardware_timestamp,
            },
            "visible": {
                "present": frame.descriptor.visible.present,
                "width": frame.descriptor.visible.width,
                "height": frame.descriptor.visible.height,
                "pixel_format": frame.descriptor.visible.pixel_format,
                "bits_per_channel": frame.descriptor.visible.bits_per_channel,
                "dtype": frame.descriptor.visible.dtype,
                "byte_count": frame.descriptor.visible.byte_count,
                "sequence": frame.descriptor.visible.sequence,
                "timestamp": frame.descriptor.visible.timestamp,
                "monotonic_timestamp": frame.descriptor.visible.monotonic_timestamp,
                "hardware_timestamp": frame.descriptor.visible.hardware_timestamp,
            },
            "sync": {
                "status": frame.descriptor.sync.status.value,
                "time_delta": frame.descriptor.sync.time_delta,
            },
            "metadata": dict(frame.descriptor.metadata),
        }

        # Serialize payload
        thermal_bytes = frame.payload.thermal.tobytes() if frame.payload.thermal is not None else b""
        visible_bytes = frame.payload.visible.tobytes() if frame.payload.visible is not None else b""

        payload_data = {
            "thermal_shape": frame.payload.thermal.shape if frame.payload.thermal is not None else None,
            "thermal_dtype": str(frame.payload.thermal.dtype) if frame.payload.thermal is not None else None,
            "visible_shape": frame.payload.visible.shape if frame.payload.visible is not None else None,
            "visible_dtype": str(frame.payload.visible.dtype) if frame.payload.visible is not None else None,
        }

        # Use pickle for simplicity in this test implementation
        return pickle.dumps({
            "descriptor": descriptor_data,
            "payload": payload_data,
            "thermal_bytes": thermal_bytes,
            "visible_bytes": visible_bytes,
        })


class NullRecordingSink(RecordingSink):
    """Null sink for testing."""

    def open(self, metadata: RecordingMetadata) -> None:
        pass

    def write_frame(self, frame: Frame) -> None:
        pass

    def close(self) -> None:
        pass

    def get_file_path(self) -> str | None:
        return None