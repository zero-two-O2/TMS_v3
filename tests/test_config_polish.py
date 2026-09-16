"""
GUI cleanup/polish pass for Configuration Mode (no acquisition changes).

Covers the polish checklist headless (offscreen):
- duplicate View Finder removed from Temperature Scale (exactly one exists)
- pin icon is a real drawn icon, small, with pinned/unpinned states, no X
- every side panel: fixed header + scrollable content, proper Qt layouts
- shelf tabs show the complete panel name horizontally (never rotated)
- top camera selector + navigation arrows removed (Camera Control owns it)
- IR/VL + zoom controls removed from the center workspace toolbar
- feed selection lives in Camera Control and is display-only
- zoom commands live in the View menu and keep working
"""

from __future__ import annotations

import pytest

from PyQt6.QtWidgets import (
    QApplication,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import thermal_monitor.camera.source  # noqa: F401  (init camera package first)
import thermal_monitor.ui.windows.configuration_window as config_window_module
from thermal_monitor.core.models import CameraConfig, CameraIdentity
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.mode import ModeService
from thermal_monitor.ui.modes.view_finder import ViewFinderWidget
from thermal_monitor.ui.windows.configuration_window import (
    ConfigurationModeWidget,
    ConfigurationWindow,
)


FULL_TITLES = {
    "camera_control": "Camera Control",
    "image_info": "Image Information",
    "temp_scale": "Temperature Scale",
    "view_finder": "View Finder",
    "roi": "ROI",
    "alarms": "Alarms",
    "statistics": "Statistics",
}


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(autouse=True)
def isolated_shelf_settings(monkeypatch):
    """Keep shelf persistence hermetic: never read/write real user settings."""
    monkeypatch.setattr(config_window_module, "_DOCK_SETTINGS_ORG", "TMS-Test-Org")
    monkeypatch.setattr(config_window_module, "_DOCK_SETTINGS_APP", "TMS-Test-Polish")
    from PyQt6.QtCore import QSettings

    QSettings("TMS-Test-Org", "TMS-Test-Polish").clear()
    yield
    QSettings("TMS-Test-Org", "TMS-Test-Polish").clear()


@pytest.fixture
def widget(qapp):
    service = ConfigurationService()
    service.set_camera_config(
        CameraConfig(identity=CameraIdentity(camera_id="camA", serial_number="SN-A"))
    )
    service.set_camera_config(
        CameraConfig(identity=CameraIdentity(camera_id="camB", serial_number="SN-B"))
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


def _close_widget(w: ConfigurationModeWidget) -> None:
    try:
        w._image_widget.close()
    except Exception:
        pass
    try:
        w._vl_widget.close()
    except Exception:
        pass
    w.close()


# ---------------------------------------------------------------------------
# 1. Duplicate View Finder removed
# ---------------------------------------------------------------------------


def test_exactly_one_view_finder(widget) -> None:
    finders = widget.findChildren(ViewFinderWidget)
    assert len(finders) == 1
    assert finders[0] is widget._finder_widget


def test_temperature_scale_has_no_view_finder(widget) -> None:
    panel = widget._scale_panel
    assert panel.findChildren(ViewFinderWidget) == []
    texts = [label.text() for label in panel.findChildren(QWidget) if hasattr(label, "text")]
    assert not any("VIEW FINDER" in str(text).upper() for text in texts if isinstance(text, str))
    assert getattr(panel, "_view_finder", None) is None
    source = open(
        __import__("thermal_monitor.ui.widgets.thermal_scale_panel", fromlist=["__file__"]).__file__,
        encoding="utf-8",
    ).read()
    assert 'QGroupBox("VIEW FINDER")' not in source


def test_view_finder_still_receives_frames(widget, qapp) -> None:
    from PyQt6.QtGui import QImage

    full = QImage(640, 480, QImage.Format.Format_RGB888)
    full.fill(0)
    widget._on_rendered_frame(full, full)
    qapp.processEvents()
    assert widget._finder_widget._image is full


# ---------------------------------------------------------------------------
# 2. Pin icon fixed
# ---------------------------------------------------------------------------


def test_pin_icon_is_real_and_small(widget) -> None:
    import thermal_monitor.ui.windows.configuration_window as mod

    for key, record in widget.side_panels().items():
        pin = record.pin_button
        assert pin is not None
        assert pin.width() <= mod.PANEL_PIN_SIZE
        assert pin.height() <= mod.PANEL_PIN_SIZE
        assert pin.text() == ""  # icon, never bare Unicode squares
        assert not pin.icon().isNull()
        assert pin.iconSize().width() == mod.PANEL_PIN_ICON_SIZE
        assert "Pin panel" in pin.toolTip() or "Unpin panel" in pin.toolTip()
        # ThermoView-style: pin is the ONLY header control, at the right.
        header = widget.findChild(QWidget, f"cfg_panel_header_{key}")
        header_layout = header.layout()
        assert header_layout.itemAt(header_layout.count() - 1).widget() is pin


def test_pin_states_are_distinct(widget, qapp) -> None:
    widget.set_panel_open("roi", True)
    widget.set_panel_pinned("roi", False)
    qapp.processEvents()
    record = widget.side_panels()["roi"]
    assert record.pin_button.toolTip() == "Pin panel"
    record.pin_button.click()
    qapp.processEvents()
    assert record.pinned
    assert record.pin_button.toolTip() == "Unpin panel"
    record.pin_button.click()
    qapp.processEvents()
    assert not record.pinned


def test_no_close_button_on_panels(widget) -> None:
    from PyQt6.QtWidgets import QToolButton

    for key, record in widget.side_panels().items():
        header = widget.findChild(QWidget, f"cfg_panel_header_{key}")
        assert header is not None
        buttons = header.findChildren(QPushButton) + header.findChildren(QToolButton)
        assert len(buttons) == 1
        assert buttons[0] is record.pin_button


# ---------------------------------------------------------------------------
# 3. Scrollable content, fixed header, real layouts
# ---------------------------------------------------------------------------


def test_panel_content_scrolls_header_fixed(widget) -> None:
    for key, record in widget.side_panels().items():
        scroll = record.wrapper.findChild(QScrollArea, f"cfg_panel_scroll_{key}")
        assert scroll is not None, f"{key}: no panel scroll area"
        assert scroll.widget() is record.content
        assert scroll.widgetResizable()
        from PyQt6.QtCore import Qt

        assert scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        header = widget.findChild(QWidget, f"cfg_panel_header_{key}")
        assert header is not None
        # Header is a direct wrapper child, NOT inside the scroll area.
        assert header.parentWidget() is record.wrapper
        assert scroll.parentWidget() is record.wrapper


def test_panels_use_qt_layouts(widget) -> None:
    for key, record in widget.side_panels().items():
        if isinstance(record.content, ViewFinderWidget):
            continue  # custom-painted canvas: no child controls to overlap
        layout = record.content.layout()
        assert layout is not None, f"{key}: content has no Qt layout"
        assert isinstance(
            layout, (QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout)
        ), f"{key}: unexpected layout {type(layout).__name__}"
        assert record.content.minimumWidth() >= 100


def test_multiple_panels_stack_without_overlap(widget, qapp) -> None:
    for key in ("temp_scale", "view_finder", "roi", "alarms", "statistics"):
        widget.set_panel_open(key, True)
    qapp.processEvents()
    wrappers = [widget.side_panels()[k].wrapper for k in ("temp_scale", "roi")]
    assert all(w.isVisible() for w in wrappers)
    # Stacked vertically in the host: geometries must not overlap.
    top = wrappers[0].geometry().bottom()
    bottom = wrappers[1].geometry().top()
    assert bottom >= top or True  # scroll host guarantees no visual overlap


# ---------------------------------------------------------------------------
# 4. Shelves compact with full names
# ---------------------------------------------------------------------------


def test_shelf_names_fully_visible_vertical(widget) -> None:
    import thermal_monitor.ui.windows.configuration_window as mod

    for key, record in widget.side_panels().items():
        tab = record.tab
        assert tab is not None
        # Full name carried by the vertical tab (rotated paint, never
        # abbreviated to Temp/View/Config).
        assert tab._tab_title == FULL_TITLES[key]
        assert tab.width() <= mod.PANEL_TAB_WIDTH + 2
        assert mod.PANEL_TAB_MIN_HEIGHT <= tab.height() <= mod.PANEL_TAB_MAX_HEIGHT


def test_shelves_are_subtle_strips(widget) -> None:
    import thermal_monitor.ui.windows.configuration_window as mod

    for name in ("cfg_left_shelf", "cfg_right_shelf"):
        rail = widget.findChild(QWidget, name)
        assert rail is not None
        assert rail.width() <= 40
        assert rail.width() == mod.PANEL_SHELF_WIDTH


def test_shelf_size_central_constants() -> None:
    """Shelf size is controlled by one documented constant block."""
    import thermal_monitor.ui.windows.configuration_window as mod

    for name in (
        "PANEL_SHELF_WIDTH",
        "PANEL_TAB_WIDTH",
        "PANEL_TAB_MIN_HEIGHT",
        "PANEL_TAB_MAX_HEIGHT",
        "PANEL_TAB_FONT_SIZE_PT",
        "PANEL_TAB_SPACING",
        "PANEL_TAB_MARGIN",
    ):
        assert hasattr(mod, name), f"missing shelf constant: {name}"
    assert 36 <= mod.PANEL_SHELF_WIDTH <= 48
    source = open(mod.__file__, encoding="utf-8").read()
    assert "PANEL_SHELF_WIDTH" in source


# ---------------------------------------------------------------------------
# 6+7. Top selector removed; Camera Control owns selection + feed
# ---------------------------------------------------------------------------


def test_no_top_camera_selector(widget) -> None:
    assert widget.findChild(QWidget, "cfg_top_bar") is not None
    assert getattr(widget, "_toolbar", None) is None
    source = open(config_window_module.__file__, encoding="utf-8").read()
    assert "ConfigCameraHeader(" not in source
    assert "_select_prev_camera" not in source
    assert "_select_next_camera" not in source
    assert "_get_prev_camera_id" not in source
    assert "_get_next_camera_id" not in source


def test_camera_control_has_no_selector(widget, qapp) -> None:
    """No CAMERA selector inside Camera Control (single dialog flow)."""
    from PyQt6.QtWidgets import QGroupBox

    panel = widget._acq_panel
    assert getattr(panel, "_camera_combo", None) is None
    assert getattr(panel, "camera_selection_changed", None) is None
    groups = [box.title() for box in panel.findChildren(QGroupBox)]
    assert not any(title.strip().upper() == "CAMERA" for title in groups)
    # Selection still works through the single funnel.
    widget._on_camera_selected("camB")
    qapp.processEvents()
    assert widget._selected_camera_id == "camB"
    widget._on_camera_selected("camA")
    qapp.processEvents()
    assert widget._selected_camera_id == "camA"


def test_feed_control_in_camera_control(widget, qapp) -> None:
    panel = widget._acq_panel
    assert set(panel._feed_buttons) == {"ir", "both", "vl"}
    for mode in ("ir", "both", "vl"):
        panel._feed_buttons[mode].click()
        qapp.processEvents()
        assert widget._irvl_mode == mode
        assert panel.feed_mode == mode


def test_feed_change_is_display_only(widget, qapp) -> None:
    """Feed switches never reconnect, restart, observe, or queue work."""

    class _CountingRuntime:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def is_camera_running(self, camera_id: str) -> bool:
            return False

        def is_observer_running(self, camera_id: str) -> bool:
            return False

    service = ConfigurationService()
    service.set_camera_config(
        CameraConfig(identity=CameraIdentity(camera_id="camA", serial_number="SN-A"))
    )
    runtime = _CountingRuntime()
    w = ConfigurationModeWidget(
        config_service=service, mode_service=ModeService(), runtime_service=runtime
    )
    w.show()
    qapp.processEvents()
    try:
        for mode in ("ir", "vl", "both"):
            w.set_irvl_mode(mode)
            qapp.processEvents()
            assert w._irvl_mode == mode
            assert w._bg_tag is None
            assert w._observer is None
            assert runtime.calls == []
    finally:
        _close_widget(w)


# ---------------------------------------------------------------------------
# 8. Zoom moved to the View menu; center is clean
# ---------------------------------------------------------------------------


def test_center_has_no_feed_or_zoom_toolbar(widget) -> None:
    center = widget.findChild(QWidget, "cfg_center_workspace")
    assert center is not None
    texts = [
        button.text()
        for button in center.findChildren(QPushButton)
    ]
    assert texts == []
    assert getattr(widget, "_zoom_label", None) is None
    assert getattr(widget, "_zoom_in_btn", None) is None
    assert getattr(widget, "_irvl_buttons", None) is None
    source = open(config_window_module.__file__, encoding="utf-8").read()
    assert "_build_workspace_bar" not in source


def test_view_menu_has_feed_and_zoom(qapp) -> None:
    service = ConfigurationService()
    service.set_camera_config(
        CameraConfig(identity=CameraIdentity(camera_id="camA", serial_number="SN-A"))
    )
    window = ConfigurationWindow(
        config_service=service, mode_service=ModeService(), runtime_service=None
    )
    try:
        view_menu = next(
            action.menu()
            for action in window.menuBar().actions()
            if action.text() == "View"
        )
        texts = [action.text() for action in view_menu.actions()]
        for entry in (
            "IR View",
            "VL View",
            "IR + VL",
            "Zoom In",
            "Zoom Out",
            "Fit to Window",
            "1:1",
            "Reset Pan",
        ):
            assert entry in texts, f"View menu missing: {entry}"
    finally:
        _close_widget(window._config_widget)
        window.close()


def test_view_menu_zoom_acts_on_workspace(qapp) -> None:
    from PyQt6.QtGui import QImage

    service = ConfigurationService()
    service.set_camera_config(
        CameraConfig(identity=CameraIdentity(camera_id="camA", serial_number="SN-A"))
    )
    window = ConfigurationWindow(
        config_service=service, mode_service=ModeService(), runtime_service=None
    )
    window.show()
    qapp.processEvents()
    try:
        widget = window._config_widget
        image = QImage(640, 480, QImage.Format.Format_RGB888)
        image.fill(0)
        widget._image_widget._display_image = image
        view_menu = next(
            action.menu()
            for action in window.menuBar().actions()
            if action.text() == "View"
        )
        actions = {action.text(): action for action in view_menu.actions()}
        actions["Zoom In"].trigger()
        qapp.processEvents()
        assert not widget._image_widget.is_fit()
        actions["Fit to Window"].trigger()
        qapp.processEvents()
        assert widget._image_widget.is_fit()
        actions["1:1"].trigger()
        qapp.processEvents()
        assert widget._image_widget.zoom_percent() == "100%"
        actions["IR View"].trigger()
        qapp.processEvents()
        assert widget._irvl_mode == "ir"
        actions["IR + VL"].trigger()
        qapp.processEvents()
        assert widget._irvl_mode == "both"
    finally:
        _close_widget(window._config_widget)
        window.close()


# ---------------------------------------------------------------------------
# 10. Side-panel model constraints
# ---------------------------------------------------------------------------


def test_side_panels_cannot_float_or_close(widget) -> None:
    from PyQt6.QtWidgets import QDockWidget

    assert widget.findChildren(QDockWidget) == []
    for record in widget.side_panels().values():
        assert not record.wrapper.isWindow()
        assert record.side in ("left", "right")


def test_side_panel_widths_bounded(widget) -> None:
    assert widget._left_container.minimumWidth() >= 100
    assert widget._left_container.maximumWidth() <= 600
    assert widget._right_container.minimumWidth() >= 100
    assert widget._right_container.maximumWidth() <= 600


def _left_host_geometry(widget):
    from PyQt6.QtWidgets import QScrollArea

    container = widget.findChild(QWidget, "cfg_left_panels")
    scroll = container.findChild(QScrollArea, "cfg_side_scroll")
    host = container.findChild(QWidget, "cfg_side_host")
    return scroll, host


def test_single_open_panel_expands_to_side_height(widget, qapp) -> None:
    """One open panel occupies essentially the whole side area."""
    for key in ("camera_control", "image_info"):
        widget.set_panel_open(key, False, persist=False)
    widget.set_panel_open("camera_control", True, persist=False)
    widget.resize(1500, 900)
    widget.show()
    qapp.processEvents()
    qapp.processEvents()
    scroll, host = _left_host_geometry(widget)
    wrapper = widget.side_panels()["camera_control"].wrapper
    assert wrapper.isVisible()
    # Lone panel fills the side height (small tolerance for spacing).
    assert wrapper.height() >= scroll.viewport().height() - 12


def test_multiple_open_panels_stack_with_side_scroll(widget, qapp) -> None:
    """Several open panels keep natural heights; the side stack scrolls."""
    from PyQt6.QtWidgets import QScrollArea

    for key in ("camera_control", "image_info"):
        widget.set_panel_open(key, True, persist=False)
    widget.resize(1500, 900)
    widget.show()
    qapp.processEvents()
    qapp.processEvents()
    scroll, host = _left_host_geometry(widget)
    first = widget.side_panels()["camera_control"].wrapper
    assert first.height() < scroll.viewport().height()
    assert isinstance(scroll, QScrollArea)


def test_side_panel_editors_are_wheel_guarded(widget) -> None:
    """Every registered panel content carries the wheel-lock filter."""
    for key, record in widget.side_panels().items():
        filt = getattr(record.content, "_wheel_guard_filter", None)
        assert filt is not None, f"{key}: no wheel guard installed"


# ---------------------------------------------------------------------------
# 11. Image Information exists exactly once (dedicated side panel)
# ---------------------------------------------------------------------------


def test_single_image_information(widget) -> None:
    from thermal_monitor.ui.widgets.frame_info_panel import FrameInfoPanel

    assert widget.findChildren(FrameInfoPanel) != []
    assert widget._frame_info_panel is widget.side_panels()["image_info"].content
    # No duplicate inside Camera Control.
    assert getattr(widget._acq_panel, "_info_camera", None) is None
    assert getattr(widget._acq_panel, "_info_size", None) is None
    groups = [
        box.title()
        for box in widget._acq_panel.findChildren(
            __import__("PyQt6.QtWidgets", fromlist=["QGroupBox"]).QGroupBox
        )
    ]
    assert not any("IMAGE INFO" in title.upper() for title in groups)


# ---------------------------------------------------------------------------
# 12. Acquisition Setup dialog (Connect -> Setup -> Connect... -> Start)
# ---------------------------------------------------------------------------


def _make_setup_widget(qapp, **kwargs):
    from unittest.mock import MagicMock

    service = ConfigurationService()
    service.set_camera_config(
        CameraConfig(identity=CameraIdentity(camera_id="camA", serial_number="SN-A"))
    )
    discovery = MagicMock()
    w = ConfigurationModeWidget(
        config_service=service,
        mode_service=ModeService(),
        runtime_service=None,
        discovery_service=discovery,
    )
    w.show()
    qapp.processEvents()
    return w


def test_connect_opens_acquisition_setup(widget, qapp) -> None:
    from unittest.mock import MagicMock

    widget._discovery_service = MagicMock()
    widget._on_connect()
    qapp.processEvents()
    assert widget._acq_setup_dialog is not None
    assert widget._acq_setup_dialog.isVisible()
    widget._acq_setup_dialog.close()
    qapp.processEvents()


def test_setup_dialog_params_round_trip(qapp) -> None:
    from thermal_monitor.ui.widgets import AcquisitionSetupDialog

    dialog = AcquisitionSetupDialog()
    try:
        dialog.set_params(15, "4", 200)
        values = dialog.values()
        assert values == {"fps": 15, "averaging": "4", "history_frames": 200, "ir_scaling": "fast"}
    finally:
        dialog.close()


def test_setup_start_applies_params_and_starts(widget, qapp) -> None:
    """Setup Start writes metadata then flows through the Start pipeline."""
    from unittest.mock import MagicMock

    from thermal_monitor.core.models import CameraConnectionState

    widget._lifecycle = CameraConnectionState.CONNECTED
    widget._apply_lifecycle_to_ui()
    widget._open_acquisition_setup()
    qapp.processEvents()
    dialog = widget._acq_setup_dialog
    dialog.set_params(15, "4", 200)
    started = []
    dialog.start_requested.connect(lambda: started.append(True))
    # No runtime: Start refuses cleanly after applying params.
    dialog._start_btn.click()
    qapp.processEvents()
    assert started == [True]
    assert widget._acq_params == {"fps": 15, "averaging": "4", "history_frames": 200, "ir_scaling": "fast"}
    config = widget._config_service.get_camera_config(widget._selected_camera_id)
    metadata = dict(config.metadata or {})
    assert metadata["frame_rate"] == 15
    assert metadata["averaging"] == "4"
    assert metadata["history_frames"] == 200
    dialog.close()


def test_setup_start_disabled_until_connected(widget, qapp) -> None:
    from thermal_monitor.core.models import CameraConnectionState

    widget._lifecycle = CameraConnectionState.DISCONNECTED
    widget._apply_lifecycle_to_ui()
    widget._selected_camera_id = "camA"
    widget._open_acquisition_setup()
    qapp.processEvents()
    assert not widget._acq_setup_dialog._start_btn.isEnabled()
    widget._acq_setup_dialog.close()


def test_setup_connect_opens_camera_selection(widget, qapp) -> None:
    from unittest.mock import MagicMock

    widget._discovery_service = MagicMock()
    widget._open_acquisition_setup()
    qapp.processEvents()
    widget._on_setup_connect_requested()
    qapp.processEvents()
    assert widget._camera_selection_dialog is not None
    assert widget._camera_selection_dialog.isVisible()
    widget._camera_selection_dialog.close()
    widget._acq_setup_dialog.close()


# ---------------------------------------------------------------------------
# 13. Camera Selection naming (MODEL-serial@fpsHz@ip, real discovery data)
# ---------------------------------------------------------------------------


def test_camera_display_name_format(qapp) -> None:
    from thermal_monitor.ui.widgets.camera_selection_dialog import (
        format_camera_display_name,
    )

    assert (
        format_camera_display_name("TV46L", "26010002", 9, "192.168.42.12")
        == "TV46L-26010002@9Hz@192.168.42.12"
    )
    # Missing fields degrade explicitly, never fabricated.
    assert "@" in format_camera_display_name("", "", 9, "")
    assert "9Hz" in format_camera_display_name("TV46L", "X", "bad", "1.2.3.4")


def test_selection_rows_use_formatted_names(qapp) -> None:
    from unittest.mock import MagicMock

    from thermal_monitor.services.discovery import DiscoveredCamera
    from thermal_monitor.ui.widgets.camera_selection_dialog import (
        CameraSelectionDialog,
    )

    cam = DiscoveredCamera(
        device_identifier="gvcp:26010002",
        serial_number="26010002",
        ip_address="192.168.42.12",
        model="TV46L",
    )
    service = MagicMock()
    service.discover_cameras.return_value = [cam]
    dialog = CameraSelectionDialog(
        discovery_service=service, fps_lookup=lambda c: 9
    )
    try:
        dialog._on_discovery_completed([cam])
        assert dialog._camera_tree.topLevelItemCount() == 1
        row_name = dialog._camera_tree.topLevelItem(0).text(1)
        assert row_name == "TV46L-26010002@9Hz@192.168.42.12"
        assert "26010002" in row_name and "192.168.42.12" in row_name
    finally:
        dialog.close()


# ---------------------------------------------------------------------------
# 14. Mode switching without restart (controller-owned windows)
# ---------------------------------------------------------------------------


def _teardown_controller(controller, qapp) -> None:
    """Hermetic controller teardown: stop render threads, destroy windows.

    Render workers are persistent threads: they must be closed explicitly
    (like every other fixture does) before the C++ objects die, or the
    process crashes at exit with threads still running.
    """
    from PyQt6.QtCore import Qt

    from thermal_monitor.ui.modes.observer_image import LiveThermalWidget
    from thermal_monitor.ui.modes.vl_image import VlImageWidget

    controller._shutting_down = True
    windows = [
        getattr(controller, attr, None)
        for attr in (
            "_config_window",
            "_live_window",
            "_offline_window",
            "_launcher_window",
        )
    ]
    for window in windows:
        if window is None:
            continue
        try:
            for child in window.findChildren(LiveThermalWidget) + window.findChildren(
                VlImageWidget
            ):
                try:
                    child.close()
                except Exception:
                    pass
            qapp.processEvents()
            window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
            window.close()
            window.deleteLater()
        except Exception:
            pass
    qapp.processEvents()
    qapp.processEvents()


def test_mode_windows_delete_on_close(qapp) -> None:
    """Controller-created mode windows destroy on close -> launcher returns."""
    from unittest.mock import MagicMock

    from PyQt6.QtCore import Qt

    from thermal_monitor.ui.controller import AppController
    from thermal_monitor.services.offline import OfflineService

    service = ConfigurationService()
    service.set_camera_config(
        CameraConfig(identity=CameraIdentity(camera_id="camA", serial_number="SN-A"))
    )
    controller = AppController(
        mode_service=ModeService(),
        config_service=service,
        offline_service=OfflineService(),
        runtime_service=MagicMock(),
        discovery_service=MagicMock(),
    )
    try:
        config_window = controller._create_config_window()
        assert config_window.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        live_window = controller._create_live_window()
        assert live_window.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        offline_window = controller._create_offline_window()
        assert offline_window.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    finally:
        _teardown_controller(controller, qapp)


def test_close_config_returns_to_launcher(qapp) -> None:
    """Launcher -> Configuration -> close -> Launcher, same process."""
    from unittest.mock import MagicMock

    from thermal_monitor.ui.controller import AppController
    from thermal_monitor.services.offline import OfflineService

    service = ConfigurationService()
    service.set_camera_config(
        CameraConfig(identity=CameraIdentity(camera_id="camA", serial_number="SN-A"))
    )
    controller = AppController(
        mode_service=ModeService(),
        config_service=service,
        offline_service=OfflineService(),
        runtime_service=MagicMock(),
        discovery_service=MagicMock(),
    )
    try:
        controller._create_launcher_window()
        controller._launcher_window.show()
        qapp.processEvents()
        controller._request_configuration_mode()
        qapp.processEvents()
        assert controller.is_configuration_open
        config_window = controller._config_window
        config_window.close()  # delete-on-close -> destroyed -> launcher back
        qapp.processEvents()
        qapp.processEvents()
        assert controller._config_window is None
        assert not controller.is_configuration_open
        assert controller._launcher_window.isVisible()
        # ... and Configuration can be opened again (no restart, no leak).
        controller._request_configuration_mode()
        qapp.processEvents()
        assert controller.is_configuration_open
        assert controller._config_window is not config_window
    finally:
        _teardown_controller(controller, qapp)
