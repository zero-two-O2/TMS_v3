"""
Global font-size setting (Settings -> Font Size).

Covers: defaults, options/labels, clamping, QSettings persistence,
token-level stylesheet scaling (no QSS patch), live apply to the
running app, shelf/tab/header response, dialogs staying usable, and
the central camera workspace staying valid.
"""

from __future__ import annotations

import pytest

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

import thermal_monitor.camera.source  # noqa: F401  (init camera package first)
import thermal_monitor.ui.theme.fonts as fonts
from thermal_monitor.core.models import CameraConfig, CameraIdentity
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.mode import ModeService
from thermal_monitor.ui.theme.manager import ThemeManager
from thermal_monitor.ui.theme.menu import FONT_SIZE_MENU_TITLE, ThemeMenuController
from thermal_monitor.ui.theme.themes import BUILTIN_THEMES
from thermal_monitor.ui.windows.configuration_window import ConfigurationModeWidget


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def isolated_font_settings(monkeypatch):
    """Hermetic QSettings scope for the font preference."""
    monkeypatch.setattr(fonts, "FONT_SETTINGS_ORG", "TMS-Test-Org")
    monkeypatch.setattr(fonts, "FONT_SETTINGS_APP", "TMS-Test-Fonts")
    QSettings("TMS-Test-Org", "TMS-Test-Fonts").clear()
    yield
    QSettings("TMS-Test-Org", "TMS-Test-Fonts").clear()


@pytest.fixture
def widget(qapp, isolated_font_settings):
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


# ---------------------------------------------------------------------------
# Defaults / options / persistence
# ---------------------------------------------------------------------------


def test_default_font_scale(isolated_font_settings) -> None:
    assert fonts.current_font_scale() == 100


def test_scale_options_and_labels() -> None:
    assert fonts.FONT_SCALE_OPTIONS == (90, 100, 110, 120, 130, 140)
    assert fonts.FONT_SCALE_LABELS[90] == "Small"
    assert fonts.FONT_SCALE_LABELS[130] == "130%"


def test_scale_clamping(isolated_font_settings) -> None:
    assert fonts.save_font_scale(999) == 150
    assert fonts.save_font_scale(-5) == 80
    assert fonts.save_font_scale("bogus") == 100
    assert fonts.save_font_scale(120) == 120


def test_scale_persists(isolated_font_settings) -> None:
    fonts.save_font_scale(130)
    assert fonts.current_font_scale() == 130
    fonts.save_font_scale(100)
    assert fonts.current_font_scale() == 100


def test_scaled_font_px_math() -> None:
    assert fonts.scaled_font_px(13, 100) == 13
    assert fonts.scaled_font_px(13, 130) == 17
    assert fonts.scaled_font_px(13, 90) == 12
    assert fonts.scaled_font_px(10, 90) == 9  # floored, still readable


# ---------------------------------------------------------------------------
# Token-level stylesheet scaling (same stylesheet, scaled tokens)
# ---------------------------------------------------------------------------


def test_stylesheet_scales_with_setting() -> None:
    from thermal_monitor.ui.theme.stylesheet import build_stylesheet

    definition = BUILTIN_THEMES["industrial_dark"]
    base = build_stylesheet(definition, 100)
    large = build_stylesheet(definition, 130)
    assert "font-size: 13px;" in base
    assert "font-size: 17px;" in large
    # Structure identical apart from scaled values: same selectors.
    assert base.count("QPushButton") == large.count("QPushButton")
    assert base.count("font-size") == large.count("font-size")


def test_manager_carries_scale(isolated_font_settings) -> None:
    manager = ThemeManager(None)
    assert manager.font_scale_pct == 100
    manager.set_font_scale(140)
    assert manager.font_scale_pct == 140
    assert "font-size: 18px;" in manager.base_stylesheet()  # 13 * 1.4
    manager.set_font_scale(100)
    assert "font-size: 13px;" in manager.base_stylesheet()


# ---------------------------------------------------------------------------
# Live apply + shelf response + workspace validity
# ---------------------------------------------------------------------------


