"""
V3 Temperature Converter Implementations

Implements the TemperatureConverter protocol using the proven V2 calibration
algorithm with 65536-entry LUT lookup.

Provides both CPU and GPU implementations behind the same protocol.
"""

from __future__ import annotations

import numpy as np
from pathlib import Path

from thermal_monitor.calibration.processor import CalibrationProcessor
from thermal_monitor.calibration.models import CameraCalibration
from thermal_monitor.calibration.parser import CalibrationParser
from thermal_monitor.processing.pipeline import TemperatureConverter, CalibrationProvider


def is_gpu_available() -> bool:
    """
    Check if CuPy GPU backend is available.

    Returns True if CuPy can be imported and a CUDA device is accessible.
    Does not raise on import failure or CUDA errors.
    """
    try:
        import cupy as cp
        cp.cuda.runtime.getDeviceCount()
        return True
    except Exception:
        return False


def get_gpu_device_name() -> str | None:
    """Get GPU device name if available, else None."""
    try:
        import cupy as cp
        device_count = cp.cuda.runtime.getDeviceCount()
        if device_count > 0:
            return cp.cuda.Device(0).name().decode() if isinstance(cp.cuda.Device(0).name(), bytes) else cp.cuda.Device(0).name()
    except Exception:
        pass
    return None


class CPUTemperatureConverter:
    """
    CPU reference implementation of TemperatureConverter.

    Uses the proven V2 calibration algorithm:
    - 65536-entry LUT per calibration range
    - Vectorized NumPy lookup for performance
    - Inverse quadratic polynomial: raw = u0 + u1*T + u2*T²
    - Linear interpolation for out-of-range values

    The emissivity, ambient_temp, distance, humidity, and reflected_temp
    parameters are accepted for protocol compatibility but are NOT used in
    the V2 proven algorithm. They are reserved for future GPU implementation
    or extended physics models.
    """

    def __init__(self, calibration_provider: CalibrationProvider | None = None):
        self._calibration_provider = calibration_provider

    def raw_to_temperature(
        self,
        raw_data: np.ndarray,
        calibration: np.ndarray | None,
        emissivity: float,
        ambient_temp: float,
        distance: float,
        humidity: float,
        reflected_temp: float,
    ) -> np.ndarray:
        """
        Convert raw thermal data to temperature values.

        Parameters
        ----------
        raw_data : np.ndarray
            Raw uint16 thermal image (H, W)
        calibration : np.ndarray | None
            Calibration LUT array (65536, float32) or CameraCalibration object.
            If None and calibration_provider is set, attempts to fetch from provider.
        emissivity : float
            Emissivity (accepted for protocol, not used in V2 algorithm)
        ambient_temp : float
            Ambient temperature °C (accepted for protocol, not used in V2 algorithm)
        distance : float
            Distance meters (accepted for protocol, not used in V2 algorithm)
        humidity : float
            Relative humidity % (accepted for protocol, not used in V2 algorithm)
        reflected_temp : float
            Reflected temperature °C (accepted for protocol, not used in V2 algorithm)

        Returns
        -------
        np.ndarray
            Temperature image float32 °C (H, W)

        Notes
        -----
        V2 algorithm uses only the LUT for conversion. The additional physics
        parameters (emissivity, ambient, distance, humidity, reflected) are
        part of the protocol for future GPU/extended implementations but do
        not affect the proven V2 CPU result.
        """
        # Handle calibration input
        if calibration is None:
            if self._calibration_provider is not None:
                raise ValueError("Calibration array required when no camera_id available")
            else:
                raise ValueError("Calibration LUT is None")

        # Determine the LUT to use
        if isinstance(calibration, CameraCalibration):
            # Use default range (0) as per V2 proven behavior
            lut = calibration.get_lookup_table(0)
            if lut is None:
                raise RuntimeError("Lookup table not built for calibration")
        elif isinstance(calibration, np.ndarray):
            lut = calibration
        else:
            raise TypeError(
                f"Calibration must be CameraCalibration or np.ndarray, got {type(calibration)}"
            )

        # Validate LUT
        if lut is None:
            raise ValueError("Calibration LUT is None")
        if lut.dtype != np.float32:
            raise ValueError(f"LUT must be float32, got {lut.dtype}")
        if lut.size != 65536:
            raise ValueError(f"LUT must have 65536 entries, got {lut.size}")

        # Convert raw data to uint16 (no copy if already uint16)
        raw_uint16 = raw_data.astype(np.uint16, copy=False)

        # Vectorized LUT lookup - the core V2 proven operation
        temperature_image = lut[raw_uint16]

        return temperature_image


