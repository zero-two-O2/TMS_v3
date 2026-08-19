"""
V2 vs V3 Calibration Comparison Test

Validates that V3 implementation produces identical output to V2 reference
implementation using the same calibration blob and raw input data.

These tests are conditionally collected - they only run when the V2 reference
package is available at reference/TMS_v2/calibration/.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Import V3 implementation
from thermal_monitor.calibration.models import (
    CameraCalibration as V3CameraCalibration,
    CalibrationRange as V3CalibrationRange,
    UniverseSegment as V3UniverseSegment,
)
from thermal_monitor.calibration.parser import CalibrationParser as V3CalibrationParser
from thermal_monitor.calibration.processor import CalibrationProcessor as V3CalibrationProcessor

# Conditionally import V2 implementation from reference
# This allows tests to gracefully skip when V2 reference is not present
v2_path = Path(__file__).resolve().parent.parent / "reference" / "TMS_v2"
if v2_path.exists():
    sys.path.insert(0, str(v2_path))
    try:
        from calibration.calibration_models import (
            CameraCalibration as V2CameraCalibration,
            CalibrationRange as V2CalibrationRange,
            UniverseSegment as V2UniverseSegment,
        )
        from calibration.calibration_parser import CalibrationParser as V2CalibrationParser
        from calibration.calibration_processor import CalibrationProcessor as V2CalibrationProcessor
        V2_AVAILABLE = True
    except ImportError:
        V2_AVAILABLE = False
else:
    V2_AVAILABLE = False

# Skip all tests in this module if V2 reference is not available
pytestmark = pytest.mark.skipif(not V2_AVAILABLE, reason="V2 reference package not available")

V2_CALIBRATION_FILE = Path("reference/TMS_v2/assets/calibration/calibration_blob.txt")


def test_v2_v3_parser_output():
    """Test that V3 parser produces same calibration data as V2 parser."""
    if not V2_CALIBRATION_FILE.exists():
        pytest.skip(f"Calibration file not found: {V2_CALIBRATION_FILE}")

    v2_parser = V2CalibrationParser()
    v2_calibration = v2_parser.load(V2_CALIBRATION_FILE)

    v3_parser = V3CalibrationParser()
    v3_calibration = v3_parser.load(V2_CALIBRATION_FILE)

    # Compare header
    assert v3_calibration.magic == v2_calibration.magic
    assert v3_calibration.enabled_ranges == v2_calibration.enabled_ranges
    assert v3_calibration.enabled_mask == v2_calibration.enabled_mask
    assert v3_calibration.calibration_date == v2_calibration.calibration_date

    # Compare ranges
    assert len(v3_calibration.ranges) == len(v2_calibration.ranges)

    for i, (v2_range, v3_range) in enumerate(zip(v2_calibration.ranges, v3_calibration.ranges)):
        assert v3_range.calibration_min == v2_range.calibration_min
        assert v3_range.calibration_max == v2_range.calibration_max
        assert v3_range.display_min == v2_range.display_min
        assert v3_range.display_max == v2_range.display_max
        assert v3_range.manual_palette_span == v2_range.manual_palette_span
        assert v3_range.auto_palette_span == v2_range.auto_palette_span
        assert v3_range.num_segments == v2_range.num_segments

        # Compare segments
        assert len(v3_range.segments) == len(v2_range.segments)
        for j, (v2_seg, v3_seg) in enumerate(zip(v2_range.segments, v3_range.segments)):
            assert v3_seg.u0 == v2_seg.u0
            assert v3_seg.u1 == v2_seg.u1
            assert v3_seg.u2 == v2_seg.u2
            assert v3_seg.start_temp == v2_seg.start_temp
            assert v3_seg.end_temp == v2_seg.end_temp


def test_v2_v3_lut_generation():
    """Test that V3 LUT generation matches V2 exactly."""
    if not V2_CALIBRATION_FILE.exists():
        pytest.skip(f"Calibration file not found: {V2_CALIBRATION_FILE}")

    v2_parser = V2CalibrationParser()
    v2_calibration = v2_parser.load(V2_CALIBRATION_FILE)
    V2CalibrationProcessor.build_lookup_tables(v2_calibration)

    v3_parser = V3CalibrationParser()
    v3_calibration = v3_parser.load(V2_CALIBRATION_FILE)
    V3CalibrationProcessor.build_lookup_tables(v3_calibration)

    # Compare LUTs
    for i in range(v2_calibration.enabled_ranges):
        v2_lut = v2_calibration.get_lookup_table(i)
        v3_lut = v3_calibration.get_lookup_table(i)

        assert v2_lut is not None, f"V2 LUT {i} is None"
        assert v3_lut is not None, f"V3 LUT {i} is None"

        # Check dtype and size
        assert v3_lut.dtype == v2_lut.dtype == np.float32
        assert v3_lut.size == v2_lut.size == 65536

        # Compare values - should be identical
        # Allow tiny floating point differences due to operation order
        diff = np.abs(v3_lut - v2_lut)
        max_diff = np.nanmax(diff[np.isfinite(diff)])
        assert max_diff < 1e-5, f"LUT {i} max difference: {max_diff}"

        # Count NaN positions (should be same)
        v2_nan = np.isnan(v2_lut)
        v3_nan = np.isnan(v3_lut)
        assert np.array_equal(v2_nan, v3_nan), f"LUT {i} NaN positions differ"


def test_v2_v3_raw_to_temperature():
    """Test that V3 raw_to_temperature matches V2 exactly."""
    if not V2_CALIBRATION_FILE.exists():
        pytest.skip(f"Calibration file not found: {V2_CALIBRATION_FILE}")

    v2_parser = V2CalibrationParser()
    v2_calibration = v2_parser.load(V2_CALIBRATION_FILE)
    V2CalibrationProcessor.build_lookup_tables(v2_calibration)

    v3_parser = V3CalibrationParser()
    v3_calibration = v3_parser.load(V2_CALIBRATION_FILE)
    V3CalibrationProcessor.build_lookup_tables(v3_calibration)

    # Test with deterministic raw data
    np.random.seed(42)
    raw = np.random.randint(0, 65535, (480, 640), dtype=np.uint16)

    for i in range(v2_calibration.enabled_ranges):
        v2_temp = V2CalibrationProcessor.raw_to_temperature(raw, v2_calibration, i)
        v3_temp = V3CalibrationProcessor.raw_to_temperature(raw, v3_calibration, i)

        assert v3_temp.dtype == v2_temp.dtype == np.float32
        assert v3_temp.shape == v2_temp.shape == raw.shape

        # Compare values
        diff = np.abs(v3_temp - v2_temp)
        max_diff = np.nanmax(diff[np.isfinite(diff)])
        assert max_diff < 1e-5, f"Range {i} max difference: {max_diff}"

        # NaN positions should match
        v2_nan = np.isnan(v2_temp)
        v3_nan = np.isnan(v3_temp)
        assert np.array_equal(v2_nan, v3_nan), f"Range {i} NaN positions differ"


def test_v2_v3_temperature_to_display():
    """Test that V3 temperature_to_display matches V2."""
    if not V2_CALIBRATION_FILE.exists():
        pytest.skip(f"Calibration file not found: {V2_CALIBRATION_FILE}")

    # Use a known temperature image
    np.random.seed(123)
    temperature = np.random.uniform(-20, 1200, (480, 640)).astype(np.float32)
    # Add some NaN values
    temperature[100:110, 100:110] = np.nan

    v2_display = V2CalibrationProcessor.temperature_to_display(temperature)
    v3_display = V3CalibrationProcessor.temperature_to_display(temperature)

    assert v3_display.dtype == v2_display.dtype == np.uint8
    assert v3_display.shape == v2_display.shape == temperature.shape

    # Values should match exactly
    assert np.array_equal(v3_display, v2_display)


def test_v2_v3_raw_to_display():
    """Test that V3 raw_to_display matches V2."""
    if not V2_CALIBRATION_FILE.exists():
        pytest.skip(f"Calibration file not found: {V2_CALIBRATION_FILE}")

    v2_parser = V2CalibrationParser()
    v2_calibration = v2_parser.load(V2_CALIBRATION_FILE)
    V2CalibrationProcessor.build_lookup_tables(v2_calibration)

    v3_parser = V3CalibrationParser()
    v3_calibration = v3_parser.load(V2_CALIBRATION_FILE)
    V3CalibrationProcessor.build_lookup_tables(v3_calibration)

    np.random.seed(456)
    raw = np.random.randint(0, 65535, (480, 640), dtype=np.uint16)

    for i in range(v2_calibration.enabled_ranges):
        v2_display = V2CalibrationProcessor.raw_to_display(raw, v2_calibration, i)
        v3_display = V3CalibrationProcessor.raw_to_display(raw, v3_calibration, i)

        assert v3_display.dtype == v2_display.dtype == np.uint8
        assert v3_display.shape == v2_display.shape == raw.shape

        # Values should match exactly
        assert np.array_equal(v3_display, v2_display), f"Range {i} display differs"


def test_v2_v3_statistics():
    """Test that V3 statistics match V2."""
    if not V2_CALIBRATION_FILE.exists():
        pytest.skip(f"Calibration file not found: {V2_CALIBRATION_FILE}")

    v2_parser = V2CalibrationParser()
    v2_calibration = v2_parser.load(V2_CALIBRATION_FILE)
    V2CalibrationProcessor.build_lookup_tables(v2_calibration)

    v3_parser = V3CalibrationParser()
    v3_calibration = v3_parser.load(V2_CALIBRATION_FILE)
    V3CalibrationProcessor.build_lookup_tables(v3_calibration)

    np.random.seed(789)
    raw = np.random.randint(0, 65535, (480, 640), dtype=np.uint16)
    v2_temp = V2CalibrationProcessor.raw_to_temperature(raw, v2_calibration, 0)
    v3_temp = V3CalibrationProcessor.raw_to_temperature(raw, v3_calibration, 0)

    v2_stats = V2CalibrationProcessor.get_temperature_statistics(v2_temp)
    v3_stats = V3CalibrationProcessor.get_temperature_statistics(v3_temp)

    for key in ("minimum", "maximum", "mean", "median", "std"):
        assert key in v3_stats
        assert key in v2_stats
        # Allow tiny floating point differences
        assert abs(v3_stats[key] - v2_stats[key]) < 1e-4, f"{key}: V3={v3_stats[key]}, V2={v2_stats[key]}"


def test_v2_v3_roi_statistics():
    """Test that V3 ROI statistics match V2."""
    if not V2_CALIBRATION_FILE.exists():
        pytest.skip(f"Calibration file not found: {V2_CALIBRATION_FILE}")

    v2_parser = V2CalibrationParser()
    v2_calibration = v2_parser.load(V2_CALIBRATION_FILE)
    V2CalibrationProcessor.build_lookup_tables(v2_calibration)

    v3_parser = V3CalibrationParser()
    v3_calibration = v3_parser.load(V2_CALIBRATION_FILE)
    V3CalibrationProcessor.build_lookup_tables(v3_calibration)

    np.random.seed(999)
    raw = np.random.randint(0, 65535, (480, 640), dtype=np.uint16)
    v2_temp = V2CalibrationProcessor.raw_to_temperature(raw, v2_calibration, 0)
    v3_temp = V3CalibrationProcessor.raw_to_temperature(raw, v3_calibration, 0)

    mask = np.zeros(v2_temp.shape, dtype=bool)
    mask[150:300, 200:450] = True

    v2_stats = V2CalibrationProcessor.get_roi_statistics(v2_temp, mask)
    v3_stats = V3CalibrationProcessor.get_roi_statistics(v3_temp, mask)

    for key in ("minimum", "maximum", "mean", "median", "std"):
        assert key in v3_stats
        assert key in v2_stats
        assert abs(v3_stats[key] - v2_stats[key]) < 1e-4, f"{key}: V3={v3_stats[key]}, V2={v2_stats[key]}"


def test_v2_v3_segment_solver():
    """Test that V3 polynomial solver matches V2."""
    # Create identical segment
    v2_segment = V2UniverseSegment(u0=1000.0, u1=50.0, u2=0.1, start_temp=-20.0, end_temp=80.0)
    v3_segment = V3UniverseSegment(u0=1000.0, u1=50.0, u2=0.1, start_temp=-20.0, end_temp=80.0)

    # Test various raw values
    test_values = [1000.0, 2040.0, 5000.0, 10000.0, 20000.0, 30000.0, 65535.0]

    for raw_val in test_values:
        v2_result = V2CalibrationProcessor.solve_polynomial(raw_val, v2_segment)
        v3_result = V3CalibrationProcessor.solve_polynomial(raw_val, v3_segment)

        if v2_result is None:
            assert v3_result is None, f"V2=None but V3={v3_result} for raw={raw_val}"
        else:
            assert v3_result is not None, f"V3=None but V2={v2_result} for raw={raw_val}"
            assert abs(v3_result - v2_result) < 1e-6, f"Mismatch: V3={v3_result}, V2={v2_result}"


def test_v2_v3_raw_value_to_temperature():
    """Test that V3 raw_value_to_temperature matches V2."""
    if not V2_CALIBRATION_FILE.exists():
        pytest.skip(f"Calibration file not found: {V2_CALIBRATION_FILE}")

    v2_parser = V2CalibrationParser()
    v2_calibration = v2_parser.load(V2_CALIBRATION_FILE)

    v3_parser = V3CalibrationParser()
    v3_calibration = v3_parser.load(V2_CALIBRATION_FILE)

    v2_range = v2_calibration.ranges[0]
    v3_range = v3_calibration.ranges[0]

    # Test specific raw values
    test_values = [0, 1000, 5000, 10000, 20000, 32768, 40000, 50000, 65535]

    for raw_val in test_values:
        v2_result = V2CalibrationProcessor.raw_value_to_temperature(raw_val, v2_range)
        v3_result = V3CalibrationProcessor.raw_value_to_temperature(raw_val, v3_range)

        if v2_result is None:
            assert v3_result is None, f"V2=None but V3={v3_result} for raw={raw_val}"
        else:
            assert v3_result is not None, f"V3=None but V2={v2_result} for raw={raw_val}"
            assert abs(v3_result - v2_result) < 1e-5, f"Mismatch: V3={v3_result}, V2={v2_result} for raw={raw_val}"


def test_v2_v3_deterministic():
    """Test that both V2 and V3 produce deterministic output."""
    if not V2_CALIBRATION_FILE.exists():
        pytest.skip(f"Calibration file not found: {V2_CALIBRATION_FILE}")

    v2_parser = V2CalibrationParser()
    v2_calibration = v2_parser.load(V2_CALIBRATION_FILE)
    V2CalibrationProcessor.build_lookup_tables(v2_calibration)

    v3_parser = V3CalibrationParser()
    v3_calibration = v3_parser.load(V2_CALIBRATION_FILE)
    V3CalibrationProcessor.build_lookup_tables(v3_calibration)

    np.random.seed(42)
    raw1 = np.random.randint(0, 65535, (100, 100), dtype=np.uint16)
    raw2 = np.random.randint(0, 65535, (100, 100), dtype=np.uint16)

    # Same input should produce same output
    v2_temp1 = V2CalibrationProcessor.raw_to_temperature(raw1, v2_calibration, 0)
    v2_temp2 = V2CalibrationProcessor.raw_to_temperature(raw1, v2_calibration, 0)
    assert np.array_equal(v2_temp1, v2_temp2)

    v3_temp1 = V3CalibrationProcessor.raw_to_temperature(raw1, v3_calibration, 0)
    v3_temp2 = V3CalibrationProcessor.raw_to_temperature(raw1, v3_calibration, 0)
    assert np.array_equal(v3_temp1, v3_temp2)

    # Different inputs should produce different outputs
    assert not np.array_equal(v2_temp1, V2CalibrationProcessor.raw_to_temperature(raw2, v2_calibration, 0))
    assert not np.array_equal(v3_temp1, V3CalibrationProcessor.raw_to_temperature(raw2, v3_calibration, 0))


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])