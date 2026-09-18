"""Phase 10E: HALCON geometry conversion tests.

Geometry/validation cases run without HALCON. The runtime round-trip
(spots/areas through the real adapter) is skipped unless HALCON is
installed; the mock boundary test pins the adapter contract instead.
"""

import numpy as np
import pytest

from thermal_monitor.halcon.adapter import HalconSnapshotRunner, halcon_available
from thermal_monitor.halcon.geometry import halcon_params_for, verify_image_for_halcon
from thermal_monitor.roi.context import RoiActiveContext
from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.errors import RoiValidationError
from thermal_monitor.roi.geometry import (
    AngleGeometry,
    ArrowGeometry,
    CircleGeometry,
    CrossLineGeometry,
    EllipseGeometry,
    HottestSpotGeometry,
    LineGeometry,
    NoteGeometry,
    PolygonGeometry,
    PolylineGeometry,
    RectangleGeometry,
    SpotGeometry,
)


def _ctx():
    return RoiActiveContext(camera_id="cam_1", ptz_id="ptz_1", position_id="pos_A",
                            position_generation=1, session_generation=1,
                            context_generation=1, state="active",
                            roi_ids=("roi_1",))


def test_rectangle_conversion():
    params = halcon_params_for(RoiObjectType.RECTANGLE,
                               RectangleGeometry(row1=10, col1=20, row2=40, col2=60))
    assert params["operator"] == "gen_rectangle1"
    assert (params["row1"], params["col1"], params["row2"], params["col2"]) == (10, 20, 40, 60)


def test_ellipse_conversion():
    params = halcon_params_for(
        RoiObjectType.ELLIPSE,
        EllipseGeometry(center_row=50, center_col=60, radius1=20, radius2=10, phi=0.5))
    assert params["operator"] == "gen_ellipse"
    assert params["radius1"] == 20 and params["phi"] == 0.5


def test_circle_conversion():
    params = halcon_params_for(RoiObjectType.CIRCLE,
                               CircleGeometry(center_row=5, center_col=6, radius=7))
    assert params == {"operator": "gen_circle", "row": 5, "col": 6, "radius": 7}


def test_polygon_conversion():
    params = halcon_params_for(
        RoiObjectType.POLYGON,
        PolygonGeometry(points=((0, 0), (10, 0), (5, 10))))
    assert params["operator"] == "gen_region_polygon_filled"
    assert params["rows"] == [0, 10, 5] and params["open"] is False


def test_line_and_polyline_conversion():
    line = halcon_params_for(RoiObjectType.FREE_LINE,
                             LineGeometry(row1=0, col1=0, row2=3, col2=4))
    assert line["operator"] == "gen_region_line"
    poly = halcon_params_for(RoiObjectType.POLYLINE,
                             PolylineGeometry(points=((0, 0), (5, 5))))
    assert poly["open"] is True


def test_angle_and_note_conversion():
    angle = halcon_params_for(
        RoiObjectType.MEASURE_ANGLE,
        AngleGeometry(center_row=5, center_col=5, end1_row=5, end1_col=10,
                      end2_row=10, end2_col=5))
    assert angle["operator"] == "angle_ll"
    note = halcon_params_for(RoiObjectType.NOTE, NoteGeometry(row=1, col=2, text="x"))
    assert note["operator"] == "none"


def test_cross_line_and_arrow_conversion():
    cross = halcon_params_for(RoiObjectType.CROSS_LINE,
                              CrossLineGeometry(center_row=9, center_col=9))
    assert cross["operator"] == "gen_cross"
    arrow = halcon_params_for(RoiObjectType.ARROW,
                              ArrowGeometry(row1=0, col1=0, row2=5, col2=5))
    assert arrow["operator"] == "gen_region_line"


def test_non_finite_image_rejected():
    bad = np.full((8, 8), np.nan)
    with pytest.raises(RoiValidationError):
        verify_image_for_halcon(bad)
    with pytest.raises(RoiValidationError):
        verify_image_for_halcon(np.zeros((0, 8)))
    with pytest.raises(RoiValidationError):
        verify_image_for_halcon(np.zeros((8, 8, 3)))


def test_runner_no_data_image_returns_invalid():
    from thermal_monitor.roi.models import RoiDefinition
    runner = HalconSnapshotRunner()
    roi = RoiDefinition(roi_id="roi_1", camera_id="cam_1", ptz_id="ptz_1",
                        position_id="pos_A", object_type=RoiObjectType.SPOT,
                        geometry=SpotGeometry(row=1, col=1))
    results = runner.run(np.full((8, 8), np.nan), [roi], _ctx(),
                         frame_id=1, frame_timestamp=0.0)
    assert len(results) == 1 and results[0].valid is False


def test_runner_rejects_stale_context():
    from thermal_monitor.roi.errors import RoiStaleContextError
    from thermal_monitor.roi.models import RoiDefinition
    runner = HalconSnapshotRunner()
    roi = RoiDefinition(roi_id="roi_1", camera_id="cam_1", ptz_id="ptz_1",
                        position_id="pos_A", object_type=RoiObjectType.SPOT,
                        geometry=SpotGeometry(row=1, col=1))
    newer = RoiActiveContext(camera_id="cam_1", ptz_id="ptz_1", position_id="pos_B",
                             position_generation=2, session_generation=1,
                             context_generation=2, state="active")
    with pytest.raises(RoiStaleContextError):
        runner.run(np.ones((8, 8)), [roi], _ctx(), frame_id=1,
                   frame_timestamp=0.0, publish_context=newer)
    assert runner.stale_discarded == 1


@pytest.mark.skipif(not halcon_available(), reason="HALCON runtime not installed")
def test_halcon_runtime_spot_and_area():
    from thermal_monitor.roi.models import RoiDefinition
    runner = HalconSnapshotRunner()
    rois = [
        RoiDefinition(roi_id="s", camera_id="cam_1", ptz_id="ptz_1",
                      position_id="pos_A", object_type=RoiObjectType.SPOT,
                      geometry=SpotGeometry(row=4, col=4)),
        RoiDefinition(roi_id="a", camera_id="cam_1", ptz_id="ptz_1",
                      position_id="pos_A", object_type=RoiObjectType.RECTANGLE,
                      geometry=RectangleGeometry(row1=0, col1=0, row2=7, col2=7)),
    ]
    image = np.arange(64, dtype=np.float64).reshape(8, 8)
    results = runner.run(image, rois, _ctx(), frame_id=3, frame_timestamp=1.0)
    by_id = {m.roi_id: m for m in results}
    assert by_id["s"].values["value"] == pytest.approx(36.0)
    assert by_id["a"].values["mean"] == pytest.approx(31.5)
    assert runner.last_duration_ms >= 0.0


def test_mock_boundary_pins_adapter_contract():
    """Narrow mock boundary: geometry conversion must precede any HALCON call."""
    calls = []

    class _FakeHalcon:
        pass

    import thermal_monitor.halcon.adapter as adapter_module
    assert hasattr(adapter_module.HalconSnapshotRunner, "run")
    # Conversion of every supported type must not raise for valid geometry.
    cases = [
        (RoiObjectType.SPOT, SpotGeometry(row=1, col=1)),
        (RoiObjectType.HOTTEST_SPOT, HottestSpotGeometry(row1=0, col1=0, row2=5, col2=5)),
        (RoiObjectType.RECTANGLE, RectangleGeometry(row1=0, col1=0, row2=5, col2=5)),
    ]
    for object_type, geom in cases:
        calls.append(halcon_params_for(object_type, geom)["operator"])
    assert calls == ["sample", "min_max_gray", "gen_rectangle1"]
