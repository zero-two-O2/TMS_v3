# TV46L V3 Recording Validation (Stage 5E)

* Run UTC: 2026-08-19
* Branch: `feature/v3-foundation`
* Interpreter: `C:/Users/admin/AppData/Local/Programs/Python/Python310/python.exe` (Python 3.10.7, real MVTec `halcon`, `cv2` 4.13.0, numpy 2.2.6)
* Camera: `HB25100004` (TV46L-1-26010002@9Hz, fw 1.0.8, IP 169.254.24.69)

## Verdict

| STEP | Check | Result |
|---|---|---|
| 10 | V3 offline chain on real IR (OfflineFrameSource -> TemperatureConverter -> HALCON ROI -> AlarmEvaluator) | **PASS** (5 frames, 100% finite temps, 26.8-34.4 °C) |
| 11 | V3 `HalconROIAdapter` vs V2 proven reference (`gen_rectangle1`+`intensity`+`min_max_gray`) | **PASS** (bit-identical, max diff 0.0, 5 frames x 5 ROIs) |
| 12 | V3 `CalibrationProcessor` vs V2 on the real frame (LUT + raw_to_temperature) | **PASS** (max abs diff 0.0) |
| 13 | Real alarm ABOVE/BELOW/OUTSIDE_RANGE/INSIDE_RANGE + clear on real statistics | **PASS** |
| 14 | Canonical 45 s recording | **PASS** (203 verified IR frames, COMPLETE, 2 chunks) |
| 15 | This report | PASS |
| 16 | Performance baselines | See below |
| — | IR/VL alternating capture via HALCON | **FAILED** (documented below; no faked VL) |

Machine report: `reports/hardware/tv46l_offline_processing_validation.json`
Validation script: `scripts/tv46l_offline_processing_validation.py`

## STEP 10-13: offline processing on real IR

### Calibration provenance

The per-camera calibration blob was fetched from the camera via HALCON
`FLK_TI_CalibrationInfo` (read-only GVCP string) and saved to
`temporary/calib_info_camera.txt`:

* length 1530 hex chars -> 764 bytes
* magic `0x52696D01`, 2 enabled ranges (`0x00000003`), calibration date `12/01/2026`
* range 0: -20.0 -> 80.0 °C (7 segments); range 1: -20.0 -> 1200.0 °C (11 segments)
* **byte-identical** to `reference/TMS_v2/assets/calibration/calibration_blob.txt`
  (verified 2026-08-19).  V2's calibration blob is the real TV46L camera calibration.

### STEP 12: V2 vs V3 calibration parity (real frame)

Same blob, same raw 640x480 uint16 frame:

| Item | V2 | V3 | max abs diff | Tolerance |
|---|---|---|---|---|
| LUT[0] (float32, 65536) | identical | identical | **0.0** | 1e-5 |
| raw_to_temperature image | identical | identical | **0.0** | 1e-4 |
| stats min / max / mean / median / std | 26.934 / 34.343 / 28.817 / 28.448 / 1.169 | same | 0.0 | 1e-3 |

The recorded frame therefore converts to the same temperature image in V2 and V3.

### STEP 11: V2 vs V3 ROI statistics (real frames)

V3 `HalconROIAdapter` (Rectangle1, batched `gen_rectangle1`, per-ROI `intensity` +
`min_max_gray`) vs V2 proven reference path (`ha.intensity` + `ha.min_max_gray`
batched, as in `halcon_roi_validation.py`).  5 frames x 5 ROIs
(`full_frame`, `center`, `hot_spot`, `corner_ul`, `corner_lr`):

* every ROI, every frame: max abs diff **0.0** for min/max/mean/deviation (tolerance 1e-3)
* example frame 0 `full_frame`: mean 28.8167 °C, dev 1.1689, min 26.934, max 34.343

V3 reproduces V2 ROI statistics exactly on real camera data.

### STEP 13: real alarm evaluation

Rules built from the measured real statistics (`center` mean 28.9 °C, max 31.6 °C;
`hot_spot` mean 29.2 °C; `full_frame` min 26.9 / max 34.3 °C):

| Rule | Condition | Result |
|---|---|---|
| `center_above` (thr = center.max - 0.1) | ABOVE | fired CRITICAL |
| `center_below` (thr = center.min + 0.1) | BELOW | fired WARNING |
| `center_ok` (thr = center.max + 2.0) | ABOVE | did not fire |
| `hot_outside` (band below hot mean) | OUTSIDE_RANGE | fired CRITICAL |
| `full_inside` (band inside [full.min, full.max]) | INSIDE_RANGE | fired WARNING |

Second frame with benign thresholds: `center_above` and `center_below` both cleared.
No unexpected firings; `AlarmEvaluator` state transitions correct on real data.

## STEP 14: canonical recording

* Location: `recordings/hardware_validation` (gitignored)
* Duration: 45.16 s wall; recording 44.94 s (first ts -> last ts)
* Frames: **203** all IR Mono16 640x480 uint16 (614400 B each)
* Reader status: **COMPLETE**, 2 chunks, 203/203 CRC verified, 0 failures
* Recording sequence: contiguous 0..202, all unique
* Hardware frame_id (buffer_frameid): monotonic with drops (packet loss, see below)
* Sync groups: 0; stream metadata matches physical records (all IR)

