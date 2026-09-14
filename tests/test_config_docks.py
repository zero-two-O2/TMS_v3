"""
Tests for Configuration Mode dock workstation (Part 2).

Verifies dock creation/placement, hide/show without state loss,
drag/reorder capability, central workspace integrity, layout
persistence and the professional View menu — all headless (offscreen).
"""

from __future__ import annotations

import pytest

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDockWidget, QMainWindow

import thermal_monitor.camera.source  # noqa: F401  (init camera package first)
import thermal_monitor.ui.windows.configuration_window as config_window_module
from thermal_monitor.core.models import CameraConfig, CameraIdentity
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.mode import ModeService
from thermal_monitor.ui.modes.observer_image import LiveThermalWidget
from thermal_monitor.ui.modes.vl_image import VlImageWidget
from thermal_monitor.ui.windows.configuration_window import (
    ConfigurationModeWidget,
    ConfigurationWindow,
)


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(autouse=True)
def isolated_dock_settings(monkeypatch):
    """Keep dock persistence hermetic: never read/write real user settings."""
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


def test_dock_creation(widget) -> None:
    """All required panels exist as independent docks."""
    docks = widget.docks()
    for key in (
        "camera_control",
        "image_info",
        "temp_scale",
        "view_finder",
        "roi",
        "alarms",
        "statistics",
    ):
        assert key in docks, f"missing dock: {key}"
    assert docks["camera_control"].windowTitle() == "Camera Control"
    assert docks["image_info"].windowTitle() == "Image Information"
    assert docks["temp_scale"].windowTitle() == "Temperature Scale"
    assert docks["view_finder"].windowTitle() == "View Finder"
    assert docks["roi"].windowTitle() == "ROI"
    assert docks["alarms"].windowTitle() == "Alarms"
    assert docks["statistics"].windowTitle() == "Statistics"


def test_dock_left_right_placement(widget) -> None:
    """Camera controls collapse left, analysis tools collapse right."""
    host = widget._dock_host
    for key in ("camera_control", "image_info"):
        assert host.dockWidgetArea(widget.docks()[key]) == Qt.DockWidgetArea.LeftDockWidgetArea
    for key in ("temp_scale", "view_finder", "roi", "alarms", "statistics"):
        assert host.dockWidgetArea(widget.docks()[key]) == Qt.DockWidgetArea.RightDockWidgetArea


def test_dock_features_movable_floatable(widget) -> None:
    """Every dock can be dragged, floated and closed (not deleted)."""
    for dock in widget.docks().values():
        features = dock.features()
        assert features & QDockWidget.DockWidgetFeature.DockWidgetMovable
        assert features & QDockWidget.DockWidgetFeature.DockWidgetFloatable
        assert features & QDockWidget.DockWidgetFeature.DockWidgetClosable


def test_dock_hide_show_preserves_state(widget, qapp) -> None:
    """Hiding a dock collapses it; showing restores it with state intact."""
    docks = widget.docks()
    roi_dock = docks["roi"]
    roi_panel = widget._roi_panel
    roi_panel.set_camera("camA")

    roi_dock.hide()
    qapp.processEvents()
    assert not roi_dock.isVisibleTo(roi_dock.parentWidget())

    widget.show_dock("roi")
    qapp.processEvents()
    assert roi_dock.isVisible()
    # Same panel object, same camera — state was never recreated.
    assert roi_dock.widget() is roi_panel
    assert widget._roi_panel is roi_panel


def test_dock_toggle_actions_checkable_and_synced(widget, qapp) -> None:
    """View-menu actions reflect and drive dock visibility."""
    actions = widget.dock_toggle_actions()
    assert len(actions) >= 7
    by_text = {action.text(): action for action in actions}
    assert "Camera Control" in by_text
    assert "ROI" in by_text
    for action in actions:
        assert action.isCheckable()

    alarms_action = by_text["Alarms"]
    alarms_dock = widget.docks()["alarms"]
    assert alarms_action.isChecked()
    alarms_action.trigger()  # uncheck -> hide
    qapp.processEvents()
    assert not alarms_dock.isVisible()
    assert not alarms_action.isChecked()
    alarms_action.trigger()  # check -> restore
    qapp.processEvents()
    assert alarms_dock.isVisible()


