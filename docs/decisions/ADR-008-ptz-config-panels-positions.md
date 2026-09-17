# ADR-008: PTZ Configuration, Shelf Panels, and Position Persistence (Phase 6)

## Status

Accepted (simulator-validated; Siemens tags still unresolved).

## Date

2026-09-17

## 1. PTZ configuration and camera association

Per-camera `ptz_id` + optional `ptz_endpoint` override live on
`cameras.mapping[]`; global `ptz.*` carries endpoint, tolerances,
velocity mode/defaults, and bounded timeouts. Blank `ptz_id` means
"not configured" (never defaulted to PTZ_01); missing endpoint means
"not connected", reported distinctly from "disconnected". Duplicate
`camera_id` entries are rejected at load. `ptz/station.py` bridges
validated YAML values into `PtzStationBinding`, merged `PtzLimits`,
and `PtzServiceConfig` without `ptz/` importing the config layer.

## 2. Shelf registration

Canonical `_register_side_panel` only: `ptz_control`/`PTZ Control` on
the left, `ptz_positions`/`Position Table` on the right. Existing
shelf tests were extended (title dicts), not weakened.

## 3. Panel responsibilities

Both panels are service-agnostic surfaces: the control panel emits
move/relative/stop/clear/calibrate intents and renders
`PtzStatus`/`PtzOperation` snapshots (enablement derives from
`accepts_commands`); the table renders `PtzPosition` rows and emits
CRUD/GoTo intents. No OPC UA, service, or DB imports in either widget.

## 4. Database position model

New `ptz_positions` table (migration `002`, GO-batch convention):
stable `position_id` UNIQUE, `camera_id` owner, recorded `ptz_id`,
pan/tilt, nullable velocities, `roi_set_ref`, enabled flag. No FK to
cameras/rois (YAML and runtime own those lifetimes); deletes never
cascade to ROI sets. `PtzPositionRepository` follows
`BaseRepository`/`RepositoryResult` with explicit domain methods.

## 5. ROI-set association strategy

Reference-only: `roi_set_ref` keys the existing
`position_roi_associations`/`PositionROIAssociation` model, resolved
for display via `resolve_rois`. ROI blobs are never copied. Go To
reports the association as metadata; ROI application stays manual
(documented Phase 6 boundary).

## 6. Go To workflow

Load position → `check_position_binding` against the live binding
(refuse cross-PTZ motion explicitly) → `move_absolute(wait=True)` →
report REACHED only on Phase 5 completion criteria (flag + tolerance).

## 7. Camera switching / mode lifecycle

`_begin_session` clears panels (no stale rows); connect completions
attach PTZ asynchronously (no auto-move, no auto-resend); disconnect
returns panels to binding-only display while the shared session stays
up for reuse. All worker outcomes return via `_ptz_*` signals guarded
by `(camera_id, generation)`. Mode deactivation/close shuts services
down on a daemon thread without blocking the visible transition.

## 8. Threading

Retained daemon threads + queued signals (existing `_run_background`
philosophy); service/monitor threads owned by `PtzService` with
bounded joins; no GUI-thread I/O (OPC UA and SQL both off-thread);
no callbacks after destruction (generation + shutdown guards).

## 9. Errors and stale state

Domain errors surface verbatim (not "Failed"); missing binding,
missing endpoint, missing database, and binding-mismatch each have
distinct messages. Stale deliveries are dropped structurally.

## 10. Simulator vs Siemens

Simulator mapping/endpoint only via configuration; production domain
holds no simulator assumptions. `SiemensPtzMapping` still refuses.
Unknown for Siemens: everything in prior ADRs, unchanged.