class CachingCalibrationProvider:
    """
    CalibrationProvider implementation that loads and caches calibration per camera.

    Loads calibration blob once per camera_id and builds LUTs.
    """

    def __init__(self, calibration_file_map: dict[str, str] | None = None):
        """
        Parameters
        ----------
        calibration_file_map : dict[str, str] | None
            Mapping from camera_id to calibration file path.
            If None, uses default path for all cameras.
        """
        self._calibration_file_map = calibration_file_map or {}
        self._cache: dict[str, CameraCalibration] = {}
        self._parser = CalibrationParser()

    def get_calibration(self, camera_id: str) -> np.ndarray | None:
        """
        Get calibration LUT array for a camera.

        Returns the LUT for range 0 (default) as per V2 proven behavior.
        """
        calibration = self._get_or_load_calibration(camera_id)
        if calibration is None:
            return None
        return calibration.get_lookup_table(0)

    def get_camera_calibration(self, camera_id: str) -> CameraCalibration | None:
        """Get full CameraCalibration object for a camera."""
        return self._get_or_load_calibration(camera_id)

    def _get_or_load_calibration(self, camera_id: str) -> CameraCalibration | None:
        if camera_id in self._cache:
            return self._cache[camera_id]

        file_path = self._calibration_file_map.get(camera_id)
        if file_path is None:
            # Try default location
            default_path = Path("assets/calibration/calibration_blob.txt")
            if default_path.exists():
                file_path = str(default_path)
            else:
                return None

        try:
            calibration = self._parser.load(Path(file_path))
            CalibrationProcessor.build_lookup_tables(calibration)
            self._cache[camera_id] = calibration
            return calibration
        except Exception as e:
            # Log error but don't crash - return None
            from thermal_monitor.core.logging import get_logger
            logger = get_logger(__name__)
            logger.error(f"Failed to load calibration for {camera_id}: {e}")
            return None

    def clear_cache(self) -> None:
        """Clear all cached calibrations."""
        self._cache.clear()


