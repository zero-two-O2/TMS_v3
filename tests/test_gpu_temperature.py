#!/usr/bin/env python3
"""
Tests for GPU Temperature Converter and CPU/GPU parity.

These tests verify:
1. CPU converter unchanged behavior
2. GPU converter correctness (when GPU available)
3. CPU/GPU numerical parity
4. NaN handling
5. uint16 boundary values
6. LUT boundary values
7. GPU unavailable fallback
8. Pipeline works with CPU backend
9. Pipeline works with GPU backend
10. No HALCON ROI regression
"""

from __future__ import annotations

import numpy as np
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from thermal_monitor.processing.temperature import (
    CPUTemperatureConverter,
    GPUTemperatureConverter,
    CachingCalibrationProvider,
    is_gpu_available,
    get_gpu_device_name,
)
from thermal_monitor.processing.pipeline import (
    SimpleProcessingPipeline,
    TemperatureConverter,
    AnalysisConfig,
)
from thermal_monitor.calibration.models import (
    CameraCalibration,
    CalibrationRange,
    UniverseSegment,
)
from thermal_monitor.calibration.processor import CalibrationProcessor
from thermal_monitor.core.models import (
    ROIConfig,
    ROIGeometry,
    ROIShape,
    TemperatureLimits,
    TemperatureUnit,
    PositionROIAssociation,
)
from thermal_monitor.core.frame import (
    Frame,
    FrameDescriptor,
    FramePayload,
    StreamMetadata,
    SyncInfo,
    SyncStatus,
)
from thermal_monitor.processing.halcon import HalconROIAdapter


def make_test_lut() -> np.ndarray:
    """Create a simple test LUT for unit testing."""
    lut = np.arange(65536, dtype=np.float32) * 0.01  # 0.01°C per raw value
    lut[0] = np.nan  # Invalid value at 0
    lut[65535] = np.nan  # Invalid value at max
    return lut


def make_valid_lut() -> np.ndarray:
    """Create a valid LUT with no NaN values (simulating real interpolated LUT)."""
    return np.arange(65536, dtype=np.float32) * 0.01


def make_test_calibration() -> CameraCalibration:
    """Create a minimal CameraCalibration with valid LUT using real parser."""
    # Use the real calibration from assets
    from thermal_monitor.calibration.parser import CalibrationParser
    from pathlib import Path
    
    parser = CalibrationParser()
    calibration_path = Path("assets/calibration/calibration_blob.txt")
    if calibration_path.exists():
        calibration = parser.load(calibration_path)
        CalibrationProcessor.build_lookup_tables(calibration)
        return calibration
    
    # Fallback: create a simple valid calibration
    calibration = CameraCalibration()
    
    # Create segments that cover the full raw range
    segments = [
        UniverseSegment(u0=0.0, u1=0.01, u2=0.0, start_temp=-20.0, end_temp=80.0),
    ]
    cal_range = CalibrationRange(
        calibration_min=-20.0,
        calibration_max=80.0,
        display_min=-22.5,
        display_max=82.5,
        manual_palette_span=100.0,
        auto_palette_span=100.0,
        num_segments=1,
        segments=segments,
    )
    calibration.add_range(cal_range)
    
    # Build LUT - this should work with linear segment
    CalibrationProcessor.build_lookup_tables(calibration)
    return calibration


