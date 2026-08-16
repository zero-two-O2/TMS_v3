# V2 Frame Data Representation — Technical Recovery Report

## Purpose

Recover the exact frame data representation and flow from the V2 reference repository (`reference/TMS_v2/`) to inform V3's shared-memory ring buffer design and frame contract (ADR-002).

This report is read-only evidence. It does not design V3, copy code, or modify V3 source. V2 file paths are relative to `reference/TMS_v2/`.

Legend:
- **IMPLEMENTED** — Working in V2 production path
- **PARTIALLY IMPLEMENTED** — Exists but incomplete/divergent
- **NOT IMPLEMENTED** — Missing in V2
- **UNKNOWN** — Cannot be determined from V2 source

---

## 1. Frame Data Flow Trace (Production Path)

```
Camera (TV46L GigE Vision)
    │
    ▼
HALCON open_framegrabber("GigEVision2", ...)
    │
    ▼
Stream Configuration:
  FLK_TI_StreamDataSourceSelector = "IR_Data"
  bits_per_channel = 16
  [Stream]DeviceStreamChannelNegotiatePacketSize = 1
  [Stream]GevStreamReceiveSocketSize = 512000 (services) / 1048576 (validation)
  num_buffers = 8 (validation only; NOT SET in services)
  FLK_TI_ControlFeature_SetFrameRate = 9
  FLK_TI_ControlFeature_REControlCmd = DisableAutomaticFineOffsets
    │
    ▼
grab_image_start(fg, -1)  // Continuous streaming
    │
    ▼
AcquisitionEngine._acquisition_loop() (daemon thread "AcquisitionEngine")
    │
    ├─ HalconDriver.grab_frame() → ha.grab_image_async(fg, 200)
    │
    ├─ ha.himage_as_numpy_array(image)  // HALCON HImage → NumPy
    │
    ▼
queue.Queue(maxsize=2)  // Drop-oldest on Full
    │
    ▼
TV46LCamera.get_frame() → AcquisitionEngine.get_latest_frame()
    │
    ▼
Returns: **bare `np.ndarray`** (no metadata)
    │
    ▼
Consumer: ProcessingPipeline.process(camera_id, position_id, raw_frame)
    │
    ├─ RawFrame expected but NOT provided by production path
    │
    ▼
CalibrationProcessor.raw_to_temperature(raw_image, calibration, range_index)
    │
    ▼
Returns: **float32 temperature image** (°C)
    │
    ▼
ProcessedFrame: { raw_frame, temperature_image, display_image }
    │
    ▼
ROIProcessor → AlarmProcessor → FrameResult
    │
    ▼
GUI/Recording (if applicable)
```

---

## 2. Frame Representations at Each Stage

### 2.1 HALCON HImage (Internal)
| Property | Value | Source |
|----------|-------|--------|
| Type | `halcon.HImage` (opaque HALCON object) | `halcon_driver.py:319` |
| Source | `ha.grab_image_async(fg, timeout)` | `halcon_driver.py:319`, `tv46l_camera.py:393` |
| Lifetime | Managed by HALCON; must not be freed manually | HALCON convention |
| Conversion | `ha.himage_as_numpy_array(image)` | `halcon_driver.py:324`, `tv46l_camera.py:425` |

### 2.2 NumPy Array (Raw Thermal Frame) — Production Path
| Property | Value | Source |
|----------|-------|--------|
| **Type** | `np.ndarray` | `halcon_driver.py:324` |
| **Dtype** | `uint16` (16-bit mono) | `halcon_driver.py:185` (`bits_per_channel=16`) |
| **Shape** | `(480, 640)` assumed (H, W) | `halcon_roi_validation.py:65-66` `FEED_H, FEED_W` |
| **Channels** | 1 (mono) | IR_Data stream is single-channel |
| **Bytes/pixel** | 2 | 16-bit |
| **Frame size** | 640 × 480 × 2 = **614,400 bytes** | Calculated |
| **Endianness** | Native (HALCON→NumPy preserves) | `himage_as_numpy_array` |
| **Copy behavior** | **Always copies** (`.copy()` in top-level; services returns direct) | `tv46l_camera.py:445` vs `halcon_driver.py:324` |

