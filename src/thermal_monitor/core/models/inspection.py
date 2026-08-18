"""
core.models.inspection -- Inspection/ROI/Analysis domain models.

Models for ROIs, temperature analysis, alarm rules, and analysis results.
Independent of HALCON, PyQt, and SQL Server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Sequence

import math
import numpy as np


class ROIType(str, Enum):
    """Type of ROI geometry."""

    RECTANGLE = "rectangle"
    CIRCLE = "circle"
    POLYGON = "polygon"
    LINE = "line"
    POINT = "point"


class ROIShape(str, Enum):
    """ROI shape types matching HALCON region generators."""

    RECTANGLE1 = "rectangle1"
    RECTANGLE2 = "rectangle2"
    CIRCLE = "circle"
    ELLIPSE = "ellipse"
    POLYGON = "polygon"


class TemperatureUnit(str, Enum):
    """Temperature unit for limits and display."""

    CELSIUS = "celsius"
    FAHRENHEIT = "fahrenheit"
    KELVIN = "kelvin"

    @staticmethod
    def convert(value: float, from_unit: TemperatureUnit, to_unit: TemperatureUnit) -> float:
        # Convert to Kelvin first
        if from_unit == TemperatureUnit.CELSIUS:
            k = value + 273.15
        elif from_unit == TemperatureUnit.FAHRENHEIT:
            k = (value + 459.67) * 5 / 9
        else:
            k = value

        # Convert from Kelvin
        if to_unit == TemperatureUnit.CELSIUS:
            return k - 273.15
        elif to_unit == TemperatureUnit.FAHRENHEIT:
            return k * 9 / 5 - 459.67
        else:
            return k


class AlarmSeverity(str, Enum):
    """Alarm severity levels."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlarmCondition(str, Enum):
    """Alarm condition type."""

    ABOVE = "above"
    BELOW = "below"
    OUTSIDE_RANGE = "outside_range"
    INSIDE_RANGE = "inside_range"
    RATE_OF_CHANGE = "rate_of_change"


