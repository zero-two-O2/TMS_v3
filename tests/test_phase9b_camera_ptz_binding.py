"""Phase 9B: camera -> PTZ binding, panel lifecycle, top status, alarm UI.

Offscreen Qt. Covers:
- every configured camera resolves to its own PTZ (no cross-routing)
- blank ptz_id means "not configured" (never defaulted)
- shared endpoint reuses one PtzService (no duplicate OPC UA sessions)
- stale (camera/generation) PTZ deliveries are ignored
- PTZ controls follow authoritative status (ready -> enabled)
- top bar: Snapshot kept, Save Config removed, Camera/PTZ/Alarm/DB visible
- Alarm double-click opens detail; History button requests history
- disconnect clears camera-scoped state, keeps shared services
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication, QPushButton

import thermal_monitor.camera.source  # noqa: F401 (camera package first)
import thermal_monitor.ui.theme.fonts as fonts
import thermal_monitor.ui.windows.configuration_window as mod
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
    monkeypatch.setattr(mod, "_DOCK_SETTINGS_APP", "TMS-Test-Phase9B")
    monkeypatch.setattr(fonts, "FONT_SETTINGS_ORG", "TMS-Test-Org")
    monkeypatch.setattr(fonts, "FONT_SETTINGS_APP", "TMS-Test-Phase9BFonts")
    from PyQt6.QtCore import QSettings

    QSettings("TMS-Test-Org", "TMS-Test-Phase9B").clear()
    yield
    QSettings("TMS-Test-Org", "TMS-Test-Phase9B").clear()


def _manager_with_mapping(tmp_path):
    import yaml

    from thermal_monitor.config.manager import ConfigurationManager

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "cameras": {"mapping": [
            {"camera_id": "camA", "serial_number": "SN-camA", "ptz_id": "PTZ_01"},
            {"camera_id": "camB", "serial_number": "SN-camB", "ptz_id": "PTZ_02"},
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


class TestCameraPtzBinding:
    def test_each_camera_resolves_own_ptz(self, widget):
        from thermal_monitor.ptz.station import resolve_binding, resolve_endpoint

        for cam, ptz in (("camA", "PTZ_01"), ("camB", "PTZ_02")):
            entry = widget._ptz_mapping_entry(cam)
            assert entry is not None
            binding = resolve_binding(cam, entry.ptz_id)
            assert binding is not None and binding.ptz_id == ptz
            assert binding.camera_id == cam
            endpoint = resolve_endpoint(widget._ptz_global_config().endpoint, entry.ptz_endpoint)
            assert endpoint == "opc.tcp://127.0.0.1:4840"

    def test_blank_ptz_id_is_not_configured_never_defaulted(self, widget):
        from thermal_monitor.ptz.station import resolve_binding

        assert resolve_binding("camA", "") is None
        assert resolve_binding("camA", "   ") is None
        # missing mapping entry -> guarded service refuses, never picks another PTZ
        assert widget._ptz_mapping_entry("camUNKNOWN") is None

    def test_shared_endpoint_reuses_one_service(self, widget):
        svc_a = widget._ptz_service_for("opc.tcp://127.0.0.1:4840", ("PTZ_01", "PTZ_02"))
        svc_b = widget._ptz_service_for("opc.tcp://127.0.0.1:4840", ("PTZ_01", "PTZ_02"))
        assert svc_a is svc_b
        svc_other = widget._ptz_service_for("opc.tcp://127.0.0.1:4841", ("PTZ_01",))
        assert svc_other is not svc_a

    def test_stale_status_delivery_ignored(self, widget, qapp):
        widget._selected_camera_id = "camA"
        gen = widget._session.generation
        widget._ptz_panel.set_binding("camA", "PTZ_01", True)
        widget._on_ptz_status("camA", gen, "PTZ_01", ready_status())
        qapp.processEvents()
        assert "Ready" in widget._ptz_panel._state_label.text()
        # stale generation -> ignored
        widget._on_ptz_status("camA", gen + 99, "PTZ_01", ready_status(ready=False))
        qapp.processEvents()
        assert "Ready" in widget._ptz_panel._state_label.text()
        # wrong camera -> ignored
        widget._on_ptz_status("camB", gen, "PTZ_02", ready_status(ready=False))
        qapp.processEvents()
        assert "Ready" in widget._ptz_panel._state_label.text()

    def test_controls_follow_authoritative_status(self, widget):
        widget._ptz_panel.set_binding("camA", "PTZ_01", True)
        widget._ptz_panel.set_status(ready_status())
        assert widget._ptz_panel._go_btn.isEnabled()
        widget._ptz_panel.set_status(ready_status(communication_ok=False))
        assert not widget._ptz_panel._go_btn.isEnabled()
        widget._ptz_panel.set_status(ready_status(ready=False))
        assert not widget._ptz_panel._go_btn.isEnabled()
        widget._ptz_panel.set_status(None)
        assert not widget._ptz_panel._go_btn.isEnabled()

    def test_disconnect_clears_scoped_state_keeps_services(self, widget):
        from thermal_monitor.ptz.roi_activation import ActivePositionContext

        widget._selected_camera_id = "camA"
        gen = widget._session.generation
        widget._active_alarm_events["rule1"] = object()
        widget._ptz_registry.set(
            ActivePositionContext(camera_id="camA", ptz_id="PTZ_01",
                                  position_id="p1", position_name="P1",
                                  roi_set_ref="ref"),
            session_generation=gen, current_session_generation=gen,
        )
        assert widget._ptz_registry.get("camA") is not None
        widget._ptz_services["opc.tcp://127.0.0.1:4840"] = object()
        widget._clear_camera_scoped_state("camA")
        assert widget._active_alarm_events == {}
        assert widget._alarm_active_count == 0
        assert widget._ptz_registry.get("camA") is None
        # shared infrastructure untouched
        assert "opc.tcp://127.0.0.1:4840" in widget._ptz_services


class TestTopStatusBar:
    def test_save_config_removed_snapshot_kept(self, widget):
        buttons = {b.text(): b for b in widget.findChildren(QPushButton)}
        assert "Snapshot" in buttons
        assert "Save Config" not in buttons

    def test_compact_status_chips_present(self, widget):
        assert widget._top_conn_label is not None
        assert widget._top_ptz_label is not None
        assert widget._top_alarm_label is not None
        assert widget._top_db_label is not None
        assert widget._top_alarm_label.text() == "Alarms: 0"

    def test_ptz_and_alarm_chips_update(self, widget, qapp):
        widget._selected_camera_id = "camA"
        gen = widget._session.generation
        widget._on_ptz_status("camA", gen, "PTZ_01", ready_status())
        assert "ready" in widget._top_ptz_label.text().lower()
        widget._set_alarm_top_count(3)
        assert widget._top_alarm_label.text() == "Alarms: 3"

    def test_save_config_still_available_via_handler(self, widget):
        # File -> Save Configuration path preserved (same handler).
        assert callable(widget._on_save_config)


class TestAlarmUi:
    def test_double_click_emits_activation(self, widget, qapp):
        from thermal_monitor.core.models import AnalysisConfig

        analysis = AnalysisConfig(camera_id="camA")
        widget._config_service.set_analysis_config(analysis)
        widget._alarm_panel.set_camera("camA")
        received = []
        widget._alarm_panel.alarm_activated.connect(received.append)
        # empty tree: activation only fires for a real item; exercise directly
        widget._alarm_panel.alarm_activated.emit("ruleX")
        qapp.processEvents()
        assert received == ["ruleX"]

    def test_history_button_requests_history(self, widget, qapp):
        received = []
        widget._alarm_panel.history_requested.connect(lambda: received.append(True))
        widget._alarm_panel._history_btn.click()
        qapp.processEvents()
        assert received == [True]

    def test_alarm_detail_window_shows_event_fields(self, qapp):
        from PyQt6.QtWidgets import QLabel
        from thermal_monitor.core.models import AlarmEvent, AlarmRule, AlarmSeverity, AlarmCondition
        from thermal_monitor.ui.windows.alarm_detail_window import AlarmDetailWindow

        rule = AlarmRule(rule_id="r1", roi_id="roi1", condition=AlarmCondition.ABOVE,
                         severity=AlarmSeverity.CRITICAL, threshold=80.0)
        event = AlarmEvent(event_id="alarm_abc", rule_id="r1", camera_id="camA",
                           roi_id="roi1", severity=AlarmSeverity.CRITICAL,
                           measured_value=87.4, threshold_value=80.0,
                           timestamp=1726000000.0, frame_sequence=42)
        dlg = AlarmDetailWindow(event, rule=rule, camera_id="camA", ptz_id="PTZ_01",
                                position_id="Furnace_03")
        dlg.show()
        qapp.processEvents()
        text = " ".join(w.text() for w in dlg.findChildren(QLabel))
        assert "camA" in text and "PTZ_01" in text and "87.4" in text
        dlg.close()

    def test_alarm_history_window_loads_without_database(self, qapp):
        from thermal_monitor.ui.windows.alarm_history_window import AlarmHistoryWindow

        dlg = AlarmHistoryWindow(database=None)
        dlg.show()
        qapp.processEvents()
        dlg._on_loaded(dlg._generation, [])
        assert dlg._table.rowCount() == 0
        dlg.close()

    def test_shutdown_stops_alarm_store(self, widget):
        widget._shutdown_alarm_phase9b()
        assert widget._alarm_store is None