class TestCPUTemperatureConverter:
    """Tests for CPU converter - must remain unchanged."""
    
    def test_basic_conversion(self):
        """Test basic LUT lookup conversion."""
        lut = make_test_lut()
        converter = CPUTemperatureConverter()
        
        raw = np.array([[0, 100], [1000, 65535]], dtype=np.uint16)
        result = converter.raw_to_temperature(
            raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0
        )
        
        assert result.shape == (2, 2)
        assert result.dtype == np.float32
        assert np.isnan(result[0, 0])  # LUT[0] is NaN
        assert np.isnan(result[1, 1])  # LUT[65535] is NaN
        assert result[0, 1] == lut[100]
        assert result[1, 0] == lut[1000]
    
    def test_no_copy_when_already_uint16(self):
        """Test that uint16 input avoids unnecessary copy."""
        lut = make_test_lut()
        converter = CPUTemperatureConverter()
        
        raw = np.array([[100, 200]], dtype=np.uint16)
        result = converter.raw_to_temperature(
            raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0
        )
        
        assert result[0, 0] == lut[100]
        assert result[0, 1] == lut[200]
    
    def test_conversion_from_other_dtypes(self):
        """Test conversion from int32, float32 inputs."""
        lut = make_test_lut()
        converter = CPUTemperatureConverter()
        
        # int32 input
        raw_int32 = np.array([[100, 200]], dtype=np.int32)
        result = converter.raw_to_temperature(
            raw_int32, lut, 1.0, 25.0, 1.0, 50.0, 25.0
        )
        assert result[0, 0] == lut[100]
        
        # float32 input
        raw_float = np.array([[100.0, 200.0]], dtype=np.float32)
        result = converter.raw_to_temperature(
            raw_float, lut, 1.0, 25.0, 1.0, 50.0, 25.0
        )
        assert result[0, 0] == lut[100]
    
    def test_invalid_lut_dtype_raises(self):
        """Test that non-float32 LUT raises ValueError."""
        lut = np.arange(65536, dtype=np.float64)
        converter = CPUTemperatureConverter()
        
        raw = np.array([[100]], dtype=np.uint16)
        with pytest.raises(ValueError, match="LUT must be float32"):
            converter.raw_to_temperature(raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0)
    
    def test_invalid_lut_size_raises(self):
        """Test that non-65536 LUT raises ValueError."""
        lut = np.arange(1000, dtype=np.float32)
        converter = CPUTemperatureConverter()
        
        raw = np.array([[100]], dtype=np.uint16)
        with pytest.raises(ValueError, match="LUT must have 65536 entries"):
            converter.raw_to_temperature(raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0)
    
    def test_none_lut_raises(self):
        """Test that None LUT raises ValueError."""
        converter = CPUTemperatureConverter()
        
        raw = np.array([[100]], dtype=np.uint16)
        with pytest.raises(ValueError, match="Calibration LUT is None"):
            converter.raw_to_temperature(raw, None, 1.0, 25.0, 1.0, 50.0, 25.0)
    
    def test_physics_parameters_accepted_but_unused(self):
        """Test that physics params are accepted but don't affect output."""
        lut = make_test_lut()
        converter = CPUTemperatureConverter()
        
        raw = np.array([[1000]], dtype=np.uint16)
        
        # Different physics params, same raw -> same output
        result1 = converter.raw_to_temperature(raw, lut, 0.95, 20.0, 2.0, 60.0, 30.0)
        result2 = converter.raw_to_temperature(raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0)
        result3 = converter.raw_to_temperature(raw, lut, 0.8, 30.0, 5.0, 80.0, 40.0)
        
        assert result1[0, 0] == result2[0, 0] == result3[0, 0] == lut[1000]
    
    def test_camera_calibration_object_input(self):
        """Test that CameraCalibration object works as calibration input."""
        calibration = make_test_calibration()
        converter = CPUTemperatureConverter()
        
        raw = np.array([[1000]], dtype=np.uint16)
        result = converter.raw_to_temperature(
            raw, calibration, 1.0, 25.0, 1.0, 50.0, 25.0
        )
        
        assert result.shape == (1, 1)
        assert result.dtype == np.float32


class TestGPUTemperatureConverter:
    """Tests for GPU converter."""
    
    def test_init_without_cupy(self):
        """Test GPUTemperatureConverter initialization when CuPy unavailable."""
        # CuPy is imported inside _init_cupy, so we test the fallback behavior
        # by checking that is_ready is False when GPU not available
        converter = GPUTemperatureConverter()
        # On this dev machine, GPU is not available
        assert not converter.is_ready()
    
    def test_init_cupy_import_failure(self):
        """Test initialization handles CuPy import failure gracefully."""
        # Test that the converter handles missing CuPy gracefully
        # (On this machine it should already be handled in _init_cupy)
        converter = GPUTemperatureConverter()
        # Should not crash, just not be ready
        assert converter._cp is None or not converter.is_ready()
    
    def test_is_ready_false_when_cupy_unavailable(self):
        """Test is_ready returns False when CuPy unavailable."""
        converter = GPUTemperatureConverter()
        # On this dev machine, GPU is not available
        assert converter._cp is None or not converter.is_ready()