> **Critical**: The production services path (`HalconDriver.grab_frame()`) returns the NumPy array **directly from `himage_as_numpy_array` with no copy**. The top-level `TV46LCamera._grab_raw_frame()` does `frame.copy()`.

### 2.3 RawFrame (Top-Level / Non-Production Path)
**File:** `processing/models/processing_models.py:34`
**Used by:** `camera/tv46l_camera.py` (diagnostics only), NOT by production services

```python
@dataclass(slots=True)
class RawFrame:
    image: np.ndarray                    # uint16, (480, 640)
    range_index: int = 0                 # Calibration range selector
    timestamp: datetime = now()          # Wall-clock receive time
    frame_number: int = 0                # Monotonic per-camera counter
    acquisition_timestamp: float = 0.0   # perf_counter() at grab start
    grab_start_time: float = 0.0         # perf_counter() before grab_image_async
    grab_complete_time: float = 0.0      # perf_counter() after grab_image_async
    numpy_complete_time: float = 0.0     # perf_counter() after himage_as_numpy_array
    publish_time: float = 0.0            # perf_counter() when pushed to queue
    sequence: int = -1                   # Same as frame_number (duplicate)
```

**Created at:** `camera/tv46l_camera.py:444-453` (`_grab_raw_frame()`)
**Copied at:** `camera/tv46l_camera.py:519-533` (`_copy_frame()` — deep copy of image + all metadata)

| Field | Source | Status |
|-------|--------|--------|
| `image` | `ha.himage_as_numpy_array(image).copy()` | IMPLEMENTED (top-level only) |
| `range_index` | Hardcoded `0` | IMPLEMENTED |
| `timestamp` | `datetime.now()` (wall clock) | IMPLEMENTED |
| `frame_number` | `_frame_counter` (incremented under lock) | IMPLEMENTED |
| `acquisition_timestamp` | `time.perf_counter()` at frame creation | IMPLEMENTED |
| `grab_start_time` | `_t0` before `grab_image_async` | IMPLEMENTED |
| `grab_complete_time` | `_t1` after `grab_image_async` | IMPLEMENTED |
| `numpy_complete_time` | `_t3` after `himage_as_numpy_array` | IMPLEMENTED |
| `publish_time` | Set by caller (`_grab_loop:362`) | IMPLEMENTED |
| `sequence` | Same as `frame_number` | IMPLEMENTED (redundant) |

### 2.4 Temperature Image (Post-Calibration)
| Property | Value | Source |
|----------|-------|--------|
| **Type** | `np.ndarray` | `calibration_processor.py:75` |
| **Dtype** | `float32` | `calibration_processor.py:70-75` |
| **Shape** | `(480, 640)` (same as raw) | Same dimensions |
| **Units** | Degrees Celsius (°C) | `calibration_processor.py:56` |
| **Invalid pixels** | `NaN` | `calibration_processor.py:262` (LUT initialized with NaN) |
| **Conversion** | `lut[raw_image]` via 65536-entry LUT | `calibration_processor.py:75` |

### 2.5 Display Image (8-bit Normalized)
| Property | Value | Source |
|----------|-------|--------|
| **Type** | `np.ndarray` | `calibration_processor.py:484` |
| **Dtype** | `uint8` | `calibration_processor.py:484` |
| **Shape** | `(480, 640)` | Same dimensions |
| **Range** | 0-255 | Min-max normalization |
| **Conversion** | `temperature_to_display()` | `calibration_processor.py:435` |

### 2.6 Visible Frame (VL_Data) — Experimental/Diagnostic Only
**NOT in production path.** Only in `halcon_camera_diagnosis.py`.

