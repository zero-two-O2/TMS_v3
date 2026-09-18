"""roi.measurements -- documented NumPy measurement implementations.

Method notes (also recorded on each result's ``method`` value):
- spot: direct sampling of the calibrated temperature image.
- hottest/coldest: argmin/argmax over the search region (ties -> first).
- area stats: min/max/mean/std over finite pixels inside the mask.
- line profile: Bresenham-style sampling along the segment.
All functions are pure (no I/O, no threads) so HALCON and unit tests
share the same documented semantics. HALCON paths must agree with
these within 1e-6 for identical inputs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from thermal_monitor.roi import geometry as _g


@dataclass(frozen=True, slots=True)
class AreaStats:
    min: float
    max: float
    mean: float
    std: float
    count: int
    hottest_row: float
    hottest_col: float
    coldest_row: float
    coldest_col: float


def _finite_image(image: np.ndarray) -> np.ndarray:
    arr = np.asarray(image, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError("temperature image must be 2-D")
    return arr


def sample_spot(image: np.ndarray, geom: _g.SpotGeometry) -> dict:
    arr = _finite_image(image)
    h, w = arr.shape
    r = min(max(int(round(geom.row)), 0), h - 1)
    c = min(max(int(round(geom.col)), 0), w - 1)
    value = float(arr[r, c])
    return {"method": "numpy_direct_sample", "row": float(r), "col": float(c),
            "value": value, "valid": bool(math.isfinite(value))}


def _region_slice(arr: np.ndarray, row1: float, col1: float, row2: float, col2: float):
    h, w = arr.shape
    r1 = min(max(int(math.floor(row1)), 0), h - 1)
    c1 = min(max(int(math.floor(col1)), 0), w - 1)
    r2 = min(max(int(math.ceil(row2)), 0), h - 1)
    c2 = min(max(int(math.ceil(col2)), 0), w - 1)
    return r1, c1, r2, c2


def hottest_in_region(image: np.ndarray, row1: float, col1: float,
                      row2: float, col2: float) -> dict:
    arr = _finite_image(image)
    r1, c1, r2, c2 = _region_slice(arr, row1, col1, row2, col2)
    window = arr[r1:r2 + 1, c1:c2 + 1]
    finite = np.isfinite(window)
    if not np.any(finite):
        return {"method": "numpy_argmax", "valid": False, "value": float("nan"),
                "row": None, "col": None}
    masked = np.where(finite, window, -np.inf)
    idx = int(np.argmax(masked))
    rr, cc = divmod(idx, masked.shape[1])
    return {"method": "numpy_argmax", "valid": True,
            "value": float(window[rr, cc]), "row": float(r1 + rr), "col": float(c1 + cc)}


def coldest_in_region(image: np.ndarray, row1: float, col1: float,
                      row2: float, col2: float) -> dict:
    arr = _finite_image(image)
    r1, c1, r2, c2 = _region_slice(arr, row1, col1, row2, col2)
    window = arr[r1:r2 + 1, c1:c2 + 1]
    finite = np.isfinite(window)
    if not np.any(finite):
        return {"method": "numpy_argmin", "valid": False, "value": float("nan"),
                "row": None, "col": None}
    masked = np.where(finite, window, np.inf)
    idx = int(np.argmin(masked))
    rr, cc = divmod(idx, masked.shape[1])
    return {"method": "numpy_argmin", "valid": True,
            "value": float(window[rr, cc]), "row": float(r1 + rr), "col": float(c1 + cc)}


def _mask_for(arr: np.ndarray, geom) -> np.ndarray:
    h, w = arr.shape
    rows, cols = np.mgrid[0:h, 0:w].astype(np.float64)
    if isinstance(geom, _g.RectangleGeometry):
        return ((rows >= geom.row1) & (rows <= geom.row2)
                & (cols >= geom.col1) & (cols <= geom.col2))
    if isinstance(geom, _g.CircleGeometry):
        return ((rows - geom.center_row) ** 2 + (cols - geom.center_col) ** 2
                <= geom.radius ** 2)
    if isinstance(geom, _g.EllipseGeometry):
        phi = geom.phi
        dy = rows - geom.center_row
        dx = cols - geom.center_col
        xr = dx * math.cos(phi) + dy * math.sin(phi)
        yr = -dx * math.sin(phi) + dy * math.cos(phi)
        return ((xr / geom.radius2) ** 2 + (yr / geom.radius1) ** 2) <= 1.0
    if isinstance(geom, (_g.PolygonGeometry,)):
        from matplotlib.path import Path as _Path  # lazy; fallback below
        try:
            path = _Path([(c, r) for r, c in geom.points])
            pts = np.column_stack([cols.ravel(), rows.ravel()])
            return path.contains_points(pts).reshape(h, w)
        except ImportError:
            pass
        # scanline fallback without matplotlib
        mask = np.zeros((h, w), dtype=bool)
        poly = [(c, r) for r, c in geom.points]
        for y in range(h):
            inside = False
            j = len(poly) - 1
            for i in range(len(poly)):
                xi, yi = poly[i]
                xj, yj = poly[j]
                if ((yi > y) != (yj > y)) and (y - yi) * (xj - xi) / ((yj - yi) or 1e-12) + xi > 0:
                    pass
                j = i
            # vectorized ray-cast per row
            xs = np.arange(w, dtype=np.float64)
            crossings = np.zeros(w, dtype=bool)
            n = len(poly)
            inside_row = np.zeros(w, dtype=bool)
            for i in range(n):
                x1, y1 = poly[i]
                x2, y2 = poly[(i + 1) % n]
                cond = ((y1 > y) != (y2 > y))
                if cond:
                    xinters = (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1
                    inside_row ^= (xs < xinters)
            mask[y] = inside_row
        return mask
    if isinstance(geom, (_g.HottestSpotGeometry, _g.HotColdSpotsGeometry)):
        mask = np.zeros((h, w), dtype=bool)
        r1, c1, r2, c2 = _region_slice(arr, geom.row1, geom.col1, geom.row2, geom.col2)
        mask[r1:r2 + 1, c1:c2 + 1] = True
        return mask
    raise ValueError(f"no area mask for {type(geom).__name__}")


def area_statistics(image: np.ndarray, geom) -> AreaStats:
    arr = _finite_image(image)
    mask = _mask_for(arr, geom)
    values = arr[mask & np.isfinite(arr)]
    if values.size == 0:
        return AreaStats(min=float("nan"), max=float("nan"), mean=float("nan"),
                         std=float("nan"), count=0, hottest_row=float("nan"),
                         hottest_col=float("nan"), coldest_row=float("nan"),
                         coldest_col=float("nan"))
    flat_mask_idx = np.flatnonzero(mask)
    finite_flat = np.isfinite(arr.ravel()[flat_mask_idx])
    idx_valid = flat_mask_idx[finite_flat]
    hot = idx_valid[int(np.argmax(arr.ravel()[idx_valid]))]
    cold = idx_valid[int(np.argmin(arr.ravel()[idx_valid]))]
    h, w = arr.shape
    return AreaStats(
        min=float(np.min(values)), max=float(np.max(values)),
        mean=float(np.mean(values)), std=float(np.std(values)),
        count=int(values.size),
        hottest_row=float(hot // w), hottest_col=float(hot % w),
        coldest_row=float(cold // w), coldest_col=float(cold % w))


def line_profile(image: np.ndarray, geom: _g.LineGeometry,
                 max_samples: int = 2048) -> dict:
    arr = _finite_image(image)
    length = math.hypot(geom.row2 - geom.row1, geom.col2 - geom.col1)
    n = max(2, min(int(math.ceil(length)) + 1, max_samples))
    rows = np.linspace(geom.row1, geom.row2, n)
    cols = np.linspace(geom.col1, geom.col2, n)
    h, w = arr.shape
    rr = np.clip(np.round(rows).astype(int), 0, h - 1)
    cc = np.clip(np.round(cols).astype(int), 0, w - 1)
    temps = arr[rr, cc]
    finite = temps[np.isfinite(temps)]
    step = length / max(n - 1, 1)
    distances = [float(i * step) for i in range(n)]
    return {
        "method": "numpy_bresenham_sample",
        "count": n,
        "distances_px": distances,
        "temperatures": [float(v) for v in temps],
        "min": float(np.min(finite)) if finite.size else float("nan"),
        "max": float(np.max(finite)) if finite.size else float("nan"),
        "mean": float(np.mean(finite)) if finite.size else float("nan"),
        "valid": bool(finite.size),
    }


def ruler_distance(geom: _g.LineGeometry, *, axis: str = "free") -> dict:
    """Pixel distance; physical distance is unavailable without calibration."""
    if axis == "horizontal":
        pixels = abs(geom.col2 - geom.col1)
    elif axis == "vertical":
        pixels = abs(geom.row2 - geom.row1)
    else:
        pixels = math.hypot(geom.row2 - geom.row1, geom.col2 - geom.col1)
    return {"pixel_distance": float(pixels), "unit": "px",
            "physical_distance": None, "physical_unit": None,
            "calibration": "unavailable"}


def measure_angle(geom: _g.AngleGeometry) -> dict:
    v1 = (geom.end1_row - geom.center_row, geom.end1_col - geom.center_col)
    v2 = (geom.end2_row - geom.center_row, geom.end2_col - geom.center_col)
    n1 = math.hypot(*v1)
    n2 = math.hypot(*v2)
    cos_a = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return {"angle_deg": float(math.degrees(math.acos(cos_a))),
            "segment1_px": float(n1), "segment2_px": float(n2)}


__all__ = [
    "AreaStats", "sample_spot", "hottest_in_region", "coldest_in_region",
    "area_statistics", "line_profile", "ruler_distance", "measure_angle",
]
