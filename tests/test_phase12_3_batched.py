"""Phase 12.3 C+D+E: batched HALCON stats, regression, numerical parity.

C: one image conversion per frame, batched intensity/min/max/area/center,
   tuple alignment, mixed shapes, empty/OOB/NaN/invalid, length mismatch.
D: ROIStatistics/AnalysisResult/consumer/alarm compat, stale protection,
   camera/position/ROI-generation switches, no-ROI position, HALCON errors.
E: HALCON vs NumPy reference parity (rel 1e-5 where float32 accumulation
   applies -- see ADR-016/017).
"""

import math

import numpy as np
import pytest

from thermal_monitor.core.models import (
    ROIConfig,
    ROIGeometry,
    ROIShape,
    ROIStatistics,
    TemperatureUnit,
)

ha = pytest.importorskip("halcon", reason="HALCON binding not installed")

from thermal_monitor.processing.halcon import (  # noqa: E402
    HalconROIAdapter,
    process_rois_with_halcon,
)


def _rect(rid, y1, x1, y2, x2, name="r"):
    return ROIConfig(
        roi_id=rid, name=name,
        geometry=ROIGeometry(
            shape=ROIShape.RECTANGLE1,
            parameters={"y1": y1, "x1": x1, "y2": y2, "x2": x2}),
    )


def _circle(rid, cy, cx, radius):
    return ROIConfig(
        roi_id=rid, name="c",
        geometry=ROIGeometry(
            shape=ROIShape.CIRCLE,
            parameters={"center_y": cy, "center_x": cx, "radius": radius}),
    )


def _flat(value=42.0, h=480, w=640):
    return np.full((h, w), value, dtype=np.float32)


CTX = {"camera_id": "cam1", "position_id": "p1", "context_generation": 0}


def _frame(camera_id, sequence, thermal):
    from thermal_monitor.core.frame import (
        Frame,
        FrameDescriptor,
        FramePayload,
        StreamMetadata,
        SyncInfo,
        SyncStatus,
    )
    h, w = thermal.shape
    meta = StreamMetadata(present=True, width=w, height=h,
                          pixel_format="IR_Data", dtype="uint16",
                          byte_count=thermal.nbytes)
    frozen = thermal.copy()
    frozen.setflags(write=False)
    return Frame(
        descriptor=FrameDescriptor(
            camera_id=camera_id, sequence=sequence, timestamp=1.0,
            monotonic_timestamp=1.0, thermal=meta,
            visible=StreamMetadata(present=False),
            sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE)),
        payload=FramePayload(thermal=frozen),
    )


# ---------------- Group C: batched statistics ----------------

def test_one_image_conversion_per_frame(monkeypatch):
    calls = []
    real = ha.himage_from_numpy_array
    monkeypatch.setattr(ha, "himage_from_numpy_array",
                        lambda arr: (calls.append(arr.shape), real(arr))[1])
    img = _flat()
    rois = [_rect(f"r{i}", i * 4, 0, i * 4 + 2, 10) for i in range(10)]
    stats = process_rois_with_halcon(rois, img, **CTX)
    assert len(stats) == 10
    assert len(calls) == 1  # exactly one conversion for 10 ROIs


def test_batched_rectangle1_stats_and_area_center():
    img = np.zeros((480, 640), dtype=np.float32)
    img[100:200, 100:200] = 50.0
    stats = process_rois_with_halcon(
        [_rect("a", 100, 100, 199, 199)], img, **CTX)
    (s,) = stats
    assert (s.roi_id, s.roi_name) == ("a", "r")
    assert s.mean_temp == pytest.approx(50.0, abs=1e-6)
    assert s.min_temp == pytest.approx(50.0, abs=1e-6)
    assert s.max_temp == pytest.approx(50.0, abs=1e-6)
    assert s.deviation == pytest.approx(0.0, abs=1e-6)
    assert s.area == pytest.approx(10000, abs=1e-6)
    assert s.center_row == pytest.approx(149.5, abs=1e-6)
    assert s.center_col == pytest.approx(149.5, abs=1e-6)
    assert s.valid is True and s.error == ""
    assert s.unit == TemperatureUnit.CELSIUS