@dataclass(frozen=True, slots=True)
class ROIGeometry:
    """ROI geometric definition using HALCON row/column convention.

    All coordinates are in image pixel coordinates (row = y, column = x).
    This matches HALCON's native coordinate system.

    Supported shapes and their parameters:
    - RECTANGLE1: y1, x1, y2, x2 (axis-aligned rectangle, two corners)
    - RECTANGLE2: center_y, center_x, phi, length1, length2 (rotated rectangle)
    - CIRCLE: center_y, center_x, radius
    - ELLIPSE: center_y, center_x, phi, radius1, radius2
    - POLYGON: points = [(y1, x1), (y2, x2), ...] (list of row/col tuples)
    """

    shape: ROIShape
    # Parameters per shape (all values in pixels, row/col convention):
    # RECTANGLE1: {"y1": float, "x1": float, "y2": float, "x2": float}
    # RECTANGLE2: {"center_y": float, "center_x": float, "phi": float, "length1": float, "length2": float}
    # CIRCLE: {"center_y": float, "center_x": float, "radius": float}
    # ELLIPSE: {"center_y": float, "center_x": float, "phi": float, "radius1": float, "radius2": float}
    # POLYGON: {"points": list[tuple[float, float]]}
    parameters: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if self.shape == ROIShape.RECTANGLE1:
            required = {"y1", "x1", "y2", "x2"}
            if not required.issubset(self.parameters.keys()):
                raise ValueError(f"Rectangle1 ROI requires {required}")
            y1 = float(self.parameters["y1"])
            x1 = float(self.parameters["x1"])
            y2 = float(self.parameters["y2"])
            x2 = float(self.parameters["x2"])
            if y1 > y2:
                raise ValueError(f"Rectangle1 ROI: y1 ({y1}) > y2 ({y2})")
            if x1 > x2:
                raise ValueError(f"Rectangle1 ROI: x1 ({x1}) > x2 ({x2})")
        elif self.shape == ROIShape.RECTANGLE2:
            required = {"center_y", "center_x", "phi", "length1", "length2"}
            if not required.issubset(self.parameters.keys()):
                raise ValueError(f"Rectangle2 ROI requires {required}")
            length1 = float(self.parameters["length1"])
            length2 = float(self.parameters["length2"])
            if length1 <= 0:
                raise ValueError(f"Rectangle2 ROI: length1 ({length1}) must be > 0")
            if length2 <= 0:
                raise ValueError(f"Rectangle2 ROI: length2 ({length2}) must be > 0")
        elif self.shape == ROIShape.CIRCLE:
            required = {"center_y", "center_x", "radius"}
            if not required.issubset(self.parameters.keys()):
                raise ValueError(f"Circle ROI requires {required}")
            radius = float(self.parameters["radius"])
            if radius <= 0:
                raise ValueError(f"Circle ROI: radius ({radius}) must be > 0")
        elif self.shape == ROIShape.ELLIPSE:
            required = {"center_y", "center_x", "phi", "radius1", "radius2"}
            if not required.issubset(self.parameters.keys()):
                raise ValueError(f"Ellipse ROI requires {required}")
            radius1 = float(self.parameters["radius1"])
            radius2 = float(self.parameters["radius2"])
            if radius1 <= 0:
                raise ValueError(f"Ellipse ROI: radius1 ({radius1}) must be > 0")
            if radius2 <= 0:
                raise ValueError(f"Ellipse ROI: radius2 ({radius2}) must be > 0")
        elif self.shape == ROIShape.POLYGON:
            if "points" not in self.parameters:
                raise ValueError("Polygon ROI requires 'points' parameter")
            points = self.parameters["points"]
            if not isinstance(points, (list, tuple)):
                raise ValueError("Polygon ROI 'points' must be a list or tuple")
            if len(points) < 3:
                raise ValueError(f"Polygon ROI requires at least 3 points, got {len(points)}")
            for i, pt in enumerate(points):
                if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                    raise ValueError(f"Polygon ROI point {i} must be a (y, x) pair")
                y, x = pt
                if not (isinstance(y, (int, float)) and isinstance(x, (int, float))):
                    raise ValueError(f"Polygon ROI point {i} coordinates must be numeric")
                if not (math.isfinite(y) and math.isfinite(x)):
                    raise ValueError(f"Polygon ROI point {i} has non-finite coordinates ({y}, {x})")

    def bounding_box(self) -> tuple[float, float, float, float]:
        """Return (y_min, x_min, y_max, x_max) bounding box in row/col coordinates."""
        import math
        if self.shape == ROIShape.RECTANGLE1:
            return (
                float(self.parameters["y1"]),
                float(self.parameters["x1"]),
                float(self.parameters["y2"]),
                float(self.parameters["x2"]),
            )
        elif self.shape == ROIShape.RECTANGLE2:
            center_y = float(self.parameters["center_y"])
            center_x = float(self.parameters["center_x"])
            phi = float(self.parameters["phi"])
            length1 = float(self.parameters["length1"])
            length2 = float(self.parameters["length2"])
            cos_p = abs(math.cos(phi))
            sin_p = abs(math.sin(phi))
            half_row = length1 * sin_p + length2 * cos_p
            half_col = length1 * cos_p + length2 * sin_p
            return (
                center_y - half_row,
                center_x - half_col,
                center_y + half_row,
                center_x + half_col,
            )
        elif self.shape == ROIShape.CIRCLE:
            center_y = float(self.parameters["center_y"])
            center_x = float(self.parameters["center_x"])
            radius = float(self.parameters["radius"])
            return (
                center_y - radius,
                center_x - radius,
                center_y + radius,
                center_x + radius,
            )
        elif self.shape == ROIShape.ELLIPSE:
            center_y = float(self.parameters["center_y"])
            center_x = float(self.parameters["center_x"])
            phi = float(self.parameters["phi"])
            radius1 = float(self.parameters["radius1"])
            radius2 = float(self.parameters["radius2"])
            cos_p = abs(math.cos(phi))
            sin_p = abs(math.sin(phi))
            half_row = radius1 * sin_p + radius2 * cos_p
            half_col = radius1 * cos_p + radius2 * sin_p
            return (
                center_y - half_row,
                center_x - half_col,
                center_y + half_row,
                center_x + half_col,
            )
        elif self.shape == ROIShape.POLYGON:
            points = self.parameters["points"]
            ys = [float(p[0]) for p in points]
            xs = [float(p[1]) for p in points]
            return (min(ys), min(xs), max(ys), max(xs))
        return (0.0, 0.0, 0.0, 0.0)

    def contains_point(self, x: float, y: float) -> bool:
        """Check if a point is inside the ROI (simplified, no rotation).
        
        Args:
            x: Column coordinate (UI convention)
            y: Row coordinate (UI convention)
        """
        if self.shape == ROIShape.RECTANGLE1:
            y1 = float(self.parameters["y1"])
            x1 = float(self.parameters["x1"])
            y2 = float(self.parameters["y2"])
            x2 = float(self.parameters["x2"])
            return x1 <= x <= x2 and y1 <= y <= y2
        elif self.shape == ROIShape.CIRCLE:
            center_y = float(self.parameters["center_y"])
            center_x = float(self.parameters["center_x"])
            radius = float(self.parameters["radius"])
            return (y - center_y) ** 2 + (x - center_x) ** 2 <= radius ** 2
        elif self.shape == ROIShape.POINT:
            return abs(x - float(self.parameters["center_x"])) < 1 and abs(y - float(self.parameters["center_y"])) < 1
        # For polygon/line/ellipse/rectangle2, simplified check
        return False