class TestCPUGPUParity:
    """Tests for CPU/GPU numerical parity (run when GPU available)."""
    
    @pytest.mark.skipif(not is_gpu_available(), reason="GPU not available")
    def test_gpu_converter_initialization(self):
        """Test GPU converter initializes correctly."""
        converter = GPUTemperatureConverter()
        assert converter._cp is not None
        # LUT not uploaded until first conversion
        assert not converter.is_ready()
    
    @pytest.mark.skipif(not is_gpu_available(), reason="GPU not available")
    def test_gpu_lut_upload(self):
        """Test LUT uploads to GPU correctly."""
        converter = GPUTemperatureConverter()
        lut = make_valid_lut()
        
        # First conversion should upload LUT
        raw = np.array([[100]], dtype=np.uint16)
        result = converter.raw_to_temperature(raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0)
        
        assert converter.is_ready()
        assert converter._gpu_lut is not None
        assert result[0, 0] == lut[100]
    
    @pytest.mark.skipif(not is_gpu_available(), reason="GPU not available")
    def test_cpu_gpu_numerical_equivalence(self):
        """Test CPU and GPU produce identical results."""
        cpu_converter = CPUTemperatureConverter()
        gpu_converter = GPUTemperatureConverter()
        lut = make_valid_lut()
        
        # Test various input patterns
        test_frames = [
            np.array([[0, 1, 100, 1000, 65534, 65535]], dtype=np.uint16).reshape(2, 3),
            np.random.randint(0, 65535, (480, 640), dtype=np.uint16),
            np.full((10, 10), 32768, dtype=np.uint16),  # Mid-range
            np.array([[0, 65535]], dtype=np.uint16),  # Boundaries
        ]
        
        for frame in test_frames:
            cpu_out = cpu_converter.raw_to_temperature(frame, lut, 1.0, 25.0, 1.0, 50.0, 25.0)
            gpu_out = gpu_converter.raw_to_temperature(frame, lut, 1.0, 25.0, 1.0, 50.0, 25.0)
            
            # Check shape and dtype
            assert cpu_out.shape == gpu_out.shape
            assert cpu_out.dtype == gpu_out.dtype == np.float32
            
            # Check NaN positions match
            cpu_nan = np.isnan(cpu_out)
            gpu_nan = np.isnan(gpu_out)
            assert np.array_equal(cpu_nan, gpu_nan), "NaN positions differ"
            
            # Check finite values match within tolerance
            both_finite = np.isfinite(cpu_out) & np.isfinite(gpu_out)
            if np.any(both_finite):
                max_diff = np.max(np.abs(cpu_out[both_finite] - gpu_out[both_finite]))
                assert max_diff < 1e-5, f"Max diff {max_diff} exceeds tolerance"
    
    @pytest.mark.skipif(not is_gpu_available(), reason="GPU not available")
    def test_gpu_fallback_on_error(self):
        """Test GPU falls back to CPU on error."""
        converter = GPUTemperatureConverter()
        lut = make_valid_lut()
        
        # First conversion to upload LUT
        raw = np.array([[100]], dtype=np.uint16)
        _ = converter.raw_to_temperature(raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0)
        
        # Now corrupt the GPU LUT to force fallback
        converter._gpu_lut = None
        converter._lut_ready = False
        
        # Should fall back to CPU
        result = converter.raw_to_temperature(raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0)
        assert result[0, 0] == lut[100]


