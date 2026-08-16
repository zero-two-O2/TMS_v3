# V2 Camera Acquisition — Technical Recovery Report

## Purpose

Recover the technical knowledge required to implement the V3 camera acquisition subsystem from the V2 reference repository (`reference/TMS_v2/`).

This report is read-only evidence. It does not design V3, copy code, or modify V3 source. V2 file paths are relative to `reference/TMS_v2/`.

Legend for the recommendation column:

- **KEEP** — proven behavior/parameter that V3 should preserve as-is.
- **REDESIGN** — proven concept that V3 must restructure to its architecture.
- **REWRITE** — concept worth keeping but the implementation is unreliable or incomplete.
- **DISCARD** — obsolete, duplicate, dead, or broken code.

---

## 1. Which V2 camera implementation is actually used

### 1.1 Startup trace (production path)

| Step | Evidence |
| --- | --- |
| Entry point | `main.py:5` creates `Application`; `main.py:9` runs it |
| App bootstrap | `app/application.py:189` `Application.initialize()` → `:193` `_discover_and_register_cameras()` |
| Discovery | `app/application.py:200` `ha.info_framegrabber("GigEVision2", "info_boards")`, inline board parsing, `camera_id = cam_{serial}` (`:233`) |
| Factory | `app/application.py:259` `factory.create_camera(model)` |
| Camera manager | `app/application_controller.py:24` creates `CameraManager`; `:190` `add_camera()` |
| Wiring | `camera/factory/camera_factory.py:56` builds **`camera.services.tv46l_camera.TV46LCamera`** |
| Services | `camera/services/tv46l_camera.py:27` `TV46LCamera` → `:63` `HalconDriver` + `:65` `AcquisitionEngine` |

**Conclusion:** the production acquisition implementation is the `camera/services/` set:
`camera/services/tv46l_camera.py`, `camera/services/halcon_driver.py`, `camera/services/acquisition_engine.py`.

### 1.2 What each acquisition implementation does and where it is used

| Implementation | File(s) | Used by | Status |
| --- | --- | --- | --- |
| `HalconDriver` + `AcquisitionEngine` + `TV46LCamera` (services) | `camera/services/*.py` | Production app via `CameraFactory`; validation harness `tests/validation/camera_bootstrap.py`; `halcon_roi_validation.py:1051` (wraps `HalconDriver` around an existing framegrabber for focus control) | **Production path** |
| `TV46LCamera` (top-level, monolithic) | `camera/tv46l_camera.py` | Only diagnostics/tests: `tests/camera_viewer.py:65`, `tests/diagnose_focus.py:28`, `tests/diagnose_fps.py:24`, `tests/diagnose_pipeline.py`, root `test_tv46l_camera.py:22`; `camera/camera_discovery.py:10` imports only its `_scalar` helper | **Obsolete / duplicate** |
| `CameraDiscovery` | `camera/camera_discovery.py:38` | `halcon_camera_diagnosis.py:2091`, `halcon_roi_validation.py:55`. The app does its own inline discovery | **Duplicate of app discovery** |
| `CameraRuntime` + worker thread (4-camera validation app) | `halcon_roi_validation.py:560` | Standalone tool (`halcon_roi_validation.py` `main`) | **Separate hardware-validated loop** |
| `HalconAcquisition` + strategy classes + `CameraWorker` | `halcon_camera_diagnosis.py:269`, `:605`-`:862`, `:1016` | Standalone diagnostic tool | **Experimental diagnostic** |
| `CameraInterface` ABC | `camera/interfaces/camera_interface.py:23` | No imports anywhere (only exported from `camera/interfaces/__init__.py`) | **DISCARD** |

### 1.3 The two frame contracts diverge (critical V2 finding)

- Top-level `TV46LCamera._grab_raw_frame` (`camera/tv46l_camera.py:388`) returns a **`RawFrame`** (`processing/models/processing_models.py:34`) with `image`, `range_index`, `timestamp`, `frame_number`, `acquisition_timestamp`, `grab_start_time`, `grab_complete_time`, `numpy_complete_time`, `publish_time`, `sequence`.
- Services `TV46LCamera.get_frame()` (`camera/services/tv46l_camera.py:148`) returns a **plain `np.ndarray`** from `AcquisitionEngine.get_latest_frame()` (`camera/services/acquisition_engine.py:235`). No timestamp, no sequence number, no camera id, no timing metadata.

