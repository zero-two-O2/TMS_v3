"""roi.serialization -- versioned JSON round-trip for RoiDefinition."""

from __future__ import annotations

from types import MappingProxyType

from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.errors import RoiValidationError
from thermal_monitor.roi.geometry import geometry_from_dict
from thermal_monitor.roi.models import SCHEMA_VERSION, RoiDefinition


def roi_to_dict(roi: RoiDefinition) -> dict:
    return {
        "roi_id": roi.roi_id,
        "camera_id": roi.camera_id,
        "ptz_id": roi.ptz_id,
        "position_id": roi.position_id,
        "object_type": roi.object_type.value,
        "geometry": roi.geometry.to_dict(),
        "name": roi.name,
        "enabled": roi.enabled,
        "visible": roi.visible,
        "created_at": roi.created_at,
        "updated_at": roi.updated_at,
        "schema_version": roi.schema_version,
        "analysis_config": dict(roi.analysis_config),
        "alarm_rule_ref": roi.alarm_rule_ref,
        "display": dict(roi.display),
    }


def roi_from_dict(data: dict) -> RoiDefinition:
    if not isinstance(data, dict):
        raise RoiValidationError("ROI payload must be a mapping")
    version = data.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise RoiValidationError(
            f"unsupported ROI schema_version {version}, expected {SCHEMA_VERSION}")
    try:
        object_type = RoiObjectType(data["object_type"])
    except (KeyError, ValueError) as exc:
        raise RoiValidationError(f"unknown object_type {data.get('object_type')!r}") from exc
    geometry = geometry_from_dict(object_type, data.get("geometry", {}))
    return RoiDefinition(
        roi_id=data["roi_id"], camera_id=data["camera_id"],
        ptz_id=data["ptz_id"], position_id=data["position_id"],
        object_type=object_type, geometry=geometry,
        name=data.get("name", ""), enabled=bool(data.get("enabled", True)),
        visible=bool(data.get("visible", True)),
        created_at=float(data.get("created_at", 0.0)),
        updated_at=float(data.get("updated_at", 0.0)),
        schema_version=version,
        analysis_config=MappingProxyType(dict(data.get("analysis_config", {}))),
        alarm_rule_ref=data.get("alarm_rule_ref", ""),
        display=MappingProxyType(dict(data.get("display", {}))))


__all__ = ["roi_to_dict", "roi_from_dict"]
