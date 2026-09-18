"""
processing.halcon -- HALCON integration for thermal monitoring.
"""

from __future__ import annotations

from thermal_monitor.processing.halcon.grouped import (
    SHAPE_ORDER,
    InvalidRoi,
    ShapeGroup,
    build_shape_groups,
)
from thermal_monitor.processing.halcon.region_cache import (
    CacheStats,
    RegionCache,
)
from thermal_monitor.processing.halcon.roi_adapter import (
    BATCHED_METHOD,
    HalconROIAdapter,
    process_rois_with_halcon,
)

__all__ = [
    "BATCHED_METHOD",
    "CacheStats",
    "HalconROIAdapter",
    "InvalidRoi",
    "RegionCache",
    "SHAPE_ORDER",
    "ShapeGroup",
    "build_shape_groups",
    "process_rois_with_halcon",
]