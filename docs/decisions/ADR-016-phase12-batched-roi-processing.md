# ADR-016: Phase 12 — HALCON Batched ROI Processing Model

Status: Accepted (design; implementation deferred to Phase 12.3)
Date: 2026-09-18
Builds on: ADR-015 (position-bound ROI ownership — unchanged)

## 1. Production path (verified, not assumed)

The only production ROI evaluation path is:

```text
Acquisition worker -> Frame(payload.thermal Mono16)
 -> SimpleProcessingPipeline.process_frame (processing/pipeline.py:246)
 -> raw_to_temperature (temperature.py:70/373; call site pipeline.py:299-308)
 -> CachedROIResolver (core/roi_resolver.py:53; call site pipeline.py:265)
 -> process_rois_with_halcon (pipeline.py:328)
 -> HalconROIAdapter.generate_regions (processing/halcon/roi_adapter.py:44)
 -> HalconROIAdapter.extract_statistics (:101)
 -> AnalysisResult.roi_results (pipeline.py:356)
 -> ProcessingConsumer._process_frame (consumer.py:418) [own thread, consumer.py:158]
 -> AlarmEvaluator.evaluate (processing/alarms.py:116)
 -> observer result_ready -> GUI slot (QueuedConnection, paint-only)
```

`RoiEvaluator` (roi/evaluator.py) and `HalconSnapshotRunner`
(halcon/adapter.py) are **test-only**: zero production callers in
Live/Config/Offline (verified by caller trace). They are the NumPy
reference oracle, not a second engine; Phase 12.3 must not wire them
into production.

## 2. Audit answers (20 questions, file:line)

1. Production evaluator: `process_rois_with_halcon` /
   `HalconROIAdapter` (pipeline.py:328).
2. Live + Config-observer + Offline all use it (consumer.py:418,
   worker.py:97; windows attach via observer result_ready).
3. Production: pipeline, consumer, roi_adapter, temperature,
   roi_resolver, processing/alarms. Test-only: roi/evaluator.py,
   halcon/adapter.py, roi/io.py.
4. Regions generated in `HalconROIAdapter.generate_regions`
   (processing/halcon/roi_adapter.py:44-99).
5. Region generation runs **once per frame** (pipeline.py:328 calls
   it on every process_frame; no cache field exists on the adapter).
6. **One** HALCON image per frame (`himage_from_numpy_array`,
   roi_adapter.py:127).
7. NumPy copies per frame: 1x `.astype(np.float32)` (roi_adapter.py:127)
   plus the HALCON-side deep copy (§4); snapshot path adds up to 3
   (`halcon/adapter.py:53,126,135`) but that path is test-only.
8. Yes: temperature conversion (pipeline.py:299) precedes ROI stats
   (:328); roi_adapter assumes float32 °C (:104).
9. Frame payload is a durable pinned copy (consumer.py:282,341);
   pipeline stores `last_temperature_image` for display reuse (:309,
   consumer.py:432-436). ROI input must still be treated read-only;
   HALCON copies so it cannot mutate the source (§4).
10. `ProcessingConsumer` owns its thread (consumer.py:158-163);
    Offline uses a QThread worker (processing/worker.py:169-174).
11. Yes — regions depend only on (context key + geometry), never on
    pixel data, so caching across frames is safe with the key in §6.
12. Thread-local: HalconROIAdapter instance, cached HObjects, HALCON
    image handle. One engine per pipeline/consumer thread; never share
    HObjects across cameras/threads (HALCON binding affinity
    unverified — assume thread-affine).
13. Alarms consume min/max/mean only (processing/alarms.py:207-267).
    GUI tables consume min/max/mean/deviation/range_temp-property
    (statistics_panel.py:117-126, roi_panel.py:668-672,
    offline_window.py:810-817). Overlay paint consumes no stats.