| Property | Value | Source |
|----------|-------|--------|
| **Stream selector** | `FLK_TI_StreamDataSourceSelector = "VL_Data"` | `halcon_camera_diagnosis.py:178, 357` |
| **Bits per channel** | `-1` (HALCON default) | `halcon_camera_diagnosis.py:360, 435` |
| **Format observed** | **RGB8** (640×480×3 = 921,600 B/frame) OR **packed YUV422 (YUYV)** | `halcon_camera_diagnosis.py:906-950` |
| **Decode** | `_decode_yuv422_to_rgb()` / direct use | `halcon_camera_diagnosis.py:906, 937` |
| **Frame size (RGB8)** | 640 × 480 × 3 = **921,600 bytes** | `halcon_camera_diagnosis.py:940` |
| **Frame size (YUYV)** | 640 × 480 × 2 = **614,400 bytes** | Packed 2 bytes/pixel |
| **Acquisition** | Requires 2nd GigE connection (IP) or time-slicing | `halcon_camera_diagnosis.py:269-286` |

---

## 3. Metadata & Identity

| Metadata | Production Path | Top-Level Path | Validation Path |
|----------|----------------|----------------|-----------------|
| **Camera ID** | `CameraModel.camera_id` (`cam_{serial}`) | `CameraInfo.serial` | `CameraInfo.serial` |
| **Sequence number** | **NOT IMPLEMENTED** | `RawFrame.sequence` / `frame_number` | `FrameBufferEntry.frame_id` |
| **Frame timestamp** | **NOT IMPLEMENTED** | `RawFrame.timestamp` (wall) + `acquisition_timestamp` (monotonic) | `FrameBufferEntry.timestamp` (time.time()) |
| **Hardware timestamp** | **NOT IMPLEMENTED** | **NOT IMPLEMENTED** | **NOT IMPLEMENTED** |
| **Grab timing** | **NOT IMPLEMENTED** | `grab_start_time`, `grab_complete_time`, `numpy_complete_time` | **NOT IMPLEMENTED** |
| **IR/Visible sync** | N/A (IR only) | N/A (IR only) | N/A (IR only) |
| **Camera temp** | `FLK_TI_Info_CurrentDeviceTemperatureC` | `tv46l_camera.py:903` | Available via parameter |
| **Firmware** | `[Device]DeviceVersion` | `tv46l_camera.py:925` | Available via parameter |
| **Focus position** | `FLK_TI_ControlFeature_CurrentFocusDistanceMm` | `tv46l_camera.py:804` | Available via parameter |
| **Packet stats** | **NOT IMPLEMENTED** | `get_stream_statistics()` reads `[Stream]GevStream*` | `halcon_roi_validation.py:1358` `_read_stream_stats()` |

---

## 4. Frame Conversions & Copies

### 4.1 Production Path (Services)
```
HALCON HImage
    │ ha.himage_as_numpy_array()  // ZERO or ONE copy (HALCON internal)
    ▼
np.ndarray (uint16)  // RETURNED DIRECTLY — no .copy()
    │ queue.Queue.put_nowait()  // Reference passed
    ▼
Consumer gets same array  // MUTABLE — consumer must copy if retaining
```

**Risk:** The array returned by `himage_as_numpy_array` may be backed by HALCON's internal buffer. Next `grab_image_async` may overwrite it. Consumer **must** copy if processing asynchronously.

### 4.2 Top-Level Path (Diagnostics)
```
HALCON HImage
    │ ha.himage_as_numpy_array()
    ▼
np.ndarray
    │ .copy()  // EXPLICIT COPY
    ▼
RawFrame(image=frame.copy(), ...)
    │ _copy_frame()  // ANOTHER DEEP COPY on every consumer read
    ▼
Consumer gets independent copy
```

**Cost:** 2 full frame copies per frame (614 KB × 2 = ~1.2 MB/frame at 9 FPS = ~11 MB/s copy bandwidth).

### 4.3 Validation Path (Proven Hardware Path)
```
HALCON HImage
    │ ha.himage_as_numpy_array()
    ▼
np.ndarray (uint16)
    │ CalibrationProcessor.raw_to_temperature()  // LUT lookup → float32
    ▼
temperature_image (float32)  // NEW ALLOCATION
    │ Stored in FrameBufferEntry.temp_numpy (ring buffer)
    ▼
ROI statistics computed on temperature_image
```

### 4.4 Temperature Conversion (Calibration)
```
raw_image (uint16) → lut[raw_image] → temperature_image (float32)
```
- **LUT size:** 65536 entries × 4 bytes = 256 KB per range
- **Operation:** Vectorized NumPy indexing (fast, no Python loop)
- **Output:** New `float32` array (614 KB → 1.2 MB, 2× expansion)
- **NaN handling:** Invalid raw values → NaN in temperature image