def test_multiple_panels_share_side_without_hardcoded_order(widget) -> None:
    """Right-side docks stack; tabification (user reorder) is supported."""
    host = widget._dock_host
    docks = widget.docks()
    # Native stacking: all right docks visible simultaneously in one area.
    host.tabifyDockWidget(docks["roi"], docks["alarms"])
    tabified = host.tabifiedDockWidgets(docks["roi"])
    assert docks["alarms"] in tabified
    # Untabify path also works (dock remains a real dock).
    host.tabifyDockWidget(docks["alarms"], docks["statistics"])
    assert docks["statistics"] in host.tabifiedDockWidgets(docks["alarms"])


def test_central_workspace_holds_ir_vl(widget) -> None:
    """IR/VL displays own the central workspace with 4:3 minimums."""
    central = widget._dock_host.centralWidget()
    assert central is not None
    ir = central.findChild(LiveThermalWidget)
    vl = central.findChild(VlImageWidget)
    assert ir is widget._image_widget
    assert vl is widget._vl_widget
    # 640x480 (4:3) minimums: side docks can never squeeze below aspect.
    assert ir.minimumWidth() * 3 == ir.minimumHeight() * 4
    assert vl.minimumWidth() * 3 == vl.minimumHeight() * 4


def test_dock_layout_persistence_roundtrip(widget, qapp) -> None:
    """saveState/restoreState preserves visibility + floating arrangement."""
    host = widget._dock_host
    saved = host.saveState(1)
    assert not saved.isEmpty()

    widget.docks()["statistics"].hide()
    widget.docks()["view_finder"].hide()
    qapp.processEvents()
    assert not widget.docks()["statistics"].isVisible()

    assert host.restoreState(saved, 1)
    qapp.processEvents()
    assert widget.docks()["statistics"].isVisible()
    assert widget.docks()["view_finder"].isVisible()

    # Settings-backed helpers use the same mechanism without new systems.
    widget._save_dock_layout()
    widget.docks()["statistics"].hide()
    qapp.processEvents()
    widget._restore_dock_layout()
    qapp.processEvents()
    assert widget.docks()["statistics"].isVisible()


def test_view_menu_lists_panels(qapp) -> None:
    """Professional View menu restores every panel after hiding."""
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
        try:
            window._config_widget._image_widget.close()
        except Exception:
            pass
        try:
            window._config_widget._vl_widget.close()
        except Exception:
            pass
        window.close()


# ---------------------------------------------------------------------------
# Panel shelf (taskbar)
# ---------------------------------------------------------------------------


def test_every_dock_has_shelf_item(widget) -> None:
    """Each dockable panel owns exactly one shelf control."""
    shelf = widget.shelf_buttons()
    assert set(shelf.keys()) == set(widget.docks().keys())
    for key, button in shelf.items():
        assert button.text() == widget.docks()[key].windowTitle()
        assert button.isCheckable()


def test_closing_dock_leaves_shelf_item(widget, qapp) -> None:
    """Hiding a dock keeps its shelf item, now reading as inactive."""
    dock = widget.docks()["roi"]
    button = widget.shelf_buttons()["roi"]
    assert button.isChecked()
    dock.hide()
    qapp.processEvents()
    assert not dock.isVisible()
    assert "roi" in widget.shelf_buttons()
    assert not button.isChecked()
    assert button.property("variant") == "ghost"


def test_shelf_click_restores_hidden_dock(widget, qapp) -> None:
    """Clicking a hidden dock's shelf item restores it in place."""
    dock = widget.docks()["alarms"]
    panel = dock.widget()
    dock.hide()
    qapp.processEvents()
    assert not dock.isVisible()
    widget.shelf_buttons()["alarms"].click()
    qapp.processEvents()
    assert dock.isVisible()
    # Same dock, same panel: nothing recreated, no duplicates.
    assert dock.widget() is panel
    assert len(widget.docks()) == len(widget.shelf_buttons())


def test_shelf_click_on_visible_dock_raises_without_duplicates(widget, qapp) -> None:
    """Clicking a visible dock's item raises it; dock count never grows."""
    before = len(widget.docks())
    dock = widget.docks()["statistics"]
    assert dock.isVisible()
    widget.shelf_buttons()["statistics"].click()
    qapp.processEvents()
    assert dock.isVisible()
    assert len(widget.docks()) == before


