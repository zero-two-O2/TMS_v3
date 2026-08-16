# V3 Acquisition Subsystem — Technical Note

## Status

Implemented (foundation only). Shared-memory transport is intentionally not
implemented; the frame publisher boundary is designed so shared memory can be
dropped in without rewriting acquisition.

Critical fixes applied in this review:
- `FrameDescriptor.metadata` now uses `MappingProxyType` for true immutability
- `FramePayload` enforces read-only NumPy arrays at construction
- `SyncStatus` uses `UNKNOWN` instead of incorrectly claiming `SYNCHRONIZED`
- Per-stream timestamps preserved (no shared wall-clock timestamp)
- Visible stream acquisition documented as not yet implemented
- `FramePublisher.publish()` returns `PublishResult` (no per-frame `stats()` call)
- Acquisition statistics separated from transport statistics

## 1. Architecture

```
TV46L
  ↓
Camera Driver            (camera/driver.py  — TV46LDriver, HALCON hardware ops)
  ↓
FrameSource protocol     (camera/driver.py  — injectable frame producer)
  ↓
AcquisitionWorker        (camera/acquisition.py — one thread per camera)
  ↓
FramePublisher protocol  (camera/acquisition.py — the future shared-memory boundary)
  ↓
InProcessLatestPublisher (temporary stand-in for development/tests)
  ↓
Processing / Observer / Recorder / Diagnostics (not implemented yet)
```

The acquisition subsystem does not know who consumes its frames. It only
produces immutable `Frame` objects (ADR-002 frame contract) and hands them to
the publisher. It performs no temperature conversion, ROI processing, alarm
evaluation, GUI updates, SQL, video encoding, or drawing.

## 2. Modules created

| Module | Responsibility |
| --- | --- |
| `src/thermal_monitor/core/frame.py` | Shared frame contract: `Frame`, `FrameDescriptor`, `FramePayload`, `StreamMetadata`, `SyncInfo`, `SyncStatus`. Used by all subsystems (ADR-002). |
| `src/thermal_monitor/camera/model.py` | Data models: `CameraIdentity`, `CameraConfig`, `GrabResult`, `AcquisitionState`, `AcquisitionStats`, `PublishResult`. No logic. |
| `src/thermal_monitor/camera/driver.py` | Hardware interaction: `TV46LDriver` (HALCON open/configure/grab/close/NUC), `FrameSource` protocol, grab error types. |
| `src/thermal_monitor/camera/acquisition.py` | Orchestration: `AcquisitionWorker` (lifecycle, sequence, timestamps, FPS, drops, reconnect, shutdown), `FramePublisher` protocol, `InProcessLatestPublisher`. |

The camera domain intentionally has only three modules plus `__init__.py`; no
factory/provider/adapter/manager/services hierarchy was created because no real
responsibility emerged for them.

## 3. Responsibilities

### Driver (`TV46LDriver`)
- Connect / disconnect framegrabber (`open_framegrabber` / `close_framegrabber`).
- Apply camera configuration (V2-proven parameter set).
- Grab frames (`grab_image_async`), convert to NumPy (`himage_as_numpy_array`),
  copy into owned read-only arrays.
- Read/write camera parameters, execute manual NUC.
- Distinguish grab timeouts (HALCON error 5322) from other grab errors.
- **Visible stream note**: TV46L is single-stream; IR and visible are time-sliced
  via `FLK_TI_StreamDataSourceSelector`. Not simultaneously acquired. V3
  implements thermal only; visible support would require separate handle or
  time-sliced acquisition (see `CameraConfig.stream_source_visible`).

### Acquisition worker (`AcquisitionWorker`)
- Runs one thread per camera (independent camera operation).
- Sequence number assignment (per-camera monotonic).
- Wall-clock and monotonic timestamps (per-stream when available).
- FPS measurement (current + average).
- Dropped-frame accounting (acquisition-side only).
- Health state machine (CREATED → CONNECTING → CONNECTED → ACQUIRING → DEGRADED
  → RECONNECTING → STOPPING → STOPPED, plus ERROR).
- Recovery scheduling with exponential backoff.
- Deterministic shutdown.
- **Does not query publisher statistics per frame** — gets `PublishResult`
  directly from `publish()`.

### Frame publisher (`FramePublisher` protocol)
- Publication boundary. A future shared-memory ring buffer will implement this
  protocol.
- `InProcessLatestPublisher` is the temporary in-process implementation
  (latest-wins, drop/overwrite/gap counters).
- **Returns `PublishResult`** with `accepted`, `sequence`, `dropped`,
  `overwritten_sequence` — the boundary for shared-memory replacement.

## 4. V2 knowledge reused

