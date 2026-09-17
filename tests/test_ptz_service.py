"""Service-level tests: bindings, camera-addressed API, monitor lifecycle.

Fake transport only (fast). Real-server integration lives in
test_ptz_service_integration.py.
"""

from __future__ import annotations

import time

import pytest

from thermal_monitor.ptz.client import OpcUaClientConfig, OpcUaSession
from thermal_monitor.ptz.controller import PtzCommandError, PtzOperationState
from thermal_monitor.ptz.errors import PtzErrorCategory
from thermal_monitor.ptz.mapping import LogicalField, SimulatorPtzMapping
from thermal_monitor.ptz.models import PtzStationBinding
from thermal_monitor.ptz.service import PtzService, PtzServiceConfig
from thermal_monitor.ptz.state import PtzStatus

from test_ptz_controller import ScriptedTransport, healthy_values, node_id

CAM_A = "cam_A"
CAM_B = "cam_B"
PTZ_A = "PTZ_01"
PTZ_B = "PTZ_02"


def make_service(values=None, **config_kwargs) -> tuple[PtzService, ScriptedTransport]:
    merged: dict[str, object] = {}
    merged.update(healthy_values(PTZ_A))
    merged.update(healthy_values(PTZ_B))
    if values:
        merged.update(values)
    transport = ScriptedTransport(merged)
    mapping = SimulatorPtzMapping(ptz_ids=(PTZ_A, PTZ_B))
    session = OpcUaSession(OpcUaClientConfig(endpoint="fake"), transport)
    session.connect()
    config = PtzServiceConfig(
        monitor_interval_s=0.02, stale_threshold_s=0.2, **config_kwargs
    )
    service = PtzService(session, mapping, config)
    service.register_binding(PtzStationBinding(camera_id=CAM_A, ptz_id=PTZ_A))
    service.register_binding(PtzStationBinding(camera_id=CAM_B, ptz_id=PTZ_B))
    return service, transport


def wait_for(predicate, timeout_s=3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestBindings:
    def test_valid_resolution(self):
        service, _ = make_service()
        assert service.binding_for_camera(CAM_A).ptz_id == PTZ_A
        assert service.binding_for_camera(CAM_B).ptz_id == PTZ_B
        service.shutdown()

    def test_unknown_camera_fails_explicitly(self):
        service, _ = make_service()
        with pytest.raises(PtzCommandError) as exc_info:
            service.binding_for_camera("cam_NOPE")
        assert exc_info.value.error.category == PtzErrorCategory.COMMAND_REJECTED
        service.shutdown()

    def test_conflicting_binding_rejected(self):
        service, _ = make_service()
        with pytest.raises(PtzCommandError):
            service.register_binding(
                PtzStationBinding(camera_id=CAM_A, ptz_id=PTZ_B)
            )
        service.shutdown()

    def test_identical_reregistration_idempotent(self):
        service, _ = make_service()
        service.register_binding(PtzStationBinding(camera_id=CAM_A, ptz_id=PTZ_A))
        assert service.binding_for_camera(CAM_A).ptz_id == PTZ_A
        service.shutdown()

    def test_independent_controllers(self):
        service, _ = make_service()
        assert service.controller_for_camera(CAM_A) is not (
            service.controller_for_camera(CAM_B)
        )
        service.shutdown()


class TestServiceApi:
    def test_get_status_and_is_ready(self):
        service, _ = make_service()
        status = service.get_status(CAM_A)
        assert isinstance(status, PtzStatus)
        assert status.accepts_commands is True
        assert service.is_ready(CAM_A) is True
        service.shutdown()

    def test_is_ready_false_when_disconnected(self):
        service, transport = make_service()
        transport.connected = False
        assert service.is_ready(CAM_A) is False
        service.shutdown()

    def test_api_after_shutdown_rejected(self):
        service, _ = make_service()
        service.shutdown()
        with pytest.raises(PtzCommandError):
            service.get_status(CAM_A)
        with pytest.raises(PtzCommandError):
            service.move_absolute(CAM_A, 1.0, 0.0, velocity=1.0, wait=False)

    def test_shutdown_idempotent(self):
        service, _ = make_service()
        service.shutdown()
        service.shutdown()
        assert service.closed is True


class TestMonitor:
    def test_listener_delivery_and_cache(self):
        service, _ = make_service()
        received: list[tuple[str, PtzStatus]] = []
        service.add_status_listener(lambda pid, st: received.append((pid, st)))
        service.start_monitoring()
        try:
            assert wait_for(lambda: len(received) >= 2, timeout_s=3.0), received
            assert service.cached_status(CAM_A) is not None
        finally:
            service.stop_monitoring()
            service.shutdown()

    def test_start_idempotent_and_restart(self):
        service, _ = make_service()
        service.start_monitoring()
        service.start_monitoring()
        service.stop_monitoring()
        service.start_monitoring()
        service.stop_monitoring()
        service.shutdown()

    def test_no_callbacks_after_shutdown(self):
        service, _ = make_service()
        received: list = []
        service.add_status_listener(lambda pid, st: received.append((pid, st)))
        service.start_monitoring()
        assert wait_for(lambda: len(received) > 0, timeout_s=3.0)
        service.shutdown()
        count = len(received)
        time.sleep(0.15)
        assert len(received) == count

    def test_remove_listener(self):
        service, _ = make_service()
        received: list = []
        listener = lambda pid, st: received.append((pid, st))
        service.add_status_listener(listener)
        service.remove_status_listener(listener)
        service.start_monitoring()
        time.sleep(0.15)
        service.stop_monitoring()
        assert received == []
        service.shutdown()

    def test_stale_marking(self):
        service, transport = make_service()
        service.start_monitoring()
        try:
            assert wait_for(
                lambda: service.cached_status(CAM_A) is not None, timeout_s=3.0
            )
            transport.read_error = Exception("boom")
            assert wait_for(
                lambda: (
                    service.cached_status(CAM_A) is not None
                    and service.cached_status(CAM_A).communication_ok is False
                ),
                timeout_s=3.0,
            )
        finally:
            transport.read_error = None
            service.stop_monitoring()
            service.shutdown()