class TestBoundaryValues:
    """Tests for uint16 and LUT boundary values."""
    
    def test_uint16_min_value(self):
        """Test raw value 0 (minimum uint16)."""
        lut = make_test_lut()
        converter = CPUTemperatureConverter()
        
        raw = np.array([[0]], dtype=np.uint16)
        result = converter.raw_to_temperature(raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0)
        
        # LUT[0] is NaN in our test LUT
        assert np.isnan(result[0, 0])
    
    def test_uint16_max_value(self):
        """Test raw value 65535 (maximum uint16)."""
        lut = make_test_lut()
        converter = CPUTemperatureConverter()
        
        raw = np.array([[65535]], dtype=np.uint16)
        result = converter.raw_to_temperature(raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0)
        
        # LUT[65535] is NaN in our test LUT
        assert np.isnan(result[0, 0])
    
    def test_uint16_mid_range(self):
        """Test mid-range uint16 values."""
        lut = make_test_lut()
        converter = CPUTemperatureConverter()
        
        for val in [1, 32767, 32768, 65534]:
            raw = np.array([[val]], dtype=np.uint16)
            result = converter.raw_to_temperature(raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0)
            assert result[0, 0] == lut[val]
    
    def test_real_lut_no_nan_after_interpolation(self):
        """Test that real LUT has no NaN after interpolation."""
        calibration = make_test_calibration()
        converter = CPUTemperatureConverter()
        lut = calibration.get_lookup_table(0)
        
        # Real LUT should have no NaN after interpolation
        assert not np.any(np.isnan(lut)), "Real LUT should have no NaN after interpolation"
        
        raw = np.array([[0, 65535]], dtype=np.uint16)
        result = converter.raw_to_temperature(raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0)
        
        assert not np.any(np.isnan(result)), "Result should have no NaN with real LUT"


class TestGPUFallback:
    """Tests for GPU unavailable fallback behavior."""
    
    def test_cpu_fallback_when_no_gpu(self):
        """Test CPU converter works when GPU unavailable."""
        converter = CPUTemperatureConverter()
        lut = make_test_lut()
        
        raw = np.array([[1000, 2000]], dtype=np.uint16)
        result = converter.raw_to_temperature(raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0)
        
        assert result[0, 0] == lut[1000]
        assert result[0, 1] == lut[2000]
    
    def test_gpu_converter_fallback_to_cpu(self):
        """Test GPUTemperatureConverter falls back to CPU when CuPy unavailable."""
        # On this dev machine, GPU is not available so it should fall back
        converter = GPUTemperatureConverter()
        lut = make_valid_lut()
        
        raw = np.array([[1000]], dtype=np.uint16)
        result = converter.raw_to_temperature(raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0)
        
        assert result[0, 0] == lut[1000]
    
    def test_protocol_methods_exist(self):
        """Test both converters have the required protocol method."""
        cpu = CPUTemperatureConverter()
        gpu = GPUTemperatureConverter()
        
        assert hasattr(cpu, 'raw_to_temperature')
        assert hasattr(gpu, 'raw_to_temperature')
        assert callable(cpu.raw_to_temperature)
        assert callable(gpu.raw_to_temperature)


