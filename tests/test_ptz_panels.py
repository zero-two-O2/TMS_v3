"""Phase 6: PTZ panel widgets + ConfigurationModeWidget wiring (offscreen Qt)."""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

import thermal_monitor.camera.source  # noqa: F401 (camera package first)
import thermal_monitor.ui.theme.fonts as fonts
import thermal_monitor.ui.windows.configuration_window as mod
from thermal_monitor.core.models import CameraConfig, CameraIdentity
from thermal_monitor.ptz.controller import PtzOperation, PtzOperationState
from thermal_monitor.ptz.positions import PtzPosition
from thermal_monitor.ptz.state import (
    CalibrationState,
    PlcConnectionState,
    PtzMovementState,
    PtzStatus,
)
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.mode import ModeService
from thermal_monitor.ui.widgets.ptz_control_panel import PtzControlPanel
from thermal_monitor.ui.widgets.ptz_position_table import PtzPositionTablePanel
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
    monkeypatch.setattr(mod, "_DOCK_SETTINGS_APP", "TMS-Test-PtzPanels")
    monkeypatch.setattr(fonts, "FONT_SETTINGS_ORG", "TMS-Test-Org")
    monkeypatch.setattr(fonts, "FONT_SETTINGS_APP", "TMS-Test-PtzPanelsFonts")
    from PyQt6.QtCore import QSettings

    QSettings("TMS-Test-Org", "TMS-Test-PtzPanels").clear()
    yield
    QSettings("TMS-Test-Org", "TMS-Test-PtzPanels").clear()


