"""
storage.repositories.recording -- Recording repository implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from thermal_monitor.core.models import RecordingMetadata, RecordingState, RecordingTrigger
from thermal_monitor.storage.database import Database
from thermal_monitor.storage.repositories.base import BaseRepository, RepositoryResult


@dataclass
class RecordingRow:
    """Database row for recordings table."""

    id: int
    recording_id: str
    camera_id: str
    trigger: str
    state: str
    start_timestamp: float
    end_timestamp: float | None
    start_sequence: int
    end_sequence: int | None
    pre_alarm_frames: int
    post_alarm_frames: int
    alarm_event_id: str | None
    position_id: str | None
    roi_config_hash: str | None
    file_path: str | None
    file_size_bytes: int
    frame_count: int
    duration_seconds: float


class RecordingRepository(BaseRepository[RecordingMetadata]):
    """Repository for recording metadata."""

    def __init__(self, database: Database) -> None:
        super().__init__(database, "recordings")

    def _get_columns(self) -> list[str]:
        return [
            "recording_id", "camera_id", "trigger", "state",
            "start_timestamp", "end_timestamp", "start_sequence", "end_sequence",
            "pre_alarm_frames", "post_alarm_frames", "alarm_event_id",
            "position_id", "roi_config_hash", "file_path",
            "file_size_bytes", "frame_count", "duration_seconds",
        ]

    def _to_entity(self, row: tuple) -> RecordingMetadata:
        r = RecordingRow(*row)
        return RecordingMetadata(
            recording_id=r.recording_id,
            camera_id=r.camera_id,
            trigger=RecordingTrigger(r.trigger),
            state=RecordingState(r.state),
            start_timestamp=r.start_timestamp,
            end_timestamp=r.end_timestamp,
            start_sequence=r.start_sequence,
            end_sequence=r.end_sequence,
            pre_alarm_frames=r.pre_alarm_frames,
            post_alarm_frames=r.post_alarm_frames,
            alarm_event_id=r.alarm_event_id,
            position_id=r.position_id,
            roi_config_hash=r.roi_config_hash,
            file_path=r.file_path,
            file_size_bytes=r.file_size_bytes,
            frame_count=r.frame_count,
            duration_seconds=r.duration_seconds,
        )

    def _to_params(self, entity: RecordingMetadata) -> tuple:
        return (
            entity.recording_id,
            entity.camera_id,
            entity.trigger.value,
            entity.state.value,
            entity.start_timestamp,
            entity.end_timestamp,
            entity.start_sequence,
            entity.end_sequence,
            entity.pre_alarm_frames,
            entity.post_alarm_frames,
            entity.alarm_event_id,
            entity.position_id,
            entity.roi_config_hash,
            entity.file_path,
            entity.file_size_bytes,
            entity.frame_count,
            entity.duration_seconds,
        )

    def find_by_camera_id(self, camera_id: str, limit: int = 50) -> RepositoryResult[list[RecordingMetadata]]:
        sql = f"SELECT TOP {limit} * FROM {self._table_name} WHERE camera_id = ? ORDER BY start_timestamp DESC"
        try:
            rows = self._db.fetch_all(sql, (camera_id,))
            entities = [self._to_entity(row) for row in rows]
            return RepositoryResult(success=True, data=entities, rows_affected=len(entities))
        except Exception as e:
            return RepositoryResult(success=False, error=str(e))

    def find_by_state(self, state: RecordingState) -> RepositoryResult[list[RecordingMetadata]]:
        return self.find_all("state = ?", (state.value,))

    def find_active(self) -> RepositoryResult[list[RecordingMetadata]]:
        return self.find_by_state(RecordingState.RECORDING)

    def update_state(self, recording_id: str, state: RecordingState) -> RepositoryResult[bool]:
        sql = f"UPDATE {self._table_name} SET state = ? WHERE recording_id = ?"
        try:
            with self._db.transaction() as cursor:
                cursor.execute(sql, (state.value, recording_id))
                rows = cursor.rowcount
            return RepositoryResult(success=True, data=rows > 0, rows_affected=rows)
        except Exception as e:
            return RepositoryResult(success=False, error=str(e))

    def update_file_info(
        self,
        recording_id: str,
        file_path: str,
        file_size_bytes: int,
        frame_count: int,
        duration_seconds: float,
    ) -> RepositoryResult[bool]:
        sql = f"""
            UPDATE {self._table_name}
            SET file_path = ?, file_size_bytes = ?, frame_count = ?, duration_seconds = ?
            WHERE recording_id = ?
        """
        try:
            with self._db.transaction() as cursor:
                cursor.execute(sql, (file_path, file_size_bytes, frame_count, duration_seconds, recording_id))
                rows = cursor.rowcount
            return RepositoryResult(success=True, data=rows > 0, rows_affected=rows)
        except Exception as e:
            return RepositoryResult(success=False, error=str(e))