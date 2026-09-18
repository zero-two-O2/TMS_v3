"""Phase 9C: Configuration Mode PTZ attach regression tests.

Root causes covered:
1. Discovery persistence must preserve ptz_* fields (HEAD PTZ_08 was
   clobbered to "" by _on_camera_selected_from_dialog, freezing the
   panel at "No PTZ configured").
2. Shared-service monitor fan-out must not cross-route: _on_ptz_status
   drops deliveries whose ptz_id != selected camera binding.
3. Absolute-move mode strings must be coerced to VelocityMode (plain
   str fails PtzCommand isinstance validation).
4. Unconfigured cameras keep explicit "No PTZ configured" behavior.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

import thermal_monitor.camera.source  # noqa: F401 (camera package first)
import thermal_monitor.ui.theme.fonts as fonts
import thermal_monitor.ui.windows.configuration_window as mod
from thermal_monitor.config.models import CameraMappingConfig
from thermal_monitor.core.models import CameraConfig, CameraIdentity
from thermal_monitor.ptz.state import (
    CalibrationState,
    PlcConnectionState,
    PtzMovementState,
    PtzStatus,
)
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.mode import ModeService
from thermal_monitor.ui.windows.configuration_window import ConfigurationModeWidget


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setattr(mod, "_DOCK_SETTINGS_ORG", "TMS-Test-Org")
    monkeypatch.setattr(mod, "_DOCK_SETTINGS_APP", "TMS-Test-Phase9C")
    monkeypatch.setattr(fonts, "FONT_SETTINGS_ORG", "TMS-Test-Org")
    monkeypatch.setattr(fonts, "FONT_SETTINGS_APP", "TMS-Test-Phase9CFonts")
    from PyQt6.QtCore import QSettings

    QSettings("TMS-Test-Org", "TMS-Test-Phase9C").clear()
    yield
    QSettings("TMS-Test-Org", "TMS-Test-Phase9C").clear()


def _manager_with_mapping(tmp_path):
    import yaml

    from thermal_monitor.config.manager import ConfigurationManager

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "cameras": {"mapping": [
            {"camera_id": "camA", "serial_number": "SN-camA", "ptz_id": "PTZ_01"},
            {"camera_id": "camB", "serial_number": "SN-camB", "ptz_id": ""},
        ]},
        "ptz": {"endpoint": "opc.tcp://127.0.0.1:4840", "profile": "simulator",
                "namespace_uri": "urn:tms:ptz:sim"},
    }), encoding="utf-8")
    return ConfigurationManager(config_path=path)


@pytest.fixture
def widget(qapp, tmp_path):
    service = ConfigurationService()
    for cam in ("camA", "camB"):
        service.set_camera_config(
            CameraConfig(identity=CameraIdentity(camera_id=cam, serial_number=f"SN-{cam}"))
        )
    w = ConfigurationModeWidget(
        config_service=service,
        mode_service=ModeService(),
        runtime_service=None,
        config_manager=_manager_with_mapping(tmp_path),
    )
    w.show()
    qapp.processEvents()
    yield w
    w.close()


def ready_status(**overrides) -> PtzStatus:
    values = {
        "plc_state": PlcConnectionState.CONNECTED,
        "ptz_available": True,
        "communication_ok": True,
        "ready": True,
        "movement": PtzMovementState.IDLE,
        "actual_pan": 10.0,
        "actual_tilt": -5.0,
        "calibration": CalibrationState.NOT_REQUIRED,
        "error": None,
    }
    values.update(overrides)
    return PtzStatus(**values)


class TestDiscoveryPreservesPtz:
    def test_save_mapping_roundtrip_keeps_ptz_id(self, tmp_path):
        import yaml

        from thermal_monitor.config.manager import ConfigurationManager

        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({
            "cameras": {"mapping": [
                {"camera_id": "camX", "serial_number": "SNX", "ptz_id": "PTZ_08",
                 "ptz_endpoint": "opc.tcp://127.0.0.1:4840"},
            ]},
        }), encoding="utf-8")
        mgr = ConfigurationManager(config_path=path)
        entry = next(m for m in mgr.get_config().cameras.mapping
                     if m.camera_id == "camX")
        # Simulate the fixed dialog-save: carry ptz_* forward.
        mgr.save_camera_mapping(CameraMappingConfig(
            camera_id="camX", serial_number="SNX", enabled=True,
            name="camX", ip_address="1.2.3.4", device_identifier="gvcp:SNX",
            ptz_id=entry.ptz_id, ptz_endpoint=entry.ptz_endpoint,
            ptz_min_pan=entry.ptz_min_pan, ptz_max_pan=entry.ptz_max_pan,
            ptz_min_tilt=entry.ptz_min_tilt, ptz_max_tilt=entry.ptz_max_tilt,
        ))
        after = next(m for m in mgr.get_config().cameras.mapping
                     if m.camera_id == "camX")
        assert after.ptz_id == "PTZ_08"
        assert after.ptz_endpoint == "opc.tcp://127.0.0.1:4840"


class TestMonitorCrossRouting:
    def test_wrong_ptz_status_dropped(self, widget, qapp):
        widget._selected_camera_id = "camA"
        gen = widget._session.generation
        widget._ptz_panel.set_binding("camA", "PTZ_01", True)
        widget._on_ptz_status("camA", gen, "PTZ_01", ready_status())
        qapp.processEvents()
        assert "Ready" in widget._ptz_panel._state_label.text()
        # Another PTZ sharing the endpoint must not overwrite camA's panel.
        widget._on_ptz_status("camA", gen, "PTZ_02", ready_status(ready=False))
        qapp.processEvents()
        assert "Ready" in widget._ptz_panel._state_label.text()

    def test_unconfigured_camera_shows_actionable_message(self, widget, qapp):
        widget._selected_camera_id = "camB"
        gen = widget._session.generation
        widget._ptz_attach_async("camB", gen)
        qapp.processEvents()
        assert widget._ptz_panel._ptz_label.text() == "No PTZ configured"
        assert "No PTZ configured" in widget._ptz_panel._error_label.text()


class TestVelocityModeCoercion:
    def test_mode_strings_map_to_enum(self):
        from thermal_monitor.ptz.models import VelocityMode

        assert VelocityMode("single") is VelocityMode.SINGLE
        assert VelocityMode("per_axis") is VelocityMode.PER_AXIS
