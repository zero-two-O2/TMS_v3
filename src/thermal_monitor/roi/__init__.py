"""roi package -- ThermoView-style ROI/analysis tools with camera+PTZ binding."""

from thermal_monitor.roi.loading import load_rois_for_position, new_operation_id
from thermal_monitor.roi.context import RoiActiveContext, inactive_context
from thermal_monitor.roi.coordinate_system import ViewportMapping
from thermal_monitor.roi.editor import RoiEditor, EditorCommand
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
from thermal_monitor.roi.handles import (
    clamp_to_image,
    handles_for,
    move_geometry,
    resize_with_handle,
)
from thermal_monitor.roi.io import export_collection, import_collection, preview_collection
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
from thermal_monitor.roi.repository import RoiDefinitionRepository
from thermal_monitor.roi.serialization import roi_from_dict, roi_to_dict
from thermal_monitor.roi.validation import check_position_belongs_to_camera, check_roi_binding

__all__ = [
    "load_rois_for_position", "new_operation_id",
    "RoiActiveContext", "inactive_context",
    "ViewportMapping",
    "RoiEditor", "EditorCommand",
    "clamp_to_image", "handles_for", "move_geometry", "resize_with_handle",
    "export_collection", "import_collection", "preview_collection",
    "RoiCategory", "RoiObjectType", "category_of",
    "RoiBindingError", "RoiError", "RoiPersistenceError",
    "RoiStaleContextError", "RoiValidationError",
    "RoiEvaluator",
    "geometry_from_dict",
    "area_statistics", "coldest_in_region", "hottest_in_region",
    "line_profile", "measure_angle", "ruler_distance", "sample_spot",
    "RoiDefinition", "RoiMeasurement", "generate_roi_id",
    "RoiDefinitionRepository",
    "roi_from_dict", "roi_to_dict",
    "check_position_belongs_to_camera", "check_roi_binding",
]
