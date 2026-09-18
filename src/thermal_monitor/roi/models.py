"""roi.models -- typed ROI domain model with camera+PTZ+position binding."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from thermal_monitor.roi.enums import RoiCategory, RoiObjectType, category_of
from thermal_monitor.roi.errors import RoiBindingError, RoiValidationError
from thermal_monitor.roi.geometry import geometry_from_dict

SCHEMA_VERSION = 1


def generate_roi_id() -> str:
    return f"roi_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True, slots=True)
class RoiDefinition:
    """One persistent ROI/analysis/measurement/annotation object.

    Ownership is exactly one (camera_id, ptz_id, position_id) triple.
    Geometry lives in image pixel coordinates (row/col), versioned by
    ``schema_version``. No Qt, no HALCON handles, no executable content.
    """

    roi_id: str
    camera_id: str
    ptz_id: str
    position_id: str
    object_type: RoiObjectType
    geometry: object  # one of the geometry dataclasses
    name: str = ""
    enabled: bool = True
    visible: bool = True
    created_at: float = 0.0
    updated_at: float = 0.0
    schema_version: int = SCHEMA_VERSION
    analysis_config: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({}))
    alarm_rule_ref: str = ""
    display: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        for label in ("roi_id", "camera_id", "ptz_id", "position_id"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value.strip():
                raise RoiBindingError(f"RoiDefinition.{label} is required")
        if not isinstance(self.object_type, RoiObjectType):
            try:
                object.__setattr__(self, "object_type", RoiObjectType(self.object_type))
            except ValueError as exc:
                raise RoiValidationError(f"unsupported object type {self.object_type!r}") from exc
        expected = geometry_expected_type(self.object_type)
        if not isinstance(self.geometry, expected):
            raise RoiValidationError(
                f"{self.object_type.value} requires {expected.__name__}, "
                f"got {type(self.geometry).__name__}")
        if not isinstance(self.name, str):
            raise RoiValidationError("name must be a string")
        if self.schema_version != SCHEMA_VERSION:
            raise RoiValidationError(
                f"unsupported schema_version {self.schema_version}, expected {SCHEMA_VERSION}")
        if self.created_at < 0 or self.updated_at < 0:
            raise RoiValidationError("timestamps must be >= 0")
        if self.alarm_rule_ref and not isinstance(self.alarm_rule_ref, str):
            raise RoiValidationError("alarm_rule_ref must be a string")
        # Only analysis objects may carry an alarm reference.
        if self.alarm_rule_ref and category_of(self.object_type) != RoiCategory.ANALYSIS:
            raise RoiValidationError(
                f"{self.object_type.value} is not alarm-capable; "
                "alarm_rule_ref must be empty")

    @property
    def category(self) -> RoiCategory:
        return category_of(self.object_type)

    @property
    def is_analysis(self) -> bool:
        return self.category == RoiCategory.ANALYSIS

    def with_updated(self, **changes) -> "RoiDefinition":
        values = {
            "roi_id": self.roi_id, "camera_id": self.camera_id,
            "ptz_id": self.ptz_id, "position_id": self.position_id,
            "object_type": self.object_type, "geometry": self.geometry,
            "name": self.name, "enabled": self.enabled, "visible": self.visible,
            "created_at": self.created_at, "updated_at": time.time(),
            "schema_version": self.schema_version,
            "analysis_config": self.analysis_config,
            "alarm_rule_ref": self.alarm_rule_ref, "display": self.display,
        }
        values.update(changes)
        # Ownership is immutable: binding changes require copy_to().
        for key in ("camera_id", "ptz_id", "position_id"):
            if values[key] != getattr(self, key):
                raise RoiBindingError(
                    f"cannot change {key} in place; use copy_to() for an explicit move")
        return RoiDefinition(**values)

    def copy_to(self, *, camera_id: str, ptz_id: str, position_id: str,
                new_roi_id: str = "") -> "RoiDefinition":
        """Explicit ownership transfer producing a new object identity."""
        return RoiDefinition(
            roi_id=new_roi_id or generate_roi_id(),
            camera_id=camera_id, ptz_id=ptz_id, position_id=position_id,
            object_type=self.object_type, geometry=self.geometry,
            name=self.name, enabled=self.enabled, visible=self.visible,
            created_at=time.time(), updated_at=time.time(),
            schema_version=self.schema_version,
            analysis_config=self.analysis_config,
            alarm_rule_ref=self.alarm_rule_ref, display=self.display)


def geometry_expected_type(object_type: RoiObjectType):  # noqa: ANN201
    from thermal_monitor.roi.geometry import GEOMETRY_BY_TYPE
    return GEOMETRY_BY_TYPE[object_type]


@dataclass(frozen=True, slots=True)
class RoiMeasurement:
    """One evaluated measurement stamped with its full context."""

    roi_id: str
    camera_id: str
    ptz_id: str
    position_id: str
    context_generation: int
    session_generation: int
    frame_id: int
    frame_timestamp: float
    processed_at: float
    kind: str
    values: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({}))
    valid: bool = True
    error: str = ""

    def __post_init__(self) -> None:
        if not self.roi_id or not self.camera_id or not self.position_id:
            raise RoiValidationError("measurement roi/camera/position ids are required")


__all__ = [
    "SCHEMA_VERSION", "RoiDefinition", "RoiMeasurement",
    "generate_roi_id", "geometry_expected_type",
]