@dataclass(frozen=True, slots=True)
class TemperatureLimits:
    """Temperature alarm limits for an ROI."""

    unit: TemperatureUnit = TemperatureUnit.CELSIUS
    min_warning: float | None = None
    max_warning: float | None = None
    min_critical: float | None = None
    max_critical: float | None = None
    rate_of_change_limit: float | None = None  # degrees per second

    def __post_init__(self) -> None:
        if self.min_warning is not None and self.max_warning is not None:
            if self.min_warning >= self.max_warning:
                raise ValueError("min_warning must be < max_warning")
        if self.min_critical is not None and self.max_critical is not None:
            if self.min_critical >= self.max_critical:
                raise ValueError("min_critical must be < max_critical")

    def evaluate(self, temperature: float, prev_temperature: float | None = None, dt: float | None = None) -> AlarmSeverity:
        """Evaluate temperature against limits."""
        # Check critical first
        if self.min_critical is not None and temperature <= self.min_critical:
            return AlarmSeverity.CRITICAL
        if self.max_critical is not None and temperature >= self.max_critical:
            return AlarmSeverity.CRITICAL
        # Check warning
        if self.min_warning is not None and temperature <= self.min_warning:
            return AlarmSeverity.WARNING
        if self.max_warning is not None and temperature >= self.max_warning:
            return AlarmSeverity.WARNING
        # Check rate of change
        if self.rate_of_change_limit is not None and prev_temperature is not None and dt is not None and dt > 0:
            rate = abs(temperature - prev_temperature) / dt
            if rate >= self.rate_of_change_limit:
                return AlarmSeverity.WARNING
        return AlarmSeverity.INFO

    def is_configured(self) -> bool:
        return any(v is not None for v in (
            self.min_warning, self.max_warning,
            self.min_critical, self.max_critical,
            self.rate_of_change_limit
        ))


@dataclass(frozen=True, slots=True)
class ROIConfig:
    """Complete ROI configuration including geometry and alarm limits."""

    roi_id: str
    name: str = ""
    enabled: bool = True
    geometry: ROIGeometry = field(default_factory=lambda: ROIGeometry(
        shape=ROIShape.RECTANGLE1,
        parameters=MappingProxyType({"y1": 0.0, "x1": 0.0, "y2": 100.0, "x2": 100.0})
    ))
    temperature_limits: TemperatureLimits = field(default_factory=TemperatureLimits)
    alarm_enabled: bool = True
    metadata: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not self.roi_id:
            raise ValueError("roi_id is required")


@dataclass(frozen=True, slots=True)
class PositionROIAssociation:
    """Association between a PTZ position and ROI configurations.

    A camera position (preset) can have multiple ROIs.
    """

    position_id: str  # References PTZPosition.name or preset_id
    position_name: str = ""
    roi_ids: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.position_id:
            raise ValueError("position_id is required")


