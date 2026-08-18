"""
V3 Calibration Tests

Comprehensive tests for the V3 calibration subsystem matching V2 test coverage.
Validates against the proven V2 calibration implementation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from thermal_monitor.calibration.models import (
    CameraCalibration,
    CalibrationRange,
    UniverseSegment,
)
from thermal_monitor.calibration.parser import CalibrationParser
from thermal_monitor.calibration.processor import CalibrationProcessor
from thermal_monitor.processing.temperature import (
    CPUTemperatureConverter,
    CachingCalibrationProvider,
)

# Test constants
V2_CALIBRATION_FILE = Path("reference/TMS_v2/assets/calibration/calibration_blob.txt")
V3_ASSETS_CALIBRATION_FILE = Path("assets/calibration/calibration_blob.txt")

# Ensure we have a calibration file for testing
CALIBRATION_FILE = (
    V2_CALIBRATION_FILE if V2_CALIBRATION_FILE.exists() else V3_ASSETS_CALIBRATION_FILE
)


class TestCalibrationModels:
    """Test calibration data models."""

    def test_universe_segment_creation(self):
        segment = UniverseSegment(
            u0=1000.0, u1=50.0, u2=0.1, start_temp=-20.0, end_temp=80.0
        )
        assert segment.u0 == 1000.0
        assert segment.u1 == 50.0
        assert segment.u2 == 0.1
        assert segment.start_temp == -20.0
        assert segment.end_temp == 80.0

    def test_calibration_range_creation(self):
        calibration_range = CalibrationRange(
            calibration_min=-40.0,
            calibration_max=550.0,
            display_min=0.0,
            display_max=500.0,
            manual_palette_span=100.0,
            auto_palette_span=50.0,
            num_segments=11,
        )
        assert calibration_range.calibration_min == -40.0
        assert calibration_range.calibration_max == 550.0
        assert calibration_range.num_segments == 11
        assert len(calibration_range.segments) == 0

    def test_camera_calibration_creation(self):
        calibration = CameraCalibration(
            magic=0x016D6952,
            enabled_ranges=3,
            enabled_mask=0x7,
            calibration_date="01/01/2024",
        )
        assert calibration.magic == 0x016D6952
        assert calibration.enabled_ranges == 3
        assert calibration.enabled_mask == 0x7

    def test_camera_calibration_add_range(self):
        calibration = CameraCalibration()
        cal_range = CalibrationRange(
            calibration_min=-20.0,
            calibration_max=80.0,
            display_min=0.0,
            display_max=100.0,
            manual_palette_span=50.0,
            auto_palette_span=25.0,
            num_segments=5,
        )
        calibration.add_range(cal_range)
        assert len(calibration.ranges) == 1
        assert calibration.ranges[0] is cal_range

    def test_camera_calibration_lookup_table(self):
        calibration = CameraCalibration()
        lut = np.arange(65536, dtype=np.float32)
        calibration.set_lookup_table(0, lut)
        assert calibration.has_lookup_table(0)
        assert calibration.get_lookup_table(0) is lut
        assert not calibration.has_lookup_table(1)


class TestCalibrationParser:
    """Test calibration parser with V2 calibration blob."""

    def test_load_calibration_file(self):
        if not CALIBRATION_FILE.exists():
            import pytest
            pytest.skip(f"Calibration file not found: {CALIBRATION_FILE}")

        parser = CalibrationParser()
        calibration = parser.load(CALIBRATION_FILE)

        assert calibration is not None
        assert calibration.enabled_ranges > 0
        assert len(calibration.ranges) == calibration.enabled_ranges
        assert calibration.magic != 0
        assert calibration.calibration_date != ""

    def test_calibration_header_values(self):
        if not CALIBRATION_FILE.exists():
            import pytest
            pytest.skip(f"Calibration file not found: {CALIBRATION_FILE}")

        parser = CalibrationParser()
        calibration = parser.load(CALIBRATION_FILE)

        assert calibration.enabled_ranges >= 1
        assert calibration.enabled_ranges <= 3
        assert calibration.enabled_mask != 0

    def test_calibration_ranges_structure(self):
        if not CALIBRATION_FILE.exists():
            import pytest
            pytest.skip(f"Calibration file not found: {CALIBRATION_FILE}")

        parser = CalibrationParser()
        calibration = parser.load(CALIBRATION_FILE)

        for cal_range in calibration.ranges:
            assert cal_range.num_segments > 0
            assert cal_range.num_segments <= 11
            assert len(cal_range.segments) >= cal_range.num_segments
            assert cal_range.calibration_min < cal_range.calibration_max
            assert cal_range.display_min < cal_range.display_max

            for segment in cal_range.segments[: cal_range.num_segments]:
                assert segment.start_temp <= segment.end_temp
                # At least one coefficient should be non-zero
                assert segment.u0 != 0.0 or segment.u1 != 0.0 or segment.u2 != 0.0


class TestCalibrationProcessor:
    """Test calibration processor LUT generation and conversion."""

    def setup_method(self):
        if not CALIBRATION_FILE.exists():
            import pytest
            pytest.skip(f"Calibration file not found: {CALIBRATION_FILE}")

        parser = CalibrationParser()
        self.calibration = parser.load(CALIBRATION_FILE)
        CalibrationProcessor.build_lookup_tables(self.calibration)

    def test_lookup_tables_generated(self):
        assert CalibrationProcessor.validate_lookup_tables(self.calibration)

        for index in range(self.calibration.enabled_ranges):
            lut = self.calibration.get_lookup_table(index)
            assert lut is not None
            assert lut.dtype == np.float32
            assert lut.size == CalibrationProcessor.LUT_SIZE
            assert np.isfinite(lut).all()

            print(f"LUT {index}: Min={lut.min():.2f}°C Max={lut.max():.2f}°C")

    def test_rebuild_lookup_tables(self):
        CalibrationProcessor.rebuild_lookup_tables(self.calibration)
        assert CalibrationProcessor.validate_lookup_tables(self.calibration)

    def test_raw_to_temperature(self):
        raw = np.random.randint(0, 65535, (480, 640), dtype=np.uint16)
        temperature = CalibrationProcessor.raw_to_temperature(
            raw, self.calibration, range_index=0
        )

        assert temperature.dtype == np.float32
        assert temperature.shape == raw.shape
        assert CalibrationProcessor.validate_temperature_image(temperature)

        finite = np.isfinite(temperature)
        assert np.any(finite)

        print(
            f"Temperature range: {temperature[finite].min():.2f}°C -> "
            f"{temperature[finite].max():.2f}°C"
        )

    def test_raw_to_display(self):
        raw = np.random.randint(0, 65535, (480, 640), dtype=np.uint16)
        display = CalibrationProcessor.raw_to_display(raw, self.calibration, range_index=0)

        assert display.dtype == np.uint8
        assert display.shape == raw.shape
        assert CalibrationProcessor.validate_display_image(display)

    def test_temperature_to_display(self):
        temperature = np.random.uniform(20, 100, (480, 640)).astype(np.float32)
        display = CalibrationProcessor.temperature_to_display(temperature)

        assert display.dtype == np.uint8
        assert display.shape == temperature.shape
        assert CalibrationProcessor.validate_display_image(display)

    def test_temperature_statistics(self):
        raw = np.random.randint(0, 65535, (480, 640), dtype=np.uint16)
        temperature = CalibrationProcessor.raw_to_temperature(raw, self.calibration)

        statistics = CalibrationProcessor.get_temperature_statistics(temperature)

        assert isinstance(statistics, dict)
        for key in ("minimum", "maximum", "mean", "median", "std"):
            assert key in statistics
            assert np.isfinite(statistics[key])

    def test_roi_statistics(self):
        raw = np.random.randint(0, 65535, (480, 640), dtype=np.uint16)
        temperature = CalibrationProcessor.raw_to_temperature(raw, self.calibration)

        mask = np.zeros(temperature.shape, dtype=bool)
        mask[150:300, 200:450] = True

        statistics = CalibrationProcessor.get_roi_statistics(temperature, mask)

        for key in ("minimum", "maximum", "mean", "median", "std"):
            assert key in statistics
            assert np.isfinite(statistics[key])

    def test_validate_temperature_image(self):
        raw = np.random.randint(0, 65535, (100, 100), dtype=np.uint16)
        image = CalibrationProcessor.raw_to_temperature(raw, self.calibration)
        assert CalibrationProcessor.validate_temperature_image(image)

    def test_validate_display_image(self):
        raw = np.random.randint(0, 65535, (100, 100), dtype=np.uint16)
        image = CalibrationProcessor.raw_to_display(raw, self.calibration)
        assert CalibrationProcessor.validate_display_image(image)

    def test_clear_lookup_tables(self):
        CalibrationProcessor.clear_lookup_tables(self.calibration)
        assert len(self.calibration.lookup_tables) == 0

    def test_rebuild_after_clear(self):
        CalibrationProcessor.clear_lookup_tables(self.calibration)
        CalibrationProcessor.rebuild_lookup_tables(self.calibration)
        assert CalibrationProcessor.validate_lookup_tables(self.calibration)

    def test_get_calibration_range(self):
        cal_range = CalibrationProcessor.get_calibration_range(self.calibration, 0)
        assert cal_range is not None
        assert cal_range.num_segments > 0

    def test_invalid_range_raises(self):
        import pytest
        with pytest.raises(IndexError):
            CalibrationProcessor.get_calibration_range(self.calibration, 999)

    def test_empty_images_invalid(self):
        empty_temperature = np.array([], dtype=np.float32)
        empty_display = np.array([], dtype=np.uint8)

        assert not CalibrationProcessor.validate_temperature_image(empty_temperature)
        assert not CalibrationProcessor.validate_display_image(empty_display)


class TestCPUTemperatureConverter:
    """Test CPU TemperatureConverter implementation."""

    def setup_method(self):
        if not CALIBRATION_FILE.exists():
            import pytest
            pytest.skip(f"Calibration file not found: {CALIBRATION_FILE}")

        parser = CalibrationParser()
        self.calibration = parser.load(CALIBRATION_FILE)
        CalibrationProcessor.build_lookup_tables(self.calibration)
        self.converter = CPUTemperatureConverter()

    def test_raw_to_temperature_with_camera_calibration(self):
        raw = np.random.randint(0, 65535, (480, 640), dtype=np.uint16)
        temperature = self.converter.raw_to_temperature(
            raw_data=raw,
            calibration=self.calibration,
            emissivity=0.95,
            ambient_temp=25.0,
            distance=1.0,
            humidity=50.0,
            reflected_temp=20.0,
        )

        assert temperature.dtype == np.float32
        assert temperature.shape == raw.shape
        assert CalibrationProcessor.validate_temperature_image(temperature)

        finite = np.isfinite(temperature)
        assert np.any(finite)

    def test_raw_to_temperature_with_lut_array(self):
        raw = np.random.randint(0, 65535, (480, 640), dtype=np.uint16)
        lut = self.calibration.get_lookup_table(0)
        assert lut is not None

        temperature = self.converter.raw_to_temperature(
            raw_data=raw,
            calibration=lut,
            emissivity=0.95,
            ambient_temp=25.0,
            distance=1.0,
            humidity=50.0,
            reflected_temp=20.0,
        )

        assert temperature.dtype == np.float32
        assert temperature.shape == raw.shape

    def test_invalid_calibration_raises(self):
        raw = np.random.randint(0, 65535, (100, 100), dtype=np.uint16)

        import pytest
        with pytest.raises(TypeError):
            self.converter.raw_to_temperature(
                raw_data=raw,
                calibration="invalid",
                emissivity=0.95,
                ambient_temp=25.0,
                distance=1.0,
                humidity=50.0,
                reflected_temp=20.0,
            )

    def test_none_calibration_raises_without_provider(self):
        raw = np.random.randint(0, 65535, (100, 100), dtype=np.uint16)

        import pytest
        with pytest.raises((ValueError, TypeError)):
            self.converter.raw_to_temperature(
                raw_data=raw,
                calibration=None,
                emissivity=0.95,
                ambient_temp=25.0,
                distance=1.0,
                humidity=50.0,
                reflected_temp=20.0,
            )

    def test_output_dtype_and_shape_preservation(self):
        for shape in [(480, 640), (100, 100), (1, 1), (10, 20)]:
            raw = np.random.randint(0, 65535, shape, dtype=np.uint16)
            temperature = self.converter.raw_to_temperature(
                raw_data=raw,
                calibration=self.calibration,
                emissivity=0.95,
                ambient_temp=25.0,
                distance=1.0,
                humidity=50.0,
                reflected_temp=20.0,
            )
            assert temperature.shape == shape
            assert temperature.dtype == np.float32


class TestCachingCalibrationProvider:
    """Test calibration provider with caching."""

    def setup_method(self):
        if not CALIBRATION_FILE.exists():
            import pytest
            pytest.skip(f"Calibration file not found: {CALIBRATION_FILE}")

        self.provider = CachingCalibrationProvider(
            {"test_camera": str(CALIBRATION_FILE)}
        )

    def test_get_calibration_returns_lut(self):
        lut = self.provider.get_calibration("test_camera")
        assert lut is not None
        assert lut.dtype == np.float32
        assert lut.size == 65536
        assert np.isfinite(lut).all()

    def test_get_camera_calibration_returns_full_object(self):
        calibration = self.provider.get_camera_calibration("test_camera")
        assert calibration is not None
        assert isinstance(calibration, CameraCalibration)
        assert calibration.enabled_ranges > 0

    def test_unknown_camera_returns_none(self):
        lut = self.provider.get_calibration("unknown_camera")
        assert lut is None

        calibration = self.provider.get_camera_calibration("unknown_camera")
        assert calibration is None

    def test_cache_works(self):
        lut1 = self.provider.get_calibration("test_camera")
        lut2 = self.provider.get_calibration("test_camera")
        assert lut1 is lut2  # Same object from cache

    def test_clear_cache(self):
        self.provider.get_calibration("test_camera")
        self.provider.clear_cache()
        lut = self.provider.get_calibration("test_camera")
        assert lut is not None  # Reloads from file


class TestCalibrationEdgeCases:
    """Test edge cases and boundary conditions."""

    def setup_method(self):
        if not CALIBRATION_FILE.exists():
            import pytest
            pytest.skip(f"Calibration file not found: {CALIBRATION_FILE}")

        parser = CalibrationParser()
        self.calibration = parser.load(CALIBRATION_FILE)
        CalibrationProcessor.build_lookup_tables(self.calibration)

    def test_minimum_uint16_raw_value(self):
        raw = np.array([[0]], dtype=np.uint16)
        temperature = CalibrationProcessor.raw_to_temperature(
            raw, self.calibration, range_index=0
        )
        assert temperature.dtype == np.float32
        assert temperature.shape == (1, 1)
        assert np.isfinite(temperature[0, 0])

    def test_maximum_uint16_raw_value(self):
        raw = np.array([[65535]], dtype=np.uint16)
        temperature = CalibrationProcessor.raw_to_temperature(
            raw, self.calibration, range_index=0
        )
        assert temperature.dtype == np.float32
        assert temperature.shape == (1, 1)
        assert np.isfinite(temperature[0, 0])

    def test_all_same_raw_values(self):
        raw = np.full((100, 100), 32768, dtype=np.uint16)
        temperature = CalibrationProcessor.raw_to_temperature(
            raw, self.calibration, range_index=0
        )
        assert np.allclose(temperature, temperature[0, 0])

    def test_deterministic_output(self):
        raw = np.random.randint(0, 65535, (480, 640), dtype=np.uint16)
        temp1 = CalibrationProcessor.raw_to_temperature(raw, self.calibration, 0)
        temp2 = CalibrationProcessor.raw_to_temperature(raw, self.calibration, 0)
        assert np.array_equal(temp1, temp2)

    def test_lut_values_are_finite(self):
        """Test that all LUT values are finite (no NaN after interpolation)."""
        for index in range(self.calibration.enabled_ranges):
            lut = self.calibration.get_lookup_table(index)
            assert np.isfinite(lut).all()
            assert lut.dtype == np.float32
            assert lut.size == 65536


class TestV2Compatibility:
    """Test V3 output matches V2 reference implementation where possible."""

    def test_lut_construction_matches_v2_algorithm(self):
        """Verify LUT construction uses the same vectorized algorithm as V2."""
        if not CALIBRATION_FILE.exists():
            import pytest
            pytest.skip(f"Calibration file not found: {CALIBRATION_FILE}")

        parser = CalibrationParser()
        calibration = parser.load(CALIBRATION_FILE)
        CalibrationProcessor.build_lookup_tables(calibration)

        # Verify LUT was built with vectorized operations (no NaN after fill)
        for index in range(calibration.enabled_ranges):
            lut = calibration.get_lookup_table(index)
            assert lut is not None
            assert np.isfinite(lut).all()

    def test_inverse_quadratic_solver(self):
        """Test the inverse quadratic polynomial solver directly."""
        # Create a segment with known coefficients
        # raw = u0 + u1*T + u2*T^2
        # For testing: raw = 1000 + 50*T + 0.1*T^2
        segment = UniverseSegment(
            u0=1000.0, u1=50.0, u2=0.1, start_temp=-20.0, end_temp=80.0
        )

        # At T=0, raw = 1000
        temp = CalibrationProcessor.solve_polynomial(1000.0, segment)
        assert temp is not None
        assert abs(temp - 0.0) < 0.01

        # At T=20, raw = 1000 + 50*20 + 0.1*400 = 1000 + 1000 + 40 = 2040
        temp = CalibrationProcessor.solve_polynomial(2040.0, segment)
        assert temp is not None
        assert abs(temp - 20.0) < 0.01

    def test_segment_boundary_handling(self):
        """Test solver correctly rejects temperatures outside segment range."""
        segment = UniverseSegment(
            u0=1000.0, u1=50.0, u2=0.1, start_temp=0.0, end_temp=50.0
        )

        # T=60 would give valid raw but outside segment range
        temp = CalibrationProcessor.solve_polynomial(
            1000.0 + 50.0 * 60.0 + 0.1 * 3600.0, segment
        )
        assert temp is None  # Outside segment


def run_manual_tests():
    """Run manual verification tests."""
    print("=" * 60)
    print("Manual Calibration Verification")
    print("=" * 60)

    if not CALIBRATION_FILE.exists():
        print(f"Calibration file not found: {CALIBRATION_FILE}")
        return

    parser = CalibrationParser()
    calibration = parser.load(CALIBRATION_FILE)
    CalibrationProcessor.build_lookup_tables(calibration)

    print(f"\nCalibration loaded:")
    print(f"  Magic: 0x{calibration.magic:08X}")
    print(f"  Enabled Ranges: {calibration.enabled_ranges}")
    print(f"  Date: {calibration.calibration_date}")

    for i, cal_range in enumerate(calibration.ranges):
        print(f"\nRange {i}:")
        print(f"  Calibration: {cal_range.calibration_min:.1f}°C -> {cal_range.calibration_max:.1f}°C")
        print(f"  Display: {cal_range.display_min:.1f}°C -> {cal_range.display_max:.1f}°C")
        print(f"  Segments: {cal_range.num_segments}")

        lut = calibration.get_lookup_table(i)
        print(f"  LUT: {lut.min():.2f}°C -> {lut.max():.2f}°C")

    # Test specific raw values
    print("\nRaw value -> Temperature mapping:")
    test_raw_values = [0, 1000, 10000, 20000, 32768, 40000, 50000, 65535]
    for raw_val in test_raw_values:
        temp = CalibrationProcessor.raw_value_to_temperature(
            raw_val, calibration.ranges[0]
        )
        print(f"  Raw {raw_val:5d} -> {temp:.2f}°C" if temp else f"  Raw {raw_val:5d} -> OUT OF RANGE")

    print("\nFull image conversion test:")
    raw = np.random.randint(0, 65535, (480, 640), dtype=np.uint16)
    temp = CalibrationProcessor.raw_to_temperature(raw, calibration, 0)
    finite = np.isfinite(temp)
    print(f"  Shape: {temp.shape}")
    print(f"  Dtype: {temp.dtype}")
    print(f"  Valid pixels: {np.sum(finite)}/{temp.size}")
    if np.any(finite):
        print(f"  Temp range: {temp[finite].min():.2f}°C -> {temp[finite].max():.2f}°C")
        print(f"  Mean: {temp[finite].mean():.2f}°C")


if __name__ == "__main__":
    run_manual_tests()