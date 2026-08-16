# V2 ROI Engine & Image Statistics — Technical Recovery Report

## Purpose

Analyze the V2 ROI implementation across `reference/TMS_v2/roi_engine/`, `reference/TMS_v2/roi/`, `reference/TMS_v2/processing/`, and `reference/TMS_v2/tests/` to determine exact algorithms, HALCON usage, proven behavior, and V3 requirements.

**Focus:** `halcon_roi_validation.py` (hardware-validated path) and `roi_engine/` (production batched engine).

---

## 1. Supported ROI Shapes

| Shape | Enum (`ROIShape`) | Geometry Class | HALCON Operator |
|-------|-------------------|----------------|-----------------|
| Rectangle (axis-aligned) | `RECTANGLE1` | `Rectangle1ROI(row1, col1, row2, col2)` | `gen_rectangle1` |
| Rotated Rectangle | `RECTANGLE2` | `Rectangle2ROI(row, col, phi, length1, length2)` | `gen_rectangle2` |
| Circle | `CIRCLE` | `CircleROI(row, col, radius)` | `gen_circle` |
| Ellipse | `ELLIPSE` | `EllipseROI(row, col, phi, radius1, radius2)` | `gen_ellipse` |
| Polygon | `POLYGON` | `PolygonROI(points: List[(row, col)])` | `gen_region_polygon_filled` |

**Order:** `ALL_SHAPES = (RECTANGLE1, RECTANGLE2, CIRCLE, ELLIPSE, POLYGON)` — batch processing follows this order.

---

## 2. Coordinate Convention

| Convention | Detail |
|------------|--------|
| **Image coordinates** | `(row, col)` — row = Y (vertical), col = X (horizontal) |
| **HALCON native** | `(row, col)` — matches image array indexing `image[row, col]` |
| **Display mapping** | `x = col, y = row` — applied by consumers (overlay, GUI) |
| **Bounding box** | `(row_min, col_min, row_max, col_max)` inclusive, int64 |
| **Hotspot** | `(hotspot_row, hotspot_col)` — pixel carrying max temperature |

---

## 3. HALCON Region Generation

### 3.1 Production Engine (`roi_engine/region_cache.py`)

**Batch generation per shape** (enabled ROIs only):
```python
# RECTANGLE1, CIRCLE, RECTANGLE2, ELLIPSE: single batched call
ha.gen_rectangle1(row1s[enabled], col1s[enabled], row2s[enabled], col2s[enabled])
ha.gen_circle(rows[enabled], cols[enabled], radii[enabled])
ha.gen_rectangle2(rows[enabled], cols[enabled], phis[enabled], length1s[enabled], length2s[enabled])
ha.gen_ellipse(rows[enabled], cols[enabled], phis[enabled], radius1s[enabled], radius2s[enabled])

# POLYGON: per-polygon + concat_obj chain
per_polygon = [ha.gen_region_polygon_filled(rows[i], cols[i]) for i in enabled]
regions = functools.reduce(ha.concat_obj, per_polygon)
```

**Critical HALCON setting** (must run before region gen):
```python
ha.set_system("clip_region", "false")  # prevents silent empty regions when no image read yet
```

**Verification:** Empirically tested on HALCON 24.11; batch `gen_rectangle1/2/circle/ellipse` ~4 ms per 500 regions.

### 3.2 Validation Tool (`halcon_roi_validation.py`)

**Per-ROI generation** (legacy, slower but proven):
```python
# Load ROIs from SQL → generate HALCON regions once at startup
rows1 = [c[0] for c in coords]; cols1 = [c[1] for c in coords]
rows2 = [c[2] for c in coords]; cols2 = [c[3] for c in coords]
self._roi_regions = ha.gen_rectangle1(rows1, cols1, rows2, cols2)  # Rectangle1 only
```

**ROI shapes:** Validation tool only uses **RECTANGLE1** (axis-aligned rectangles from SQL `y1, x1, y2, x2`).

---

## 4. ROI Validation

### 4.1 Geometry Validation (`roi/geometry.py`)
Each ROI class has `validate()`:
- **Rectangle1:** `row1 < row2`, `col1 < col2`
- **Circle:** `radius > 0`
- **Ellipse:** `radius1 > 0`, `radius2 > 0`
- **Rectangle2:** `length1 > 0`, `length2 > 0`
- **Polygon:** ≥3 vertices, non-self-intersecting (not strictly checked)