def test_floating_dock_remains_represented(widget, qapp) -> None:
    """A floating dock still reads as open on the shelf."""
    dock = widget.docks()["view_finder"]
    dock.setFloating(True)
    qapp.processEvents()
    assert dock.isFloating()
    assert dock.isVisible()
    button = widget.shelf_buttons()["view_finder"]
    assert button.isChecked()
    assert button.property("variant") == "accent"
    dock.setFloating(False)
    qapp.processEvents()


def test_shelf_usable_when_all_docks_hidden(widget, qapp) -> None:
    """The shelf survives outside the dock host: every dock restorable."""
    for dock in widget.docks().values():
        dock.hide()
    qapp.processEvents()
    assert all(not dock.isVisible() for dock in widget.docks().values())
    shelf = widget.shelf_buttons()
    assert len(shelf) == len(widget.docks())
    for key, button in shelf.items():
        assert button.isVisible()  # the shelf itself never collapses
        button.click()
    qapp.processEvents()
    assert all(dock.isVisible() for dock in widget.docks().values())


# ---------------------------------------------------------------------------
# Window snap (native behavior preserved)
# ---------------------------------------------------------------------------


def test_floating_dock_is_native_top_level_window(widget, qapp) -> None:
    """Floating docks are real top-level windows: Windows Aero snap applies.

    Qt does not intercept native window management for floating docks,
    so Win+arrows / edge-drag snapping works without any custom system.
    """
    dock = widget.docks()["roi"]
    dock.setFloating(True)
    qapp.processEvents()
    try:
        assert dock.isFloating()
        assert dock.isWindow()  # top-level: owned by the OS window manager
    finally:
        dock.setFloating(False)
        qapp.processEvents()


def test_dock_host_keeps_native_indicators(widget) -> None:
    """Internal drag indicators come from QMainWindow docking itself."""
    options = widget._dock_host.dockOptions()
    assert options & QMainWindow.DockOption.AllowNestedDocks
    assert options & QMainWindow.DockOption.AllowTabbedDocks
    assert options & QMainWindow.DockOption.AnimatedDocks


# ---------------------------------------------------------------------------
# Workspace bar + View Finder wiring + zoom persistence
# ---------------------------------------------------------------------------


def _workspace_image() -> "QImage":
    from PyQt6.QtGui import QImage

    image = QImage(640, 480, QImage.Format.Format_RGB888)
    image.fill(0)
    return image


def test_workspace_bar_controls(widget, qapp) -> None:
    """IR identity + Fit/step/1:1 controls drive the view only."""
    assert widget._zoom_label.text() == "Fit"
    widget._image_widget._display_image = _workspace_image()

    widget._zoom_in_btn.click()
    qapp.processEvents()
    assert not widget._image_widget.is_fit()
    assert widget._zoom_label.text() == widget._image_widget.zoom_percent()

    widget._zoom_fit_btn.click()
    qapp.processEvents()
    assert widget._image_widget.is_fit()
    assert widget._zoom_label.text() == "Fit"

    widget._zoom_1to1_btn.click()
    qapp.processEvents()
    assert widget._zoom_label.text() == "100%"

    widget._zoom_out_btn.click()  # floors at fit, never below
    qapp.processEvents()
    assert widget._zoom_label.text() in ("Fit", widget._image_widget.zoom_percent())


def test_finder_receives_full_frame_and_syncs(widget, qapp) -> None:
    """Same latest frame in finder; rect tracks zoom; drag pans main."""
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


def test_zoom_persists_and_restores_safely(widget, qapp) -> None:
    """Zoom/pan persist with the layout; garbage restores as Fit."""
    from PyQt6.QtCore import QSettings

    widget._image_widget._display_image = _workspace_image()
    widget._image_widget.set_zoom_factor(2.0)
    widget._save_dock_layout()

    widget._image_widget.zoom_fit()
    assert widget._image_widget.is_fit()
    widget._restore_dock_layout()
    assert widget._image_widget._zoom == pytest.approx(2.0)

    settings = QSettings("TMS-Test-Org", "TMS-Test-App")
    settings.setValue("workspace_zoom_v1", 999.0)
    widget._restore_dock_layout()
    assert widget._image_widget.is_fit()
    settings.setValue("workspace_zoom_v1", -3.0)
    widget._restore_dock_layout()
    assert widget._image_widget.is_fit()
