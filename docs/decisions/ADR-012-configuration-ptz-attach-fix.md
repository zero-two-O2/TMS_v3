# ADR-012: Configuration Mode PTZ Attach Fix (Phase 9C)

## Status

Accepted. Simulator-only. No Siemens compatibility claimed.

## Context

Phase 9B left Configuration Mode showing `PTZ: No PTZ configured`
(`PLC: —`, `State: —`, all PTZ controls disabled) even though camera
acquisition was streaming and the independent PLC & PTZ Monitor showed
all simulator units connected (`opc.tcp://127.0.0.1:4840`, profile
`simulator`, namespace `urn:tms:ptz:sim`).

## Root causes (all verified in code, one with git forensics)

1. **Discovery persistence clobbered `ptz_id` (primary).**
   `ConfigurationModeWidget._on_camera_selected_from_dialog`
   (`src/thermal_monitor/ui/windows/configuration_window.py`) built the
   `CameraMappingConfig` for `save_camera_mapping()` without any `ptz_*`
   fields, so every camera Connect overwrote the YAML entry's `ptz_id`
   with `""`. Git evidence: `HEAD` had `ptz_id: PTZ_08` for
   `cam_HB25100001`; the working tree (and the app-written `.bak`) had
   `ptz_id: ''`. With a blank `ptz_id`, `station.resolve_binding()`
   returns `None` by contract, `_ptz_attach_async()` returns early, and
   the panel correctly — but misleadingly — renders `No PTZ configured`.
   The monitor was unaffected because it builds its own private service
   from the same YAML independently and never writes the mapping.
2. **Shared-service monitor fan-out could cross-route panels.**
   One cached `PtzService` per endpoint fans monitor callbacks out per
   PTZ controller, but `_on_ptz_status` only guarded
   `(camera_id, generation)` — never `ptz_id`. The last unrelated PTZ
   in the loop could overwrite the selected camera's panel.
3. **Absolute-move mode strings rejected by validation.**
   The panel emits `"single"` / `"per_axis"` strings, but `PtzCommand`
   requires `isinstance(velocity_mode, VelocityMode)`. Uncoerced strings
   fail as `COMMAND_REJECTED` before reaching OPC UA.

## Decision

Minimal fix inside the existing station/service architecture; no new
control path, no direct OPC UA writes from the UI:

- Dialog-save now carries all existing `ptz_*` fields forward
  (`ptz_id`, `ptz_endpoint`, min/max pan/tilt); discovery data can never
  erase a station association again.
- Restored the clobbered `ptz_id: PTZ_08` for `cam_HB25100001` in
  `config/config.yaml` (undoes the bug's damage; no binding invented —
  `cam_HB25100004` stays genuinely unconfigured and renders the
  actionable `No PTZ configured` state).
- `_on_ptz_status` drops deliveries whose `ptz_id` differs from the
  selected camera's binding.
- `_on_ptz_move_requested` coerces the mode string to `VelocityMode`.
- `_on_start_acquisition` re-invokes `_ptz_attach_async()` after
  `ACQUIRING` (idempotent: shared service reused, identical binding
  registration is a no-op, monitor starts exactly once), so acquisition
  — the operator-visible "connected" state — always triggers PTZ attach.
- Unconfigured cameras get an explicit panel + top-chip message naming
  the camera and the `cameras.mapping` entry to fix.

Command flow (unchanged, now actually reachable):

```text
Configuration UI → PtzService → PtzController → OpcUaSession
    → OPC UA transport → Simulator PTZ
```

## Control-enable conditions (final)

Buttons follow `PtzStatus.accepts_commands` plus the current operation
(`STOP` only while moving, `Clear Error` only with an error,
`Calibrate` only when connected/available/healthy/error-free/not
already calibrating). Disabled controls carry the reason in the status
or error label (`No PTZ configured`, `Connecting…`, `PTZ disconnected`,
`Communication unavailable`, `PTZ is moving`, `Calibrating`,
`PTZ error: ...`).

## Verification

- New `tests/test_phase9c_config_ptz_fix.py`: mapping round-trip keeps
  `ptz_id`, wrong-PTZ monitor delivery dropped, unconfigured camera
  message, `VelocityMode` coercion — all pass.
- Targeted: 102 passed (phase9b binding + sqlite alarm + phase9c +
  ptz service/controller/state).
- Simulator integration: 56 passed
  (`test_ptz_service_integration`, `test_ptz_simulator`,
  `test_ptz_simulator_opcua`).
- Live simulator E2E (manual, same service chain as the UI):
  `connect → ready=True/comm_ok=True → move_absolute(20, 10) →
  REACHED → actual 20.0/10.0 → stop → clear_error → shutdown` — PASS.
- `python -m compileall src tools` — clean.
- Full suite: not run to completion in one shot (1599 tests, exceeds
  the 10-minute tool timeout); affected groups run in splits above.
  One pre-existing unrelated failure observed:
  `test_config_polish.py::test_shelf_size_central_constants`
  (`PANEL_SHELF_WIDTH` 25 vs asserted 36–48; shelf geometry, untouched
  by this change).

## Limitations

- GUI manual acceptance table (camera switching, disconnect/reconnect,
  simulator restart, monitor regression while moving, responsiveness
  under load) was NOT executed end-to-end; only the headless service
  E2E above plus Qt-offscreen unit tests.
- Siemens PLC compatibility remains unverified (simulator profile
  only; Siemens gate untouched and still blocked).
- `cam_HB25100004` (and any entry with blank `ptz_id`) intentionally
  shows `No PTZ configured` — correct behavior, not a defect.
