# ADR-007: PTZ Controller and Service Layer (Phase 5)

## Status

Accepted (no GUI, no database; simulator-validated).

## Date

2026-09-17

## 1. Responsibilities

- `PtzController` (one per `ptz_id`): gating against live status,
  validation, dispatch (logical fields + strobe), bounded completion
  monitoring, stop / clear-error / calibration workflows, operation
  bookkeeping. Owns no threads.
- `PtzService`: camera→PTZ bindings, shared-session controllers,
  camera-addressed public API, listener registry, exactly one monitor
  thread. Dispatch paths always read live; the monitor only feeds UI
  snapshots and staleness marking.
- `OpcUaSession` (unchanged): transport, reconnect, subscriptions.

## 2. Why GUI-independent

All calls are blocking-but-bounded (explicit timeouts, monotonic
deadlines) and thread-safe, so a future thin QThread host can expose
them to panels without touching logic. No Qt import exists in `ptz/`
(UI must call from a worker thread; documented on `connect`).

## 3. Camera→PTZ binding

`PtzStationBinding` registry; unknown cameras fail explicitly (never
default to another PTZ); conflicting re-registration fails; identical
re-registration is idempotent. One controller per `ptz_id`, shared
across cameras bound to it; per-PTZ locks guard bookkeeping only, never
held across network I/O.

## 4. Absolute command boundary

`dispatch` accepts ABSOLUTE commands only; RELATIVE is resolved in
`move_relative` via live-read actuals + `PtzCommand.to_absolute`. No
relative value reaches the protocol. SINGLE mode mirrors velocity onto
the axis nodes (simulator convention; Siemens rule defined later).

## 5. Relative movement

Live `read_status_strict`; reject when not `communication_ok` /
`ptz_available`. Increments (1°/5°/10°) are caller inputs, validated
against limits after resolution like any absolute target.

## 6. Movement completion

Terminal REACHED requires dispatched write + authoritative
`POSITION_REACHED` **and** independent `within_tolerance` agreement
(flag outside tolerance never counts). Already-at-target completes
without requiring a MOVING observation. Poll interval 0.05 s default.

## 7. Timeout policy

`move_timeout_s` (30 s), `calibration_timeout_s` (60 s), per-call
override; monotonic deadlines; transient read errors retried until
deadline; timeout yields FAILED/MOVEMENT_TIMEOUT (never silent).

## 8. Connection loss / reconnect

Session owns reconnect (bounded, Phase 3). Controller wait loops fail
operations fast on non-CONNECTED session, retry transient reads
otherwise, and never resend movement automatically. `stop`/`shutdown`
during operations cancel deterministically; waiters detect
cancellation via active-id mismatch (fixed during Phase 5 testing).

## 9. Calibration gating

Calibrate rejected unless connected, error-free, and idle (explicit
STOP-first policy). Moves rejected while ACTIVE via `accepts_commands`.
Completion = ACTIVE seen then COMPLETE (or ready+error-free); error or
bare ACTIVE-clear without COMPLETE raises CALIBRATION failure. No
simulator timing assumed (duration/timeout configurable).

## 10. Error handling

Uniform `PtzCommandError(.error: PtzError, .operation?)`; categories:
validation/limits→COMMAND_REJECTED, offline→COMMUNICATION,
latched→PTZ, active calibration→CALIBRATION, timeout→MOVEMENT_TIMEOUT.
`CommandStrobe` isolates simulator codes (MOVE=1/STOP=2/CLEAR=3);
Siemens replaces the three methods only.

## 11. Status monitoring

Bounded polling loop (subscriptions deferred: single loop, no restore
races, deterministic in tests). Live reads assemble `PtzStatus`
(movement from MOVING/REACHED/ERROR flags, calibration precedence
ACTIVE>COMPLETE>REQUIRED, error code mapped to PTZ category); failures
yield degraded last-known snapshots with `communication_ok=False`;
stale beyond threshold marked accordingly. Listeners contained,
generation-guarded (none after shutdown). Shutdown: idempotent,
bounded monitor join, operations cancelled, session disconnected
(transport stays with its creator).

## 12. Hardware boundary

Validated against simulator only. Unknown for Siemens: endpoint,
namespace, tags, handshake, velocity rule, tolerance, limits, error
codes, calibration sequence. `SiemensPtzMapping` still refuses.

## 13. UI integration points (future)

`PtzService` API (`connect/disconnect/get_status/move_absolute/
move_relative/stop/clear_error/request_calibration/wait_until_reached/
is_ready/shutdown`) + `PtzOperation` snapshots + status listeners are
the complete panel/position-table contract. Panels live on the existing
shelf; association comes from configuration (later phase).
