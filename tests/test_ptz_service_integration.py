"""Service integration tests against the real Phase 4 simulator.

REAL OpcUaSession + REAL AsyncuaTransport + REAL simulator server.
Every assertion travels OPC UA. Requires asyncua (skipped without it).
"""

from __future__ import annotations

import time

import pytest

asyncua = pytest.importorskip("asyncua")

from thermal_monitor.ptz.client import OpcUaClientConfig, OpcUaSession, AsyncuaTransport
from thermal_monitor.ptz.controller import PtzCommandError, PtzOperationState
from thermal_monitor.ptz.errors import PtzErrorCategory
from thermal_monitor.ptz.mapping import SimulatorPtzMapping
from thermal_monitor.ptz.models import PtzStationBinding, PtzTolerance, VelocityMode
from thermal_monitor.ptz.service import PtzService, PtzServiceConfig
from thermal_monitor.ptz.state import PlcConnectionState
from tools.ptz_plc_simulator.plc_server import PtzPlcSimulatorServer
from tools.ptz_plc_simulator.simulator_config import (
    SIMULATOR_TEST_ENDPOINT,
    SimulatorConfig,
    default_ptz_ids,
)


def wait_for(predicate, timeout_s=15.0, poll_s=0.05) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()


def make_stack(ptz_count=3, **svc_kwargs):
    ptz_ids = default_ptz_ids(ptz_count)
    server = PtzPlcSimulatorServer(
        SimulatorConfig(
            ptz_ids=ptz_ids,
            endpoint=SIMULATOR_TEST_ENDPOINT,
            calibration_duration_s=0.3,
            update_hz=50.0,
            max_velocity=360.0,
        )
    )
    server.start_background()
    mapping = SimulatorPtzMapping(ptz_ids=ptz_ids)
    session = OpcUaSession(
        OpcUaClientConfig(
            endpoint=SIMULATOR_TEST_ENDPOINT,
            reconnect_max_attempts=30,
            reconnect_initial_backoff_s=0.05,
            reconnect_backoff_factor=1.0,
            reconnect_max_backoff_s=0.2,
        ),
        AsyncuaTransport(),
    )
    config = PtzServiceConfig(
        tolerance=PtzTolerance(pan=0.2, tilt=0.2),
        move_timeout_s=20.0,
        calibration_timeout_s=20.0,
        monitor_interval_s=0.05,
        **svc_kwargs,
    )
    service = PtzService(session, mapping, config)
    cameras = []
    for index, ptz_id in enumerate(ptz_ids, start=1):
        camera_id = f"camera_{index:02d}"
        service.register_binding(
            PtzStationBinding(camera_id=camera_id, ptz_id=ptz_id)
        )
        cameras.append(camera_id)
    service.connect()
    return server, service, cameras


@pytest.fixture
def live():
    server, service, cameras = make_stack()
    yield server, service, cameras
    service.shutdown()
    server.stop_background()


