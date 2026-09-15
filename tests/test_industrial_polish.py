"""
Compact industrial polish pass (ThermoView-inspired density).

Covers: shelf narrowed by 2 px with complete vertical names, compact
button sizing via light-only theme metrics, panel density from central
tokens, the redesigned Industrial Light theme (grey/blue-grey,
de-purpled) with the dark theme untouched, the compact Temperature
Scale, panel headers, and font-scale compatibility.
"""

from __future__ import annotations

import pytest

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QGroupBox

import thermal_monitor.camera.source  # noqa: F401  (init camera package first)
import thermal_monitor.ui.theme.fonts as fonts
import thermal_monitor.ui.windows.configuration_window as config_window_module
from thermal_monitor.core.models import CameraConfig, CameraIdentity
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.mode import ModeService
from thermal_monitor.ui.theme.manager import ThemeManager
from thermal_monitor.ui.theme.stylesheet import build_stylesheet
from thermal_monitor.ui.theme.themes import BUILTIN_THEMES
from thermal_monitor.ui.theme.tokens import DEFAULT_METRICS, LIGHT_METRICS
from thermal_monitor.ui.widgets.image_acquisition_panel import ImageAcquisitionPanel
from thermal_monitor.ui.widgets.thermal_scale_panel import ThermalScalePanel
from thermal_monitor.ui.windows.configuration_window import ConfigurationModeWidget


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def isolated_font_settings(monkeypatch):
    monkeypatch.setattr(fonts, "FONT_SETTINGS_ORG", "TMS-Test-Org")
    monkeypatch.setattr(fonts, "FONT_SETTINGS_APP", "TMS-Test-Polish2")
    QSettings("TMS-Test-Org", "TMS-Test-Polish2").clear()
    yield
    QSettings("TMS-Test-Org", "TMS-Test-Polish2").clear()


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
# Shelf: ThermoView-style width, complete vertical names
# ---------------------------------------------------------------------------


def test_shelf_dimensions_reduced() -> None:
    assert config_window_module.PANEL_SHELF_WIDTH == 40
    assert config_window_module.PANEL_TAB_WIDTH == 36
    assert config_window_module.PANEL_TAB_MIN_HEIGHT == 60
    assert config_window_module.PANEL_TAB_MAX_HEIGHT == 400


def test_rails_match_central_constants(widget) -> None:
    from PyQt6.QtWidgets import QWidget

    for name in ("cfg_left_shelf", "cfg_right_shelf"):
        rail = widget.findChild(QWidget, name)
        assert rail.width() == config_window_module.PANEL_SHELF_WIDTH
    for record in widget.side_panels().values():
        assert record.tab.width() == config_window_module.PANEL_TAB_WIDTH
        assert record.tab._tab_title == record.title  # never abbreviated


def test_tab_heights_follow_name_length(widget) -> None:
    heights = {
        key: record.tab.height() for key, record in widget.side_panels().items()
    }
    # Longer names consume vertical space, never horizontal space.
    assert heights["roi"] <= heights["view_finder"]
    assert heights["view_finder"] <= heights["configuration_editor"] if "configuration_editor" in heights else True
    for height in heights.values():
        assert (
            config_window_module.PANEL_TAB_MIN_HEIGHT
            <= height
            <= config_window_module.PANEL_TAB_MAX_HEIGHT
        )


# ---------------------------------------------------------------------------
# Compact buttons via light-only metrics (dark theme untouched)
# ---------------------------------------------------------------------------


def test_light_metrics_are_compact() -> None:
    assert LIGHT_METRICS.control_height < DEFAULT_METRICS.control_height
    assert LIGHT_METRICS.button_padding_v < DEFAULT_METRICS.button_padding_v
    assert LIGHT_METRICS.panel_spacing < DEFAULT_METRICS.panel_spacing
    assert LIGHT_METRICS.font_size_base == DEFAULT_METRICS.font_size_base


def test_button_sizing_in_stylesheet() -> None:
    light = build_stylesheet(BUILTIN_THEMES["industrial_light"], 100)
    dark = build_stylesheet(BUILTIN_THEMES["industrial_dark"], 100)
    # Compact rectangular buttons in light: min-height 22px, 3px padding.
    assert "min-height: 22px;" in light
    assert "padding: 3px 10px;" in light
    # Dark theme keeps its historic sizing.
    assert "padding: 6px 12px;" in dark
    # Inputs tighten in light only.
    assert "padding: 3px 6px;" in light
    assert "padding: 4px 8px;" in dark


def test_button_grows_with_font_scale() -> None:
    light_big = build_stylesheet(BUILTIN_THEMES["industrial_light"], 130)
    assert "min-height: 29px;" in light_big  # round(22 * 1.3)


# ---------------------------------------------------------------------------
# Panel density from central tokens
# ---------------------------------------------------------------------------


def test_camera_control_uses_compact_metrics(qapp) -> None:
    panel = ImageAcquisitionPanel(None)
    try:
        assert panel.layout().spacing() == DEFAULT_METRICS.panel_spacing
    finally:
        panel.close()