### 4.2 Store Build Validation (`roi_engine/store/factory.py:_valid_configs()`)
```python
for cfg in configurations:
    try:
        cfg.geometry.validate()
    except ValueError:
        logger.warning(f"Skipping ROI {cfg.roi_id} with invalid geometry")
        continue
    valid.append(cfg)
```
- **Invalid ROIs skipped** — single bad ROI doesn't invalidate whole position
- **Warning logged** with ROI id, name, shape

---

## 5. ROI Storage Model

### 5.1 Configuration (Mutable, GUI-facing)
```python
# roi/configuration.py
@dataclass
class ROIConfiguration:
    roi_id: str
    name: str
    acquisition_state: AcquisitionState
    geometry: BaseROI          # Rectangle1ROI, CircleROI, etc.
    enabled: bool = True
    visible: bool = True
    style: ROIStyle = ...
    alarm: ROIAlarmSettings = ...
```

### 5.2 Immutable Snapshot (`roi_engine/store/roi_store.py`)
```python
@dataclass(frozen=True, slots=True)
class ROIStore:
    camera_id: str
    position_id: str
    generation: int
    _stores: dict[ROIShape, TypeStoreBase]  # per-shape parallel arrays
    _index: dict[str, tuple[ROIShape, int]]  # roi_id → (shape, index)
```
- **Built once per position** via `build_store()` → atomic reference swap
- **Generation** increments on every change (cache invalidation key)
- **Per-shape TypeStore** (parallel arrays):
  - Config arrays (frozen): `roi_ids`, `names`, `enabled`, `visible`, `alarm_*`, `bboxes`, geometry arrays
  - Runtime arrays (mutable): `RuntimeStatsArrays` — overwritten every frame

### 5.3 Geometry Arrays (per shape)
| Shape | Arrays (float64) |
|-------|-----------------|
| RECTANGLE1 | `row1s`, `col1s`, `row2s`, `col2s` |
| RECTANGLE2 | `rows`, `cols`, `phis`, `length1s`, `length2s` |
| CIRCLE | `rows`, `cols`, `radii` |
| ELLIPSE | `rows`, `cols`, `phis`, `radius1s`, `radius2s` |
| POLYGON | `rows` (list[array]), `cols` (list[array]) — variable vertex count |

---

## 6. ROI Caching Strategy

### 6.1 RegionCache (`roi_engine/region_cache.py`)
- **Key:** `ROIShape` → `HObject` (HALCON region tuple)
- **Rebuild trigger:** Store generation changed
- **Alignment:** Regions ordered by `enabled_indices` (enabled ROIs only)
- **Memory:** Tracks sum of `area_center()` pixel counts

### 6.2 MaskCache (`roi_engine/masks.py`)
- **Purpose:** Boolean masks for **hotspot search** (numpy argmax), not statistics
- **Key:** `(ROIShape, store_index)` → `(mask: bool[h, w] | None, origin: (r0, c0))`
- **Mask = None** for RECTANGLE1 (bbox slice IS the region)
- **Rasterization rules** (empirically verified vs HALCON 24.11):
  - CIRCLE: pixel-center test (error 0.0-0.7%)
  - ELLIPSE: 2×2 supersampled (error ~2.4%) — HALCON axis convention: `radius1` on (-sin φ, cos φ), `radius2` on (cos φ, sin φ)
  - RECTANGLE2: polygon fill of 4 corners (error ~0.2%)
  - POLYGON: 4×4 supersampled even-odd (error 0.3-0.8%)
- **Rebuild trigger:** Store generation OR image shape changed
- **Clamping:** Masks clipped to image bounds

---

## 7. Region Caching

**RegionCache** (HALCON regions):
- Built from immutable store snapshot
- Reused across frames until geometry changes
- `ha.area_center()` called once at build for pixel count tracking
- `clip_region=false` guard ensures regions valid without prior image read

**MaskCache** (numpy boolean masks):
- Built per-enabled-ROI, clamped to image
- Used **only for hotspot pixel search** (numpy `argmax` on masked crop)
- Dilated 8-connectivity at search time to cover boundary pixels

---

## 8. Mask Caching

See Section 6.2. Key points:
- **RECTANGLE1:** No mask (bbox = region)
- **Other shapes:** Boolean mask over clamped bbox
- **Supersampling:** Circle 1×1, Ellipse 2×2, Polygon 4×4, Rect2 polygon fill
- **Dilation:** Applied at search time (`_dilate8`) not at build time

---

## 9. Batched Statistics (HALCON)