class GPUTemperatureConverter:
    """
    GPU implementation of TemperatureConverter using CuPy.

    Uses the same proven V2 calibration algorithm:
    - 65536-entry LUT per calibration range (uploaded once to GPU)
    - Vectorized GPU lookup via CuPy advanced indexing
    - Identical numerical results to CPU implementation

    The LUT is uploaded to GPU memory once during initialization and reused
    for all subsequent frames, avoiding repeated host-to-device transfers.

    Falls back to CPU if CuPy is unavailable or GPU memory allocation fails.
    """

    def __init__(
        self,
        calibration_provider: CalibrationProvider | None = None,
        device_id: int = 0,
    ):
        self._calibration_provider = calibration_provider
        self._device_id = device_id
        self._gpu_lut: "cp.ndarray | None" = None
        self._lut_ready = False
        self._cp = None
        self._init_cupy()

    def _init_cupy(self) -> None:
        """Initialize CuPy context and verify GPU availability."""
        try:
            import cupy
            self._cp = cupy
            # Verify device exists
            device_count = cupy.cuda.runtime.getDeviceCount()
            if device_count <= self._device_id:
                raise RuntimeError(f"GPU device {self._device_id} not found (count: {device_count})")
            cupy.cuda.Device(self._device_id).use()
        except Exception as e:
            self._cp = None
            self._lut_ready = False
            from thermal_monitor.core.logging import get_logger
            logger = get_logger(__name__)
            logger.warning(f"GPUTemperatureConverter: CuPy initialization failed: {e}")

    def _ensure_lut_on_gpu(self, calibration: np.ndarray | CameraCalibration) -> bool:
        """Upload LUT to GPU if not already present."""
        if self._cp is None:
            return False

        if self._lut_ready and self._gpu_lut is not None:
            return True

        # Extract LUT array
        if isinstance(calibration, CameraCalibration):
            lut = calibration.get_lookup_table(0)
            if lut is None:
                raise RuntimeError("Lookup table not built for calibration")
        elif isinstance(calibration, np.ndarray):
            lut = calibration
        else:
            raise TypeError(f"Calibration must be CameraCalibration or np.ndarray, got {type(calibration)}")

        if lut is None:
            raise ValueError("Calibration LUT is None")
        if lut.dtype != np.float32:
            raise ValueError(f"LUT must be float32, got {lut.dtype}")
        if lut.size != 65536:
            raise ValueError(f"LUT must have 65536 entries, got {lut.size}")

        # Upload to GPU (async copy, then synchronize)
        try:
            self._gpu_lut = self._cp.asarray(lut)
            # Ensure upload completes
            self._cp.cuda.Stream.null.synchronize()
            self._lut_ready = True
            return True
        except Exception as e:
            from thermal_monitor.core.logging import get_logger
            logger = get_logger(__name__)
            logger.error(f"Failed to upload LUT to GPU: {e}")
            self._gpu_lut = None
            self._lut_ready = False
            return False

    def raw_to_temperature(
        self,
        raw_data: np.ndarray,
        calibration: np.ndarray | None,
        emissivity: float,
        ambient_temp: float,
        distance: float,
        humidity: float,
        reflected_temp: float,
    ) -> np.ndarray:
        """
        Convert raw thermal data to temperature values on GPU.

        Parameters
        ----------
        raw_data : np.ndarray
            Raw uint16 thermal image (H, W)
        calibration : np.ndarray | None
            Calibration LUT array (65536, float32) or CameraCalibration object.
            If None and calibration_provider is set, attempts to fetch from provider.
        emissivity : float
            Emissivity (accepted for protocol, not used in V2 algorithm)
        ambient_temp : float
            Ambient temperature °C (accepted for protocol, not used in V2 algorithm)
        distance : float
            Distance meters (accepted for protocol, not used in V2 algorithm)
        humidity : float
            Relative humidity % (accepted for protocol, not used in V2 algorithm)
        reflected_temp : float
            Reflected temperature °C (accepted for protocol, not used in V2 algorithm)

        Returns
        -------
        np.ndarray
            Temperature image float32 °C (H, W)

        Notes
        -----
        V2 algorithm uses only the LUT for conversion. The additional physics
        parameters (emissivity, ambient, distance, humidity, reflected) are
        part of the protocol for future GPU/extended implementations but do
        not affect the proven V2 CPU result.

        If GPU is unavailable or upload fails, falls back to CPU implementation.
        """
        if self._cp is None:
            # CuPy not available - fall back to CPU
            cpu_converter = CPUTemperatureConverter(self._calibration_provider)
            return cpu_converter.raw_to_temperature(
                raw_data, calibration, emissivity, ambient_temp, distance, humidity, reflected_temp
            )

        # Handle calibration input
        if calibration is None and self._calibration_provider is not None:
            raise ValueError("Calibration array required when no camera_id available")

        # Ensure LUT is on GPU
        if not self._ensure_lut_on_gpu(calibration):
            # GPU upload failed - fall back to CPU
            cpu_converter = CPUTemperatureConverter(self._calibration_provider)
            return cpu_converter.raw_to_temperature(
                raw_data, calibration, emissivity, ambient_temp, distance, humidity, reflected_temp
            )

        try:
            # Convert raw data to uint16 (no copy if already uint16)
            raw_uint16 = raw_data.astype(np.uint16, copy=False)

            # Upload raw frame to GPU
            gpu_raw = self._cp.asarray(raw_uint16)

            # GPU LUT lookup - the core operation
            gpu_temp = self._gpu_lut[gpu_raw]

            # Download result to host
            temp_image = self._cp.asnumpy(gpu_temp)

            return temp_image

        except Exception as e:
            from thermal_monitor.core.logging import get_logger
            logger = get_logger(__name__)
            logger.error(f"GPU conversion failed, falling back to CPU: {e}")
            # Fallback to CPU on any GPU error
            cpu_converter = CPUTemperatureConverter(self._calibration_provider)
            return cpu_converter.raw_to_temperature(
                raw_data, calibration, emissivity, ambient_temp, distance, humidity, reflected_temp
            )

    def is_ready(self) -> bool:
        """Check if GPU converter is ready (CuPy available and LUT uploaded)."""
        return self._cp is not None and self._lut_ready

    def get_device_name(self) -> str | None:
        """Get GPU device name if available."""
        if self._cp is None:
            return None
        try:
            name = self._cp.cuda.Device(self._device_id).name()
            return name.decode() if isinstance(name, bytes) else name
        except Exception:
            return None