class TestPipelineIntegration:
    """Tests for pipeline integration with both backends."""
    
    def create_test_frame(self) -> Frame:
        """Create a test frame with thermal data."""
        thermal = np.zeros((100, 100), dtype=np.uint16)
        thermal[40:60, 40:60] = 30000
        thermal.setflags(write=False)
        
        thermal_meta = StreamMetadata(
            present=True, width=100, height=100, pixel_format="IR_Data",
            bits_per_channel=16, dtype="uint16", byte_count=thermal.nbytes,
        )
        descriptor = FrameDescriptor(
            camera_id="test_cam", sequence=0, timestamp=1.0, monotonic_timestamp=1.0,
            thermal=thermal_meta, visible=StreamMetadata(present=False),
            sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
        )
        return Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal))
    
    def create_analysis_config(self) -> AnalysisConfig:
        """Create test analysis config with one ROI."""
        roi = ROIConfig(
            roi_id="roi_1", name="Test ROI",
            geometry=ROIGeometry(shape=ROIShape.RECTANGLE1, parameters={"y1": 40, "x1": 40, "y2": 60, "x2": 60}),
            temperature_limits=TemperatureLimits(unit=TemperatureUnit.CELSIUS, max_warning=100.0),
        )
        assoc = PositionROIAssociation(position_id="default", roi_ids=("roi_1",))
        return AnalysisConfig(camera_id="test_cam", rois={"roi_1": roi}, position_associations={"default": assoc})
    
    def test_pipeline_with_cpu_converter(self):
        """Test SimpleProcessingPipeline with CPU converter."""
        # Use a valid LUT directly
        lut = make_valid_lut()
        
        config = self.create_analysis_config()
        converter = CPUTemperatureConverter()
        
        # Mock calibration provider that returns our test LUT
        class MockCalibrationProvider:
            def get_calibration(self, camera_id: str):
                return lut
        
        class MockHalcon:
            def generate_regions(self, rois): return "mock"
            def extract_statistics(self, regions, temp_image, rois=None):
                from thermal_monitor.core.models import ROIStatistics
                return [ROIStatistics(roi_id="roi_1", roi_name="Test ROI", min_temp=20.0, max_temp=30.0, mean_temp=25.0, deviation=2.0, unit=TemperatureUnit.CELSIUS)]
            def release_regions(self, regions): pass
        
        pipeline = SimpleProcessingPipeline(
            config=config,
            temperature_converter=converter,
            calibration_provider=MockCalibrationProvider(),
            halcon_adapter=MockHalcon(),
        )
        
        frame = self.create_test_frame()
        result = pipeline.process_frame(frame)
        
        assert result.camera_id == "test_cam"
        assert "roi_1" in result.roi_results
    
    @pytest.mark.skipif(not is_gpu_available(), reason="GPU not available")
    def test_pipeline_with_gpu_converter(self):
        """Test SimpleProcessingPipeline with GPU converter."""
        lut = make_valid_lut()
        
        config = self.create_analysis_config()
        converter = GPUTemperatureConverter()
        
        class MockCalibrationProvider:
            def get_calibration(self, camera_id: str):
                return lut
        
        class MockHalcon:
            def generate_regions(self, rois): return "mock"
            def extract_statistics(self, regions, temp_image, rois=None):
                from thermal_monitor.core.models import ROIStatistics
                return [ROIStatistics(roi_id="roi_1", roi_name="Test ROI", min_temp=20.0, max_temp=30.0, mean_temp=25.0, deviation=2.0, unit=TemperatureUnit.CELSIUS)]
            def release_regions(self, regions): pass
        
        pipeline = SimpleProcessingPipeline(
            config=config,
            temperature_converter=converter,
            calibration_provider=MockCalibrationProvider(),
            halcon_adapter=MockHalcon(),
        )
        
        frame = self.create_test_frame()
        result = pipeline.process_frame(frame)
        
        assert result.camera_id == "test_cam"
        assert "roi_1" in result.roi_results


class TestHALCONROIRegression:
    """Ensure HALCON ROI processing is not affected by temperature converter changes."""
    
    def test_roi_adapter_interface_unchanged(self):
        """Test that HalconROIAdapter interface is unchanged."""
        adapter = HalconROIAdapter()
        
        # These methods must exist and work
        assert hasattr(adapter, 'generate_regions')
        assert hasattr(adapter, 'extract_statistics')
        assert hasattr(adapter, 'release_regions')
    
    def test_temperature_converter_independent_of_roi(self):
        """Test temperature conversion doesn't depend on ROI logic."""
        lut = make_test_lut()
        converter = CPUTemperatureConverter()
        
        # Conversion should work without any ROI configuration
        raw = np.full((480, 640), 30000, dtype=np.uint16)
        result = converter.raw_to_temperature(raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0)
        
        assert result.shape == (480, 640)
        assert result.dtype == np.float32
        # All pixels should have same temperature (uniform input)
        assert np.allclose(result, lut[30000])


class TestGPUAvailabilityDetection:
    """Tests for GPU availability detection functions."""
    
    def test_is_gpu_available_returns_bool(self):
        """Test is_gpu_available returns boolean."""
        result = is_gpu_available()
        assert isinstance(result, bool)
    
    def test_get_gpu_device_name(self):
        """Test get_gpu_device_name returns string or None."""
        result = get_gpu_device_name()
        assert result is None or isinstance(result, str)


