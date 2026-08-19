# V3 Stage 6 Architecture Audit Report

**Date:** 2026-08-19
**Branch:** feature/v3-foundation
**Test Status:** 369 passed, 15 skipped (3 consecutive runs)

---

## Executive Summary

The V3 architecture is fundamentally sound with clean layer separation, but several **CRITICAL** and **HIGH** violations were found that must be fixed before proceeding. The most severe is a **processing → storage** dependency cycle violation. No flaky tests detected. No resource leaks found in tests.

---

## 1. Test Stability ✅

| Metric | Result |
|--------|--------|
| Run 1 | 369 passed, 15 skipped (19.80s) |
| Run 2 | 369 passed, 15 skipped (19.10s) |
| Run 3 | 369 passed, 15 skipped (18.63s) |
| Flaky tests | **None detected** |
| Timing-dependent tests | **None** (no `time.sleep` in tests except legitimate backoff tests) |
| Resource leaks | **None** (SHM tests validate pin/release, close idempotence) |
| Compile check | **Pass** (`python -m compileall src` - no syntax errors) |

**Verdict:** Test suite is stable.

---

## 2. UI Thread Audit ⚠️ HIGH

### Violations Found

| File | Line | Operation | Severity |
|------|------|-----------|----------|
| `ui/modes/offline.py` | 542-566 | `_process_current_frame()` runs calibration + ROI + alarm evaluation synchronously on UI thread via QTimer tick | **HIGH** |
| `ui/modes/offline.py` | 687-714 | `_update_display_image()` normalizes thermal data to 8-bit on paint path | **MEDIUM** |
| `ui/modes/offline.py` | 748-784 | `_draw_roi_overlays()` computes ROI geometry scaling on paint | **LOW** |
| `ui/modes/configuration.py` | 609-656 | `_load_rois()` calls `config_service.get_analysis_config()` (in-memory, OK) | **INFORMATIONAL** |

### Root Cause

`OfflineModeWidget` uses a `QTimer` for playback that directly calls `_process_current_frame()` which executes the full `SimpleProcessingPipeline.process_frame()` + `AlarmEvaluator.evaluate()` on the UI thread. At ~74ms/frame (documented baseline), this will block the UI at 9 FPS.

### Required Fix

Move offline processing to a `QThread` worker. The `OfflineFrameSource` already implements `FrameSource` protocol — create a `ProcessingWorker` that consumes frames and emits `AnalysisResult`/`AlarmEvaluationResult` via signals.

---

## 3. Mode Audit ✅

### Transition Matrix Verified

| From \ To | CONFIGURATION | OBSERVER | OFFLINE |
|-----------|---------------|----------|---------|
| CONFIGURATION | ❌ (no-op) | ✅ | ✅ |
| OBSERVER | ✅ | ❌ (no-op) | ✅ |
| OFFLINE | ✅ | ✅ | ❌ (no-op) |

**Implementation:** `core.modes.ModeManager` enforces this exactly. `services.mode.ModeService` wraps it. UI widgets (`MainWindow`, mode widgets) only call `transition_to_*` methods — no widget decides mode independently.

**Observer Placeholder:** `ui/modes/observer.py` clearly displays "Not Available" with blocker explanation. No live acquisition pretended.

**Verdict:** Mode management is correct.

---

## 4. Configuration Boundary Audit ❌ CRITICAL

### Violation: Processing depends on Storage

```
processing/roi_resolver.py:13-14  →  imports storage.database.Database, storage.repositories.roi
processing/pipeline.py:28         →  imports storage.database.Database
processing/pipeline.py:169,177    →  SimpleProcessingPipeline.__init__ requires Database
```

**Architecture Rule Violated:** `processing` must not depend on `storage` (SQL). The ROI resolution should come from `AnalysisConfig` (already in memory via `ConfigurationService`), not from SQL at processing time.

**Impact:** This couples the processing pipeline to SQL Server, prevents offline/replay from working without a database, and violates the "processing consumes frame data and configuration" boundary.

### Required Fix

1. Remove `Database` parameter from `SimpleProcessingPipeline.__init__`
2. Remove `ROIResolver` database dependency — use `resolve_from_config()` only
3. `ConfigurationService` already holds `AnalysisConfig` in memory; pass that to pipeline
4. `ROIResolver` should be a pure domain function: `resolve_rois(config: AnalysisConfig, position_id: str) -> Sequence[ROIConfig]`

---

## 5. Historical Data Immutability ✅

