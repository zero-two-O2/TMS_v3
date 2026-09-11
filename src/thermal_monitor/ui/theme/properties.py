"""
ui.theme.properties -- Semantic styling helpers for widgets.

Widgets style themselves exclusively through these helpers: they declare
*what* a widget is (``variant`` / ``status`` / ``role`` / ``tileState`` /
``dirty``) and the central stylesheet decides *how* it looks.  No widget
should embed literal colors in per-widget style sheets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PyQt6.QtWidgets import QWidget


def repolish(widget: QWidget) -> None:
    """Re-evaluate dynamic-property selectors for *widget*.

    Cheap single-widget operation (unpolish/polish + update).  Safe to
    call on state changes; never touches acquisition, processing, or
    frame data.  Silently ignores already-deleted C++ objects.
    """
    try:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()
    except RuntimeError:
        pass


def set_variant(widget: QWidget, variant: str | None) -> None:
    """Set button emphasis: primary/secondary/accent/danger/outline/ghost."""
    widget.setProperty("variant", variant or "")
    repolish(widget)


def set_status(widget: QWidget, status: str | None) -> None:
    """Set connection/lifecycle/alarm status (see STATUS_VALUES)."""
    widget.setProperty("status", status or "")
    repolish(widget)


def set_role(widget: QWidget, role: str | None) -> None:
    """Set label/chrome role (see LABEL_ROLES, plus 'tile'/'toolbar')."""
    widget.setProperty("role", role or "")
    repolish(widget)


def set_tile_state(widget: QWidget, state: str | None) -> None:
    """Set live-camera tile frame state (see TILE_STATES)."""
    widget.setProperty("tileState", state or "")
    repolish(widget)


def set_dirty(widget: QWidget, dirty: bool) -> None:
    """Mark e.g. a save button as having unsaved changes."""
    widget.setProperty("dirty", bool(dirty))
    repolish(widget)


def refresh_all_widgets(app=None) -> int:
    """Repolish every widget after a theme switch.

    Returns the number of widgets refreshed.  This is a pure GUI-style
    operation: it recreates no widgets, restarts no workers, and does
    not touch acquisition, SHM, recording, or frame timing.
    """
    if app is None:
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
    if app is None:
        return 0
    count = 0
    try:
        widgets = list(app.allWidgets())
    except RuntimeError:
        return 0
    for widget in widgets:
        try:
            style = widget.style()
            style.unpolish(widget)
            style.polish(widget)
            count += 1
        except RuntimeError:
            continue
    return count


__all__ = [
    "repolish",
    "refresh_all_widgets",
    "set_dirty",
    "set_role",
    "set_status",
    "set_tile_state",
    "set_variant",
]