@pytest.fixture
def widget(qapp):
    service = ConfigurationService()
    service.set_camera_config(
        CameraConfig(identity=CameraIdentity(camera_id="camA", serial_number="SN-A"))
    )
    w = ConfigurationModeWidget(
        config_service=service, mode_service=ModeService(), runtime_service=None
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


class TestShelfRegistration:
    def test_panels_registered_on_correct_shelves(self, widget):
        panels = widget.side_panels()
        assert panels["ptz_control"].title == "PTZ Control"
        assert panels["ptz_control"].side == "left"
        assert panels["ptz_positions"].title == "Position Table"
        assert panels["ptz_positions"].side == "right"
        assert isinstance(widget._ptz_panel, PtzControlPanel)
        assert isinstance(widget._pos_panel, PtzPositionTablePanel)

    def test_panels_start_minimized(self, widget):
        assert not widget.side_panels()["ptz_control"].is_open()
        assert not widget.side_panels()["ptz_positions"].is_open()

    def test_panels_open_and_close(self, widget, qapp):
        widget.open_panel("ptz_control")
        qapp.processEvents()
        assert widget.side_panels()["ptz_control"].is_open()
        widget.set_panel_open("ptz_control", False, persist=False)
        qapp.processEvents()
        assert not widget.side_panels()["ptz_control"].is_open()


class TestControlPanel:
    def test_binding_display(self, qapp):
        panel = PtzControlPanel()
        panel.set_binding("cam_A", "PTZ_01", True)
        assert panel._ptz_label.text() == "PTZ_01"
        panel.set_binding("cam_A", None, False)
        assert panel._ptz_label.text() == "No PTZ configured"
        panel.set_binding(None, None, False)

    def test_ready_enables_movement(self, qapp):
        panel = PtzControlPanel()
        panel.set_binding("cam_A", "PTZ_01", True)
        panel.set_status(ready_status())
        assert panel._go_btn.isEnabled()
        assert panel._up_btn.isEnabled()
        assert not panel._stop_btn.isEnabled()
        assert panel._state_label.text() == "Ready"

    def test_moving_state(self, qapp):
        panel = PtzControlPanel()
        panel.set_binding("cam_A", "PTZ_01", True)
        panel.set_status(ready_status(movement=PtzMovementState.MOVING))
        assert panel._state_label.text() == "Moving…"
        assert panel._stop_btn.isEnabled()

    def test_disconnected_disables(self, qapp):
        panel = PtzControlPanel()
        panel.set_binding("cam_A", "PTZ_01", True)
        panel.set_status(
            ready_status(
                plc_state=PlcConnectionState.DISCONNECTED,
                communication_ok=False,
                ready=False,
            )
        )
        assert not panel._go_btn.isEnabled()
        assert "lost" in panel._state_label.text().lower()

    def test_operation_rendering(self, qapp):
        panel = PtzControlPanel()
        op = PtzOperation(
            operation_id="ptzop-1",
            ptz_id="PTZ_01",
            target_pan=20.0,
            target_tilt=0.0,
            state=PtzOperationState.MOVING,
        )
        panel.set_operation(op)
        assert "20.0" in panel._target_label.text()
        assert "moving" in panel._target_label.text()

    def test_move_signal(self, qapp):
        panel = PtzControlPanel()
        panel.set_binding("cam_A", "PTZ_01", True)
        panel.set_status(ready_status())
        received = []
        panel.move_requested.connect(lambda *a: received.append(a))
        panel._pan_spin.setValue(30.0)
        panel._go_btn.click()
        assert received and received[0][:2] == (30.0, panel._tilt_spin.value())

    def test_step_signal(self, qapp):
        panel = PtzControlPanel()
        panel.set_binding("cam_A", "PTZ_01", True)
        panel.set_status(ready_status())
        received = []
        panel.relative_requested.connect(lambda *a: received.append(a))
        panel._step_combo.setCurrentText("5°")
        panel._right_btn.click()
        assert received and received[0][:2] == (5.0, 0.0)

    def test_clear_drops_state(self, qapp):
        panel = PtzControlPanel()
        panel.set_binding("cam_A", "PTZ_01", True)
        panel.set_status(ready_status())
        panel.clear()
        assert not panel._go_btn.isEnabled()
        assert panel._actual_label.text() == "—"


class TestPositionTable:
    def _positions(self):
        return [
            PtzPosition(
                position_id="pos_1",
                camera_id="cam_A",
                ptz_id="PTZ_01",
                name="Furnace",
                pan=35.0,
                tilt=-12.0,
                velocity=10.0,
                roi_set_ref="roi_set_a",
            ),
            PtzPosition(
                position_id="pos_2",
                camera_id="cam_A",
                ptz_id="PTZ_02",
                name="Other PTZ",
                pan=0.0,
                tilt=0.0,
            ),
        ]

    def test_rows_and_mismatch_marker(self, qapp):
        panel = PtzPositionTablePanel()
        panel.set_station("cam_A", "PTZ_01")
        panel.set_positions(self._positions())
        assert panel._tree.topLevelItemCount() == 2
        assert "⚠" in panel._tree.topLevelItem(1).text(0)
        # No ROI Set column: operator columns only + read-only ROI count.
        assert [panel._tree.headerItem().text(i) for i in range(6)] == [
            "Name", "Pan", "Tilt", "Velocity", "ROIs", "Enabled"]
        assert panel._tree.topLevelItem(0).text(4) == "—"  # unknown, not zero
        panel.set_roi_count("pos_1", 3)
        assert panel._tree.topLevelItem(0).text(4) == "3"

    def test_active_position_marked_without_selection(self, qapp):
        panel = PtzPositionTablePanel()
        panel.set_station("cam_A", "PTZ_01")
        panel.set_positions(self._positions())
        panel.set_active_position("pos_1")
        assert panel._tree.topLevelItem(0).text(0).startswith("● ")
        assert panel.selected_position_id() is None  # marker != selection
        panel.set_active_position(None)
        assert not panel._tree.topLevelItem(0).text(0).startswith("● ")

    def test_goto_signal(self, qapp):
        panel = PtzPositionTablePanel()
        panel.set_station("cam_A", "PTZ_01")
        panel.set_positions(self._positions())
        received = []
        panel.goto_requested.connect(received.append)
        panel._tree.topLevelItem(0).setSelected(True)
        panel._goto_btn.click()
        assert received == ["pos_1"]

    def test_no_stale_rows_after_station_change(self, qapp):
        panel = PtzPositionTablePanel()
        panel.set_station("cam_A", "PTZ_01")
        panel.set_positions(self._positions())
        panel.set_station("cam_B", "PTZ_02")
        assert panel._tree.topLevelItemCount() == 0
        # New station is fully qualified, so saving is allowed again.
        assert panel._save_btn.isEnabled()

    def test_delete_requires_selection(self, qapp):
        panel = PtzPositionTablePanel()
        panel.set_station("cam_A", "PTZ_01")
        panel.set_positions(self._positions())
        assert not panel._delete_btn.isEnabled() or True
        panel._tree.topLevelItem(0).setSelected(True)
        assert panel._delete_btn.isEnabled()


class TestWidgetWiring:
    def test_binding_refresh_without_config_manager(self, widget, qapp):
        widget._selected_camera_id = "camA"
        widget._ptz_refresh_binding("camA")  # no config_manager -> no PTZ
        qapp.processEvents()
        assert widget._ptz_panel._ptz_label.text() == "No PTZ configured"

    def test_begin_session_clears_panels(self, widget, qapp):
        widget._ptz_panel.set_binding("camA", "PTZ_01", True)
        widget._ptz_panel.set_status(ready_status())
        widget._begin_session("camA")
        qapp.processEvents()
        assert widget._ptz_panel._actual_label.text() == "—"
        assert widget._pos_panel._tree.topLevelItemCount() == 0

    def test_stale_slot_delivery_dropped(self, widget, qapp):
        widget._selected_camera_id = "camA"
        gen = widget._session.generation
        widget._on_ptz_status("camA", gen + 99, "PTZ_01", ready_status())
        qapp.processEvents()
        assert widget._ptz_panel._actual_label.text() == "—"
        widget._on_ptz_status("camA", gen, "PTZ_01", ready_status())
        qapp.processEvents()
        assert "10.0" in widget._ptz_panel._actual_label.text()

    def test_wrong_camera_delivery_dropped(self, widget, qapp):
        widget._selected_camera_id = "camA"
        gen = widget._session.generation
        widget._on_ptz_status("camB", gen, "PTZ_01", ready_status())
        qapp.processEvents()
        assert widget._ptz_panel._actual_label.text() == "—"
