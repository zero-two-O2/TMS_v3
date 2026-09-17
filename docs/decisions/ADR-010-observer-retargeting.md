# ADR-010: Observer Retargeting and Safe ROI Activation (Phase 8)

## Status

Accepted. Simulator-validated. Observer retargeting is implemented
through the audited per-frame resolution path; no Siemens data exists.

## Date

2026-09-17

## 1. Why retargeting is safe here

The pipeline already resolves `(camera_id, position_id)` per frame from
frame metadata, and acquisition never stamps `position_id`. The only
missing piece was a thread-safe active-position source. The fix is a
locked `(position_id, context_generation)` tuple on the pipeline,
snapshotted once per frame, with `AnalysisConfig` untouched (it already
holds every position; it is immutable and swapped never). No
acquisition, SHM, wire-protocol, or ROI-algorithm changes.

## 2. Stale-result prevention

Every produced `AnalysisResult` carries
`metadata={position_id, context_generation}`. The Configuration display
path drops results whose generation differs from the registry context
for that camera; legacy results without metadata pass through; the
pre-existing session-generation and camera checks are unchanged.
Latest-wins continues to drain backlog. No stale ROI result can
overwrite the active display.

## 3. Registry contract

`ActivePositionRegistry` is the single authority: immutable snapshots,
lock-serialized writes, generation-checked `set` (mismatch raises
`PtzStateError`), failure/cancel preservation, `clear` per camera on
session renewal, idempotent `shutdown`. New context fields
(`context_generation`, `session_generation`, `activation_state`,
`operation_id`) default so older constructions keep working.

## 4. Coordinator

`ObserverRetargetCoordinator` (one per endpoint, latest-wins per
camera) runs the existing `RoiActivationWorkflow` with
`commit_context=False`, publishes via the observer pass-through,
confirms by read-back, then commits the registry in one
generation-checked write. Publish failure, mismatch, cancellation, or
disconnect leaves the previous context intact (rollback = change
nothing; PTZ motion is never rolled back). No second workflow and no
second registry exist.

## 5. Movement vs activation success

COMPLETED requires reached + validated ROI + published + committed.
`first_result_confirmed` records observer read-back (False for
PTZ-only operation without an attached observer). ROI failure after a
good move keeps the reached PTZ position but records FAILED with the
old context active and visible.

## 6. REPLACE confirmation

Preview is unchanged (plus machine-readable `conflicts`). When
conflicts are the sole problem, the GUI lists affected records in a
Yes/No dialog; the background importer waits with generation checks
and treats stale/cancel as No. Commit stays all-or-nothing; ROI sets
are never deleted.

## 7. Observer UI surface

PTZ panel shows the registry-confirmed active position name (cleared
with the station). No Live changes: Live never commands PTZ, so no
retarget occurs there.

## 8. Lifecycle

Retained daemon threads + queued signals; bounded waits; generation
guards on every delivery; observer re-apply on (re)start with exact
stored generation; registry cleared on session renewal; idempotent
shutdowns everywhere. Eight-camera independence preserved (per-camera
locks, contexts, and cancel events).

## 9. Siemens gate

Unchanged and still blocked: no verified endpoint, namespace, nodes,
types, handshake, limits, tolerance, calibration, or error data. The
Phase 7 checklist stands as-is.