The production path therefore has **no frame identity** (no sequence numbers) and cannot detect dropped frames or correlate IR/visible. This directly conflicts with V3 ADR-002's frame model. The top-level `RawFrame` shape is the closest V2 has to the V3 contract — but it is not the production path.

---

## 2. Answers to the 20 investigation questions

### Q1. Which camera implementation is actually used

The `camera/services/` set (see 1.1/1.2). Assembled only by `CameraFactory`, never constructed directly in the app.

### Q2. Which implementation is obsolete / duplicate

- `camera/tv46l_camera.py` (top-level) — superseded by services; only diagnostics/tests import it.
- `camera/camera_discovery.py` — app inlines its own discovery.
- `camera/interfaces/camera_interface.py` — dead interface.
- `halcon_roi_validation.py` and `halcon_camera_diagnosis.py` are not part of the app; they are standalone tools but contain the most hardware knowledge (see Q18).

### Q3. HALCON acquisition interface / operators

- Interface: **`"GigEVision2"`** (GigE Vision HALCON interface), Python module `halcon`.
- Operators: `open_framegrabber`, `set_framegrabber_param`, `get_framegrabber_param`, `grab_image_start`, `grab_image_async`, `himage_as_numpy_array`, `close_framegrabber`, `info_framegrabber`.
- Grab-timeout detection uses HALCON error code **5322** (`halcon_roi_validation.py:143`, `halcon_camera_diagnosis.py:63`, helper `_is_grab_timeout` `halcon_roi_validation.py:1408`).
- Evidence files: `camera/services/halcon_driver.py`, `camera/tv46l_camera.py`, `halcon_roi_validation.py:1045`, `halcon_camera_diagnosis.py:352`.

### Q4. `open_framegrabber` parameters

Identical call in every implementation:

```python
ha.open_framegrabber(
    "GigEVision2", 0, 0, 0, 0, 0, 0,
    "progressive", -1, "default", -1, "false",
    "default", <device>, 0, -1,
)
```

- `camera/services/halcon_driver.py:89` uses `self.camera.device_identifier`.
- `camera/tv46l_camera.py:173` uses `self._info.device`.
- `halcon_roi_validation.py:1045` and `halcon_camera_diagnosis.py:352` use the HALCON discovery device string.
- `halcon_camera_diagnosis.py:337` `_visible_device_candidates()`: a second connection uses the **IP address** because "HALCON will not re-open the already-in-use discovery device ID".

### Q5. `grab_image_async` behavior

- Services driver `HalconDriver.grab_frame()` (`camera/services/halcon_driver.py:308`): `ha.grab_image_async(fg, 200)` — **200 ms timeout**; any exception is caught and `None` returned.
- Top-level `TV46LCamera._grab_raw_frame()` (`camera/tv46l_camera.py:388`): `ha.grab_image_async(acq, 200)` — same 200 ms; exceptions re-raised and counted as timeouts.
- Validation loop `halcon_roi_validation.py:1594`: `ha.grab_image_async(self._framegrabber, GRAB_TIMEOUT_MS)` where `GRAB_TIMEOUT_MS = 500` (`:141`); explicit error-code 5322 handling; `CONSECUTIVE_FAIL_LIMIT = 3` (`:145`).
- Diagnosis loops use **non-blocking 0 ms** grabs and a blocking first-frame grab with `FIRST_FRAME_TIMEOUT_MS = 5000` (`halcon_camera_diagnosis.py:65`, `:406`-`:412`).
- Streaming is armed with `ha.grab_image_start(fg, -1)` (`camera/services/halcon_driver.py:128`, `camera/tv46l_camera.py:281`, `halcon_roi_validation.py:1061`).

### Q6. Camera initialization sequence

Services path (`camera/services/tv46l_camera.py:77` `connect()` → `camera/services/halcon_driver.py:71` `connect()`):

1. `open_framegrabber` (device string)
2. `_configure_camera()` (`halcon_driver.py:169`)
3. `grab_image_start(fg, -1)` (streaming begins)

