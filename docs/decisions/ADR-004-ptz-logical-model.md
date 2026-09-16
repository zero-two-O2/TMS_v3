# ADR-004: Logical PTZ Model (Phase 2)

## Status

Accepted (model only; no OPC UA, simulator, UI, or DB in this phase).

## Date

2026-09-16

## 1. What `PtzCommand` represents

`ptz.models.PtzCommand` is an immutable application-level movement request:
"move this PTZ to this target" (pan/tilt in degrees, velocity in deg/s,
`VelocityMode`, `MoveMode`, application-only `request_id`). It is NOT a PLC
tag write. A future Siemens/simulator mapping translates it into
protocol-specific operations; the command never changes.

## 2. What `PtzStatus` represents

`ptz.state.PtzStatus` is an immutable authoritative status snapshot as
reported by the PLC/simulator: PLC session state, PTZ availability,
communication health, readiness, movement state, actual pan/tilt,
calibration state, and an optional structured `PtzError`. It carries no UI
fields. `moving`/`position_reached` are derived from the authoritative
`PtzMovementState`; "command sent" never implies "position reached".

## 3. Logical model vs PLC mapping

The logical model (`src/thermal_monitor/ptz/`) knows no OPC UA, node IDs,
namespaces, DB offsets, endpoints, or datatypes. The later mapping layer
(`protocol.py`, Phase 3+) converts `PtzCommand <-> OPC UA nodes` for the
simulator and, separately, for the real Siemens CPU 1510SP-1 PN, whose tag
table is still unknown. The UI/controller only ever speaks logical types.

## 4. PLC connection vs PTZ availability

One OPC UA session (PLC-level) serves many PTZ tag-sets (PTZ-level).
`PtzStatus.plc_state` tracks the session; `ptz_available` /
`communication_ok` / `ready` track the logical PTZ. Camera acquisition and
PTZ communication are independent: camera CONNECTED + PTZ UNAVAILABLE is a
first-class representable state, and a PTZ failure must never disconnect
the thermal camera.

## 5. Velocity abstraction

`VelocityMode.SINGLE` (one velocity) vs `PER_AXIS` (pan/tilt velocities).
The real PLC semantic is unknown, so the command enforces internal
consistency per mode (SINGLE requires `velocity` only; PER_AXIS requires
both axis velocities) and the mapping translates to whichever form the
hardware uses.

## 6. Position reached

`PtzMovementState`: `IDLE -> MOVING -> POSITION_REACHED` (skipping `MOVING`
is rejected by `allowed_movement_transition`). Reached is evaluated with
the pure predicate `within_tolerance(actual, target, PtzTolerance)` against
PLC-reported actuals, never assumed from command dispatch.

## 7. Calibration states

`CalibrationState`: `NOT_REQUIRED / REQUIRED / ACTIVE / COMPLETE / FAILED`.
State representation only; the real Siemens calibration sequence is
unknown and will live in the later controller/mapping. `ACTIVE`
calibration and any `error` both fail the `accepts_commands` gate.

## 8. Unknown hardware values

Endpoint, namespace, node IDs, datatypes, DB offsets, handshake, limits,
velocity semantics, tolerance, error codes, calibration sequence, and
per-PTZ addressing are all UNKNOWN (no PLC/HMI reference provided). All
are configurable (`PtzLimits` bounds are `Optional`, `None` =
unconstrained; `PtzTolerance` takes explicit values). Tests use
`TEST_LIMITS`-style fixtures only, never claimed machine values.
Validation never silently clamps; it raises `PtzValidationError`.
