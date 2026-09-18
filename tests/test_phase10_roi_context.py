"""Phase 10B/D: binding validation + position context + authoritative loader."""

import pytest

from thermal_monitor.roi.context import RoiActiveContext, inactive_context
from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.errors import RoiBindingError, RoiStaleContextError
from thermal_monitor.roi.geometry import SpotGeometry
from thermal_monitor.roi.loading import load_rois_for_position
from thermal_monitor.roi.models import RoiDefinition
from thermal_monitor.roi.validation import check_position_belongs_to_camera, check_roi_binding


def _roi():
    return RoiDefinition(roi_id="roi_1", camera_id="cam_A", ptz_id="ptz_A",
                         position_id="pos_A", object_type=RoiObjectType.SPOT,
                         geometry=SpotGeometry(row=5.0, col=5.0))


def _context(**overrides):
    values = dict(camera_id="cam_A", ptz_id="ptz_A", position_id="pos_A",
                  position_generation=1, session_generation=3,
                  context_generation=7, state="active", operation_id="roiop_1",
                  roi_ids=("roi_1",))
    values.update(overrides)
    return RoiActiveContext(**values)


def test_roi_bound_to_exactly_one_triple():
    roi = _roi()
    check_roi_binding(roi, camera_id="cam_A", ptz_id="ptz_A", position_id="pos_A")
    with pytest.raises(RoiBindingError):
        check_roi_binding(roi, camera_id="cam_B", ptz_id="ptz_A", position_id="pos_A")
    with pytest.raises(RoiBindingError):
        check_roi_binding(roi, camera_id="cam_A", ptz_id="ptz_X", position_id="pos_A")
    with pytest.raises(RoiBindingError):
        check_roi_binding(roi, camera_id="cam_A", ptz_id="ptz_A", position_id="pos_B")


def test_ptz_mismatch_rejected():
    roi = _roi()
    with pytest.raises(RoiBindingError):
        check_roi_binding(roi, camera_id="cam_A", ptz_id="ptz_OTHER",
                          position_id="pos_A")


def test_unknown_position_rejected_when_known_set_given():
    roi = _roi()
    with pytest.raises(RoiBindingError):
        check_roi_binding(roi, camera_id="cam_A", ptz_id="ptz_A",
                          position_id="pos_NOPE", known_positions={"pos_A"})


class _FakePosition:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_position_from_camera_a_never_saved_under_camera_b():
    pos = _FakePosition(position_id="p1", camera_id="cam_A", ptz_id="ptz_A")
    check_position_belongs_to_camera(pos, "cam_A", "ptz_A")
    with pytest.raises(RoiBindingError):
        check_position_belongs_to_camera(pos, "cam_B", "ptz_A")
    with pytest.raises(RoiBindingError):
        check_position_belongs_to_camera(pos, "cam_A", "ptz_OTHER")


def test_initial_context_is_inactive():
    ctx = inactive_context("cam_A", 1)
    assert ctx.state == "inactive"
    assert not ctx.matches(camera_id="cam_A", session_generation=1,
                           position_id="", context_generation=0)


def test_successful_activation_matches():
    ctx = _context()
    assert ctx.matches(camera_id="cam_A", session_generation=3,
                       position_id="pos_A", context_generation=7)


def test_failed_context_never_matches_new_generation():
    ctx = _context()
    assert not ctx.matches(camera_id="cam_A", session_generation=3,
                           position_id="pos_A", context_generation=8)
    assert not ctx.matches(camera_id="cam_A", session_generation=4,
                           position_id="pos_A", context_generation=7)


def _loader_fixtures():
    positions = {
        "pos_A": _FakePosition(position_id="pos_A", camera_id="cam_A",
                               ptz_id="ptz_A", name="A"),
    }
    rois = [_roi()]

    class _Repo:
        def list_for_position(self, camera_id, position_id):
            assert (camera_id, position_id) == ("cam_A", "pos_A")
            return list(rois)

    return positions, _Repo()


def test_loader_returns_immutable_session():
    positions, repo = _loader_fixtures()
    context, loaded = load_rois_for_position(
        "cam_A", "pos_A", 3, 1,
        position_provider=positions.get, roi_repository=repo,
        current_session_generation=3, context_generation=7,
        operation_id="roiop_1")
    assert context.camera_id == "cam_A" and context.ptz_id == "ptz_A"
    assert context.position_id == "pos_A"
    assert context.session_generation == 3 and context.context_generation == 7
    assert context.roi_ids == ("roi_1",)
    assert [r.roi_id for r in loaded] == ["roi_1"]
    assert context.matches(camera_id="cam_A", session_generation=3,
                           position_id="pos_A", context_generation=7)


def test_loader_rejects_unknown_position():
    positions, repo = _loader_fixtures()
    with pytest.raises(RoiBindingError):
        load_rois_for_position("cam_A", "pos_NOPE", 3, 1,
                               position_provider=positions.get,
                               roi_repository=repo)


def test_loader_rejects_foreign_camera_position():
    positions = {"pos_X": _FakePosition(position_id="pos_X", camera_id="cam_B",
                                        ptz_id="ptz_B", name="X")}
    _, repo = _loader_fixtures()
    with pytest.raises(RoiBindingError):
        load_rois_for_position("cam_A", "pos_X", 3, 1,
                               position_provider=positions.get,
                               roi_repository=repo)


def test_loader_rejects_stale_session():
    positions, repo = _loader_fixtures()
    with pytest.raises(RoiStaleContextError):
        load_rois_for_position("cam_A", "pos_A", 2, 1,
                               position_provider=positions.get,
                               roi_repository=repo,
                               current_session_generation=3)


def test_loader_rejects_foreign_roi_rows():
    positions, repo = _loader_fixtures()

    class _BadRepo:
        def list_for_position(self, camera_id, position_id):
            return [RoiDefinition(
                roi_id="evil", camera_id="cam_A", ptz_id="ptz_A",
                position_id="pos_OTHER", object_type=RoiObjectType.SPOT,
                geometry=SpotGeometry(row=1, col=1))]

    with pytest.raises(RoiBindingError):
        load_rois_for_position("cam_A", "pos_A", 3, 1,
                               position_provider=positions.get,
                               roi_repository=_BadRepo())


def test_loader_requires_ids():
    positions, repo = _loader_fixtures()
    with pytest.raises(RoiBindingError):
        load_rois_for_position("", "pos_A", 3, 1,
                               position_provider=positions.get,
                               roi_repository=repo)
    with pytest.raises(RoiBindingError):
        load_rois_for_position("cam_A", "", 3, 1,
                               position_provider=positions.get,
                               roi_repository=repo)