14. Today: empty ROI set -> `[]`/empty result (roi_adapter.py:297,
    pipeline.py:273); disabled ROIs skipped (:69, :297); degenerate
    geometry raises inside HALCON (proof: HOperatorError #1303/1304)
    and is caught per-region into 0.0 stats (:168-178) — a **defect**:
    failure is indistinguishable from a real 0 °C reading and `range`
    is dropped. Invalid/NaN images: `verify_image_for_halcon`
    (halcon/geometry.py:15) rejects only on the test-only path; the
    production path has no NaN guard. Out-of-bounds: HALCON clips
    (proof §5), V3 pre-check is permissive by design
    (halcon/geometry.py:86-92).
15. Production shapes: Rectangle1/2, Circle, Ellipse, Polygon
    (roi_adapter.py:80-91; ROIShape in core/models).
16. HALCON supports all five (proof tests pass for each generator).
17. No shape *requires* NumPy fallback for stats. Hotspot row/col has
    no proven HALCON coordinate operator -> stays documented NumPy
    (`measurements.hottest_in_region`), parity-tested.
18. V2 lower latency comes from: regions built once per ROI-set change
    (`_generate_halcon_regions`, halcon_roi_validation.py:1186) and
    reused; one image conversion per frame (:1662); one
    `intensity` + one `min_max_gray` over the whole region tuple
    (:1669-1670). V3 instead rebuilds regions every frame and loops
    `select_obj + intensity + min_max_gray` per ROI
    (roi_adapter.py:144-150). V2 is Rectangle1-only and
    acquisition-coupled — copy the batching, not the coupling.
19. `CachedROIResolver` caches **ROI definition tuples only**
    (core/roi_resolver.py:57-63), keyed `(camera_id, position_id)`
    without generation. It caches neither regions nor images.
20. Hot-reload paths: `update_config` (pipeline.py:129-131) replaces
    the whole config without invalidating the resolver cache;
    `set_active_position` bumps generation (pipeline.py:201-217).
    Neither touches HALCON regions today (nothing cached). The
    Phase 12.3 cache must subscribe to both (see §6).

Note: the phase brief lists `src/thermal_monitor/alarms.py` — that
file does not exist; alarm evaluation lives at
`src/thermal_monitor/processing/alarms.py`.

## 3. Proposed pipeline (12.3, no interface change)

```text
Temperature snapshot (immutable, pipeline-owned)
 -> position/context validation (existing _active_position snapshot)
 -> grouped ROI definition view (transient, §5)
 -> RegionCache lookup (worker-owned, §6)
 -> build only missing/dirty shape groups
 -> one HALCON image per snapshot (§4: deep copy, safe)
 -> 3 batched calls per non-empty shape group
    (intensity, min_max_gray, area_center)
 -> index-aligned mapping to ROIStatistics (zip, never .index)
 -> generation re-validation -> AnalysisResult
```

`ROIStatistics` / `AnalysisResult` types unchanged. Optional
`area/center_row/center_col` fields (default None) may be added after
the consumer check in §8 — alarms and GUI ignore unknown fields, so
this is backward compatible.

## 4. HALCON image lifetime (proven, tests/test_phase12_2_halcon_proofs.py)

`himage_from_numpy_array` **deep-copies** (binding docstring:
"Has to perform a deep copy"). Proven: mutating the NumPy array
after conversion leaves HALCON results unchanged
(`test_image_conversion_deep_copies`). Consequences:

- Ownership rule: caller retains the NumPy snapshot; HALCON owns its
  copy. No lifetime coupling, no use-after-free class.
- Cost: one full-frame copy per frame is mandatory, not optimizable
  away. The optimization budget is therefore: 1 image conversion +
  0 region rebuilds + 3 calls per shape group on cache hit.
- Non-contiguous input accepted (binding handles it; proven).
- float32 preserved. NaN propagates through intensity/min_max_gray
  (proven) -> map NaN results to `valid=False`-style handling, never
  0.0 (fixes the defect in §2.14).
- No context manager needed; `del`/reassignment releases via
  reference counting (existing `release_regions` rationale stands).

## 5. Batched statistics (proven)

On a region tuple, `intensity`, `min_max_gray`, and `area_center`
each return **lists aligned with tuple order** (proven for n=2..3,
mixed-shape concat, polygon concat). `select_obj` loop returns
identical values element-wise (`test_batched_matches_per_roi_loop`),
so batching changes call count, not semantics:

- Before: 3 HALCON calls **per ROI** per frame (+ region rebuild).
- After: 3 calls **per non-empty shape group** per frame (max 15
  calls at 5 shapes, typically 3), regions reused.
- Measured on this machine (480x640, n=100 Rectangle1): per-ROI loop
  2.52 ms vs batched 0.80 ms (~3.2x on stats; small-n runs are
  noise-dominated). Region-regen elimination is additional and
  unmeasured — no claim beyond the n=100 stats number above.
- Empty object: all three ops return empty tuples, no exception
  (proven) -> empty shape groups are skipped without branching cost.
- `count_obj`/`select_obj` remain for diagnostics only, not the
  frame loop. `reduce_domain` stays **unused** (V2 finding stands:
  wrong tool for region tuples).

## 6. Grouped-array view (transient, no schema change)

Builder input: `Sequence[ROIConfig]` (production) — `RoiDefinition`
compatible via the same field extraction. Output per shape: frozen
dataclass holding parallel tuples + `roi_ids: tuple[str, ...]` in
HALCON call order:

- Rectangle1: rows1/cols1/rows2/cols2 (int, rounded once at build).
- Rectangle2: rows/cols/phis/length1s/length2s (float).
- Circle: rows/cols/radii. Ellipse: rows/cols/phis/radius1s/radius2s.
- Polygon: per-ROI `(rows, cols)` vertex lists + offsets; the only
  group requiring per-ROI generation + `concat_obj` (proven).

Rules: stable roi_id carried, never name-as-identity; result mapping
by positional `zip Longest-strict`; no `list.index`; immutable after
construction (frozen dataclass, tuples); worker-thread-local use.
Pre-generation validation (finite numbers, row2>row1, col2>col1,
radius/length > 0, polygon >= 3 vertices) — HALCON raises #1303/1304
otherwise (proven).

## 7. RegionCache (worker-owned)

Owner: the `SimpleProcessingPipeline` instance (one per consumer
thread). Never shared across threads/cameras; GUI- and DB-independent.

Key: `(camera_id, position_id, context_generation, roi_generation,
shape)` where `roi_generation` is a new monotonically bumped counter
on the pipeline, incremented by `update_config`,
`CachedROIResolver.invalidate`, and `set_active_position`. Camera_id
alone is never the key (same camera, different positions).

Behavior: first frame per key builds required shape groups (record
build ms + region count); cache hit reuses without regeneration;
geometry edit bumps `roi_generation` and rebuilds lazily on next
frame (per-shape rebuild when the fingerprint says one group
changed; full rebuild acceptable v1); position/session change makes
the key mismatch so old regions are unreachable, never reused;
release by dropping the HObject reference (refcount frees; no
`clear_obj` — error #4051 rationale already documented in code).

## 8. Operators: use / reject

Use: `himage_from_numpy_array` (1/frame), `gen_rectangle1/2`,
`gen_circle`, `gen_ellipse`, `gen_region_polygon_filled`,
`concat_obj`, `gen_empty_obj`, `intensity`, `min_max_gray`,
`area_center` (adds area + center_row/center_col; static-vs-dynamic
in §9), `count_obj` (diagnostics/cache stats only).
Investigated, rejected/deferred: `reduce_domain` (semantically wrong
for tuples), `boundary` (outline cache deferred — overlays paint from
editor geometry today, no stats consumer needs it per-frame),
`smallest_rectangle1/2`, `union1`, `intersection`, `difference`,
`paint_region`, `region_to_bin`, `get_region_points` (no product
requirement this phase), hotspot-coordinate ops (none proven —
documented NumPy fallback retained).

## 9. Area / center / boundary (§11)

- `area` (from area_center): dynamic (depends on clipping), small,
  alarm/GUI-independent today -> optional field, diagnostics + future
  rules. Static-vs-dynamic: region pixel count after clipping, so
  per-frame.
- `center_row/center_col`: same treatment as area.
- `boundary`/outline: static per geometry; NOT sent through frame
  results (size). Deferred: no current consumer needs it (overlays
  use editor geometry). Cache design reserves a `outlines` slot.
- Existing fields (min/max/mean/deviation, range as property) frozen.

## 10. NumPy policy (§12)

Retained: temperature LUT conversion, finite checks, hotspot/coldest
coordinates fallback, reference oracle (`roi/measurements.py`,
tolerance: 1e-6 abs for small constants, rel 1e-5 for accumulated
gradient means — established by proof failures/retries), test-only
evaluator. Forbidden: per-frame full-image boolean mask per ROI,
mgrid allocations, second float32 conversion, per-ROI Python stat
loops where §5 batching applies.

## 11. Snapshot contract (§13) and stale protection (§14)

Snapshot (constructed in `process_frame`, owned by pipeline):
read-only temperature view + (camera_id, position_id,
context_generation from the atomic `_active_position` snapshot,
session id as available, frame sequence, timestamp, roi_generation).
No image duplication (view, not copy; HALCON makes its own copy).
Stale guards at both ends (existing pre-check kept; add post-stats
re-validation of `(position_id, context_generation,
roi_generation)` before publishing — closes the race where a
retarget lands mid-processing). Position workflow
(reached -> load -> atomic publish) untouched per ADR-015.

## 12. Benchmark plan (§15) and 12.3 order

Benchmark (new test, 640x480, shapes mixed + single-shape): 1/10/50/
100 ROIs; stages timed separately (prep, convert, region build,
cache hit/miss, stats, mapping, total); counters (image creations,
region builds, HALCON calls); scenarios (warm-up, hit, invalidate,
position change, empty set, OOB, NaN image). No targets claimed
until measured; the only committed numbers are the §5 n=100 stats
timing above.

12.3 order: (1) grouped-view builder + validation; (2) RegionCache +
pipeline wiring (key, invalidate hooks); (3) batched stats +
index mapping + NaN/invalid handling fix; (4) optional area/center
fields; (5) benchmark + parity/regression tests; (6) technical-guide
update. No acquisition/GUI/PTZ/SQL/alarm changes.

## 13. Risks and unknowns

- HObject cross-thread safety officially unverified -> mitigated by
  thread-local ownership (one engine per pipeline).
- `himage_from_numpy_array` failure modes for exotic dtypes
  untested (production always feeds float32; adapter validates).
- Float-coordinate rasterization differs slightly from NumPy window
  slicing (measured) -> parity tolerance must stay relative.
- V2 is Rectangle1-only: Rectangle2/Circle/Ellipse/Polygon batching
  is proven here at the operator level, not against V2 behavior.
- No real-camera validation in this phase (synthetic only).