Then separately `TV46LCamera.start()` (`camera/services/tv46l_camera.py:118`) starts the `AcquisitionEngine` thread. **Note:** the GUI "Connect" action (`gui/main_window.py:499` → `controller.connect_all()`) never calls `start_all()`; the acquisition thread is never started by the production GUI (see Q9 / limitations).

Validation path `CameraRuntime.initialize()` (`halcon_roi_validation.py:1042`):

1. `open_framegrabber`
2. `_configure_camera()`
3. `grab_image_start`
4. blocking first-frame grab (`5000 ms`)
5. load calibration, load ROIs, `_apply_default_focus()`
6. set `_startup_nuc_pending = True` (one-time startup NUC on first valid frame)

### Q7. Camera configuration parameters

| Parameter | Value | Where |
| --- | --- | --- |
| `FLK_TI_StreamDataSourceSelector` | `"IR_Data"` (thermal) / `"VL_Data"` (visible) | `halcon_driver.py:178-181`, `tv46l_camera.py:202-206`, `halcon_roi_validation.py:1101`, `halcon_camera_diagnosis.py:357,434` |
| `bits_per_channel` | `16` (IR), `-1` (visible) | `halcon_driver.py:183`, `halcon_camera_diagnosis.py:360,435` |
| `[Stream]DeviceStreamChannelNegotiatePacketSize` | `1` | `halcon_driver.py:189`, `tv46l_camera.py:219`, `halcon_roi_validation.py:1105`, `halcon_camera_diagnosis.py:370` |
| `[Stream]GevStreamReceiveSocketSize` | `512000` (services) / `1048576` (top-level, validation, diagnosis) | `halcon_driver.py:204`, `tv46l_camera.py:231`, `halcon_roi_validation.py:1110`, `halcon_camera_diagnosis.py:375` |
| `num_buffers` | `8` (validation + diagnosis only; **not set** in services/top-level) | `halcon_roi_validation.py:1117`, `halcon_camera_diagnosis.py:380` |
| `FLK_TI_ControlFeature_SetFrameRate` | `9` (config `camera.fps`, `config.json:3`) | `halcon_driver.py:214`, `tv46l_camera.py:244`, `halcon_roi_validation.py:1125` |
| `FLK_TI_ControlFeature_REControlCmd` | `FLK_TI_ControlFeature_REControlCmd_DisableAutomaticFineOffsets` | `halcon_driver.py:224`, `tv46l_camera.py:263`, `halcon_roi_validation.py:1133` |
| Focus read / write / limits | `FLK_TI_ControlFeature_CurrentFocusDistanceMm` / `_SetFocusDistanceMm` / `_FocusDistanceMm_Min/_Max` | `halcon_driver.py:47-48,382-425` |
| Device temperature | `FLK_TI_Info_CurrentDeviceTemperatureC`, `FLK_TI_Info_CriticalDeviceTemperatureC` | `camera/tv46l_camera.py:903-919` |
| Firmware | `[Device]DeviceVersion` | `camera/tv46l_camera.py:925-930` |
| Discovery reads | `[Device]DeviceSerialNumber`, `[Device]DeviceModelName`, `[Device]DeviceVendorName`, `[Device]GevDeviceIPAddress`, `[Device]DeviceUserID` | `camera/camera_discovery.py:134-157` |

### Q8. Camera connection / reconnection behavior

- **Services path: no reconnection logic at all.** `HalconDriver.connect()`/`disconnect()` (`halcon_driver.py:71,141`) only. A failed `grab_image_async` returns `None` and the engine loop just sleeps 0.1 s (`acquisition_engine.py:149-155`). Permanent stall is silently stuck.
- **Top-level `TV46LCamera.reconnect()`** (`camera/tv46l_camera.py:724`): `disconnect()` → sleep `RECONNECT_INTERVAL = 5.0` (`configuration/settings.py:18`) → `connect()` → `start()`. **No callers** — dead code. `stream_healthy()` (`:745`)/`is_alive()` (`:625`) used only by `tests/camera_viewer.py`.
- **Validation loop (hardware-proven recovery):** `_handle_grab_timeout()` (`halcon_roi_validation.py:1537`) counts consecutive timeouts; after `CONSECUTIVE_FAIL_LIMIT = 3` it calls `_reassign_framegrabber()` (`:1452`) which **closes and reopens only the framegrabber**, reuses calibration/LUT/ROI state, re-applies config, re-arms, and requires a valid first frame. When `_connected` is False the loop calls `_attempt_reconnect()` (`:1945`) every `reconnect_seconds = 3` (default `halcon_roi_validation.py:627`).
- **Diagnosis worker:** `_reopen_framegrabber()` (`halcon_camera_diagnosis.py:1365`) fires when no IR frame arrives for `WEDGE_RECOVERY_S = 3.0` (`:69`).

