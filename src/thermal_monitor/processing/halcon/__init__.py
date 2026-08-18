"""
processing.halcon -- HALCON integration for thermal monitoring.
"""

from __future__ import annotations

from thermal_monitor.processing.halcon.roi_adapter import (
    HalconROIAdapter,
    process_rois_with_halcon,
)

__all__ = [
    "HalconROIAdapter",
    "process_rois_with_halcon",
]