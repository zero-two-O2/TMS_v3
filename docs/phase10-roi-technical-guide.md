# ROI System — Technical Guide (Phases 10–11)

How the ThermoView-style, position-bound ROI and analysis system works
in TMS_v3, and how to operate it.

## 0. Ownership example

```text
Camera A (cam_HB25100001 / PTZ_08)
    Position 1 (pos_aaa; pan 90°, tilt 90°)
        ROI 1 (Chair ROI)
        ROI 2 (Machine ROI)
    Position 2 (pos_bbb; pan 60.3°, tilt 65.9°)
        ROI 3 (Door ROI)
        ROI 4 (Equipment ROI)
```

The operator selects or reaches a PTZ position; the software
automatically loads the matching ROI definitions. Position 1 ROIs are
never displayed while Position 2 is active, and vice versa.

## 1. Creating an ROI

1. In Configuration Mode, select a camera, then `Go To` a saved PTZ
   position in the Position Table.
2. After the PTZ reaches the target, the Camera Region toolbar enables
   and shows `Camera / PTZ / Position / ROI: N object(s)` (or `ROI: No
   ROI configured for this position`).
3. Pick a tool from the compact icon toolbar (Select, Spots, Lines,
   Regions, Measure, Annotate). Hover any icon for its full name.
4. Draw on the image:
   - Spot / Note: single click.
   - Lines, rectangles, ellipses, circles, cross, search regions:
     click-drag-release, or click-click.
   - Polyline / Polygon: click vertices, double-click to finish,
     `Esc` cancels.
   - Measure Angle: three clicks (end, center, end).
5. The object is created in **image coordinates**, becomes selected,
   and the tool returns to Select. It is saved to the database on a
   background thread; if persistence is unavailable the change is kept
   for the session and a notice is shown.

Without a reached position the toolbar stays disabled: an ROI can
never be silently saved under the wrong camera or position.

## 2. Geometry storage

`RoiDefinition` (`src/thermal_monitor/roi/models.py`) is a frozen,
validated dataclass: `(camera_id, ptz_id, position_id)` ownership,
`RoiObjectType` (22 types), a typed geometry object, name, enabled /
visible flags, timestamps, `schema_version = 1`, optional
`analysis_config`, optional `alarm_rule_ref` (analysis objects only),
and display properties.

Canonical coordinates are **image pixels in HALCON row/col convention**
(row = y down, col = x right) tied to the source image size
(640x480). Widget coordinates are never persisted. Geometry serializes
to versioned JSON (`roi_to_dict` / `roi_from_dict`); unknown schema
versions and type/geometry mismatches are rejected.

Bounds: names ≤ 120 chars, notes ≤ 500 chars, polygons/polylines ≤ 256
vertices, ≤ 500 objects per position, geometry JSON ≤ 64 KiB.

## 3. Loading after position selection

`Position Table Go To` (row click alone never moves) → canvas locked
("Moving…", previous context visible but read-only) → `PtzService`
move → authoritative reached → `ObserverRetargetCoordinator` publishes
the processing context and commits `ActivePositionRegistry` → the same
worker calls `roi.loading.load_rois_for_position` (owner + generation
validation, exact `(camera, position)` query, per-ROI validation) →
GUI installs the immutable session atomically. A second request
supersedes the first (operation / generation guards); failures and
cancellations unlock the previous session unchanged. Empty sets
publish an explicit `NO_ROIS` state that still permits creation.
Deleting a position removes its ROI rows in the same transaction; if
the active position vanishes, the session clears rather than showing
stale ROIs.

## 4. HALCON input

`halcon/HalconSnapshotRunner.run(snapshot, rois, context, ...)`:

1. Copies the NumPy snapshot read-only (caller-owned, worker thread).
2. Verifies 2-D, non-empty, finite pixels (no-data → explicit invalid).
3. Converts geometry via `halcon.geometry.halcon_params_for`
   (`gen_rectangle1/2`, `gen_circle`, `gen_ellipse`,
   `gen_region_polygon_filled`, `gen_region_line`, `min_max_gray`,
   `angle_ll`).
4. Area ROIs use the proven `HalconROIAdapter` (`intensity` +
   `min_max_gray`); all other tools use the documented NumPy
   implementations in `roi.measurements` (same semantics, ±1e-6).
5. Returns typed `RoiMeasurement` values stamped with the full context.

HALCON is installed in this environment (`import halcon` succeeds);
area statistics additionally run through the real operators when
available, otherwise the NumPy path. No HALCON on the GUI thread, no
shared mutable HALCON handles, no per-frame display conversions.

## 5. Temperature statistics

Raw thermal values only — never RGB, never the visible feed. Spot =
direct sample; hottest/coldest = argmax/argmin over finite pixels of
the search region (documented `numpy_argmax`/`numpy_argmin`); areas =
masked min/max/mean/std/count plus extrema locations; lines =
Bresenham sampling with distances, per-point temperatures,
min/max/mean (the on-image profile payload). Raw→temperature
conversion happens once per frame upstream (calibration provider /
`CPUTemperatureConverter`); per-ROI work is statistics only.
Uncalibrated rulers report pixels + `calibration: unavailable`.

## 6. Result delivery and stale rejection

GUI widgets (`CameraRegion`, Configuration canvas) accept measurements
only when `(camera, session, position, context generations)` match the
published snapshot; anything else is discarded before painting, with a
counter (`stale_discarded`, evaluator `dropped_stale`). The evaluator
keeps a single latest-frame slot (bounded, latest-wins) with
`submitted/evaluated/dropped` stats and per-run duration. Invalidation
is camera-scoped: other cameras are unaffected.

## 7. Future alarm source

`RoiMeasurement` already carries everything the alarm evaluator needs
(camera, PTZ, position, ROI id, kind, values, frame/context metadata).
Only `category == ANALYSIS` objects may reference alarm rules today;
measurement/annotation objects cannot. Wiring the evaluator to consume
these results (with the same generation guards) is the defined next
step — no model changes required.

## 8. Status meanings

- `No position — select and reach a saved position`: no session.
- `Moving to X… (ROI locked)`: PTZ in flight, previous context
  read-only.
- `ROI: N object(s)`: active session with objects.
- `ROI: No ROI configured for this position`: active session, creation
  allowed.
- `ROI session failed: …`: load/validation failed, previous session
  kept.
- Position Table `ROIs` column shows the validated count (`—` =
  unknown, never zero-by-default); the reached row carries a `●`
  marker independent of row selection.
- Set ROI button: edits ROIs for the selected position, moving there
  first via Go To when the camera has not reached it. The internal
  `roi_set_ref` is retained for legacy activation compatibility but is
  never shown or edited in the table.
