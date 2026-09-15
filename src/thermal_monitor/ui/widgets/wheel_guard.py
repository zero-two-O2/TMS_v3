"""
ui.widgets.wheel_guard -- Mouse-wheel locking for value editors in panels.

Usability rule: scrolling a side panel must NEVER accidentally change a
configuration value. QSpinBox / QDoubleSpinBox / QComboBox (and sliders)
consume wheel events by default, so a scroll gesture over an unfocused
editor silently rewrites its value.

This module installs a tiny event filter on such editors inside scrollable
panel content:

- Editor WITHOUT keyboard focus  -> wheel is re-targeted at the enclosing
  QScrollArea viewport, so the panel scrolls instead.
- Editor WITH keyboard focus (the user explicitly clicked into it) ->
  normal wheel behavior (value changes), keyboard editing untouched.

Arrow buttons, text entry, and focus visuals (central ``:focus`` QSS) are
unaffected. No global application filter, no per-frame work, no layout
changes — the filter only acts on wheel events over guarded editors.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QEvent, QPoint, QPointF
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractScrollArea,
    QAbstractSlider,
    QAbstractSpinBox,
    QComboBox,
    QScrollArea,
    QWidget,
)

#: Editor types whose wheel input is locked until explicitly focused.
_GUARDED_TYPES = (QAbstractSpinBox, QComboBox, QAbstractSlider)


def enclosing_scroll_area(widget: QWidget) -> QScrollArea | None:
    """Nearest enclosing QScrollArea viewport host, if any."""
    candidate = widget.parentWidget()
    while candidate is not None:
        if isinstance(candidate, QScrollArea):
            return candidate
        # A QAbstractScrollArea viewport's parent chain also leads here;
        # either way the first QScrollArea ancestor owns the scroll.
        if isinstance(candidate, QAbstractScrollArea):
            return candidate  # type: ignore[return-value]
        candidate = candidate.parentWidget()
    return None


class WheelForwardFilter(QObject):
    """Re-target wheel events from unfocused editors to their scroll area."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt override)
        if event.type() == QEvent.Type.ChildAdded:
            # Lazily built editors (e.g. config-editor fields created on
            # navigation) are guarded as soon as they appear.
            try:
                child = event.child()
            except Exception:
                return False
            if isinstance(child, QWidget):
                self._guard_subtree(child)
            return False
        if event.type() != QEvent.Type.Wheel:
            return False
        if not isinstance(watched, QWidget):
            return False
        if watched.hasFocus():
            return False  # explicit editing state: keep native behavior
        scroll = enclosing_scroll_area(watched)
        if scroll is None:
            return False
        if not isinstance(event, QWheelEvent):
            return False
        try:
            viewport = scroll.viewport()
            global_pos = watched.mapToGlobal(event.position().toPoint())
            local_pos = viewport.mapFromGlobal(global_pos)
            forwarded = QWheelEvent(
                QPointF(local_pos),
                QPointF(global_pos),
                event.pixelDelta(),
                event.angleDelta(),
                event.buttons(),
                event.modifiers(),
                event.phase(),
                False,
            )
        except Exception:
            return False
        QApplication.sendEvent(viewport, forwarded)
        return True  # consumed: the editor value is untouched

    def _guard_subtree(self, node: QWidget) -> None:
        """Install this filter on every guarded editor under ``node``."""
        if isinstance(node, _GUARDED_TYPES):
            try:
                node.installEventFilter(self)
            except Exception:
                pass
        for guarded_type in _GUARDED_TYPES:
            try:
                children = node.findChildren(guarded_type)
            except Exception:
                continue
            for child in children:
                try:
                    child.installEventFilter(self)
                except Exception:
                    continue


def install_wheel_guards(root: QWidget) -> WheelForwardFilter:
    """Guard every spin/combo/slider under ``root`` (one shared filter).

    The filter is parented to ``root`` so its lifetime matches the panel
    content, and watches ``root`` for lazily added children. Idempotent
    per widget (Qt ignores duplicate installs). Returns the filter for
    tests/introspection.
    """
    existing = getattr(root, "_wheel_guard_filter", None)
    if isinstance(existing, WheelForwardFilter):
        filt = existing
    else:
        filt = WheelForwardFilter(root)
        try:
            root._wheel_guard_filter = filt  # type: ignore[attr-defined]
        except Exception:
            pass
    try:
        root.installEventFilter(filt)
    except Exception:
        pass
    filt._guard_subtree(root)
    return filt


__all__ = [
    "WheelForwardFilter",
    "enclosing_scroll_area",
    "install_wheel_guards",
]
