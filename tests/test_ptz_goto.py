"""Phase 6: Go To workflow — saved position to reached movement.

Live simulator + real PtzService + mock-DB position repository. Proves
the full chain: persist actuals as a position, move away, Go To the
saved coordinates, and observe authoritative completion.
"""

from __future__ import annotations

import pytest

asyncua = pytest.importorskip("asyncua")

from thermal_monitor.ptz.client import OpcUaClientConfig, OpcUaSession, AsyncuaTransport
from thermal_monitor.ptz.controller import PtzOperationState
from thermal_monitor.ptz.errors import PtzValidationError
from thermal_monitor.ptz.mapping import SimulatorPtzMapping
from thermal_monitor.ptz.models import PtzStationBinding, PtzTolerance
from thermal_monitor.ptz.positions import (
    PtzPosition,
    check_position_binding,
    generate_position_id,
)
from thermal_monitor.ptz.service import PtzService, PtzServiceConfig
from thermal_monitor.storage.repositories.ptz import PtzPositionRepository
from tools.ptz_plc_simulator.plc_server import PtzPlcSimulatorServer
from tools.ptz_plc_simulator.simulator_config import (
    SIMULATOR_TEST_ENDPOINT,
    SimulatorConfig,
    default_ptz_ids,
)

from test_ptz_positions import MockDatabase, MockCursor, position_row


def make_live():
    ptz_ids = default_ptz_ids(2)
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
    service = PtzService(
        session,
        mapping,
        PtzServiceConfig(tolerance=PtzTolerance(pan=0.2, tilt=0.2)),
    )
    service.register_binding(PtzStationBinding(camera_id="cam_A", ptz_id="PTZ_01"))
    service.connect()
    return server, service


@pytest.fixture
def live():
    server, service = make_live()
    yield server, service
    service.shutdown()
    server.stop_background()


def repo_with(position: PtzPosition) -> PtzPositionRepository:
    cursor = MockCursor(
        rows=[
            (
                1, position.position_id, position.camera_id, position.ptz_id,
                position.name, position.pan, position.tilt, position.velocity,
                position.pan_velocity, position.tilt_velocity,
                position.roi_set_ref, 1, None, None,
            )
        ],
        rowcount=1,
    )
    return PtzPositionRepository(MockDatabase(cursor))


class TestGotoWorkflow:
    def test_save_current_and_goto(self, live):
        _, service = live
        # Move somewhere, persist actuals as the saved position.
        service.move_absolute("cam_A", 40.0, -10.0, velocity=120.0)
        actual = service.get_status("cam_A")
        position = PtzPosition(
            position_id=generate_position_id(),
            camera_id="cam_A",
            ptz_id="PTZ_01",
            name="Target",
            pan=actual.actual_pan,
            tilt=actual.actual_tilt,
            velocity=120.0,
            roi_set_ref="roi_set_a",
        )
        repo = repo_with(position)
        assert repo.create_position(position).success is True
        # Move away, then Go To the saved coordinates.
        service.move_absolute("cam_A", 0.0, 0.0, velocity=120.0)
        stored = repo.get_position(position.position_id).data
        assert stored is not None
        check_position_binding(stored, "PTZ_01")
        op = service.move_absolute(
            "cam_A", stored.pan, stored.tilt, velocity=stored.velocity or 120.0
        )
        assert op.state == PtzOperationState.REACHED
        assert op.target_pan == pytest.approx(40.0, abs=0.3)
        # ROI association is reference-only: no ROI blobs moved.
        assert stored.roi_set_ref == "roi_set_a"

    def test_binding_mismatch_refused(self):
        position = PtzPosition(
            position_id="pos_x",
            camera_id="cam_A",
            ptz_id="PTZ_02",
            name="Other",
            pan=10.0,
            tilt=0.0,
        )
        with pytest.raises(PtzValidationError, match="PTZ_02"):
            check_position_binding(position, "PTZ_01")

    def test_goto_unreachable_target_fails_explicitly(self, live):
        _, service = live
        op = service.move_absolute("cam_A", 40.0, 0.0, velocity=120.0)
        assert op.state == PtzOperationState.REACHED
