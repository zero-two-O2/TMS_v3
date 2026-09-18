# ADR-013: Simulator-Based GUI Acceptance & PTZ Hardening (Phase 9D)

## Status

Accepted. Simulator-only. No Siemens compatibility claimed.

## Context

ADR-012 (Phase 9C) fixed Configuration Mode PTZ attachment at the unit
level, but GUI click-through acceptance was incomplete and the real
Siemens PLC is unavailable. Phase 9D ran the actual GUI widgets against
the live simulator (`python -m tools.ptz_plc_simulator`,
`opc.tcp://127.0.0.1:4840`, `urn:tms:ptz:sim`, 8 PTZ units) via
offscreen Qt driving the real `ConfigurationModeWidget` handlers, the
real `PtzService`/`OpcUaSession` chain, and the real SQLite store.
Camera child-process acquisition has no hardware here, so the camera
transport path itself could not be exercised.

## Defects found and fixed

1. **Monitor outage poll spam (production behavior).**
   `PtzService._monitor_loop` polled at the full 0.5 s interval during
   a dead-link outage, emitting one `[OPC-UA] ... transport failure`
   warning per controller per cycle (proven: sim-kill probe).
   Fix: bounded exponential outage backoff (interval → 2x → 4x, capped
   at `_OUTAGE_BACKOFF_MAX_S = 2.0` s). Degraded statuses are still
   delivered every cycle; any fresh read resets immediately; the wait
   uses `Event.wait` so `stop_monitoring` stays prompt.
2. **Simulator per-Read INFO spam (simulator-only).**
   asyncua logs every OPC UA Read at INFO via
   `asyncua.server.uaprocessor` (~7/s per client; 125 lines in ~18 s
   for one PTZ). Proven normal poll traffic, not errors. Fix:
   `_quiet_asyncua_poll_logging()` sets exactly that logger to WARNING
   in the simulator entry point. Verified live: 125 → 0 lines; startup
   nodeset INFO lines remain (one-time, untouched).

## What was verified live (29/29 scripted checks)

Attach → Ready (`PTZ_01` shown, Go enabled, top chip ready); absolute
moves incl. second/tilt-only moves (Moving → Reached, ±0.6°);
independent monitor-service consistency (same actual/target/moving);
incremental ±5° pan both directions off fresh actuals; STOP mid-move
(`moving=False, reached=False`, frozen mid-travel); error display +
live Clear Error round-trip; calibration start → Ready; camera switch
`PTZ_01 → PTZ_03` with stale-A delivery ignored; unconfigured camera
explicit `No PTZ configured`; disconnect clears scoped state while the
shared service survives; re-attach restores; SQLite insert/history/
restart-survival; mapping save preserves `ptz_id`; no `.tmp`
leftovers; GUI `processEvents` < 1 ms during moves. Simulator restart:
kill → `comm=False, ready=False` with no traceback; fresh service
reconnects `ready=True` after restart (old sessions do not auto-rejoin
by design — re-attach is the bounded lifecycle). Headless app boot:
construct + init clean, controller owns a connected `SqliteDatabase`
at `data/tms_local.db`.

## Follow-up: ptz_positions schema fix (live defect)

Live log `Save failed: table ptz_positions has no column named
velocity` (Position Table save for `cam_HB25100001`) exposed a
three-layer stack: (1) SQLite `002_ptz_positions.sql` diverged from
canonical SQL Server `002` — extra `zoom`, missing
`velocity/pan_velocity/tilt_velocity`, wrong column order for the
positional `SELECT *` mapping; (2) `BaseRepository.insert`'s T-SQL
`SELECT SCOPE_IDENTITY()` probe fails on SQLite (same trap already
documented in `sqlite_alarm.py`). Fix: versioned
`sqlite/003_ptz_positions_velocity.sql` rebuilds the table to
canonical shape/order preserving rows; `PtzPositionRepository.insert`
overrides to a probe-free INSERT on the SQLite backend only (SQL
Server path unchanged). Verified on a copy of the live DB
(2→3 upgrade, full CRUD, duplicate rejection) plus new regression
tests. The live `data/tms_local.db` (0 position rows) picks up 003
automatically on next app start via the existing startup migration
run — restart the app to clear the error. No data at risk.

## Boundaries (not validated)

Camera child-process connect/streaming, thermal-triggered alarm edges,
and any Siemens behavior. Thermal alarm generation is BLOCKED (no
camera input); edge-trigger/dedup logic is covered by Phase 9B unit
tests only. Alarm detail/history windows covered by Phase 9B offscreen
tests. Full 1599-test suite not run in one shot (tool timeout);
affected groups run in splits with no new failures.