### Verification

| Artifact | Offline Can Modify? | Evidence |
|----------|---------------------|----------|
| Recording manifest | ❌ No | `RecordingReader` read-only; `OfflineFrameSource` read-only |
| Recording config snapshot | ❌ No | JSON files in `config/` never rewritten by reader |
| Recording ROI snapshot | ❌ No | Same |
| Recording calibration snapshot | ❌ No | Same |
| Recorded alarm events | ❌ No | `events/alarms.json` never modified by offline |

**Offline reprocessing** produces `AnalysisResult` (current derived results) only. `OfflineModeWidget` displays both "Recorded Alarms" and "Current Offline Analysis Alarms" separately with legend.

**Verdict:** Historical data is immutable.

---

## 6. ROI Audit ⚠️ HIGH

### V2 Proven Path Verification

| Step | V2 (`halcon_roi_validation.py`) | V3 (`processing/halcon/roi_adapter.py`) | Match |
|------|--------------------------------|----------------------------------------|-------|
| Region generation | `ha.gen_rectangle1(rows1, cols1, rows2, cols2)` | `_gen_rectangle1_batch()` same call | ✅ |
| Statistics | `ha.intensity(region, image)` + `ha.min_max_gray(region, image, 0)` | `extract_statistics()` same calls | ✅ |
| Batched per shape | Yes (groups by shape) | Yes (`regions_by_shape` grouping) | ✅ |
| Coordinate convention | HALCON row/col (y/x) | Explicitly documented row/col | ✅ |

**V2-Proven:** Rectangle1 only.

**HALCON-Ready (not V2-proven):** Rectangle2, Circle, Ellipse, Polygon — correctly documented as such in `roi_adapter.py` header.

### UI Coordinate Conversion

| Location | Convention | Verified |
|----------|------------|----------|
| `ROIGeometry` (domain) | row/col (y, x) | ✅ |
| `ROIEditorWidget` (UI) | "row/col coordinates" label, Y1/X1/Y2/X2 fields | ✅ |
| `OfflineImageWidget._draw_roi_overlays()` | Uses `params["y1"]` as row, `params["x1"]` as col | ✅ |

**No x/y ↔ row/col inversion found.**

### Issue

`ROIConfigurationTab._create_default_geometry()` uses hardcoded defaults (100,100)-(200,200). Should use image center based on actual frame dimensions when available.

---

## 7. Calibration Audit ✅

### V2 Comparison Tests (All Pass)

| Test | Status |
|------|--------|
| `test_v2_v3_parser_output` | ✅ |
| `test_v2_v3_lut_generation` | ✅ (max diff < 1e-5) |
| `test_v2_v3_raw_to_temperature` | ✅ (max diff < 1e-5) |
| `test_v2_v3_temperature_to_display` | ✅ (exact match) |
| `test_v2_v3_raw_to_display` | ✅ (exact match) |
| `test_v2_v3_statistics` | ✅ (diff < 1e-4) |
| `test_v2_v3_roi_statistics` | ✅ (diff < 1e-4) |
| `test_v2_v3_segment_solver` | ✅ (diff < 1e-6) |
| `test_v2_v3_raw_value_to_temperature` | ✅ (diff < 1e-5) |
| `test_v2_v3_deterministic` | ✅ |

**Real calibration blob used:** `reference/TMS_v2/assets/calibration/calibration_blob.txt` (1530 bytes)

**Algorithm unchanged:** Inverse quadratic solver, 65536-entry LUT, NaN interpolation — all match V2 exactly.

---

## 8. Recording Audit ✅

### Compatibility Verified

| Component | CRC | Index | Chunk Rollover | Incomplete | Corrupted | IR/VL Separation | Sequence | Timestamps | Metadata | Camera Identity |
|-----------|-----|-------|----------------|------------|-----------|------------------|----------|------------|----------|-----------------|
| RecordingWriter | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (separate records) | ✅ | ✅ | ✅ | ✅ |
| RecordingReader | ✅ | ✅ | ✅ | ✅ (reads up to truncation) | ✅ (CRC fail = exception) | ✅ (stream_type per record) | ✅ | ✅ | ✅ | ✅ |
| OfflineFrameSource | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (StreamFilter) | ✅ | ✅ | ✅ | ✅ |

### Manifest Stream Reporting

`RecordingWriter._manifest_document()` (line 275-291) uses `self._written_streams` — **actual physical records**, not declared intent. Verified: IR=1, VL=2 stream types tracked per camera.

