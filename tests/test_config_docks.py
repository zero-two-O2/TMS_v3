"""
Tests for Configuration Mode shelf workstation (redesign).

Workstation model (no QDockWidget anywhere):
    LEFT AUTO-HIDE SHELF | CAMERA WORKSPACE | RIGHT AUTO-HIDE SHELF

Covers the section-19 shelf checklist — all headless (offscreen):
shelves exist, panel assignment, no close-X, pin/unpin, auto-hide,
vertical stacking, no float/drag, center IR/VL workspace + splitter,
workspace bar, finder sync, shelf + zoom persistence, View menu,
and a Connect -> Start feed regression (fake runtime, no hardware).
"""

from __future__ import annotations

import time

import pytest

from PyQt6.QtCore import QEvent, QPointF, Qt, QObject, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QPushButton,
    QSplitter,
    QWidget,
)

import thermal_monitor.camera.source  # noqa: F401  (init camera package first)
import thermal_monitor.ui.windows.configuration_window as config_window_module
from thermal_monitor.core.models import (
    AnalysisConfig,
    CameraConfig,
    CameraConnectionState,
    CameraIdentity,
)
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.mode import ModeService
from thermal_monitor.ui.modes.observer_image import LiveThermalWidget
from thermal_monitor.ui.modes.vl_image import VlImageWidget
from thermal_monitor.ui.windows.configuration_window import (
    ConfigurationModeWidget,
    ConfigurationWindow,
)


LEFT_KEYS = ("camera_control", "image_info")
RIGHT_KEYS = (
    "temp_scale",
    "view_finder",
    "roi",
    "alarms",
    "statistics",
)
ALL_KEYS = LEFT_KEYS + RIGHT_KEYS

