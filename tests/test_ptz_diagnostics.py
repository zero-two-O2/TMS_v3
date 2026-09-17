"""Phase 7: diagnostics runner against the simulator backend."""

from __future__ import annotations

import pytest

asyncua = pytest.importorskip("asyncua")

from thermal_monitor.ptz.client import OpcUaClientConfig, OpcUaSession, AsyncuaTransport
from thermal_monitor.ptz.diagnostics import run_movement_checks, run_read_only_checks
from thermal_monitor.ptz.mapping import SimulatorPtzMapping
from thermal_monitor.ptz.models import PtzStationBinding
from thermal_monitor.ptz.service import PtzService
from tools.ptz_plc_simulator.plc_server import PtzPlcSimulatorServer
from tools.ptz_plc_simulator.simulator_config import (
    SIMULATOR_TEST_ENDPOINT,
    SimulatorConfig,
    default_ptz_ids,
)


def make_service():
    ptz_ids = default_ptz_ids(1)
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
        OpcUaClientConfig(endpoint=SIMULATOR_TEST_ENDPOINT), AsyncuaTransport()
    )
    service = PtzService(session, mapping)
    service.register_binding(PtzStationBinding(camera_id="cam_A", ptz_id="PTZ_01"))
    service.connect()
    return server, service, mapping


@pytest.fixture
def live():
    server, service, mapping = make_service()
    yield server, service, mapping
    service.shutdown()
    server.stop_background()


class TestDiagnostics:
    def test_read_only_passes_on_simulator(self, live):
        _, service, mapping = live
        report = run_read_only_checks(
            service, mapping, "cam_A", "PTZ_01",
            backend="simulator", endpoint=SIMULATOR_TEST_ENDPOINT,
        )
        assert report.failed == 0
        assert report.passed >= 8
        assert "simulator" in report.summary()

    def test_movement_requires_confirmation(self, live):
        _, service, _ = live
        report = run_movement_checks(
            service, "cam_A", "PTZ_01",
            backend="simulator", endpoint=SIMULATOR_TEST_ENDPOINT,
            target_pan=20.0, target_tilt=0.0, velocity=60.0,
            confirmed=False,
        )
        assert report.skipped == 1
        assert report.failed == 0

    def test_confirmed_movement_passes(self, live):
        _, service, _ = live
        report = run_movement_checks(
            service, "cam_A", "PTZ_01",
            backend="simulator", endpoint=SIMULATOR_TEST_ENDPOINT,
            target_pan=20.0, target_tilt=0.0, velocity=120.0,
            confirmed=True,
        )
        assert report.failed == 0
        assert report.passed == 2

    def test_never_labels_unverified_pass(self, live):
        server, service, mapping = live
        server.stop_listening()
        try:
            report = run_read_only_checks(
                service, mapping, "cam_A", "PTZ_01",
                backend="simulator", endpoint=SIMULATOR_TEST_ENDPOINT,
            )
        finally:
            server.start_listening()
        assert report.failed >= 1