---

## 9. Shared Memory Audit ✅

### Architecture Verified

```
Acquisition → FramePublisher → SharedMemoryRingBuffer → Consumers (Processing/Observer/Recorder)
```

- `core/shm.py`: **No GUI imports** (only `threading`, `numpy`, `multiprocessing.shared_memory`, `struct`, `logging`, `os`, `time`)
- Producer never blocks on consumers (non-blocking `reserve()`)
- Consumers have independent positions, pinning for tear-free reads
- `InProcessLatestPublisher` is development stand-in only (documented)

### SHM Handle Management

- `SharedMemoryRingBuffer.close()` calls `shm.close()` and `shm.unlink()` (Windows no-op)
- `Consumer.close()` releases pinned slot
- `__exit__` context managers on `PinnedView`
- Tests validate: `test_consumer_close_releases_pin`, `test_ring_buffer_close_idempotent`, `test_frame_view_valid_after_close`

---

## 10. File Count / Project Structure Audit

### Current Structure (66 Python files in src/)

| Domain | Files | Assessment |
|--------|-------|------------|
| core | 6 | ✅ Cohesive |
| camera | 4 | ✅ Cohesive |
| calibration | 4 | ✅ Cohesive |
| processing | 7 | ⚠️ `roi_resolver.py` should move to `core` or `services` |
| storage | 12 | ✅ Cohesive (recording subpackage justified) |
| offline | 3 | ✅ Cohesive |
| services | 7 | ⚠️ `recording.py` imports `storage.recording_legacy` (dead code?) |
| ui | 5 + 4 modes | ✅ Cohesive |

### Recommendations

| File | Recommendation | Reason |
|------|----------------|--------|
| `processing/roi_resolver.py` | **MOVE** to `core/roi_resolver.py` | Pure domain logic, no HALCON/SQL; removes processing→storage violation |
| `processing/halcon/__init__.py` | **KEEP** | Thin re-export, OK |
| `services/recording.py` | **DELETE** `storage.recording_legacy` import | Dead compatibility module (check if used) |
| `storage/recording_legacy/` | **DELETE** if unused | V2 compatibility only |
| `core/models/__init__.py` re-exports | **KEEP** | Convenience, no duplication |
| `calibration/parser.py` + `processor.py` | **MERGE** into `calibration/core.py` | Single responsibility (calibration engine); reduces file count |
| `camera/model.py` + `driver.py` | **KEEP SEPARATE** | Different responsibilities (config vs HALCON) |

### Summary

| Action | Files |
|--------|-------|
| **KEEP** | 58 files (core architecture) |
| **MERGE** | 2 (calibration parser + processor) |
| **MOVE** | 1 (roi_resolver → core) |
| **DELETE** | 2-3 (recording_legacy, possibly services.recording if redundant) |
| **FUTURE** | GPU processing, Observer acquisition, PTZ hardware, SQL migration runner |

---

## 11. Dependency Audit ❌ CRITICAL

### Import Graph Violations

| Violation | Location | Severity |
|-----------|----------|----------|
| `processing` → `storage.database` | `pipeline.py:28`, `roi_resolver.py:14` | **CRITICAL** |
| `processing.pipeline` → `storage.repositories.roi` | via `ROIResolver` | **CRITICAL** |
| `services.recording` → `storage.recording_legacy` | `services/recording.py` | **HIGH** (dead code) |
| `offline.source` → `processing.pipeline` | `offline/source.py:40` | **MEDIUM** (acceptable: offline reuses processing) |

### Layer Compliance

| Layer | Allowed Dependencies | Actual |
|-------|---------------------|--------|
| `core` | (none) | ✅ |
| `camera` | `core` | ✅ |
| `calibration` | `core` | ✅ |
| `processing` | `core`, `calibration` | ❌ (`storage`) |
| `storage` | `core` | ✅ |
| `offline` | `core`, `storage`, `processing` | ✅ |
| `services` | `core`, `camera`, `processing`, `storage`, `offline`, `calibration` | ✅ |
| `ui` | `services`, `core`, `offline`, `storage` | ✅ |

### HALCON Containment ✅

HALCON imports only in:
- `camera/driver.py` (lazy import in methods)
- `processing/halcon/roi_adapter.py` (lazy import in methods)

**No HALCON leaks** into `core`, `storage`, `offline`, `ui`, `services`.

### SQL Containment ✅

`pyodbc` only in `storage/database.py` and `storage/repositories/*.py`.

---