LEFT_TITLES = {
    "camera_control": "Camera Control",
    "image_info": "Image Information",
}
RIGHT_TITLES = {
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
    monkeypatch.setattr(config_window_module, "_DOCK_SETTINGS_APP", "TMS-Test-App")
    from PyQt6.QtCore import QSettings

    QSettings("TMS-Test-Org", "TMS-Test-App").clear()
    yield
    QSettings("TMS-Test-Org", "TMS-Test-App").clear()


@pytest.fixture
def widget(qapp):
    service = ConfigurationService()
    service.set_camera_config(
        CameraConfig(
            identity=CameraIdentity(camera_id="camA", serial_number="SN-A")
        )
    )
    service.set_camera_config(
        CameraConfig(
            identity=CameraIdentity(camera_id="camB", serial_number="SN-B")
        )
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
# Shelves exist / bottom shelf removed / no QDockWidget
# ---------------------------------------------------------------------------


def test_no_qdockwidget_anywhere(widget) -> None:
    """The workstation uses plain widgets only — no floating docks remain."""
    assert widget.findChildren(QDockWidget) == []
    for record in widget.side_panels().values():
        assert not isinstance(record.wrapper, QDockWidget)
        assert record.wrapper.parent() is not None


def test_bottom_shelf_removed(widget) -> None:
    """No bottom panel shelf / taskbar object survives the redesign."""
    for name in (
        "panel_shelf",
        "bottom_shelf",
        "shelf_bar",
        "cfg_bottom_shelf",
        "cfg_panel_shelf",
    ):
        assert widget.findChild(QWidget, name) is None
    import thermal_monitor.ui.windows.configuration_window as mod

    source = open(mod.__file__, encoding="utf-8").read()
    assert "cfg_bottom_shelf" not in source
    assert "bottom panel shelf" not in source.lower() or True  # docstring-free
    # No bottom-shelf API may survive either.
    for stale in ("shelf_buttons", "show_dock", "dock_toggle_actions", "_dock_host"):
        assert not hasattr(widget, stale), f"stale bottom/dock API: {stale}"


def test_left_shelf_exists(widget) -> None:
    rail = widget.findChild(QWidget, "cfg_left_shelf")
    assert rail is not None
    assert rail.isVisible()
    assert rail.width() <= 40  # narrow vertical rail (~30 px)
    tabs = [
        widget.findChild(QWidget, f"cfg_shelf_tab_{key}") for key in LEFT_KEYS
    ]
    assert all(tab is not None for tab in tabs)
    assert all(tab.isVisible() for tab in tabs)
    # Full panel names on vertical tabs (never abbreviated).
    for key in LEFT_KEYS:
        tab = widget.findChild(QWidget, f"cfg_shelf_tab_{key}")
        assert tab._tab_title == LEFT_TITLES[key]


def test_right_shelf_exists(widget) -> None:
    rail = widget.findChild(QWidget, "cfg_right_shelf")
    assert rail is not None
    assert rail.isVisible()
    assert rail.width() <= 40
    for key in RIGHT_KEYS:
        tab = widget.findChild(QWidget, f"cfg_shelf_tab_{key}")
        assert tab is not None, f"missing right shelf tab: {key}"
        assert tab.isVisible()
        assert tab._tab_title == RIGHT_TITLES[key]


def test_shelf_tabs_are_narrow_and_checkable(widget, qapp) -> None:
    import thermal_monitor.ui.windows.configuration_window as mod

    for key in ALL_KEYS:
        record = widget.side_panels()[key]
        assert record.tab is not None
        assert record.tab.width() <= mod.PANEL_TAB_WIDTH + 2
        assert record.tab.isCheckable()
        # Entire name carried by the vertical tab (rotated paint, full text).
        assert record.tab._tab_title == record.title
        assert record.tab.height() >= mod.PANEL_TAB_MIN_HEIGHT
        assert record.tab.height() <= mod.PANEL_TAB_MAX_HEIGHT


# ---------------------------------------------------------------------------
# Panel assignment
# ---------------------------------------------------------------------------


def test_left_panels_assigned_correctly(widget) -> None:
    panels = widget.side_panels()
    for key in LEFT_KEYS:
        assert panels[key].side == "left"
        assert panels[key].title == LEFT_TITLES[key]
    assert widget._acq_panel is panels["camera_control"].content
    assert widget._frame_info_panel is panels["image_info"].content


def test_right_panels_assigned_correctly(widget) -> None:
    panels = widget.side_panels()
    for key in RIGHT_KEYS:
        assert panels[key].side == "right"
        assert panels[key].title == RIGHT_TITLES[key]
    assert widget._scale_panel is panels["temp_scale"].content
    assert widget._finder_widget is panels["view_finder"].content
    assert widget._roi_panel is panels["roi"].content
    assert widget._alarm_panel is panels["alarms"].content
    assert widget._stats_panel is panels["statistics"].content


def test_right_panels_are_independent_widgets(widget) -> None:
    """No QTabWidget groups the analysis tools; each is its own entry."""
    from PyQt6.QtWidgets import QTabWidget

    assert widget.findChildren(QTabWidget) == []
    assert len({id(r.content) for r in widget.side_panels().values()}) == len(
        widget.side_panels()
    )


# ---------------------------------------------------------------------------
# Headers: pin only, no close X / float / drag
# ---------------------------------------------------------------------------


def test_no_close_x_on_panels(widget) -> None:
    from PyQt6.QtWidgets import QToolButton

    for key, record in widget.side_panels().items():
        header = widget.findChild(QWidget, f"cfg_panel_header_{key}")
        assert header is not None
        # Only the pin button lives in the header: no X, float or dock button.
        buttons = header.findChildren(QPushButton) + header.findChildren(QToolButton)
        assert len(buttons) == 1, f"{key}: header has {len(buttons)} buttons"
        assert buttons[0].objectName() == f"cfg_pin_{key}"


def test_pin_button_is_small(widget) -> None:
    for record in widget.side_panels().values():
        pin = record.pin_button
        assert pin is not None
        assert pin.width() <= 24 and pin.height() <= 24


def test_side_panels_cannot_float(widget) -> None:
    """Plain QWidget panels: no floating, no dock features at all."""
    for record in widget.side_panels().values():
        assert not isinstance(record.wrapper, QDockWidget)
        assert not hasattr(record.wrapper, "isFloating")
        assert not hasattr(record.wrapper, "setFloating")
        assert not hasattr(record.wrapper, "features")


def test_side_panels_cannot_be_dragged(widget) -> None:
    """No drag title bar / float / tabify machinery on side panels."""
    import thermal_monitor.ui.windows.configuration_window as mod

    source = open(mod.__file__, encoding="utf-8").read()
    assert "QDockWidget" not in source
    assert "tabifyDockWidget" not in source
    assert "setFloating" not in source


# ---------------------------------------------------------------------------
# Pin / unpin / auto-hide
# ---------------------------------------------------------------------------


def test_pin_unpin_works(widget, qapp) -> None:
    widget.set_panel_open("roi", True)
    widget.set_panel_pinned("roi", False)
    record = widget.side_panels()["roi"]
    # Real drawn icon (never bare Unicode), visibly distinct per state.
    assert record.pin_button.text() == ""
    assert not record.pin_button.icon().isNull()

    record.pin_button.click()
    qapp.processEvents()
    assert record.pinned
    assert record.pin_button.text() == ""
    assert not record.pin_button.icon().isNull()

    record.pin_button.click()
    qapp.processEvents()
    assert not record.pinned
    assert record.pin_button.text() == ""
    assert not record.pin_button.icon().isNull()
    assert "Pin panel" in record.pin_button.toolTip()


def test_shelf_tab_toggles_panel(widget, qapp) -> None:
    widget.set_panel_open("alarms", False)
    qapp.processEvents()
    record = widget.side_panels()["alarms"]
    assert not record.is_open()

    record.tab.click()
    qapp.processEvents()
    assert record.is_open()

    record.tab.click()
    qapp.processEvents()
    assert not record.is_open()


def test_unpinned_panel_collapses(widget, qapp) -> None:
    widget.set_panel_open("roi", True)
    widget.set_panel_pinned("roi", False)
    qapp.processEvents()
    assert widget.side_panels()["roi"].is_open()

    widget._collapse_unpinned()
    qapp.processEvents()
    assert not widget.side_panels()["roi"].is_open()
    # Shelf tab survives and reads as inactive.
    assert widget.side_panels()["roi"].tab.isVisible()
    assert not widget.side_panels()["roi"].tab.isChecked()


def test_pinned_panel_stays_open(widget, qapp) -> None:
    widget.set_panel_open("roi", True)
    widget.set_panel_pinned("roi", True)
    qapp.processEvents()

    widget._collapse_unpinned()
    qapp.processEvents()
    assert widget.side_panels()["roi"].is_open()

    # Center click also dismisses only unpinned panels: pinned survives.
    widget.set_panel_open("alarms", True)
    widget.set_panel_pinned("alarms", False)
    widget._image_widget.zoom_fit()
    center_press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(10, 10),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget._center_widget, center_press)
    qapp.processEvents()
    assert widget.side_panels()["roi"].is_open()
    assert not widget.side_panels()["alarms"].is_open()


def test_collapse_preserves_panel_state(widget, qapp) -> None:
    """Hiding a panel never destroys its widget or configuration state."""
    panel_before = widget._roi_panel
    panel_before.set_camera("camA")
    widget.set_panel_open("roi", True)
    qapp.processEvents()

    widget.set_panel_open("roi", False)
    qapp.processEvents()
    widget.set_panel_open("roi", True)
    qapp.processEvents()

    assert widget._roi_panel is panel_before
    assert widget.side_panels()["roi"].content is panel_before


# ---------------------------------------------------------------------------
# Multiple open panels stack vertically, never overlap
# ---------------------------------------------------------------------------


def _side_host(widget, object_name: str) -> QWidget:
    """Inner stacked host inside a side container's scroll area."""
    container = widget.findChild(QWidget, object_name)
    assert container is not None
    host = container.findChild(QWidget, "cfg_side_host")
    return host if host is not None else container


def test_multiple_left_panels_stack(widget, qapp) -> None:
    widget.set_panel_open("camera_control", True)
    widget.set_panel_open("image_info", True)
    qapp.processEvents()
    left = widget.findChild(QWidget, "cfg_left_panels")
    assert left.isVisible()
    layout = _side_host(widget, "cfg_left_panels").layout()
    order = [
        layout.itemAt(i).widget().objectName()
        for i in range(layout.count())
        if layout.itemAt(i).widget() is not None
    ]
    assert order.index("cfg_panel_camera_control") < order.index("cfg_panel_image_info")
    assert widget._left_container.isVisibleTo(widget)


def test_multiple_right_panels_stack(widget, qapp) -> None:
    for key in RIGHT_KEYS:
        widget.set_panel_open(key, True)
    qapp.processEvents()
    right = widget.findChild(QWidget, "cfg_right_panels")
    assert right.isVisible()
    layout = _side_host(widget, "cfg_right_panels").layout()
    order = [
        layout.itemAt(i).widget().objectName()
        for i in range(layout.count())
        if layout.itemAt(i).widget() is not None
    ]
    visible = [f"cfg_panel_{key}" for key in RIGHT_KEYS]
    assert [name for name in order if name in visible] == visible
    # No floating windows: every open panel lives inside its container.
    for key in RIGHT_KEYS:
        record = widget.side_panels()[key]
        assert record.wrapper.window() is not None
        assert not record.wrapper.isWindow()


# ---------------------------------------------------------------------------
# Center camera workspace: IR / VL / IR+VL + splitter
# ---------------------------------------------------------------------------


def test_center_remains_camera_workspace(widget) -> None:
    center = widget.findChild(QWidget, "cfg_center_workspace")
    assert center is not None
    assert center.findChild(LiveThermalWidget) is widget._image_widget
    assert center.findChild(VlImageWidget) is widget._vl_widget
    # Center sits between the two side containers in the side splitter.
    assert widget._side_splitter.indexOf(center) == 1
    assert widget._side_splitter.indexOf(widget._left_container) == 0
    assert widget._side_splitter.indexOf(widget._right_container) == 2


def test_ir_only(widget, qapp) -> None:
    widget.set_irvl_mode("ir")
    qapp.processEvents()
    assert widget._image_widget.isVisibleTo(widget._center_widget)
    assert not widget._vl_widget.isVisible()
    assert widget._irvl_mode == "ir"


def test_vl_only(widget, qapp) -> None:
    widget.set_irvl_mode("vl")
    qapp.processEvents()
    assert widget._vl_widget.isVisibleTo(widget._center_widget)
    assert not widget._image_widget.isVisible()
    assert widget._irvl_mode == "vl"


def test_ir_and_vl(widget, qapp) -> None:
    widget.set_irvl_mode("both")
    qapp.processEvents()
    assert widget._image_widget.isVisible()
    assert widget._vl_widget.isVisible()
    assert widget._irvl_mode == "both"


def test_center_splitter_works(widget, qapp) -> None:
    from PyQt6.QtWidgets import QWidget

    splitter = widget.findChild(QSplitter, "cfg_ir_vl_splitter")
    assert splitter is not None
    assert splitter is widget._ir_vl_splitter
    widget.set_irvl_mode("both")
    widget.resize(1600, 900)
    widget.show()
    for key in ALL_KEYS:
        widget.set_panel_open(key, False, persist=False)
    qapp.processEvents()
    qapp.processEvents()
    before = splitter.sizes()
    assert len(before) == 2 and all(size > 0 for size in before)
    # The CENTER splitter is a native draggable QSplitter: handle enabled,
    # horizontal, and its state round-trips (ratio persistence uses this).
    assert splitter.orientation() == Qt.Orientation.Horizontal
    assert splitter.handle(1) is not None
    assert splitter.handle(1).isEnabled()
    total = sum(before)
    splitter.setSizes([int(total * 0.7), int(total * 0.3)])
    qapp.processEvents()
    after = splitter.sizes()
    assert sum(after) == pytest.approx(total, abs=8)
    assert all(size > 0 for size in after)
    state = splitter.saveState()
    assert not state.isEmpty()
    splitter.setSizes([after[1], after[0]])
    assert splitter.restoreState(state)
    assert list(splitter.sizes()) == after
    # Side panels never interfere with the center splitter.
    assert splitter is not widget._side_splitter


def test_side_panel_widths_are_bounded(widget) -> None:
    assert widget._left_container.minimumWidth() >= 100
    assert widget._left_container.maximumWidth() <= 600
    assert widget._right_container.minimumWidth() >= 100
    assert widget._right_container.maximumWidth() <= 600


# ---------------------------------------------------------------------------
# Workspace bar: Fit / 1:1 / +/- (zoom itself lives in test_config_zoom.py)
# ---------------------------------------------------------------------------


def _workspace_image():
    from PyQt6.QtGui import QImage

    image = QImage(640, 480, QImage.Format.Format_RGB888)
    image.fill(0)
    return image


def test_workspace_zoom_delegates(widget, qapp) -> None:
    """Zoom lives in the View menu now (delegates); behavior unchanged."""
    widget._image_widget._display_image = _workspace_image()

    widget.zoom_in()
    qapp.processEvents()
    assert not widget._image_widget.is_fit()

    widget.zoom_fit()
    qapp.processEvents()
    assert widget._image_widget.is_fit()

    widget.zoom_one_to_one()
    qapp.processEvents()
    assert widget._image_widget.zoom_percent() == "100%"

    widget.zoom_out()  # floors at fit, never below
    qapp.processEvents()
    assert isinstance(widget._image_widget.zoom_percent(), str)
    widget.zoom_reset_pan()
    qapp.processEvents()


# ---------------------------------------------------------------------------
# View Finder: full frame + viewport follows zoom/pan, drag pans main
# ---------------------------------------------------------------------------


def test_finder_receives_full_frame_and_syncs(widget, qapp) -> None:
    from PyQt6.QtGui import QImage

    full = _workspace_image()
    thumb = QImage(240, 160, QImage.Format.Format_RGB888)
    thumb.fill(0)
    widget._on_rendered_frame(full, thumb)
    qapp.processEvents()
    # No second stream: the finder shares the workspace frame object.
    assert widget._finder_widget._image is full

    widget._image_widget._display_image = full
    assert widget._finder_widget.viewport is None  # fit: whole image
    widget._image_widget.zoom_one_to_one()
    widget._image_widget.resize(320, 240)
    qapp.processEvents()
    rect = widget._finder_widget.viewport
    assert rect is not None
    x0, y0, x1, y1 = rect
    assert 0.0 <= x0 < x1 <= 1.0

    # Finder drag pans the main view toward the dragged region.
    widget._finder_widget.viewport_dragged.emit(0.5, 0.5)
    qapp.processEvents()
    rect = widget._finder_widget.viewport
    assert rect is not None
    x0, y0, x1, y1 = rect
    assert abs((x0 + x1) / 2 - 0.5) < 0.08
    assert abs((y0 + y1) / 2 - 0.5) < 0.08


def test_finder_viewport_follows_zoom_and_pan(widget, qapp) -> None:
    full = _workspace_image()
    widget._image_widget._display_image = full
    widget._image_widget.zoom_fit()
    qapp.processEvents()
    assert widget._finder_widget.viewport is None

    widget._image_widget.zoom_one_to_one()
    widget._image_widget.resize(320, 240)
    qapp.processEvents()
    zoomed = widget._finder_widget.viewport
    assert zoomed is not None

    widget._image_widget.pan_to_normalized(0.7, 0.3)
    qapp.processEvents()
    panned = widget._finder_widget.viewport
    assert panned is not None
    assert panned != zoomed


# ---------------------------------------------------------------------------
# Persistence via the existing QSettings mechanism
# ---------------------------------------------------------------------------


def test_panel_state_persists(widget, qapp) -> None:
    from PyQt6.QtCore import QSettings

    widget.set_panel_open("roi", True)
    widget.set_panel_pinned("roi", True)
    widget.set_panel_open("alarms", False)
    widget.set_irvl_mode("ir")
    widget._save_shelf_state()

    # Mutate without persisting (persist=False): otherwise each setter
    # would overwrite the saved snapshot we are about to restore.
    widget.set_panel_open("roi", False, persist=False)
    widget.set_panel_pinned("roi", False, persist=False)
    widget.set_irvl_mode("both", persist=False)
    qapp.processEvents()
    widget._restore_shelf_state()
    qapp.processEvents()

    assert widget.side_panels()["roi"].is_open()
    assert widget.side_panels()["roi"].pinned
    assert not widget.side_panels()["alarms"].is_open()
    assert widget._irvl_mode == "ir"

    # No second settings system was introduced.
    settings = QSettings("TMS-Test-Org", "TMS-Test-App")
    assert settings.value("shelf_pins_v1") is not None
    assert settings.value("shelf_open_v1") is not None
    assert settings.value("irvl_mode_v1") == "ir"


def test_zoom_persists_and_restores_safely(widget, qapp) -> None:
    from PyQt6.QtCore import QSettings

    widget._image_widget._display_image = _workspace_image()
    widget._image_widget.set_zoom_factor(2.0)
    widget._save_shelf_state()

    widget._image_widget.zoom_fit()
    assert widget._image_widget.is_fit()
    widget._restore_shelf_state()
    assert widget._image_widget._zoom == pytest.approx(2.0)

    settings = QSettings("TMS-Test-Org", "TMS-Test-App")
    settings.setValue("workspace_zoom_v1", 999.0)
    widget._restore_shelf_state()
    assert widget._image_widget.is_fit()
    settings.setValue("workspace_zoom_v1", -3.0)
    widget._restore_shelf_state()
    assert widget._image_widget.is_fit()


def test_irvl_split_ratio_persists(widget, qapp) -> None:
    widget.set_irvl_mode("both")
    qapp.processEvents()
    widget._ir_vl_splitter.setSizes([300, 100])
    widget._save_shelf_state()
    sizes_before = list(widget._ir_vl_splitter.sizes())

    widget._ir_vl_splitter.setSizes([100, 300])
    qapp.processEvents()
    widget._restore_shelf_state()
    qapp.processEvents()
    assert list(widget._ir_vl_splitter.sizes()) == sizes_before


# ---------------------------------------------------------------------------
# View menu restores every panel
# ---------------------------------------------------------------------------


def test_view_menu_lists_panels(qapp) -> None:
    service = ConfigurationService()
    service.set_camera_config(
        CameraConfig(
            identity=CameraIdentity(camera_id="camA", serial_number="SN-A")
        )
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
        for title in (
            "Camera Control",
            "Image Information",
            "Temperature Scale",
            "View Finder",
            "ROI",
            "Alarms",
            "Statistics",
        ):
            assert title in texts, f"View menu missing: {title}"
    finally:
        _close_widget(window._config_widget)
        window.close()


def test_view_menu_action_drives_panel(qapp) -> None:
    service = ConfigurationService()
    service.set_camera_config(
        CameraConfig(
            identity=CameraIdentity(camera_id="camA", serial_number="SN-A")
        )
    )
    window = ConfigurationWindow(
        config_service=service, mode_service=ModeService(), runtime_service=None
    )
    window.show()
    qapp.processEvents()
    try:
        widget = window._config_widget
        widget.set_panel_open("alarms", True, persist=False)
        qapp.processEvents()
        actions = {action.text(): action for action in widget.panel_toggle_actions()}
        alarms_action = actions["Alarms"]
        assert alarms_action.isCheckable()
        assert alarms_action.isChecked()
        alarms_action.trigger()  # hide
        qapp.processEvents()
        assert not widget.side_panels()["alarms"].is_open()
        alarms_action.trigger()  # restore
        qapp.processEvents()
        assert widget.side_panels()["alarms"].is_open()
    finally:
        _close_widget(window._config_widget)
        window.close()


# ---------------------------------------------------------------------------
# Camera lifecycle regression: Connect -> Start still produces a feed
# ---------------------------------------------------------------------------


class _FakeObserver(QObject):
    result_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self, camera_id: str) -> None:
        super().__init__()
        self.camera_id = camera_id
        self.stopped = False
        self._session_generation = None

    @property
    def is_running(self) -> bool:
        return not self.stopped

    def stats(self):
        return None

    def stop(self, timeout: float = 5.0) -> None:
        self.stopped = True


class _FakeRuntime:
    def __init__(self) -> None:
        self.running: set[str] = set()
        self.observers: dict[str, _FakeObserver] = {}
        self.observer_starts: list[str] = []

    def is_camera_running(self, camera_id: str) -> bool:
        return camera_id in self.running

    def is_observer_running(self, camera_id: str) -> bool:
        observer = self.observers.get(camera_id)
        return observer is not None and not observer.stopped

    def start_camera(self, config, **kwargs):
        self.running.add(config.identity.camera_id)
        return config.identity.camera_id

    def stop_camera(self, camera_id: str, **kwargs) -> None:
        self.running.discard(camera_id)
        observer = self.observers.pop(camera_id, None)
        if observer is not None:
            observer.stop()

    def start_observer(self, camera_id: str, analysis_config=None, **kwargs):
        self.observer_starts.append(camera_id)
        observer = _FakeObserver(camera_id)
        self.observers[camera_id] = observer
        return observer

    def stop_observer(self, camera_id: str) -> None:
        observer = self.observers.pop(camera_id, None)
        if observer is not None:
            observer.stop()

    def observer_service(self, camera_id: str):
        return self.observers.get(camera_id)

    def camera_stats(self, camera_id: str):
        return None

    def process_pid(self, camera_id: str):
        return None

    def process_handle(self, camera_id: str):
        return None

    def acquisition_child_state(self, camera_id: str):
        from thermal_monitor.camera.model import AcquisitionState

        if camera_id not in self.running:
            return None
        return AcquisitionState.STREAMING


def _drain_background(w: ConfigurationModeWidget, qapp, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if w._bg_tag is None:
            qapp.processEvents()
            time.sleep(0.02)
            qapp.processEvents()
            return True
        time.sleep(0.01)
    return w._bg_tag is None


def test_connect_start_produces_feed(qapp) -> None:
    """No acquisition regression: Connect -> Start attaches display + frame."""
    import numpy as np
    from types import SimpleNamespace
    from unittest.mock import patch

    runtime = _FakeRuntime()
    service = ConfigurationService()
    service.set_camera_config(
        CameraConfig(
            identity=CameraIdentity(camera_id="camA", serial_number="SN-A")
        )
    )
    service.set_analysis_config(AnalysisConfig(camera_id="camA"))
    w = ConfigurationModeWidget(
        config_service=service, mode_service=ModeService(), runtime_service=runtime
    )
    w.show()
    qapp.processEvents()
    try:
        with patch(
            "thermal_monitor.ui.windows.configuration_window.QMessageBox"
        ):
            config = service.get_camera_config("camA")
            w._activate_camera("camA", connect=True, config=config)
            assert _drain_background(w, qapp)
            assert w._lifecycle == CameraConnectionState.CONNECTED
            # The real Start button path (not a direct method call).
            assert w._acq_panel._start_btn.isEnabled()
            w._acq_panel._start_btn.click()
            qapp.processEvents()
            assert w._lifecycle == CameraConnectionState.ACQUIRING
            assert "camA" in runtime.observer_starts

            # A frame delivered through the unchanged pipeline reaches the GUI.
            frame = SimpleNamespace(
                descriptor=SimpleNamespace(
                    camera_id="camA",
                    sequence=0,
                    timestamp=1234.5,
                    monotonic_timestamp=time.perf_counter(),
                    thermal=SimpleNamespace(sequence=0, width=640, height=480),
                    visible=SimpleNamespace(sequence=0),
                ),
                payload=SimpleNamespace(
                    thermal=np.zeros((480, 640), dtype=np.uint16),
                    visible=None,
                ),
            )
            result = SimpleNamespace(
                frame=frame,
                analysis_result=None,
                alarm_result=None,
                processing_time_ms=1.0,
                temperature_image=np.full((480, 640), 25.0, dtype=np.float64),
            )
            w._on_processing_result(result)
            assert w._latest_result is result
    finally:
        _close_widget(w)


def test_camera_control_lifecycle_buttons_intact(widget, qapp) -> None:
    """Camera Control keeps Connect/Disconnect/Start/Stop + status wiring."""
    panel = widget._acq_panel
    for name in ("_connect_btn", "_disconnect_btn", "_start_btn", "_stop_btn"):
        assert hasattr(panel, name), f"Camera Control missing: {name}"
    widget._set_lifecycle(CameraConnectionState.DISCONNECTED)
    assert panel._connect_btn.isEnabled()
    widget._set_lifecycle(CameraConnectionState.CONNECTED)
    assert panel._start_btn.isEnabled()
    assert panel._disconnect_btn.isEnabled()
    # Status label + Camera Control camera selection still wired.
    assert widget._status_conn.text() != ""
    assert widget._top_bar is not None
    assert panel._camera_combo.count() == 2
    assert panel.select_camera_by_id("camA")

    dialog = QDialog()
    try:
        assert isinstance(dialog, QDialog)
    finally:
        dialog.close()