### Q9. Acquisition thread behavior

- **Services** `AcquisitionEngine.start()` (`camera/services/acquisition_engine.py:81`): one daemon `threading.Thread` named `"AcquisitionEngine"` (identical name for every camera). `_acquisition_loop()` (`:127`) loops `while self._running`: `driver.grab_frame()` (blocking ≤200 ms) → `_push_frame()` → `_update_statistics()`; on exception log + `time.sleep(0.1)`. `stop()` (`:104`) sets flag and `join(timeout=2.0)`.
- **Top-level** `TV46LCamera._grab_loop()` (`camera/tv46l_camera.py:307`): daemon thread named `"TV46L-{serial}"`; per-second `[DIAG]` prints of grab/conversion timings (measurement scaffolding left in code); manual NUC executed inline; grabs via `_grab_raw_frame()`.
- **Validation** worker runs in a `QThread` per camera; `run()` (`halcon_roi_validation.py:1567`) grabs (500 ms), converts, processes, emits; `stop()` (`:1961`) only flips `_running`; cleanup happens in the worker thread (`_cleanup` `:1764`) so `close_framegrabber` never races an in-flight `grab_image_async`.
- **Diagnosis** `CameraWorker` (QThread, `halcon_camera_diagnosis.py:1016`) per camera; strategy loop; wedge recovery; per-second metrics.

### Q10. Queue / buffer behavior

- **Services:** `queue.Queue(maxsize=2)` (`acquisition_engine.py:39,49`). `_push_frame()` (`:165`): `put_nowait`; on `Full`, drop the **oldest** (`get_nowait`), `put_nowait` the new one, increment `_dropped_frames` (`:196`). `get_latest_frame()` (`:235`) drains the queue and returns the newest. `get_frame(timeout)` (`:198`) blocks with timeout.
- **Top-level:** single latest-wins slot `_latest_frame` under a `threading.Lock` (`camera/tv46l_camera.py:364-370`); consumers get a defensive copy (`_copy_frame` `:518`).
- **Validation:** 2-second ring buffer `deque(maxlen=FRAME_BUFFER_SIZE=30)` of `FrameBufferEntry` for alarm snapshots (`halcon_roi_validation.py:68,1007,1685-1695`).
- **Diagnosis:** latest-wins display copies per stream; HALCON internal `num_buffers=8` (`halcon_camera_diagnosis.py:380`).

### Q11. Frame-drop behavior

- **Services:** `dropped_frames` counter when the size-2 queue overflows (drop-oldest). No sequence numbers exist, so gaps cannot be detected from frame identity.
- **Top-level:** `timeout_count` increments on grab failure; `is_alive()` returns False if no frame for 2 s (`camera/tv46l_camera.py:625-634`).
- **Validation:** consecutive-timeout counting (`_consecutive_failures`), GigE packet counters (`_read_stream_stats` `halcon_roi_validation.py:1358`): `[Stream]GevStreamLostPacketCount`, `GevStreamSeenPacketCount`, `GevStreamDeliveredPacketCount`, `GevStreamResendPacketCount` (with fallback candidate names without the `[Stream]` prefix).

### Q12. FPS handling