Recovered from `reference/TMS_v2/` (read-only) — evidence details in
`docs/architecture/v2-camera-acquisition.md`:

- Production path was the `camera/services/` set (`HalconDriver` +
  `AcquisitionEngine`), confirmed by the V2 startup trace
  (`app/application.py:189` → `CameraFactory` → `TV46LCamera` → driver + engine).
- `open_framegrabber("GigEVision2", 0,0,0,0,0,0, "progressive", -1, "default",
  -1, "false", "default", <device>, 0, -1)` — reused verbatim.
- Stream selection: `FLK_TI_StreamDataSourceSelector = "IR_Data"`,
  `bits_per_channel = 16`.
- Proven recovery/transport tuning: `num_buffers = 8`, receive socket
  1048576, negotiated packet size, `FLK_TI_ControlFeature_SetFrameRate` (9),
  `DisableAutomaticFineOffsets` (from `halcon_roi_validation.py:1099`).
- `grab_image_start(fg, -1)` to arm streaming; `grab_image_async` with timeout.
- Timeout detection via HALCON error code 5322
  (`halcon_roi_validation.py:143`, helper `_is_grab_timeout`).
- Recovery ladder concept: consecutive-failure threshold → close/reopen only
  the framegrabber (`halcon_roi_validation.py:1537`, `_reassign_framegrabber`),
  reapply config, re-arm, require a valid first frame.
- Manual NUC sequence: RequestFineOffset → ExecuteFineOffset → pause → flush
  (`halcon_driver.py:340`, `halcon_roi_validation.py:1792`).
- Camera identity: `cam_{serial}` (`app/application.py:233`); serial
  HB25080011, model `TV46L-1-26010003@9Hz`.
- `himage_as_numpy_array` may return a buffer backed by HALCON, so a copy is
  required before the raw frame can be treated as immutable (V2 top-level
  `_grab_raw_frame` copied; services path did not — a V2 bug avoided here).

## 5. V2 behavior intentionally not reused

- **Blocking queue transport.** V2 used `queue.Queue(maxsize=2)` with
  drop-oldest backpressure (`acquisition_engine.py:39`). V3 publishes through
  a `FramePublisher` boundary that assumes multiple consumers, latest-frame
  access, independent consumer speeds, and measurable frame loss. No blocking
  consumer queue in the acquisition core.
- **Bare `np.ndarray` frame contract.** V2 services path returned arrays with
  no identity/sequence/timestamp, making drop detection impossible. V3
  publishes the ADR-002 frame contract.
- **No reconnection in the production V2 path.** V2 services had none; V3
  implements the recovery ladder.
- **Identical thread name `"AcquisitionEngine"` for every camera.** V3 names
  threads per camera.
- **`camera/interfaces/camera_interface.py`** (dead ABC) — not carried over.
- **Duplicate monolithic/top-level `TV46LCamera`** and `CameraManager`
  multi-camera orchestration — V3 keeps one worker per camera with no shared
  global state; a manager will only appear when a real multi-camera
  coordination requirement appears.
- **Diagnostic scaffolding / `[DIAG]` per-second prints** left in V2 driver
  code — not carried over.

## 6. Frame publication design

- The worker builds a `Frame` per successful grab:
  `FrameDescriptor` (identity + metadata + payload reference) and
  `FramePayload` (thermal / visible read-only arrays).
- `timestamp` (wall clock) and `monotonic_timestamp` (perf_counter) are both
  set on the descriptor. Hardware timestamps are explicitly `None` because
  the TV46L/GigE timestamp availability is unverified (see §12).
- `StreamMetadata` separates thermal vs visible presence, geometry, format,
  and **per-stream timestamps**; synchronization state is recorded in `SyncInfo`.
- **Sync status**: `UNKNOWN` when both streams present (TV46L time-slices
  IR/visible; not simultaneously acquired). Do not claim `SYNCHRONIZED`.
- Sequence numbers are assigned per acquired frame before publication. The
  publisher detects gaps when a published sequence is not exactly
  `last + 1`.
- Acquisition never mutates published arrays: the driver copies into owned
  read-only arrays (`setflags(write=False)`), and `FramePayload.__post_init__`
  validates `writeable=False` at the contract boundary.
- **Per-stream timestamps**: Thermal gets hardware timestamp if available;
  visible gets application timestamp when acquired. They are NOT the same
  timestamp merely because `time.time()` was called once.

## 7. Shared-memory compatibility

- `FramePublisher` is the only place transport semantics live. The worker is
  transport-agnostic; replacing `InProcessLatestPublisher` with a shared
  memory ring buffer requires no worker changes.
- The frame contract separates metadata from payload explicitly
  (`FrameDescriptor` vs `FramePayload`), matching the future model where the
  descriptor stays in-process and payload bytes live in shared memory.