### 9.1 Production Engine (`roi_engine/statistics_engine.py`)
```python
# Per shape, batched on all enabled ROIs:
mean_t, dev_t = ha.intensity(regs, himage)           # mean, std dev
min_t, max_t, _ = ha.min_max_gray(regs, himage, 0)   # min, max
area_t, _, _ = ha.area_center(regs)                  # pixel count
```
- **HALCON authoritative** for min/max/mean/std/pixel_count
- **Input:** `HObject` (HALCON image from `ha.himage_from_numpy_array(temp_image)`)
- **Output:** HALCON tuples → NumPy arrays written into `RuntimeStatsArrays` at `enabled_indices`
- **Error handling:** Tuple length mismatch → RuntimeError; any exception → mark type `valid=False`, continue

### 9.2 Validation Tool (`halcon_roi_validation.py`)
```python
# Per-frame, same HALCON operators but on pre-generated regions:
mean_vals, dev_vals = ha.intensity(self._roi_regions, halcon_temp_image)
min_vals, max_vals, range_vals = ha.min_max_gray(self._roi_regions, halcon_temp_image, 0)
```
- **Only RECTANGLE1** regions (from SQL)
- **Statistics emitted per ROI:** `ROIStatistics(name, mean, deviation, minimum, maximum, range_val)`
- **Proven hardware path** — used in 4-camera validation runs

---

## 10. Statistics Calculations

| Statistic | Source | Formula |
|-----------|--------|---------|
| **Minimum** | HALCON `min_max_gray` | Exact |
| **Maximum** | HALCON `min_max_gray` | Exact |
| **Mean** | HALCON `intensity` | Exact |
| **Std Dev** | HALCON `intensity` | Exact |
| **Pixel Count** | HALCON `area_center` | Exact region area |
| **Hotspot (row, col)** | NumPy `nanargmax` on masked crop | First max pixel (plateau→first) |

**Hotspot algorithm** (`StatisticsEngine._hotspot()`):
1. Crop temperature image to ROI bbox (clamped to image)
2. Get mask from MaskCache (None for Rect1)
3. Apply mask + dilate 8-connectivity
4. Filter values > HALCON maximum → NaN (discard boundary artifacts)
5. `np.nanargmax` → local coordinates → add bbox origin
6. Returns `(-1, -1)` if all NaN or empty

**Validation parity:** Tests verify engine hotspot within 16 px of legacy (plateau center vs first-max difference).

---

## 11. Thread-Safety

| Component | Thread-Safety | Notes |
|-----------|---------------|-------|
| `ROIEngine` | **NOT thread-safe** | Single acquisition thread per camera |
| `ROIEnginePool` | Thread-safe for `engine()` getter | Dict + lock-free (CPython GIL) |
| `ROIEngineManager` | **NOT thread-safe** | GUI thread only (serialized by Qt) |
| `RegionCache` / `MaskCache` | Not thread-safe | Owned by single `ROIEngine` |
| `StatisticsEngine` | Not thread-safe | Called from `ROIEngine.process_frame()` |
| `CalibrationProcessor` | **Thread-safe** | Pure staticmethods, no shared state |

**V2 Architecture:** One `ROIEngine` per camera, called from camera's acquisition thread (or Observation Window poll timer). No concurrent access.

---

## 12. Performance at Different ROI Counts

| ROI Count | Shapes Tested | Processing Time | Notes |
|-----------|---------------|-----------------|-------|
| 10 | Mixed | ~1-2 ms | Baseline |
| 75 | 45 rect + 20 circle + 10 poly | ~3-5 ms | `test_roi_engine.py` |
| 500 | Single shape (perf test) | ~4 ms region gen | `region_cache.py` comment |

**Scaling:**
- Region gen: O(enabled ROIs) — batched HALCON calls
- Statistics: O(enabled ROIs) — batched `intensity`/`min_max_gray`
- Hotspot: O(ROI_pixels) per ROI — NumPy crop + argmax
- Mask build: O(ROI_pixels) per ROI — once per geometry change

---

## 13. Position-Dependent ROI Behavior

### 13.1 Position Model
```python
# roi/acquisition_state.py
@dataclass
class AcquisitionState:
    camera_id: str
    pan: float = 0.0
    tilt: float = 0.0
    zoom: float = 0.0
    focus: float = 0.0
    position_id: str = ""  # database camera_positions.id
```

### 13.2 Position Loading
- **Calibration Window:** Saves ROIs per `(camera_id, position_id)` via `JSONROIRepository`
- **Observation Runtime:** `set_position(camera_id, position_id)` → loads new `ROIStore`
- **Validation Tool:** Loads ROIs from SQL `dbo.rois` filtered by `camera_id` AND `position_id`

### 13.3 Engine Behavior
- `ROIEngine.load_position(configurations)` → new store, generation+1, clears region/mask caches
- `ROIEngineManager.mark_dirty(roi_id)` with `config_provider` detects edits/deletes → reloads store
- **Stale ROIs unregistered** from alarm manager on position switch (`ObservationRuntime._load_position()`)

