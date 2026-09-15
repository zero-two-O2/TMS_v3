"""
Wheel-locking for value editors inside side panels.

Scrolling a panel must NEVER accidentally change a configuration value:
wheel over an UNFOCUSED spin/combo scrolls the enclosing panel instead,
while an explicitly FOCUSED editor keeps native wheel editing.
"""

from __future__ import annotations

import pytest

from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from thermal_monitor.ui.widgets.wheel_guard import (
    WheelForwardFilter,
    enclosing_scroll_area,
    install_wheel_guards,
)


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _wheel(target: QWidget, delta: int = 120) -> QWheelEvent:
    """A vertical wheel event centered on ``target``."""
    center = QPointF(target.rect().center())
    return QWheelEvent(
        center,
        QPointF(target.mapToGlobal(center.toPoint())),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


@pytest.fixture
def scrolled_panel(qapp):
    """Tall content (with spin/double/combo) inside a small scroll area."""
    host = QWidget()
    host.resize(300, 200)
    outer = QVBoxLayout(host)
    scroll = QScrollArea()
    scroll.setFixedSize(280, 120)
    content = QWidget()
    layout = QVBoxLayout(content)
    layout.setContentsMargins(0, 0, 0, 0)
    spin = QSpinBox()
    spin.setRange(0, 100)
    spin.setValue(50)
    spin.setFixedHeight(60)
    double = QDoubleSpinBox()
    double.setRange(0.0, 100.0)
    double.setValue(25.0)
    double.setFixedHeight(60)
    combo = QComboBox()
    combo.addItems(["a", "b", "c"])
    combo.setFixedHeight(60)
    filler = QWidget()
    filler.setFixedHeight(300)  # force a real scrollbar
    for child in (spin, double, combo, filler):
        layout.addWidget(child)
    scroll.setWidget(content)
    outer.addWidget(scroll)
    host.show()
    qapp.processEvents()
    install_wheel_guards(content)
    yield host, scroll, content, spin, double, combo
    host.close()


def test_unfocused_spin_wheel_does_not_change_value(scrolled_panel, qapp) -> None:
    _, _, _, spin, _, _ = scrolled_panel
    assert not spin.hasFocus()
    QApplication.sendEvent(spin, _wheel(spin))
    qapp.processEvents()
    assert spin.value() == 50


def test_focused_spin_wheel_edits_value(scrolled_panel, qapp) -> None:
    _, _, _, spin, _, _ = scrolled_panel
    spin.setFocus()
    qapp.processEvents()
    assert spin.hasFocus()
    QApplication.sendEvent(spin, _wheel(spin))
    qapp.processEvents()
    assert spin.value() != 50


def test_unfocused_double_spin_locked(scrolled_panel, qapp) -> None:
    _, _, _, _, double, _ = scrolled_panel
    assert not double.hasFocus()
    QApplication.sendEvent(double, _wheel(double))
    qapp.processEvents()
    assert double.value() == 25.0


def test_unfocused_combo_wheel_does_not_change_selection(
    scrolled_panel, qapp
) -> None:
    _, _, _, _, _, combo = scrolled_panel
    assert not combo.hasFocus()
    QApplication.sendEvent(combo, _wheel(combo))
    qapp.processEvents()
    assert combo.currentIndex() == 0


def test_filter_contract_focused_vs_unfocused(scrolled_panel, qapp) -> None:
    """The filter consumes wheel only for UNFOCUSED editors."""
    _, _, content, spin, _, combo = scrolled_panel
    filt = getattr(content, "_wheel_guard_filter")
    assert isinstance(filt, WheelForwardFilter)
    # Unfocused: consumed (value protected, panel scrolls instead).
    assert filt.eventFilter(spin, _wheel(spin)) is True
    assert filt.eventFilter(combo, _wheel(combo)) is True
    # Explicitly focused: native behavior preserved.
    spin.setFocus()
    qapp.processEvents()
    assert spin.hasFocus()
    assert filt.eventFilter(spin, _wheel(spin)) is False
    combo.setFocus()
    qapp.processEvents()
    assert combo.hasFocus()
    assert filt.eventFilter(combo, _wheel(combo)) is False
    spin.clearFocus()
    combo.clearFocus()
    qapp.processEvents()


def test_wheel_scrolls_enclosing_panel(scrolled_panel, qapp) -> None:
    _, scroll, _, spin, _, _ = scrolled_panel
    before = scroll.verticalScrollBar().value()
    assert scroll.verticalScrollBar().maximum() > 0
    QApplication.sendEvent(spin, _wheel(spin, delta=-240))
    qapp.processEvents()
    assert scroll.verticalScrollBar().value() != before


def test_keyboard_and_arrows_still_work(scrolled_panel, qapp) -> None:
    _, _, _, spin, _, _ = scrolled_panel
    spin.setFocus()
    qapp.processEvents()
    spin.stepBy(1)
    assert spin.value() == 51
    spin.setValue(42)
    assert spin.value() == 42


def test_lazily_added_editor_is_guarded(scrolled_panel, qapp) -> None:
    _, _, content, _, _, _ = scrolled_panel
    late = QSpinBox()
    late.setRange(0, 100)
    late.setValue(7)
    content.layout().addWidget(late)
    qapp.processEvents()
    assert not late.hasFocus()
    QApplication.sendEvent(late, _wheel(late))
    qapp.processEvents()
    assert late.value() == 7


def test_enclosing_scroll_area_lookup(scrolled_panel) -> None:
    _, scroll, content, spin, _, _ = scrolled_panel
    assert enclosing_scroll_area(spin) is scroll
    assert enclosing_scroll_area(content) is scroll
    orphan = QWidget()
    try:
        assert enclosing_scroll_area(orphan) is None
    finally:
        orphan.close()


def test_filter_is_shared_and_parented(scrolled_panel) -> None:
    _, _, content, _, _, _ = scrolled_panel
    filt = install_wheel_guards(content)
    assert isinstance(filt, WheelForwardFilter)
    assert install_wheel_guards(content) is filt