- Requirements the future transport must satisfy (already assumed by the
  worker): multiple consumers, bounded memory, latest-frame access, frame
  sequence tracking, frame-drop detection, independent consumer speeds, safe
  producer/consumer synchronization, no per-consumer copies of the payload.
- `publish()` returns `PublishResult` with `accepted`, `sequence`, `dropped`,
  `overwritten_sequence` — the immediate feedback the ring buffer will provide
  without a separate `stats()` call.
- **`InProcessLatestPublisher.latest()` is a development convenience only**.
  A shared-memory ring buffer will not expose "latest frame" as a Python object;
  consumers will acquire read-only views into ring buffer slots.

## 8. Threading model

- One daemon thread per camera worker, named `Acquisition-<camera_id>`.
- `start()` transitions to CONNECTING and spawns the thread; `stop()` sets a
  stop event, joins with a timeout, and guarantees disconnect + publisher
  close happen before the worker reports STOPPED.
- No shared mutable state between camera workers. Each worker owns its source,
  publisher, config, and counters.
- All state/counter access is guarded by locks (`_state_lock`,
  `_stats_lock`); the publisher has its own lock.

## 9. Error / reconnection model

- `CameraGrabTimeout` (5322) and `CameraGrabError` are raised by the driver;
  `CameraConnectionError` is raised for open/configure failures.
- Single grab failure → DEGRADED. Consecutive failures ≥
  `consecutive_fail_limit` → RECONNECTING → `source.reopen()` (close + reopen
  framegrabber) with exponential backoff, up to `max_reconnect_attempts`,
  then ERROR.
- Connect failures retry through the same RECONNECTING path.
- Errors are logged (stdlib `logging`, module logger — the V3 core logging
  will layer onto this) and recorded in `AcquisitionStats.last_error`.
- Shutdown is deterministic: a stop request during a blocking grab resolves
  when the grab returns/raises; `stop()` joins with a timeout and never leaves
  the thread running after it returns STOPPED.
- A camera failure cannot stop other cameras (independent threads + sources).

## 10. Performance counters

`AcquisitionWorker.stats()` → `AcquisitionStats`:

- `total_acquired`, `published`, `dropped` (acquisition-side)
- `sequence_gaps` (acquisition sequence only)
- `consecutive_failures`, `reconnect_count`
- `last_grab_duration_s`
- `current_fps` (rolling 1 s window), `average_fps`
- `last_error`

Transport statistics (overwritten, consumer gaps, per-consumer drops) are the
responsibility of the transport layer, not acquisition. The worker does not
query `publisher.stats()` every frame.

Frame descriptor metadata carries `grab_started`, `grab_completed`,
`converted_at`, and `grab_duration_s` so per-frame acquisition latency is
measurable. No expensive work is performed inside the acquisition loop beyond
grab → copy → timestamp → sequence → publish.

## 11. Raw payload ownership and copy chain

Documented in `driver.py:238-247`:

1. **HALCON internal buffer** — `himage_as_numpy_array` may return a view
2. **`array.copy()`** → owned NumPy array (Copy #1, in driver)
3. **`array.setflags(write=False)`** → read-only view of owned array
4. **`FramePayload.__post_init__`** validates `writeable=False` at contract boundary
5. **Publisher receives `Frame`** — no additional copy in `InProcessLatestPublisher`
6. **Future `SharedMemoryRingBufferPublisher`** will copy into ring buffer slot (Copy #2)

The current in-process path has **one copy** (driver). The shared-memory path
will have **two copies** (driver + ring buffer slot write). A zero-copy path
writing directly into the ring buffer at grab time is a future optimization
(see §13).

## 12. Hardware-dependent behavior not yet verified

The following require a real TV46L and must be resolved before finalizing the
shared-memory design (from `docs/architecture/v2-camera-acquisition.md` §4):

1. Exact thermal geometry/format (`image_width`/`image_height`/`pixel_format`/
   `bits_per_channel`); V2 hardcodes 640×480 / 16-bit.
2. Visible stream (`VL_Data`) format: RGB8 vs packed YUV422, resolution, byte
   size; V2 diagnosis-only evidence.
3. Whether IR + visible can be acquired simultaneously (dual GigE handle via
   IP) or only time-sliced; TV46L is a single-stream camera
   (`DeviceStreamChannelCount = 1`).
4. True native visible frame rate vs the 9 FPS IR setting.
5. Grab-timeout semantics on the deployed HALCON build (5322 confirmed?),
   sensible timeout value (200 ms vs 500 ms), consecutive-failure threshold.
6. Packet-loss behavior / exposed GigE counters
   (`[Stream]GevStream*` vs unprefixed).
7. Reconnection on unplug/replug: which error surfaces, whether
   `close_framegrabber` + `open_framegrabber` reliably recovers, device
   string reuse vs IP.
8. Whether the camera exposes a GigE/PTP hardware timestamp.
9. NUC timing/duration and whether frames truly stop during NUC (V3 NUC-aware
   timeout handling is deferred).
10. One-time startup NUC requirement.
11. Focus motor limits/sentinel (`FOCUS_UNAVAILABLE_MM`).
12. Memory/time cost of `himage_as_numpy_array` + `.copy()` at full rate.

## 13. Remaining decisions for the future shared-memory implementation

- Ring-buffer capacity and slot layout (thermal 614,400 B + visible 921,600 B
  assumed per frame at V2 constants; must be confirmed on hardware).
- Zero/minimal-copy path: write directly into the ring buffer at grab time
  instead of the current driver copy, once raw buffer reuse is proven safe.
- Latest-frame access semantics and per-consumer sequence tracking.
- Buffer naming derived from stable camera identity (serial-based
  `camera_id`).
- Whether processing reads payloads by reference from shared memory or via
  zero-copy views.
- `num_buffers`/socket sizing for 8 simultaneous cameras.
- Where NUC-aware timeout handling belongs (worker vs driver) once NUC timing
  is measured.

## 14. Final review checklist

| Question | Answer |
| --- | --- |
| Can one camera acquisition fail without stopping another? | Yes — independent workers/threads (tested). |
| Can acquisition run without the GUI? | Yes — no GUI imports in camera/. |
| Can processing be replaced without changing acquisition? | Yes — worker only publishes `Frame`; consumers are decoupled. |
| Can the temporary publisher later be replaced by shared memory? | Yes — `FramePublisher` protocol with `PublishResult` is the seam. |
| Are raw thermal frames preserved? | Yes — 16-bit raw copied read-only, no 8-bit/temperature conversion. |
| Are thermal and visible data represented separately? | Yes — separate `StreamMetadata` + payload slots + `SyncInfo`. |
| Are timestamps and sequence numbers available? | Yes — wall + monotonic + hardware (when available), per-camera sequence. |
| Can dropped frames be detected? | Yes — dropped/overwritten/gap counters at worker and publisher. |
| Can the worker shut down cleanly? | Yes — deterministic stop/join/disconnect (tested). |
| Is V2 completely untouched? | Yes — nothing under `reference/TMS_v2/` was modified. |
| Unnecessary V3 files avoided? | Yes — three camera modules only. |
| GUI/processing/database dependencies in camera/? | None (verified by search). |
| TV46L assumptions requiring hardware verification? | Listed in §12. |
| Frame descriptor metadata immutable? | Yes — `MappingProxyType` (tested). |
| Frame payload arrays read-only? | Yes — validated at `FramePayload` construction (tested). |
| Sync status correct? | Yes — `UNKNOWN` for simultaneous IR/visible (TV46L time-slices). |
| Per-stream timestamps preserved? | Yes — thermal/visible have independent timestamps. |
| Visible stream honestly documented? | Yes — marked as not implemented in driver. |
| Publisher returns result, not stats? | Yes — `PublishResult` from `publish()`. |
| Acquisition never blocks on consumer? | Yes — `publish()` is non-blocking, returns immediately. |
| 8 independent workers? | Yes — no shared mutable global state (tested with 2). |
| Deterministic shutdown/reconnect? | Yes — tested with reconnection exhaustion and clean stop. |
| Sequence numbering correct? | Yes — acquisition sequence separate from consumer gaps. |
| Timestamp types defined? | Yes — wall-clock (recording), monotonic (relative), hardware (if available). |
| Copy chain documented? | Yes — in `driver.py` and §11. |

## 15. Testing performed

32 unit tests, all passing (`python -m pytest`):

- Frame contract: separation, immutable metadata (`MappingProxyType`), read-only
  payloads (enforced at construction), sync statuses, frame freezing.
- Driver timeout detection (error code 5322 and message fallback) without
  HALCON; config defaults match V2-proven tuning.
- Worker: monotonic sequence numbers, timestamps, state transitions, clean
  shutdown, degraded→reconnect→recovery, reconnect exhaustion → ERROR,
  connect failure behavior, frame publication, invalid-frame handling
  (nothing published, error recorded), FPS measurement, prompt stop.
- Two workers run independently (stopping one does not stop the other).
- `InProcessLatestPublisher`: latest-wins, overwrite counting with
  `overwritten_sequence`, gap detection, reset/close, `PublishResult` return.
- Workers operate with both the fake publisher and the in-process publisher,
  demonstrating the publisher protocol boundary.

Tests use a scripted `FakeFrameSource` and `FakePublisher` in
`tests/conftest.py`; no HALCON runtime is required.