def test_positional_mapping_with_interleaved_shapes():
    img = np.zeros((480, 640), dtype=np.float32)
    img[100:200, 100:200] = 50.0
    img[300:400, 300:400] = 100.0
    rois = [_rect("b", 300, 300, 399, 399),
            _circle("c", 350.0, 350.0, 10.0),
            _rect("a", 100, 100, 199, 199)]
    stats = process_rois_with_halcon(rois, img, **CTX)
    assert [s.roi_id for s in stats] == ["b", "c", "a"]
    by_id = {s.roi_id: s for s in stats}
    assert by_id["b"].mean_temp == pytest.approx(100.0, abs=1e-4)
    assert by_id["a"].mean_temp == pytest.approx(50.0, abs=1e-6)
    assert by_id["c"].mean_temp == pytest.approx(100.0, abs=1e-4)


def test_empty_roi_set_without_halcon_calls():
    assert process_rois_with_halcon([], _flat(), **CTX) == []


def test_out_of_bounds_region_clipped():
    img = np.arange(480 * 640, dtype=np.float32).reshape(480, 640) % 50 + 20.0
    stats = process_rois_with_halcon(
        [_rect("oob", 470, 630, 500, 700)], img, **CTX)
    (s,) = stats
    expected = img[470:480, 630:640]
    assert s.valid is True
    assert s.mean_temp == pytest.approx(float(np.mean(expected)), rel=1e-5)


def test_nan_region_marked_invalid_not_zero():
    img = _flat(30.0)
    img[10:20, 10:20] = np.nan
    stats = process_rois_with_halcon([_rect("n", 10, 10, 19, 19)], img, **CTX)
    (s,) = stats
    assert s.valid is False
    assert math.isnan(s.mean_temp) and math.isnan(s.min_temp)
    assert s.error != ""
    # Must never look like a real 0 C reading.
    assert s.mean_temp != 0.0 or math.isnan(s.mean_temp)


def test_invalid_geometry_marked_invalid():
    class Bad:
        roi_id = "bad"
        name = "bad"
        enabled = True

        class geometry:
            shape = ROIShape.CIRCLE
            parameters = {"center_y": 1.0, "center_x": 1.0, "radius": 0.0}

    stats = process_rois_with_halcon([Bad()], _flat(), **CTX)
    (s,) = stats
    assert s.valid is False and s.roi_id == "bad"


def test_tuple_length_mismatch_marked_invalid(monkeypatch):
    real_intensity = ha.intensity
    monkeypatch.setattr(ha, "intensity", lambda regions, image: ([1.0], [0.0]))
    stats = process_rois_with_halcon(
        [_rect("a", 0, 0, 4, 4), _rect("b", 10, 10, 14, 14)],
        _flat(), **CTX)
    assert len(stats) == 2
    assert all(s.valid is False for s in stats)
    assert all("mismatch" in s.error for s in stats)
    monkeypatch.undo()
    assert ha.intensity is real_intensity


def test_compat_extract_statistics_is_batched(monkeypatch):
    """Legacy (regions, image, rois) entry point: no per-ROI loop."""
    select_calls = []
    real_select = ha.select_obj
    monkeypatch.setattr(
        ha, "select_obj",
        lambda *a, **k: (select_calls.append(a), real_select(*a, **k))[1])
    ad = HalconROIAdapter()
    img = _flat(25.0)
    rois = [_rect("a", 0, 0, 9, 9), _rect("b", 20, 20, 29, 29)]
    regions = ad.generate_regions(rois)
    try:
        stats = ad.extract_statistics(regions, img, rois=rois)
    finally:
        ad.release_regions(regions)
    assert [s.roi_id for s in stats] == ["a", "b"]
    assert all(s.mean_temp == pytest.approx(25.0, abs=1e-6) for s in stats)
    assert select_calls == []


# ---------------- Group D: regression ----------------

def test_statistics_contract_compatible():
    s = ROIStatistics(roi_id="x", roi_name="X", min_temp=1.0, max_temp=2.0,
                      mean_temp=1.5, deviation=0.5)
    assert s.range_temp == pytest.approx(1.0)
    assert s.std_temp == pytest.approx(0.5)
    assert s.valid is True and s.area is None


