# Stage 5E Report: TV46L Recording Validation

* Branch: `feature/v3-foundation`
* Date (UTC): 2026-08-19
* Interpreter: Python 3.10.7 (real MVTec HALCON 24.11.3.0), cv2 4.13.0, numpy 2.2.6
* Camera: `HB25100004` (TV46L-1-26010002@9Hz, fw 1.0.8, IP 169.254.24.69)

## Verdict

| Objective | Result |
|---|---|
| Capture real TV46L data into the V3 recording format | **PASS** |
| Round-trip integrity (write -> read -> byte-exact) | **PASS** |
| Real 30-60 s canonical recording | **PASS** |
| V3 offline processing chain on real IR | **PASS** |
| V2 vs V3 calibration / ROI / temperature parity on real data | **PASS** (bit-identical) |
| Real alarm evaluation on real statistics | **PASS** |
| Alternate IR/VL capture via HALCON | **FAILED** (hardware/provider limitation) |

Stage overall: **PARTIAL — offline processing fully validated on real IR; IR/VL
alternation not achievable through HALCON. No visible data was faked.**

## Evidence

| Artifact | Location |
|---|---|
| Offline processing + parity + alarm validation | `reports/hardware/tv46l_offline_processing_validation.json` (`overall_pass: true`) |
| Recording validation report (this stage) | `docs/hardware/tv46l-v3-recording-validation.md` |
| Canonical 45 s recording | `recordings/hardware_validation` (203 IR frames, COMPLETE, 203/203 CRC verified) |
| Short-burst IR recording | `recordings/hardware_test_001` (5 frames processed; round-trip exact) |
| Round-trip proof | `recordings/roundtrip_test_001` (5/5 byte-for-byte) |
| Real calibration blob (camera-fetched) | `temporary/calib_info_camera.txt` — byte-identical to `reference/TMS_v2/assets/calibration/calibration_blob.txt` |
| Characterization / probe | `docs/hardware/tv46l-characterization.md`, `reports/hardware/tv46l_probe_cam_HB25100004.json` |
| IR/VL alternation evidence | `docs/hardware/tv46l-halcon-dual-mode.md`, `tv46l-halcon-interleaved-pcap-analysis.md`, `tv46l-thermoview-stream-analysis.md` |

## Key numbers

* Real temps on IR: min 26.83-27.00 °C, max 34.34-34.44 °C, mean ~28.8 °C, std ~1.15 °C (plausible ambient)
* V2 vs V3: LUT max_abs_diff **0.0**, raw_to_temperature **0.0**, all ROI stats (5 ROIs x 5 frames) **0.0** vs proven V2 HALCON reference
* Alarms: ABOVE/BELOW/OUTSIDE_RANGE/INSIDE_RANGE fire + clear exactly as expected
* Performance (first baseline): full chain ~74 ms/frame (LUT 43.5 ms dominant), ROI stats 5.2-7.8 ms, alarms 5.2 ms
* Sustained capture: ~4.5 FPS with growing packet loss (28159 lost / 186 incomplete in 45 s) vs 0 loss in 5 s burst — a host/driver transport issue for production acquisition, not a format defect

## Code changes in this stage

* `src/thermal_monitor/processing/halcon/roi_adapter.py` — real-HALCON fixes surfaced by hardware validation:
  * `extract_statistics()` accepts the `rois=` kwarg (was missing; crashed with real HALCON) and populates `roi_id`/`roi_name`
  * `release_regions()` drops the HALCON reference instead of `clear_obj()` (fixed HALCON error #4051 double-free)
* `scripts/tv46l_capture_to_recording.py` — experimental capture tool (already modified in earlier steps; setup-order + transport fixes)
* `scripts/tv46l_offline_processing_validation.py` — new, STEPS 10-13 hardware validation
* `tests/test_v2_v3_comparison.py` — existing V2/V3 parity tests (all pass, 10/10)

## Test suite

`python -m pytest`: **349 passed, 15 skipped** (skips = SQL Server dependency). The
two `test_synthetic_pipeline.py` timing tests flake under full-suite load
(timing-sensitive, unrelated to these changes); they pass in isolation and on
subsequent full runs.

## Outstanding / follow-up

* **FAILED**: IR/VL alternating acquisition via HALCON — provider resets the dual-mode
  register on open and delivers IR-only Mono16. Real VL exists on the wire
  (ThermoView PCAP) and via selector switching (RGB8 verified in probe). Requires a
  non-HALCON transport (raw GigE/GVCP) in production acquisition to realize the V3
  recording contract's alternating IR/VL design.
* Recording manifest declares both IR and VL streams while physical records are IR
  only — writer should emit a truthful stream list.
* Sustained-capture packet loss needs real acquisition tuning (socket buffers, MTU,
  dedicated NIC, ring sizing); the capture tool's current defaults are not safe for
  long recordings.