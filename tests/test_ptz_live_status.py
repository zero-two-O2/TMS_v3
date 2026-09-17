"""Phase 7: Live PTZ status readout + monitor lifecycle (offscreen Qt)."""

from __future__ import annotations

import time
from unittest.mock import Mock

import pytest

asyncua = pytest.importorskip("asyncua")

from PyQt6.QtWidgets import QApplication

from thermal_monitor.core.models import CameraIdentity
from thermal_monitor.ptz.models import PtzStationBinding
from thermal_monitor.ptz.state import (
    CalibrationState,
    PlcConnectionState,
    PtzMovementState,
    PtzStatus,
)
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.ui.windows.live_window import LiveCameraTile, LiveModeWidget
from tools.ptz_plc_simulator.plc_server import PtzPlcSimulatorServer
from tools.ptz_plc_simulator.simulator_config import (
    SIMULATOR_TEST_ENDPOINT,
    SimulatorConfig,
    default_ptz_ids,
)


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def ready_status(**overrides) -> PtzStatus:
    values = {
        "plc_state": PlcConnectionState.CONNECTED,
        "ptz_available": True,
        "communication_ok": True,
        "ready": True,
        "movement": PtzMovementState.IDLE,
        "actual_pan": 12.5,
        "actual_tilt": -4.5,
        "calibration": CalibrationState.NOT_REQUIRED,
        "error": None,
    }
    values.update(overrides)
    return PtzStatus(**values)


class FakeService:
    def __init__(self, status) -> None:
        self._status = status

    def binding_for_camera(self, camera_id):
        return PtzStationBinding(camera_id=camera_id, ptz_id="PTZ_01")

    def get_status(self, camera_id):
        return self._status


class TestTileReadout:
    def test_set_and_clear(self, qapp):
        tile = LiveCameraTile(0, camera_id="cam_1", name="CAM 01")
        assert tile._ptz_label.text() == ""
        tile.set_ptz_status("PTZ 1.0,2.0 =", "tip")
        assert tile._ptz_label.text() == "PTZ 1.0,2.0 ="
        assert tile._ptz_label.toolTip() == "tip"
        tile.clear()
        assert tile._ptz_label.text() == ""

    def test_clear_camera_resets(self, qapp):
        tile = LiveCameraTile(0, camera_id="cam_1", name="CAM 01")
        tile.set_ptz_status("PTZ x", "tip")
        tile.clear_camera()
        assert tile._ptz_label.text() == ""


class TestTextFormatting:
    def test_ready(self):
        text, tip = LiveModeWidget._ptz_text_for(
            FakeService(ready_status()), "cam_A"
        )
        assert text == "PTZ 12.5,-4.5"
        assert "PTZ_01" in tip

    def test_moving(self):
        text, _ = LiveModeWidget._ptz_text_for(
            FakeService(ready_status(movement=PtzMovementState.MOVING)), "cam_A"
        )
        assert text.endswith("…")

    def test_reached(self):
        text, _ = LiveModeWidget._ptz_text_for(
            FakeService(ready_status(movement=PtzMovementState.POSITION_REACHED)),
            "cam_A",
        )
        assert text.endswith("=")

    def test_disconnected(self):
        text, _ = LiveModeWidget._ptz_text_for(
            FakeService(
                ready_status(
                    plc_state=PlcConnectionState.DISCONNECTED,
                    communication_ok=False,
                    ready=False,
                )
            ),
            "cam_A",
        )
        assert text == "PTZ --"

    def test_unknown_camera_blank(self):
        class NoBinding(FakeService):
            def binding_for_camera(self, camera_id):
                raise RuntimeError("no binding")

        text, tip = LiveModeWidget._ptz_text_for(NoBinding(ready_status()), "cam_X")
        assert text == ""


def _wall(qapp, config_manager=None):
    config_service = ConfigurationService()
    identity = CameraIdentity(camera_id="cam_A", serial_number="SN-A")
    config_service.set_camera_config(
        config_service.create_camera_config(identity=identity, name="cam_A")
    )
    wall = LiveModeWidget(
        mode_service=Mock(),
        config_service=config_service,
        theme_manager=None,
        config_manager=config_manager,
    )
    return wall


class TestReadoutGuards:
    def test_stale_token_dropped(self, qapp):
        wall = _wall(qapp)
        wall._camera_to_slot["cam_A"] = 0
        wall._tiles[0].set_camera("cam_A", "cam_A", "SN-A")
        wall._on_ptz_readout(wall._startup_token + 99, "cam_A", "PTZ x", "")
        QApplication.processEvents()
        assert wall._tiles[0]._ptz_label.text() == ""
        wall._on_ptz_readout(wall._startup_token, "cam_A", "PTZ 1.0,2.0", "")
        QApplication.processEvents()
        assert wall._tiles[0]._ptz_label.text() == "PTZ 1.0,2.0"
        wall.close()

    def test_unassigned_camera_dropped(self, qapp):
        wall = _wall(qapp)
        wall._on_ptz_readout(wall._startup_token, "cam_ZZZ", "PTZ x", "")
        QApplication.processEvents()
        wall.close()

    def test_timer_lifecycle(self, qapp):
        wall = _wall(qapp)
        assert wall._ptz_timer is None
        wall._start_ptz_timer()
        assert wall._ptz_timer.isActive()
        wall._stop_ptz_timer()
        assert not wall._ptz_timer.isActive()
        wall.close()


class TestLiveMonitorIntegration:
    def test_monitor_updates_tile(self, qapp, tmp_path):
        import yaml

        from thermal_monitor.config import ConfigurationManager

        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "ptz": {
                        "profile": "simulator",
                        "endpoint": SIMULATOR_TEST_ENDPOINT,
                    },
                    "cameras": {
                        "mapping": [
                            {
                                "camera_id": "cam_A",
                                "serial_number": "SN-A",
                                "ptz_id": "PTZ_01",
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        config_manager = ConfigurationManager(config_path=path)
        server = PtzPlcSimulatorServer(
            SimulatorConfig(
                ptz_ids=default_ptz_ids(1),
                endpoint=SIMULATOR_TEST_ENDPOINT,
                max_velocity=360.0,
            )
        )
        server.start_background()
        wall = _wall(qapp, config_manager)
        try:
            wall._camera_to_slot["cam_A"] = 0
            wall._tiles[0].set_camera("cam_A", "cam_A", "SN-A")
            wall._poll_ptz_status()
            deadline = time.monotonic() + 15.0
            while (
                wall._tiles[0]._ptz_label.text() == ""
                and time.monotonic() < deadline
            ):
                QApplication.processEvents()
                time.sleep(0.05)
            QApplication.processEvents()
            assert wall._tiles[0]._ptz_label.text().startswith("PTZ")
        finally:
            wall._stop_ptz_timer()
            wall._shutdown_ptz_services()
            wall.close()
            server.stop_background()
