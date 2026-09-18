"""Phase 10 session GUI tests: toolbar presence, session install, counts, clearing."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtWidgets import QApplication, QPushButton, QWidget  # noqa: E402

import thermal_monitor.camera.source  # noqa: F401 (init camera package first)
from thermal_monitor.core.models import CameraConfig, CameraIdentity  # noqa: E402
from thermal_monitor.roi.enums import RoiObjectType  # noqa: E402
from thermal_monitor.roi.geometry import SpotGeometry  # noqa: E402
from thermal_monitor.roi.models import RoiDefinition  # noqa: E402
from thermal_monitor.roi.serialization import roi_to_dict  # noqa: E402
from thermal_monitor.services.configuration import ConfigurationService  # noqa: E402
from thermal_monitor.services.mode import ModeService  # noqa: E402
from thermal_monitor.ui.windows.configuration_window import (  # noqa: E402
    ConfigurationModeWidget,
)


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


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
    try:
        w._image_widget.close()
    except Exception:
        pass
    try:
        w._vl_widget.close()
    except Exception:
        pass
    w.close()


def _payload(position_generation=1, context_generation=3):
    from thermal_monitor.roi.context import RoiActiveContext
    roi = RoiDefinition(
        roi_id="roi_1", camera_id="camA", ptz_id="PTZ_01", position_id="pos_A",
        object_type=RoiObjectType.SPOT, geometry=SpotGeometry(row=5, col=5))
    context = RoiActiveContext(
        camera_id="camA", ptz_id="PTZ_01", position_id="pos_A",
        position_generation=position_generation, session_generation=1,
        context_generation=context_generation, state="active",
        operation_id="roiop_1", roi_ids=("roi_1",))
    return {"context": context, "roi_dicts": [roi_to_dict(roi)]}


def test_roi_toolbar_inside_camera_region(widget):
    toolbar = widget.findChild(QWidget, "cfg_roi_toolbar")
    assert toolbar is not None
    center = widget.findChild(QWidget, "cfg_center_workspace")
    assert toolbar in center.findChildren(QWidget)
    # Compact: icon buttons only, no QPushButton text buttons in the center.
    assert center.findChildren(QPushButton) == []


def test_session_install_sets_editor_and_label(widget, qapp):
    widget._selected_camera_id = "camA"
    generation = widget._session.generation
    widget._on_roi_session_loaded("camA", generation, _payload())
    qapp.processEvents()
    canvas = widget._roi_canvas
    assert canvas.editor is not None
    assert [r.roi_id for r in canvas.editor.rois] == ["roi_1"]
    label = widget._roi_toolbar._context_label.text()
    assert "camA" in label and "pos_A" in label or "Furnace" in label
    assert widget._roi_session_active is True
    # Tools enabled once the session is active.
    assert widget._roi_toolbar._group_buttons["Spots"].isEnabled()


def test_no_rois_state_allows_creation(widget, qapp):
    import dataclasses
    widget._selected_camera_id = "camA"
    generation = widget._session.generation
    payload = _payload()
    payload["context"] = dataclasses.replace(payload["context"], roi_ids=())
    payload["roi_dicts"] = []
    widget._on_roi_session_loaded("camA", generation, payload)
    qapp.processEvents()
    assert widget._roi_canvas.editor is not None
    assert "No ROI configured" in widget._roi_toolbar._context_label.text()
    assert widget._roi_toolbar._group_buttons["Regions"].isEnabled()


def test_stale_session_payload_rejected(widget, qapp):
    widget._selected_camera_id = "camA"
    old_generation = widget._session.generation
    widget._begin_session("camA")  # bump the session epoch
    widget._on_roi_session_loaded("camA", old_generation, _payload())
    qapp.processEvents()
    assert widget._roi_canvas.editor is None
    assert widget._roi_session_active is False


def test_camera_switch_clears_session(widget, qapp):
    widget._selected_camera_id = "camA"
    generation = widget._session.generation
    widget._on_roi_session_loaded("camA", generation, _payload())
    qapp.processEvents()
    assert widget._roi_canvas.editor is not None
    widget._clear_roi_session("No position")
    qapp.processEvents()
    assert widget._roi_canvas.editor is None
    assert widget._roi_toolbar._context_label.text() == "No position"


def test_roi_counts_reach_position_table(widget, qapp):
    widget._selected_camera_id = "camA"
    generation = widget._session.generation
    widget._pos_panel.set_station("camA", "PTZ_01")
    from thermal_monitor.ptz.positions import PtzPosition
    widget._pos_panel.set_positions([
        PtzPosition(position_id="pos_A", camera_id="camA", ptz_id="PTZ_01",
                    name="Furnace", pan=0.0, tilt=0.0)])
    widget._on_roi_counts("camA", generation, {"pos_A": 2})
    qapp.processEvents()
    assert widget._pos_panel._tree.topLevelItem(0).text(4) == "2"


def test_failed_session_keeps_previous(widget, qapp):
    widget._selected_camera_id = "camA"
    generation = widget._session.generation
    widget._on_roi_session_loaded("camA", generation, _payload())
    qapp.processEvents()
    assert widget._roi_canvas.editor is not None
    bad = _payload()
    bad["roi_dicts"] = [{"roi_id": "", "camera_id": "camA"}]  # invalid
    widget._on_roi_session_loaded("camA", generation, bad)
    qapp.processEvents()
    # Previous valid session preserved, never a partial context.
    assert [r.roi_id for r in widget._roi_canvas.editor.rois] == ["roi_1"]
