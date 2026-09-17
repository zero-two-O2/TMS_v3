"""
storage.alarm_store -- Threaded alarm-history persistence (Phase 9B).

Lifecycle model (edge-triggered, never one row per frame):

TRIGGERED -> ACTIVE -> CLEARED

``record_evaluation`` consumes :class:`AlarmEvaluationResult` objects
from the alarm evaluator. Only transition events produce writes:

* a new ``alarm_*`` event -> one INSERT with status ACTIVE
* a ``clear_*`` event   -> UPDATE the matching rule's open row(s) to
  CLEARED (no new history row per frame while active)

All database I/O runs on a single dedicated daemon worker thread that
owns its SQLite connection. GUI/event threads only enqueue. Failures
are recorded in ``last_error`` and exposed via ``status()`` — they
never raise into acquisition or the GUI, and acquisition continues.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field

from thermal_monitor.core.models import AlarmEvent

logger = logging.getLogger(__name__)


@dataclass
class AlarmStoreStats:
    persisted: int = 0
    cleared: int = 0
    dropped: int = 0
    errors: int = 0
    last_error: str = ""
    last_write_at: float = 0.0


class AlarmHistoryStore:
    """Owns alarm-history writes behind a queue + worker thread."""

    def __init__(self, database, max_queue: int = 2000) -> None:
        self._db = database
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._stats = AlarmStoreStats()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._open: dict[str, str] = {}  # rule_id -> event_id (ACTIVE row)
        self._thread = threading.Thread(
            target=self._run, name="AlarmHistoryStore", daemon=True
        )

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if not self._thread.is_alive():
            try:
                self._thread.start()
            except RuntimeError:
                pass  # already started

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=timeout_s)
            if self._thread.is_alive():
                logger.warning("AlarmHistoryStore worker still running after %.1fs", timeout_s)
        try:
            self._db.disconnect()
        except Exception:
            pass

    # -- producer API (any thread, never blocks on I/O) -------------------

    def record_event(self, event: AlarmEvent) -> None:
        """Enqueue a single evaluator event (trigger or clear)."""
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._lock:
                self._stats.dropped += 1
            logger.warning("Alarm history queue full; dropping event %s", event.event_id)

    def record_evaluation(self, evaluation) -> None:
        """Enqueue every event from an AlarmEvaluationResult (edge feed)."""
        for event in getattr(evaluation, "events", ()) or ():
            self.record_event(event)

    def stats_snapshot(self) -> AlarmStoreStats:
        with self._lock:
            return AlarmStoreStats(
                persisted=self._stats.persisted,
                cleared=self._stats.cleared,
                dropped=self._stats.dropped,
                errors=self._stats.errors,
                last_error=self._stats.last_error,
                last_write_at=self._stats.last_write_at,
            )

    def status(self) -> tuple[str, str]:
        """(state, detail) for status UI: ok | degraded | unavailable."""
        with self._lock:
            if self._stats.errors and not self._stats.persisted:
                return "unavailable", self._stats.last_error
            if self._stats.last_error:
                return "degraded", self._stats.last_error
        try:
            state, detail = self._db.status
        except Exception:
            return "ok", ""
        if state == "connected":
            return "ok", ""
        if state == "error":
            return "unavailable", detail
        return "ok", detail

    # -- worker ------------------------------------------------------------

    def _run(self) -> None:
        from thermal_monitor.storage.repositories.sqlite_alarm import (
            SqliteAlarmEventRepository,
        )

        repo = SqliteAlarmEventRepository(self._db)
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._persist(repo, item)
            except Exception as exc:
                with self._lock:
                    self._stats.errors += 1
                    self._stats.last_error = str(exc)
                logger.warning("Alarm history write failed: %s", exc)

    def _persist(self, repo, event: AlarmEvent) -> None:
        eid = event.event_id or ""
        if eid.startswith("clear_"):
            # Clear event: mark the rule's open row CLEARED (no new row).
            result = repo.mark_cleared(self._open.get(event.rule_id, event.rule_id))
            if not result.success:
                raise RuntimeError(result.error or "clear update failed")
            # Fallback: if no open row tracked, try rule_id-scoped clear.
            if not result.data:
                with self._db.transaction() as cursor:
                    cursor.execute(
                        "UPDATE alarm_events SET status='CLEARED', cleared_at=? "
                        "WHERE rule_id=? AND status<>'CLEARED'",
                        (event.timestamp or time.time(), event.rule_id),
                    )
            self._open.pop(event.rule_id, None)
            with self._lock:
                self._stats.cleared += 1
                self._stats.last_error = ""
                self._stats.last_write_at = time.time()
            return
        # Trigger event: exactly one INSERT per activation (evaluator is
        # edge-triggered; repeats while ACTIVE never reach here).
        if event.rule_id in self._open:
            return  # duplicate guard: rule already has an open row
        result = repo.insert(event)
        if not result.success:
            raise RuntimeError(result.error or "insert failed")
        self._open[event.rule_id] = eid
        with self._lock:
            self._stats.persisted += 1
            self._stats.last_error = ""
            self._stats.last_write_at = time.time()


__all__ = ["AlarmHistoryStore", "AlarmStoreStats"]