@dataclass(frozen=True, slots=True)
class AnalysisConfig:
    """Configuration for the analysis pipeline."""

    camera_id: str
    rois: Mapping[str, ROIConfig] = field(default_factory=lambda: MappingProxyType({}))
    position_associations: Mapping[str, PositionROIAssociation] = field(default_factory=lambda: MappingProxyType({}))
    alarm_rules: Mapping[str, AlarmRule] = field(default_factory=lambda: MappingProxyType({}))

    def get_rois_for_position(self, position_id: str) -> Sequence[ROIConfig]:
        assoc = self.position_associations.get(position_id)
        if assoc is None:
            return ()
        return tuple(self.rois[roi_id] for roi_id in assoc.roi_ids if roi_id in self.rois)
    default_emissivity: float = 0.95
    ambient_temperature: float = 25.0
    distance: float = 1.0
    humidity: float = 50.0
    reflected_temperature: float = 20.0
    unit: TemperatureUnit = TemperatureUnit.CELSIUS
    metadata: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not self.camera_id:
            raise ValueError("camera_id is required")
        if not (0.0 < self.default_emissivity <= 1.0):
            raise ValueError("emissivity must be in (0, 1]")

    def get_rois_for_position(self, position_id: str) -> Sequence[ROIConfig]:
        assoc = self.position_associations.get(position_id)
        if assoc is None:
            return ()
        return tuple(self.rois[roi_id] for roi_id in assoc.roi_ids if roi_id in self.rois)


@dataclass(frozen=True, slots=True)
class ROIStatistics:
    """Statistical results for one ROI."""

    roi_id: str
    roi_name: str
    min_temp: float
    max_temp: float
    mean_temp: float
    deviation: float  # standard deviation (proven V2 name)
    unit: TemperatureUnit = TemperatureUnit.CELSIUS

    @property
    def range_temp(self) -> float:
        return self.max_temp - self.min_temp

    @property
    def std_temp(self) -> float:
        """Alias for deviation for backward compatibility."""
        return self.deviation


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Result of analyzing one frame.

    Contains per-ROI statistics and overall frame analysis metadata.
    """

    camera_id: str
    frame_sequence: int
    frame_timestamp: float
    roi_results: Mapping[str, ROIStatistics] = field(default_factory=lambda: MappingProxyType({}))
    overall_min: float | None = None
    overall_max: float | None = None
    overall_mean: float | None = None
    unit: TemperatureUnit = TemperatureUnit.CELSIUS
    processing_time_ms: float = 0.0
    metadata: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def has_alarms(self) -> bool:
        # This would be evaluated by AlarmEvaluator, not stored here
        return False


@dataclass(frozen=True, slots=True)
class AlarmRule:
    """Alarm rule definition for an ROI."""

    rule_id: str
    roi_id: str
    condition: AlarmCondition
    severity: AlarmSeverity
    threshold: float
    threshold_low: float | None = None  # For OUTSIDE_RANGE/INSIDE_RANGE
    threshold_high: float | None = None
    unit: TemperatureUnit = TemperatureUnit.CELSIUS
    enabled: bool = True
    description: str = ""
    metadata: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not self.rule_id:
            raise ValueError("rule_id is required")
        if not self.roi_id:
            raise ValueError("roi_id is required")
        if self.condition in (AlarmCondition.OUTSIDE_RANGE, AlarmCondition.INSIDE_RANGE):
            if self.threshold_low is None or self.threshold_high is None:
                raise ValueError(f"{self.condition.value} requires threshold_low and threshold_high")
            if self.threshold_low >= self.threshold_high:
                raise ValueError("threshold_low must be < threshold_high")
        else:
            if self.threshold is None:
                raise ValueError(f"{self.condition.value} requires threshold")


@dataclass(frozen=True, slots=True)
class AlarmEvent:
    """Alarm event generated when a rule is triggered."""

    event_id: str
    rule_id: str
    camera_id: str
    roi_id: str
    severity: AlarmSeverity
    measured_value: float
    threshold_value: float
    timestamp: float
    frame_sequence: int
    position_id: str | None = None
    acknowledged: bool = False
    acknowledged_at: float | None = None
    acknowledged_by: str | None = None
    metadata: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("event_id is required")