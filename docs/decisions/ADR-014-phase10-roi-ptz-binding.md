# ADR-014: Phase 10 — Position-Bound ROI and HALCON Analysis Tools

Status: Accepted
Date: 2026-09-18

## Context

ThermoView-style interactive ROI tools (spots, lines, areas, rulers,
annotations) were missing from TMS_v3. The existing ROI model
(`core.models.inspection.ROIConfig`) is camera-global, HALCON-shape-only
(rectangle1/rectangle2/circle/ellipse/polygon), and resolved per position
through `position_roi_associations` without any PTZ ownership. The
Phase 7/8 `ActivePositionRegistry` + `ObserverRetargetCoordinator` move
the PTZ and publish a processing context, but the UI had no drawing
surface, no measurement semantics, and no guard against stale results
after a position change.

## Decision

1. **Binding**: every ROI/analysis object (`roi.RoiDefinition`) is owned
   by exactly one `(camera_id, ptz_id, position_id)` triple. Ownership is
   immutable in place; rebinding requires the explicit `copy_to()`
   workflow. A position from camera A can never be saved under camera B
   (`validation.check_position_belongs_to_camera`).
2. **Active context ownership**: one `roi.RoiContextRegistry` per
   application is authoritative for the active `RoiActiveContext`
   snapshot (camera, PTZ, position, position/session/context
   generations, operation id, ROI ids, image size). It coexists with the
   Phase 7 `ActivePositionRegistry` (processing-path seam) rather than
   replacing it; the Phase 10 `RoiActivationPublisher` reuses the
   coordinator's observer-published generation so both stay aligned.
3. **Context generation / stale prevention**: every measurement is
   stamped `(camera, session, position, context generations, frame
   id/timestamp)`. Results failing the match are discarded before UI or
   alarm delivery. Older operations can never publish after newer ones
   (operation-id ordering + session-generation checks); failures keep
   the previous context untouched.
4. **Coordinate system**: canonical persistent representation is image
   pixel coordinates in HALCON row/col convention, tied to source image
   size (640x480). `roi.ViewportMapping` converts widget<->image with
   letterbox offset, uniform zoom scale, and pan. Widget resizes never
   modify stored geometry.
5. **HALCON boundary** (`halcon/` package): immutable NumPy snapshot in,
   typed `RoiMeasurement` list out. Geometry converts via
   `halcon.geometry.halcon_params_for`; area ROIs reuse the proven
   `HalconROIAdapter` (`intensity` + `min_max_gray`) when HALCON is
   installed, all other tools use the documented NumPy implementations
   in `roi.measurements` (direct sample, argmin/argmax, masked stats,
   Bresenham sampling). No PyQt in domain models; no blocking HALCON on
   the GUI thread (evaluation runs on worker/consumer threads;
   `RoiEvaluator` is latest-frame-wins with a single-slot queue).
6. **Persistent geometry**: versioned JSON (`schema_version=1`) in the
   new `roi_definitions` table; explicit column lists everywhere, no
   `SELECT *` mapping. SQLite (`004_...`) and SQL Server (`003_...`)
   schemas aligned. Legacy `rois` / `position_roi_associations` tables
   untouched.
7. **Threading**: GUI thread never blocks (no HALCON, no DB I/O, no
   waits). Bounded single-slot evaluator queue; per-frame duration and
   stale-drop counters exposed for diagnostics.
8. **Modes**: Configuration Mode is editable (toolbar + properties);
   Live/Observer is read-only (overlays follow the active context);
   Offline reuses definitions/measurement logic and shows an explicit
   state when no position context is confirmed.

## Consequences

- ROI CRUD is position-scoped; undo/redo history is cleared on every
  position change.
- Ruler tools report pixel distances only; physical units are marked
  `calibration: unavailable` (no invented mm values).
- Raw intensity is never presented as Celsius; no-data images produce
  explicit invalid results.
- Hardware acceptance (thermal alarms, GVSP input, Siemens PLC) remains
  BLOCKED; simulator + synthetic coverage is recorded in the test suite.

## Known limitations

- `CameraRegion` composes (not replaces) `LiveThermalWidget`; legacy
  `ROIPanel` (camera-global list) still exists alongside the new
  position-scoped repository — full panel migration is future work.
- Polygon hit-testing/editing and hottest-spot region rendering are
  minimal (creation + statistics work; vertex dragging is not
  implemented).
- SQL Server migration `003` is syntax-checked only (no live server in
  CI); SQLite `004` is fully tested including upgrade preservation.
