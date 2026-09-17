# ADR-011: PLC & PTZ Live Monitoring Window (Phase 9A)

## Status

Accepted. Simulator-only. No Siemens compatibility claimed.

## Context

Operators need to watch PLC communication health and all eight PTZ
units in real time without disturbing Live, Configuration, or Offline
modes. All prior PTZ work (models, OPC UA session/service, simulator,
controller, panels) is reusable; the gap was a standalone,
non-commanding observer surface.

## Decision

### Window

New `ui.windows.ptz_monitor_window.PtzMonitorWindow` (QMainWindow):

- PLC summary panel: profile, endpoint, connection, session,
  comm health, last update, reconnects observed, error.
- Per-PTZ table (one row per unit): PTZ ID, camera binding, conn,
  ready, actual pan/tilt, target pan/tilt (read-only node reads),
  movement, calibration, active position (last reached target latched
  by the monitor, `Unknown` until observed), error, updated timestamp.
- Fields the simulator does not expose render as `Unknown`/`Unavailable`;
  values are never fabricated.

### Data architecture

- The window owns a private `PtzService` (own OPC UA session/transport)
  built via `station.build_service` + `SimulatorPtzMapping`; the
  Siemens gate (`validate_profile`) must report ready/simulator or the
  window shows the reason and polls nothing.
- Two minimal read-only service APIs were added (nothing else changed):
  `PtzService.status_for_ptz` (per-PTZ status without a camera binding)
  and `PtzService.read_field` (single mapped node read).
- One QTimer (500 ms) spawns at most one background daemon worker
  (overlap guard); the worker emits one snapshot signal carrying the
  window generation; the slot drops stale generations. No OPC UA on the
  GUI thread; no thread per PTZ.
- Reconnects are observed (comm-health False→True transitions), never
  forced, except a best-effort manual Reconnect button (disconnect +
  connect in a worker).
- Close stops the timer, invalidates pending snapshots, and shuts the
  owned service down in a background thread. Injected (test) services
  are never shut down by the window.

### Launcher / controller integration

- Launcher gains a "PLC & PTZ MONITOR" button + `ptz_monitor_requested`
  signal; `AppController` owns the window exactly like the Offline
  window (lazy create, delete-on-close, destroyed handler, shutdown
  close list). No mutual-exclusion changes: the monitor coexists with
  every mode.

## Simulator integration (verified)

- Startup: `python -m tools.ptz_plc_simulator [--endpoint
  opc.tcp://127.0.0.1:4840] [--ptz-count 8]`
- Endpoint `opc.tcp://127.0.0.1:4840`, namespace `urn:tms:ptz:sim`.
- Available fields: TargetPan/TargetTilt/Velocity/PanVelocity/
  TiltVelocity/Command/CalibrationRequest (write); ActualPan/
  ActualTilt/Moving/PositionReached/Ready/Error/ErrorCode/
  CalibrationRequired/CalibrationActive/CalibrationComplete (read).
- Transitions: idle→moving→reached within tolerance, error latch +
  clear, calibration request→active→complete, `stop_listening()` for
  comm-loss simulation.
- Unsupported: zoom, actual velocity, per-PTZ comm state, wire request
  IDs, active-position registry (config-window concept only).
- Limitations: single localhost server; timing/values are development
  defaults, not machine specs; no Siemens semantics.

## Consequences

- Operators get real-time PLC/PTZ visibility with zero impact on
  camera acquisition or modes.
- The window is strictly read-only: no move/stop/calibrate path exists
  in it, so it cannot disturb running operations.
- Siemens remains blocked behind the unchanged gate.
