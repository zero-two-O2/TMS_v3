"""Phase 10A: ROI domain model tests (valid/invalid/binding/serialization)."""

import pytest

from thermal_monitor.roi.enums import RoiCategory, RoiObjectType, category_of
from thermal_monitor.roi.errors import RoiBindingError, RoiValidationError
from thermal_monitor.roi.geometry import (
    CircleGeometry,
    LineGeometry,
    PolygonGeometry,
    RectangleGeometry,
    SpotGeometry,
    geometry_from_dict,
)
from thermal_monitor.roi.models import RoiDefinition, generate_roi_id
from thermal_monitor.roi.serialization import roi_from_dict, roi_to_dict


def _roi(**overrides):
    values = {
        "roi_id": "roi_001", "camera_id": "cam_1", "ptz_id": "ptz_1",
        "position_id": "pos_A",
        "object_type": RoiObjectType.RECTANGLE,
        "geometry": RectangleGeometry(row1=10.0, col1=10.0, row2=50.0, col2=60.0),
        "name": "Heater",
    }
    values.update(overrides)
    return RoiDefinition(**values)


def test_valid_geometry_per_type():
    assert SpotGeometry(row=1.0, col=2.0)
    assert CircleGeometry(center_row=5.0, center_col=5.0, radius=3.0)
    assert LineGeometry(row1=0.0, col1=0.0, row2=0.0, col2=10.0)


def test_invalid_coordinates_rejected():
    with pytest.raises(RoiValidationError):
        SpotGeometry(row=float("nan"), col=1.0)
    with pytest.raises(RoiValidationError):
        SpotGeometry(row=float("inf"), col=1.0)
    with pytest.raises(RoiValidationError):
        CircleGeometry(center_row=0.0, center_col=0.0, radius=0.0)
    with pytest.raises(RoiValidationError):
        CircleGeometry(center_row=0.0, center_col=0.0, radius=-2.0)


def test_degenerate_geometry_rejected():
    with pytest.raises(RoiValidationError):
        RectangleGeometry(row1=10.0, col1=10.0, row2=10.0, col2=10.0)
    with pytest.raises(RoiValidationError):
        LineGeometry(row1=5.0, col1=5.0, row2=5.0, col2=5.4)
    with pytest.raises(RoiValidationError):
        PolygonGeometry(points=((0.0, 0.0), (1.0, 1.0)))
    with pytest.raises(RoiValidationError):
        PolygonGeometry(points=((0.0, 0.0), (0.0, 0.0), (0.0, 0.0)))


def test_geometry_type_mismatch_rejected():
    with pytest.raises(RoiValidationError):
        RoiDefinition(roi_id="r", camera_id="c", ptz_id="p", position_id="q",
                      object_type=RoiObjectType.CIRCLE,
                      geometry=RectangleGeometry(row1=0, col1=0, row2=5, col2=5))


def test_binding_required():
    with pytest.raises(RoiBindingError):
        RoiDefinition(roi_id="r", camera_id="", ptz_id="p", position_id="q",
                      object_type=RoiObjectType.SPOT,
                      geometry=SpotGeometry(row=1.0, col=1.0))


def test_all_twenty_types_have_category():
    assert len(list(RoiObjectType)) == 22
    for object_type in RoiObjectType:
        assert category_of(object_type) in RoiCategory


def test_alarm_ref_only_for_analysis():
    roi = _roi(object_type=RoiObjectType.SPOT,
               geometry=SpotGeometry(row=1.0, col=1.0), alarm_rule_ref="rule_1")
    assert roi.alarm_rule_ref == "rule_1"
    with pytest.raises(RoiValidationError):
        _roi(object_type=RoiObjectType.NOTE,
             geometry=__import__("thermal_monitor.roi.geometry", fromlist=["NoteGeometry"]).NoteGeometry(
                 row=1.0, col=1.0, text="hi"),
             alarm_rule_ref="rule_1")


def test_ownership_immutable_in_place():
    roi = _roi()
    with pytest.raises(RoiBindingError):
        roi.with_updated(camera_id="cam_2")
    moved = roi.copy_to(camera_id="cam_2", ptz_id="ptz_2", position_id="pos_B")
    assert moved.camera_id == "cam_2" and moved.roi_id != roi.roi_id


def test_serialization_round_trip():
    roi = _roi()
    restored = roi_from_dict(roi_to_dict(roi))
    assert restored == roi


def test_schema_version_rejected():
    payload = roi_to_dict(_roi())
    payload["schema_version"] = 999
    with pytest.raises(RoiValidationError):
        roi_from_dict(payload)


def test_unknown_object_type_rejected():
    payload = roi_to_dict(_roi())
    payload["object_type"] = "teleporter"
    with pytest.raises(RoiValidationError):
        roi_from_dict(payload)


def test_geometry_from_dict_dispatch():
    g = geometry_from_dict(RoiObjectType.SPOT, {"row": 3.0, "col": 4.0})
    assert isinstance(g, SpotGeometry)
    with pytest.raises(RoiValidationError):
        geometry_from_dict(RoiObjectType.SPOT, {"row": 3.0})


def test_generate_roi_id_unique():
    assert generate_roi_id() != generate_roi_id()
