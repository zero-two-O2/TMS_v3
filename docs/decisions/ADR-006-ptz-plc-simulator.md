# ADR-006: PTZ/PLC Simulator (Phase 4)

## Status

Accepted (development simulator; NOT a Siemens emulator).

## Date

2026-09-16

## 1. Why the simulator exists

The real Siemens ET 200SP / CPU 1510SP-1 PN is unavailable, so a
deterministic OPC UA server reproducing the *logical subset* TMS_v3 needs
(movement, reached, velocity, calibration, errors, loss) lets the Phase 3
client, the future controller, and the future UI be validated now. It runs
as an independent headless process (`python -m tools.ptz_plc_simulator`)
and is never imported by production camera runtime code.

## 2. Namespace

`urn:tms:ptz:sim`, development-only. The server asserts the registered
namespace index equals `SimulatorPtzMapping.namespace_index` (2) and fails
fast otherwise, so node IDs can never silently diverge from the mapping.

## 3. Node mapping source

`SimulatorPtzMapping` is the single authority. The server builds every
`PTZ_xx.<Name>` variable from `available_fields` + `resolve` -- no
duplicated node IDs. Simulator OPC datatype choices: float→Double,
bool→Boolean, int→Int64 (plain Python round-trips), published with
explicit variants.

## 4. Eight PTZ instances

One `PtzSimulationEngine` + eight independent `SimulatorPtzState`
objects (default `PTZ_01..PTZ_08`, configurable). Verified: per-PTZ
targets never cross-talk, including an 8-way concurrent wire test.

## 5. Movement model

Time-based: each tick advances `actual` toward `target` by at most
`velocity*dt` per axis (no overshoot, monotonic clock, single loop for
all PTZs, per-PTZ exceptions isolated). Pan/tilt integrate independently;
`POSITION_REACHED` requires both axes within `PtzTolerance`, then snaps
to target. Latest MOVE wins (no queue); STOP aborts in place.

## 6. Velocity semantics

Phase 2/3 `SINGLE`/`PER_AXIS` preserved via `command_to_fields`.
Simulator convention: PER_AXIS is armed when a client writes
PAN/TILT_VELOCITY differing from VELOCITY, so SINGLE-mode clients must
write all three velocity nodes consistently (the integration helper and
the future controller do this). Out-of-envelope velocities are rejected
with latched errors, never clamped.

## 7. Position-reached tolerance

Phase 2 `PtzTolerance`, per axis, from simulator config (default 0.1° --
SIMULATOR DEFAULT, not a machine spec).

## 8. Calibration behaviour (SIMULATED)

`CALIBRATION_REQUEST=True` → ACTIVE, READY=False, motion stopped →
after `calibration_duration_s` → COMPLETE, REQUIRED=False, READY=True,
actual/target reset to the configured home (default 0,0). Movement
during ACTIVE is rejected. A `fail_next_calibration` hook produces
ERROR + `ERR_CALIBRATION_FAILED` instead. The real Siemens sequence is
unknown; none of this claims to represent it.

## 9. Error injection (deterministic, never random)

`inject_ptz_error` (ERROR + code, READY=False, moves rejected),
`clear_error`, `inject_motion_freeze` (MOVING held, never converges --
timeout scenario), `fail_next_calibration`, `set_enabled(False)`
(not-ready scenario), `stop_listening`/`start_listening` (endpoint down,
engine alive -- communication-loss scenario). Rejections latch
`ERROR` + code + `last_rejection`; `COMMAND=3` clears.

## 10. Command semantics (SIMULATOR PROTOCOL)

Stage TARGET_PAN/TILT (+velocities), then `COMMAND=1` (MOVE). `2`=STOP,
`3`=CLEAR_ERROR, server resets COMMAND to 0 after processing. This
handshake is development-only; the real PLC handshake is unknown.

## 11. NOT representative of Siemens

Namespace, node names/IDs, datatypes, Int64 choice, limits, tolerance,
timings, command codes, calibration sequence/duration/home-reset, error
codes, timeout and loss behaviour are all simulator inventions.

## 12. Siemens replacement path

When tag documentation arrives: implement the tag table in a configured
`SiemensPtzMapping` (still a stub that refuses to guess), point the
client at the real endpoint/security, and leave `PtzCommand`,
`PtzStatus`, `OpcUaSession`, engine-free production code untouched. The
simulator remains for regression testing.

## 13. Integration findings applied

* Phase 3 `AsyncuaTransport.subscribe` imported `Subscription` from the
  wrong location for asyncua>=2 -- fixed minimally to
  `asyncua.common.subscription` (only Phase 3 change; no semantics).
* Dependency: `asyncua>=1.0` verified installable as the `ptz` extra;
  integration tests skip cleanly without it (`importorskip`).
