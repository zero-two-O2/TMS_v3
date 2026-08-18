"""
storage.repositories.system -- System configuration repository.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from thermal_monitor.core.models import SystemConfig, RecordingConfig
from thermal_monitor.storage.database import Database
from thermal_monitor.storage.repositories.base import BaseRepository, RepositoryResult


@dataclass
class SystemConfigRow:
    """Database row for system configuration table."""

    id: int
    config_key: str
    config_value: str
    description: str | None


class SystemConfigRepository(BaseRepository[SystemConfig]):
    """Repository for system configuration."""

    def __init__(self, database: Database) -> None:
        super().__init__(database, "system_config")

    def _get_columns(self) -> list[str]:
        return ["config_key", "config_value", "description"]

    def _to_entity(self, row: tuple) -> SystemConfig:
        # This is a key-value store, so we need to reconstruct from multiple rows
        raise NotImplementedError("Use get_config/set_config methods")

    def _to_params(self, entity: SystemConfig) -> tuple:
        raise NotImplementedError("Use get_config/set_config methods")

    def get_config(self, key: str, default: Any = None) -> Any:
        """Get a configuration value by key."""
        sql = f"SELECT config_value FROM {self._table_name} WHERE config_key = ?"
        try:
            result = self._db.fetch_one(sql, (key,))
            if result and result[0]:
                return json.loads(result[0])
            return default
        except Exception:
            return default

    def set_config(self, key: str, value: Any, description: str | None = None) -> bool:
        """Set a configuration value."""
        sql = f"""
            MERGE INTO {self._table_name} AS target
            USING (SELECT ? as config_key) AS source
            ON target.config_key = source.config_key
            WHEN MATCHED THEN
                UPDATE SET config_value = ?, description = ?
            WHEN NOT MATCHED THEN
                INSERT (config_key, config_value, description) VALUES (?, ?, ?);
        """
        try:
            with self._db.transaction() as cursor:
                cursor.execute(sql, (key, json.dumps(value), description, key, json.dumps(value), description))
            return True
        except Exception:
            return False

    def get_all_configs(self) -> dict[str, Any]:
        """Get all configuration values."""
        sql = f"SELECT config_key, config_value FROM {self._table_name}"
        try:
            rows = self._db.fetch_all(sql)
            return {row[0]: json.loads(row[1]) for row in rows}
        except Exception:
            return {}


@dataclass
class RecordingConfigRow:
    """Database row for recording configuration table."""

    id: int
    camera_id: str
    enabled: bool
    pre_alarm_seconds: float
    post_alarm_seconds: float
    max_duration_seconds: float
    max_file_size_mb: int
    storage_path: str
    compression_enabled: bool


class RecordingConfigRepository(BaseRepository[RecordingConfig]):
    """Repository for per-camera recording configurations."""

    def __init__(self, database: Database) -> None:
        super().__init__(database, "recording_configs")

    def _get_columns(self) -> list[str]:
        return [
            "camera_id", "enabled", "pre_alarm_seconds", "post_alarm_seconds",
            "max_duration_seconds", "max_file_size_mb", "storage_path", "compression_enabled",
        ]

    def _to_entity(self, row: tuple) -> RecordingConfig:
        r = RecordingConfigRow(*row)
        return RecordingConfig(
            camera_id=r.camera_id,
            enabled=r.enabled,
            pre_alarm_seconds=r.pre_alarm_seconds,
            post_alarm_seconds=r.post_alarm_seconds,
            max_duration_seconds=r.max_duration_seconds,
            max_file_size_mb=r.max_file_size_mb,
            storage_path=r.storage_path,
            compression_enabled=r.compression_enabled,
        )

    def _to_params(self, entity: RecordingConfig) -> tuple:
        return (
            entity.camera_id,
            entity.enabled,
            entity.pre_alarm_seconds,
            entity.post_alarm_seconds,
            entity.max_duration_seconds,
            entity.max_file_size_mb,
            entity.storage_path,
            entity.compression_enabled,
        )

    def find_by_camera_id(self, camera_id: str) -> RepositoryResult[RecordingConfig | None]:
        return self.find_all("camera_id = ?", (camera_id,))