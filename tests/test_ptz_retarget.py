"""Phase 8: registry contract, retarget coordinator, pipeline override."""

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
from thermal_monitor.ptz.errors import PtzError, PtzErrorCategory, PtzStateError
from thermal_monitor.ptz.models import PtzStationBinding
from thermal_monitor.ptz.positions import PtzPosition
from thermal_monitor.ptz.retarget import ObserverRetargetCoordinator
from thermal_monitor.ptz.roi_activation import (
    ActivePositionContext,
    ActivePositionRegistry,
    RoiActivationState,
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


def reached_op(op_id="ptzop-1") -> PtzOperation:
    return PtzOperation(
        operation_id=op_id,
        ptz_id="PTZ_01",
        target_pan=30.0,
        target_tilt=-10.0,
        state=PtzOperationState.REACHED,
        started_monotonic=time.monotonic() - 1.0,
        finished_monotonic=time.monotonic(),
    )


class FakeService:
    def __init__(self, outcome="reached") -> None:
        self.outcome = outcome
        self.move_calls = 0
        self.stops = 0

    def binding_for_camera(self, camera_id):
        return PtzStationBinding(camera_id="cam_A", ptz_id="PTZ_01")

    def move_absolute(self, camera_id, pan, tilt, **kwargs):
        self.move_calls += 1
        if self.outcome == "reject":
            raise PtzCommandError(
                PtzError(code="m", message="no", category=PtzErrorCategory.COMMAND_REJECTED)
            )
        return PtzOperation(
            operation_id=f"ptzop-{self.move_calls}",
            ptz_id="PTZ_01",
            target_pan=pan,
            target_tilt=tilt,
            state=PtzOperationState.ACCEPTED,
            started_monotonic=time.monotonic(),
        )

    def wait_until_reached(self, camera_id, operation_id, timeout_s):
        if self.outcome == "timeout":
            raise PtzCommandError(
                PtzError(code="t", message="timeout", category=PtzErrorCategory.MOVEMENT_TIMEOUT)
            )
        if self.outcome == "slow":
            time.sleep(min(timeout_s, 0.4))
            raise PtzCommandError(
                PtzError(code="t", message="timeout", category=PtzErrorCategory.MOVEMENT_TIMEOUT)
            )
        return reached_op(operation_id)

    def stop(self, camera_id):
        self.stops += 1
        return reached_op()


class FakeObserver:
    """Structural observer double with the Phase 8 pass-through contract."""

    def __init__(self, camera_id="cam_A") -> None:
        self.camera_id = camera_id
        self._position = ("default", 0)
        self._lock = threading.Lock()

    def set_active_position(self, position_id, generation=None):
        with self._lock:
            gen = self._position[1] + 1 if generation is None else generation
            self._position = (position_id, gen)
            return gen

    @property
    def active_position(self):
        with self._lock:
            return self._position


def run_coordinator(service, position=None, observer=None, **kwargs):
    coordinator = ObserverRetargetCoordinator(service)
    kwargs.setdefault("analysis_config_provider", lambda cam: make_analysis())
    kwargs.setdefault("observer_provider", lambda: observer)
    kwargs.setdefault("session_generation_provider", lambda: 7)
    kwargs.setdefault("timeout_s", 5.0)
    result = coordinator.retarget("cam_A", position or make_position(), **kwargs)
    return coordinator, result


class TestRegistryContract:
    def test_initial_empty(self):
        assert ActivePositionRegistry().get("cam_A") is None

    def test_set_get_snapshot(self):
        registry = ActivePositionRegistry()
        context = ActivePositionContext(
            camera_id="cam_A", ptz_id="PTZ_01", position_id="pos_1",
            position_name="F", roi_set_ref="set_a", roi_ids=("roi_1",),
            context_generation=3, session_generation=7,
        )
        registry.set(context)
        fetched = registry.get("cam_A")
        assert fetched == context
        assert fetched.context_generation == 3

    def test_generation_mismatch_rejected(self):
        registry = ActivePositionRegistry()
        context = ActivePositionContext(
            camera_id="cam_A", ptz_id="PTZ_01", position_id="pos_1",
            position_name="F", roi_set_ref="",
        )
        with pytest.raises(PtzStateError):
            registry.set(context, session_generation=6, current_session_generation=7)
        assert registry.get("cam_A") is None

    def test_generation_match_accepted(self):
        registry = ActivePositionRegistry()
        context = ActivePositionContext(
            camera_id="cam_A", ptz_id="PTZ_01", position_id="pos_1",
            position_name="F", roi_set_ref="",
        )
        registry.set(context, session_generation=7, current_session_generation=7)
        assert registry.get("cam_A") == context

    def test_clear_and_clear_all(self):
        registry = ActivePositionRegistry()
        registry.set(
            ActivePositionContext(
                camera_id="cam_A", ptz_id="P", position_id="p",
                position_name="n", roi_set_ref="",
            )
        )
        registry.clear("cam_A")
        assert registry.get("cam_A") is None
        registry.set(
            ActivePositionContext(
                camera_id="cam_B", ptz_id="P", position_id="p",
                position_name="n", roi_set_ref="",
            )
        )
        registry.clear_all()
        assert registry.get("cam_B") is None

    def test_shutdown_rejects_writes(self):
        registry = ActivePositionRegistry()
        registry.shutdown()
        assert registry.closed is True
        with pytest.raises(PtzStateError):
            registry.set(
                ActivePositionContext(
                    camera_id="cam_A", ptz_id="P", position_id="p",
                    position_name="n", roi_set_ref="",
                )
            )
        registry.shutdown()  # idempotent
        assert registry.get("cam_A") is None

    def test_concurrent_readers(self):
        registry = ActivePositionRegistry()
        registry.set(
            ActivePositionContext(
                camera_id="cam_A", ptz_id="P", position_id="p",
                position_name="n", roi_set_ref="", context_generation=1,
            )
        )
        seen: list = []

        def _read():
            for _ in range(200):
                context = registry.get("cam_A")
                if context is not None:
                    seen.append(context.context_generation)

        threads = [threading.Thread(target=_read) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
        assert seen and all(gen == 1 for gen in seen)


class TestRetarget:
    def test_success_with_observer(self):
        observer = FakeObserver()
        coordinator, result = run_coordinator(FakeService(), observer=observer)
        assert result.state == RoiActivationState.COMPLETED
        assert result.first_result_confirmed is True
        assert observer.active_position == ("set_a", 1)
        context = coordinator.registry.get("cam_A")
        assert context is not None
        assert context.context_generation == 1
        assert context.session_generation == 7
        assert context.operation_id == "ptzop-1"
        assert context.roi_ids == ("roi_1",)

    def test_success_without_observer(self):
        coordinator, result = run_coordinator(FakeService(), observer=None)
        assert result.state == RoiActivationState.COMPLETED
        assert result.first_result_confirmed is False
        assert coordinator.registry.get("cam_A") is not None

    def test_empty_roi_ref_uses_default(self):
        observer = FakeObserver()
        coordinator, result = run_coordinator(
            FakeService(), make_position(roi_set_ref=""), observer=observer
        )
        assert result.state == RoiActivationState.COMPLETED
        assert observer.active_position == ("default", 1)

    def test_movement_failure_preserves_context(self):
        observer = FakeObserver()
        service = FakeService()
        coordinator = ObserverRetargetCoordinator(service)
        first = coordinator.retarget(
            "cam_A", make_position(),
            analysis_config_provider=lambda cam: make_analysis(),
            observer_provider=lambda: observer,
            session_generation_provider=lambda: 7,
            timeout_s=5.0,
        )
        assert first.state == RoiActivationState.COMPLETED
        service.outcome = "timeout"
        second = coordinator.retarget(
            "cam_A", make_position(position_id="pos_2"),
            analysis_config_provider=lambda cam: make_analysis(),
            observer_provider=lambda: observer,
            session_generation_provider=lambda: 7,
            timeout_s=2.0,
        )
        assert second.state == RoiActivationState.FAILED
        assert coordinator.registry.get("cam_A").position_id == "pos_1"
        assert observer.active_position == ("set_a", 1)

    def test_observer_wrong_camera_fails(self):
        observer = FakeObserver(camera_id="cam_B")
        _, result = run_coordinator(FakeService(), observer=observer)
        assert result.state == RoiActivationState.FAILED
        assert "another camera" in (result.error.message if result.error else "")

    def test_stale_session_rejected(self):
        generation = {"gen": 7}
        coordinator = ObserverRetargetCoordinator(FakeService())
        result = coordinator.retarget(
            "cam_A", make_position(),
            analysis_config_provider=lambda cam: make_analysis(),
            observer_provider=lambda: FakeObserver(),
            session_generation_provider=lambda: generation["gen"],
            timeout_s=5.0,
        )
        assert result.state == RoiActivationState.COMPLETED
        generation["gen"] = 8  # switch mid-flight for the next call
        coordinator2 = ObserverRetargetCoordinator(
            FakeService(), coordinator.registry
        )
        # Simulate staleness arising during commit: provider flips after run.
        calls = {"n": 0}

        def _flipping():
            calls["n"] += 1
            return 8 if calls["n"] > 3 else 7

        result2 = coordinator2.retarget(
            "cam_A", make_position(position_id="pos_2"),
            analysis_config_provider=lambda cam: make_analysis(),
            observer_provider=lambda: FakeObserver(),
            session_generation_provider=_flipping,
            timeout_s=5.0,
        )
        assert result2.state in (
            RoiActivationState.CANCELLED, RoiActivationState.FAILED
        )

    def test_second_request_supersedes_first(self):
        service = FakeService(outcome="slow")
        coordinator = ObserverRetargetCoordinator(service)
        results: dict = {}

        def _first():
            results["first"] = coordinator.retarget(
                "cam_A", make_position(),
                analysis_config_provider=lambda cam: make_analysis(),
                observer_provider=lambda: FakeObserver(),
                session_generation_provider=lambda: 7,
                timeout_s=10.0,
            )

        thread = threading.Thread(target=_first, daemon=True)
        thread.start()
        time.sleep(0.2)
        service.outcome = "reached"
        results["second"] = coordinator.retarget(
            "cam_A", make_position(position_id="pos_2"),
            analysis_config_provider=lambda cam: make_analysis(),
            observer_provider=lambda: FakeObserver(),
            session_generation_provider=lambda: 7,
            timeout_s=10.0,
        )
        thread.join(timeout=15.0)
        assert results["second"].state == RoiActivationState.COMPLETED
        assert results["first"].state == RoiActivationState.CANCELLED

    def test_cancel_method(self):
        service = FakeService(outcome="slow")
        coordinator = ObserverRetargetCoordinator(service)
        holder: list = []

        def _run():
            holder.append(
                coordinator.retarget(
                    "cam_A", make_position(),
                    analysis_config_provider=lambda cam: make_analysis(),
                    observer_provider=lambda: FakeObserver(),
                    session_generation_provider=lambda: 7,
                    timeout_s=15.0,
                )
            )

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        time.sleep(0.2)
        coordinator.cancel("cam_A")
        thread.join(timeout=15.0)
        assert holder and holder[0].state == RoiActivationState.CANCELLED

    def test_repeated_same_position(self):
        service = FakeService()
        observer = FakeObserver()
        coordinator = ObserverRetargetCoordinator(service)
        for _ in range(2):
            result = coordinator.retarget(
                "cam_A", make_position(),
                analysis_config_provider=lambda cam: make_analysis(),
                observer_provider=lambda: observer,
                session_generation_provider=lambda: 7,
                timeout_s=5.0,
            )
            assert result.state == RoiActivationState.COMPLETED
        assert coordinator.registry.get("cam_A").context_generation == 2
        assert observer.active_position == ("set_a", 2)


class TestPipelineOverride:
    def test_override_and_stamping(self):
        from thermal_monitor.processing.pipeline import SimpleProcessingPipeline

        analysis = make_analysis()
        pipeline = SimpleProcessingPipeline(config=analysis)
        assert pipeline.active_position == ("default", 0)
        generation = pipeline.set_active_position("set_a")
        assert generation == 1
        assert pipeline.active_position == ("set_a", 1)

    def test_explicit_generation_reapply(self):
        from thermal_monitor.processing.pipeline import SimpleProcessingPipeline

        pipeline = SimpleProcessingPipeline(config=make_analysis())
        pipeline.set_active_position("set_a")
        pipeline.set_active_position("set_a", 1)  # re-apply, no bump
        assert pipeline.active_position == ("set_a", 1)

    def test_frame_resolution_uses_override(self):
        import numpy as np

        from thermal_monitor.core.frame import Frame
        from thermal_monitor.processing.pipeline import SimpleProcessingPipeline

        analysis = make_analysis()
        pipeline = SimpleProcessingPipeline(config=analysis)
        pipeline.set_active_position("set_a")
        frame = Frame(
            descriptor=_descriptor("cam_A", 3, {}),
            payload=_payload(),
        )
        result = pipeline.process_frame(frame)
        assert result.metadata["position_id"] == "set_a"
        assert result.metadata["context_generation"] == 1
        assert set(result.roi_results) == {"roi_1"}

    def test_frame_metadata_wins_over_override(self):
        from thermal_monitor.core.frame import Frame
        from thermal_monitor.processing.pipeline import SimpleProcessingPipeline

        pipeline = SimpleProcessingPipeline(config=make_analysis())
        pipeline.set_active_position("set_a")
        frame = Frame(
            descriptor=_descriptor("cam_A", 4, {"position_id": "other"}),
            payload=_payload(),
        )
        result = pipeline.process_frame(frame)
        assert result.metadata["position_id"] == "other"
        assert result.metadata["context_generation"] == 1

    def test_empty_thermal_still_stamped(self):
        from thermal_monitor.core.frame import Frame
        from thermal_monitor.processing.pipeline import SimpleProcessingPipeline

        pipeline = SimpleProcessingPipeline(config=make_analysis())
        frame = Frame(
            descriptor=_descriptor("cam_A", 5, {}),
            payload=_payload(empty=True),
        )
        result = pipeline.process_frame(frame)
        assert result.metadata["position_id"] == "default"
        assert result.roi_results == {}


def _descriptor(camera_id, sequence, metadata):
    from thermal_monitor.core.frame import (
        FrameDescriptor,
        StreamMetadata,
        SyncInfo,
        SyncStatus,
    )

    return FrameDescriptor(
        camera_id=camera_id,
        sequence=sequence,
        timestamp=float(sequence),
        monotonic_timestamp=float(sequence),
        thermal=StreamMetadata(present=True, width=4, height=4, sequence=sequence),
        visible=StreamMetadata(present=False),
        sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
        metadata=dict(metadata),
    )


def _payload(empty=False):
    import numpy as np

    from thermal_monitor.core.frame import FramePayload

    thermal = None if empty else np.zeros((4, 4), dtype=np.uint16)
    if thermal is not None:
        thermal.setflags(write=False)
    return FramePayload(thermal=thermal)
