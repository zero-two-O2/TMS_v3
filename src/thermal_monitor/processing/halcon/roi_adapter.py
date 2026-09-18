"""
processing.halcon.roi_adapter -- HALCON ROI statistics adapter.

Implements the proven V2 ROI statistics pipeline using HALCON operators.
Hides all HALCON types from the rest of V3.

Phase 12.3 architecture (see ADR-017):

- Transient grouped views (grouped.build_shape_groups) align ROI IDs
  with HALCON tuple order. Positional zip mapping only -- never
  ``list.index``.
- RegionCache (one per adapter instance; the adapter is owned by one
  pipeline on one worker thread) rebuilds only dirty shape groups.
- One ``himage_from_numpy_array`` per processed frame. The binding
  deep-copies, so the caller's NumPy array is never mutated or
  retained (proven in Phase 12.2).
- Batched ``intensity`` + ``min_max_gray`` + ``area_center`` per shape
  group. No per-ROI ``select_obj`` statistics loop on any path.
- Invalid/NaN measurements are explicit (NaN temps, valid=False) and
  never 0.0, so the alarm evaluator cannot false-trigger on them
  (NaN comparisons evaluate False -> INFO).

Region generation operators (all verified against the installed
binding in Phase 12.2 proof tests):

- Rectangle1: gen_rectangle1 (batched)
- Rectangle2: gen_rectangle2 (batched)
- Circle: gen_circle (batched)
- Ellipse: gen_ellipse (batched)
- Polygon: gen_region_polygon_filled per polygon + concat_obj
  (vertex counts differ per ROI, so no single batched call exists)
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Sequence, Any

import numpy as np

from thermal_monitor.core.models import ROIConfig, ROIStatistics, ROIShape, TemperatureUnit
from thermal_monitor.processing.halcon.grouped import (
    InvalidRoi,
    ShapeGroup,
    build_shape_groups,
)
from thermal_monitor.processing.halcon.region_cache import RegionCache

_logger = logging.getLogger(__name__)

# Type alias for opaque HALCON region handle
HalconRegionHandle = Any

BATCHED_METHOD = "halcon_batched"

# Warn at most once per minute per key on repeated frame failures so a
# stuck context cannot flood the logs while still surfacing new issues.
_WARN_INTERVAL_S = 60.0


def _invalid_stats(roi_id: str, roi_name: str, error: str) -> ROIStatistics:
    """Explicitly invalid measurement. NaN (never 0.0) + valid=False."""
    nan = float("nan")
    return ROIStatistics(
        roi_id=roi_id,
        roi_name=roi_name,
        min_temp=nan,
        max_temp=nan,
        mean_temp=nan,
        deviation=nan,
        unit=TemperatureUnit.CELSIUS,
        area=None,
        center_row=None,
        center_col=None,
        valid=False,
        error=error,
        method=BATCHED_METHOD,
    )


@dataclass(slots=True)
class HalconROIAdapter:
    """
    Thin wrapper around proven HALCON ROI statistics.

    Hides: HObject, HALCON tuples, row/col convention, batched operators.

    Ownership: one instance is owned by one SimpleProcessingPipeline on
    one worker thread. The RegionCache (and every cached HObject) is
    thread-local to that worker and must never cross threads.
    """

    _clip_region_configured: bool = False
    _region_cache: RegionCache = field(default_factory=RegionCache, repr=False)
    _warned_at: dict = field(default_factory=dict, repr=False)

    @property
    def region_cache(self) -> RegionCache:
        """Worker-owned region cache (diagnostics/tests only)."""
        return self._region_cache

    def _warn_throttled(self, key: str, msg: str, *args: Any) -> None:
        now = time.monotonic()
        if now - self._warned_at.get(key, 0.0) >= _WARN_INTERVAL_S:
            self._warned_at[key] = now
            _logger.warning(msg, *args)

    # ------------------------------------------------------------------
    # Compatibility API (signatures frozen: mocks + regression test
    # test_gpu_temperature.py::TestHALCONROIRegression depend on them)
    # ------------------------------------------------------------------

    def generate_regions(self, rois: Sequence[ROIConfig]) -> HalconRegionHandle:
        """
        Create batched HALCON regions from ROI configurations.

        First-seen shape order is preserved (matches historical
        behavior). For the cached production path use process_cached().
        """
        import halcon as ha

        self._configure_clip_region(ha)

        if not rois:
            return ha.gen_empty_obj()

        # Group ROIs by shape for batched generation (first-seen order).
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
            group = self._group_from_configs(
                shape, shape_rois, camera_id="", position_id="",
                context_generation=0,
            )
            region = self._build_group_regions(ha, group)
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
        Compute statistics using batched HALCON operators over the whole
        region tuple: one intensity + one min_max_gray + one area_center.

        ``rois`` (when given) must be aligned with the region-tuple
        order. No per-ROI select_obj/statistics loop on this path.
        """
        import halcon as ha

        if temperature_image is None or temperature_image.size == 0:
            return []
        if getattr(temperature_image, "ndim", 2) != 2:
            _logger.warning("HALCON stats rejected: image must be 2-D")
            return []

        try:
            halcon_temp_image = self._convert_image(ha, temperature_image)
        except Exception:
            _logger.exception("HALCON image conversion failed")
            return self._invalid_for_rois(rois, "halcon image conversion failed")

        try:
            region_count = int(ha.count_obj(regions))
        except Exception:
            _logger.exception("Failed to count HALCON regions")
            return []

        if region_count == 0:
            return []

        try:
            mean_vals, dev_vals = ha.intensity(regions, halcon_temp_image)
            min_vals, max_vals, _range_vals = ha.min_max_gray(
                regions, halcon_temp_image, 0)
            area_vals, row_vals, col_vals = ha.area_center(regions)
        except Exception:
            _logger.exception("Batched HALCON statistics failed")
            return self._invalid_for_rois(rois, "batched halcon statistics failed")

        expected = region_count
        for label, vals in (("intensity", mean_vals), ("min_max_gray", min_vals),
                            ("area_center", area_vals)):
            if len(vals) != expected:
                self._warn_throttled(
                    f"tuple-length-{label}",
                    "HALCON %s returned %d values for %d regions; "
                    "marking affected measurements invalid",
                    label, len(vals), expected)
                return self._invalid_for_rois(
                    rois, f"halcon {label} tuple length mismatch")

        results: list[ROIStatistics] = []
        roi_ids = [r.roi_id for r in rois] if rois is not None else [""] * expected
        roi_names = [r.name for r in rois] if rois is not None else [""] * expected
        # Positional zip only: tuple element [i] belongs to roi_ids[i].
        for i in range(expected):
            results.append(self._map_one(
                roi_ids[i] if i < len(roi_ids) else "",
                roi_names[i] if i < len(roi_names) else "",
                float(mean_vals[i]), float(dev_vals[i]),
                float(min_vals[i]), float(max_vals[i]),
                float(area_vals[i]), float(row_vals[i]), float(col_vals[i]),
            ))
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
    # Cached production path (Phase 12.3)
    # ------------------------------------------------------------------

    def process_cached(
        self,
        rois: Sequence[ROIConfig],
        temperature_image: np.ndarray,
        *,
        camera_id: str,
        position_id: str,
        context_generation: int,
    ) -> list[ROIStatistics]:
        """Batched ROI statistics with worker-owned region caching.

        Steps: validate image -> build shape groups -> evict stale
        contexts -> rebuild dirty groups only -> one HALCON image ->
        batched stats per group -> positional mapping (input ROI order
        restored) -> invalid ROIs mapped explicitly.
        """
        import halcon as ha

        self._configure_clip_region(ha)

        if temperature_image is None or temperature_image.size == 0:
            return []
        if getattr(temperature_image, "ndim", 2) != 2:
            _logger.warning(
                "Camera %s position %s: HALCON stats rejected, image must be 2-D",
                camera_id, position_id)
            return []

        groups, invalid = build_shape_groups(
            rois, camera_id=camera_id, position_id=position_id,
            context_generation=context_generation,
        )

        # Position A regions must never serve Position B: drop entries
        # outside the frame's own context before any lookup.
        self._region_cache.prune_context(
            camera_id=camera_id, position_id=position_id,
            context_generation=int(context_generation))

        try:
            halcon_temp_image = self._convert_image(ha, temperature_image)
        except Exception:
            _logger.exception(
                "Camera %s position %s: HALCON image conversion failed",
                camera_id, position_id)
            return [self._invalid_for_roi(r, "halcon image conversion failed")
                    for r in rois if r.enabled]

        # input order restoration (pipeline historically returns stats
        # in input ROI order; groups are processed in SHAPE_ORDER).
        order = [r.roi_id for r in rois if r.enabled]
        by_id: dict[str, ROIStatistics] = {}

        for group in groups:
            regions = self._region_cache.lookup(group)
            if regions is None:
                try:
                    regions = self._build_group_regions(ha, group)
                except Exception:
                    _logger.exception(
                        "Camera %s position %s: region build failed for %s",
                        camera_id, position_id, group.shape)
                    for rid, rname in zip(group.roi_ids, group.roi_names):
                        by_id[rid] = _invalid_stats(rid, rname, "region build failed")
                    continue
                self._region_cache.store(group, regions)
            try:
                mean_vals, dev_vals = ha.intensity(regions, halcon_temp_image)
                min_vals, max_vals, _range_vals = ha.min_max_gray(
                    regions, halcon_temp_image, 0)
                area_vals, row_vals, col_vals = ha.area_center(regions)
            except Exception:
                _logger.exception(
                    "Camera %s position %s: batched stats failed for %s",
                    camera_id, position_id, group.shape)
                for rid, rname in zip(group.roi_ids, group.roi_names):
                    by_id[rid] = _invalid_stats(rid, rname, "batched statistics failed")
                continue
            if not (len(mean_vals) == len(min_vals) == len(area_vals) == len(group)):
                self._warn_throttled(
                    f"tuple-length-{group.shape}",
                    "Camera %s position %s: HALCON tuple length mismatch "
                    "for %s; marking group invalid",
                    camera_id, position_id, group.shape)
                for rid, rname in zip(group.roi_ids, group.roi_names):
                    by_id[rid] = _invalid_stats(
                        rid, rname, "halcon tuple length mismatch")
                continue
            # Positional mapping: tuple element [i] <-> roi_ids[i].
            for i, (rid, rname) in enumerate(zip(group.roi_ids, group.roi_names)):
                by_id[rid] = self._map_one(
                    rid, rname,
                    float(mean_vals[i]), float(dev_vals[i]),
                    float(min_vals[i]), float(max_vals[i]),
                    float(area_vals[i]), float(row_vals[i]), float(col_vals[i]),
                )

        for bad in invalid:
            by_id[bad.roi_id] = _invalid_stats(bad.roi_id, bad.roi_name, bad.error)

        return [by_id[rid] for rid in order if rid in by_id]

    # ------------------------------------------------------------------
    # Internal helpers
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

    @staticmethod
    def _convert_image(ha: Any, temperature_image: np.ndarray) -> HalconRegionHandle:
        """One HALCON image per frame.

        Ownership: the caller's NumPy array is never mutated. The
        binding deep-copies into the HObject (proven Phase 12.2), so the
        HALCON image stays valid after this call returns regardless of
        what happens to the NumPy memory. Non-contiguous input is copied
        into a contiguous float32 buffer first (no-op when already
        compliant).
        """
        contiguous = np.ascontiguousarray(temperature_image, dtype=np.float32)
        return ha.himage_from_numpy_array(contiguous)

    @staticmethod
    def _map_one(roi_id: str, roi_name: str, mean_val: float, dev_val: float,
                 min_val: float, max_val: float, area_val: float,
                 row_val: float, col_val: float) -> ROIStatistics:
        """Map one aligned tuple element to ROIStatistics.

        Any non-finite temperature marks the measurement invalid
        (NaN temps + valid=False); valid measurements carry area and
        center from area_center.
        """
        if not (math.isfinite(mean_val) and math.isfinite(dev_val)
                and math.isfinite(min_val) and math.isfinite(max_val)):
            return _invalid_stats(
                roi_id, roi_name,
                "non-finite statistic (NaN/inf pixels in region)")
        return ROIStatistics(
            roi_id=roi_id,
            roi_name=roi_name,
            min_temp=min_val,
            max_temp=max_val,
            mean_temp=mean_val,
            deviation=dev_val,
            unit=TemperatureUnit.CELSIUS,
            area=float(area_val),
            center_row=float(row_val),
            center_col=float(col_val),
            valid=True,
            error="",
            method=BATCHED_METHOD,
        )

    @staticmethod
    def _invalid_for_roi(roi: ROIConfig, error: str) -> ROIStatistics:
        return _invalid_stats(str(roi.roi_id), str(roi.name), error)

    def _invalid_for_rois(
        self, rois: Sequence[ROIConfig] | None, error: str,
    ) -> list[ROIStatistics]:
        if not rois:
            return []
        return [self._invalid_for_roi(r, error) for r in rois]

    @staticmethod
    def _group_from_configs(shape: ROIShape, shape_rois: Sequence[ROIConfig],
                            *, camera_id: str, position_id: str,
                            context_generation: int) -> ShapeGroup:
        """Build one ShapeGroup from validated ROIConfigs (compat path)."""
        from thermal_monitor.processing.halcon.grouped import _extract_params

        ids: list[str] = []
        names: list[str] = []
        params: list[Any] = []
        for roi in shape_rois:
            params.append(_extract_params(shape, roi.geometry.parameters))
            ids.append(str(roi.roi_id))
            names.append(str(roi.name))
        return ShapeGroup(
            shape=shape,
            roi_ids=tuple(ids),
            roi_names=tuple(names),
            params=tuple(params),
            fingerprint=tuple((rid, p) for rid, p in zip(ids, params)),
            camera_id=camera_id,
            position_id=position_id,
            context_generation=int(context_generation),
        )

    def _build_group_regions(self, ha: Any, group: ShapeGroup) -> HalconRegionHandle:
        """Generate one HALCON region tuple for a validated shape group."""
        if group.shape == ROIShape.RECTANGLE1:
            rows1 = [p[0] for p in group.params]
            cols1 = [p[1] for p in group.params]
            rows2 = [p[2] for p in group.params]
            cols2 = [p[3] for p in group.params]
            return ha.gen_rectangle1(rows1, cols1, rows2, cols2)
        if group.shape == ROIShape.RECTANGLE2:
            rows = [p[0] for p in group.params]
            cols = [p[1] for p in group.params]
            phis = [p[2] for p in group.params]
            length1s = [p[3] for p in group.params]
            length2s = [p[4] for p in group.params]
            return ha.gen_rectangle2(rows, cols, phis, length1s, length2s)
        if group.shape == ROIShape.CIRCLE:
            rows = [p[0] for p in group.params]
            cols = [p[1] for p in group.params]
            radii = [p[2] for p in group.params]
            return ha.gen_circle(rows, cols, radii)
        if group.shape == ROIShape.ELLIPSE:
            rows = [p[0] for p in group.params]
            cols = [p[1] for p in group.params]
            phis = [p[2] for p in group.params]
            radius1s = [p[3] for p in group.params]
            radius2s = [p[4] for p in group.params]
            return ha.gen_ellipse(rows, cols, phis, radius1s, radius2s)
        if group.shape == ROIShape.POLYGON:
            # Vertex counts differ per ROI: per-polygon generation +
            # concat_obj (verified approach, Phase 12.2 proofs).
            regions = None
            for rows_cols in group.params:
                rows, cols = rows_cols
                region = ha.gen_region_polygon_filled(list(rows), list(cols))
                regions = region if regions is None else ha.concat_obj(regions, region)
            if regions is None:
                return ha.gen_empty_obj()
            return regions
        raise ValueError(f"Unsupported ROI shape: {group.shape}")