- Camera frame rate is **commanded to 9 FPS** via `FLK_TI_ControlFeature_SetFrameRate` (Q7); the model name even embeds it (`TV46L-1-26010003@9Hz`, `test_tv46l_camera.py:40`). At 9 FPS a frame interval is ~111 ms (comments at `halcon_roi_validation.py:139` and `halcon_camera_diagnosis.py:66`).
- Measured FPS is computed in software:
  - Services: `_update_statistics()` (`acquisition_engine.py:262`) — `fps = counter / elapsed` over a 1 s `perf_counter` window.
  - Top-level: `_update_fps()` (`camera/tv46l_camera.py:592`) — frames counted over 1 s `time.time()` window.
  - Validation: frames/elapsed sampled once per second (`halcon_roi_validation.py:1717-1729`).
  - Diagnosis: rolling `FpsMeter` over a 2 s window using `perf_counter` (`halcon_camera_diagnosis.py:209`).

### Q13. Camera identity handling

- `camera_id = f"cam_{serial}"` (fallback `cam_{device}`) (`app/application.py:233-235`).
- `CameraModel` (`camera/models/camera_model.py:31`): `camera_id`, `camera_name`, `serial_number`, `model`, `vendor`, `ip_address`, `mac_address`, `device_identifier`, `interface_name`, capability flags (`supports_focus`, `supports_nuc`, `supports_visible_stream`).
- `CameraInfo` (`camera/camera_info.py:18`): `device`, `serial`, `model`, `vendor`, `ip`, `firmware`, `user_name`.
- Real device example: device `3408e1d8dbbe_FlukeProcessInstruments_TV46L1260100039Hz`, serial `HB25080011`, model `TV46L-1-26010003@9Hz`, vendor `Fluke Process Instruments`, IP `169.254.91.157` (`test_tv46l_camera.py:37-43`).
- `CameraManager.MAX_CAMERAS = 8` (`camera/manager/camera_manager.py:24`); `add_camera` keys on `camera_id`, rejects duplicates (`:50`), disconnects on remove (`:76`).

### Q14. Camera shutdown behavior

- **Services:** `TV46LCamera.disconnect()` (`camera/services/tv46l_camera.py:96`): `stop()` (engine: flag + `join(2.0)` + clear queue, `acquisition_engine.py:104-121`) then `HalconDriver.disconnect()` → `close_framegrabber` (`halcon_driver.py:141-163`).
- **Validation:** `stop()` only flips `_running`; `close_framegrabber` happens in the worker thread after the loop returns (`halcon_roi_validation.py:1764-1786`), explicitly avoiding closing while a grab may be in flight.
- **Top-level:** `stop()` `join(2.0)` (`camera/tv46l_camera.py:605-619`), `disconnect()` closes the framegrabber (`:147`); `__del__` also calls `disconnect()` (`:1128`).

### Q15. Camera errors and recovery

- **Services:** `HalconDriver.grab_frame()` swallows all exceptions → `None` (`halcon_driver.py:328-334`); engine logs and sleeps 0.1 s. **No reconnect, no recovery ladder.**
- **Validation (proven):** 5322 timeout detection; 3 consecutive timeouts → framegrabber close+reopen; NUC-aware timeout handling (`_handle_nuc_wait_timeout` `halcon_roi_validation.py:1500` — during NUC the camera deliberately stops producing frames, timeouts are expected and never count as failures; escalate to the normal ladder only after `grab_timeout_before_reconnect_seconds = 8`).
- **Manual NUC sequence** (`perform_nuc` in `halcon_driver.py:340`, `_execute_nuc` in `halcon_roi_validation.py:1792`): `REControlCmd = RequestFineOffset` → `ExecuteFineOffset` → `time.sleep(0.05)` → 3 flush grabs (`grab_image_async(fg, 0)`).
- **Diagnosis:** wedge-reopen after 3 s of no IR frame; dual-stream conflict detection (`_ingest` `halcon_camera_diagnosis.py:1417-1433`: if IR byte size changes, the visible handle has flipped the shared source → close visible handle, re-select IR).

### Q16. Thermal stream behavior

- `FLK_TI_StreamDataSourceSelector = "IR_Data"`, `bits_per_channel = 16` → 16-bit mono thermal data.
- Feed dimensions constants `FEED_W = 640`, `FEED_H = 480` (`halcon_roi_validation.py:65-66`); the diagnosis tool reads the actual `image_width`/`image_height`/`pixel_format` from the framegrabber (`halcon_camera_diagnosis.py:466-484`).
- Display normalization (`_normalize_to_uint8`, `halcon_camera_diagnosis.py:884`) explicitly never mutates raw data.