## 12. Virtual Environment Audit ✅

### .gitignore Coverage

| Pattern | Ignored? |
|---------|----------|
| `.venv/` | ✅ |
| `__pycache__/` | ✅ |
| `*.pyc` | ✅ |
| `.env` | ✅ (`.env.example` kept) |
| `recordings/` | ✅ |
| `temporary/` | ✅ |
| `generated reports` | ✅ (`reports/generated/`) |
| SQL Server files (`*.mdf`, `*.ldf`, `*.bak`) | ✅ |
| `reference/` | ✅ (V2 reference, read-only) |

### Fresh Install Test

```bash
python -m pip install -e .
```
- **No pyproject.toml dependencies declared** — only `setuptools` and `pytest` config
- **Missing declarations:** `numpy`, `pyodbc` (optional), `PyQt6` (ui extra), `halcon` (external runtime, not PyPI)

**Required Fix:** Add proper dependency declarations to `pyproject.toml`:

```toml
[project]
dependencies = ["numpy>=1.24"]

[project.optional-dependencies]
ui = ["PyQt6>=6.5"]
database = ["pyodbc>=5.0"]
dev = ["pytest>=7.0", "pytest-cov"]

# HALCON is EXTERNAL runtime dependency — document in README
```

---

## 13. Python Dependency Audit ❌ HIGH

**Current `pyproject.toml` declares ZERO runtime dependencies.** All imports rely on packages pre-installed in the developer's `.venv`.

### Required Dependencies

| Package | Used By | Type |
|---------|---------|------|
| `numpy` | calibration, processing, recording, offline, shm | **core** |
| `pyodbc` | storage.database, repositories | **optional (database)** |
| `PyQt6` | ui.* | **optional (ui)** |
| `halcon` (MVTec) | camera.driver, processing.halcon.roi_adapter | **external runtime** |

---

## 14. SQL Server Audit ✅

### Verification

| Check | Result |
|-------|--------|
| Only SQL Server backend | ✅ (`storage/database.py` only) |
| No SQLite fallback | ✅ |
| No PostgreSQL fallback | ✅ |
| No JSON/local DB fallback | ✅ |
| `NullDatabase` only for tests | ✅ (`storage/null_database.py`, used in `test_synthetic_pipeline.py`, `offline.py`) |
| Production config cannot silently fall back | ✅ `Database.connect()` raises if `pyodbc` missing or connection fails |

**Note:** V2 had fallback server logic — V3 correctly removes this. Single configured server only.

---

## 15. Offline Audit ✅

### Offline Mode Works Without

| Dependency | Required? | Verified |
|------------|-----------|----------|
| Camera hardware | ❌ | ✅ `OfflineFrameSource` reads recording files |
| GigE | ❌ | ✅ |
| HALCON acquisition | ❌ | ✅ (uses `RecordingReader`, not framegrabber) |
| Production acquisition thread | ❌ | ✅ |

### Offline Mode Uses

| Dependency | Required? | Verified |
|------------|-----------|----------|
| HALCON processing | ✅ | `HalconROIAdapter` used for ROI stats (correct — ROI analysis uses HALCON) |
| Processing pipeline | ✅ | `SimpleProcessingPipeline` |
| Calibration | ✅ | `CalibrationProcessor` / `CPUTemperatureConverter` |

### Capabilities Verified

| Operation | Works Offline? |
|-----------|----------------|
| Open recording | ✅ |
| Select camera | ✅ |
| Select stream (IR/VL) | ✅ |
| Seek (timestamp/sequence/index) | ✅ |
| Play/Pause | ✅ |
| Process (calibration + ROI + alarms) | ✅ |
| Display | ✅ |
| Alarm evaluation | ✅ |

---

## 16. Observer Placeholder ✅

**Verified:** `ui/modes/observer.py` is a pure placeholder:
- No live data acquisition
- Displays "Not Available" with blocker explanation
- Lists known blockers (IR/VL alternating, packet loss, HALCON engineer investigation)
- No fake/mock data
- Mode transition allowed (CONFIGURATION ↔ OBSERVER, OFFLINE ↔ OBSERVER) but no behavior implemented

---

## 17. Performance Audit (Baseline Documented) 📊 INFORMATIONAL

