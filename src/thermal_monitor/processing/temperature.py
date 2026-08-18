"""
V3 CPU Temperature Converter Implementation

Implements the TemperatureConverter protocol using the proven V2 calibration
algorithm with 65536-entry LUT lookup.
"""

from __future__ import annotations

import numpy as np
from pathlib import Path

from thermal_monitor.calibration.processor import CalibrationProcessor
from thermal_monitor.calibration.models import CameraCalibration
from thermal_monitor.calibration.parser import CalibrationParser
from thermal_monitor.processing.pipeline import TemperatureConverter, CalibrationProvider


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
        if calibration is None and self._calibration_provider is not None:
            # This would need a camera_id; for now raise if not provided
            raise ValueError("Calibration array required when no camera_id available")

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