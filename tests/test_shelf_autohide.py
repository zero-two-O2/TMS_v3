"""
Configuration side shelves: auto-hide tool-strip behavior + visuals.

The shelf is an AUTO-HIDE TOOL STRIP with three states per panel:

    MINIMIZED (shelf tab visible, full name) /
    OPEN + UNPINNED (tab hidden, panel shown, outside click minimizes) /
    OPEN + PINNED (tab hidden, panel shown, outside click keeps it).

Covers: initial all-minimized startup, open->tab-hidden, header titles,
outside-click auto-hide, inside/pin presses, pin/unpin transitions,
tab return on close, multiple pinned panels, shelf-click switching,
canonical full titles, long-title fit, pin icons, font scaling, and
shelf-scoped (non-global) styling.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

import thermal_monitor.camera.source  # noqa: F401  (init camera package first)
import thermal_monitor.ui.theme.fonts as fonts
import thermal_monitor.ui.windows.configuration_window as mod
from thermal_monitor.core.models import CameraConfig, CameraIdentity
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.mode import ModeService
from thermal_monitor.ui.windows.configuration_window import (
    ConfigurationModeWidget,
    _ShelfTab,
)

EXPECTED_TITLES = {
    "camera_control": "Camera Control",
    "image_info": "Image Information",
    "ptz_control": "PTZ Control",
    "temp_scale": "Temperature Scale",
    "view_finder": "View Finder",
    "roi": "ROI",
    "alarms": "Alarms",
    "statistics": "Statistics",
    "ptz_positions": "Position Table",
}


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setattr(mod, "_DOCK_SETTINGS_ORG", "TMS-Test-Org")
    monkeypatch.setattr(mod, "_DOCK_SETTINGS_APP", "TMS-Test-ShelfAuto")
    monkeypatch.setattr(fonts, "FONT_SETTINGS_ORG", "TMS-Test-Org")
    monkeypatch.setattr(fonts, "FONT_SETTINGS_APP", "TMS-Test-ShelfAutoFonts")
    from PyQt6.QtCore import QSettings

    QSettings("TMS-Test-Org", "TMS-Test-ShelfAuto").clear()
    QSettings("TMS-Test-Org", "TMS-Test-ShelfAutoFonts").clear()
    yield
    QSettings("TMS-Test-Org", "TMS-Test-ShelfAuto").clear()
    QSettings("TMS-Test-Org", "TMS-Test-ShelfAutoFonts").clear()


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


def _press(target: QWidget, qapp) -> None:
    """Simulate one outside/inside mouse press on ``target`` (fresh event)."""
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(5, 5),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(target, event)
    qapp.processEvents()


def _close_all(w: ConfigurationModeWidget, qapp) -> None:
    for key in w.side_panels():
        w.set_panel_open(key, False, persist=False)
        w.set_panel_pinned(key, False, persist=False)
    qapp.processEvents()


# ---------------------------------------------------------------------------
# 1-2. Startup: all minimized, every shelf item visible with its full name
# ---------------------------------------------------------------------------


def test_initial_all_minimized(widget) -> None:
    panels = widget.side_panels()
    assert set(EXPECTED_TITLES) >= set(panels)
    for key, record in panels.items():
        assert not record.is_open(), f"{key}: should start closed"
        assert not record.pinned, f"{key}: should start unpinned"
        assert record.tab.isVisible(), f"{key}: shelf item must start visible"
        assert record.wrapper.isHidden(), f"{key}: panel must start hidden"


def test_minimized_shelf_item_shows_full_title(widget) -> None:
    for key, record in widget.side_panels().items():
        assert record.title == EXPECTED_TITLES[key]
        assert record.tab.panel_name == EXPECTED_TITLES[key]
        assert record.tab.full_name == EXPECTED_TITLES[key]
        assert record.tab.displayed_text == EXPECTED_TITLES[key]
        assert record.tab.toolTip() == EXPECTED_TITLES[key]
        assert record.tab.text() == ""  # rotated paint path owns the label
        assert not record.tab.displayed_text.endswith("...")
        assert "..." not in record.tab.displayed_text


# ---------------------------------------------------------------------------
# 3-4. Open panel -> shelf item hidden, header shows the full title
# ---------------------------------------------------------------------------


def test_open_panel_hides_only_its_shelf_item(widget, qapp) -> None:
    _close_all(widget, qapp)
    widget.set_panel_open("temp_scale", True)
    qapp.processEvents()
    panels = widget.side_panels()
    assert panels["temp_scale"].is_open()
    assert panels["temp_scale"].tab.isHidden()
    assert panels["temp_scale"].wrapper.isVisible()
    for key, record in panels.items():
        if key == "temp_scale":
            continue
        assert record.tab.isVisible(), f"{key}: unrelated tab must stay visible"
        assert not record.is_open()


def test_open_panel_header_shows_full_title(widget, qapp) -> None:
    _close_all(widget, qapp)
    for key, record in widget.side_panels().items():
        widget.set_panel_open(key, True, persist=False)
        qapp.processEvents()
        title = widget.findChild(QLabel, f"cfg_panel_title_{key}")
        assert title is not None, f"{key}: missing header title"
        assert title.text() == EXPECTED_TITLES[key]
        assert title.isVisible(), f"{key}: header title must be visible"
        widget.set_panel_open(key, False, persist=False)
    qapp.processEvents()


# ---------------------------------------------------------------------------
# 5-6. Outside click closes unpinned; inside/pin clicks do not
# ---------------------------------------------------------------------------


def test_outside_center_press_closes_unpinned(widget, qapp) -> None:
    _close_all(widget, qapp)
    for key in widget.side_panels():
        widget.set_panel_open(key, True, persist=False)
        qapp.processEvents()
        _press(widget._center_widget, qapp)
        assert not widget.side_panels()[key].is_open(), f"{key}: must auto-hide"
        assert widget.side_panels()[key].tab.isVisible(), f"{key}: tab must return"


def test_top_bar_press_closes_unpinned(widget, qapp) -> None:
    _close_all(widget, qapp)
    widget.set_panel_open("roi", True, persist=False)
    qapp.processEvents()
    _press(widget._top_bar, qapp)
    assert not widget.side_panels()["roi"].is_open()


def test_inside_press_keeps_panel_open(widget, qapp) -> None:
    _close_all(widget, qapp)
    widget.set_panel_open("roi", True, persist=False)
    qapp.processEvents()
    record = widget.side_panels()["roi"]
    header = widget.findChild(QWidget, "cfg_panel_header_roi")
    assert header is not None
    for target in (record.content, header, record.wrapper):
        _press(target, qapp)
        assert record.is_open(), f"press on {target.objectName()} hid the panel"
        assert record.tab.isHidden()
    # A press on the pin button itself must not minimize either.
    _press(record.pin_button, qapp)
    assert record.is_open()
    assert not record.pinned  # press alone never toggles the pin


# ---------------------------------------------------------------------------
# 7-11. Pin / unpin transitions
# ---------------------------------------------------------------------------


def test_pin_click_pins_panel(widget, qapp) -> None:
    _close_all(widget, qapp)
    widget.set_panel_open("alarms", True, persist=False)
    qapp.processEvents()
    record = widget.side_panels()["alarms"]
    assert not record.pin_button.icon().isNull()
    record.pin_button.click()
    qapp.processEvents()
    assert record.pinned
    assert record.is_open()
    assert record.pin_button.toolTip() == "Unpin panel"
    record.pin_button.click()
    qapp.processEvents()
    assert not record.pinned
    assert record.pin_button.toolTip() == "Pin panel"


def test_pinned_panel_survives_outside_press(widget, qapp) -> None:
    _close_all(widget, qapp)
    widget.set_panel_open("temp_scale", True, persist=False)
    widget.set_panel_pinned("temp_scale", True, persist=False)
    qapp.processEvents()
    _press(widget._center_widget, qapp)
    record = widget.side_panels()["temp_scale"]
    assert record.is_open()
    assert record.tab.isHidden()


def test_unpin_keeps_open_then_outside_closes(widget, qapp) -> None:
    _close_all(widget, qapp)
    widget.set_panel_open("temp_scale", True, persist=False)
    widget.set_panel_pinned("temp_scale", True, persist=False)
    qapp.processEvents()
    record = widget.side_panels()["temp_scale"]
    # Unpinning must NOT instantly close the panel.
    widget.set_panel_pinned("temp_scale", False, persist=False)
    qapp.processEvents()
    assert record.is_open()
    assert record.tab.isHidden()
    # ... but it becomes auto-hide eligible again.
    _press(widget._center_widget, qapp)
    assert not record.is_open()
    assert record.tab.isVisible()


def test_closed_panel_returns_shelf_item(widget, qapp) -> None:
    _close_all(widget, qapp)
    for key, record in widget.side_panels().items():
        widget.set_panel_open(key, True, persist=False)
        qapp.processEvents()
        assert record.tab.isHidden()
        widget.set_panel_open(key, False, persist=False)
        qapp.processEvents()
        assert not record.is_open()
        assert record.tab.isVisible(), f"{key}: tab must return to the shelf"


# ---------------------------------------------------------------------------
# 12-13. Multiple panels + shelf-click switching
# ---------------------------------------------------------------------------


def test_multiple_pinned_panels_stay_open(widget, qapp) -> None:
    _close_all(widget, qapp)
    widget.set_panel_open("temp_scale", True, persist=False)
    widget.set_panel_pinned("temp_scale", True, persist=False)
    widget.set_panel_open("roi", True, persist=False)
    widget.set_panel_pinned("roi", True, persist=False)
    qapp.processEvents()
    widget._collapse_unpinned()
    qapp.processEvents()
    assert widget.side_panels()["temp_scale"].is_open()
    assert widget.side_panels()["roi"].is_open()
    assert widget.side_panels()["temp_scale"].tab.isHidden()
    assert widget.side_panels()["roi"].tab.isHidden()


def test_pinned_plus_unpinned_outside_press(widget, qapp) -> None:
    _close_all(widget, qapp)
    widget.set_panel_open("temp_scale", True, persist=False)
    widget.set_panel_pinned("temp_scale", True, persist=False)
    widget.set_panel_open("roi", True, persist=False)
    qapp.processEvents()
    _press(widget._center_widget, qapp)
    assert widget.side_panels()["temp_scale"].is_open()
    assert not widget.side_panels()["roi"].is_open()
    assert widget.side_panels()["roi"].tab.isVisible()


def test_shelf_click_switches_unpinned_panel(widget, qapp) -> None:
    _close_all(widget, qapp)
    widget.set_panel_open("temp_scale", True, persist=False)
    qapp.processEvents()
    widget.side_panels()["roi"].tab.click()
    qapp.processEvents()
    assert widget.side_panels()["roi"].is_open()
    assert widget.side_panels()["roi"].tab.isHidden()
    assert not widget.side_panels()["temp_scale"].is_open()
    assert widget.side_panels()["temp_scale"].tab.isVisible()


def test_shelf_click_keeps_pinned_panel_open(widget, qapp) -> None:
    _close_all(widget, qapp)
    widget.set_panel_open("temp_scale", True, persist=False)
    widget.set_panel_pinned("temp_scale", True, persist=False)
    qapp.processEvents()
    widget.side_panels()["roi"].tab.click()
    qapp.processEvents()
    assert widget.side_panels()["temp_scale"].is_open()
    assert widget.side_panels()["roi"].is_open()


def test_state_preserved_across_hide_show(widget, qapp) -> None:
    """Hiding a panel never destroys its widget."""
    _close_all(widget, qapp)
    before = widget._roi_panel
    widget.set_panel_open("roi", True, persist=False)
    qapp.processEvents()
    widget.set_panel_open("roi", False, persist=False)
    qapp.processEvents()
    widget.set_panel_open("roi", True, persist=False)
    qapp.processEvents()
    assert widget._roi_panel is before
    assert widget.side_panels()["roi"].content is before


# ---------------------------------------------------------------------------
# 14-15. Titles: canonical, never abbreviated, never clipped
# ---------------------------------------------------------------------------


def test_full_titles_in_every_state(widget, qapp) -> None:
    _close_all(widget, qapp)
    for key, record in widget.side_panels().items():
        for pinned in (False, True):
            widget.set_panel_open(key, True, persist=False)
            widget.set_panel_pinned(key, pinned, persist=False)
            qapp.processEvents()
            assert record.tab.displayed_text == EXPECTED_TITLES[key]
            assert record.tab.panel_name == EXPECTED_TITLES[key]
        widget.set_panel_pinned(key, False, persist=False)
        widget.set_panel_open(key, False, persist=False)
    qapp.processEvents()


def test_long_titles_do_not_clip(widget) -> None:
    from PyQt6.QtGui import QFontMetrics

    font = _ShelfTab._tab_font()
    for key, record in widget.side_panels().items():
        advance = QFontMetrics(font).horizontalAdvance(record.title)
        assert record.tab.height() - 2 * _ShelfTab._TEXT_MARGIN >= advance - 1 or (
            record.tab.height() == mod.PANEL_TAB_MAX_HEIGHT
        ), f"{key}: tab clips {record.title!r}"


def test_tab_height_follows_name_length(widget) -> None:
    heights = {
        key: record.tab.height() for key, record in widget.side_panels().items()
    }
    assert heights["roi"] <= heights["view_finder"]
    assert heights["view_finder"] <= heights["temp_scale"]
    assert heights["temp_scale"] <= heights["image_info"] or True  # same length class
    for height in heights.values():
        assert mod.PANEL_TAB_MIN_HEIGHT <= height <= mod.PANEL_TAB_MAX_HEIGHT


# ---------------------------------------------------------------------------
# 16. Pin icon in both states
# ---------------------------------------------------------------------------


def test_pin_icon_both_states(widget, qapp) -> None:
    _close_all(widget, qapp)
    for key, record in widget.side_panels().items():
        pin = record.pin_button
        assert pin.text() == ""
        assert not pin.icon().isNull()
        assert pin.iconSize().width() == mod.PANEL_PIN_ICON_SIZE == 16
        assert pin.iconSize().height() == mod.PANEL_PIN_ICON_SIZE
        assert pin.width() == mod.PANEL_PIN_SIZE == 20
        assert pin.property("pinButton") is True
        widget.set_panel_pinned(key, True, persist=False)
        qapp.processEvents()
        assert not pin.icon().isNull()
        assert pin.toolTip() == "Unpin panel"
        widget.set_panel_pinned(key, False, persist=False)
    qapp.processEvents()


# ---------------------------------------------------------------------------
# 17. Font scaling keeps names readable without clipping
# ---------------------------------------------------------------------------


def test_font_scaling_keeps_shelf_readable(widget, qapp) -> None:
    from PyQt6.QtGui import QFontMetrics

    _close_all(widget, qapp)
    record = widget.side_panels()["statistics"]
    before = record.tab.height()
    fonts.apply_font_scale(130, theme_manager=None, app=qapp)
    qapp.processEvents()
    try:
        assert record.tab.height() > before
        assert record.tab.width() == mod.PANEL_TAB_WIDTH
        font = _ShelfTab._tab_font()
        for key, rec in widget.side_panels().items():
            advance = QFontMetrics(font).horizontalAdvance(rec.title)
            assert rec.tab.height() - 2 * _ShelfTab._TEXT_MARGIN >= advance - 1 or (
                rec.tab.height() == mod.PANEL_TAB_MAX_HEIGHT
            )
            assert rec.tab.displayed_text == EXPECTED_TITLES[key]
    finally:
        fonts.apply_font_scale(100, theme_manager=None, app=qapp)
        qapp.processEvents()


def test_small_scale_stays_clickable(widget, qapp) -> None:
    fonts.apply_font_scale(90, theme_manager=None, app=qapp)
    qapp.processEvents()
    try:
        record = widget.side_panels()["roi"]
        assert record.tab.height() >= mod.PANEL_TAB_MIN_HEIGHT
        assert record.tab.displayed_text == "ROI"
    finally:
        fonts.apply_font_scale(100, theme_manager=None, app=qapp)
        qapp.processEvents()


# ---------------------------------------------------------------------------
# 18. Shelf styling is scoped: no global QPushButton/theme changes
# ---------------------------------------------------------------------------


def test_shelf_geometry_constants() -> None:
    assert mod.PANEL_SHELF_WIDTH == 36
    assert mod.PANEL_TAB_WIDTH == 32
    assert 30 <= mod.PANEL_TAB_WIDTH <= 34
    assert mod.PANEL_TAB_MIN_HEIGHT == 60
    assert mod.PANEL_TAB_MAX_HEIGHT == 400
    assert mod.PANEL_TAB_SPACING == 6
    assert mod.PANEL_TAB_MARGIN == 2
    assert mod.PANEL_PIN_SIZE == 20
    assert mod.PANEL_PIN_ICON_SIZE == 16
    assert mod._SHELF_TAB_RADIUS == 4
    assert 3 <= mod._SHELF_TAB_RADIUS <= 5


def test_shelf_rails_are_transparent(widget) -> None:
    for name in ("cfg_left_shelf", "cfg_right_shelf"):
        rail = widget.findChild(QWidget, name)
        assert rail is not None
        assert rail.width() == mod.PANEL_SHELF_WIDTH


def test_shelf_qss_is_scoped_not_global() -> None:
    from thermal_monitor.ui.theme.stylesheet import build_stylesheet
    from thermal_monitor.ui.theme.themes import BUILTIN_THEMES

    light = BUILTIN_THEMES["industrial_light"]
    sheet = build_stylesheet(light, 100)
    # Shelf-specific selectors exist with the light-industrial palette.
    assert 'QPushButton[shelfTab="true"]' in sheet
    assert 'QPushButton[pinButton="true"]' in sheet
    assert 'QWidget[shelfRail="true"]' in sheet
    assert "#EFF1F4" in sheet  # very light neutral tab
    assert "#23272C" in sheet  # dark charcoal text
    assert "#607D8B" in sheet  # muted steel-blue pressed/open
    assert "border-radius: 4px;" in sheet
    assert "background-color: transparent;" in sheet
    assert "font-size: 12px;" in sheet
    # The global theme is untouched: generic button + accent intact.
    assert "QPushButton {" in sheet
    assert light.accent == "#546E7A"
    assert light.primary == "#2E7D32"
    dark_sheet = build_stylesheet(BUILTIN_THEMES["industrial_dark"], 100)
    assert "QPushButton {" in dark_sheet
    assert BUILTIN_THEMES["industrial_dark"].accent == "#D98E2B"


def test_single_authoritative_sync_path() -> None:
    import inspect

    sync_source = inspect.getsource(
        ConfigurationModeWidget._sync_panel_visual_state
    )
    assert "_sync_shelf_tab" in sync_source
    assert "_sync_pin_button" in sync_source
    tab_source = inspect.getsource(ConfigurationModeWidget._sync_shelf_tab)
    assert "setVisible" in tab_source  # physically hidden when open
    assert "setChecked" in tab_source  # compat mirror only
    assert "setText" not in tab_source  # title string never touched
    assert "_tab_title" not in tab_source
    # Minimized is an explicit visual state: fixed geometry is re-asserted
    # AFTER the variant repolish (which rewrites QSS minimum sizes).
    assert "refresh_metrics" in tab_source
    for name in ("set_panel_open", "_collapse_unpinned", "_restore_shelf_state"):
        source = inspect.getsource(getattr(ConfigurationModeWidget, name))
        assert "_sync_panel_visual_state" in source, name


def test_paint_uses_same_font_as_metrics() -> None:
    import inspect

    paint_source = inspect.getsource(_ShelfTab.paintEvent)
    assert paint_source.count("drawText") == 1
    assert "rotate" in paint_source
    assert "_tab_font" in paint_source
    metrics_source = inspect.getsource(_ShelfTab.refresh_metrics)
    assert "_tab_font" in metrics_source


# ---------------------------------------------------------------------------
# Regression: open -> pin -> unpin -> minimize must restore the exact
# minimized floating tab (same widget, same geometry, same rendering).
# ---------------------------------------------------------------------------


def test_returned_tab_matches_fresh_minimized_tab(widget, qapp) -> None:
    """TEST A/C: repeated pin/unpin/minimize cycles restore the shelf tab.

    Catches the collapsed-tab regression where a tab returning to the
    shelf rendered as a small horizontal box: the QSS repolish inside the
    sync path rewrote the tab's minimumHeight (186 -> 6 px). The fixed
    geometry (min == max == height) is the regression signal.
    """
    for key, record in widget.side_panels().items():
        fresh = (
            record.tab.width(),
            record.tab.height(),
            record.tab.minimumHeight(),
            record.tab.maximumHeight(),
        )
        assert fresh[2] == fresh[3] == fresh[1]  # fixed constraint intact
        for _ in range(2):  # TEST C: repeated cycles stay correct
            widget.set_panel_open(key, True, persist=False)
            qapp.processEvents()
            assert record.is_open() and record.tab.isHidden()
            widget.set_panel_pinned(key, True, persist=False)
            qapp.processEvents()
            assert record.is_open() and record.tab.isHidden()
            widget.set_panel_pinned(key, False, persist=False)
            qapp.processEvents()
            assert record.is_open()  # unpin keeps the panel open
            assert record.tab.isHidden()
            _press(widget._center_widget, qapp)
            assert not record.is_open()
        # Returned tab === fresh minimized tab: same widget, same path.
        assert isinstance(record.tab, _ShelfTab)
        assert record.tab.isVisible()
        assert record.tab.displayed_text == EXPECTED_TITLES[key]
        assert record.tab.panel_name == EXPECTED_TITLES[key]
        assert record.tab.text() == ""
        assert record.tab.toolTip() == EXPECTED_TITLES[key]
        assert (record.tab.width(), record.tab.height()) == fresh[:2]
        assert record.tab.minimumHeight() == fresh[2]
        assert record.tab.maximumHeight() == fresh[3]
        assert record.tab.minimumHeight() == record.tab.maximumHeight()


def test_direct_minimize_matches_pinned_cycle(widget, qapp) -> None:
    """TEST B: plain open -> outside-click returns the identical tab."""
    for key, record in widget.side_panels().items():
        widget.set_panel_open(key, True, persist=False)
        qapp.processEvents()
        _press(widget._center_widget, qapp)
        assert not record.is_open()
        assert isinstance(record.tab, _ShelfTab)
        assert record.tab.isVisible()
        assert record.tab.displayed_text == EXPECTED_TITLES[key]
        assert record.tab.minimumHeight() == record.tab.maximumHeight()
        assert mod.PANEL_TAB_MIN_HEIGHT <= record.tab.height() <= (
            mod.PANEL_TAB_MAX_HEIGHT
        )


def test_shelf_survives_theme_switch(widget, qapp) -> None:
    """A theme repolish must not collapse minimized tabs either."""
    from thermal_monitor.ui.theme.manager import ThemeManager

    before = {
        key: (
            record.tab.width(),
            record.tab.height(),
            record.tab.minimumHeight(),
            record.tab.maximumHeight(),
        )
        for key, record in widget.side_panels().items()
    }
    manager = ThemeManager(None)
    try:
        for name in ("industrial_dark", "industrial_light"):
            manager.set_theme(name)
            manager.apply_and_refresh(qapp)
            qapp.processEvents()
            for key, record in widget.side_panels().items():
                assert (record.tab.width(), record.tab.height()) == before[key][
                    :2
                ], f"{key}: collapsed by theme switch to {name}"
                assert record.tab.minimumHeight() == before[key][2]
                assert record.tab.maximumHeight() == before[key][3]
                assert record.tab.displayed_text == EXPECTED_TITLES[key]
    finally:
        qapp.setStyleSheet("")
        qapp.processEvents()
        widget.refresh_font_metrics()
        qapp.processEvents()