def test_alarm_compat_valid_and_nan():
    from thermal_monitor.core.models import (
        AlarmCondition,
        AnalysisConfig,
        AnalysisResult,
        AlarmRule,
        AlarmSeverity,
    )
    from thermal_monitor.processing.alarms import AlarmEvaluator
    from types import MappingProxyType

    rule = AlarmRule(rule_id="rule1", roi_id="r1", condition=AlarmCondition.ABOVE,
                     severity=AlarmSeverity.CRITICAL, threshold=80.0, enabled=True)
    config = AnalysisConfig(camera_id="cam1", alarm_rules={"rule1": rule})
    ev = AlarmEvaluator(config=config)
    hot = ROIStatistics(roi_id="r1", roi_name="R", min_temp=90.0, max_temp=95.0,
                        mean_temp=92.0, deviation=1.0)
    res = AnalysisResult(camera_id="cam1", frame_sequence=1, frame_timestamp=1.0,
                         roi_results=MappingProxyType({"r1": hot}))
    assert ev.evaluate(res).active_alarms == ("rule1",)
    # Invalid (NaN) stats must not trigger: comparisons are False -> INFO.
    bad = ROIStatistics(roi_id="r1", roi_name="R", min_temp=float("nan"),
                        max_temp=float("nan"), mean_temp=float("nan"),
                        deviation=float("nan"), valid=False, error="nan")
    res2 = AnalysisResult(camera_id="cam1", frame_sequence=2, frame_timestamp=2.0,
                          roi_results=MappingProxyType({"r1": bad}))
    assert ev.evaluate(res2).active_alarms == ()


def test_pipeline_stale_context_discards(monkeypatch):
    """A retarget landing mid-processing discards ROI results."""
    from thermal_monitor.core.models import AnalysisConfig
    from thermal_monitor.processing import SimpleProcessingPipeline
    import thermal_monitor.processing.pipeline as pl

    real = pl.process_rois_with_halcon

    def flip(*args, **kwargs):
        pipe.set_active_position("p2")  # retarget races the frame
        return real(*args, **kwargs)

    monkeypatch.setattr(pl, "process_rois_with_halcon", flip)
    lut = np.arange(65536, dtype=np.float32)
    roi = _rect("r1", 0, 0, 9, 9)
    from thermal_monitor.core.models import PositionROIAssociation
    config = AnalysisConfig(
        camera_id="cam1", rois={"r1": roi},
        position_associations={"p1": PositionROIAssociation(
            position_id="p1", position_name="P1", roi_ids=("r1",))},
    )

    class Prov:
        def get_calibration(self, camera_id):
            return lut

    from thermal_monitor.processing.temperature import CPUTemperatureConverter
    pipe = SimpleProcessingPipeline(
        config=config, calibration_provider=Prov(),
        temperature_converter=CPUTemperatureConverter())
    pipe.set_active_position("p1")
    thermal = np.zeros((480, 640), dtype=np.uint16)
    frame = _frame("cam1", 7, thermal)
    out = pipe.process_frame(frame)
    assert out.roi_results == {}
    assert out.metadata.get("stale_discarded") is True
    assert pipe.stats.frames_dropped == 1


def test_pipeline_no_roi_position_ok():
    from thermal_monitor.core.models import AnalysisConfig
    from thermal_monitor.processing import SimpleProcessingPipeline
    from thermal_monitor.processing.temperature import CPUTemperatureConverter

    class Prov:
        def get_calibration(self, camera_id):
            return np.arange(65536, dtype=np.float32)

    pipe = SimpleProcessingPipeline(
        config=AnalysisConfig(camera_id="cam1"),
        calibration_provider=Prov(),
        temperature_converter=CPUTemperatureConverter())
    pipe.set_active_position("empty-pos")
    frame = _frame("cam1", 1, np.zeros((480, 640), dtype=np.uint16))
    out = pipe.process_frame(frame)
    assert out.roi_results == {}
    assert out.overall_min is None


def test_halcon_exception_yields_invalid_not_crash(monkeypatch):
    monkeypatch.setattr(
        HalconROIAdapter, "_build_group_regions",
        lambda self, ha_mod, group: (_ for _ in ()).throw(RuntimeError("boom")))
    stats = process_rois_with_halcon([_rect("a", 0, 0, 4, 4)], _flat(), **CTX)
    assert len(stats) == 1 and stats[0].valid is False


# ---------------- Group E: parity ----------------

def test_rectangle1_parity_with_numpy_reference():
    rng = np.random.default_rng(7)
    img = (rng.uniform(20.0, 100.0, size=(480, 640))).astype(np.float32)
    rois = [_rect("a", 100, 100, 199, 199), _rect("b", 0, 0, 9, 9)]
    stats = process_rois_with_halcon(rois, img, **CTX)
    for s, (r1, c1, r2, c2) in zip(stats, [(100, 100, 199, 199), (0, 0, 9, 9)]):
        win = img[r1:r2 + 1, c1:c2 + 1].astype(np.float64)
        assert s.mean_temp == pytest.approx(float(np.mean(win)), rel=1e-5)
        assert s.min_temp == pytest.approx(float(np.min(win)), rel=1e-5)
        assert s.max_temp == pytest.approx(float(np.max(win)), rel=1e-5)
        assert s.deviation == pytest.approx(float(np.std(win)), rel=1e-5)