### Q17. Visible stream behavior (if present)

- `FLK_TI_StreamDataSourceSelector = "VL_Data"` (`halcon_camera_diagnosis.py:178`); `bits_per_channel = -1` (`:360,435`).
- Delivery is hardware-dependent: **RGB8 640×480×3 (921600 B/f)** on this camera, or **packed YUV422 (YUYV)** on other builds (`halcon_camera_diagnosis.py:906-950`, `_visible_to_display` `:937`).
- Only the diagnostic tool implements visible acquisition. Production/services and validation tools are IR-only.

### Q18. Can IR and visible be acquired simultaneously?

**Not with a single stream channel.** The diagnosis tool's own documentation states (`halcon_camera_diagnosis.py:269-286`):

- HALCON's `GigEVision2` interface opens only **one stream channel per camera**.
- The TV46L is a **single-stream camera** (`DeviceStreamChannelCount = 1`); the camera-global `FLK_TI_StreamDataSourceSelector` picks IR or visible for that channel.
- **Dual mode (experimental):** a second GigE connection opened via the **IP address** may deliver a second concurrent stream at native rate. If the camera rejects it, or if the second handle echoes the IR source (byte-size comparison detects this, `_verify_visible_stream` `:1182`), it falls back to time slicing.
- **Time slicing (fallback):** switching the shared source costs "roughly a second" (`:284-285`); per-frame switching previously capped the loop at ~0.8 FPS. `TimeSlicedStrategy` (`:717`) holds IR for 2.0 s then visible for 0.6 s.

These are **experimental hardware findings from a diagnostic tool**, not proven production behavior. Simultaneous IR+visible must be re-verified on the real TV46L.

### Q19. Hardware assumptions

- TV46L is a GigE Vision single-stream camera using HALCON `GigEVision2`.
- Direct link, link-local IP (e.g. `169.254.91.157`).
- Thermal is 16-bit mono, ~640×480, 9 FPS (settable via FLK feature; model string `@9Hz`).
- Focus motor present; `FOCUS_UNAVAILABLE_MM = 1000000.0` sentinel means "focus unavailable" (`camera/tv46l_camera.py:802`); default focus applied once after connect in validation (`halcon_roi_validation.py:1082,1920-1943`).
- During NUC the camera stops producing frames (validation treats timeouts as expected).
- Auto-NUC interval: `nuc.interval_seconds = 400` in `config.json:15` (default `300` in `halcon_roi_validation.py:637`); `nuc_duration_seconds = 2.5` (`:628`).
- Startup NUC once after first connection, never on reconnect (`halcon_roi_validation.py:968-970,1085-1088,1623-1631`).
- Up to 8 cameras (`CameraManager.MAX_CAMERAS`, `configuration/settings.py:15`); validation tool was built for 4 (`CAMERA_COUNT = 4`, `halcon_roi_validation.py:591`).
- A second connection to an in-use discovery device string is refused; use the IP address (`halcon_camera_diagnosis.py:337-348`).

### Q20. Known limitations

1. **Production GUI never starts the acquisition engine.** `gui/main_window.py:499` Connect → `controller.connect_all()` only connects; `start_all()` exists (`app/application_controller.py:143`) but has **no GUI caller**. Frame delivery through the services path never starts in the normal app flow; the observation/calibration/detail windows poll an empty queue forever (`gui/observer/observer_window.py:287`, `gui/camera_detail_window.py:240`, `gui/calibration/calibration_window.py:415`).
2. **Production frame contract is a bare `np.ndarray`** — no sequence, timestamp, or identity → cannot detect drops, cannot correlate streams. Conflicts with ADR-002.
3. **No reconnection in the production path**; a stalled camera stays stalled.
4. **Dead/broken API:** `ApplicationController.process_frame()` calls `context.get_latest_frame()` / `context.process_latest_frame()` (`app/application_controller.py:438-444`) which do not exist on `CameraContext` (`camera/models/camera_context.py`).
5. **Broken validation harness call:** `tests/validation/camera_bootstrap.py:155` reads `context.calibration_manager`, which is not a `CameraContext` field.
6. **Inconsistent transport tuning:** services driver uses socket size `512000` and omits `num_buffers=8`, while the proven validation path uses `1048576` + `num_buffers=8` (Q7).
7. **Identical thread name `"AcquisitionEngine"`** for all cameras (`acquisition_engine.py:98`) — poor multi-camera diagnostics.
8. **Root test `test_camera.py:80-83`** passes `name=` but `CameraModel` uses `camera_name` → the test would crash; not a real test of acquisition.
9. **`CameraFrameSource.next_frame`** (`tests/validation/frame_source.py:44`) never updates `_last_frame_number` and always returns `frame_number=0`.
10. **Duplicate implementations** (top-level vs services) have diverged (socket size, num_buffers, reconnect, frame model).
11. **Timeout difference:** services/top-level use 200 ms; proven validation uses 500 ms (better tolerance at 9 FPS / 111 ms interval).
12. Top-level `_grab_loop` (`camera/tv46l_camera.py:307`) prints `[DIAG]` per second — diagnostic scaffolding left in driver code.

