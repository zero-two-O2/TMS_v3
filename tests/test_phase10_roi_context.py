"""Phase 10B/D: binding validation + position context tests."""

import threading

import pytest

from thermal_monitor.ptz.errors import PtzStateError
from thermal_monitor.roi.context import RoiActiveContext, inactive_context
from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.errors import RoiBindingError, RoiStaleContextError
from thermal_monitor.roi.geometry import RectangleGeometry, SpotGeometry
from thermal_monitor.roi.models import RoiDefinition
from thermal_monitor.roi.registry import RoiContextRegistry
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


def test_registry_rejects_stale_session():
    registry = RoiContextRegistry()
    registry.publish(_context())
    with pytest.raises(RoiStaleContextError):
        registry.publish(_context(session_generation=2),
                         current_session_generation=3)


def test_registry_rejects_older_operation():
    registry = RoiContextRegistry()
    registry.claim_operation("cam_A", "roiop_9")
    with pytest.raises(RoiStaleContextError):
        registry.publish(_context(operation_id="roiop_1"))


def test_second_retarget_supersedes_first():
    registry = RoiContextRegistry()
    registry.publish(_context(operation_id="roiop_1", context_generation=1))
    registry.claim_operation("cam_A", "roiop_2")
    registry.publish(_context(operation_id="roiop_2", context_generation=2,
                              position_id="pos_B"))
    stored = registry.get("cam_A")
    assert stored.position_id == "pos_B"
    assert stored.context_generation == 2


def test_previous_context_preserved_after_failed_publish():
    registry = RoiContextRegistry()
    registry.publish(_context())
    with pytest.raises(RoiStaleContextError):
        registry.publish(_context(session_generation=99),
                         current_session_generation=3)
    assert registry.get("cam_A").session_generation == 3


def test_camera_disconnect_invalidates_context():
    registry = RoiContextRegistry()
    registry.publish(_context())
    registry.invalidate("cam_A", session_generation=4)
    stored = registry.get("cam_A")
    assert stored.state == "inactive"
    assert not stored.matches(camera_id="cam_A", session_generation=3,
                              position_id="pos_A", context_generation=7)


def test_no_partial_publication_under_concurrency():
    registry = RoiContextRegistry()
    registry.publish(_context(operation_id="roiop_0", context_generation=0))
    errors = []

    def _publish(op, gen):
        try:
            registry.claim_operation("cam_A", op)
            registry.publish(_context(operation_id=op, context_generation=gen))
        except RoiStaleContextError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=_publish, args=(f"roiop_{i}", i))
               for i in range(1, 6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stored = registry.get("cam_A")
    assert stored is not None and stored.state == "active"


def test_shutdown_rejects_writes():
    registry = RoiContextRegistry()
    registry.shutdown()
    with pytest.raises(PtzStateError):
        registry.publish(_context())
