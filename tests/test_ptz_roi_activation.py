"""Phase 7: ROI activation workflow tests (fake service, no server)."""

from __future__ import annotations

import threading
import time

import pytest

from thermal_monitor.core.models import (
    AnalysisConfig,
    PositionROIAssociation,
    ROIConfig,
    ROIGeometry,
    ROIShape,
)
from thermal_monitor.ptz.controller import PtzCommandError, PtzOperation, PtzOperationState
from thermal_monitor.ptz.errors import PtzError, PtzErrorCategory
from thermal_monitor.ptz.models import PtzStationBinding
from thermal_monitor.ptz.positions import PtzPosition
from thermal_monitor.ptz.roi_activation import (
    ActivePositionRegistry,
    RoiActivationResult,
    RoiActivationState,
    RoiActivationWorkflow,
)


def make_position(**overrides) -> PtzPosition:
    values = {
        "position_id": "pos_1",
        "camera_id": "cam_A",
        "ptz_id": "PTZ_01",
        "name": "Furnace",
        "pan": 30.0,
        "tilt": -10.0,
        "velocity": 60.0,
        "roi_set_ref": "set_a",
    }
    values.update(overrides)
    return PtzPosition(**values)


def make_analysis() -> AnalysisConfig:
    roi = ROIConfig(
        roi_id="roi_1",
        name="R1",
        geometry=ROIGeometry(
            shape=ROIShape.RECTANGLE1,
            parameters={"y1": 0.0, "x1": 0.0, "y2": 10.0, "x2": 10.0},
        ),
    )
    return AnalysisConfig(
        camera_id="cam_A",
        rois={"roi_1": roi},
        position_associations={
            "set_a": PositionROIAssociation(
                position_id="set_a", position_name="A", roi_ids=("roi_1",)
            )
        },
    )


def reached_op() -> PtzOperation:
    return PtzOperation(
        operation_id="ptzop-1",
        ptz_id="PTZ_01",
        target_pan=30.0,
        target_tilt=-10.0,
        state=PtzOperationState.REACHED,
        started_monotonic=time.monotonic() - 1.0,
        finished_monotonic=time.monotonic(),
    )


class FakeService:
    """Structural PtzService double with scripted outcomes."""

    def __init__(self, outcome="reached") -> None:
        self.outcome = outcome
        self.move_calls: list = []
        self.stops = 0
        self.binding = PtzStationBinding(camera_id="cam_A", ptz_id="PTZ_01")

    def binding_for_camera(self, camera_id):
        if camera_id != "cam_A":
            raise PtzCommandError(
                PtzError(code="x", message="unknown", category=PtzErrorCategory.UNKNOWN)
            )
        return self.binding

    def move_absolute(self, camera_id, pan, tilt, **kwargs):
        self.move_calls.append((pan, tilt, kwargs))
        if self.outcome == "reject":
            raise PtzCommandError(
                PtzError(
                    code="move:rejected", message="nope",
                    category=PtzErrorCategory.COMMAND_REJECTED,
                )
            )
        return PtzOperation(
            operation_id="ptzop-9",
            ptz_id="PTZ_01",
            target_pan=pan,
            target_tilt=tilt,
            state=PtzOperationState.ACCEPTED,
            started_monotonic=time.monotonic(),
        )

    def wait_until_reached(self, camera_id, operation_id, timeout_s):
        if self.outcome == "timeout":
            raise PtzCommandError(
                PtzError(
                    code="move:timeout", message="timed out",
                    category=PtzErrorCategory.MOVEMENT_TIMEOUT,
                )
            )
        if self.outcome == "ptz-error":
            return PtzOperation(
                operation_id=operation_id,
                ptz_id="PTZ_01",
                target_pan=30.0,
                target_tilt=-10.0,
                state=PtzOperationState.FAILED,
                started_monotonic=time.monotonic() - 1.0,
                finished_monotonic=time.monotonic(),
                error=PtzError(
                    code="ptz:1", message="drive fault",
                    category=PtzErrorCategory.PTZ,
                ),
            )
        if self.outcome == "lost":
            raise PtzCommandError(
                PtzError(
                    code="move:lost", message="connection lost",
                    category=PtzErrorCategory.COMMUNICATION,
                )
            )
        if self.outcome == "slow":
            time.sleep(min(timeout_s, 0.3))
            raise PtzCommandError(
                PtzError(
                    code="move:timeout", message="timed out",
                    category=PtzErrorCategory.MOVEMENT_TIMEOUT,
                )
            )
        return reached_op()

    def stop(self, camera_id):
        self.stops += 1
        return reached_op()


def run(service, position=None, **kwargs):
    workflow = RoiActivationWorkflow(service)
    kwargs.setdefault("analysis_config_provider", lambda cam: make_analysis())
    kwargs.setdefault("timeout_s", 5.0)
    return workflow, workflow.run("cam_A", position or make_position(), **kwargs)


