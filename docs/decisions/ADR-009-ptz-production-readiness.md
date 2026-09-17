# ADR-009: Production Readiness — Profiles, Live Status, ROI Activation,
Import/Export, Diagnostics (Phase 7)

## Status

Accepted. Simulator-validated. No Siemens information exists; the
Siemens path is implemented as a blocked gate, not a mapping.

## Date

2026-09-17

## 1. Production configuration strategy

`ptz.profile` selects exactly one backend: `simulator` (localhost
endpoint enforced, development mapping), `generic` (endpoint + security
validated, but unusable until a concrete mapping is supplied), or
`siemens` (validated for endpoint presence, always blocked afterwards).
`validate_profile` never falls back across profiles. Security posture is
`none` or `username` + `password_env` (secrets stay in the environment;
the editor renders `password_env` read-only). `namespace_uri` is
declared for future server-side verification, never trusted blindly.

## 2. Simulator versus Siemens profiles

Simulator results are labelled `backend=simulator` in every report and
log. Nothing in the simulator path — namespace, node names, Int64,
limits, 0.1°-class tolerances, command codes, calibration timing,
error codes — is presented as Siemens behavior.

## 3. Siemens verification gate

`ptz/siemens.py` owns `REQUIRED_SIEMENS_FIELDS` (22 items),
`SiemensMappingDocument` (`siemens-ptz-map/v1`), `verify_siemens_document`
(fail-fast structural check), and `mapping_for_profile` (raises for
anything but simulator, even with a complete document — no
implementation exists yet). The meeting checklist is
`docs/siemens-plc-checklist.md`.

## 4. Live status architecture

Each tile carries one compact mono PTZ readout (`set_ptz_status`,
cleared with the tile). The wall owns a read-only monitor: 2 s timer,
one retained thread per tick with overlap guard, own `PtzService`
built via the shared `station.build_service` factory, `get_status`
only. Delivery is `(token, camera_id, text, tooltip)`; stale tokens
and unassigned cameras are dropped. Timer stops in the detach path;
services shut down inside the existing background teardown. Fixed
8-tile layout untouched; no acquisition/render coupling.

## 5. PTZ-to-ROI activation workflow

`RoiActivationWorkflow.run` executes VALIDATING → MOVING → WAITING →
ACTIVATING → COMPLETED/FAILED/CANCELLED on the caller (worker) thread
with bounded waits. Movement goes through `PtzService`; completion
requires Phase 5 reached criteria. Activation validates the binding,
the ROI reference (against `position_associations`), and generation
freshness, resolves via `resolve_rois`, then records an
`ActivePositionContext` in `ActivePositionRegistry`. Failures and
cancellation keep the previous context (rollback = change nothing; no
invented rollback movement). The observer is deliberately untouched:
acquisition never stamps `position_id` and the observer pins its
config, so no safe retarget API exists — the registry is Phase 8's
seam. Configuration Go To now runs this workflow.

## 6. Import/export format

`tms-ptz-positions/v1` JSON: IDs, camera/PTZ refs, coordinates,
velocities, metadata, ROI-set references, no ROI blobs, no SQL, no
executables. `preview_import` validates everything (schema, version,
fields, camera/PTZ scoping, limits left to service, duplicates,
1000-record bound) before `commit_import` persists all-or-nothing.
Policies: REJECT (default), SKIP, REPLACE (explicit). Panel Export/
Import buttons run dialogs on the GUI thread and I/O off-thread.

## 7. Conflict policy

REJECT fails the import listing conflicts; SKIP keeps existing rows;
REPLACE overwrites only after the preview was accepted by the caller
(UI: explicit dialog confirmation is a Phase 8 refinement; the library
already separates preview from commit so no silent overwrite exists).

## 8. Hardware acceptance

`ptz/diagnostics.py`: read-only checks (connect, namespace, per-field
reads with datatype assertions, status snapshot) plus movement checks
gated on explicit `confirmed=True` (STOP included). Every check logs
timestamp/PTZ/operation/outcome; unexecuted checks are `skipped`, never
passed. `python -m thermal_monitor.ptz.diagnostics` runs the same path
from a shell.

## 9. Safety and retry policy

No auto-move on startup/connect/reconnect; no command resend after
loss or interruption; STOP always available; limits confirmed from
configuration before diagnostics movement; no repeated movement loops.

## 10. Lifecycle and stale sessions

Retained daemon threads + queued signals everywhere; service/monitor
threads shut down bounded and idempotent; no callbacks after shutdown;
generation/token guards on every delivery; Live and Config services
are independent (modes are mutually exclusive).

## 11. Remaining uncertainties

Every Siemens item in §3 of the checklist; real tolerance/limits/error
semantics; observer retarget mechanism (Phase 8); multi-PTZ uplink
behavior on one CPU; production certificate handling.
