"""
ui.windows.alarm_detail_window -- Read-only active-alarm detail dialog (Phase 9B).

Shows exactly the fields available from the existing alarm domain model
(:class:`AlarmEvent` + :class:`AlarmRule` + station context). Nothing is
fabricated: unavailable context renders as "—".

Lifecycle: plain QDialog, no threads, no workers. The caller passes a
snapshot; closing the dialog leaks nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QVBoxLayout,
)


def _fmt_time(timestamp: float | None) -> str:
    if not timestamp:
        return "—"
    try:
        return datetime.fromtimestamp(float(timestamp)).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "—"


class AlarmDetailWindow(QDialog):
    """Read-only detail view for one active alarm."""

    def __init__(
        self,
        event,
        rule=None,
        camera_id: str = "",
        ptz_id: str = "",
        position_id: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Alarm Details — {getattr(event, 'event_id', 'alarm')}")
        self.setMinimumWidth(380)
        self._build(event, rule, camera_id, ptz_id, position_id)

    def _build(self, event, rule, camera_id: str, ptz_id: str, position_id: str) -> None:
        layout = QVBoxLayout(self)

        group = QGroupBox("ALARM DETAILS")
        form = QFormLayout(group)

        alarm_type = getattr(rule, "condition", None)
        alarm_type = alarm_type.value if alarm_type is not None else getattr(event, "rule_id", "—")
        severity = getattr(event, "severity", None)
        severity = severity.value.upper() if severity is not None else "—"
        meta = dict(getattr(event, "metadata", None) or {})
        status = str(meta.get("status") or "ACTIVE")

        def row(label: str, value: object) -> None:
            text = str(value) if value not in (None, "") else "—"
            field = QLabel(text)
            field.setTextInteractionFlags(
                field.textInteractionFlags() | Qt.TextInteractionFlag.TextSelectableByMouse
            )
            form.addRow(label, field)

        row("Camera:", camera_id or getattr(event, "camera_id", "") or "—")
        row("PTZ:", ptz_id or meta.get("ptz_id", "") or "—")
        row("Position:", position_id or getattr(event, "position_id", "") or "—")
        row("ROI:", getattr(event, "roi_id", "") or "—")
        row("Alarm:", alarm_type)
        row("Severity:", severity)
        row("Current:", f"{float(getattr(event, 'measured_value', 0.0) or 0.0):.1f} °C")
        row("Threshold:", f"{float(getattr(event, 'threshold_value', 0.0) or 0.0):.1f} °C")
        row("Triggered:", _fmt_time(getattr(event, "timestamp", None)))
        row("Status:", status)
        ack = "Yes" if getattr(event, "acknowledged", False) else "No"
        by = getattr(event, "acknowledged_by", None)
        row("Acknowledged:", f"{ack}" + (f" ({by})" if by else ""))
        if rule is not None and getattr(rule, "description", ""):
            row("Description:", rule.description)
        elif meta.get("description"):
            row("Description:", meta["description"])

        layout.addWidget(group)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


__all__ = ["AlarmDetailWindow"]
