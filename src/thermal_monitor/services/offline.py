"""
services.offline -- Offline service for managing offline playback sessions.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Callable, Optional

from thermal_monitor.core.models import AnalysisConfig, CameraConfig
from thermal_monitor.offline import FrameSource, OfflineFrameSource, OfflineFrameSourceConfig, StreamFilter
from thermal_monitor.processing.sources import SyntheticFrameSource
from thermal_monitor.processing.alarms import AlarmEvaluator


@dataclasses.dataclass
class OfflineSession:
    """Represents an offline playback session."""

    session_id: str
    camera_id: str
    source: FrameSource
    analysis_config: AnalysisConfig
    evaluator: AlarmEvaluator | None = None
    current_frame_index: int = 0
    is_playing: bool = False
    playback_speed: float = 1.0


class OfflineService:
    """Application-level service for managing offline playback sessions.

    Handles loading recorded data, creating offline frame sources,
    and managing playback sessions.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, OfflineSession] = {}
        self._frame_callbacks: dict[str, list[Callable]] = {}
        self._session_callbacks: dict[str, list[Callable]] = {}

    def create_session(
        self,
        session_id: str,
        camera_id: str,
        recording_dir: str,
        analysis_config: AnalysisConfig,
        evaluator: AlarmEvaluator | None = None,
        *,
        stream_filter: StreamFilter = StreamFilter.ALL,
        start_timestamp: float | None = None,
        end_timestamp: float | None = None,
    ) -> OfflineSession:
        """Create an offline session from a recording directory.

        Args:
            session_id: Unique session identifier.
            camera_id: Camera ID to filter (or None for all cameras in recording).
            recording_dir: Path to the recording directory (Stage 5C format).
            analysis_config: Analysis configuration for processing.
            evaluator: Optional alarm evaluator.
            stream_filter: Filter by stream type (IR, VL, or ALL).
            start_timestamp: Only include frames at or after this timestamp.
            end_timestamp: Only include frames at or before this timestamp.

        Returns:
            OfflineSession with opened OfflineFrameSource.
        """
        config = OfflineFrameSourceConfig(
            camera_id=camera_id,
            stream_filter=stream_filter,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
        source = OfflineFrameSource(recording_dir, config=config)
        source.open()

        if evaluator is None:
            evaluator = AlarmEvaluator(config=analysis_config)

        session = OfflineSession(
            session_id=session_id,
            camera_id=camera_id,
            source=source,
            analysis_config=analysis_config,
            evaluator=evaluator,
        )
        self._sessions[session_id] = session
        self._notify_session_created(session)
        return session

    def create_synthetic_session(
        self,
        session_id: str,
        camera_id: str,
        analysis_config: AnalysisConfig,
        evaluator: AlarmEvaluator | None = None,
        thermal_shape: tuple[int, int] = (480, 640),
    ) -> OfflineSession:
        """Create a synthetic session for testing."""
        source = SyntheticFrameSource(
            camera_id=camera_id,
            thermal_shape=thermal_shape,
        )
        if evaluator is None:
            evaluator = AlarmEvaluator(config=analysis_config)

        session = OfflineSession(
            session_id=session_id,
            camera_id=camera_id,
            source=source,
            analysis_config=analysis_config,
            evaluator=evaluator,
        )
        self._sessions[session_id] = session
        self._notify_session_created(session)
        return session

    def get_session(self, session_id: str) -> OfflineSession | None:
        return self._sessions.get(session_id)

    def get_all_sessions(self) -> list[OfflineSession]:
        return list(self._sessions.values())

    def remove_session(self, session_id: str) -> bool:
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False

    def play(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is not None:
            session.is_playing = True
            self._notify_session_changed(session)
            return True
        return False

    def pause(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is not None:
            session.is_playing = False
            self._notify_session_changed(session)
            return True
        return False

    def seek(self, session_id: str, frame_index: int) -> bool:
        session = self._sessions.get(session_id)
        if session is not None:
            if session.source.seek_to_index(frame_index):
                session.current_frame_index = frame_index
                self._notify_session_changed(session)
                return True
        return False

    def seek_to_sequence(self, session_id: str, sequence: int) -> bool:
        session = self._sessions.get(session_id)
        if session is not None:
            if session.source.seek(sequence):
                # Update index to match
                # Note: this would require the source to expose current index
                self._notify_session_changed(session)
                return True
        return False

    def set_playback_speed(self, session_id: str, speed: float) -> bool:
        session = self._sessions.get(session_id)
        if session is not None:
            session.playback_speed = max(0.1, min(10.0, speed))
            self._notify_session_changed(session)
            return True
        return False

    def get_next_frame(self, session_id: str):
        """Get the next frame from the session's source."""
        session = self._sessions.get(session_id)
        if session is not None and session.source is not None:
            frame = session.source.get_next_frame()
            if frame is not None:
                session.current_frame_index += 1
                self._notify_frame(session, frame)
            return frame
        return None

    def get_latest_frame(self, session_id: str):
        """Get the latest frame without advancing."""
        session = self._sessions.get(session_id)
        if session is not None and session.source is not None:
            return session.source.get_latest_frame()
        return None

    def process_next_frame(self, session_id: str):
        """Process the next frame through analysis and alarm evaluation."""
        frame = self.get_next_frame(session_id)
        if frame is None:
            return None

        session = self._sessions.get(session_id)
        if session is None:
            return None

        # Run analysis
        # This would use the analysis pipeline
        # For now, return the frame

        # Run alarm evaluation if evaluator exists
        if session.evaluator is not None:
            # Would need analysis result
            pass

        return frame

    def add_frame_callback(self, session_id: str, callback: Callable) -> None:
        if session_id not in self._frame_callbacks:
            self._frame_callbacks[session_id] = []
        self._frame_callbacks[session_id].append(callback)

    def remove_frame_callback(self, session_id: str, callback: Callable) -> None:
        if session_id in self._frame_callbacks:
            try:
                self._frame_callbacks[session_id].remove(callback)
            except ValueError:
                pass

    def add_session_callback(self, session_id: str, callback: Callable) -> None:
        if session_id not in self._session_callbacks:
            self._session_callbacks[session_id] = []
        self._session_callbacks[session_id].append(callback)

    def remove_session_callback(self, session_id: str, callback: Callable) -> None:
        if session_id in self._session_callbacks:
            try:
                self._session_callbacks[session_id].remove(callback)
            except ValueError:
                pass

    def _notify_frame(self, session: OfflineSession, frame) -> None:
        callbacks = self._frame_callbacks.get(session.session_id, [])
        for cb in callbacks:
            try:
                cb(session, frame)
            except Exception:
                pass

    def _notify_session_created(self, session: OfflineSession) -> None:
        callbacks = self._session_callbacks.get(session.session_id, [])
        for cb in callbacks:
            try:
                cb(session, "created")
            except Exception:
                pass

    def _notify_session_changed(self, session: OfflineSession) -> None:
        callbacks = self._session_callbacks.get(session.session_id, [])
        for cb in callbacks:
            try:
                cb(session, "changed")
            except Exception:
                pass