class TestSuccess:
    def test_completed_and_registry(self):
        workflow, result = run(FakeService())
        assert result.state == RoiActivationState.COMPLETED
        assert result.roi_ids == ("roi_1",)
        context = workflow.registry.get("cam_A")
        assert context is not None
        assert context.position_id == "pos_1"
        assert context.roi_ids == ("roi_1",)

    def test_empty_roi_ref_activates_empty_set(self):
        workflow, result = run(FakeService(), make_position(roi_set_ref=""))
        assert result.state == RoiActivationState.COMPLETED
        assert result.roi_ids == ()
        assert workflow.registry.get("cam_A").roi_ids == ()

    def test_repeated_activation(self):
        service = FakeService()
        workflow = RoiActivationWorkflow(service)
        first = workflow.run(
            "cam_A", make_position(),
            analysis_config_provider=lambda cam: make_analysis(), timeout_s=5.0,
        )
        second = workflow.run(
            "cam_A", make_position(position_id="pos_2", name="Two"),
            analysis_config_provider=lambda cam: make_analysis(), timeout_s=5.0,
        )
        assert first.state == RoiActivationState.COMPLETED
        assert second.state == RoiActivationState.COMPLETED
        assert workflow.registry.get("cam_A").position_id == "pos_2"


class TestFailures:
    def test_binding_mismatch(self):
        _, result = run(FakeService(), make_position(ptz_id="PTZ_02"))
        assert result.state == RoiActivationState.FAILED
        assert "PTZ_02" in (result.error.message if result.error else "")

    def test_unknown_camera(self):
        workflow = RoiActivationWorkflow(FakeService())
        result = workflow.run(
            "cam_ZZZ", make_position(),
            analysis_config_provider=lambda cam: make_analysis(), timeout_s=5.0,
        )
        assert result.state == RoiActivationState.FAILED

    def test_move_rejected(self):
        _, result = run(FakeService(outcome="reject"))
        assert result.state == RoiActivationState.FAILED
        assert result.error is not None
        assert result.error.category == PtzErrorCategory.COMMAND_REJECTED

    def test_movement_timeout(self):
        _, result = run(FakeService(outcome="timeout"))
        assert result.state == RoiActivationState.FAILED
        assert result.error.category == PtzErrorCategory.MOVEMENT_TIMEOUT

    def test_ptz_error(self):
        workflow, result = run(FakeService(outcome="ptz-error"))
        assert result.state == RoiActivationState.FAILED
        assert workflow.registry.get("cam_A") is None  # rollback: nothing recorded

    def test_communication_loss(self):
        _, result = run(FakeService(outcome="lost"))
        assert result.state == RoiActivationState.FAILED
        assert result.error.category == PtzErrorCategory.COMMUNICATION

    def test_invalid_roi_reference(self):
        _, result = run(FakeService(), make_position(roi_set_ref="set_missing"))
        assert result.state == RoiActivationState.FAILED
        assert "roi-ref" in (result.error.code if result.error else "")

    def test_missing_analysis_config(self):
        workflow = RoiActivationWorkflow(FakeService())
        result = workflow.run(
            "cam_A", make_position(),
            analysis_config_provider=lambda cam: None, timeout_s=5.0,
        )
        assert result.state == RoiActivationState.FAILED

    def test_failed_keeps_previous_context(self):
        service = FakeService()
        workflow = RoiActivationWorkflow(service)
        ok = workflow.run(
            "cam_A", make_position(),
            analysis_config_provider=lambda cam: make_analysis(), timeout_s=5.0,
        )
        assert ok.state == RoiActivationState.COMPLETED
        service.outcome = "timeout"
        failed = workflow.run(
            "cam_A", make_position(position_id="pos_2"),
            analysis_config_provider=lambda cam: make_analysis(), timeout_s=2.0,
        )
        assert failed.state == RoiActivationState.FAILED
        assert workflow.registry.get("cam_A").position_id == "pos_1"


class TestCancellationAndStaleness:
    def test_cancel_event(self):
        service = FakeService(outcome="slow")
        event = threading.Event()
        event.set()
        _, result = run(service, cancel_event=event)
        assert result.state == RoiActivationState.CANCELLED
        # Cancelled during VALIDATING: nothing was dispatched, so no STOP.
        assert service.stops == 0
        assert service.move_calls == []

    def test_cancel_mid_wait_stops_motion(self):
        service = FakeService(outcome="slow")
        event = threading.Event()
        holder: list = []

        def _run():
            workflow = RoiActivationWorkflow(service)
            holder.append(
                workflow.run(
                    "cam_A", make_position(),
                    analysis_config_provider=lambda cam: make_analysis(),
                    timeout_s=10.0, cancel_event=event,
                )
            )

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        time.sleep(0.2)
        event.set()
        thread.join(timeout=10.0)
        assert holder and holder[0].state == RoiActivationState.CANCELLED
        assert service.stops >= 1

    def test_stale_generation(self):
        _, result = run(FakeService(), is_current=lambda: False)
        assert result.state == RoiActivationState.CANCELLED

    def test_camera_switch_mid_wait_cancels(self):
        current = {"cam": "cam_A"}

        _, result = run(
            FakeService(outcome="slow"),
            is_current=lambda: current["cam"] == "cam_A",
            timeout_s=5.0,
        )
        assert result.state in (
            RoiActivationState.FAILED, RoiActivationState.CANCELLED
        )