---

## 3. Evidence table (file → function → decision)

| Finding | V2 file path | Class / function | Used | Tested | Hardware-verified | V3 recommendation |
| --- | --- | --- | --- | --- | --- | --- |
| Production factory wiring | `camera/factory/camera_factory.py:33` | `CameraFactory.create_camera()` | Yes (app) | Partial (`test_camera.py` is broken) | Via validation harness only | REDESIGN (V3 owns construction) |
| Services camera facade | `camera/services/tv46l_camera.py:27` | `TV46LCamera` | Yes (app) | Partial | Via validation harness | REWRITE (facade with proper frame contract) |
| HALCON low-level driver | `camera/services/halcon_driver.py:31` | `HalconDriver` | Yes (app) | No pytest | Partial (validation uses its config) | KEEP open/configure/grab parameter knowledge; REWRITE code |
| Acquisition thread | `camera/services/acquisition_engine.py:28` | `AcquisitionEngine` | Yes (app) | No pytest | Partial | REWRITE (no sequence, no reconnect, thread never started by GUI) |
| Monolithic driver | `camera/tv46l_camera.py:39` | `TV46LCamera` | Diagnostics only | `test_tv46l_camera.py` (root) | Yes (real serial/IP embedded) | KEEP the `RawFrame` metadata/timing concept; DISCARD driver itself |
| Discovery class | `camera/camera_discovery.py:38` | `CameraDiscovery` | Diagnosis tools only | No | Partial | KEEP device-parsing/retry knowledge; DISCARD class duplication |
| App discovery | `app/application.py:193` | `_discover_and_register_cameras` | Yes (app) | No | Partial | REDESIGN |
| Camera manager | `camera/manager/camera_manager.py:19` | `CameraManager` | Yes (app) | `test_camera.py` (broken) | No | REDESIGN |
| Camera model / info | `camera/models/camera_model.py:31`, `camera/camera_info.py:18` | `CameraModel`, `CameraInfo` | Yes (app) | No | No | KEEP identity fields |
| Identity: `cam_{serial}` | `app/application.py:233` | `camera_id` | Yes | — | Yes (serial HB25080011) | KEEP (matches ADR-002 serial-based identity) |
| open_framegrabber args | `camera/services/halcon_driver.py:89` | `connect()` | Yes | — | Yes | KEEP |
| Stream config IR | `camera/services/halcon_driver.py:169` | `_configure_camera()` | Yes | — | Yes (IR_Data 16-bit) | KEEP parameter set |
| Stream config (validation) | `halcon_roi_validation.py:1099` | `_configure_camera()` | Validation tool | — | Yes (proven baseline) | KEEP (adds num_buffers=8, socket 1048576, fps) |
| Grab timeout & recovery ladder | `halcon_roi_validation.py:1537,1452,1945` | `_handle_grab_timeout`, `_reassign_framegrabber`, `_attempt_reconnect` | Validation tool | No pytest | Yes | KEEP behavior; REDESIGN into V3 recovery policy |
| NUC sequence | `camera/services/halcon_driver.py:340` | `perform_nuc()` | Yes | No pytest | Yes | KEEP (RequestFineOffset→ExecuteFineOffset→flush) |
| NUC-aware timeouts | `halcon_roi_validation.py:1500,1792` | `_handle_nuc_wait_timeout`, `_execute_nuc` | Validation tool | No pytest | Yes | KEEP |
| Focus control | `camera/services/halcon_driver.py:382-449` | `get/set_focus_distance`, `get_focus_limits`, `wait_for_focus` | Yes | `tests/test_focus_control.py` | Yes | KEEP parameter names & sentinel |
| Visible stream (experimental) | `halcon_camera_diagnosis.py:269` | `HalconAcquisition` | Diagnostic tool | No pytest | Experimental | REWRITE after hardware verification |
| Visible decode | `halcon_camera_diagnosis.py:906,937` | `_decode_yuv422_to_rgb`, `_visible_to_display` | Diagnostic tool | No pytest | Experimental | KEEP as reference; verify format on hardware |
| Stream stats counters | `halcon_roi_validation.py:1358` | `_read_stream_stats()` | Validation tool | No pytest | Yes | KEEP parameter names |
| `camera/interfaces/camera_interface.py:23` | `CameraInterface` | — | No | No | No | DISCARD |