---

## 5. IR / Visible Synchronization

**V2 Finding:** **NOT IMPLEMENTED in any production path.**

### Hardware Reality (from diagnostic tool):
- TV46L is a **single-stream camera** (`DeviceStreamChannelCount = 1`)
- `FLK_TI_StreamDataSourceSelector` is **camera-global** — selects IR_Data OR VL_Data for the ONE channel
- **Simultaneous IR+Visible requires:**
  - **Dual-handle mode:** 2nd GigE connection via IP address (may be rejected)
  - **Time-sliced mode:** Switch source, wait ~1 s for resync, grab visible burst, switch back
- **Byte-size conflict detection:** If visible handle flips shared source, IR frame size changes (16-bit mono → RGB) — detected at `halcon_camera_diagnosis.py:1417-1433`

### Synchronization Options for V3:
| Approach | Latency | IR FPS Impact | Visible FPS | Complexity |
|----------|---------|---------------|-------------|------------|
| Dual handle (if supported) | Native | None | Native | Medium |
| Time-sliced (IR priority) | ~1 s switch | Minimal (2 s IR / 0.6 s VIS) | ~1-2 FPS | Low |
| Rapid switch (per-frame) | ~250 ms | Severe (~0.8 FPS) | ~0.8 FPS | High |

---

## 6. Frame Contract Comparison: V2 vs V3 ADR-002

| ADR-002 Requirement | V2 Production | V2 Top-Level | V2 Validation | V3 Gap |
|---------------------|---------------|--------------|---------------|--------|
| Camera identity | CameraModel.camera_id | CameraInfo.serial | CameraInfo.serial | KEEP identity model |
| Per-camera monotonic sequence | **MISSING** | RawFrame.sequence | FrameBufferEntry.frame_id | **MUST ADD** |
| Acquisition timestamp (monotonic) | **MISSING** | RawFrame.acquisition_timestamp | FrameBufferEntry.timestamp | **MUST ADD** |
| Wall-clock timestamp | **MISSING** | RawFrame.timestamp | **MISSING** | **MUST ADD** |
| Thermal raw data (immutable) | Mutable np.ndarray | RawFrame.image.copy() | FrameBufferEntry.temp_numpy (derived) | **MUST COPY/IMMUTABLE** |
| Visible raw data | **MISSING** | **MISSING** | Experimental only | **DESIGN REQUIRED** |
| Sync info (IR/Vis) | N/A | N/A | N/A | **DESIGN REQUIRED** |
| Acquisition metadata | **MISSING** | Grab timing (4 timestamps) | **MISSING** | **NICE TO HAVE** |
| ROI/alarm/overlay | Separate (processing) | Separate (processing) | Separate (FrameBufferEntry) | KEEP SEPARATE |
| Frame drop detection | Queue drops (no sequence) | Timeout counter | Consecutive failures + packet counters | **MUST ADD SEQUENCE** |

---

## 7. Implementation Status Summary