def process_rois_with_halcon(
    rois: Sequence[ROIConfig],
    temperature_image: np.ndarray,
    adapter: HalconROIAdapter | None = None,
    *,
    camera_id: str | None = None,
    position_id: str | None = None,
    context_generation: int | None = None,
) -> list[ROIStatistics]:
    """
    Generate regions, extract statistics, release regions.

    When the full processing context (camera/position/generation) is
    supplied and the adapter supports it, the worker-owned cached
    batched path is used. Otherwise (mocks, missing context) the
    legacy generate/extract/release flow runs, which keeps the
    MockHalconAdapter test contract intact.
    """
    if adapter is None:
        adapter = HalconROIAdapter()

    enabled_rois = [roi for roi in rois if roi.enabled]
    if not enabled_rois:
        return []

    process_cached = getattr(adapter, "process_cached", None)
    if (process_cached is not None and camera_id is not None
            and position_id is not None and context_generation is not None):
        return process_cached(
            enabled_rois, temperature_image,
            camera_id=camera_id, position_id=position_id,
            context_generation=context_generation)

    regions = adapter.generate_regions(enabled_rois)
    try:
        stats = adapter.extract_statistics(regions, temperature_image, rois=enabled_rois)
        # Rebuild positionally (input order) without list.index().
        by_id = {s.roi_id: s for s in stats}
        ordered: list[ROIStatistics] = []
        for roi in enabled_rois:
            stat = by_id.get(roi.roi_id)
            if stat is None:
                continue
            ordered.append(ROIStatistics(
                roi_id=roi.roi_id,
                roi_name=roi.name,
                min_temp=stat.min_temp,
                max_temp=stat.max_temp,
                mean_temp=stat.mean_temp,
                deviation=stat.deviation,
                unit=stat.unit,
                area=stat.area,
                center_row=stat.center_row,
                center_col=stat.center_col,
                valid=stat.valid,
                error=stat.error,
                method=stat.method,
            ))
        return ordered
    finally:
        adapter.release_regions(regions)
