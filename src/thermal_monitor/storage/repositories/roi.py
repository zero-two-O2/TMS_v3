"""
storage.repositories.roi -- ROI repository implementation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from thermal_monitor.core.models import (
    AlarmRule,
    AnalysisConfig,
    PositionROIAssociation,
    ROIConfig,
    ROIGeometry,
    ROIShape,
    TemperatureLimits,
    TemperatureUnit,
)
from thermal_monitor.storage.database import Database
from thermal_monitor.storage.repositories.base import BaseRepository, RepositoryResult


@dataclass
class ROIRow:
    """Database row for ROI table."""

    id: int
    roi_id: str
    camera_id: str
    name: str
    enabled: bool
    shape: str
    parameters_json: str
    temp_unit: str
    min_warning: float | None
    max_warning: float | None
    min_critical: float | None
    max_critical: float | None
    rate_of_change_limit: float | None
    alarm_enabled: bool


@dataclass
class PositionROIRow:
    """Database row for position-ROI association table."""

    id: int
    camera_id: str
    position_id: str
    position_name: str
    roi_ids_json: str


@dataclass
class AlarmRuleRow:
    """Database row for alarm rules table."""

    id: int
    rule_id: str
    camera_id: str
    roi_id: str
    condition: str
    severity: str
    threshold: float | None
    threshold_low: float | None
    threshold_high: float | None
    unit: str
    enabled: bool
    description: str


@dataclass
class AnalysisConfigRow:
    """Database row for analysis config table."""

    id: int
    camera_id: str
    default_emissivity: float
    ambient_temperature: float
    distance: float
    humidity: float
    reflected_temperature: float
    unit: str


class ROIRepository(BaseRepository[ROIConfig]):
    """Repository for ROI configurations."""

    def __init__(self, database: Database) -> None:
        super().__init__(database, "rois")

    def _get_columns(self) -> list[str]:
        return [
            "roi_id", "camera_id", "name", "enabled",
            "shape", "parameters_json", "rotation",
            "temp_unit", "min_warning", "max_warning",
            "min_critical", "max_critical", "rate_of_change_limit",
            "alarm_enabled",
        ]

    def _to_entity(self, row: tuple) -> ROIConfig:
        r = ROIRow(*row)
        geometry = ROIGeometry(
            shape=ROIShape(r.shape),
            parameters=json.loads(r.parameters_json),
        )
        limits = TemperatureLimits(
            unit=TemperatureUnit(r.temp_unit),
            min_warning=r.min_warning,
            max_warning=r.max_warning,
            min_critical=r.min_critical,
            max_critical=r.max_critical,
            rate_of_change_limit=r.rate_of_change_limit,
        )
        return ROIConfig(
            roi_id=r.roi_id,
            name=r.name,
            enabled=r.enabled,
            geometry=geometry,
            temperature_limits=limits,
            alarm_enabled=r.alarm_enabled,
        )

    def _to_params(self, entity: ROIConfig) -> tuple:
        return (
            entity.roi_id,
            entity.geometry.shape.value,
            json.dumps(dict(entity.geometry.parameters)),
            entity.temperature_limits.unit.value,
            entity.temperature_limits.min_warning,
            entity.temperature_limits.max_warning,
            entity.temperature_limits.min_critical,
            entity.temperature_limits.max_critical,
            entity.temperature_limits.rate_of_change_limit,
            entity.alarm_enabled,
        )

    def find_by_camera_id(self, camera_id: str) -> RepositoryResult[list[ROIConfig]]:
        return self.find_all("camera_id = ?", (camera_id,))

    def find_by_roi_id(self, roi_id: str) -> RepositoryResult[ROIConfig | None]:
        return self.find_by_id(roi_id)

    def find_by_camera_and_position(self, camera_id: str, position_id: str) -> RepositoryResult[list[ROIConfig]]:
        return self.find_all("camera_id = ? AND position_id = ?", (camera_id, position_id))


class PositionROIRepository(BaseRepository[PositionROIAssociation]):
    """Repository for position-ROI associations."""

    def __init__(self, database: Database) -> None:
        super().__init__(database, "position_roi_associations")

    def _get_columns(self) -> list[str]:
        return ["camera_id", "position_id", "position_name", "roi_ids_json"]

    def _to_entity(self, row: tuple) -> PositionROIAssociation:
        r = PositionROIRow(*row)
        return PositionROIAssociation(
            position_id=r.position_id,
            position_name=r.position_name,
            roi_ids=tuple(json.loads(r.roi_ids_json)),
        )

    def _to_params(self, entity: PositionROIAssociation) -> tuple:
        return (
            entity.position_id,
            entity.position_name,
            json.dumps(list(entity.roi_ids)),
        )

    def find_by_camera_id(self, camera_id: str) -> RepositoryResult[list[PositionROIAssociation]]:
        return self.find_all("camera_id = ?", (camera_id,))

    def find_by_position_id(self, camera_id: str, position_id: str) -> RepositoryResult[PositionROIAssociation | None]:
        result = self.find_all("camera_id = ? AND position_id = ?", (camera_id, position_id))
        if result.success and result.data:
            return RepositoryResult(success=True, data=result.data[0])
        return RepositoryResult(success=True, data=None)


class AlarmRuleRepository(BaseRepository[AlarmRule]):
    """Repository for alarm rules."""

    def __init__(self, database: Database) -> None:
        super().__init__(database, "alarm_rules")

    def _get_columns(self) -> list[str]:
        return [
            "rule_id", "camera_id", "roi_id", "condition", "severity",
            "threshold", "threshold_low", "threshold_high", "unit",
            "enabled", "description",
        ]

    def _to_entity(self, row: tuple) -> AlarmRule:
        r = AlarmRuleRow(*row)
        return AlarmRule(
            rule_id=r.rule_id,
            roi_id=r.roi_id,
            condition=r.condition,
            severity=r.severity,
            threshold=r.threshold,
            threshold_low=r.threshold_low,
            threshold_high=r.threshold_high,
            unit=TemperatureUnit(r.unit),
            enabled=r.enabled,
            description=r.description,
        )

    def _to_params(self, entity: AlarmRule) -> tuple:
        return (
            entity.rule_id,
            entity.condition.value if hasattr(entity.condition, 'value') else str(entity.condition),
            entity.severity.value if hasattr(entity.severity, 'value') else str(entity.severity),
            entity.threshold,
            entity.threshold_low,
            entity.threshold_high,
            entity.unit.value if hasattr(entity.unit, 'value') else str(entity.unit),
            entity.enabled,
            entity.description,
        )

    def find_by_camera_id(self, camera_id: str) -> RepositoryResult[list[AlarmRule]]:
        return self.find_all("camera_id = ?", (camera_id,))

    def find_by_roi_id(self, roi_id: str) -> RepositoryResult[list[AlarmRule]]:
        return self.find_all("roi_id = ?", (roi_id,))

    def find_enabled(self) -> RepositoryResult[list[AlarmRule]]:
        return self.find_all("enabled = 1")


class AnalysisConfigRepository(BaseRepository[AnalysisConfig]):
    """Repository for analysis configurations."""

    def __init__(self, database: Database) -> None:
        super().__init__(database, "analysis_configs")
        self._roi_repo = ROIRepository(database)
        self._pos_roi_repo = PositionROIRepository(database)
        self._alarm_repo = AlarmRuleRepository(database)

    def _get_columns(self) -> list[str]:
        return [
            "camera_id", "default_emissivity", "ambient_temperature",
            "distance", "humidity", "reflected_temperature", "unit",
        ]

    def _to_entity(self, row: tuple) -> AnalysisConfig:
        r = AnalysisConfigRow(*row)
        # Load related data
        rois_result = self._roi_repo.find_by_camera_id(r.camera_id)
        pos_roi_result = self._pos_roi_repo.find_by_camera_id(r.camera_id)
        alarm_result = self._alarm_repo.find_by_camera_id(r.camera_id)

        rois = {roi.roi_id: roi for roi in (rois_result.data or [])}
        pos_assocs = {pa.position_id: pa for pa in (pos_roi_result.data or [])}
        alarms = {alarm.rule_id: alarm for alarm in (alarm_result.data or [])}

        return AnalysisConfig(
            camera_id=r.camera_id,
            rois=rois,
            position_associations=pos_assocs,
            alarm_rules=alarms,
            default_emissivity=r.default_emissivity,
            ambient_temperature=r.ambient_temperature,
            distance=r.distance,
            humidity=r.humidity,
            reflected_temperature=r.reflected_temperature,
            unit=TemperatureUnit(r.unit),
        )

    def _to_params(self, entity: AnalysisConfig) -> tuple:
        return (
            entity.camera_id,
            entity.default_emissivity,
            entity.ambient_temperature,
            entity.distance,
            entity.humidity,
            entity.reflected_temperature,
            entity.unit.value,
        )

    def find_by_camera_id(self, camera_id: str) -> RepositoryResult[AnalysisConfig | None]:
        result = self.find_all("camera_id = ?", (camera_id,))
        if result.success and result.data:
            return RepositoryResult(success=True, data=result.data[0])
        return RepositoryResult(success=True, data=None)