| Feature | Status | Evidence |
|---------|--------|----------|
| Thermal raw frame (uint16) | **IMPLEMENTED** | `halcon_driver.py:324`, `tv46l_camera.py:425` |
| Visible raw frame | **PARTIALLY IMPLEMENTED** | `halcon_camera_diagnosis.py` only; not in production |
| HALCON HImage → NumPy | **IMPLEMENTED** | `himage_as_numpy_array` at all grab sites |
| NumPy dtype (uint16) | **IMPLEMENTED** | `bits_per_channel=16` configured |
| Frame dimensions (640×480) | **PARTIALLY IMPLEMENTED** | Hardcoded constants; validation reads actual `image_width`/`image_height` |
| Channels (1 for thermal) | **IMPLEMENTED** | IR_Data is mono |
| Bytes/pixel (2 for thermal) | **IMPLEMENTED** | 16-bit = 2 bytes |
| Temperature data (float32 °C) | **IMPLEMENTED** | `calibration_processor.py:75` |
| Frame timestamps (wall) | **PARTIALLY IMPLEMENTED** | Top-level only: `datetime.now()` |
| Frame timestamps (monotonic) | **PARTIALLY IMPLEMENTED** | Top-level only: `perf_counter()` at 4 points |
| Sequence numbers | **PARTIALLY IMPLEMENTED** | Top-level & validation only; NOT in production |
| Hardware metadata (temp, focus, firmware) | **IMPLEMENTED** | `tv46l_camera.py:903-930` via HALCON params |
| Camera metadata (serial, IP, model) | **IMPLEMENTED** | `CameraInfo` / `CameraModel` |
| IR/Visible sync | **NOT IMPLEMENTED** | Single-stream camera; experimental dual-handle only |
| Frame conversions (HALCON→NumPy→Temp→Display) | **IMPLEMENTED** | Full pipeline in `calibration_processor.py` |
| Copies (HALCON→NumPy) | **PARTIALLY IMPLEMENTED** | Services: 0 copy; Top-level: 2 copies |
| Copies (NumPy→Temp) | **IMPLEMENTED** | Always allocates new float32 array (LUT indexing) |
| Raw data loss | **UNKNOWN** | Services returns mutable array; next grab may overwrite |
| Packet loss counters | **PARTIALLY IMPLEMENTED** | Validation reads `[Stream]GevStream*`; production does not |
| NUC frame gaps | **IMPLEMENTED** | Validation handles NUC timeouts; production swallows errors |

---

## 8. Critical V2 Findings for V3 Ring Buffer Design

### 8.1 Frame Sizes (Must Verify on Hardware)
| Stream | Assumed Size | Actual Must Confirm |
|--------|-------------|---------------------|
| Thermal (IR_Data) | 640×480×2 = 614,400 B | `image_width` × `image_height` × `bits_per_channel/8` |
| Visible (VL_Data) RGB8 | 640×480×3 = 921,600 B | Could be YUYV (614,400 B) or other |
| Temperature (float32) | 640×480×4 = 1,228,800 B | Derived from thermal |
| Display (uint8) | 640×480×1 = 307,200 B | Derived from temperature |

### 8.2 Timing Measurements Needed
| Measurement | V2 Evidence | V3 Need |
|-------------|-------------|---------|
| `grab_image_async` latency (typical/max) | Top-level diagnostic: ~avg/max ms printed per-second | Ring buffer slot hold time |
| `himage_as_numpy_array` latency | Top-level diagnostic: separate measurement | Copy vs zero-copy decision |
| LUT lookup (raw→temp) latency | Not measured | Processing pipeline budget |
| Source switch latency (IR↔Vis) | Diagnosis: `switch_to_visible_ms`, `switch_to_ir_ms` (~hundreds of ms) | Dual-stream feasibility |
| NUC blackout duration | Validation: `nuc_duration_seconds=2.5`, flush 3 frames | Frame gap handling |
| Packet loss at 9 FPS | Validation reads counters; services uses socket 512KB vs validation 1MB | Socket/buffer sizing |

### 8.3 Queue/Buffer Behavior
| Path | Buffer | Drop Policy | Sequence Tracking |
|------|--------|-------------|-------------------|
| Services | `Queue(maxsize=2)` | Drop oldest | **NONE** |
| Top-level | Single slot + lock | Latest wins | Monotonic counter |
| Validation | `deque(maxlen=30)` (2 s ring) | Oldest dropped | `frame_id` monotonic |

---

## 9. Measurements That Must Be Obtained from the Real TV46L

The following **cannot be determined from V2 source** and must be verified on hardware before designing the V3 shared-memory ring buffer:

1. **Thermal frame geometry/format:** Exact `image_width`, `image_height`, `pixel_format`, `bits_per_channel` reported by framegrabber for `IR_Data`. (V2 hardcodes 640×480 / 16-bit.)

2. **Visible stream format:** `VL_Data` resolution, pixel format (RGB8 vs packed YUV422 vs other), byte size per frame. V2 has both decode paths; only hardware says which applies.

3. **Simultaneous IR+Visible feasibility:** Does the camera accept a second concurrent GigE connection (dual handle) and deliver two independent streams? If not, what is the true source-switch latency (V2 estimates ~1 s)? Does the second handle echo the IR source (byte-size comparison)?

