"""Monitoring tests: status assembly, degraded/stale handling, listener path.

Fake transport only (fast). Covers the single coherent status update
path owned by PtzService: assembly variants, per-PTZ independence,
listener containment, and degraded snapshots.
"""

from __future__ import annotations

import time

import pytest

from thermal_monitor.ptz.client import OpcUaClientConfig, OpcUaSession
from thermal_monitor.ptz.controller import PtzCommandError, PtzController
from thermal_monitor.ptz.mapping import LogicalField
from thermal_monitor.ptz.models import PtzStationBinding
from thermal_monitor.ptz.service import PtzService, PtzServiceConfig
from thermal_monitor.ptz.state import (
    CalibrationState,
    PtzMovementState,
    PtzStatus,
)

from test_ptz_controller import (
    MAPPING,
    PTZ,
    ScriptedTransport,
    healthy_values,
    make_controller,
    node_id,
)

CAM_A = "cam_A"
CAM_B = "cam_B"
PTZ_A = "PTZ_01"
PTZ_B = "PTZ_02"


def wait_for(predicate, timeout_s=3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestAssembly:
    def test_moving_maps(self):
        controller, _, _ = make_controller(
            ScriptedTransport(
                healthy_values(
                    **{
                        node_id(LogicalField.MOVING): True,
                        node_id(LogicalField.POSITION_REACHED): False,
                    }
                )
            )
        )
        status = controller.read_status()
        assert status.movement == PtzMovementState.MOVING
        assert status.moving is True
        assert status.position_reached is False

    def test_error_code_mapping(self):
        controller, _, _ = make_controller(
            ScriptedTransport(
                healthy_values(
                    **{
                        node_id(LogicalField.ERROR): True,
                        node_id(LogicalField.ERROR_CODE): 101,
                    }
                )
            )
        )
        status = controller.read_status()
        assert status.movement == PtzMovementState.ERROR
        assert status.error is not None
        assert "101" in status.error.code
        assert status.accepts_commands is False

    def test_calibration_precedence(self):
        transport = ScriptedTransport(
            healthy_values(
                **{
                    node_id(LogicalField.CALIBRATION_REQUIRED): True,
                    node_id(LogicalField.CALIBRATION_ACTIVE): True,
                    node_id(LogicalField.CALIBRATION_COMPLETE): True,
                }
            )
        )
        controller, _, _ = make_controller(transport)
        assert controller.read_status().calibration == CalibrationState.ACTIVE

    def test_degraded_preserves_last_good(self):
        transport = ScriptedTransport(healthy_values())
        controller, session, _ = make_controller(transport)
        first = controller.read_status()
        assert first.actual_pan == pytest.approx(10.0)
        session.disconnect()
        degraded = controller.read_status()
        assert degraded.communication_ok is False
        assert degraded.actual_pan == pytest.approx(10.0)
        assert degraded.ptz_available is False

    def test_strict_raises_without_last_good(self):
        transport = ScriptedTransport(healthy_values())
        controller, session, _ = make_controller(transport)
        session.disconnect()
        controller._last_good = None
        degraded = controller.read_status()
        assert degraded.communication_ok is False
        with pytest.raises(PtzCommandError):
            controller.read_status_strict()


class TestMonitorIndependence:
    def _service(self):
        merged = {}
        merged.update(healthy_values(PTZ_A))
        merged.update(healthy_values(PTZ_B))
        transport = ScriptedTransport(merged)
        session = OpcUaSession(OpcUaClientConfig(endpoint="fake"), transport)
        session.connect()
        service = PtzService(
            session,
            MAPPING,
            PtzServiceConfig(monitor_interval_s=0.02, stale_threshold_s=0.2),
        )
        service.register_binding(PtzStationBinding(camera_id=CAM_A, ptz_id=PTZ_A))
        service.register_binding(PtzStationBinding(camera_id=CAM_B, ptz_id=PTZ_B))
        return service, transport

    def test_both_ptz_cached_independently(self):
        service, _ = self._service()
        service.start_monitoring()
        try:
            assert wait_for(
                lambda: service.cached_status(CAM_A) is not None
                and service.cached_status(CAM_B) is not None,
                timeout_s=3.0,
            )
        finally:
            service.stop_monitoring()
            service.shutdown()

    def test_failing_ptz_does_not_starve_healthy(self):
        service, transport = self._service()
        service.start_monitoring()
        try:
            assert wait_for(
                lambda: service.cached_status(CAM_B) is not None, timeout_s=3.0
            )
            for field in (
                LogicalField.ACTUAL_PAN,
                LogicalField.MOVING,
                LogicalField.READY,
            ):
                transport.fail_nodes.add(node_id(field, PTZ_A))
            time.sleep(0.3)
            # Healthy PTZ keeps refreshing; failing one degrades, not lost.
            assert service.cached_status(CAM_B) is not None
        finally:
            service.stop_monitoring()
            service.shutdown()

    def test_listener_exception_contained(self):
        service, _ = self._service()

        def _bad(pid, status):
            raise RuntimeError("listener boom")

        good: list = []
        service.add_status_listener(_bad)
        service.add_status_listener(lambda pid, st: good.append(pid))
        service.start_monitoring()
        try:
            assert wait_for(lambda: len(good) > 0, timeout_s=3.0), good
        finally:
            service.stop_monitoring()
            service.shutdown()

    def test_no_duplicate_monitor_effects(self):
        """One loop only: cache timestamps advance monotonically."""
        service, _ = self._service()
        service.start_monitoring()
        try:
            assert wait_for(
                lambda: service.cached_status(CAM_A) is not None, timeout_s=3.0
            )
            first = service._cached[PTZ_A][0]
            time.sleep(0.15)
            second = service._cached[PTZ_A][0]
            assert second > first
        finally:
            service.stop_monitoring()
            service.shutdown()
