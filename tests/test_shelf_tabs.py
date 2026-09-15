"""
Side-shelf tab name regression (ThermoView-style tabs).

Guards the "full name disappears after click" regression: the exact same
complete panel name must render in EVERY state (collapsed / open /
pinned / unpinned / scrolled). Panel state may only affect visual state
(background/border/text-color via variant + checked), never the label
string. There is no eliding, no 3-letter abbreviation, no per-character
stacking — one rotated string from a single canonical title property.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QScrollArea, QWidget

import thermal_monitor.camera.source  # noqa: F401  (init camera package first)
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
    "temp_scale": "Temperature Scale",
    "view_finder": "View Finder",
    "roi": "ROI",
    "alarms": "Alarms",
    "statistics": "Statistics",
    "config_editor": "Configuration Editor",  # only when a config manager exists
}

BANNED_STANDALONE = (
    "Can",
    "Tem",
    "Vie",
    "Ala",
    "Sta",
    "Con",
    "Cam",
    "Ima",
)


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(autouse=True)
def isolated_shelf_settings(monkeypatch):
    monkeypatch.setattr(mod, "_DOCK_SETTINGS_ORG", "TMS-Test-Org")
    monkeypatch.setattr(mod, "_DOCK_SETTINGS_APP", "TMS-Test-ShelfTabs")
    from PyQt6.QtCore import QSettings

    QSettings("TMS-Test-Org", "TMS-Test-ShelfTabs").clear()
    yield
    QSettings("TMS-Test-Org", "TMS-Test-ShelfTabs").clear()


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


def _assert_full_name(record, expected: str | None = None) -> None:
    tab = record.tab
    assert tab is not None
    canonical = record.title
    if expected is not None:
        assert canonical == expected
    # Single canonical source renders in every state.
    assert tab._tab_title == canonical
    assert tab.panel_name == canonical
    assert tab.full_name == canonical
    assert tab.displayed_text == canonical
    assert tab.toolTip() == canonical
    # Never a standalone 3-letter stub and never "..." eliding.
    assert tab.displayed_text not in BANNED_STANDALONE, (
        f"{record.key}: abbreviated to {tab.displayed_text!r}"
    )
    assert not tab.displayed_text.endswith("..."), f"{record.key}: elided"
    assert "..." not in tab.displayed_text, f"{record.key}: elided"
    # QPushButton text stays empty (bevel only); the rotated paint path
    # owns the label so QSS/state can never elide it.
    assert tab.text() == ""


def test_canonical_titles_match_expected(widget) -> None:
    panels = widget.side_panels()
    assert set(EXPECTED_TITLES) >= set(panels)
    for key, record in panels.items():
        _assert_full_name(record, EXPECTED_TITLES[key])


def test_names_survive_open_close_select_deselect(widget, qapp) -> None:
    for key in ("camera_control", "temp_scale", "view_finder"):
        record = widget.side_panels()[key]
        before = record.tab.panel_name
        widget.set_panel_open(key, True)
        qapp.processEvents()
        assert record.tab.isChecked()
        _assert_full_name(record, before)
        widget.set_panel_open(key, False)
        qapp.processEvents()
        assert not record.tab.isChecked()
        _assert_full_name(record, before)
        # Click path (toggle via the real tab button).
        record.tab.click()
        qapp.processEvents()
        assert record.is_open()
        _assert_full_name(record, before)
        record.tab.click()
        qapp.processEvents()
        assert not record.is_open()
        _assert_full_name(record, before)


def test_names_survive_pin_unpin(widget, qapp) -> None:
    widget.set_panel_open("camera_control", True)
    widget.set_panel_pinned("camera_control", True)
    qapp.processEvents()
    _assert_full_name(widget.side_panels()["camera_control"], "Camera Control")
    widget.set_panel_pinned("camera_control", False)
    qapp.processEvents()
    _assert_full_name(widget.side_panels()["camera_control"], "Camera Control")
    # Header pin-button clicks also preserve the tab name.
    record = widget.side_panels()["camera_control"]
    record.pin_button.click()
    qapp.processEvents()
    assert record.pinned
    _assert_full_name(record, "Camera Control")
    record.pin_button.click()
    qapp.processEvents()
    assert not record.pinned
    _assert_full_name(record, "Camera Control")


def test_names_survive_many_open_and_shelf_scroll(widget, qapp) -> None:
    for key in widget.side_panels():
        widget.set_panel_open(key, True, persist=False)
    qapp.processEvents()
    for key, record in widget.side_panels().items():
        _assert_full_name(record)
    # Outer shelf scroll containers exist and scroll the TAB LIST.
    for scroll_name in ("cfg_left_shelf_scroll", "cfg_right_shelf_scroll"):
        scroll = widget.findChild(QScrollArea, scroll_name)
        assert scroll is not None, f"missing outer shelf scroll: {scroll_name}"
        assert scroll.widgetResizable()
        assert (
            scroll.horizontalScrollBarPolicy()
            == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        assert (
            scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        host = scroll.widget()
        assert host is not None
        tabs = host.findChildren(_ShelfTab)
        assert tabs != []
    # Scrolling the shelf must not alter any label.
    right_scroll = widget.findChild(QScrollArea, "cfg_right_shelf_scroll")
    bar = right_scroll.verticalScrollBar()
    if bar is not None and bar.maximum() > bar.minimum():
        bar.setValue(bar.maximum())
        qapp.processEvents()
        bar.setValue(bar.minimum())
        qapp.processEvents()
    for key, record in widget.side_panels().items():
        _assert_full_name(record)


def test_tab_geometry_fits_full_title(widget) -> None:
    from PyQt6.QtGui import QFontMetrics

    assert mod.PANEL_SHELF_WIDTH == 40
    assert mod.PANEL_TAB_WIDTH == 36
    assert mod.PANEL_TAB_MIN_HEIGHT == 60
    assert mod.PANEL_TAB_MAX_HEIGHT == 400
    for name in ("cfg_left_shelf", "cfg_right_shelf"):
        rail = widget.findChild(QWidget, name)
        assert rail is not None
        assert rail.width() == mod.PANEL_SHELF_WIDTH
    for key, record in widget.side_panels().items():
        tab = record.tab
        assert tab.width() == mod.PANEL_TAB_WIDTH
        assert mod.PANEL_TAB_MIN_HEIGHT <= tab.height() <= mod.PANEL_TAB_MAX_HEIGHT
        advance = QFontMetrics(_ShelfTab._tab_font()).horizontalAdvance(
            record.title
        )
        # Height reservation covers the full advance (+ 2 * margin) unless
        # clamped by MAX (only at very large font scales); it never clips.
        assert tab.height() - 2 * _ShelfTab._TEXT_MARGIN >= advance - 1 or (
            tab.height() == mod.PANEL_TAB_MAX_HEIGHT
        )


def test_paint_uses_single_rotated_string_no_eliding() -> None:
    import inspect

    source = inspect.getsource(_ShelfTab.paintEvent)
    assert "rotate" in source
    assert "drawText" in source
    assert source.count("drawText") == 1  # one rotated string, not per-character
    assert "displayed_text" in source or "_tab_title" in source
    module_source = open(mod.__file__, encoding="utf-8").read()
    assert "elidedText" not in module_source
    assert "QFontMetrics.elidedText" not in module_source
    # State sync touches only checked/variant, never the label string.
    sync_source = inspect.getsource(
        ConfigurationModeWidget._sync_shelf_tab
    )
    assert "setChecked" in sync_source
    assert "setText" not in sync_source
    assert "_tab_title" not in sync_source
