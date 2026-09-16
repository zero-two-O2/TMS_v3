# ADR-005: PTZ OPC UA Communication Architecture (Phase 3)

## Status

Accepted (foundation only; simulator server is Phase 4, UI is later).

## Date

2026-09-16

## 1. Why OPC UA is isolated from the PTZ model

`ptz/models.py` and `ptz/state.py` (Phase 2) import nothing from
`protocol.py`, `mapping.py`, or `client.py`. The dependency direction is
strictly `models/state -> mapping/protocol -> client`. The logical
`PtzCommand`/`PtzStatus` contract therefore survives any transport swap
(simulator today, Siemens tomorrow, something else later).

## 2. Why node IDs hide behind mappings

`PtzMapping.resolve(LogicalField, ptz_id) -> PtzNodeDescriptor` is the
only place node syntax (`ns=2;s=PTZ_01.TargetPan`) exists. Controller and
(future) UI code handle `LogicalField` + `ptz_id` only. Availability is
explicit (`available_fields`) instead of assuming every field exists in
the real PLC.

## 3. Simulator mapping vs Siemens mapping

`SimulatorPtzMapping` uses the proposed development namespace
`urn:tms:ptz:sim` with deterministic `<ptz_id>.<Name>` nodes. These are
NOT Siemens tags and are never presented as such. `SiemensPtzMapping` is
a placeholder: every `resolve` raises `PtzMappingError("Siemens PTZ
mapping is not configured ...")` until real PLC documentation arrives.
Swapping them is a configuration choice, not a code change.

## 4. One vs multiple sessions

`OpcUaSession` is per-endpoint, shared by all PTZ instances on that
endpoint (`ptz_id` selects the tag set; PTZ_01..PTZ_08 verified without
per-instance clients). A future multi-PLC deployment instantiates one
session per endpoint with the same class.

## 5. Why UI never calls OPC UA directly

Every transport call has a bounded timeout and reconnection runs on a
private daemon thread; callbacks are plain callables (no Qt signals) so
the package imports and tests headless. The future Qt host will be a
thin QObject wrapper around `OpcUaSession`, following the proven
FocusWorker/NucWorker retire pattern (retained refs, no destroy-while-
running, asynchronous cleanup behind mode transitions).

## 6. Connection/reconnection lifecycle

`DISCONNECTED -> CONNECTING -> CONNECTED`, loss ->
`COMMUNICATION_LOST -> RECONNECTING -> CONNECTED`, exhaustion -> `ERROR`
(all Phase 2 `PlcConnectionState`, unchanged). Bounded attempts with
capped exponential backoff; no tight loops. On reconnect the session
restores subscriptions and reports readiness; the controller (later)
re-reads authoritative state.

## 7. Error translation

`translate_error`: timeout -> COMMUNICATION, node failure -> PLC,
mapping failure -> UNKNOWN, invalid value -> COMMAND_REJECTED, anything
else -> UNKNOWN. Raw library exceptions never reach the UI as the
application contract; `PtzError` goes to `on_error`.

## 8. Subscription strategy

Monitored items for fast state (`actual_pan/tilt`, `moving`,
`position_reached`, `ready`, `error`, calibration bits); commands and
infrequent reads stay request/response. Server writes mean "accepted by
the server" only -- position-reached authority always comes from later
status reads via `within_tolerance`. High-frequency values are logged at
DEBUG, never INFO.

## 9. Currently unknown hardware information

Endpoint, namespace, node IDs, datatypes, DB offsets, command/strobe
handshake, movement and position-reached semantics, velocity mode,
limits, tolerance, error codes, calibration sequence, per-PTZ
addressing. Nothing in `mapping.py`/`client.py` guesses them.

## 10. What changes when Siemens documentation arrives

Only: a configured `SiemensPtzMapping` tag table (+ endpoint/security
config). `PtzCommand`, `PtzStatus`, `OpcUaSession`, and the future UI
remain unchanged.

## 11. Dependency decision

`asyncua>=1.0` (single library; legacy `opcua`/python-opcua rejected as
unmaintained), declared as the optional `ptz` extra so base installs and
headless tests need nothing new. `AsyncuaTransport` imports it lazily;
`import thermal_monitor.ptz` requires neither asyncua nor PyQt6
(verified by test). Security modes are modelled (`ANONYMOUS/USERNAME/
CERTIFICATE`) with anonymous for the simulator; certificate
infrastructure is deferred to the production phase.
