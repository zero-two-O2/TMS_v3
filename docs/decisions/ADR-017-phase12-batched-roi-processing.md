# ADR-017: Phase 12.3 — Batched ROI Processing Implementation

Status: Accepted
Date: 2026-09-18
Implements: ADR-016 (design)
Leaves unchanged: ADR-015 (position-bound ownership, Go-To workflow)

## 1. Problem statement

The production ROI path (`SimpleProcessingPipeline` →
`process_rois_with_halcon` → `HalconROIAdapter`) rebuilt HALCON regions
on every frame and ran `select_obj` + `intensity` + `min_max_gray` once
**per ROI** (3 HALCON calls per ROI per frame), remapped results with
O(n²) `list.index`, dropped the HALCON `range` output, and reported
HALCON failures as fake `0.0` readings. V2 avoided this with prepared
regions and tuple-wide operators but was acquisition-coupled and
Rectangle1-only.

## 2. What was built

Same production path, same types, new internals:

```text
process_frame -> context snapshot (existing)
 -> process_rois_with_halcon(..., camera/position/generation)
 -> HalconROIAdapter.process_cached
 -> build_shape_groups (transient, validated, fingerprinted)
 -> RegionCache.prune_context + per-group lookup (rebuild dirty only)
 -> one himage_from_numpy_array per frame
 -> per group: intensity + min_max_gray + area_center (3 calls)
 -> positional zip -> ROIStatistics (input order restored)
 -> pipeline post-guard -> AnalysisResult -> consumer -> alarms
```

New modules: `processing/halcon/grouped.py` (`ShapeGroup`,
`InvalidRoi`, `build_shape_groups`, `SHAPE_ORDER`), `
processing/halcon/region_cache.py` (`RegionCache`, `CacheStats`).
Rewritten: `processing/halcon/roi_adapter.py`. Extended:
`processing/pipeline.py` (context passing, post-guard, stale discard
counter via existing `frames_dropped`, invalid stats skipped in frame
aggregates). Additive: `ROIStatistics` gains `area`, `center_row`,
`center_col`, `valid`, `error`, `method` (all defaulted; every
existing construction is keyword-based, storage never serializes
`ROIStatistics` — verified by audit).

## 3. Why V2 helped and what was not copied

V2's latency came from build-once regions + one conversion + tuple-wide
`intensity`/`min_max_gray` (halcon_roi_validation.py:1186,1657-1680).
V3 adopts exactly that, but stays snapshot-based: regions keyed by
(camera, position, context generation, fingerprint), never shared
across threads, never coupled to acquisition. V2's QThread acquisition
worker, SQL layer, and GUI coupling were not copied.

## 4. Ownership and lifetime

One `HalconROIAdapter` per `SimpleProcessingPipeline` per consumer
thread; `RegionCache` lives on the adapter. Caller's NumPy array is
never mutated; the binding deep-copies (proven 12.2), so the HALCON
image is self-owned. Release = drop the Python reference (explicit
`clear_obj` double-frees: error #4051). `generate_regions` /
`extract_statistics` / `release_regions` signatures frozen (mocks +
`test_roi_adapter_interface_unchanged` depend on them);
`extract_statistics` is now also batched (no `select_obj` loop on any
path; `select_obj` retained only in HALCON itself, unused by us).

## 5. Mapping, invalid data, generations

Tuple element [i] ↔ `roi_ids[i]`, strict positional zip; length
mismatch invalidates the group with a throttled warning (never
misassociated, never crash). Invalid geometry is rejected before any
HALCON call; NaN/inf statistics → NaN temps + `valid=False` (never
0.0). NaN needs no alarm change: comparisons evaluate False → INFO,
identical to the existing missing-ROI outcome (verified against
`processing/alarms.py:152-232`; no false trigger possible).
Pre-existing guard (context snapshot per frame) kept; new post-guard
discards results when a retarget landed mid-processing (empty
`roi_results` + `metadata.stale_discarded`, `frames_dropped` +1, alarms
never see them). Session generation still absent from `FrameDescriptor`
— not fabricated (unchanged from ADR-016).

## 6. Benchmark (synthetic 640×480, this machine, 20 repeats)

`scripts/benchmark_roi_12_3.py` (mixed 5-shape sets):

| n | legacy mean | batched-warm mean | HALCON calls/frame |
|---|---:|---:|---|
| 1 | 0.13 ms | 0.11 ms | 4 → ≤4 |
| 10 | 0.49 ms | 0.47 ms | 31 → ≤16 |
| 50 | 1.91 ms | 0.99 ms | 151 → ≤16 |
| 100 | 3.55 ms | 1.92 ms | 301 → ≤16 |
| 250 | 8.97 ms | 4.03 ms | 751 → ≤16 |

Cache: 1 miss per shape on prime, 100% hits after. These are
single-machine synthetic numbers, not production guarantees; small-n
is noise-dominated.

## 7. Rejected operators

`reduce_domain` (wrong for tuples), `boundary` (no per-frame
consumer), `smallest_rectangle*`, `union1/intersection/difference`,
`paint_region`, `region_to_bin`, `get_region_points` (no requirement),
hotspot-coordinate ops (none proven — NumPy fallback retained).
`count_obj` kept for empty-tuple validation only.

## 8. Tests and results

New: `test_phase12_3_grouped_cache.py` (16), `test_phase12_3_batched.py`
(15). Relevant suites: 226 passed. Adjacent (gpu/synthetic/ptz):
91 passed, 1 pre-existing environment failure
(`test_cpu_fallback_reuses_instance` — fails identically on the clean
tree, verified via stash). `compileall src tools` clean.

## 9. Risks and future work

HObject thread-safety officially unverified → mitigated by
thread-local ownership. Float-coordinate rasterization differs
slightly from NumPy slicing → relative tolerance rel=1e-5 kept.
Non-Rectangle1 batching proven at operator level only. No
simulator/hardware validation (synthetic only). Possible follow-ups:
cached `boundary` outlines if a per-frame consumer appears;
HALCON hotspot coordinates as a separately audited task.
