#!/usr/bin/env python3
"""
Phase 12.3 ROI benchmark: legacy per-ROI loop vs cached batched path.

Synthetic 640x480 float32 temperature data (NOT camera data). Compares:

1. legacy: generate_regions + per-ROI select_obj/intensity/min_max_gray
   (the pre-12.3 algorithm, reimplemented inline for comparison)
2. batched cold: HalconROIAdapter.process_cached, empty cache
3. batched warm: same adapter reused (region cache hit)

Reports mean/median/p95, HALCON call counts, and cache counters.
Never runs in production; manual use only:
    python scripts/benchmark_roi_12_3.py [--counts 1 10 50 100 250]
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

import numpy as np

import thermal_monitor.camera.source  # safe import order (see conftest)
import halcon as ha

from thermal_monitor.core.models import ROIConfig, ROIGeometry, ROIShape
from thermal_monitor.processing.halcon import HalconROIAdapter

ha.set_system("clip_region", "false")

H, W = 480, 640


def make_rois(n: int) -> list[ROIConfig]:
    rois: list[ROIConfig] = []
    for i in range(n):
        kind = i % 5
        y = (i * 37) % (H - 60)
        x = (i * 53) % (W - 60)
        if kind == 0:
            shape, params = ROIShape.RECTANGLE1, {
                "y1": float(y), "x1": float(x),
                "y2": float(y + 40), "x2": float(x + 40)}
        elif kind == 1:
            shape, params = ROIShape.RECTANGLE2, {
                "center_y": float(y + 20), "center_x": float(x + 20),
                "phi": 0.3, "length1": 20.0, "length2": 12.0}
        elif kind == 2:
            shape, params = ROIShape.CIRCLE, {
                "center_y": float(y + 20), "center_x": float(x + 20),
                "radius": 15.0}
        elif kind == 3:
            shape, params = ROIShape.ELLIPSE, {
                "center_y": float(y + 20), "center_x": float(x + 20),
                "phi": 0.0, "radius1": 18.0, "radius2": 10.0}
        else:
            shape, params = ROIShape.POLYGON, {
                "points": [(float(y), float(x)), (float(y), float(x + 40)),
                           (float(y + 40), float(x + 20))]}
        rois.append(ROIConfig(roi_id=f"roi_{i:04d}", name=f"ROI {i}",
                              geometry=ROIGeometry(shape=shape,
                                                   parameters=params)))
    return rois


def legacy_process(adapter: HalconROIAdapter, rois, image) -> None:
    """Pre-12.3 algorithm: rebuild regions, per-ROI stats loop."""
    regions = adapter.generate_regions(rois)
    try:
        himg = ha.himage_from_numpy_array(
            np.ascontiguousarray(image, dtype=np.float32))
        count = int(ha.count_obj(regions))
        for i in range(1, count + 1):
            single = ha.select_obj(regions, i)
            ha.intensity(single, himg)
            ha.min_max_gray(single, himg, 0)
    finally:
        adapter.release_regions(regions)


def _timed(fn, repeats: int) -> dict:
    fn()  # warm-up (excluded)
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    return {
        "mean": statistics.fmean(samples),
        "median": samples[len(samples) // 2],
        "p95": samples[min(len(samples) - 1, int(len(samples) * 0.95))],
        "max": samples[-1],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--counts", type=int, nargs="+",
                    default=[1, 10, 50, 100, 250])
    ap.add_argument("--repeats", type=int, default=20)
    args = ap.parse_args()

    rng = np.random.default_rng(12)
    image = rng.uniform(20.0, 100.0, size=(H, W)).astype(np.float32)

    print(f"image={H}x{W} float32 synthetic | repeats={args.repeats}")
    print(f"{'n':>5} {'path':>12} {'mean_ms':>9} {'median_ms':>9} "
          f"{'p95_ms':>9} {'max_ms':>9} {'halcon_calls':>12}")
    for n in args.counts:
        rois = make_rois(n)
        adapter = HalconROIAdapter()

        legacy = _timed(lambda: legacy_process(adapter, rois, image),
                        args.repeats)
        # legacy calls per frame: 1 convert + n*(select+intensity+minmax)
        # + region gens (batched per shape, counted as <=5).
        print(f"{n:>5} {'legacy':>12} {legacy['mean']:>9.2f} "
              f"{legacy['median']:>9.2f} {legacy['p95']:>9.2f} "
              f"{legacy['max']:>9.2f} {1 + 3 * n:>12}")

        cold_adapter = HalconROIAdapter()
        cold = _timed(lambda: _cold(cold_adapter, rois, image),
                      args.repeats)
        print(f"{n:>5} {'batched-cold':>12} {cold['mean']:>9.2f} "
              f"{cold['median']:>9.2f} {cold['p95']:>9.2f} "
              f"{cold['max']:>9.2f} {'~15':>12}")

        warm_adapter = HalconROIAdapter()
        _warm_prime(warm_adapter, rois, image)
        warm = _timed(
            lambda: warm_adapter.process_cached(
                rois, image, camera_id="bench",
                position_id="p", context_generation=0),
            args.repeats)
        print(f"{n:>5} {'batched-warm':>12} {warm['mean']:>9.2f} "
              f"{warm['median']:>9.2f} {warm['p95']:>9.2f} "
              f"{warm['max']:>9.2f} {'<=16':>12}")
        st = warm_adapter.region_cache.stats
        print(f"      cache: hits={st.hits} misses={st.misses} "
              f"builds={st.builds} rebuilds={st.rebuilds}")
    return 0


def _cold(adapter: HalconROIAdapter, rois, image):
    adapter.region_cache.clear()
    return adapter.process_cached(
        rois, image, camera_id="bench",
        position_id="p", context_generation=0)


def _warm_prime(adapter: HalconROIAdapter, rois, image):
    adapter.process_cached(rois, image, camera_id="bench",
                           position_id="p", context_generation=0)


if __name__ == "__main__":
    sys.exit(main())
