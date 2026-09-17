"""
ui.windows.alarm_history_window -- Alarm-history table dialog (Phase 9B).

Loads a bounded number of rows (default 200, newest first) off the GUI
thread via a daemon worker; results return through a queued signal
carrying the dialog generation so stale loads after close/refresh can
never touch a dead table. Sorting by timestamp is newest-first;
filtering/pagination beyond the limit stays out of scope until the
history genuinely grows (the repository caps rows server-side).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

logger = logging.getLogger(__name__)

_COLUMNS = ("Time", "Camera", "PTZ", "Position", "ROI", "Alarm", "Temp", "Threshold", "Status")

_DEFAULT_LIMIT = 200


def _fmt_time(timestamp: float | None) -> str:
    if not timestamp:
        return "—"
    try:
        return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "—"


class AlarmHistoryWindow(QDialog):
    """Paginated (bounded) read-only alarm-history view."""

    _loaded = pyqtSignal(int, object)  # generation, list[AlarmEvent]

    def __init__(self, database=None, limit: int = _DEFAULT_LIMIT, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Alarm History")
        self.setMinimumSize(860, 420)
        self._database = database
        self._limit = max(1, int(limit))
        self._generation = 0
        self._status_label = QLabel("Loading…")
        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSortingEnabled(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.verticalHeader().setVisible(False)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(self._status_label)
        top.addStretch(1)
        layout.addLayout(top)
        layout.addWidget(self._table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._loaded.connect(self._on_loaded)
        self.refresh()

    # -- loading (worker thread, generation-guarded) -----------------------

    def refresh(self) -> None:
        self._generation += 1
        generation = self._generation
        database = self._database
        limit = self._limit
        self._status_label.setText("Loading…")
        if database is None:
            self._loaded.emit(generation, [])
            return

        def _load() -> None:
            try:
                from thermal_monitor.storage.repositories.sqlite_alarm import (
                    SqliteAlarmEventRepository,
                )

                repo = SqliteAlarmEventRepository(database)
                result = repo.find_recent(limit)
                events = list(result.data) if result.success and result.data else []
                if not result.success:
                    logger.warning("Alarm history load failed: %s", result.error)
            except Exception as exc:
                logger.warning("Alarm history load failed: %s", exc)
                events = []
            try:
                self._loaded.emit(generation, events)
            except RuntimeError:
                pass  # dialog already destroyed

        threading.Thread(target=_load, name="AlarmHistoryLoad", daemon=True).start()

    @pyqtSlot(int, object)
    def _on_loaded(self, generation: int, events: object) -> None:
        if generation != self._generation:
            return  # stale load (refresh superseded)
        rows = list(events or [])
        self._table.setRowCount(len(rows))
        for r, event in enumerate(rows):
            meta = dict(getattr(event, "metadata", None) or {})
            severity = getattr(event, "severity", None)
            values = (
                _fmt_time(getattr(event, "timestamp", None)),
                getattr(event, "camera_id", "") or "—",
                meta.get("ptz_id", "") or "—",
                getattr(event, "position_id", None) or "—",
                getattr(event, "roi_id", "") or "—",
                getattr(event, "rule_id", "") or "—",
                f"{float(getattr(event, 'measured_value', 0.0) or 0.0):.1f}",
                f"{float(getattr(event, 'threshold_value', 0.0) or 0.0):.1f}",
                str(meta.get("status") or ("INFO" if severity is not None and severity.value == "info" else "ACTIVE")),
            )
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(r, c, item)
        self._table.resizeColumnsToContents()
        if self._database is None:
            self._status_label.setText("History unavailable (no database).")
        else:
            self._status_label.setText(f"{len(rows)} event(s), newest first (limit {self._limit}).")

    def closeEvent(self, event) -> None:
        self._generation += 1  # drop any in-flight load
        super().closeEvent(event)


__all__ = ["AlarmHistoryWindow"]