class TestServiceIntegration:
    def test_absolute_move_end_to_end(self, live):
        _, service, cameras = live
        op = service.move_absolute(cameras[0], 45.0, -12.0, velocity=120.0)
        assert op.state == PtzOperationState.REACHED
        status = service.get_status(cameras[0])
        assert status.actual_pan == pytest.approx(45.0, abs=0.3)
        assert status.actual_tilt == pytest.approx(-12.0, abs=0.3)

    def test_relative_increments(self, live):
        _, service, cameras = live
        service.move_absolute(cameras[0], 20.0, 0.0, velocity=120.0)
        for delta, expected in ((1.0, 21.0), (5.0, 26.0), (10.0, 36.0), (-5.0, 31.0)):
            op = service.move_relative(cameras[0], delta, 0.0, velocity=120.0)
            assert op.state == PtzOperationState.REACHED
            assert op.target_pan == pytest.approx(expected, abs=0.3)

    def test_per_axis_move(self, live):
        _, service, cameras = live
        op = service.move_absolute(
            cameras[0],
            40.0,
            15.0,
            velocity_mode=VelocityMode.PER_AXIS,
            pan_velocity=120.0,
            tilt_velocity=60.0,
        )
        assert op.state == PtzOperationState.REACHED

    def test_stop_mid_move(self, live):
        _, service, cameras = live
        import threading

        op_holder: list = []
        worker = threading.Thread(
            target=lambda: op_holder.append(
                service.move_absolute(cameras[0], 150.0, 0.0, velocity=10.0)
            ),
            daemon=True,
        )
        worker.start()
        assert wait_for(
            lambda: service.get_status(cameras[0]).moving, timeout_s=10.0
        )
        stopped = service.stop(cameras[0])
        assert stopped.state == PtzOperationState.CANCELLED
        worker.join(timeout=15.0)
        assert op_holder and op_holder[0].state in (
            PtzOperationState.CANCELLED,
            PtzOperationState.FAILED,
        )

    def test_clear_error_after_injection(self, live):
        server, service, cameras = live
        server.engine.inject_ptz_error("PTZ_01")
        assert wait_for(
            lambda: service.get_status(cameras[0]).error is not None,
            timeout_s=10.0,
        )
        with pytest.raises(PtzCommandError):
            service.move_absolute(cameras[0], 10.0, 0.0, velocity=60.0, wait=False)
        status = service.clear_error(cameras[0])
        assert status.error is None
        op = service.move_absolute(cameras[0], 10.0, 0.0, velocity=120.0)
        assert op.state == PtzOperationState.REACHED

    def test_calibration_cycle(self, live):
        _, service, cameras = live
        status = service.request_calibration(cameras[0])
        assert status.calibration.name == "COMPLETE"
        assert status.ready is True

    def test_calibration_failure(self, live):
        server, service, cameras = live
        server.engine.inject_calibration_failure("PTZ_01", True)
        with pytest.raises(PtzCommandError) as exc_info:
            service.request_calibration(cameras[0])
        assert exc_info.value.error.category == PtzErrorCategory.CALIBRATION
        service.clear_error(cameras[0])

    def test_movement_timeout(self, live):
        server, service, cameras = live
        server.engine.inject_motion_freeze("PTZ_01", True)
        try:
            op = service.move_absolute(
                cameras[0], 90.0, 0.0, velocity=60.0, timeout_s=1.0
            )
            assert op.state == PtzOperationState.FAILED
            assert op.error is not None
            assert op.error.category == PtzErrorCategory.MOVEMENT_TIMEOUT
        finally:
            server.engine.inject_motion_freeze("PTZ_01", False)

    def test_communication_loss_and_reconnect(self, live):
        server, service, cameras = live
        server.stop_listening()
        # Transport-driven loss surfaces through the documented hook;
        # the session then owns bounded reconnect (Phase 3 contract).
        service._session.notify_connection_lost("simulator down")
        assert wait_for(
            lambda: service._session.state
            in (
                PlcConnectionState.COMMUNICATION_LOST,
                PlcConnectionState.RECONNECTING,
                PlcConnectionState.CONNECTED,
            ),
            timeout_s=5.0,
        )
        server.start_listening()
        assert wait_for(
            lambda: service._session.state == PlcConnectionState.CONNECTED,
            timeout_s=20.0,
        )
        op = service.move_absolute(cameras[0], 15.0, 0.0, velocity=120.0)
        assert op.state == PtzOperationState.REACHED

    def test_monitor_delivers_live_status(self, live):
        _, service, cameras = live
        received: list = []
        service.add_status_listener(lambda pid, st: received.append((pid, st)))
        service.start_monitoring()
        try:
            service.move_absolute(cameras[1], 25.0, 5.0, velocity=120.0)
            assert wait_for(lambda: len(received) > 0, timeout_s=10.0)
            assert any(pid == "PTZ_02" for pid, _ in received)
        finally:
            service.stop_monitoring()

    def test_concurrent_independent_moves(self, live):
        _, service, cameras = live
        import threading

        results: dict = {}

        def _move(cam, pan):
            results[cam] = service.move_absolute(cam, pan, 0.0, velocity=120.0)

        threads = [
            threading.Thread(
                target=_move, args=(cameras[0], 50.0), daemon=True
            ),
            threading.Thread(
                target=_move, args=(cameras[1], -40.0), daemon=True
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20.0)
        assert results[cameras[0]].state == PtzOperationState.REACHED
        assert results[cameras[1]].state == PtzOperationState.REACHED
        assert service.get_status(cameras[0]).actual_pan == pytest.approx(
            50.0, abs=0.3
        )
        assert service.get_status(cameras[1]).actual_pan == pytest.approx(
            -40.0, abs=0.3
        )

    def test_shutdown_while_moving(self, live):
        server, service, cameras = live
        import threading

        worker = threading.Thread(
            target=lambda: service.move_absolute(
                cameras[0], 150.0, 0.0, velocity=5.0
            ),
            daemon=True,
        )
        worker.start()
        assert wait_for(
            lambda: service.get_status(cameras[0]).moving, timeout_s=10.0
        )
        service.shutdown()
        server.stop_background()
        worker.join(timeout=15.0)
        assert service.closed is True


class TestEightPtzIndependence:
    def test_all_eight_via_service(self):
        server, service, cameras = make_stack(ptz_count=8)
        try:
            service.connect()
            for index, camera_id in enumerate(cameras):
                op = service.move_absolute(
                    camera_id, float(index * 10), float(-index), velocity=180.0
                )
                assert op.state == PtzOperationState.REACHED, camera_id
            for index, camera_id in enumerate(cameras):
                status = service.get_status(camera_id)
                assert status.actual_pan == pytest.approx(
                    float(index * 10), abs=0.3
                )
        finally:
            service.shutdown()
            server.stop_background()