| Operation | Measured Baseline | Notes |
|-----------|-------------------|-------|
| Calibration (raw → temperature) | ~43.5 ms/frame | 640x480, LUT lookup |
| ROI statistics (Rectangle1, 10 ROIs) | ~5-8 ms/frame | HALCON `intensity` + `min_max_gray` |
| Alarm evaluation | ~5 ms/frame | Pure Python |
| **Complete chain** | **~74 ms/frame** | At 9 FPS = 111ms budget, leaves ~37ms headroom |

**No optimization attempted** — audit only.

---

## 18. Documentation Created

This report serves as `docs/architecture/v3-stage6-audit.md`.

---

## 19. Final Test Verification

After fixes for CRITICAL/HIGH items:
- Run `python -m pytest` (must pass 3×)
- Run `python -m compileall src` (no syntax errors)

---

## 20. Final Report Summary

### Tests Before/After

| Metric | Before Fixes | After Fixes (Target) |
|--------|--------------|----------------------|
| Passed | 369 | 369+ |
| Skipped | 15 | 15 |
| Flaky | 0 | 0 |

### Findings by Severity

| Severity | Count | Items |
|----------|-------|-------|
| **CRITICAL** | 2 | 1. Processing → Storage dependency (roi_resolver, pipeline)<br>2. Zero declared dependencies in pyproject.toml |
| **HIGH** | 3 | 3. UI thread blocking in OfflineModeWidget<br>4. Missing runtime dependencies<br>5. Dead code: storage.recording_legacy |
| **MEDIUM** | 2 | 6. Processing imports storage.repositories via roi_resolver<br>7. Offline imports processing.pipeline (acceptable but document) |
| **LOW** | 1 | 8. ROI default geometry hardcoded |
| **INFORMATIONAL** | 5 | 9. Calibration parser+processor merge candidate<br>10. File consolidation opportunities<br>11. ROIEditorWidget defaults<br>12. Performance baseline documented<br>13. Observer placeholder confirmed |

### Files Modified (Required Fixes)

| File | Change |
|------|--------|
| `processing/roi_resolver.py` | **MOVE** to `core/roi_resolver.py`; remove `Database`/`ROIRepository` deps; make pure function `resolve_rois(config, position_id)` |
| `processing/pipeline.py` | Remove `Database` param from `SimpleProcessingPipeline`; use `config.get_rois_for_position()` directly |
| `services/offline.py` | Update `create_session` to pass `AnalysisConfig` to pipeline without `Database` |
| `ui/modes/offline.py` | Move `_process_current_frame` to `QThread` worker; emit results via signal |
| `pyproject.toml` | Add `dependencies`, `optional-dependencies` |
| `storage/recording_legacy/` | **DELETE** if unused (verify `services.recording` import) |
| `calibration/parser.py` + `calibration/processor.py` | **MERGE** into `calibration/core.py` (optional, reduces file count) |

### Remaining Blockers (Post-Fix)

1. **GPU processing** — Not started (deferred per scope)
2. **Observer live acquisition** — Blocked on HALCON engineer investigation
3. **PTZ hardware control** — Not implemented
4. **Production alarm hardware integration** — Not implemented
5. **SQL migration runner** — Not configured (ADR mentions but no runner)
6. **Playback enhancements** — Deferred

---

## Sign-Off

**Audit Complete.** Fix CRITICAL/HIGH items before continuing development.

---

## 21. Stage 6B Fix Status Summary

### CRITICAL Issues

| # | Issue | Status | Resolution |
|---|-------|--------|------------|
| 1 | Processing → Storage dependency (roi_resolver, pipeline) | **FIXED** | Moved `roi_resolver.py` to `core/roi_resolver.py` as pure domain function `resolve_rois(config, camera_id, position_id)`. Removed `Database` parameter from `SimpleProcessingPipeline.__init__`. Pipeline now uses `CachedROIResolver` with `AnalysisConfig` only. |
| 2 | Zero declared dependencies in pyproject.toml | **FIXED** | Added `dependencies = ["numpy>=1.24"]`, `optional-dependencies.ui = ["PyQt6>=6.5"]`, `optional-dependencies.database = ["pyodbc>=5.0"]`, `optional-dependencies.dev = ["pytest>=7.0", "pytest-cov"]`. Verified clean install works. |

### HIGH Issues