---

## 4. Information required before implementing V3 acquisition

The following cannot be determined from V2 source and **must be verified on the real TV46L hardware**:

1. **Thermal frame geometry/format:** exact width × height, pixel format name, byte layout, and bit depth reported by `image_width`/`image_height`/`pixel_format`/`bits_per_channel`. (V2 only hardcodes 640×480 / 16-bit assumptions.)
2. **Visible stream format:** RGB8 vs packed YUV422 (YUYV), resolution, and byte size of `VL_Data`. V2 has both paths; only hardware says which applies.
3. **Simultaneous IR+visible feasibility:** does the camera accept a second concurrent GigE connection (dual handle) and deliver two independent streams? If not, is time-sliced switching acceptable, and what is the true source-switch latency (V2 estimates ~1 s)? Does the second handle echo the IR source?
4. **True native visible frame rate** vs the 9 FPS IR setting (visible may stream at a different native rate).
5. **Frame-rate feature limits:** `FLK_TI_ControlFeature_SetFrameRate` min/max/increment and writability (`frame_rate_capabilities`, `camera/tv46l_camera.py:946`); is 9 FPS optimal?
6. **Grab-timeout semantics:** confirm the timeout error is HALCON code 5322 on the deployed HALCON build; measure a sensible timeout (200 ms vs 500 ms) and consecutive-failure threshold.
7. **Packet-loss behavior:** which GigE counters are exposed on this camera (`[Stream]GevStream*` vs unprefixed), negotiated packet size, interface MTU, and the socket size / `num_buffers` values that prevent loss with multiple concurrent streams.
8. **Reconnection behavior:** what happens on unplug/replug — which error surfaces on `grab_image_async`, does `close_framegrabber` + `open_framegrabber` reliably recover, and is the discovery device string reusable or must the IP be used?
9. **NUC timing:** actual duration of `RequestFineOffset`/`ExecuteFineOffset`, whether frames truly stop during NUC, and the safe retry interval.
10. **Startup NUC:** whether a one-time NUC after connect is genuinely required for stable 16-bit data.
11. **Focus motor:** confirm min/max/settling behavior and that `FOCUS_UNAVAILABLE_MM = 1000000.0` is the real "unavailable" sentinel.
12. **Hardware timestamps:** whether the camera exposes a GigE/PTP timestamp usable as an acquisition timestamp (needed for IR/visible synchronization and ADR-002's timestamp requirement).
13. **Device temperature features:** confirm `FLK_TI_Info_CurrentDeviceTemperatureC` / `CriticalDeviceTemperatureC` exist and their units.
14. **IP/link configuration:** link-local behavior (`169.254.x.x`), static vs DHCP, and whether camera identity truly stays serial-based across IP changes.
15. **Number of supported simultaneous connections** per camera (relevant to 8-camera deployments and dual-handle visible).
16. **Actual memory/time cost of `himage_as_numpy_array` + `.copy()`** per frame at full rate (V2 timed grabs in diagnostics; V3 must size its zero/minimal-copy transport accordingly).
