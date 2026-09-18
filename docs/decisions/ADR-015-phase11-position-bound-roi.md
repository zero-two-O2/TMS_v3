# ADR-015: Phase 11 — Corrected Position-Bound ROI Architecture

Status: Accepted
Date: 2026-09-18
Supersedes (in part): ADR-014 §"Active context ownership"

## 1. Why Phase 10 was incorrect

Audit of the Phase 10 tree found five defects:

1. **Dead duplicate registry.** `roi.RoiContextRegistry` was populated
   only by `RoiActivationPublisher`, which no production path used
   (Configuration drives `ObserverRetargetCoordinator` directly).
   Two "authoritative" context stores existed; only
   `ActivePositionRegistry` was live.
2. **ROI Set column.** The Position Table exposed the internal
   `roi_set_ref` and a manual association dialog, forcing operators to
   manage collection ids. The normal workflow must derive ROIs from the
   reached position alone.
3. **Orphaned ROI rows.** Position deletion never removed
   `roi_definitions` rows, which could later load under another query.
4. **Legacy overlay competition.** With no session, Configuration
   painted camera-global legacy ROIs as if they were the active view.
5. **Schema drift.** SQL Server `003` carried `created_at_dt` /
   `updated_at_dt` audit columns that SQLite `004` lacked.
6. **No movement lock.** Drawing stayed enabled while the PTZ moved.

Go To transport itself was sound (existing suites green); the
breakage was ROI follow-through and table UX.

## 2. Ownership (final)

```text
Camera (camera_id)
 └── Saved PTZ Position (position_id, stored record — never name/pan/tilt)
      └── ROI Definition (roi_id, camera_id, ptz_id, position_id)
```

Every persisted ROI carries the full triple; loads query exact
`(camera_id, position_id)` with explicit column lists. `position_id`
is the stable `pos_<hex>` record id in every path.

## 3. Why the ROI Set column was removed

`roi_set_ref` remains a real column: legacy activation
(`RoiActivationWorkflow`) treats a non-empty ref as a known-association
key, and position import/export round-trips it. Deleting the column
would break that compat and strand data, so it is **retained
internally, hidden from the UI**. The visible table shows operator
data only: Name, Pan, Tilt, Velocity, ROIs (read-only validated
count, "—" unknown), Enabled. The reached row carries a "●" marker
independent of Qt selection. The manual association dialog was
replaced by a Set ROI action that starts the position-bound editing
workflow (Go To + wait when not reached).

## 4. Position-switch lifecycle (exact)

```text
Go To requested (GUI) -> movement lock (canvas read-only, "Moving…" label)
 -> PtzService move_absolute -> authoritative REACHED (tolerance-checked)
 -> ObserverRetargetCoordinator publishes processing context + commits
    ActivePositionRegistry (single registry; failures/cancel keep prior)
 -> load_rois_for_position validates owner + generations, queries ONLY
    (camera, position), validates every ROI -> immutable RoiActiveContext
 -> GUI installs RoiEditor session atomically (overlays replaced wholesale,
    old results invalidated camera-scoped, table marker + counts updated)
```

Row selection alone never moves the PTZ and never changes the active
context. Failure/cancel emits `_roi_session_unlock`: the previous
session resumes editable, nothing partial publishes. Camera switch and
vanished positions clear the session.

## 5. Failure preservation & stale rejection

Previous context survives movement failure, timeout, cancellation,
binding mismatch, stale sessions, and failed session installs. Every
measurement is stamped
(camera, session, position, context generations, frame); mismatches are
discarded before paint/alarm/DB. Callbacks verify
`_ptz_is_current(camera, generation)`; the coordinator's latest-wins
per-camera serialization prevents older completions overwriting newer
contexts.

## 6. Legacy ROI handling

Legacy `ROIConfig`/`AnalysisConfig` in-memory models still feed the
observer processing pipeline and the ROIPanel editor (dormant SQL
repositories untouched per the SQL Server constraint). Configuration
**display** is session-owned exclusively: with a session the legacy
painter returns early; without one it clears instead of painting
camera-global defaults. One overlay owner, no clobbering.

## 7. Schema alignment & deletion policy

SQLite `005` adds the audit columns to match SQL Server `003`; a
contract test parses both migration files and asserts identical column
sets. Position deletion = ONE transaction deleting the position row
(camera-scoped) plus all its ROI definitions (policy 1); pre-ROI
databases fall back gracefully. Import/export (`tms-roi-collection`
v1 + position profiles) preserves camera/position ownership.

## 8. HALCON vs NumPy boundary

- Pure interaction (hit-test, drag, resize, vertex edit, coordinate
  mapping): PyQt6 + pure geometry, never HALCON, never the GUI thread
  for analysis.
- Processing boundary (`HalconSnapshotRunner`, worker thread):
  snapshot in, typed `RoiMeasurement` out. Area ROIs use the proven
  adapter (`gen_rectangle1/2`, `gen_circle`, `gen_ellipse`,
  `gen_region_polygon_filled`, `intensity`, `min_max_gray`); spots,
  extrema (`min_max_gray` semantics), line profiles, rulers, angles use
  the documented NumPy implementations. GUI-thread separation is
  enforced by a subprocess import test (domain + HALCON layers import
  without PyQt6).

## 9. Limitations / hardware-gated acceptance

Simulator acceptance (scripted transport + real service/coordinator/
repositories) covers the full lifecycle including failure preservation
and delete-cascade. BLOCKED/NOT TESTED: physical TV46L thermal input,
Siemens PLC, live SQL Server migration run, on-hardware timing. GUI
event-loop acceptance is offscreen Qt (no display server available).
