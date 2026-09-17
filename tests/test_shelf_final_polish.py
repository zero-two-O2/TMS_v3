"""
Final shelf tab size/polish tuning (narrow tab + larger readable text).

Locks the visual tuning pass:

    OLD shelf font: 10 pt -> NEW: 12 pt (shelf-tab font only)
    OLD tab width:  36 px -> NEW: 32 px (rail 40 px -> 36 px)

Height stays dynamically derived (text advance + padding), never
hardcoded. The StyleChange/repolish geometry protection from the
previous fix must remain the final authority.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QFontMetrics, QMouseEvent
from PyQt6.QtWidgets import QApplication, QWidget

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

ALL_TITLES = (
    "Camera Control",
    "Image Information",
    "PTZ Control",
    "Temperature Scale",
    "View Finder",
    "ROI",
    "Alarms",
    "Statistics",
    "Position Table",
    "Configuration Editor",
)

EXPECTED_BY_KEY = {
    "camera_control": "Camera Control",
    "image_info": "Image Information",
    "ptz_control": "PTZ Control",
    "temp_scale": "Temperature Scale",
    "view_finder": "View Finder",
    "roi": "ROI",
    "alarms": "Alarms",
    "statistics": "Statistics",
    "ptz_positions": "Position Table",
    "config_editor": "Configuration Editor",
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
    monkeypatch.setattr(mod, "_DOCK_SETTINGS_APP", "TMS-Test-ShelfPolish")
    monkeypatch.setattr(fonts, "FONT_SETTINGS_ORG", "TMS-Test-Org")
    monkeypatch.setattr(fonts, "FONT_SETTINGS_APP", "TMS-Test-ShelfPolishFonts")
    from PyQt6.QtCore import QSettings

    QSettings("TMS-Test-Org", "TMS-Test-ShelfPolish").clear()
    QSettings("TMS-Test-Org", "TMS-Test-ShelfPolishFonts").clear()
    yield
    QSettings("TMS-Test-Org", "TMS-Test-ShelfPolish").clear()
    QSettings("TMS-Test-Org", "TMS-Test-ShelfPolishFonts").clear()


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
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(5, 5),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(target, event)
    qapp.processEvents()


def _expected_height(title: str) -> int:
    advance = QFontMetrics(_ShelfTab._tab_font()).horizontalAdvance(title)
    return min(
        mod.PANEL_TAB_MAX_HEIGHT,
        max(mod.PANEL_TAB_MIN_HEIGHT, advance + 2 * _ShelfTab._TEXT_MARGIN),
    )


# 1. Shelf font increased by ~2 px (shelf-tab font only).
def test_shelf_font_increased_by_2px() -> None:
    assert fonts.SHELF_TAB_FONT_PT == 12  # old value was 10
    assert mod.PANEL_TAB_FONT_SIZE_PT == 12
    assert mod.PANEL_TAB_FONT_SIZE_PT == fonts.SHELF_TAB_FONT_PT
    # Global/header type untouched: panel headers stay at 10 pt.
    assert fonts.PANEL_TITLE_FONT_PT == 10
    # Authoritative pixel size at 100% scale is 12 px (+2 px).
    assert fonts.scaled_font_px(fonts.SHELF_TAB_FONT_PT, 100) == 12
    assert _ShelfTab._tab_font().pixelSize() == 12


# 2. Shelf width reduced to the new target range.
def test_shelf_width_reduced_to_compact_range(widget) -> None:
    assert mod.PANEL_TAB_WIDTH == 32  # old value was 36
    assert 30 <= mod.PANEL_TAB_WIDTH <= 34
    assert mod.PANEL_SHELF_WIDTH == 36  # old value was 40
    assert mod.PANEL_SHELF_WIDTH == mod.PANEL_TAB_WIDTH + 2 * mod.PANEL_TAB_MARGIN
    for name in ("cfg_left_shelf", "cfg_right_shelf"):
        rail = widget.findChild(QWidget, name)
        assert rail is not None
        assert rail.width() == mod.PANEL_SHELF_WIDTH
    for record in widget.side_panels().values():
        assert record.tab.width() == 32
        assert record.tab.minimumWidth() == record.tab.maximumWidth() == 32


# 3. Height still dynamically calculated from the font metrics.
def test_height_dynamically_calculated_from_font(qapp) -> None:
    import inspect

    for title in ALL_TITLES:
        tab = _ShelfTab(title)
        try:
            assert tab.height() == _expected_height(title)
        finally:
            tab.close()
    # Single authoritative font for measure + paint (no divergent fonts).
    assert "_tab_font" in inspect.getsource(_ShelfTab.refresh_metrics)
    assert "_tab_font" in inspect.getsource(_ShelfTab.paintEvent)
    assert inspect.getsource(_ShelfTab.paintEvent).count("drawText") == 1


# 4. Longest panel title is not clipped.
def test_longest_title_not_clipped(qapp) -> None:
    for title in ("Configuration Editor", "Temperature Scale", "Image Information"):
        tab = _ShelfTab(title)
        try:
            advance = QFontMetrics(_ShelfTab._tab_font()).horizontalAdvance(title)
            assert (
                tab.height() - 2 * _ShelfTab._TEXT_MARGIN >= advance - 1
                or tab.height() == mod.PANEL_TAB_MAX_HEIGHT
            ), f"{title!r} clipped"
        finally:
            tab.close()


# 5. Short titles do not receive excessive unnecessary height.
def test_short_titles_not_excessively_tall(qapp) -> None:
    roi = _ShelfTab("ROI")
    alarms = _ShelfTab("Alarms")
    long_tab = _ShelfTab("Configuration Editor")
    try:
        # Exact dynamic formula: max(MIN, advance + padding), never a fixed height.
        assert roi.height() == _expected_height("ROI")
        assert alarms.height() == _expected_height("Alarms")
        assert roi.height() <= alarms.height() <= long_tab.height()
        assert roi.height() >= mod.PANEL_TAB_MIN_HEIGHT
        assert long_tab.height() > roi.height()
    finally:
        roi.close()
        alarms.close()
        long_tab.close()


# 6. Startup minimized tabs are correct.
def test_startup_minimized_tabs_correct(widget) -> None:
    for key, record in widget.side_panels().items():
        assert not record.is_open()
        assert not record.pinned
        assert record.tab.isVisible()
        assert record.tab.width() == 32
        assert record.tab.height() == _expected_height(record.title)
        assert mod.PANEL_TAB_MIN_HEIGHT <= record.tab.height() <= mod.PANEL_TAB_MAX_HEIGHT
        assert record.tab.displayed_text == record.title


# 7. Open -> pin -> unpin -> minimize returns the same correctly-sized tab.
def test_pin_cycle_returns_same_correctly_sized_tab(widget, qapp) -> None:
    for key, record in widget.side_panels().items():
        tab_before = record.tab
        fresh = (
            tab_before.width(),
            tab_before.height(),
            tab_before.minimumHeight(),
            tab_before.maximumHeight(),
        )
        assert fresh[0] == 32
        widget.set_panel_open(key, True, persist=False)
        qapp.processEvents()
        assert record.tab.isHidden()
        widget.set_panel_pinned(key, True, persist=False)
        qapp.processEvents()
        assert record.is_open() and record.tab.isHidden()
        widget.set_panel_pinned(key, False, persist=False)
        qapp.processEvents()
        assert record.is_open() and record.tab.isHidden()
        _press(widget._center_widget, qapp)
        assert not record.is_open()
        assert record.tab is tab_before  # SAME instance returns
        assert record.tab.isVisible()
        assert (record.tab.width(), record.tab.height()) == fresh[:2]
        assert record.tab.minimumHeight() == record.tab.maximumHeight() == fresh[1]
        assert record.tab.width() == 32
        assert record.tab.displayed_text == EXPECTED_BY_KEY[key]


# 8. Theme switching does not collapse the tab geometry.
def test_theme_switch_preserves_geometry(widget, qapp) -> None:
    import inspect

    from thermal_monitor.ui.theme.manager import ThemeManager

    # Previous-fix protection must remain: refresh after polish,
    # StyleChange hook, and sync-path ordering.
    assert "refresh_metrics" in inspect.getsource(
        ConfigurationModeWidget._sync_shelf_tab
    )
    assert "set_variant" in inspect.getsource(ConfigurationModeWidget._sync_shelf_tab)
    assert "StyleChange" in inspect.getsource(_ShelfTab.changeEvent)
    assert "refresh_metrics" in inspect.getsource(_ShelfTab.changeEvent)

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
                assert (record.tab.width(), record.tab.height()) == before[key][:2]
                assert record.tab.minimumHeight() == before[key][2]
                assert record.tab.maximumHeight() == before[key][3]
                assert record.tab.width() == 32
    finally:
        qapp.setStyleSheet("")
        qapp.processEvents()
        widget.refresh_font_metrics()
        qapp.processEvents()


# 9. All shelf tabs remain fully readable (full names, rotated, no clipping).
def test_all_shelf_tabs_fully_readable(widget) -> None:
    import inspect

    for title in ALL_TITLES:
        tab = _ShelfTab(title)
        try:
            assert tab.displayed_text == title
            assert tab.panel_name == title
            assert tab.full_name == title
            assert tab.text() == ""  # rotated paint path owns the label
            assert not tab.displayed_text.endswith("...")
            assert "..." not in tab.displayed_text
        finally:
            tab.close()
    for key, record in widget.side_panels().items():
        assert record.tab.displayed_text == EXPECTED_BY_KEY[key]
        assert record.tab.toolTip() == EXPECTED_BY_KEY[key]
        advance = QFontMetrics(_ShelfTab._tab_font()).horizontalAdvance(record.title)
        assert (
            record.tab.height() - 2 * _ShelfTab._TEXT_MARGIN >= advance - 1
            or record.tab.height() == mod.PANEL_TAB_MAX_HEIGHT
        )
    paint_source = inspect.getsource(_ShelfTab.paintEvent)
    assert "rotate" in paint_source
    assert paint_source.count("drawText") == 1
