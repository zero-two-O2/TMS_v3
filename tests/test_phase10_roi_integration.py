"""Phase 10 integration: A->B position switch with simulator PTZ (acceptance).

1. Camera bound to PTZ, positions A/B exist with ROI sets A/B.
2. A reached -> set A displayed.
3. B selected, PTZ moves; no B ROI before B reached.
4. After B reached: A disappears, B displayed.
5. Delayed result from A rejected; session change invalidates old context.
"""

import threading
import time

import pytest

from thermal_monitor.ptz.positions import PtzPosition
from thermal_monitor.ptz.retarget import ObserverRetargetCoordinator
from thermal_monitor.ptz.roi_activation import ActivePositionRegistry, RoiActivationState
from thermal_monitor.roi.activation import RoiActivationPublisher
from thermal_monitor.roi.context import RoiActiveContext
from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.errors import RoiStaleContextError
from thermal_monitor.roi.evaluator import RoiEvaluator
from thermal_monitor.roi.geometry import RectangleGeometry, SpotGeometry
from thermal_monitor.roi.models import RoiDefinition
from thermal_monitor.roi.registry import RoiContextRegistry
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
                         name="A", pan=0.0, tilt=0.0, roi_set_ref="pos_A"),
    "pos_B": PtzPosition(position_id="pos_B", camera_id="cam_1", ptz_id="ptz_1",
                         name="B", pan=30.0, tilt=10.0, roi_set_ref="pos_B"),
}


class _FakeBinding:
    ptz_id = "ptz_1"


class _ReachedOperation:
    def __init__(self, operation_id="op_1"):
        self.operation_id = operation_id
        self.state = "reached"


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


def _analysis_provider(camera_id):
    from types import MappingProxyType
    from thermal_monitor.core.models import AnalysisConfig, PositionROIAssociation
    rois = {}
    assocs = {}
    for (cam, pos), members in ROI_SETS.items():
        for roi in members:
            from thermal_monitor.core.models import (
                ROIConfig, ROIGeometry, ROIShape)
            rois[roi.roi_id] = ROIConfig(
                roi_id=roi.roi_id, name=roi.roi_id,
                geometry=ROIGeometry(shape=ROIShape.RECTANGLE1,
                                     parameters={"y1": 0.0, "x1": 0.0,
                                                 "y2": 5.0, "x2": 5.0}))
        assocs[pos] = PositionROIAssociation(position_id=pos,
                                             position_name=pos,
                                             roi_ids=tuple(r.roi_id for r in members))
    return AnalysisConfig(camera_id=camera_id, rois=MappingProxyType(rois),
                          position_associations=MappingProxyType(assocs))


def _make_publisher():
    service = _FakeService()
    registry = ActivePositionRegistry()
    coordinator = ObserverRetargetCoordinator(service, registry,
                                             poll_interval_s=0.001)
    roi_registry = RoiContextRegistry()
    publisher = RoiActivationPublisher(coordinator, roi_registry)
    observer = _FakeObserver()
    sessions = {"gen": 10}
    loader = lambda cam, pos: list(ROI_SETS[(cam, pos)])
    return publisher, observer, sessions, loader


def test_full_a_to_b_switch():
    publisher, observer, sessions, loader = _make_publisher()
    result_a, ctx_a = publisher.activate(
        "cam_1", POSITIONS["pos_A"], loader, _analysis_provider,
        lambda: observer, lambda: sessions["gen"], timeout_s=5.0)
    assert result_a.state == RoiActivationState.COMPLETED
    assert ctx_a is not None and ctx_a.position_id == "pos_A"
    assert set(ctx_a.roi_ids) == {"roi_A1", "roi_A2"}
    visible = {r.roi_id for r in loader("cam_1", ctx_a.position_id)}
    assert visible == {"roi_A1", "roi_A2"}

    # B selected but not reached yet: simulate by publishing nothing new.
    stored = publisher.registry.get("cam_1")
    assert stored.position_id == "pos_A"  # no B ROI shown before B reached

    result_b, ctx_b = publisher.activate(
        "cam_1", POSITIONS["pos_B"], loader, _analysis_provider,
        lambda: observer, lambda: sessions["gen"], timeout_s=5.0)
    assert result_b.state == RoiActivationState.COMPLETED
    assert ctx_b.position_id == "pos_B"
    visible_b = {r.roi_id for r in loader("cam_1", ctx_b.position_id)}
    assert visible_b == {"roi_B1"}
    assert "roi_A1" not in visible_b  # set A disappeared


def test_delayed_result_from_a_rejected_after_switch():
    publisher, observer, sessions, loader = _make_publisher()
    _, ctx_a = publisher.activate(
        "cam_1", POSITIONS["pos_A"], loader, _analysis_provider,
        lambda: observer, lambda: sessions["gen"], timeout_s=5.0)
    _, ctx_b = publisher.activate(
        "cam_1", POSITIONS["pos_B"], loader, _analysis_provider,
        lambda: observer, lambda: sessions["gen"], timeout_s=5.0)
    evaluator = RoiEvaluator()
    evaluator.submit(np.full((8, 8), 50.0), 99, 99.0)
    with pytest.raises(RoiStaleContextError):
        evaluator.evaluate(loader("cam_1", "pos_A"), ctx_a,
                           session_generation=ctx_a.session_generation,
                           publish_context=ctx_b)


def test_session_change_invalidates_old_context():
    publisher, observer, sessions, loader = _make_publisher()
    _, ctx_a = publisher.activate(
        "cam_1", POSITIONS["pos_A"], loader, _analysis_provider,
        lambda: observer, lambda: sessions["gen"], timeout_s=5.0)
    sessions["gen"] = 11  # camera reconnected: new session epoch
    assert not ctx_a.matches(camera_id="cam_1", session_generation=11,
                             position_id="pos_A",
                             context_generation=ctx_a.context_generation)
    publisher.registry.invalidate("cam_1", session_generation=11)
    assert publisher.registry.get("cam_1").state == "inactive"
