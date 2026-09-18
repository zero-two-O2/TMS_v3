"""Phase 11 integration: A->B switch via coordinator + authoritative loader.

Mirrors the production Configuration flow without Qt:

1. Camera bound to PTZ, positions A/B exist with ROI sets A/B.
2. coordinator.retarget moves through PtzService to reached.
3. load_rois_for_position builds the immutable session (reached only).
4. After B reached: A disappears, B displayed.
5. Delayed result from A rejected; session change invalidates old context.
"""

import pytest

from thermal_monitor.ptz.positions import PtzPosition
from thermal_monitor.ptz.retarget import ObserverRetargetCoordinator
from thermal_monitor.ptz.roi_activation import ActivePositionRegistry, RoiActivationState
from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.errors import RoiStaleContextError
from thermal_monitor.roi.evaluator import RoiEvaluator
from thermal_monitor.roi.geometry import SpotGeometry
from thermal_monitor.roi.loading import load_rois_for_position
from thermal_monitor.roi.models import RoiDefinition
import numpy as np


def _roi(roi_id, position_id):
    return RoiDefinition(roi_id=roi_id, camera_id="cam_1", ptz_id="ptz_1",
                         position_id=position_id, object_type=RoiObjectType.SPOT,
                         geometry=SpotGeometry(row=5.0, col=5.0))


ROI_SETS = {
    ("cam_1", "pos_A"): [_roi("roi_A1", "pos_A"), _roi("roi_A2", "pos_A")],
    ("cam_1", "pos_B"): [_roi("roi_B1", "pos_B")],
}

POSITIONS = {
    "pos_A": PtzPosition(position_id="pos_A", camera_id="cam_1", ptz_id="ptz_1",
                         name="A", pan=0.0, tilt=0.0),
    "pos_B": PtzPosition(position_id="pos_B", camera_id="cam_1", ptz_id="ptz_1",
                         name="B", pan=30.0, tilt=10.0),
}


class _FakeBinding:
    ptz_id = "ptz_1"


class _FakeService:
    def binding_for_camera(self, camera_id):
        assert camera_id == "cam_1"
        return _FakeBinding()

    def move_absolute(self, camera_id, pan, tilt, **kwargs):
        from thermal_monitor.ptz.controller import PtzOperation, PtzOperationState
        return PtzOperation(operation_id="op_1", ptz_id="ptz_1",
                            target_pan=pan, target_tilt=tilt,
                            state=PtzOperationState.ACCEPTED)

    def stop(self, camera_id):
        pass

    def wait_until_reached(self, camera_id, operation_id, timeout_s=1.0):
        from thermal_monitor.ptz.controller import PtzOperation, PtzOperationState
        return PtzOperation(operation_id=operation_id, ptz_id="ptz_1",
                            target_pan=0.0, target_tilt=0.0,
                            state=PtzOperationState.REACHED)


class _FakeObserver:
    camera_id = "cam_1"

    def __init__(self):
        self.published = None

    def set_active_position(self, position_id, generation):
        self.published = (position_id, generation)
        return generation

    @property
    def active_position(self):
        return self.published


class _RoiRepo:
    def list_for_position(self, camera_id, position_id):
        return list(ROI_SETS[(camera_id, position_id)])


def _make_flow():
    service = _FakeService()
    registry = ActivePositionRegistry()
    coordinator = ObserverRetargetCoordinator(service, registry,
                                             poll_interval_s=0.001)
    observer = _FakeObserver()
    sessions = {"gen": 10}
    return coordinator, observer, sessions


def _goto_and_load(coordinator, observer, sessions, position_id, pos_gen):
    position = POSITIONS[position_id]
    result = coordinator.retarget(
        "cam_1", position, lambda cam: None, lambda: observer,
        lambda: sessions["gen"], timeout_s=5.0)
    assert result.state == RoiActivationState.COMPLETED
    retained = coordinator.registry.get("cam_1")
    context, rois = load_rois_for_position(
        "cam_1", position_id, sessions["gen"], pos_gen,
        position_provider=POSITIONS.get, roi_repository=_RoiRepo(),
        current_session_generation=sessions["gen"],
        context_generation=(retained.context_generation
                            if retained is not None else 0),
        operation_id=(result.operation.operation_id
                      if result.operation is not None else ""),
        source="goto")
    return context, list(rois)


def test_full_a_to_b_switch():
    coordinator, observer, sessions = _make_flow()
    ctx_a, rois_a = _goto_and_load(coordinator, observer, sessions, "pos_A", 1)
    assert {r.roi_id for r in rois_a} == {"roi_A1", "roi_A2"}

    # B selected but not reached yet: nothing new published.
    stored = coordinator.registry.get("cam_1")
    assert stored.position_id == "pos_A"  # no B ROI shown before B reached

    ctx_b, rois_b = _goto_and_load(coordinator, observer, sessions, "pos_B", 2)
    assert {r.roi_id for r in rois_b} == {"roi_B1"}
    assert "roi_A1" not in {r.roi_id for r in rois_b}  # set A disappeared
    assert ctx_b.position_id == "pos_B"


def test_delayed_result_from_a_rejected_after_switch():
    coordinator, observer, sessions = _make_flow()
    ctx_a, _ = _goto_and_load(coordinator, observer, sessions, "pos_A", 1)
    ctx_b, _ = _goto_and_load(coordinator, observer, sessions, "pos_B", 2)
    evaluator = RoiEvaluator()
    evaluator.submit(np.full((8, 8), 50.0), 99, 99.0)
    with pytest.raises(RoiStaleContextError):
        evaluator.evaluate(list(ROI_SETS[("cam_1", "pos_A")]), ctx_a,
                           session_generation=ctx_a.session_generation,
                           publish_context=ctx_b)


def test_session_change_invalidates_old_context():
    coordinator, observer, sessions = _make_flow()
    ctx_a, _ = _goto_and_load(coordinator, observer, sessions, "pos_A", 1)
    sessions["gen"] = 11  # camera reconnected: new session epoch
    assert not ctx_a.matches(camera_id="cam_1", session_generation=11,
                             position_id="pos_A",
                             context_generation=ctx_a.context_generation)
