"""halcon package -- HALCON boundary for Phase 10 ROI tools.

All HALCON handles stay inside this package. Application code passes
immutable NumPy snapshots in and receives typed results out.
"""

from thermal_monitor.halcon.adapter import HalconSnapshotRunner, halcon_available
from thermal_monitor.halcon.geometry import halcon_params_for, verify_image_for_halcon

__all__ = ["HalconSnapshotRunner", "halcon_available", "halcon_params_for",
           "verify_image_for_halcon"]
