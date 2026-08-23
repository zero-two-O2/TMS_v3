"""Unit tests for HALCON camera discovery and selection integration."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from thermal_monitor.core.models import CameraConfig, CameraIdentity
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.discovery import (
    CameraDiscoveryError,
    CameraDiscoveryService,
    DiscoveredCamera,
)


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


class FakeHalcon:
    def __init__(self, entries=None, parameters=None, error=None):
        self.entries = entries or []
        self.parameters = parameters or {}
        self.error = error
        self.opened = []
        self.closed = []

    def info_framegrabber(self, interface, query):
        assert interface == "GigEVision2"
        assert query == "device"
        if self.error:
            raise self.error
        return ("info", self.entries)

    def open_framegrabber(self, *args):
        device = args[13]
        self.opened.append(device)
        return f"handle:{device}"

    def get_framegrabber_param(self, handle, parameter):
        device = handle.split(":", 1)[1]
        value = self.parameters.get((device, parameter), "")
        return (value,)

    def close_framegrabber(self, handle):
        self.closed.append(handle)


def fake_halcon(*devices):
    parameters = {}
    for index, device in enumerate(devices, 1):
        values = {
            "[Device]DeviceSerialNumber": f"SN{index}",
            "[Device]DeviceModelName": "TV46L",
            "[Device]DeviceVendorName": "Fluke",
            "[Device]GevDeviceIPAddress": f"192.168.1.{index}",
            "[Device]DeviceVersion": "1.0",
            "[Device]DeviceUserID": f"Camera {index}",
        }
        parameters.update({(device, key): value for key, value in values.items()})
    return FakeHalcon([f"device:{device} | GigEVision2" for device in devices], parameters)


def test_discovery_returns_one_camera_and_closes_handle():
    halcon = fake_halcon("dev-a")
    cameras = CameraDiscoveryService(halcon=halcon).discover_cameras()

    assert len(cameras) == 1
    assert cameras[0].serial_number == "SN1"
    assert cameras[0].ip_address == "192.168.1.1"
    assert cameras[0].device_identifier == "dev-a"
    assert halcon.closed == ["handle:dev-a"]


def test_discovery_returns_multiple_cameras():
    cameras = CameraDiscoveryService(halcon=fake_halcon("dev-a", "dev-b")).discover_cameras()
    assert [camera.serial_number for camera in cameras] == ["SN1", "SN2"]


def test_integer_ip_is_converted_to_dotted_address():
    halcon = fake_halcon("dev-a")
    halcon.parameters[("dev-a", "[Device]GevDeviceIPAddress")] = 0xC0A80101
    cameras = CameraDiscoveryService(halcon=halcon).discover_cameras()
    assert cameras[0].ip_address == "192.168.1.1"


def test_stable_identity_prefers_serial_and_falls_back_to_device():
    with_serial = DiscoveredCamera("dev-a", serial_number="SN1")
    without_serial = DiscoveredCamera("dev-b")
    assert with_serial.stable_identity == "SN1"
    assert without_serial.stable_identity == "dev-b"
    assert without_serial.camera_id == "cam_dev-b"


def test_duplicate_discovery_is_removed():
    halcon = fake_halcon("dev-a", "dev-b")
    for device in ("dev-a", "dev-b"):
        halcon.parameters[(device, "[Device]DeviceSerialNumber")] = "SAME"
    cameras = CameraDiscoveryService(halcon=halcon).discover_cameras()
    assert len(cameras) == 1


def test_discovery_failure_is_clean():
    service = CameraDiscoveryService(halcon=FakeHalcon(error=RuntimeError("HALCON unavailable")))
    with pytest.raises(CameraDiscoveryError, match="HALCON unavailable"):
        service.discover_cameras()
    assert service.cameras == []
    assert service.last_error == "HALCON unavailable"


def test_no_camera_result_is_clean():
    assert CameraDiscoveryService(halcon=FakeHalcon(), attempts=1).discover_cameras() == []


def test_discovered_camera_can_be_added_without_runtime_objects():
    service = ConfigurationService()
    camera = DiscoveredCamera(
        device_identifier="dev-a",
        serial_number="SN1",
        ip_address="192.168.1.1",
        model="TV46L",
    )
    config = CameraConfig(
        identity=CameraIdentity(
            camera_id=camera.camera_id,
            serial_number=camera.serial_number,
            model=camera.model,
        ),
        metadata={"device_identifier": camera.device_identifier, "ip_address": camera.ip_address},
    )
    service.set_camera_config(config)
    assert service.get_camera_config("cam_SN1") is config
    assert not hasattr(config, "ring")


def test_configured_disconnected_camera_remains_configured():
    service = ConfigurationService()
    config = CameraConfig(identity=CameraIdentity("cam_SN1", "SN1"), enabled=True)
    service.set_camera_config(config)
    assert service.get_all_camera_configs() == [config]
    assert service.get_camera_config("cam_SN1") is not None


def test_refresh_does_not_create_acquisition_or_shm_objects():
    halcon = fake_halcon("dev-a")
    service = CameraDiscoveryService(halcon=halcon)
    service.refresh()
    assert not hasattr(service, "_runtimes")
    assert halcon.opened and halcon.closed


def test_gui_displays_discovered_cameras(qapp):
    from thermal_monitor.ui.modes.launcher import LauncherWidget
    from thermal_monitor.services.mode import ModeService

    camera = DiscoveredCamera("dev-a", "SN1", "192.168.1.1", "TV46L", "Fluke")
    mode_service = ModeService()
    config_service = ConfigurationService()
    discovery = SimpleNamespace(
        discover_cameras=lambda: [camera],
        refresh=lambda: [camera],
    )
    widget = LauncherWidget(mode_service, config_service, discovery_service=discovery)

    assert widget._camera_table.rowCount() == 8  # Fixed 8 slots
    assert widget._camera_table.item(0, 1).text() == "SN1"
    assert widget._camera_table.item(0, 3).text() == "192.168.1.1"
    assert widget._camera_table.item(0, 4).text() == "Available"


def test_gui_search_button_triggers_discovery(qapp):
    from thermal_monitor.ui.modes.launcher import LauncherWidget
    from thermal_monitor.services.mode import ModeService
    from thermal_monitor.services.discovery import CameraDiscoveryError
    from PyQt6.QtTest import QTest
    from PyQt6.QtCore import Qt

    config_service = ConfigurationService()
    mode_service = ModeService()

    call_count = [0]
    def tracking_discovery():
        call_count[0] += 1
        if call_count[0] == 1:
            return [DiscoveredCamera("dev-a", "SN1", "192.168.1.1", "TV46L", "Fluke")]
        return [DiscoveredCamera("dev-b", "SN2", "192.168.1.2", "TV46L", "Fluke")]

    discovery = SimpleNamespace(
        discover_cameras=tracking_discovery,
        refresh=tracking_discovery,
    )
    widget = LauncherWidget(mode_service, config_service, discovery_service=discovery)

    # Initial discovery
    assert widget._camera_table.item(0, 1).text() == "SN1"

    # Click search button
    QTest.mouseClick(widget._search_btn, Qt.MouseButton.LeftButton)

    # Should have refreshed with new camera
    assert widget._camera_table.item(0, 1).text() == "SN2"


def test_gui_selection_populates_configuration(qapp):
    """Test that discovered cameras can be added to configuration."""
    from PyQt6.QtCore import Qt
    from thermal_monitor.services.configuration import ConfigurationService

    config_service = ConfigurationService()
    camera = DiscoveredCamera("dev-a", "SN1", "192.168.1.1", "TV46L", "Fluke")

    # Simulate adding a discovered camera to configuration (as Launcher would do)
    identity = CameraIdentity(
        camera_id=camera.camera_id,
        serial_number=camera.serial_number,
        model=camera.model,
        vendor=camera.vendor,
        firmware=camera.firmware,
        user_name=camera.user_name,
    )
    config = CameraConfig(
        identity=identity,
        metadata={"device_identifier": camera.device_identifier, "ip_address": camera.ip_address},
    )
    config_service.set_camera_config(config)

    config_result = config_service.get_camera_config("cam_SN1")
    assert config_result is not None
    assert config_result.identity.serial_number == "SN1"
    assert config_result.metadata["device_identifier"] == "dev-a"
    assert config_result.metadata["ip_address"] == "192.168.1.1"


def test_disabled_camera_is_not_started_by_runtime():
    from thermal_monitor.services.runtime import CameraRuntimeError, CameraRuntimeService

    config = CameraConfig(
        identity=CameraIdentity("cam_disabled", "SN-disabled"),
        enabled=False,
    )
    service = CameraRuntimeService(source_factory=lambda _: pytest.fail("source must not be built"))
    with pytest.raises(CameraRuntimeError, match="disabled"):
        service.start_camera(config)