---

## 14. ROI Loading

| Path | Source | Format |
|------|--------|--------|
| **Production (Calibration Window)** | JSON files | `data/roi/{camera_id}/{position_id}.json` |
| **Validation Tool** | SQL Server | `dbo.rois` (y1, x1, y2, x2) + `dbo.camera_positions` |
| **Observation Runtime** | JSON (same as Calibration) | `JSONROIRepository.load_all(AcquisitionState)` |

**JSON schema** (`roi/persistence/serializer.py`):
```json
{
  "roi_id": "uuid",
  "name": "ROI 1",
  "geometry": { "type": "rectangle1", "row1": 10, "col1": 10, "row2": 100, "col2": 100 },
  "enabled": true,
  "visible": true,
  "style": { "color": "#FFFF00", "line_width": 2, "label_visible": true, "label_size": 12 },
  "alarm": { "enabled": true, "condition": "HIGH", "value": 80.0, "hysteresis": 2.0, "delay_ms": 0 }
}
```

---

## 15. Required Configuration

| Config | Source | Purpose |
|--------|--------|---------|
| `ROI_ENGINE_DUAL_VALIDATION` | `configuration/settings.py:29` | Run legacy manager in parallel, log mismatches |
| Calibration LUT | `CalibrationManager` | Temperature conversion before ROI stats |
| Image shape | Frame metadata | MaskCache rebuild trigger |
| Alarm settings | Per-ROI in configuration | Threshold, condition, hysteresis, delay |

---

## 16. Reusable in V3

| Component | Reusability | Notes |
|-----------|-------------|-------|
| **Batched HALCON statistics** | ✅ HIGH | Core algorithm; `intensity` + `min_max_gray` + `area_center` proven |
| **Mask rasterization** | ✅ HIGH | Empirically validated vs HALCON; pure NumPy, no HALCON runtime needed |
| **Hotspot search (masked argmax)** | ✅ HIGH | Dilated mask + NaN filter for plateau |
| **Immutable store + generation** | ✅ HIGH | Atomic snapshot pattern; enables lock-free reads |
| **Per-shape parallel arrays** | ✅ HIGH | Cache-friendly, vectorizable |
| **ROI geometry classes** | ✅ MEDIUM | Good math; decouple from HALCON |
| **JSON persistence** | ✅ MEDIUM | Schema stable; repository pattern |
| **Position-dependent loading** | ✅ MEDIUM | Clean separation |

---

## 17. Tied to V2 Architecture (Redesign)

| Component | Issue | V3 Approach |
|-----------|-------|-------------|
| **HALCON region generation** | Requires HALCON runtime at statistics time | Pre-generate regions/masks offline; ship as data |
| **`ha.himage_from_numpy_array`** | HALCON dependency in hot path | Use NumPy-only statistics (or pre-compiled HALCON procedures) |
| **Single-threaded `ROIEngine`** | No parallel frame processing | Stateless functions + worker pool |
| **GUI-coupled `ROIEngineManager`** | Qt signals, `RuntimeROI` views | Pure data API; GUI adapts |
| **Legacy `RuntimeROIManagerImpl` dual validation** | V2-only compatibility | Remove; replace with golden-file tests |
| **SQL-coupled validation tool** | `halcon_roi_validation.py` reads DB directly | Separate validation harness |

---

## 18. HALCON Batched-Statistics Approach (Key Finding)

**The proven hardware path uses:**
```python
# 1. Convert temperature image to HALCON HObject
halcon_temp_image = ha.himage_from_numpy_array(temp_frame.astype(np.float32))

# 2. Batch statistics on pre-generated regions
mean_vals, dev_vals = ha.intensity(roi_regions, halcon_temp_image)
min_vals, max_vals, range_vals = ha.min_max_gray(roi_regions, halcon_temp_image, 0)

# 3. Per-ROI statistics assembly
for i, name in enumerate(roi_names):
    statistics.append(ROIStatistics(
        name=name,
        mean=float(mean_vals[i]),
        deviation=float(dev_vals[i]),
        minimum=float(min_vals[i]),
        maximum=float(max_vals[i]),
        range_val=float(range_vals[i])
    ))
```

**Why it works:**
- `intensity` + `min_max_gray` are **single HALCON calls** regardless of ROI count
- Regions pre-generated at position load (not per-frame)
- HALCON handles region union/clipping internally
- **Measured ~1-2 ms for 50-100 ROIs** on target hardware