4. **True native visible frame rate** vs the 9 FPS IR setting (visible may stream at a different native rate).

5. **Frame-rate feature limits:** `FLK_TI_ControlFeature_SetFrameRate` min/max/increment and writability (`frame_rate_capabilities`, `tv46l_camera.py:946`); is 9 FPS optimal?

6. **Grab-timeout semantics:** Confirm HALCON error code 5322 on deployed build; measure sensible timeout (200 ms vs 500 ms) and consecutive-failure threshold.

7. **Packet-loss behavior:** Which GigE counters are exposed (`[Stream]GevStream*` vs unprefixed), negotiated packet size, interface MTU, and socket size / `num_buffers` values that prevent loss with multiple concurrent streams.

8. **Reconnection behavior:** What happens on unplug/replug — which error surfaces on `grab_image_async`, does `close_framegrabber` + `open_framegrabber` reliably recover, and is the discovery device string reusable or must the IP be used?

9. **NUC timing:** Actual duration of `RequestFineOffset`/`ExecuteFineOffset`, whether frames truly stop during NUC, and the safe retry interval.

10. **Startup NUC:** Whether a one-time NUC after connect is genuinely required for stable 16-bit data.

11. **Focus motor:** Confirm min/max/settling behavior and that `FOCUS_UNAVAILABLE_MM = 1000000.0` is the real "unavailable" sentinel.

12. **Hardware timestamps:** Whether the camera exposes a GigE/PTP timestamp usable as an acquisition timestamp (needed for IR/visible synchronization and ADR-002's timestamp requirement).

13. **Device temperature features:** Confirm `FLK_TI_Info_CurrentDeviceTemperatureC` / `CriticalDeviceTemperatureC` exist and their units.

14. **IP/link configuration:** Link-local behavior (`169.254.x.x`), static vs DHCP, and whether camera identity truly stays serial-based across IP changes.

15. **Number of supported simultaneous connections** per camera (relevant to 8-camera deployments and dual-handle visible).

16. **Actual memory/time cost of `himage_as_numpy_array` + `.copy()`** per frame at full rate (V2 timed grabs in diagnostics; V3 must size its zero/minimal-copy transport accordingly).

17. **Maximum sustainable throughput:** With 4-8 cameras at 9 FPS, what is the real CPU/memory/bandwidth cost of the HALCON→NumPy→Temperature pipeline?

---

## 10. Key V2 File References

| Component | File | Key Functions |
|-----------|------|---------------|
| RawFrame model | `processing/models/processing_models.py:34` | `RawFrame` dataclass |
| Production grab | `camera/services/halcon_driver.py:308` | `HalconDriver.grab_frame()` |
| Production queue | `camera/services/acquisition_engine.py:49` | `queue.Queue(maxsize=2)` |
| Top-level grab | `camera/tv46l_camera.py:388` | `_grab_raw_frame()` |
| Top-level RawFrame creation | `camera/tv46l_camera.py:444` | `RawFrame(...)` |
| Top-level copy | `camera/tv46l_camera.py:519` | `_copy_frame()` |
| Temperature conversion | `calibration/calibration_processor.py:49` | `raw_to_temperature()` |
| LUT build | `calibration/calibration_processor.py:254` | `_build_lookup_table()` |
| Validation frame buffer | `halcon_roi_validation.py:714` | `FrameBufferEntry` |
| Validation grab config | `halcon_roi_validation.py:1099` | `_configure_camera()` (num_buffers=8, socket=1MB) |
| Visible stream decode | `halcon_camera_diagnosis.py:906` | `_decode_yuv422_to_rgb()` |
| Visible display convert | `halcon_camera_diagnosis.py:937` | `_visible_to_display()` |
| Dual-handle detection | `halcon_camera_diagnosis.py:1182` | `_verify_visible_stream()` |
| Stream stats | `halcon_roi_validation.py:1358` | `_read_stream_stats()` |
| Camera identity | `app/application.py:233` | `camera_id = f"cam_{serial}"` |
| Camera model | `camera/models/camera_model.py:31` | `CameraModel` |

---

(End of report)