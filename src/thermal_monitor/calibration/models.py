"""
V3 Calibration Data Models

Matches V2 proven calibration structure:
- UniverseSegment: Inverse quadratic polynomial segment (raw = u0 + u1*T + u2*T²)
- CalibrationRange: Temperature range with multiple segments
- CameraCalibration: Complete per-camera calibration with lookup tables
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

import numpy as np


@dataclass(slots=True)
class UniverseSegment:
    """
    One inverse polynomial segment.

    Forward equation (camera): raw = u0 + u1*T + u2*T²
    Inverse (conversion): solve u2*T² + u1*T + (u0 - raw) = 0 for T
    """

    u0: float
    u1: float
    u2: float
    start_temp: float
    end_temp: float


@dataclass(slots=True)
class CalibrationRange:
    """One camera calibration range."""

    calibration_min: float
    calibration_max: float
    display_min: float
    display_max: float
    manual_palette_span: float
    auto_palette_span: float
    num_segments: int
    segments: List[UniverseSegment] = field(default_factory=list)


@dataclass(slots=True)
class CameraCalibration:
    """Complete calibration information for one camera."""

    # Header
    magic: int = 0
    enabled_ranges: int = 0
    enabled_mask: int = 0
    calibration_date: str = ""

    # Calibration ranges
    ranges: List[CalibrationRange] = field(default_factory=list)

    # Cached lookup tables (range_index -> LUT array)
    lookup_tables: Dict[int, np.ndarray] = field(default_factory=dict)

    def get_range(self, index: int) -> CalibrationRange:
        return self.ranges[index]

    def add_range(self, calibration_range: CalibrationRange) -> None:
        self.ranges.append(calibration_range)

    def set_lookup_table(self, range_index: int, lut: np.ndarray) -> None:
        self.lookup_tables[range_index] = lut

    def get_lookup_table(self, range_index: int) -> np.ndarray | None:
        return self.lookup_tables.get(range_index)

    def has_lookup_table(self, range_index: int) -> bool:
        return range_index in self.lookup_tables