def test_camera_control_light_density(qapp) -> None:
    manager = ThemeManager(None)
    manager.set_theme("industrial_light")
    panel = ImageAcquisitionPanel(manager)
    try:
        assert panel.layout().spacing() == LIGHT_METRICS.panel_spacing
        assert panel.layout().spacing() < DEFAULT_METRICS.panel_spacing
    finally:
        panel.close()


def test_no_excessive_group_margins(qapp) -> None:
    """Group content margins come from tokens, not scattered literals."""
    panel = ImageAcquisitionPanel(None)
    try:
        for box in panel.findChildren(QGroupBox):
            margins = box.layout().contentsMargins()
            assert margins.left() <= DEFAULT_METRICS.panel_group_margin
            assert margins.top() <= DEFAULT_METRICS.panel_group_margin_top
    finally:
        panel.close()


# ---------------------------------------------------------------------------
# Industrial Light redesign / dark untouched
# ---------------------------------------------------------------------------


def test_industrial_light_is_grey_not_white() -> None:
    definition = BUILTIN_THEMES["industrial_light"]
    assert definition.background == "#E9ECEF"
    assert definition.surface == "#F4F6F8"
    assert definition.surface_alt == "#D9E0E7"
    assert definition.title == "#455A64"


def test_industrial_light_de_purpled() -> None:
    definition = BUILTIN_THEMES["industrial_light"]
    assert definition.accent == "#546E7A"
    assert definition.accent != "#7B1FA2"
    # Functional + semantic + thermal colors intentionally unchanged.
    assert definition.primary == "#2E7D32"
    assert definition.secondary == "#1976D2"
    assert definition.alarm == "#D32F2F"
    assert definition.thermal_hot == "#FF5722"
    assert definition.thermal_cold == "#2196F3"


def test_dark_theme_untouched() -> None:
    definition = BUILTIN_THEMES["industrial_dark"]
    assert definition.background == "#15181D"
    assert definition.accent == "#D98E2B"
    assert definition.metrics is DEFAULT_METRICS


def test_light_stylesheet_uses_grey_header_rule() -> None:
    sheet = build_stylesheet(BUILTIN_THEMES["industrial_light"], 100)
    assert '[panelHeader="true"]' in sheet
    assert "#D9E0E7" in sheet  # muted blue-grey header surface


def test_explicit_widget_type_scales_in_stylesheet() -> None:
    """Shelf/tab/header type follows the font setting in the central QSS."""
    base = build_stylesheet(BUILTIN_THEMES["industrial_light"], 100)
    large = build_stylesheet(BUILTIN_THEMES["industrial_light"], 130)
    assert '[shelfTab="true"]' in base
    assert '[panelTitle="true"]' in base
    assert "font-size: 10px;" in base
    assert "font-size: 13px;" in large  # round(10 * 1.3)


def test_tabs_carry_shelf_property(widget) -> None:
    for record in widget.side_panels().values():
        assert record.tab.property("shelfTab") is True


def test_panel_headers_carry_header_property(widget) -> None:
    from PyQt6.QtWidgets import QWidget

    for key in widget.side_panels():
        header = widget.findChild(QWidget, f"cfg_panel_header_{key}")
        assert header is not None
        assert header.property("panelHeader") is True


# ---------------------------------------------------------------------------
# Temperature Scale compact layout
# ---------------------------------------------------------------------------


def test_temperature_scale_compact(qapp) -> None:
    panel = ThermalScalePanel(None)
    try:
        assert panel._legend.minimumWidth() == 48
        assert panel._legend.maximumWidth() == 64
        # Controls still present and functional.
        assert panel._palette_combo.count() == 5
        panel.set_palette("iron")
        assert panel._legend._palette == "iron"
        panel.set_manual_range(10.0, 50.0)
        panel.update_cursor_temperature(23.5)
        assert "23.5" in panel._cursor_temp_label.text()
    finally:
        panel.close()


def test_temperature_scale_renders(qapp) -> None:
    panel = ThermalScalePanel(None)
    try:
        panel.show()
        qapp.processEvents()
        pixmap = panel.grab()
        assert not pixmap.isNull()
        assert pixmap.width() > 0 and pixmap.height() > 0
    finally:
        panel.close()


# ---------------------------------------------------------------------------
# Font-scale compatibility at large scale
# ---------------------------------------------------------------------------


def test_dense_layout_survives_large_font(widget, qapp, isolated_font_settings) -> None:
    """130% scale: panels grow, scroll areas absorb, nothing collapses."""
    from PyQt6.QtWidgets import QScrollArea, QWidget

    fonts.apply_font_scale(130, theme_manager=None, app=qapp)
    qapp.processEvents()
    try:
        for key, record in widget.side_panels().items():
            assert record.wrapper.minimumWidth() >= 100
            scroll = record.wrapper.findChild(QScrollArea, f"cfg_panel_scroll_{key}")
            assert scroll is not None
        center = widget.findChild(QWidget, "cfg_center_workspace")
        assert center.width() > 0
    finally:
        fonts.apply_font_scale(100, theme_manager=None, app=qapp)
        qapp.processEvents()