### Sustained-capture packet loss

| Metric | 5 s capture (hardware_test_001) | 45 s capture (hardware_validation) |
|---|---|---|
| Frames | 37 | 203 |
| Avg FPS (tool-measured) | ~7.4 | ~4.5 |
| Lost packets (GevStreamLostPacketCount) | 0 | **28159** |
| Incomplete blocks | 0 | **186** |
| Frame interval mean / median | ~0.13 / 0.11 s | 0.222 / 0.121 s |

The transport params that gave 0 loss on the 5 s run
(`[Stream]DeviceStreamChannelNegotiatePacketSize=1`,
`GevStreamReceiveSocketSize=1048576`, `num_buffers=8`) are NOT sufficient for a
45 s sustained capture on this host: packet loss grows over time and the effective
FPS drops from ~8 to ~4.5.  All recorded frames still pass CRC (HALCON delivered
complete blocks; dropped packets appear as frame-id gaps, e.g. frame_id 7->8, 20->34,
273->284).  This is a **real** host/driver limitation to fix in production
acquisition (larger socket buffer, ring-buffer sizing, jumbo MTU, and/or a
dedicated NIC), not a recording-format defect.  Do not ship the capture tool's
current defaults for long recordings.

## STEP 16: performance baselines (real data, Python 3.10, HALCON 24.11)

Measured on `recordings/hardware_test_001` frames (via
`tv46l_offline_processing_validation.py`):

* STEP 10 full chain (read frame + LUT convert + 5 ROIs + stats): **368 ms / 5 frames** (~74 ms/frame)
* STEP 12 calibration conversion (LUT lookup on one 640x480 frame): **43.5 ms**
* STEP 11 ROI statistics (5 Rectangle1 ROIs, adapter + V2 reference): **5.2-7.8 ms / frame**
* STEP 13 alarm evaluation (5 rules): **5.2 ms**

The temperature LUT lookup dominates (uint16 -> float32 gather on 307200 pixels).
ROI stats are cheap.  These are **first baselines only**; production targets
(documented in ADR-003) must be measured on the production acquisition path.

## IR/VL alternating capture: FAILED (no faking)

As previously documented (`tv46l-halcon-dual-mode.md`,
`tv46l-halcon-interleaved-pcap-analysis.md`):

* GVCP dual-mode register `0x10a110=2` writes and reads back correctly when the CCP
  is held, but HALCON's provider resets it on open (reads 0 after close) and HALCON
  delivers **only Mono16 IR** (all 203 records are stream_type=1; 0 YUV422_8).
* While HALCON holds the CCP, external GVCP reads/writes fail with 0x8006.
* Alternation provably exists on the wire (ThermoView PCAP analysis) but HALCON's
  GigEVision2 provider cannot consume it.

Consequences for this stage:

* The canonical recording contains **IR only**.  Its manifest `streams`
  `{"IR": true, "VL": true}` is therefore **inaccurate metadata** (the capture tool
  declares both streams up front; actual physical records are IR only).  The reader
  does not depend on it, but the writer should emit a truthful stream list.
* Visible frames remain obtainable only by **single-source selector switching**
  (probe: `FLK_TI_StreamDataSourceSelector=VL_Data` yields real RGB8 640x480x3,
  saved under `temporary/vl_diag/`).  This is not the V3 recording contract.
* No visible frames were fabricated; none were duplicated.

## Files touched in this stage

* `scripts/tv46l_offline_processing_validation.py` (new, STEP 10-13)
* `scripts/tv46l_capture_to_recording.py` (already modified; STEP 3 fixes)
* `src/thermal_monitor/processing/halcon/roi_adapter.py` (fixed `rois=` param
  support + `release_regions` refcount-safe; see below)
* `reports/hardware/tv46l_offline_processing_validation.json` (generated)
* `docs/hardware/tv46l-characterization.md`, `reports/hardware/tv46l_probe_cam_HB25100004.json`
  (generated earlier this stage)
* `recordings/hardware_validation/`, `recordings/hardware_test_001/`,
  `recordings/roundtrip_test_001/`, `temporary/` (gitignored)

### Adapter fixes required by real-HALCON validation

Two real-HALCON defects found and fixed in `HalconROIAdapter`:

1. `process_rois_with_halcon()` passed `rois=enabled_rois` to `extract_statistics()`
   but the real adapter signature lacked the parameter -> `TypeError` with the real
   HALCON (mock-only tests had masked it).  Added optional `rois` parameter that
   populates `roi_id`/`roi_name`.
2. `release_regions()` called `ha.clear_obj()` on a handle the Python wrapper still
   references -> double-free (`HALCON error #4051 object has been deleted already`)
   at GC.  HALCON 24.11 Python bindings are reference-counted; now the adapter drops
   the reference instead of clearing the object.

Both were masked by mock-based unit tests; the real-hardware validation surfaced them.
