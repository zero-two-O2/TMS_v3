"""
services.recording -- Recording service for coordinating alarm-triggered recording.
"""

from __future__ import annotations

import dataclasses
from typing import Callable, Optional

from thermal_monitor.core.models import (
    AlarmEvent,
    CameraConfig,
    RecordingConfig,
    RecordingMetadata,
    RecordingState,
    RecordingTrigger,
)
from thermal_monitor.storage.recording import Recorder, FileRecordingSink, NullRecordingSink, RecordingSink


@dataclasses.dataclass
class RecordingService:
    """Application-level service for coordinating recording.

    Manages recorders for each camera and handles alarm-triggered
    recording with pre/post-alarm capture.
    """

    recorders: dict[str, Recorder] = dataclasses.field(default_factory=dict)
    recording_configs: dict[str, RecordingConfig] = dataclasses.field(default_factory=dict)
    sink_factory: Optional[Callable[[RecordingMetadata], RecordingSink]] = None
    _recording_callbacks: dict[str, list[Callable[[RecordingMetadata], None]]] = dataclasses.field(default_factory=dict)
    _completion_callbacks: dict[str, list[Callable[[RecordingMetadata], None]]] = dataclasses.field(default_factory=dict)

    def __init__(
        self,
        sink_factory: Optional[Callable[[RecordingMetadata], RecordingSink]] = None,
    ) -> None:
        self.sink_factory = sink_factory or self._default_sink_factory

    def _default_sink_factory(self, metadata: RecordingMetadata) -> RecordingSink:
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