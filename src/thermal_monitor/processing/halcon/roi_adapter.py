"""
processing.halcon.roi_adapter -- HALCON ROI statistics adapter.

Implements the proven V2 ROI statistics pipeline using HALCON operators.
Hides all HALCON types from the rest of V3.

Proven behavior (from halcon_roi_validation.py):
- Rectangle1: gen_rectangle1 + intensity + min_max_gray (batched)
- Statistics: mean, deviation, minimum, maximum, range

HALCON-ready extensions (not V2-production-validated):
- Rectangle2: gen_rectangle2
- Circle: gen_circle
- Ellipse: gen_ellipse
- Polygon: gen_region_polygon_filled
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Sequence, Any

import numpy as np

from thermal_monitor.core.models import ROIConfig, ROIStatistics, ROIShape, TemperatureUnit

_logger = logging.getLogger(__name__)

# Type alias for opaque HALCON region handle
HalconRegionHandle = Any


@dataclass(slots=True)
class HalconROIAdapter:
    """
    Thin wrapper around proven HALCON ROI statistics.

    Hides: HObject, HALCON tuples, row/col convention, batched operators.
    """
    _clip_region_configured: bool = False

    def generate_regions(self, rois: Sequence[ROIConfig]) -> HalconRegionHandle:
        """
        Create batched HALCON regions from ROI configurations.

        Args:
            rois: Sequence of ROIConfig with supported geometries.
                  All coordinates use HALCON row/col convention (y/x).

        Returns:
            Opaque HALCON region tuple (HObject) — do not inspect directly.

        Raises:
            ValueError: If an unsupported geometry is encountered.
            RuntimeError: If HALCON region generation fails.
        """
        import halcon as ha

        self._configure_clip_region(ha)

        if not rois:
            return ha.gen_empty_obj()

        # Group ROIs by shape for batched generation
        regions_by_shape: dict[ROIShape, list[ROIConfig]] = {}
        for roi in rois:
            if not roi.enabled:
                continue
            regions_by_shape.setdefault(roi.geometry.shape, []).append(roi)

        combined_regions = ha.gen_empty_obj()
        first = True

        for shape, shape_rois in regions_by_shape.items():
            if not shape_rois:
                continue

            if shape == ROIShape.RECTANGLE1:
                region = self._gen_rectangle1_batch(ha, shape_rois)
            elif shape == ROIShape.RECTANGLE2:
                region = self._gen_rectangle2_batch(ha, shape_rois)
            elif shape == ROIShape.CIRCLE:
                region = self._gen_circle_batch(ha, shape_rois)
            elif shape == ROIShape.ELLIPSE:
                region = self._gen_ellipse_batch(ha, shape_rois)
            elif shape == ROIShape.POLYGON:
                region = self._gen_polygon_batch(ha, shape_rois)
            else:
                raise ValueError(f"Unsupported ROI shape: {shape}")

            if first:
                combined_regions = region
                first = False
            else:
                combined_regions = ha.concat_obj(combined_regions, region)

        return combined_regions

    def extract_statistics(
        self,
        regions: HalconRegionHandle,
        temperature_image: np.ndarray,  # float32, °C
        rois: Sequence[ROIConfig] | None = None,
    ) -> list[ROIStatistics]:
        """
        Compute statistics using proven HALCON operators.

        Uses: ha.intensity + ha.min_max_gray (batched per shape)

        Args:
            regions: Opaque HALCON region handle from generate_regions()
            temperature_image: Calibrated temperature image (float32, °C)

        Returns:
            List of ROIStatistics with:
            - min_temp, max_temp, mean_temp, deviation, range_temp
            - roi_id, roi_name, unit=CELSIUS
        """
        import halcon as ha

        if temperature_image is None or temperature_image.size == 0:
            return []

        # Convert temperature image to HALCON
        halcon_temp_image = ha.himage_from_numpy_array(temperature_image.astype(np.float32))

        # Get all regions as a tuple and iterate
        # Note: HALCON returns tuples aligned with the input order
        results: list[ROIStatistics] = []

        # We need to process each ROI individually since we need per-ROI statistics
        # The batched approach from V2 processes all ROIs of the same shape together
        # For simplicity and correctness, we iterate through the region tuple

        # Get region count
        try:
            region_count = ha.count_obj(regions)
        except Exception:
            _logger.exception("Failed to count HALCON regions")
            return []

        for i in range(1, region_count + 1):
            try:
                single_region = ha.select_obj(regions, i)

                # Proven V2 statistics pipeline
                mean_vals, dev_vals = ha.intensity(single_region, halcon_temp_image)
                min_vals, max_vals, range_vals = ha.min_max_gray(single_region, halcon_temp_image, 0)

                # HALCON returns tuples; for single region, each is a single-element tuple
                mean_val = float(mean_vals[0]) if mean_vals else 0.0
                dev_val = float(dev_vals[0]) if dev_vals else 0.0
                min_val = float(min_vals[0]) if min_vals else 0.0
                max_val = float(max_vals[0]) if max_vals else 0.0
                range_val = float(range_vals[0]) if range_vals else 0.0

                results.append(ROIStatistics(
                    roi_id="",  # Will be filled by caller (or from rois below)
                    roi_name="",  # Will be filled by caller (or from rois below)
                    min_temp=min_val,
                    max_temp=max_val,
                    mean_temp=mean_val,
                    deviation=dev_val,
                    unit=TemperatureUnit.CELSIUS,
                ))
            except Exception as e:
                _logger.warning(f"Failed to extract statistics for region {i}: {e}")
                results.append(ROIStatistics(
                    roi_id="",
                    roi_name="",
                    min_temp=0.0,
                    max_temp=0.0,
                    mean_temp=0.0,
                    deviation=0.0,
                    unit=TemperatureUnit.CELSIUS,
                ))

        if rois is not None:
            for stat, roi in zip(results, rois):
                results[results.index(stat)] = ROIStatistics(
                    roi_id=roi.roi_id,
                    roi_name=roi.name,
                    min_temp=stat.min_temp,
                    max_temp=stat.max_temp,
                    mean_temp=stat.mean_temp,
                    deviation=stat.deviation,
                    unit=stat.unit,
                )

        return results

    def release_regions(self, regions: HalconRegionHandle) -> None:
        """Release HALCON region resources.

        HALCON Python bindings (24.11) use reference counting for HObject
        handles: dropping the Python reference lets the wrapper free the
        underlying object exactly once.  Calling clear_obj() here on a handle
        still referenced by the Python wrapper causes a second delete at
        garbage-collection time (HALCON error #4051 "object has been deleted
        already"), so we rely on reference counting instead.
        """
        del regions

    # ------------------------------------------------------------------
    # Internal region generation methods
    # ------------------------------------------------------------------

    def _configure_clip_region(self, ha: Any) -> None:
        """Disable HALCON region clipping so standalone region generation works."""
        if self._clip_region_configured:
            return
        try:
            ha.set_system("clip_region", "false")
            self._clip_region_configured = True
        except Exception as e:
            _logger.warning(f"Failed to set HALCON clip_region=false: {e}")

    def _round(self, value: float) -> int:
        """Round a float to the nearest integer (HALCON pixel coordinate)."""
        return int(round(value))

    def _gen_rectangle1_batch(self, ha: Any, rois: Sequence[ROIConfig]) -> HalconRegionHandle:
        """Generate batched Rectangle1 regions (V2-proven)."""
        rows1 = [self._round(float(roi.geometry.parameters["y1"])) for roi in rois]
        cols1 = [self._round(float(roi.geometry.parameters["x1"])) for roi in rois]
        rows2 = [self._round(float(roi.geometry.parameters["y2"])) for roi in rois]
        cols2 = [self._round(float(roi.geometry.parameters["x2"])) for roi in rois]
        return ha.gen_rectangle1(rows1, cols1, rows2, cols2)

    def _gen_rectangle2_batch(self, ha: Any, rois: Sequence[ROIConfig]) -> HalconRegionHandle:
        """Generate batched Rectangle2 regions (HALCON-ready)."""
        rows = [float(roi.geometry.parameters["center_y"]) for roi in rois]
        cols = [float(roi.geometry.parameters["center_x"]) for roi in rois]
        phis = [float(roi.geometry.parameters["phi"]) for roi in rois]
        length1s = [float(roi.geometry.parameters["length1"]) for roi in rois]
        length2s = [float(roi.geometry.parameters["length2"]) for roi in rois]
        return ha.gen_rectangle2(rows, cols, phis, length1s, length2s)

    def _gen_circle_batch(self, ha: Any, rois: Sequence[ROIConfig]) -> HalconRegionHandle:
        """Generate batched Circle regions (HALCON-ready)."""
        rows = [float(roi.geometry.parameters["center_y"]) for roi in rois]
        cols = [float(roi.geometry.parameters["center_x"]) for roi in rois]
        radii = [float(roi.geometry.parameters["radius"]) for roi in rois]
        return ha.gen_circle(rows, cols, radii)

    def _gen_ellipse_batch(self, ha: Any, rois: Sequence[ROIConfig]) -> HalconRegionHandle:
        """Generate batched Ellipse regions (HALCON-ready)."""
        rows = [float(roi.geometry.parameters["center_y"]) for roi in rois]
        cols = [float(roi.geometry.parameters["center_x"]) for roi in rois]
        phis = [float(roi.geometry.parameters["phi"]) for roi in rois]
        radius1s = [float(roi.geometry.parameters["radius1"]) for roi in rois]
        radius2s = [float(roi.geometry.parameters["radius2"]) for roi in rois]
        return ha.gen_ellipse(rows, cols, phis, radius1s, radius2s)

    def _gen_polygon_batch(self, ha: Any, rois: Sequence[ROIConfig]) -> HalconRegionHandle:
        """Generate batched Polygon regions (HALCON-ready)."""
        # Polygons must be generated individually and concatenated
        # because each polygon can have different number of vertices
        regions = []
        for roi in rois:
            points = roi.geometry.parameters["points"]
            rows = [float(p[0]) for p in points]
            cols = [float(p[1]) for p in points]
            region = ha.gen_region_polygon_filled(rows, cols)
            regions.append(region)

        if not regions:
            return ha.gen_empty_obj()

        combined = regions[0]
        for region in regions[1:]:
            combined = ha.concat_obj(combined, region)
        return combined


def process_rois_with_halcon(
    rois: Sequence[ROIConfig],
    temperature_image: np.ndarray,
    adapter: HalconROIAdapter | None = None,
) -> list[ROIStatistics]:
    """
    Convenience function: generate regions, extract statistics, release regions.

    Args:
        rois: ROI configurations to process
        temperature_image: Calibrated temperature image (float32, °C)
        adapter: Optional pre-created adapter instance

    Returns:
        List of ROIStatistics with roi_id and roi_name populated
    """
    if adapter is None:
        adapter = HalconROIAdapter()

    enabled_rois = [roi for roi in rois if roi.enabled]
    if not enabled_rois:
        return []

    regions = adapter.generate_regions(enabled_rois)
    try:
        stats = adapter.extract_statistics(regions, temperature_image, rois=enabled_rois)
        # Populate roi_id and roi_name from input ROIs
        for stat, roi in zip(stats, enabled_rois):
            # Create new ROIStatistics with correct IDs (dataclass is frozen)
            idx = stats.index(stat)
            stats[idx] = ROIStatistics(
                roi_id=roi.roi_id,
                roi_name=roi.name,
                min_temp=stat.min_temp,
                max_temp=stat.max_temp,
                mean_temp=stat.mean_temp,
                deviation=stat.deviation,
                unit=stat.unit,
            )
        return stats
    finally:
        adapter.release_regions(regions)