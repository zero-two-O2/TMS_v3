"""
services.recording -- Recording service for coordinating alarm-triggered and continuous recording.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from thermal_monitor.core.models import (
    AlarmEvent,
    CameraConfig,
    RecordingConfig,
    RecordingMetadata,
    RecordingState,
    RecordingTrigger,
)
from thermal_monitor.services.recording_consumer import RecordingConsumer, RecordingConsumerStats, create_recording_consumer
from thermal_monitor.storage.recording import (
    RecordingWriteMetadata,
    Recorder,
    FileRecordingSink,
    NullRecordingSink,
)
from thermal_monitor.core.shm import SharedMemoryRingBuffer
import numpy as np


@dataclass
class RecordingService:
    """Application-level service for coordinating recording.

    Manages recorders for each camera and handles alarm-triggered
    recording with pre/post-alarm capture.
    """

    recorders: dict[str, Recorder] = field(default_factory=dict)
    recording_configs: dict[str, RecordingConfig] = field(default_factory=dict)
    sink_factory: Optional[Callable[[RecordingMetadata], RecordingSink]] = None
    _recording_callbacks: dict[str, list[Callable[[RecordingMetadata], None]]] = field(default_factory=dict)
    _completion_callbacks: dict[str, list[Callable[[RecordingMetadata], None]]] = field(default_factory=dict)

    def __init__(
        self,
        sink_factory: Optional[Callable[[RecordingMetadata], "FileRecordingSink | NullRecordingSink"]] = None,
    ) -> None:
        self.sink_factory = sink_factory or self._default_sink_factory

    def _default_sink_factory(self, metadata: RecordingMetadata) -> "FileRecordingSink | NullRecordingSink":
        return NullRecordingSink()

    def get_recorder(self, camera_id: str) -> Recorder | None:
        return self.recorders.get(camera_id)

    def get_all_recorders(self) -> list[Recorder]:
        return list(self.recorders.values())

    def create_recorder(self, config: RecordingConfig) -> Recorder:
        recorder = Recorder(config=config, sink_factory=self.sink_factory)
        self.recorders[config.camera_id] = recorder
        self.recording_configs[config.camera_id] = config
        return recorder

    def remove_recorder(self, camera_id: str) -> bool:
        if camera_id in self.recorders:
            del self.recorders[camera_id]
            if camera_id in self.recording_configs:
                del self.recording_configs[camera_id]
            return True
        return False

    def get_recording_config(self, camera_id: str) -> RecordingConfig | None:
        return self.recording_configs.get(camera_id)

    def set_recording_config(self, config: RecordingConfig) -> None:
        self.recording_configs[config.camera_id] = config
        if config.camera_id in self.recorders:
            self.recorders[config.camera_id]._config = config

    def feed_frame(
        self,
        camera_id: str,
        frame,
        position_id: str | None = None,
        roi_config_hash: str | None = None,
    ) -> None:
        """Feed a frame to the recorder."""
        recorder = self.recorders.get(camera_id)
        if recorder is not None:
            recorder.feed_frame(frame, position_id, roi_config_hash)

    def trigger_recording(
        self,
        alarm_event: AlarmEvent,
        camera_id: str,
        position_id: str | None = None,
        roi_config_hash: str | None = None,
    ) -> RecordingMetadata | None:
        """Trigger recording due to an alarm event."""
        recorder = self.recorders.get(camera_id)
        if recorder is None:
            return None

        metadata = recorder.trigger_recording(alarm_event, camera_id, position_id, roi_config_hash)
        self._notify_recording_started(metadata)
        return metadata

    def stop_recording(self, camera_id: str) -> RecordingMetadata | None:
        """Manually stop recording for a camera."""
        recorder = self.recorders.get(camera_id)
        if recorder is not None:
            metadata = recorder.stop_recording()
            if metadata is not None:
                self._notify_recording_completed(metadata)
            return metadata
        return None

    def arm_recorder(self, camera_id: str) -> None:
        """Arm recorder for potential alarm."""
        recorder = self.recorders.get(camera_id)
        if recorder is not None:
            recorder.arm()

    def disarm_recorder(self, camera_id: str) -> None:
        """Disarm recorder."""
        recorder = self.recorders.get(camera_id)
        if recorder is not None:
            recorder.disarm()

    def get_recorder_state(self, camera_id: str) -> RecordingState | None:
        recorder = self.recorders.get(camera_id)
        if recorder is not None:
            return recorder.state
        return None

    def get_all_recorder_states(self) -> dict[str, RecordingState]:
        return {cam_id: rec.state for cam_id, rec in self.recorders.items()}

    def get_current_metadata(self, camera_id: str) -> RecordingMetadata | None:
        recorder = self.recorders.get(camera_id)
        if recorder is not None:
            return recorder.get_current_metadata()
        return None

    def add_recording_callback(self, camera_id: str, callback: Callable[[RecordingMetadata], None]) -> None:
        if camera_id not in self._recording_callbacks:
            self._recording_callbacks[camera_id] = []
        self._recording_callbacks[camera_id].append(callback)

    def remove_recording_callback(self, camera_id: str, callback: Callable[[RecordingMetadata], None]) -> None:
        if camera_id in self._recording_callbacks:
            try:
                self._recording_callbacks[camera_id].remove(callback)
            except ValueError:
                pass

    def add_completion_callback(self, camera_id: str, callback: Callable[[RecordingMetadata], None]) -> None:
        if camera_id not in self._completion_callbacks:
            self._completion_callbacks[camera_id] = []
        self._completion_callbacks[camera_id].append(callback)

    def remove_completion_callback(self, camera_id: str, callback: Callable[[RecordingMetadata], None]) -> None:
        if camera_id in self._completion_callbacks:
            try:
                self._completion_callbacks[camera_id].remove(callback)
            except ValueError:
                pass

    def _notify_recording_started(self, metadata: RecordingMetadata) -> None:
        callbacks = self._recording_callbacks.get(metadata.camera_id, [])
        for cb in callbacks:
            try:
                cb(metadata)
            except Exception:
                pass

    def _notify_recording_completed(self, metadata: RecordingMetadata) -> None:
        callbacks = self._completion_callbacks.get(metadata.camera_id, [])
        for cb in callbacks:
            try:
                cb(metadata)
            except Exception:
                pass

    def get_active_recordings(self) -> list[RecordingMetadata]:
        """Get all currently active recordings."""
        return [
            rec.get_current_metadata()
            for rec in self.recorders.values()
            if rec.state == RecordingState.RECORDING and rec.get_current_metadata() is not None
        ]


@dataclass
class ContinuousRecordingManager:
    """Manages continuous (non-alarm-triggered) recording consumers for each camera.

    Each camera's SHM ring buffer has an independent RecordingConsumer that writes
    to the Stage 5C RecordingWriter format. This is separate from the alarm-triggered
    Recorder which uses the older RecordingSink format.
    """

    # Output directory for recordings (required, no default)
    output_dir: Path
    # Per-camera recording consumers
    consumers: dict[str, RecordingConsumer] = field(default_factory=dict)
    # Per-camera SHM ring buffers (owned by producer, but we hold refs for cleanup)
    ring_buffers: dict[str, SharedMemoryRingBuffer] = field(default_factory=dict)
    # Ring buffer depth (must match producer)
    ring_depth: int = 32
    # Chunk target bytes for RecordingWriter
    chunk_target_bytes: int = 64 * 1024 * 1024
    # Thermal frame parameters
    thermal_width: int = 640
    thermal_height: int = 480
    thermal_dtype: type = np.uint16

    def __init__(
        self,
        output_dir: str | Path,
        *,
        ring_depth: int = 32,
        chunk_target_bytes: int = 64 * 1024 * 1024,
        thermal_width: int = 640,
        thermal_height: int = 480,
        thermal_dtype: type = np.uint16,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.ring_depth = ring_depth
        self.chunk_target_bytes = chunk_target_bytes
        self.thermal_width = thermal_width
        self.thermal_height = thermal_height
        self.thermal_dtype = np.dtype(thermal_dtype)
        # Initialize dict fields that have default_factory in dataclass
        self.consumers: dict[str, RecordingConsumer] = {}
        self.ring_buffers: dict[str, SharedMemoryRingBuffer] = {}

    def start_recording(
        self,
        camera_id: str,
        recording_id: str,
        camera_snapshots: list[dict] | None = None,
        roi_snapshots: list[dict] | None = None,
        ptz_snapshots: list[dict] | None = None,
        calibration_snapshots: list[dict] | None = None,
        alarm_snapshots: list[dict] | None = None,
        trigger: str = "manual",
    ) -> tuple[SharedMemoryRingBuffer, RecordingConsumer]:
        """Start continuous recording for a camera.

        Attaches to the existing SHM ring buffer (created by the producer)
        and starts a RecordingConsumer thread.

        Args:
            camera_id: Camera identifier
            recording_id: Unique recording identifier (e.g., "rec_<uuid>")
            camera_snapshots: Camera configuration snapshots
            roi_snapshots: ROI configuration snapshots
            ptz_snapshots: PTZ configuration snapshots
            calibration_snapshots: Calibration snapshots
            alarm_snapshots: Alarm rule snapshots
            trigger: Recording trigger type

        Returns:
            Tuple of (ring_buffer, recording_consumer)
        """
        if camera_id in self.consumers:
            raise RuntimeError(f"Recording already active for camera {camera_id}")

        # Create recording metadata
        metadata = RecordingWriteMetadata(
            recording_id=recording_id,
            cameras=[camera_id],
            streams={camera_id: ["IR"]},  # Only IR stream for now
            trigger=trigger,
            camera_snapshots=camera_snapshots or [],
            roi_snapshots=roi_snapshots or [],
            ptz_snapshots=ptz_snapshots or [],
            calibration_snapshots=calibration_snapshots or [],
            alarm_snapshots=alarm_snapshots or [],
        )

        # Attach to ring buffer and create consumer
        ring, consumer = create_recording_consumer(
            camera_id=camera_id,
            output_dir=self.output_dir,
            recording_metadata=metadata,
            ring_depth=self.ring_depth,
            chunk_target_bytes=self.chunk_target_bytes,
            thermal_width=self.thermal_width,
            thermal_height=self.thermal_height,
            thermal_dtype=self.thermal_dtype,
        )

        consumer.start()

        self.consumers[camera_id] = consumer
        self.ring_buffers[camera_id] = ring

        return ring, consumer

    def stop_recording(self, camera_id: str) -> RecordingConsumerStats | None:
        """Stop continuous recording for a camera and return final stats."""
        consumer = self.consumers.pop(camera_id, None)
        ring = self.ring_buffers.pop(camera_id, None)

        if consumer is None:
            return None

        stats = consumer.stats()
        consumer.stop()
        consumer.close()

        # Note: We don't close the ring buffer here - it's owned by the producer
        # The producer will close it when the acquisition worker stops

        return stats

    def abort_recording(self, camera_id: str) -> None:
        """Abort recording without finalizing (for crash recovery testing)."""
        consumer = self.consumers.pop(camera_id, None)
        ring = self.ring_buffers.pop(camera_id, None)

        if consumer is not None:
            consumer.abort()
            consumer.close()

    def get_consumer(self, camera_id: str) -> RecordingConsumer | None:
        """Get the recording consumer for a camera."""
        return self.consumers.get(camera_id)

    def get_stats(self, camera_id: str) -> RecordingConsumerStats | None:
        """Get current stats for a camera's recording consumer."""
        consumer = self.consumers.get(camera_id)
        if consumer is not None:
            return consumer.stats()
        return None

    def get_all_stats(self) -> dict[str, RecordingConsumerStats]:
        """Get stats for all active recording consumers."""
        return {cam_id: consumer.stats() for cam_id, consumer in self.consumers.items()}

    def stop_all(self) -> dict[str, RecordingConsumerStats]:
        """Stop all recordings and return final stats."""
        results = {}
        for camera_id in list(self.consumers.keys()):
            stats = self.stop_recording(camera_id)
            if stats is not None:
                results[camera_id] = stats
        return results

    def abort_all(self) -> None:
        """Abort all recordings without finalizing."""
        for camera_id in list(self.consumers.keys()):
            self.abort_recording(camera_id)