class TestGPUTemperatureConverterLifecycle:
    """Tests for GPU converter LUT lifecycle management."""

    @pytest.mark.skipif(not is_gpu_available(), reason="GPU not available")
    def test_load_lut_and_release(self):
        """Test explicit load_lut and release methods."""
        converter = GPUTemperatureConverter()
        lut = make_valid_lut()
        
        # Initially no LUTs loaded
        assert converter.get_loaded_cameras() == []
        assert not converter.is_ready()
        
        # Load LUT for camera_a
        result = converter.load_lut("camera_a", lut)
        assert result is True
        assert converter.is_ready()
        assert "camera_a" in converter.get_loaded_cameras()
        
        # Load LUT for camera_b
        result = converter.load_lut("camera_b", lut)
        assert result is True
        assert "camera_b" in converter.get_loaded_cameras()
        assert len(converter.get_loaded_cameras()) == 2
        
        # Release camera_a
        converter.release("camera_a")
        assert "camera_a" not in converter.get_loaded_cameras()
        assert "camera_b" in converter.get_loaded_cameras()
        assert converter.is_ready()  # Still ready because camera_b loaded
        
        # Release camera_b
        converter.release("camera_b")
        assert converter.get_loaded_cameras() == []
        assert not converter.is_ready()
        
        # Release non-existent camera (should not raise)
        converter.release("non_existent")
        assert converter.get_loaded_cameras() == []

    @pytest.mark.skipif(not is_gpu_available(), reason="GPU not available")
    def test_release_all(self):
        """Test release_all clears all LUTs."""
        converter = GPUTemperatureConverter()
        lut = make_valid_lut()
        
        converter.load_lut("camera_a", lut)
        converter.load_lut("camera_b", lut)
        assert len(converter.get_loaded_cameras()) == 2
        
        converter.release_all()
        assert converter.get_loaded_cameras() == []
        assert not converter.is_ready()

    @pytest.mark.skipif(not is_gpu_available(), reason="GPU not available")
    def test_load_lut_invalid_lut_raises(self):
        """Test load_lut validates LUT format."""
        converter = GPUTemperatureConverter()
        
        # Wrong dtype
        bad_lut = np.arange(65536, dtype=np.float64)
        with pytest.raises(ValueError, match="LUT must be float32"):
            converter.load_lut("camera_a", bad_lut)
        
        # Wrong size
        bad_lut = np.arange(1000, dtype=np.float32)
        with pytest.raises(ValueError, match="LUT must have 65536 entries"):
            converter.load_lut("camera_a", bad_lut)
        
        # None
        with pytest.raises(ValueError, match="Calibration LUT is None"):
            converter.load_lut("camera_a", None)

    def test_load_lut_behavior_matches_gpu_availability(self):
        """Test load_lut returns appropriate value based on GPU availability."""
        converter = GPUTemperatureConverter()
        lut = make_valid_lut()
        
        result = converter.load_lut("camera_a", lut)
        
        if is_gpu_available():
            # On GPU machine, load_lut should succeed
            assert result is True
            assert converter.is_ready()
            assert "camera_a" in converter.get_loaded_cameras()
        else:
            # On non-GPU machine, load_lut should return False
            assert result is False
            assert not converter.is_ready()
            assert converter.get_loaded_cameras() == []


