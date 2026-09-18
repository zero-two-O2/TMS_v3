"""roi package -- ThermoView-style ROI/analysis tools with camera+PTZ binding."""

from thermal_monitor.roi.activation import RoiActivationPublisher, new_operation_id
from thermal_monitor.roi.context import RoiActiveContext, inactive_context
from thermal_monitor.roi.coordinate_system import ViewportMapping
from thermal_monitor.roi.enums import RoiCategory, RoiObjectType, category_of
from thermal_monitor.roi.errors import (
    RoiBindingError,
    RoiError,
    RoiPersistenceError,
    RoiStaleContextError,
    RoiValidationError,
)
from thermal_monitor.roi.evaluator import RoiEvaluator
from thermal_monitor.roi.geometry import geometry_from_dict
from thermal_monitor.roi.measurements import (
    area_statistics,
    coldest_in_region,
    hottest_in_region,
    line_profile,
    measure_angle,
    ruler_distance,
    sample_spot,
)
from thermal_monitor.roi.models import RoiDefinition, RoiMeasurement, generate_roi_id
from thermal_monitor.roi.registry import RoiContextRegistry
from thermal_monitor.roi.repository import RoiDefinitionRepository
from thermal_monitor.roi.serialization import roi_from_dict, roi_to_dict
from thermal_monitor.roi.validation import check_position_belongs_to_camera, check_roi_binding

__all__ = [
    "RoiActivationPublisher", "new_operation_id",
    "RoiActiveContext", "inactive_context",
    "ViewportMapping",
    "RoiCategory", "RoiObjectType", "category_of",
    "RoiBindingError", "RoiError", "RoiPersistenceError",
    "RoiStaleContextError", "RoiValidationError",
    "RoiEvaluator",
    "geometry_from_dict",
    "area_statistics", "coldest_in_region", "hottest_in_region",
    "line_profile", "measure_angle", "ruler_distance", "sample_spot",
    "RoiDefinition", "RoiMeasurement", "generate_roi_id",
    "RoiContextRegistry", "RoiDefinitionRepository",
    "roi_from_dict", "roi_to_dict",
    "check_position_belongs_to_camera", "check_roi_binding",
]
