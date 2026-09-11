"""
Tests for Configuration Mode ThermoView-style connection workflow.
"""

from __future__ import annotations

import pytest
import time
from unittest.mock import Mock, MagicMock, patch, PropertyMock

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

from thermal_monitor.ui.widgets.camera_selection_dialog import CameraSelectionDialog
from thermal_monitor.ui.widgets.image_acquisition_panel import ImageAcquisitionPanel
from thermal_monitor.ui.widgets.config_camera_header import ConfigCameraHeader
from thermal_monitor.ui.modes.observer_image import LiveThermalWidget, ROIOverlay
from thermal_monitor.services.discovery import CameraDiscoveryService, DiscoveredCamera
from thermal_monitor.core.models import CameraConnectionState, CameraIdentity
from thermal_monitor.ui.theme import ThemeManager


@pytest.fixture
def qapp():
    """Create QApplication instance."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def mock_theme():
    """Create a mock theme manager."""
    theme = Mock(spec=ThemeManager)
    
    class MockColors:
        background = "#1e1e1e"
        panel = "#2d2d2d"
        border = "#444444"
        text_primary = "#ffffff"
        text_secondary = "#aaaaaa"
        text_muted = "#888888"
        text_disabled = "#666666"
        accent = "#0078d4"
        accent_hover = "#106ebe"
        hover = "#3e3e3e"
        disabled = "#666666"
        success = "#107c10"
        info = "#0078d4"
        warning = "#ff8c00"
        danger = "#e81123"
        secondary_hover = "#3e3e3e"
        primary_disabled_text = "#888888"
        secondary_disabled_text = "#888888"
    
    theme.colors = Mock(return_value=MockColors())
    theme.text_secondary = Mock(return_value="#aaaaaa")
    theme.success = Mock(return_value="#107c10")
    theme.error = Mock(return_value="#e81123")
    theme.warning = Mock(return_value="#ff8c00")
    theme.primary_button_stylesheet = Mock(return_value="")
    theme.secondary_button_stylesheet = Mock(return_value="")
    theme.accent_button_stylesheet = Mock(return_value="")
    theme.base_stylesheet = Mock(return_value="")
    theme.window_config = Mock(return_value={
        "config_min_width": 1400,
        "config_min_height": 900,
        "start_maximized": True,
    })
    
    return theme


@pytest.fixture
def mock_discovery_service():
    """Create a mock discovery service."""
    service = Mock(spec=CameraDiscoveryService)
    service._halcon_interface = "GigEVision2"
    return service


def wait_for_discovery(qapp, dialog, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while dialog._discovery_worker is not None and dialog._discovery_worker.isRunning() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()


def wait_for_render(qapp, widget, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while widget.display_array is None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()


@pytest.fixture
def sample_discovered_cameras():
    """Sample discovered cameras for testing."""
    return [
        DiscoveredCamera(
            device_identifier="device_001",
            serial_number="26010002",
            ip_address="169.254.24.69",
            model="TV46L",
            vendor="FLIR",
            firmware="1.2.3",
            user_name="Camera1",
        ),
        DiscoveredCamera(
            device_identifier="device_002",
            serial_number="26010003",
            ip_address="169.254.24.70",
            model="TV46L",
            vendor="FLIR",
            firmware="1.2.3",
            user_name="Camera2",
        ),
    ]


class TestCameraSelectionDialog:
    """Tests for CameraSelectionDialog."""

    def test_dialog_creation(self, qapp, mock_discovery_service, mock_theme):
        """Test dialog can be created."""
        dialog = CameraSelectionDialog(mock_discovery_service, mock_theme)
        wait_for_discovery(qapp, dialog)
        assert dialog.windowTitle() == "Select Camera"
        assert dialog.isModal()
        dialog.close()

    def test_dialog_populates_cameras(self, qapp, mock_discovery_service, sample_discovered_cameras, mock_theme):
        """Test dialog populates camera list from discovery."""
        mock_discovery_service.discover_cameras.return_value = sample_discovered_cameras
        dialog = CameraSelectionDialog(mock_discovery_service, mock_theme)
        wait_for_discovery(qapp, dialog)
        
        # Check tree has cameras
        assert dialog._camera_tree.topLevelItemCount() == 2
        
        # Check first camera data
        item0 = dialog._camera_tree.topLevelItem(0)
        assert item0.text(1) == "cam_26010002"  # camera_id
        assert item0.text(2) == "26010002"      # serial
        assert item0.text(3) == "169.254.24.69"  # IP
        assert item0.text(4) == "TV46L"          # model
        
        cam = item0.data(0, Qt.ItemDataRole.UserRole)
        assert isinstance(cam, DiscoveredCamera)
        assert cam.serial_number == "26010002"
        dialog.close()

    def test_refresh_button_calls_discovery(self, qapp, mock_discovery_service, sample_discovered_cameras, mock_theme):
        """Test refresh button triggers discovery."""
        mock_discovery_service.discover_cameras.return_value = sample_discovered_cameras
        dialog = CameraSelectionDialog(mock_discovery_service, mock_theme)
        wait_for_discovery(qapp, dialog)
        
        # Clear and refresh
        dialog._camera_tree.clear()
        dialog._refresh_cameras()
        wait_for_discovery(qapp, dialog)
        
        assert mock_discovery_service.discover_cameras.call_count == 2  # init + refresh
        assert dialog._camera_tree.topLevelItemCount() == 2
        dialog.close()

    def test_selection_enables_connect(self, qapp, mock_discovery_service, sample_discovered_cameras, mock_theme):
        """Test selecting a camera enables Connect button."""
        mock_discovery_service.discover_cameras.return_value = sample_discovered_cameras
        dialog = CameraSelectionDialog(mock_discovery_service, mock_theme)
        wait_for_discovery(qapp, dialog)
        
        # Initially disabled
        assert not dialog._connect_btn.isEnabled()
        
        # Select first camera
        dialog._camera_tree.setCurrentItem(dialog._camera_tree.topLevelItem(0))
        
        # Connect should be enabled
        assert dialog._connect_btn.isEnabled()
        assert dialog._selected_camera == sample_discovered_cameras[0]
        dialog.close()

    def test_double_click_connects(self, qapp, mock_discovery_service, sample_discovered_cameras, mock_theme):
        """Test double-clicking a camera emits camera_selected."""
        mock_discovery_service.discover_cameras.return_value = sample_discovered_cameras
        dialog = CameraSelectionDialog(mock_discovery_service, mock_theme)
        wait_for_discovery(qapp, dialog)
        
        received = []
        dialog.camera_selected.connect(lambda cam: received.append(cam))
        
        # Select first item
        dialog._camera_tree.setCurrentItem(dialog._camera_tree.topLevelItem(0))
        
        # Double-click first item
        item = dialog._camera_tree.topLevelItem(0)
        dialog._on_double_click(item, 0)
        
        assert len(received) == 1
        assert received[0] == sample_discovered_cameras[0]
        dialog.close()


class TestImageAcquisitionPanel:
    """Tests for ImageAcquisitionPanel."""

    def test_panel_creation(self, qapp, mock_theme):
        """Test panel can be created."""
        panel = ImageAcquisitionPanel(mock_theme)
        assert panel.windowTitle() == ""  # No window title for widget
        assert panel._start_btn.text() == "Start"
        assert panel._stop_btn.text() == "Stop"

    def test_initial_state_disconnected(self, qapp, mock_theme):
        """Test initial state shows disconnected."""
        panel = ImageAcquisitionPanel(mock_theme)
        
        assert panel._connection_state == CameraConnectionState.DISCONNECTED
        assert panel._status_text.text() == "Disconnected"
        assert not panel._start_btn.isEnabled()
        assert not panel._stop_btn.isEnabled()
        assert not panel._acq_controls.isEnabled()

    def test_set_camera_identity(self, qapp, mock_theme):
        """Test setting camera identity updates display."""
        panel = ImageAcquisitionPanel(mock_theme)
        identity = CameraIdentity(
            camera_id="cam_001",
            serial_number="26010002",
            model="TV46L",
            vendor="FLIR",
        )
        panel.set_camera_identity(identity)
        
        assert "TV46L" in panel._camera_label.text()
        assert "26010002" in panel._camera_label.text()
        assert panel._info_camera.text() == "TV46L"
        assert panel._info_serial.text() == "26010002"

    def test_connection_state_transitions(self, qapp, mock_theme):
        """Test connection state transitions update UI correctly."""
        panel = ImageAcquisitionPanel(mock_theme)
        
        # DISCONNECTED -> CONNECTING
        panel.set_connection_state(CameraConnectionState.CONNECTING)
        assert panel._connection_state == CameraConnectionState.CONNECTING
        assert "Connecting" in panel._status_text.text()
        
        # CONNECTING -> CONNECTED
        panel.set_connection_state(CameraConnectionState.CONNECTED)
        assert panel._connection_state == CameraConnectionState.CONNECTED
        assert panel._start_btn.isEnabled()
        assert panel._acq_controls.isEnabled()
        
        # CONNECTED -> ACQUIRING
        panel.set_acquisition_running(True)
        assert panel._acquisition_running
        assert not panel._start_btn.isEnabled()
        assert panel._stop_btn.isEnabled()
        
        # ACQUIRING -> CONNECTED (stop)
        panel.set_acquisition_running(False)
        assert not panel._acquisition_running
        assert panel._start_btn.isEnabled()
        assert not panel._stop_btn.isEnabled()
        
        # CONNECTED -> DISCONNECTED
        panel.set_connection_state(CameraConnectionState.DISCONNECTED)
        assert panel._connection_state == CameraConnectionState.DISCONNECTED
        assert not panel._start_btn.isEnabled()
        assert not panel._acq_controls.isEnabled()

    def test_error_state(self, qapp, mock_theme):
        """Test error state displays correctly."""
        panel = ImageAcquisitionPanel(mock_theme)
        panel.set_connection_state(CameraConnectionState.ERROR)
        
        assert panel._connection_state == CameraConnectionState.ERROR
        assert "Error" in panel._status_text.text()


class TestConfigCameraHeader:
    """Tests for ConfigCameraHeader toolbar."""

    def test_toolbar_creation(self, qapp, mock_theme):
        """Test toolbar can be created."""
        toolbar = ConfigCameraHeader(mock_theme)
        # Toolbar should have camera selector, connection status, and global actions
        assert toolbar._camera_combo is not None
        assert toolbar._conn_indicator is not None
        assert toolbar._conn_label is not None
        assert toolbar._snapshot_btn is not None
        assert toolbar._save_btn is not None
        # Should NOT have connection/acquisition buttons (moved to ImageAcquisitionPanel)
        assert not hasattr(toolbar, '_connect_btn')
        assert not hasattr(toolbar, '_disconnect_btn')
        assert not hasattr(toolbar, '_start_btn')
        assert not hasattr(toolbar, '_stop_btn')

    def test_camera_list_population(self, qapp, mock_theme):
        """Test camera list population."""
        toolbar = ConfigCameraHeader(mock_theme)
        identity = CameraIdentity(camera_id="cam_001", serial_number="26010002", model="TV46L")
        cameras = [("cam_001", "TV46L-26010002", identity, True)]
        toolbar.set_cameras(cameras)
        
        assert toolbar._camera_combo.count() == 1
        assert toolbar._camera_combo.itemText(0) == "TV46L-26010002"
        assert toolbar._camera_combo.itemData(0) == "cam_001"

    def test_connection_state_updates(self, qapp, mock_theme):
        """Test connection state updates toolbar status indicator."""
        toolbar = ConfigCameraHeader(mock_theme)
        
        # Initial: disconnected
        toolbar.set_connection_state(CameraConnectionState.DISCONNECTED)
        assert "Disconnected" in toolbar._conn_label.text()
        
        # Connected
        toolbar.set_connection_state(CameraConnectionState.CONNECTED)
        assert "Connected" in toolbar._conn_label.text()
        
        # Acquiring
        toolbar.set_connection_state(CameraConnectionState.ACQUIRING)
        assert "Acquiring" in toolbar._conn_label.text()
        
        # Error
        toolbar.set_connection_state(CameraConnectionState.ERROR)
        assert "Error" in toolbar._conn_label.text()

    def test_camera_selection_signal(self, qapp, mock_theme):
        """Test camera selection emits signal."""
        toolbar = ConfigCameraHeader(mock_theme)
        identity = CameraIdentity(camera_id="cam_001", serial_number="26010002", model="TV46L")
        cameras = [("cam_001", "TV46L-26010002", identity, True)]
        toolbar.set_cameras(cameras)
        
        # Signal should be emitted when set_cameras selects the first camera
        received = []
        toolbar.camera_selected.connect(lambda cid: received.append(cid))
        
        # Change selection to trigger signal again
        toolbar._camera_combo.setCurrentIndex(-1)  # Clear selection
        toolbar._camera_combo.setCurrentIndex(0)   # Re-select
        
        assert len(received) == 1
        assert received[0] == "cam_001"


class TestLiveThermalWidget:
    """Tests for LiveThermalWidget with ROI overlays."""

    def test_widget_creation(self, qapp):
        """Test widget can be created."""
        widget = LiveThermalWidget()
        assert widget.minimumWidth() == 480
        assert widget.minimumHeight() == 360

    def test_set_frame_updates_display(self, qapp):
        """Test setting frame updates display."""
        widget = LiveThermalWidget()
        import numpy as np
        temp = np.arange(256, dtype=np.float32).reshape(16, 16)
        frame = Mock()
        frame.payload.thermal = np.zeros((16, 16), dtype=np.uint16)
        frame.sequence = 1
        frame.timestamp = 1.0
        
        widget.set_frame(temp, frame)
        wait_for_render(qapp, widget)
        
        assert widget._display_array is not None
        assert widget._display_array.shape == (16, 16, 3)  # RGB

    def test_roi_overlay_rendering(self, qapp):
        """Test ROI overlays are rendered."""
        widget = LiveThermalWidget()
        import numpy as np
        temp = np.arange(256, dtype=np.float32).reshape(16, 16)
        frame = Mock()
        frame.payload.thermal = np.zeros((16, 16), dtype=np.uint16)
        frame.sequence = 1
        frame.timestamp = 1.0
        
        widget.set_frame(temp, frame)
        wait_for_render(qapp, widget)
        
        # Add ROI overlay
        overlay = ROIOverlay(
            roi_id="roi_001",
            shape="rectangle1",
            geometry={"y1": 2, "x1": 2, "y2": 8, "x2": 8},
            color="#FFFF00",
            selected=False,
            alarm_active=False,
        )
        widget.set_roi_overlays([overlay])
        
        # Should not crash
        widget.update()

    def test_roi_highlight(self, qapp):
        """Test ROI highlight functionality."""
        widget = LiveThermalWidget()
        import numpy as np
        temp = np.arange(256, dtype=np.float32).reshape(16, 16)
        frame = Mock()
        frame.payload.thermal = np.zeros((16, 16), dtype=np.uint16)
        frame.sequence = 1
        frame.timestamp = 1.0
        
        widget.set_frame(temp, frame)
        wait_for_render(qapp, widget)
        
        overlay = ROIOverlay(
            roi_id="roi_001",
            shape="rectangle1",
            geometry={"y1": 2, "x1": 2, "y2": 8, "x2": 8},
            selected=False,
        )
        widget.set_roi_overlays([overlay])
        
        # Highlight
        widget.highlight_roi("roi_001")
        assert widget._selected_roi_id == "roi_001"
        assert overlay.selected == True
        
        # Unhighlight
        widget.highlight_roi(None)
        assert widget._selected_roi_id is None
        assert overlay.selected == False

    def test_palette_change(self, qapp):
        """Test palette change updates display."""
        widget = LiveThermalWidget()
        import numpy as np
        temp = np.arange(256, dtype=np.float32).reshape(16, 16)
        frame = Mock()
        frame.payload.thermal = np.zeros((16, 16), dtype=np.uint16)
        frame.sequence = 1
        frame.timestamp = 1.0
        
        widget.set_frame(temp, frame)
        wait_for_render(qapp, widget)
        
        # Change palette
        widget.set_palette("iron")
        assert widget._palette == "iron"
        
        widget.set_palette("rainbow")
        assert widget._palette == "rainbow"

    def test_zoom_modes(self, qapp):
        """Test zoom modes."""
        widget = LiveThermalWidget()
        import numpy as np
        temp = np.arange(256, dtype=np.float32).reshape(16, 16)
        frame = Mock()
        frame.payload.thermal = np.zeros((16, 16), dtype=np.uint16)
        frame.sequence = 1
        frame.timestamp = 1.0
        
        widget.set_frame(temp, frame)
        wait_for_render(qapp, widget)
        
        widget.set_zoom("100%")
        assert widget._zoom_mode == "100%"
        
        widget.set_zoom("Fit to Window")
        assert widget._zoom_mode == "Fit to Window"

    def test_range_changed_signal(self, qapp):
        """Test range_changed signal is emitted."""
        widget = LiveThermalWidget()
        import numpy as np
        temp = np.arange(256, dtype=np.float32).reshape(16, 16)
        frame = Mock()
        frame.payload.thermal = np.zeros((16, 16), dtype=np.uint16)
        frame.sequence = 1
        frame.timestamp = 1.0
        
        received = []
        widget.range_changed.connect(lambda lo, hi: received.append((lo, hi)))
        
        widget.set_frame(temp, frame)
        wait_for_render(qapp, widget)
        
        assert len(received) == 1
        lo, hi = received[0]
        assert lo == 0.0  # min of temp array
        assert hi == 255.0  # max of temp array


class TestConnectionStateModel:
    """Tests for CameraConnectionState model."""

    def test_state_values(self):
        """Test all connection state values."""
        assert CameraConnectionState.DISCONNECTED.value == "disconnected"
        assert CameraConnectionState.CONNECTING.value == "connecting"
        assert CameraConnectionState.CONNECTED.value == "connected"
        assert CameraConnectionState.ACQUIRING.value == "acquiring"
        assert CameraConnectionState.DEGRADED.value == "degraded"
        assert CameraConnectionState.RECONNECTING.value == "reconnecting"
        assert CameraConnectionState.ERROR.value == "error"

    def test_is_connected_property(self):
        """Test CameraStatus.is_connected property."""
        from thermal_monitor.core.models import CameraStatus
        
        status = CameraStatus(camera_id="cam_001", connection_state=CameraConnectionState.DISCONNECTED)
        assert not status.is_connected
        
        status = CameraStatus(camera_id="cam_001", connection_state=CameraConnectionState.CONNECTING)
        assert not status.is_connected
        
        status = CameraStatus(camera_id="cam_001", connection_state=CameraConnectionState.CONNECTED)
        assert status.is_connected
        
        status = CameraStatus(camera_id="cam_001", connection_state=CameraConnectionState.ACQUIRING)
        assert status.is_connected
        
        status = CameraStatus(camera_id="cam_001", connection_state=CameraConnectionState.DEGRADED)
        assert status.is_connected
        
        # RECONNECTING is not considered connected in the model
        status = CameraStatus(camera_id="cam_001", connection_state=CameraConnectionState.RECONNECTING)
        assert not status.is_connected
        
        status = CameraStatus(camera_id="cam_001", connection_state=CameraConnectionState.ERROR)
        assert not status.is_connected

    def test_is_acquiring_property(self):
        """Test CameraStatus.is_acquiring property."""
        from thermal_monitor.core.models import CameraStatus
        
        for state in CameraConnectionState:
            status = CameraStatus(camera_id="cam_001", connection_state=state)
            if state == CameraConnectionState.ACQUIRING:
                assert status.is_acquiring
            else:
                assert not status.is_acquiring


class TestConnectionWorkflow:
    """Integration tests for connection workflow."""

    def test_discovery_to_connection_flow(self, qapp, mock_discovery_service, sample_discovered_cameras):
        """Test full flow: discovery -> selection -> connection."""
        mock_discovery_service.discover_cameras.return_value = sample_discovered_cameras
        
        # 1. Create dialog
        dialog = CameraSelectionDialog(mock_discovery_service)
        wait_for_discovery(qapp, dialog)
        assert dialog._camera_tree.topLevelItemCount() == 2
        
        # 2. Select camera
        dialog._camera_tree.setCurrentItem(dialog._camera_tree.topLevelItem(0))
        assert dialog._connect_btn.isEnabled()
        
        # 3. Connect emits signal
        received = []
        dialog.camera_selected.connect(lambda cam: received.append(cam))
        dialog._on_connect()
        
        assert len(received) == 1
        assert received[0].serial_number == "26010002"

    def test_acquisition_panel_fps_control(self, qapp):
        """Test FPS control in acquisition panel."""
        panel = ImageAcquisitionPanel()
        
        received = []
        panel.fps_changed.connect(lambda fps: received.append(fps))
        
        panel._requested_fps.setValue(15)
        
        assert len(received) == 1
        assert received[0] == 15

    def test_acquisition_panel_averaging_control(self, qapp):
        """Test averaging control."""
        panel = ImageAcquisitionPanel()
        
        received = []
        panel.averaging_changed.connect(lambda val: received.append(val))
        
        panel._averaging_combo.setCurrentText("4")
        
        assert len(received) == 1
        assert received[0] == "4"

    def test_acquisition_panel_history_control(self, qapp):
        """Test history length control."""
        panel = ImageAcquisitionPanel()
        
        received = []
        panel.history_changed.connect(lambda val: received.append(val))
        
        panel._history_spin.setValue(200)
        
        assert len(received) == 1
        assert received[0] == 200


class TestDependencyInjectionRegression:
    """Regression tests for CameraDiscoveryService dependency injection."""

    def test_controller_passes_discovery_service_to_config_window(self, qapp):
        """Test that AppController passes discovery_service to ConfigurationWindow."""
        from thermal_monitor.ui.controller import AppController
        from thermal_monitor.services.mode import ModeService
        from thermal_monitor.services.configuration import ConfigurationService
        from thermal_monitor.services.offline import OfflineService
        from thermal_monitor.services.runtime import CameraRuntimeService
        from thermal_monitor.services.discovery import CameraDiscoveryService
        from thermal_monitor.config import ConfigurationManager, create_config_manager
        from thermal_monitor.ui.theme import ThemeManager

        # Create real services
        config_manager = create_config_manager()
        config = config_manager.get_config()

        mode_service = ModeService()
        config_service = ConfigurationService()
        offline_service = OfflineService()
        discovery_service = CameraDiscoveryService()
        runtime_service = CameraRuntimeService(
            cameras_config=config.cameras,
            system_config=config.system,
            recording_config=config.recording,
            storage_config=config.storage,
            calibration_config=config.calibration,
        )
        theme_manager = ThemeManager(config_manager)

        # Create controller
        controller = AppController(
            mode_service=mode_service,
            config_service=config_service,
            offline_service=offline_service,
            runtime_service=runtime_service,
            discovery_service=discovery_service,
            config_manager=config_manager,
            theme_manager=theme_manager,
        )

        # Access the private discovery service to verify it's set
        assert controller._discovery_service is not None
        assert controller._discovery_service is discovery_service

        # Create config window - this should pass discovery_service
        config_window = controller._create_config_window()

        # Verify ConfigurationWindow received discovery_service
        assert config_window._discovery_service is not None
        assert config_window._discovery_service is discovery_service

        # Verify ConfigurationModeWidget received discovery_service
        config_widget = config_window._config_widget
        assert config_widget._discovery_service is not None
        assert config_widget._discovery_service is discovery_service

        # Cleanup
        config_window.close()
        controller.shutdown()
        runtime_service.shutdown()

    def test_camera_selection_dialog_receives_discovery_service(self, qapp, mock_discovery_service, mock_theme):
        """Test that CameraSelectionDialog receives discovery_service and can call discover_cameras."""
        from thermal_monitor.ui.widgets.camera_selection_dialog import CameraSelectionDialog
        from thermal_monitor.services.discovery import DiscoveredCamera

        sample_cameras = [
            DiscoveredCamera(
                device_identifier="device_001",
                serial_number="26010002",
                ip_address="169.254.24.69",
                model="TV46L",
                vendor="FLIR",
                firmware="1.2.3",
                user_name="Camera1",
            ),
        ]

        mock_discovery_service.discover_cameras.return_value = sample_cameras

        # Create dialog with mock discovery service
        dialog = CameraSelectionDialog(mock_discovery_service, mock_theme)
        wait_for_discovery(qapp, dialog)

        # Verify the dialog has the discovery service
        assert dialog._discovery_service is mock_discovery_service

        # Call refresh - this should invoke discover_cameras
        dialog._refresh_cameras()
        wait_for_discovery(qapp, dialog)

        # Verify discover_cameras was called
        mock_discovery_service.discover_cameras.assert_called()

        # Verify cameras were populated
        assert dialog._camera_tree.topLevelItemCount() == 1

        dialog.close()


def _teardown_controller():
    """Build a real AppController with real services (offscreen-safe)."""
    from thermal_monitor.ui.controller import AppController
    from thermal_monitor.services.mode import ModeService
    from thermal_monitor.services.configuration import ConfigurationService
    from thermal_monitor.services.offline import OfflineService
    from thermal_monitor.services.runtime import CameraRuntimeService
    from thermal_monitor.services.discovery import CameraDiscoveryService
    from thermal_monitor.config import create_config_manager
    from thermal_monitor.ui.theme import ThemeManager

    config_manager = create_config_manager()
    config = config_manager.get_config()
    controller = AppController(
        mode_service=ModeService(),
        config_service=ConfigurationService(),
        offline_service=OfflineService(),
        runtime_service=CameraRuntimeService(
            cameras_config=config.cameras,
            system_config=config.system,
            recording_config=config.recording,
            storage_config=config.storage,
            calibration_config=config.calibration,
        ),
        discovery_service=CameraDiscoveryService(),
        config_manager=config_manager,
        theme_manager=ThemeManager(config_manager),
    )
    return controller


class TestControllerTeardownRace:
    """Launcher may be half-dead (C++ children gone) while controller lives.

    Regression for: RuntimeError: wrapped C/C++ object of type QPushButton
    has been deleted (controller -> _update_launcher_buttons ->
    launcher._live_btn.setEnabled during teardown). The dead-children state
    is injected by making set_mode_buttons_enabled raise exactly that
    RuntimeError; the controller must skip + log instead of propagating.
    """

    def test_update_launcher_buttons_survives_dead_children(self, qapp):
        controller = _teardown_controller()
        controller._create_launcher_window()

        def dead(*args, **kwargs):
            raise RuntimeError(
                "wrapped C/C++ object of type QPushButton has been deleted"
            )

        controller._launcher_window.set_mode_buttons_enabled = dead
        controller._update_launcher_buttons()  # must not raise
        controller.shutdown()

    def test_config_destroyed_with_dead_children(self, qapp):
        from unittest.mock import Mock

        controller = _teardown_controller()
        controller._create_launcher_window()
        controller._create_config_window()

        def dead(*args, **kwargs):
            raise RuntimeError(
                "wrapped C/C++ object of type QPushButton has been deleted"
            )

        controller._launcher_window.set_mode_buttons_enabled = dead
        controller._show_launcher = Mock()
        controller._on_config_window_destroyed()  # must not raise
        assert controller._config_window is None
        controller._show_launcher.assert_called_once()
        controller.shutdown()

    def test_destroyed_handlers_noop_during_shutdown(self, qapp):
        from unittest.mock import Mock

        controller = _teardown_controller()
        controller._create_launcher_window()
        controller._create_config_window()
        controller._show_launcher = Mock()
        controller._shutting_down = True
        controller._on_config_window_destroyed()
        controller._on_live_window_destroyed()
        controller._show_launcher.assert_not_called()
        controller.shutdown()

    def test_update_launcher_buttons_without_launcher(self, qapp):
        controller = _teardown_controller()
        controller._update_launcher_buttons()  # no launcher yet: no-op


class TestFocusApplyClickSlot:
    """The diagnostic click slot fires from the visible button, WARNING-level."""

    def test_debug_slot_logs_click(self, qapp, caplog):
        import logging

        controller = _teardown_controller()
        widget = controller._create_config_window()._config_widget
        btn = widget._acq_panel.focus_apply_button
        assert btn.objectName() == "focusApplyButton"
        with caplog.at_level(logging.WARNING):
            widget._debug_focus_apply_clicked(False)
        assert "FOCUS APPLY BUTTON CLICKED" in caplog.text
        assert "focusApplyButton" in caplog.text
        controller.shutdown()

    def test_apply_button_click_reaches_both_slots(self, qapp):
        controller = _teardown_controller()
        widget = controller._create_config_window()._config_widget
        btn = widget._acq_panel.focus_apply_button
        widget._acq_panel.set_focus_enabled(True)
        seen: list = []
        widget._acq_panel.focus_set_requested.connect(seen.append)
        widget._acq_panel._focus_spin.setValue(1000)
        btn.click()
        assert seen == [1000]
        controller.shutdown()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
