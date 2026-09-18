"""Phase 10F: ROI processing tests (spot/area/line/extrema/stale/latest-wins)."""

import numpy as np
import pytest

from thermal_monitor.roi.context import RoiActiveContext
from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.errors import RoiStaleContextError
from thermal_monitor.roi.evaluator import RoiEvaluator
from thermal_monitor.roi.geometry import (
    HotColdSpotsGeometry,
    HottestSpotGeometry,
    LineGeometry,
    RectangleGeometry,
    SpotGeometry,
)
from thermal_monitor.roi.measurements import (
    area_statistics,
    coldest_in_region,
    hottest_in_region,
    line_profile,
)
from thermal_monitor.roi.models import RoiDefinition


def _ctx(**overrides):
    values = dict(camera_id="cam_1", ptz_id="ptz_1", position_id="pos_A",
                  position_generation=1, session_generation=1,
                  context_generation=1, state="active", roi_ids=("roi_1",))
    values.update(overrides)
    return RoiActiveContext(**values)


def _roi(roi_id, object_type, geometry):
    return RoiDefinition(roi_id=roi_id, camera_id="cam_1", ptz_id="ptz_1",
                         position_id="pos_A", object_type=object_type,
                         geometry=geometry)


def _image():
    img = np.arange(100, dtype=np.float64).reshape(10, 10)
    img.setflags(write=False)
    return img


def test_spot_measurement():
    ev = RoiEvaluator()
    ev.submit(_image(), 1, 10.0)
    out = ev.evaluate([_roi("s", RoiObjectType.SPOT, SpotGeometry(row=2, col=3))],
                      _ctx())
    assert len(out) == 1
    assert out[0].values["value"] == pytest.approx(23.0)
    assert out[0].frame_timestamp == 10.0 and out[0].frame_id == 1


def test_area_statistics_min_max_mean():
    stats = area_statistics(_image(), RectangleGeometry(row1=0, col1=0, row2=1, col2=1))
    assert (stats.min, stats.max, stats.mean, stats.count) == (0.0, 11.0, 5.5, 4)


def test_line_profile_min_max_mean():
    profile = line_profile(_image(), LineGeometry(row1=0, col1=0, row2=0, col2=9))
    assert profile["min"] == pytest.approx(0.0)
    assert profile["max"] == pytest.approx(9.0)
    assert profile["mean"] == pytest.approx(4.5)
    assert profile["count"] == 10


def test_hottest_and_coldest_point():
    hot = hottest_in_region(_image(), 0, 0, 9, 9)
    cold = coldest_in_region(_image(), 0, 0, 9, 9)
    assert (hot["value"], hot["row"], hot["col"]) == (99.0, 9.0, 9.0)
    assert (cold["value"], cold["row"], cold["col"]) == (0.0, 0.0, 0.0)


def test_invalid_no_data_image():
    ev = RoiEvaluator()
    ev.submit(np.full((4, 4), np.nan), 2, 0.0)
    out = ev.evaluate([_roi("s", RoiObjectType.SPOT, SpotGeometry(row=1, col=1))],
                      _ctx())
    assert out[0].valid is False and out[0].error == "no-data"


def test_hottest_spot_no_valid_pixels():
    out = hottest_in_region(np.full((4, 4), np.nan), 0, 0, 3, 3)
    assert out["valid"] is False


def test_stale_result_rejected():
    ev = RoiEvaluator()
    ev.submit(_image(), 1, 0.0)
    old = _ctx(context_generation=1)
    new = _ctx(context_generation=2, position_id="pos_B")
    with pytest.raises(RoiStaleContextError):
        ev.evaluate([_roi("s", RoiObjectType.SPOT, SpotGeometry(row=1, col=1))],
                    old, session_generation=1, publish_context=new)
    assert ev.stats.dropped_stale == 1


def test_latest_frame_wins():
    ev = RoiEvaluator()
    ev.submit(np.zeros((4, 4)), 1, 0.0)
    ev.submit(np.full((4, 4), 42.0), 2, 1.0)  # supersedes frame 1
    out = ev.evaluate([_roi("s", RoiObjectType.SPOT, SpotGeometry(row=0, col=0))],
                      _ctx())
    assert out[0].frame_id == 2
    assert out[0].values["value"] == pytest.approx(42.0)
    assert ev.stats.submitted == 2 and ev.stats.evaluated == 1


def test_bounded_queue_single_slot():
    ev = RoiEvaluator()
    for i in range(100):
        ev.submit(np.full((2, 2), float(i)), i, float(i))
    assert ev._pending[1] == 99  # only the newest frame is retained
    out = ev.evaluate([_roi("s", RoiObjectType.SPOT, SpotGeometry(row=0, col=0))],
                      _ctx())
    assert out[0].frame_id == 99


def test_wrong_position_rois_skipped():
    ev = RoiEvaluator()
    ev.submit(_image(), 1, 0.0)
    foreign = RoiDefinition(roi_id="x", camera_id="cam_1", ptz_id="ptz_1",
                            position_id="pos_OTHER", object_type=RoiObjectType.SPOT,
                            geometry=SpotGeometry(row=1, col=1))
    assert ev.evaluate([foreign], _ctx()) == []


def test_hot_cold_spots_evaluation():
    ev = RoiEvaluator()
    ev.submit(_image(), 1, 0.0)
    roi = _roi("hc", RoiObjectType.HOT_COLD_SPOTS,
               HotColdSpotsGeometry(row1=0, col1=0, row2=9, col2=9))
    out = ev.evaluate([roi], _ctx())
    assert out[0].kind == "hot_cold_spots" and out[0].valid is True
    assert out[0].values["hot"]["value"] == pytest.approx(99.0)


def test_hottest_spot_evaluation():
    ev = RoiEvaluator()
    ev.submit(_image(), 1, 0.0)
    roi = _roi("hs", RoiObjectType.HOTTEST_SPOT,
               HottestSpotGeometry(row1=0, col1=0, row2=2, col2=2))
    out = ev.evaluate([roi], _ctx())
    assert out[0].values["value"] == pytest.approx(22.0)