| # | Issue | Status | Resolution |
|---|-------|--------|------------|
| 3 | UI thread blocking in OfflineModeWidget | **FIXED** | Created `ProcessingWorker` in `processing/worker.py` running in `QThread`. `OfflineModeWidget` now requests frame processing via signal, worker emits `ProcessingResult` via `result_ready` signal. Bounded processing (drops frames if worker busy). |
| 4 | Missing runtime dependencies | **FIXED** | Same as #2 — pyproject.toml now declares all required dependencies. |
| 5 | Dead code: storage.recording_legacy | **FIXED** | Removed `recording_legacy` dynamic import from `storage/recording/__init__.py`. Moved `recording.py` into `storage/recording/` package. Legacy classes (`FileRecordingSink`, `NullRecordingSink`, `Recorder`, `RollingFrameBuffer`) now exported directly from package. No remaining references to `recording_legacy`. |

### MEDIUM Issues

| # | Issue | Status | Resolution |
|---|-------|--------|------------|
| 6 | Processing imports storage.repositories via roi_resolver | **FIXED** | Resolved as part of #1 — old `processing/roi_resolver.py` deleted, new `core/roi_resolver.py` has no storage imports. |
| 7 | Offline imports processing.pipeline | **NOT FIXED** (acceptable) | `offline/source.py` imports `FrameSource` protocol from `processing.pipeline` — this is by design (offline reuses live processing pipeline). Documented as acceptable. |

### LOW Issues

| # | Issue | Status | Resolution |
|---|-------|--------|------------|
| 8 | ROI default geometry hardcoded | **NOT FIXED** (deferred) | `ROIConfigurationTab._create_default_geometry()` still uses hardcoded (100,100)-(200,200). Deferred per scope — would require camera/image dimension awareness. |

### INFORMATIONAL Items

| # | Issue | Status | Resolution |
|---|-------|--------|------------|
| 9 | Calibration parser+processor merge | **DEFERRED** | Not merged — separation of parsing vs. computation is clean. Revisit if navigation becomes difficult. |
| 10 | File consolidation opportunities | **PARTIAL** | Moved `recording.py` into `storage/recording/` package. Other consolidations deferred. |
| 11 | ROIEditorWidget defaults | **DEFERRED** | Same as #8. |
| 12 | Performance baseline documented | **NO ACTION NEEDED** | Baseline documented for future optimization. |
| 13 | Observer placeholder confirmed | **NO ACTION NEEDED** | Verified as pure placeholder. |

### Corrected Architecture Diagram

```
core (no deps)
  ↑
camera → core
calibration → core
processing → core, calibration
storage → core
offline → core, storage, processing
services → all domains
ui → services, core, offline, storage

CRITICAL RULE: processing → storage = FORBIDDEN (now enforced)
```

### Test Results After Fixes

| Metric | Before Fixes | After Fixes |
|--------|--------------|-------------|
| Passed | 369 | 369 |
| Skipped | 15 | 15 |
| Flaky | 0 | 0 |
| 3× Stability | ✅ | ✅ (3 consecutive runs: 17.30s, 18.47s, 17.55s) |
| Compile Check | ✅ | ✅ (no syntax errors) |

### Files Changed

| File | Change |
|------|--------|
| `core/roi_resolver.py` | **CREATED** — pure domain function `resolve_rois()` and `CachedROIResolver` |
| `processing/roi_resolver.py` | **DELETED** — old implementation with storage dependencies |
| `processing/pipeline.py` | **MODIFIED** — removed `Database` param, uses `CachedROIResolver` |
| `processing/worker.py` | **CREATED** — `ProcessingWorker` and `create_processing_worker()` factory |
| `processing/__init__.py` | **MODIFIED** — export `ProcessingWorker`, `ProcessingResult`, `create_processing_worker` |
| `ui/modes/offline.py` | **MODIFIED** — uses `ProcessingWorker` via QThread, bounded processing |
| `pyproject.toml` | **MODIFIED** — added dependencies and optional-dependencies |
| `services/recording.py` | **MODIFIED** — removed `recording_legacy` import |
| `storage/recording/__init__.py` | **MODIFIED** — removed legacy dynamic import, uses relative imports |
| `storage/recording/recording.py` | **MOVED** — from `storage/recording.py` to `storage/recording/recording.py` |

### Remaining Architecture Issues (Post-Stage 6B)

1. **GPU processing** — Not started (deferred per scope)
2. **Observer live acquisition** — Blocked on HALCON engineer investigation
3. **PTZ hardware control** — Not implemented
4. **Production alarm hardware integration** — Not implemented
5. **SQL migration runner** — Not configured (ADR mentions but no runner)
6. **ROI default geometry** — Hardcoded, deferred
7. **Calibration parser/processor merge** — Deferred, separation is clean

**All CRITICAL and HIGH findings from the audit have been resolved.**