**V3 implication:** This batched approach is the performance anchor. Any V3 replacement must match or beat this latency without HALCON runtime in the hot path.

---

## 19. Exact Class/Function Reference

| Component | File | Key Functions |
|-----------|------|---------------|
| **ROI Shapes** | `roi/geometry.py` | `Rectangle1ROI`, `Rectangle2ROI`, `CircleROI`, `EllipseROI`, `PolygonROI`, `bounding_box()`, `validate()` |
| **Configuration** | `roi/configuration.py` | `ROIConfiguration`, `ROIStyle`, `ROIAlarmSettings` |
| **Acquisition State** | `roi/acquisition_state.py` | `AcquisitionState` |
| **Production Engine** | `roi_engine/engine.py` | `ROIEngine.process_frame()`, `load_position()`, `invalidate()`, `get_runtime_statistics()` |
| **Engine Pool** | `roi_engine/engine.py` | `ROIEnginePool.engine()`, `remove()`, `clear()` |
| **Region Cache** | `roi_engine/region_cache.py` | `RegionCache.rebuild()`, `regions()`, `needs_rebuild()` |
| **Mask Cache** | `roi_engine/masks.py` | `MaskCache.rebuild()`, `mask()`, `needs_rebuild()`, `_circle_mask()`, `_ellipse_mask()`, `_rectangle2_mask()`, `_polygon_mask()` |
| **Statistics Engine** | `roi_engine/statistics_engine.py` | `StatisticsEngine.process()`, `_process_type()`, `_hotspot()`, `_dilate8()` |
| **Store** | `roi_engine/store/roi_store.py` | `ROIStore.store_for()`, `enabled_roi_count()`, `roi_index()` |
| **Store Factory** | `roi_engine/store/factory.py` | `build_store()`, `_geometry_kwargs()`, `_valid_configs()` |
| **Runtime Arrays** | `roi_engine/runtime.py` | `RuntimeStatsArrays`, `TypeFrameStats`, `FrameStats` |
| **Integration Adapter** | `roi_engine/integration.py` | `ROIEngineManager.process_frame()`, `mark_dirty()`, `load()`, `dual_validation` |
| **Validation Tool** | `halcon_roi_validation.py` | `CameraWorker.run()`, `_roi_regions`, `ha.intensity()`, `ha.min_max_gray()` |
| **Observation Runtime** | `observation/runtime.py` | `ObservationRuntime.process_frame()`, `set_position()`, `add_camera()` |
| **Overlay** | `observation/overlay.py` | `draw_roi_overlays()`, `_draw_geometry()` |

---

## 20. V3 ROI Requirements Derived from V2

### Proven Behavior (Must Preserve)
1. **Batched HALCON statistics** — `intensity` + `min_max_gray` + `area_center` per frame, per shape
2. **Immutable store snapshots** — Generation-based cache invalidation; atomic swap
3. **Per-shape parallel arrays** — Config (frozen) + Runtime (mutable) separation
4. **Mask-based hotspot** — Dilated boolean mask + `nanargmax` on temperature crop
5. **Position-dependent ROIs** — Per `(camera, position)` configuration; alarm state cleared on switch
6. **RECTANGLE1 optimization** — No mask needed; bbox = region
7. **Supersampled rasterization** — Circle 1×1, Ellipse 2×2, Polygon 4×4 (empirically validated)
8. **HALCON `clip_region=false`** — Required for region generation without prior image

### Assumptions (Need Verification)
1. **640×480 fixed geometry** — V2 hardcodes `FEED_W=640, FEED_H=480`; real camera may differ
2. **Single temperature range (index 0)** — Multi-range parsing exists but unused
3. **Polygon vertex count variable** — Store uses list-of-arrays; affects mask build
4. **Hotspot = first max pixel** — Not plateau center; differs from legacy by ≤16 px
5. **Statistics validity** — `valid=False` for disabled/error ROIs; consumers must check
6. **Frame ID monotonic** — Per-camera counter; used for alarm delay timing
7. **Temperature units = °C** — Hardcoded throughout; no unit conversion

### V3 Must-Haves
- **Stateless statistics function:** `stats = compute_roi_stats(temp_image: ndarray, store: ROIStore, regions: Regions, masks: Masks) → FrameStats`
- **Pre-computed regions/masks:** Ship as data (msgpack/flatbuffers); no HALCON at runtime
- **Worker pool:** Process N frames in parallel across cameras
- **Offline identical:** Same function works on live `uint16→temp` and saved `temp` arrays
- **Validation harness:** Golden-file comparison (engine vs NumPy reference) replacing dual-validation

(End of ROI engine report)