def test_apply_scales_shelf_and_headers(widget, qapp, isolated_font_settings) -> None:
    """130%: tab heights grow from the deterministic constructed font.

    (Widget .font() itself is QSS-driven whenever a theme is active, so
    height — not pointSize — is the behavioral assertion. Type size is
    covered by the central-QSS tests.)
    """
    record = widget.side_panels()["statistics"]  # headroom above MIN, below MAX
    before_height = record.tab.height()
    title = widget.findChild(
        __import__("PyQt6.QtWidgets", fromlist=["QLabel"]).QLabel,
        "cfg_panel_title_temp_scale",
    )
    assert title.property("panelTitle") is True

    fonts.apply_font_scale(130, theme_manager=None, app=qapp)
    qapp.processEvents()

    assert record.tab.height() > before_height
    # Shelf stays narrow: width is a constant, never font-driven.
    import thermal_monitor.ui.windows.configuration_window as mod

    assert record.tab.width() <= mod.PANEL_TAB_WIDTH + 2
    assert widget.findChild(
        __import__("PyQt6.QtWidgets", fromlist=["QWidget"]).QWidget,
        "cfg_right_shelf",
    ).width() <= 40


def test_apply_smaller_scale(widget, qapp, isolated_font_settings) -> None:
    record = widget.side_panels()["roi"]
    before_height = record.tab.height()
    fonts.apply_font_scale(90, theme_manager=None, app=qapp)
    qapp.processEvents()
    assert record.tab.height() <= before_height
    assert record.tab.height() >= 60  # still clickable, full name intact
    assert record.tab._tab_title == "ROI"


def test_center_workspace_valid_at_large_scale(
    widget, qapp, isolated_font_settings
) -> None:
    from PyQt6.QtWidgets import QSplitter, QWidget

    fonts.apply_font_scale(140, theme_manager=None, app=qapp)
    qapp.processEvents()
    center = widget.findChild(QWidget, "cfg_center_workspace")
    assert center is not None
    assert center.width() > 0 and center.height() > 0
    splitter = widget.findChild(QSplitter, "cfg_ir_vl_splitter")
    assert splitter is not None
    # 4:3 painters letterbox independently of fonts: widgets stay alive.
    assert widget._image_widget is not None
    assert widget._vl_widget is not None


def test_dialogs_usable_at_large_scale(qapp, isolated_font_settings) -> None:
    from thermal_monitor.ui.widgets import AcquisitionSetupDialog

    fonts.apply_font_scale(140, theme_manager=None, app=qapp)
    qapp.processEvents()
    dialog = AcquisitionSetupDialog()
    try:
        dialog.show()
        qapp.processEvents()
        assert dialog.isVisible()
        assert dialog.minimumWidth() >= 300
        values = dialog.values()
        assert set(values) == {"fps", "averaging", "history_frames"}
    finally:
        dialog.close()


def test_full_apply_path_persists_and_repolishes(
    widget, qapp, isolated_font_settings
) -> None:
    """End-to-end: menu-equivalent apply persists + rescales + repolishes."""
    manager = ThemeManager(None)
    applied = fonts.apply_font_scale(120, theme_manager=manager, app=qapp)
    qapp.processEvents()
    try:
        assert applied == 120
        assert fonts.current_font_scale() == 120
        assert manager.font_scale_pct == 120
        assert "font-size: 16px;" in manager.base_stylesheet()  # 13 * 1.2
    finally:
        fonts.apply_font_scale(100, theme_manager=manager, app=qapp)
        qapp.processEvents()


# ---------------------------------------------------------------------------
# Settings -> Font Size menu
# ---------------------------------------------------------------------------


def test_font_size_menu_exists(qapp, isolated_font_settings) -> None:
    controller = ThemeMenuController(theme_manager=ThemeManager(None))
    try:
        settings = controller.create_settings_menu(None)
        titles = [
            action.menu().title()
            for action in settings.actions()
            if action.menu() is not None
        ]
        assert FONT_SIZE_MENU_TITLE in titles
        font_menu = next(
            action.menu()
            for action in settings.actions()
            if action.menu() is not None
            and action.menu().title() == FONT_SIZE_MENU_TITLE
        )
        entries = [action.text() for action in font_menu.actions()]
        assert entries == ["Small", "100%", "110%", "120%", "130%", "140%"]
        assert all(action.isCheckable() for action in font_menu.actions())
    finally:
        controller.deleteLater()


def test_font_size_menu_select_applies(qapp, isolated_font_settings) -> None:
    manager = ThemeManager(None)
    controller = ThemeMenuController(theme_manager=manager)
    try:
        applied = controller.select_font_scale(130)
        assert applied == 130
        assert fonts.current_font_scale() == 130
        assert manager.font_scale_pct == 130
    finally:
        controller.select_font_scale(100)
        controller.deleteLater()
