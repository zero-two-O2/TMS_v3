"""
storage.repositories.alarm -- Alarm event repository implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from thermal_monitor.core.models import AlarmEvent, AlarmSeverity
from thermal_monitor.storage.database import Database
from thermal_monitor.storage.repositories.base import BaseRepository, RepositoryResult


@dataclass
class AlarmEventRow:
    """Database row for alarm events table."""

    id: int
    event_id: str
    rule_id: str
    camera_id: str
    roi_id: str
    severity: str
    measured_value: float
    threshold_value: float
    timestamp: float
    frame_sequence: int
    position_id: str | None
    acknowledged: bool
    acknowledged_at: float | None
    acknowledged_by: str | None


class AlarmEventRepository(BaseRepository[AlarmEvent]):
    """Repository for alarm events."""

    def __init__(self, database: Database) -> None:
        super().__init__(database, "alarm_events")

    def _get_columns(self) -> list[str]:
        return [
            "event_id", "rule_id", "camera_id", "roi_id", "severity",
            "measured_value", "threshold_value", "timestamp", "frame_sequence",
            "position_id", "acknowledged", "acknowledged_at", "acknowledged_by",
        ]

    def _to_entity(self, row: tuple) -> AlarmEvent:
        r = AlarmEventRow(*row)
        return AlarmEvent(
            event_id=r.event_id,
            rule_id=r.rule_id,
            camera_id=r.camera_id,
            roi_id=r.roi_id,
            severity=AlarmSeverity(r.severity),
            measured_value=r.measured_value,
            threshold_value=r.threshold_value,
            timestamp=r.timestamp,
            frame_sequence=r.frame_sequence,
            position_id=r.position_id,
            acknowledged=r.acknowledged,
            acknowledged_at=r.acknowledged_at,
            acknowledged_by=r.acknowledged_by,
        )

    def _to_params(self, entity: AlarmEvent) -> tuple:
        return (
            entity.event_id,
            entity.rule_id,
            entity.camera_id,
            entity.roi_id,
            entity.severity.value,
            entity.measured_value,
            entity.threshold_value,
            entity.timestamp,
            entity.frame_sequence,
            entity.position_id,
            entity.acknowledged,
            entity.acknowledged_at,
            entity.acknowledged_by,
        )

    def find_by_camera_id(self, camera_id: str, limit: int = 100) -> RepositoryResult[list[AlarmEvent]]:
        sql = f"SELECT TOP {limit} * FROM {self._table_name} WHERE camera_id = ? ORDER BY timestamp DESC"
        try:
            rows = self._db.fetch_all(sql, (camera_id,))
            entities = [self._to_entity(row) for row in rows]
            return RepositoryResult(success=True, data=entities, rows_affected=len(entities))
        except Exception as e:
            return RepositoryResult(success=False, error=str(e))

    def find_unacknowledged(self, camera_id: str | None = None) -> RepositoryResult[list[AlarmEvent]]:
        where = "acknowledged = 0"
        params: tuple = ()
        if camera_id:
            where += " AND camera_id = ?"
            params = (camera_id,)
        return self.find_all(where, params)

    def acknowledge(self, event_id: str, acknowledged_by: str) -> RepositoryResult[bool]:
        import time
        sql = f"UPDATE {self._table_name} SET acknowledged = 1, acknowledged_at = ?, acknowledged_by = ? WHERE event_id = ?"
        try:
            with self._db.transaction() as cursor:
                cursor.execute(sql, (time.time(), acknowledged_by, event_id))
                rows = cursor.rowcount
            return RepositoryResult(success=True, data=rows > 0, rows_affected=rows)
        except Exception as e:
            return RepositoryResult(success=False, error=str(e))