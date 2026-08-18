"""
V3 Calibration Processor

Temperature conversion and lookup table generation.
Matches V2 proven implementation exactly.
"""

from __future__ import annotations

import math

import numpy as np

from thermal_monitor.calibration.models import (
    CameraCalibration,
    CalibrationRange,
    UniverseSegment,
)

from thermal_monitor.core.logging import get_logger

logger = get_logger(__name__)


class CalibrationProcessor:
    """
    Processes calibration data.

    Responsibilities:
    - Build lookup tables
    - Convert Raw -> Temperature
    - Convert Temperature -> Display
    - Calculate statistics
    """

    LUT_SIZE = 65536

    @classmethod
    def raw_to_temperature(
        cls,
        raw_image: np.ndarray,
        calibration: CameraCalibration,
        range_index: int = 0,
    ) -> np.ndarray:
        """
        Convert uint16 detector image into a float32
        temperature image using the lookup table.
        """
        lut = calibration.get_lookup_table(range_index)

        if lut is None:
            raise RuntimeError("Lookup table has not been generated.")

        raw_image = raw_image.astype(np.uint16, copy=False)

        return lut[raw_image]

    @staticmethod
    def solve_polynomial(
        raw_power: float,
        segment: UniverseSegment,
    ) -> float | None:
        """
        Solve the inverse quadratic calibration equation.

            raw = u0 + u1*T + u2*T²

        Returns
        -------
        Temperature in °C or None
        """
        if segment.u2 == 0.0:
            return None

        discriminant = (
            segment.u1 * segment.u1
            - 4.0 * segment.u2 * (segment.u0 - raw_power)
        )

        if discriminant < 0.0:
            return None

        temperature = (
            -segment.u1 + math.sqrt(discriminant)
        ) / (2.0 * segment.u2)

        if (
            segment.start_temp <= temperature <= segment.end_temp
        ):
            return temperature

        return None

    @classmethod
    def raw_value_to_temperature(
        cls,
        raw_value: int,
        calibration_range: CalibrationRange,
    ) -> float | None:
        """
        Convert one detector value into temperature.

        This is used only while building the lookup table.
        """
        valid_segments = calibration_range.segments[: calibration_range.num_segments]

        for segment in valid_segments:
            temperature = cls.solve_polynomial(raw_value, segment)
            if temperature is not None:
                return temperature

        return None

    @classmethod
    def build_lookup_tables(cls, calibration: CameraCalibration) -> None:
        """Build lookup tables for every calibration range."""
        logger.info("=" * 60)
        logger.info("Generating Lookup Tables")
        logger.info("=" * 60)

        for index, calibration_range in enumerate(calibration.ranges):
            lut = cls._build_lookup_table(calibration_range)
            calibration.set_lookup_table(index, lut)
            logger.info(f"Range {index} completed.")

        logger.info("Lookup table generation finished.")

    @classmethod
    def _build_lookup_table(cls, calibration_range: CalibrationRange) -> np.ndarray:
        """
        Build a 65536-entry lookup table using NumPy vectorization.
        """
        lut = np.full(cls.LUT_SIZE, np.nan, dtype=np.float32)

        raw = np.arange(cls.LUT_SIZE, dtype=np.float32)

        valid_segments = calibration_range.segments[: calibration_range.num_segments]

        logger.info(
            "Building LUT "
            f"({calibration_range.calibration_min:.1f}°C -> "
            f"{calibration_range.calibration_max:.1f}°C)"
        )

        for segment in valid_segments:
            if segment.u2 == 0.0:
                continue

            discriminant = (
                segment.u1 * segment.u1
                - 4.0 * segment.u2 * (segment.u0 - raw)
            )

            valid = discriminant >= 0.0

            if not np.any(valid):
                continue

            temperature = np.empty_like(raw)
            temperature.fill(np.nan)

            temperature[valid] = (
                -segment.u1 + np.sqrt(discriminant[valid])
            ) / (2.0 * segment.u2)

            mask = (
                valid
                & (temperature >= segment.start_temp)
                & (temperature <= segment.end_temp)
                & np.isnan(lut)
            )

            lut[mask] = temperature[mask]

        cls._fill_invalid_values(lut)

        return lut

    @staticmethod
    def _fill_invalid_values(lut: np.ndarray) -> None:
        """
        Replace NaN entries by linear interpolation.

        This creates a continuous LUT while preserving all calibrated values.
        """
        valid = np.isfinite(lut)

        if valid.all():
            return

        valid_indices = np.flatnonzero(valid)

        if valid_indices.size == 0:
            raise RuntimeError("Calibration produced an empty LUT.")

        invalid_indices = np.flatnonzero(~valid)

        lut[invalid_indices] = np.interp(
            invalid_indices, valid_indices, lut[valid_indices]
        ).astype(np.float32)

        first = valid_indices[0]
        if first > 0:
            lut[:first] = lut[first]

        last = valid_indices[-1]
        if last < len(lut) - 1:
            lut[last + 1 :] = lut[last]

        logger.info("Lookup table interpolation completed.")

    @staticmethod
    def temperature_to_display(
        temperature_image: np.ndarray,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> np.ndarray:
        """
        Convert a temperature image into an 8-bit grayscale image.
        """
        if temperature_image.size == 0:
            return np.zeros((1, 1), dtype=np.uint8)

        finite = np.isfinite(temperature_image)

        if not np.any(finite):
            return np.zeros(temperature_image.shape, dtype=np.uint8)

        valid = temperature_image[finite]

        if minimum is None:
            minimum = float(valid.min())

        if maximum is None:
            maximum = float(valid.max())

        if maximum <= minimum:
            maximum = minimum + 1.0

        normalized = np.clip(
            (temperature_image - minimum) / (maximum - minimum), 0.0, 1.0
        )

        normalized[~finite] = 0.0

        return (normalized * 255.0).astype(np.uint8)

    @classmethod
    def raw_to_display(
        cls,
        raw_image: np.ndarray,
        calibration: CameraCalibration,
        range_index: int = 0,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> np.ndarray:
        """Convert raw detector values directly to grayscale display."""
        temperature = cls.raw_to_temperature(raw_image, calibration, range_index)
        return cls.temperature_to_display(temperature, minimum, maximum)

    @staticmethod
    def apply_colormap(
        display_image: np.ndarray,
        colormap: int = 2,  # cv2.COLORMAP_INFERNO
    ) -> np.ndarray:
        """Apply an OpenCV false-color palette."""
        try:
            import cv2

            return cv2.applyColorMap(display_image, colormap)
        except ImportError:
            # Fallback: return grayscale as 3-channel
            return np.stack([display_image] * 3, axis=-1)

    @staticmethod
    def get_temperature_statistics(temperature_image: np.ndarray) -> dict:
        """Calculate image statistics."""
        finite = np.isfinite(temperature_image)

        if not np.any(finite):
            return {
                "minimum": np.nan,
                "maximum": np.nan,
                "mean": np.nan,
                "median": np.nan,
                "std": np.nan,
            }

        values = temperature_image[finite]

        return {
            "minimum": float(np.min(values)),
            "maximum": float(np.max(values)),
            "mean": float(np.mean(values)),
            "median": float(np.median(values)),
            "std": float(np.std(values)),
        }

    @staticmethod
    def get_roi_statistics(
        temperature_image: np.ndarray, roi_mask: np.ndarray
    ) -> dict:
        """Calculate statistics inside an ROI."""
        values = temperature_image[roi_mask]
        values = values[np.isfinite(values)]

        if values.size == 0:
            return {
                "minimum": np.nan,
                "maximum": np.nan,
                "mean": np.nan,
                "median": np.nan,
                "std": np.nan,
            }

        return {
            "minimum": float(values.min()),
            "maximum": float(values.max()),
            "mean": float(values.mean()),
            "median": float(np.median(values)),
            "std": float(values.std()),
        }

    @staticmethod
    def normalize_temperature(
        temperature_image: np.ndarray, minimum: float, maximum: float
    ) -> np.ndarray:
        """Normalize a temperature image to the range [0, 1]."""
        if maximum <= minimum:
            maximum = minimum + 1.0

        normalized = (temperature_image - minimum) / (maximum - minimum)
        normalized = np.clip(normalized, 0.0, 1.0)
        normalized[~np.isfinite(normalized)] = 0.0

        return normalized

    @staticmethod
    def clip_temperature(
        temperature_image: np.ndarray, minimum: float, maximum: float
    ) -> np.ndarray:
        """Clip temperature image."""
        return np.clip(temperature_image, minimum, maximum)

    @classmethod
    def validate_lookup_tables(cls, calibration: CameraCalibration) -> bool:
        """Ensure every calibration range has a valid LUT."""
        if not calibration.lookup_tables:
            logger.error("No lookup tables available.")
            return False

        for index, lut in calibration.lookup_tables.items():
            if lut is None:
                logger.error(f"Lookup table {index} is None.")
                return False

            if lut.dtype != np.float32:
                logger.error(f"Lookup table {index} has invalid dtype.")
                return False

            if lut.size != cls.LUT_SIZE:
                logger.error(f"Lookup table {index} has invalid size.")
                return False

        return True

    @staticmethod
    def clear_lookup_tables(calibration: CameraCalibration) -> None:
        """Remove all cached lookup tables."""
        calibration.lookup_tables.clear()
        logger.info("Lookup tables cleared.")

    @classmethod
    def rebuild_lookup_tables(cls, calibration: CameraCalibration) -> None:
        """Rebuild every lookup table."""
        cls.clear_lookup_tables(calibration)
        cls.build_lookup_tables(calibration)

    @staticmethod
    def get_calibration_range(
        calibration: CameraCalibration, index: int = 0
    ) -> CalibrationRange:
        """Return one calibration range."""
        if index >= len(calibration.ranges):
            raise IndexError("Invalid calibration range index.")
        return calibration.ranges[index]

    @staticmethod
    def validate_temperature_image(image: np.ndarray) -> bool:
        """Verify that a temperature image is valid."""
        if image is None:
            return False
        if image.size == 0:
            return False
        if image.dtype != np.float32:
            return False
        return True

    @staticmethod
    def validate_display_image(image: np.ndarray) -> bool:
        """Verify an 8-bit display image."""
        if image is None:
            return False
        if image.size == 0:
            return False
        if image.dtype != np.uint8:
            return False
        return True