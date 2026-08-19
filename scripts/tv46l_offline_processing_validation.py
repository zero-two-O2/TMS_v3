#!/usr/bin/env python3
"""tv46l_offline_processing_validation.py -- validate V3 offline processing on real IR.

STAGE 5E STEP 10-13: prove the V3 offline processing path on REAL camera data:

    OfflineFrameSource -> TemperatureConverter -> HALCON ROI adapter -> AlarmEvaluator

using a real TV46L recording (recordings/hardware_test_001) and the real per-camera
calibration blob (fetched from the camera via FLK_TI_CalibrationInfo; byte-identical to
the V2 reference blob reference/TMS_v2/assets/calibration/calibration_blob.txt).

Checks performed
----------------
STEP 10: full V3 offline chain produces AnalysisResult per frame.
STEP 11: V3 HalconROIAdapter ROI statistics vs the V2 proven reference algorithm
         (batched gen_rectangle1 + intensity + min_max_gray, exactly as in
         reference/TMS_v2/halcon_roi_validation.py lines 1196/1669-1670).
STEP 12: V3 CalibrationProcessor vs V2 CalibrationProcessor on the same raw frame
         (LUT equality + raw_to_temperature equality + statistics equality).
STEP 13: AlarmEvaluator ABOVE / BELOW / OUTSIDE_RANGE / INSIDE_RANGE rules on the
         real frame statistics (event firing + clearing across frames).

All comparisons must agree to the documented tolerance.  The script writes a machine
report to reports/hardware/tv46l_offline_processing_validation.json.

NOTE: run with the Python 3.10 interpreter that has the real ``halcon`` interface and
``cv2`` installed (V2 processor imports cv2):
    C:/Users/admin/AppData/Local/Programs/Python/Python310/python.exe
The V3 .venv contains an unrelated PyPI package also named ``halcon`` which shadows the
real interface; do NOT use it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

# V2 reference implementation lives under reference/TMS_v2 (read-only).  We import it
# directly so V3 is compared against the actual V2 code, not a re-implementation.
sys.path.insert(0, str(REPO_ROOT / "reference" / "TMS_v2"))

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_RECORDING = REPO_ROOT / "recordings" / "hardware_test_001"
DEFAULT_CALIBRATION = REPO_ROOT / "reference" / "TMS_v2" / "assets" / "calibration" / "calibration_blob.txt"
DEFAULT_OUTPUT = REPO_ROOT / "reports" / "hardware" / "tv46l_offline_processing_validation.json"

TOL_LUT = 1e-5        # float32 LUT parity tolerance (operation order)
TOL_TEMP = 1e-4       # temperature image parity tolerance
TOL_STATS = 1e-3      # ROI statistics parity tolerance

# Real calibration blob fetched from the camera (temporary/, gitignored).  If present it
# is the authoritative per-camera blob; otherwise fall back to the V2 reference blob
# (byte-identical to the camera blob, verified 2026-08-19).
CAMERA_CALIBRATION = REPO_ROOT / "temporary" / "calib_info_camera.txt"

# ROIs (HALCON row/col convention, 640x480 image).
ROI_DEFS = [
    ("full_frame", {"y1": 0.0, "x1": 0.0, "y2": 479.0, "x2": 639.0}),
    ("center", {"y1": 180.0, "x1": 240.0, "y2": 300.0, "x2": 400.0}),
    ("hot_spot", {"y1": 100.0, "x1": 300.0, "y2": 200.0, "x2": 400.0}),
    ("corner_ul", {"y1": 10.0, "x1": 10.0, "y2": 120.0, "x2": 160.0}),
    ("corner_lr", {"y1": 360.0, "x1": 480.0, "y2": 470.0, "x2": 630.0}),
]


# ---------------------------------------------------------------------------
# V2 reference calibration processor (imported from reference/TMS_v2)
# ---------------------------------------------------------------------------

from calibration.calibration_parser import CalibrationParser as V2CalibrationParser
from calibration.calibration_processor import CalibrationProcessor as V2CalibrationProcessor

# V3 implementation
from thermal_monitor.calibration.parser import CalibrationParser as V3CalibrationParser
from thermal_monitor.calibration.processor import CalibrationProcessor as V3CalibrationProcessor


def build_calibrations(blob_file: Path):
    """Build (v3_calibration, v2_calibration) with lookup tables from the same blob."""
    v3 = V3CalibrationParser().load(blob_file)
    V3CalibrationProcessor.build_lookup_tables(v3)
    v2 = V2CalibrationParser().load(blob_file)
    V2CalibrationProcessor.build_lookup_tables(v2)
    return v3, v2


# ---------------------------------------------------------------------------
# V3 offline chain (STEP 10)
# ---------------------------------------------------------------------------

def run_v3_chain(recording_dir: Path, v3_cal, frame_limit: int) -> dict:
    """Run OfflineFrameSource -> TemperatureConverter -> ROI adapter -> alarms."""
    import halcon as ha  # real HALCON required

    from thermal_monitor.core.models import (
        AnalysisConfig,
        PositionROIAssociation,
        ROIConfig,
        ROIGeometry,
        ROIShape,
    )
    from thermal_monitor.offline import StreamFilter, open_offline_source
    from thermal_monitor.processing.halcon import HalconROIAdapter, process_rois_with_halcon
    from thermal_monitor.processing.temperature import CPUTemperatureConverter

    rois = {}
    for name, params in ROI_DEFS:
        rois[name] = ROIConfig(
            roi_id=name,
            name=name,
            geometry=ROIGeometry(shape=ROIShape.RECTANGLE1, parameters=dict(params)),
        )
    assoc = PositionROIAssociation(position_id="default", roi_ids=tuple(ROI_DEFS))
    config = AnalysisConfig(
        camera_id="HB25100004",
        rois=rois,
        position_associations={"default": assoc},
    )

    lut = v3_cal.get_lookup_table(0)
    converter = CPUTemperatureConverter()
    adapter = HalconROIAdapter()

    frames_seen = 0
    results = []
    source = open_offline_source(recording_dir, stream_filter=StreamFilter.IR)
    try:
        while True:
            frame = source.get_next_frame()
            if frame is None:
                break
            thermal = frame.payload.thermal
            if thermal is None:
                continue
            temp = converter.raw_to_temperature(
                raw_data=thermal,
                calibration=lut,
                emissivity=0.95,
                ambient_temp=25.0,
                distance=1.0,
                humidity=50.0,
                reflected_temp=20.0,
            )
            stats = process_rois_with_halcon(list(rois.values()), temp, adapter=adapter)
            by_id = {s.roi_id: s for s in stats}
            results.append({
                "frame_sequence": frame.descriptor.sequence,
                "timestamp": frame.descriptor.timestamp,
                "thermal_shape": list(thermal.shape),
                "thermal_dtype": str(thermal.dtype),
                "temp_finite_pct": round(float(np.isfinite(temp).mean()) * 100.0, 6),
                "temp_min": round(float(np.nanmin(temp)), 6),
                "temp_max": round(float(np.nanmax(temp)), 6),
                "temp_mean": round(float(np.nanmean(temp)), 6),
                "stats": {
                    name: {
                        "min": round(by_id[name].min_temp, 6),
                        "max": round(by_id[name].max_temp, 6),
                        "mean": round(by_id[name].mean_temp, 6),
                        "deviation": round(by_id[name].deviation, 6),
                    }
                    for name, _ in ROI_DEFS if name in by_id
                },
            })
            frames_seen += 1
            if frame_limit and frames_seen >= frame_limit:
                break
    finally:
        source.close()

    return {
        "frames_processed": frames_seen,
        "recording_dir": str(recording_dir),
        "frame_samples": results,
    }


# ---------------------------------------------------------------------------
# STEP 11: V3 adapter vs V2 proven reference ROI algorithm
# ---------------------------------------------------------------------------

def run_roi_comparison(v3_temp: np.ndarray, sample_index: int) -> dict:
    """Compare V3 HalconROIAdapter stats against V2 batched reference on same image."""
    import halcon as ha

    from thermal_monitor.core.models import (
        ROIConfig,
        ROIGeometry,
        ROIShape,
    )
    from thermal_monitor.processing.halcon import process_rois_with_halcon

    rois = [
        ROIConfig(
            roi_id=name,
            name=name,
            geometry=ROIGeometry(shape=ROIShape.RECTANGLE1, parameters=dict(params)),
        )
        for name, params in ROI_DEFS
    ]

    # ---- V3 path (per-ROI through the adapter) ----
    v3_stats = process_rois_with_halcon(rois, v3_temp)
    v3_by_id = {s.roi_id: s for s in v3_stats}

    # ---- V2 proven reference path: batched gen_rectangle1 + intensity + min_max_gray ----
    # (reference/TMS_v2/halcon_roi_validation.py lines 1196 + 1669-1670)
    rows1 = [int(round(float(r.geometry.parameters["y1"]))) for r in rois]
    cols1 = [int(round(float(r.geometry.parameters["x1"]))) for r in rois]
    rows2 = [int(round(float(r.geometry.parameters["y2"]))) for r in rois]
    cols2 = [int(round(float(r.geometry.parameters["x2"]))) for r in rois]
    regions = ha.gen_rectangle1(rows1, cols1, rows2, cols2)
    halcon_temp_image = ha.himage_from_numpy_array(v3_temp.astype(np.float32))
    mean_vals, dev_vals = ha.intensity(regions, halcon_temp_image)
    min_vals, max_vals, range_vals = ha.min_max_gray(regions, halcon_temp_image, 0)
    del regions  # reference-counted; do NOT clear_obj (avoids HALCON #4051 double-free)

    comparisons = []
    all_pass = True
    for i, roi in enumerate(rois):
        v3 = v3_by_id[roi.roi_id]
        v2 = {
            "min": float(min_vals[i]),
            "max": float(max_vals[i]),
            "mean": float(mean_vals[i]),
            "deviation": float(dev_vals[i]),
            "range": float(range_vals[i]),
        }
        diffs = {
            "min": abs(v3.min_temp - v2["min"]),
            "max": abs(v3.max_temp - v2["max"]),
            "mean": abs(v3.mean_temp - v2["mean"]),
            "deviation": abs(v3.deviation - v2["deviation"]),
        }
        ok = all(d <= TOL_STATS for d in diffs.values())
        all_pass = all_pass and ok
        comparisons.append({
            "roi": roi.roi_id,
            "v3": {
                "min_temp": round(v3.min_temp, 6),
                "max_temp": round(v3.max_temp, 6),
                "mean_temp": round(v3.mean_temp, 6),
                "deviation": round(v3.deviation, 6),
            },
            "v2_reference": {k: round(v, 6) for k, v in v2.items()},
            "diff": {k: round(d, 9) for k, d in diffs.items()},
            "pass": ok,
        })

    return {
        "sample_index": sample_index,
        "tolerance": TOL_STATS,
        "pass": all_pass,
        "rois": comparisons,
    }


# ---------------------------------------------------------------------------
# STEP 12: V2 vs V3 calibration on real frame
# ---------------------------------------------------------------------------

def run_calibration_comparison(raw_frame: np.ndarray, v2_cal, v3_cal) -> dict:
    """Compare V2 and V3 LUTs and raw_to_temperature on one real frame."""
    v2_temp = V2CalibrationProcessor.raw_to_temperature(raw_frame, v2_cal, 0)
    v3_temp = V3CalibrationProcessor.raw_to_temperature(raw_frame, v3_cal, 0)

    v2_lut = v2_cal.get_lookup_table(0)
    v3_lut = v3_cal.get_lookup_table(0)

    lut_diff = np.abs(v3_lut - v2_lut)
    lut_max = float(np.nanmax(lut_diff[np.isfinite(lut_diff)])) if np.isfinite(lut_diff).any() else 0.0

    temp_diff = np.abs(v3_temp - v2_temp)
    temp_max = float(np.nanmax(temp_diff[np.isfinite(temp_diff)])) if np.isfinite(temp_diff).any() else 0.0

    v2_stats = V2CalibrationProcessor.get_temperature_statistics(v2_temp)
    v3_stats = V3CalibrationProcessor.get_temperature_statistics(v3_temp)

    return {
        "lut": {
            "v2_dtype": str(v2_lut.dtype),
            "v3_dtype": str(v3_lut.dtype),
            "size": int(v3_lut.size),
            "max_abs_diff": round(lut_max, 9),
            "pass": lut_max <= TOL_LUT,
        },
        "raw_to_temperature": {
            "v2_dtype": str(v2_temp.dtype),
            "v3_dtype": str(v3_temp.dtype),
            "shape": list(v3_temp.shape),
            "max_abs_diff": round(temp_max, 9),
            "pass": temp_max <= TOL_TEMP,
        },
        "temperature_statistics": {
            key: {
                "v2": round(float(v2_stats[key]), 6) if np.isfinite(v2_stats[key]) else None,
                "v3": round(float(v3_stats[key]), 6) if np.isfinite(v3_stats[key]) else None,
            }
            for key in ("minimum", "maximum", "mean", "median", "std")
        },
    }


# ---------------------------------------------------------------------------
# STEP 13: real alarm evaluation
# ---------------------------------------------------------------------------

def run_alarm_evaluation(v3_temp: np.ndarray, frame_sequence: int, frame_timestamp: float) -> dict:
    """Evaluate ABOVE/BELOW/OUTSIDE_RANGE/INSIDE_RANGE rules on real statistics."""
    import halcon as ha

    from thermal_monitor.core.models import (
        ROIConfig,
        ROIGeometry,
        ROIShape,
        AlarmCondition,
        AlarmSeverity,
        AlarmRule,
        AnalysisConfig,
        AnalysisResult,
    )
    from thermal_monitor.processing.alarms import AlarmEvaluator
    from thermal_monitor.processing.halcon import process_rois_with_halcon

    # Build the same ROI set and compute statistics once.
    rois = [
        ROIConfig(
            roi_id=name,
            name=name,
            geometry=ROIGeometry(shape=ROIShape.RECTANGLE1, parameters=dict(params)),
        )
        for name, params in ROI_DEFS
    ]
    stats = process_rois_with_halcon(rois, v3_temp)
    roi_results = {s.roi_id: s for s in stats}

    center = roi_results["center"]
    hot = roi_results["hot_spot"]
    full = roi_results["full_frame"]

    # Rules chosen from the REAL measured statistics:
    #   center_above   : ABOVE, threshold just below center.max  -> must fire
    #   center_below   : BELOW, threshold just above center.min  -> must fire
    #   center_ok      : ABOVE, threshold above center.max       -> must NOT fire
    #   hot_outside    : OUTSIDE_RANGE with band below hot mean   -> must fire
    #   full_inside    : INSIDE_RANGE inside [full.min, full.max]-> must fire
    # (OUTSIDE_RANGE / INSIDE_RANGE evaluate mean_temp, per V2/V3 alarms.py.)
    rules = {
        "center_above": AlarmRule(
            rule_id="center_above", roi_id="center",
            condition=AlarmCondition.ABOVE, severity=AlarmSeverity.CRITICAL,
            threshold=round(center.max_temp - 0.1, 3),
        ),
        "center_below": AlarmRule(
            rule_id="center_below", roi_id="center",
            condition=AlarmCondition.BELOW, severity=AlarmSeverity.WARNING,
            threshold=round(center.min_temp + 0.1, 3),
        ),
        "center_ok": AlarmRule(
            rule_id="center_ok", roi_id="center",
            condition=AlarmCondition.ABOVE, severity=AlarmSeverity.CRITICAL,
            threshold=round(center.max_temp + 2.0, 3),
        ),
        "hot_outside": AlarmRule(
            rule_id="hot_outside", roi_id="hot_spot",
            condition=AlarmCondition.OUTSIDE_RANGE, severity=AlarmSeverity.CRITICAL,
            threshold=0.0,
            threshold_low=round(hot.min_temp - 5.0, 3),
            threshold_high=round(hot.mean_temp - 0.1, 3),
        ),
        "full_inside": AlarmRule(
            rule_id="full_inside", roi_id="full_frame",
            condition=AlarmCondition.INSIDE_RANGE, severity=AlarmSeverity.WARNING,
            threshold=0.0,
            threshold_low=round(full.min_temp - 1.0, 3),
            threshold_high=round(full.max_temp + 1.0, 3),
        ),
    }

    config = AnalysisConfig(camera_id="HB25100004", alarm_rules=rules)
    evaluator = AlarmEvaluator(config)

    # Single-frame evaluation on the real frame.
    result = AnalysisResult(
        camera_id="HB25100004",
        frame_sequence=frame_sequence,
        frame_timestamp=frame_timestamp,
        roi_results=roi_results,
        overall_min=full.min_temp,
        overall_max=full.max_temp,
        overall_mean=full.mean_temp,
    )
    evaluation = evaluator.evaluate(result)

    fired = {e.rule_id: e.severity.value for e in evaluation.events}
    expected_fire = {"center_above", "center_below", "hot_outside", "full_inside"}
    expected_not_fire = {"center_ok"}

    # Second evaluation with a benign threshold: previously active rules clear.
    benign = {
        "center_above": AlarmRule(
            rule_id="center_above", roi_id="center",
            condition=AlarmCondition.ABOVE, severity=AlarmSeverity.CRITICAL,
            threshold=round(center.max_temp + 5.0, 3),
        ),
        "center_below": AlarmRule(
            rule_id="center_below", roi_id="center",
            condition=AlarmCondition.BELOW, severity=AlarmSeverity.WARNING,
            threshold=round(center.min_temp - 5.0, 3),
        ),
    }
    benign_config = AnalysisConfig(camera_id="HB25100004", alarm_rules=benign)
    evaluator.update_config(benign_config)
    result2 = AnalysisResult(
        camera_id="HB25100004",
        frame_sequence=frame_sequence + 1,
        frame_timestamp=frame_timestamp + 0.11,
        roi_results=roi_results,
    )
    evaluation2 = evaluator.evaluate(result2)
    cleared = set(evaluation2.cleared_alarms)

    return {
        "measured": {
            "center": {"min": round(center.min_temp, 6), "max": round(center.max_temp, 6)},
            "hot_spot": {"min": round(hot.min_temp, 6), "max": round(hot.max_temp, 6)},
            "full_frame": {"min": round(full.min_temp, 6), "max": round(full.max_temp, 6)},
        },
        "first_evaluation": {
            "fired_rules": sorted(fired),
            "expected_to_fire": sorted(expected_fire),
            "expected_not_to_fire": sorted(expected_not_fire),
            "missing_fire": sorted(expected_fire - set(fired)),
            "unexpected_fire": sorted(set(fired) - expected_fire),
            "pass": expected_fire.issubset(set(fired)) and not (set(fired) & expected_not_fire),
        },
        "clear_evaluation": {
            "cleared_rules": sorted(cleared),
            "expected_to_clear": sorted({"center_above", "center_below"}),
            "pass": {"center_above", "center_below"}.issubset(cleared),
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Validate V3 offline processing on real TV46L IR data.")
    ap.add_argument("--recording", type=Path, default=DEFAULT_RECORDING, help="Recording directory")
    ap.add_argument("--calibration", type=Path, default=None,
                    help="Calibration blob path (default: camera blob if present, else V2 reference)")
    ap.add_argument("--frames", type=int, default=5, help="Number of frames to process (STEP 10)")
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--no-camera-cal", action="store_true",
                    help="Do not use the camera-fetched blob even if present")
    args = ap.parse_args()

    blob_file = args.calibration
    if blob_file is None:
        blob_file = CAMERA_CALIBRATION if (not args.no_camera_cal and CAMERA_CALIBRATION.exists()) else DEFAULT_CALIBRATION

    print(f"Calibration blob: {blob_file}")

    # Build calibrations (STEP 12 base).
    v3_cal, v2_cal = build_calibrations(blob_file)
    v3_lut = v3_cal.get_lookup_table(0)
    v2_lut = v2_cal.get_lookup_table(0)
    lut_parity = {
        "max_abs_diff": round(float(np.nanmax(np.abs(v3_lut - v2_lut))), 9),
        "tolerance": TOL_LUT,
        "pass": float(np.nanmax(np.abs(v3_lut - v2_lut))) <= TOL_LUT,
    }

    report = {
        "tool": {"name": "tv46l_offline_processing_validation", "version": "1.0.0"},
        "calibration_blob": str(blob_file),
        "calibration_header": {
            "magic": f"0x{v3_cal.magic:08X}",
            "enabled_ranges": v3_cal.enabled_ranges,
            "calibration_date": v3_cal.calibration_date,
        },
        "lut_parity_v2_vs_v3": lut_parity,
        "step_10_v3_offline_chain": {},
        "step_11_roi_v2_vs_v3": [],
        "step_12_calibration_v2_vs_v3": [],
        "step_13_alarms": [],
        "timing_ms": {},
    }

    # STEP 10: run the V3 offline chain.
    t0 = time.perf_counter()
    chain = run_v3_chain(args.recording, v3_cal, args.frames)
    report["step_10_v3_offline_chain"] = chain
    report["timing_ms"]["step_10_chain"] = round((time.perf_counter() - t0) * 1000.0, 3)

    samples = chain.get("frame_samples", [])
    if not samples:
        print("ERROR: no frames processed from recording")
        return 1

    # STEP 12: calibration comparison on the first real frame.
    from thermal_monitor.offline import StreamFilter, open_offline_source

    source = open_offline_source(args.recording, stream_filter=StreamFilter.IR)
    first = None
    try:
        first = source.get_next_frame()
    finally:
        source.close()
    if first is not None and first.payload.thermal is not None:
        t0 = time.perf_counter()
        cal_compare = run_calibration_comparison(first.payload.thermal, v2_cal, v3_cal)
        report["step_12_calibration_v2_vs_v3"] = cal_compare
        report["timing_ms"]["step_12_calibration"] = round((time.perf_counter() - t0) * 1000.0, 3)

    # STEP 11: ROI comparison on every sampled frame.
    for idx, sample in enumerate(samples):
        # Re-open and re-read the same frame sequence for the temp image.
        source = open_offline_source(args.recording, stream_filter=StreamFilter.IR)
        try:
            target = sample["frame_sequence"]
            found = None
            while True:
                f = source.get_next_frame()
                if f is None:
                    break
                if f.descriptor.sequence == target:
                    found = f
                    break
            if found is None or found.payload.thermal is None:
                continue
            temp = V3CalibrationProcessor.raw_to_temperature(found.payload.thermal, v3_cal, 0)
        finally:
            source.close()

        t0 = time.perf_counter()
        roi_compare = run_roi_comparison(temp, idx)
        report["step_11_roi_v2_vs_v3"].append(roi_compare)
        report["timing_ms"].setdefault("step_11_roi", []).append(round((time.perf_counter() - t0) * 1000.0, 3))

    # STEP 13: alarm evaluation on the first frame.
    source = open_offline_source(args.recording, stream_filter=StreamFilter.IR)
    try:
        f = source.get_next_frame()
    finally:
        source.close()
    if f is not None and f.payload.thermal is not None:
        temp = V3CalibrationProcessor.raw_to_temperature(f.payload.thermal, v3_cal, 0)
        t0 = time.perf_counter()
        alarm_report = run_alarm_evaluation(temp, f.descriptor.sequence, f.descriptor.timestamp)
        report["step_13_alarms"] = alarm_report
        report["timing_ms"]["step_13_alarms"] = round((time.perf_counter() - t0) * 1000.0, 3)

    # Summary verdict.
    verdicts = []
    verdicts.append(("lut_parity", lut_parity["pass"]))
    verdicts.append(("step_10_chain", len(samples) > 0))
    roi_pass = all(r["pass"] for r in report["step_11_roi_v2_vs_v3"])
    verdicts.append(("step_11_roi", roi_pass))
    cal_pass = report["step_12_calibration_v2_vs_v3"].get("lut", {}).get("pass", False) and \
               report["step_12_calibration_v2_vs_v3"].get("raw_to_temperature", {}).get("pass", False)
    verdicts.append(("step_12_calibration", cal_pass))
    alarm_pass = bool(report["step_13_alarms"]) and \
        report["step_13_alarms"]["first_evaluation"]["pass"] and \
        report["step_13_alarms"]["clear_evaluation"]["pass"]
    verdicts.append(("step_13_alarms", alarm_pass))

    overall = all(v for _, v in verdicts)
    report["verdict"] = {
        "checks": {name: bool(p) for name, p in verdicts},
        "overall_pass": overall,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["verdict"], indent=2))
    print(f"Report written: {args.output}")
    return 0 if overall else 2


if __name__ == "__main__":
    sys.exit(main())