class TestGPUTemperatureConverterMultiCamera:
    """Tests for multi-camera LUT isolation."""

    @pytest.mark.skipif(not is_gpu_available(), reason="GPU not available")
    def test_multi_camera_lut_isolation(self):
        """Test two cameras with different LUTs cannot share wrong LUT."""
        converter = GPUTemperatureConverter()
        
        # Create two different LUTs
        lut_a = np.arange(65536, dtype=np.float32) * 0.01   # 0.01°C per raw
        lut_b = np.arange(65536, dtype=np.float32) * 0.02   # 0.02°C per raw
        
        # Load different LUTs for different cameras
        converter.load_lut("camera_a", lut_a)
        converter.load_lut("camera_b", lut_b)
        
        # Test raw value that produces different temperatures
        raw = np.array([[1000]], dtype=np.uint16)
        
        # Convert with camera_a
        result_a = converter.raw_to_temperature(raw, lut_a, 1.0, 25.0, 1.0, 50.0, 25.0, camera_id="camera_a")
        # Convert with camera_b
        result_b = converter.raw_to_temperature(raw, lut_b, 1.0, 25.0, 1.0, 50.0, 25.0, camera_id="camera_b")
        
        # Results should be different (0.01*1000 = 10.0 vs 0.02*1000 = 20.0)
        assert result_a[0, 0] == lut_a[1000]
        assert result_b[0, 0] == lut_b[1000]
        assert result_a[0, 0] != result_b[0, 0]
        assert abs(result_a[0, 0] - 10.0) < 1e-5
        assert abs(result_b[0, 0] - 20.0) < 1e-5

    @pytest.mark.skipif(not is_gpu_available(), reason="GPU not available")
    def test_camera_id_required_for_multi_camera(self):
        """Test that camera_id parameter is required for multi-camera isolation."""
        converter = GPUTemperatureConverter()
        
        lut_a = np.arange(65536, dtype=np.float32) * 0.01
        lut_b = np.arange(65536, dtype=np.float32) * 0.02
        
        converter.load_lut("camera_a", lut_a)
        converter.load_lut("camera_b", lut_b)
        
        raw = np.array([[1000]], dtype=np.uint16)
        
        # Without camera_id, should use default LUT (first loaded or explicit)
        # This tests backward compatibility - when camera_id not provided,
        # it uses a default key
        result_default = converter.raw_to_temperature(raw, lut_a, 1.0, 25.0, 1.0, 50.0, 25.0)
        
        # Should still work (uses default key)
        assert result_default[0, 0] == lut_a[1000]

    @pytest.mark.skipif(not is_gpu_available(), reason="GPU not available")
    def test_concurrent_camera_processing(self):
        """Test processing frames from multiple cameras in sequence."""
        converter = GPUTemperatureConverter()
        
        # Create distinct LUTs for 3 cameras
        lut_cam1 = np.arange(65536, dtype=np.float32) * 0.01
        lut_cam2 = np.arange(65536, dtype=np.float32) * 0.02
        lut_cam3 = np.arange(65536, dtype=np.float32) * 0.05
        
        converter.load_lut("cam1", lut_cam1)
        converter.load_lut("cam2", lut_cam2)
        converter.load_lut("cam3", lut_cam3)
        
        raw = np.full((100, 100), 2000, dtype=np.uint16)
        
        # Process frames from each camera in alternating sequence
        for _ in range(10):
            result1 = converter.raw_to_temperature(raw, lut_cam1, 1.0, 25.0, 1.0, 50.0, 25.0, camera_id="cam1")
            result2 = converter.raw_to_temperature(raw, lut_cam2, 1.0, 25.0, 1.0, 50.0, 25.0, camera_id="cam2")
            result3 = converter.raw_to_temperature(raw, lut_cam3, 1.0, 25.0, 1.0, 50.0, 25.0, camera_id="cam3")
            
            # Each camera should produce its own temperature
            assert result1[0, 0] == lut_cam1[2000]
            assert result2[0, 0] == lut_cam2[2000]
            assert result3[0, 0] == lut_cam3[2000]
            assert result1[0, 0] != result2[0, 0] != result3[0, 0]


class TestGPUFallbackReuse:
    """Tests for CPU fallback instance reuse."""

    def test_cpu_fallback_reuses_instance(self):
        """Test that CPU fallback reuses the same converter instance."""
        converter = GPUTemperatureConverter()
        lut = make_valid_lut()
        
        # First call - should create CPU converter instance
        raw = np.array([[1000]], dtype=np.uint16)
        result1 = converter.raw_to_temperature(raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0)
        
        # Get the CPU converter instance
        cpu_converter1 = converter._cpu_converter
        assert cpu_converter1 is not None
        
        # Second call - should reuse same instance
        result2 = converter.raw_to_temperature(raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0)
        
        cpu_converter2 = converter._cpu_converter
        assert cpu_converter2 is cpu_converter1, "CPU converter instance should be reused"
        
        # Results should be identical
        assert np.array_equal(result1, result2)

    def test_cpu_fallback_with_camera_id(self):
        """Test CPU fallback works with camera_id parameter."""
        converter = GPUTemperatureConverter()
        lut = make_valid_lut()
        
        raw = np.array([[1000]], dtype=np.uint16)
        result = converter.raw_to_temperature(raw, lut, 1.0, 25.0, 1.0, 50.0, 25.0, camera_id="test_cam")
        
        assert result[0, 0] == lut[1000]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])