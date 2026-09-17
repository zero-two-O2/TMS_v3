"""
storage.repositories.sqlite_alarm -- SQLite alarm-event repository.

Same domain model as :class:`AlarmEventRepository` but with
SQLite-dialect SQL (``LIMIT`` instead of ``SELECT TOP``, explicit
column lists instead of ``SELECT *``, no ``SCOPE_IDENTITY``).
The SQL Server repository is untouched; callers pick the
implementation matching the active backend.
"""

from __future__ import annotations

import time

from thermal_monitor.core.models import AlarmEvent, AlarmSeverity
from thermal_monitor.storage.repositories.base import (
    BaseRepository,
    RepositoryResult,
)

_COLUMNS = [
    "event_id",
    "rule_id",
    "camera_id",
    "roi_id",
    "severity",
    "measured_value",
    "threshold_value",
    "timestamp",
    "frame_sequence",
    "position_id",
    "ptz_id",
    "acknowledged",
    "acknowledged_at",
    "acknowledged_by",
    "status",
    "cleared_at",
    "description",
]

_SELECT = (
    "id, event_id, rule_id, camera_id, roi_id, severity, measured_value,"
    " threshold_value, timestamp, frame_sequence, position_id, ptz_id,"
    " acknowledged, acknowledged_at, acknowledged_by, status, cleared_at,"
    " description"
)


def _to_entity(row: tuple) -> AlarmEvent:
    (rid, event_id, rule_id, camera_id, roi_id, severity, measured, threshold,
     timestamp, frame_seq, position_id, ptz_id, ack, ack_at, ack_by,
     status, cleared_at, description) = row
    return AlarmEvent(
        event_id=event_id,
        rule_id=rule_id,
        camera_id=camera_id,
        roi_id=roi_id,
        severity=AlarmSeverity(severity),
        measured_value=float(measured or 0.0),
        threshold_value=float(threshold or 0.0),
        timestamp=float(timestamp or 0.0),
        frame_sequence=int(frame_seq or 0),
        position_id=position_id,
        acknowledged=bool(ack),
        acknowledged_at=ack_at,
        acknowledged_by=ack_by,
        metadata={
            "ptz_id": ptz_id or "",
            "status": status or "ACTIVE",
            "cleared_at": cleared_at or 0.0,
            "description": description or "",
        },
    )


def _to_params(entity: AlarmEvent) -> tuple:
    meta = entity.metadata or {}
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
        (meta.get("ptz_id") or "") or None,
        1 if entity.acknowledged else 0,
        entity.acknowledged_at,
        entity.acknowledged_by,
        str(meta.get("status") or "ACTIVE"),
        meta.get("cleared_at"),
        str(meta.get("description") or ""),
    )


class SqliteAlarmEventRepository(BaseRepository[AlarmEvent]):
    """SQLite-backed alarm-event repository (explicit columns)."""

    def __init__(self, database) -> None:
        super().__init__(database, "alarm_events")

    def _get_columns(self) -> list[str]:
        return list(_COLUMNS)

    def _to_entity(self, row: tuple) -> AlarmEvent:
        return _to_entity(row)

    def _to_params(self, entity: AlarmEvent) -> tuple:
        return _to_params(entity)

    # BaseRepository.insert issues SELECT SCOPE_IDENTITY (T-SQL); override.
    def insert(self, entity: AlarmEvent) -> RepositoryResult[AlarmEvent]:
        placeholders = ",".join(["?"] * len(_COLUMNS))
        sql = (f"INSERT OR IGNORE INTO {self._table_name} "
               f"({','.join(_COLUMNS)}) VALUES ({placeholders})")
        try:
            with self._db.transaction() as cursor:
                cursor.execute(sql, _to_params(entity))
                rows = cursor.rowcount
            return RepositoryResult(success=True, data=entity, rows_affected=rows)
        except Exception as exc:
            return RepositoryResult(success=False, error=str(exc))

    def mark_cleared(self, event_id: str, cleared_at: float | None = None) -> RepositoryResult[bool]:
        sql = (f"UPDATE {self._table_name} SET status='CLEARED', cleared_at=? "
               f"WHERE event_id=? AND status<>'CLEARED'")
        try:
            with self._db.transaction() as cursor:
                cursor.execute(sql, (cleared_at or time.time(), event_id))
                rows = cursor.rowcount
            return RepositoryResult(success=True, data=rows > 0, rows_affected=rows)
        except Exception as exc:
            return RepositoryResult(success=False, error=str(exc))

    def find_recent(self, limit: int = 200) -> RepositoryResult[list[AlarmEvent]]:
        sql = (f"SELECT {_SELECT} FROM {self._table_name} "
               f"ORDER BY timestamp DESC LIMIT ?")
        try:
            rows = self._db.fetch_all(sql, (int(limit),))
            entities = [_to_entity(r) for r in rows]
            return RepositoryResult(success=True, data=entities, rows_affected=len(entities))
        except Exception as exc:
            return RepositoryResult(success=False, error=str(exc))

    def find_by_camera_id(self, camera_id: str, limit: int = 100) -> RepositoryResult[list[AlarmEvent]]:
        sql = (f"SELECT {_SELECT} FROM {self._table_name} WHERE camera_id=? "
               f"ORDER BY timestamp DESC LIMIT ?")
        try:
            rows = self._db.fetch_all(sql, (camera_id, int(limit)))
            entities = [_to_entity(r) for r in rows]
            return RepositoryResult(success=True, data=entities, rows_affected=len(entities))
        except Exception as exc:
            return RepositoryResult(success=False, error=str(exc))

    def acknowledge(self, event_id: str, acknowledged_by: str) -> RepositoryResult[bool]:
        sql = (f"UPDATE {self._table_name} SET acknowledged=1, acknowledged_at=?, "
               f"acknowledged_by=? WHERE event_id=?")
        try:
            with self._db.transaction() as cursor:
                cursor.execute(sql, (time.time(), acknowledged_by, event_id))
                rows = cursor.rowcount
            return RepositoryResult(success=True, data=rows > 0, rows_affected=rows)
        except Exception as exc:
            return RepositoryResult(success=False, error=str(exc))


__all__ = ["SqliteAlarmEventRepository"]
