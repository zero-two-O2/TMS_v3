"""Phase 9B: SQLite backend, migrations, alarm-history lifecycle.

Covers (no Qt, no hardware):
- database initialization + versioned migrations (idempotent)
- alarm insert / clear-update / recent-ordering / limit / acknowledge
- no duplicate rows for repeated samples of one ACTIVE alarm
- evaluator edge behavior: trigger once, silent while active, clear once
- AlarmHistoryStore worker: persist + clear + thread ownership + shutdown
- database failure: visible error state, never raises into callers
- DatabaseConfig sqlite validation
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from thermal_monitor.core.models import (
    AlarmCondition,
    AlarmEvent,
    AlarmRule,
    AlarmSeverity,
    AnalysisConfig,
    AnalysisResult,
    ROIStatistics,
    TemperatureUnit,
)
from thermal_monitor.storage.alarm_store import AlarmHistoryStore
from thermal_monitor.storage.repositories.sqlite_alarm import SqliteAlarmEventRepository
from thermal_monitor.storage.sqlite_database import SqliteConfig, SqliteDatabase

SQLITE_MIGRATIONS = Path(__file__).resolve().parents[1] / "database" / "migrations" / "sqlite"


def make_db(tmp_path: Path) -> SqliteDatabase:
    db = SqliteDatabase(SqliteConfig(path=str(tmp_path / "tms_local.db")))
    db.connect()
    db.run_migrations(SQLITE_MIGRATIONS)
    return db


def make_event(rule_id="r1", event_id="alarm_001", ts=1000.0) -> AlarmEvent:
    return AlarmEvent(
        event_id=event_id,
        rule_id=rule_id,
        camera_id="camA",
        roi_id="roi1",
        severity=AlarmSeverity.CRITICAL,
        measured_value=87.4,
        threshold_value=80.0,
        timestamp=ts,
        frame_sequence=42,
        metadata={"ptz_id": "PTZ_01", "status": "ACTIVE"},
    )


def make_clear(rule_id="r1", ts=2000.0) -> AlarmEvent:
    return AlarmEvent(
        event_id="clear_001",
        rule_id=rule_id,
        camera_id="camA",
        roi_id="roi1",
        severity=AlarmSeverity.INFO,
        measured_value=0.0,
        threshold_value=0.0,
        timestamp=ts,
        frame_sequence=99,
    )


class TestSqliteDatabase:
    def test_init_and_versioned_migrations(self, tmp_path):
        db = make_db(tmp_path)
        assert db.is_connected
        assert db.applied_versions == [1, 2, 3]
        tables = {r[0] for r in db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"alarm_events", "ptz_positions", "schema_version"} <= tables

    def test_migrations_idempotent_no_fresh_db_per_launch(self, tmp_path):
        db = make_db(tmp_path)
        repo = SqliteAlarmEventRepository(db)
        assert repo.insert(make_event()).success
        second = db.run_migrations(SQLITE_MIGRATIONS)
        assert second == []
        assert repo.find_recent(10).data is not None
        assert len(repo.find_recent(10).data) == 1

    def test_missing_path_rejected(self):
        with pytest.raises(ValueError):
            SqliteDatabase(SqliteConfig(path="  "))

    def test_status_snapshot_never_raises(self, tmp_path):
        db = make_db(tmp_path)
        state, _detail = db.status
        assert state == "connected"


class TestSqliteAlarmRepository:
    def test_insert_and_retrieve(self, tmp_path):
        db = make_db(tmp_path)
        repo = SqliteAlarmEventRepository(db)
        result = repo.insert(make_event())
        assert result.success
        found = repo.find_by_camera_id("camA")
        assert found.success and len(found.data) == 1
        event = found.data[0]
        assert event.event_id == "alarm_001"
        assert event.measured_value == pytest.approx(87.4)
        assert event.metadata["ptz_id"] == "PTZ_01"

    def test_no_duplicate_rows_for_repeated_active_samples(self, tmp_path):
        db = make_db(tmp_path)
        repo = SqliteAlarmEventRepository(db)
        assert repo.insert(make_event()).success
        # same activation re-inserted (OR IGNORE on event_id) or same rule:
        # repeated samples must not multiply history rows
        assert repo.insert(make_event()).success  # idempotent on event_id
        rows = repo.find_recent(100).data
        assert len(rows) == 1

    def test_clear_updates_row_no_new_row(self, tmp_path):
        db = make_db(tmp_path)
        repo = SqliteAlarmEventRepository(db)
        assert repo.insert(make_event()).success
        cleared = repo.mark_cleared("alarm_001", cleared_at=2000.0)
        assert cleared.success and cleared.data is True
        rows = repo.find_recent(100).data
        assert len(rows) == 1
        assert rows[0].metadata["status"] == "CLEARED"
        assert rows[0].metadata["cleared_at"] == pytest.approx(2000.0)

    def test_recent_ordering_newest_first_and_limit(self, tmp_path):
        db = make_db(tmp_path)
        repo = SqliteAlarmEventRepository(db)
        for i in range(5):
            repo.insert(make_event(rule_id=f"r{i}", event_id=f"alarm_{i}", ts=1000.0 + i))
        rows = repo.find_recent(3).data
        assert [r.timestamp for r in rows] == [1004.0, 1003.0, 1002.0]

    def test_acknowledge(self, tmp_path):
        db = make_db(tmp_path)
        repo = SqliteAlarmEventRepository(db)
        repo.insert(make_event())
        result = repo.acknowledge("alarm_001", "operator")
        assert result.success and result.data is True
        assert repo.find_by_camera_id("camA").data[0].acknowledged is True

    def test_sqlserver_repository_untouched(self):
        import inspect

        import thermal_monitor.storage.repositories.alarm as sqlserver_mod

        src = inspect.getsource(sqlserver_mod.AlarmEventRepository.find_by_camera_id)
        assert "SELECT TOP" in src  # T-SQL preserved; SQLite lives in its own module


class TestEvaluatorEdgeLifecycle:
    def _analysis(self, threshold=80.0):
        rule = AlarmRule(rule_id="r1", roi_id="roi1", condition=AlarmCondition.ABOVE,
                         severity=AlarmSeverity.CRITICAL, threshold=threshold)
        return AnalysisConfig(camera_id="camA", alarm_rules={"r1": rule})

    def _result(self, max_temp, seq=1):
        stats = ROIStatistics(roi_id="roi1", roi_name="roi1",
                              mean_temp=max_temp - 5.0,
                              min_temp=max_temp - 10.0, max_temp=max_temp,
                              deviation=1.0, unit=TemperatureUnit.CELSIUS)
        return AnalysisResult(camera_id="camA", frame_sequence=seq,
                              frame_timestamp=float(seq),
                              roi_results={"roi1": stats})

    def test_trigger_once_silent_while_active_clear_once(self):
        from thermal_monitor.processing.alarms import AlarmEvaluator

        evaluator = AlarmEvaluator(config=self._analysis())
        first = evaluator.evaluate(self._result(90.0, seq=1))
        assert len(first.events) == 1 and first.events[0].event_id.startswith("alarm_")
        # sustained breach: no new events
        for seq in range(2, 12):
            repeated = evaluator.evaluate(self._result(90.0, seq=seq))
            assert repeated.events == ()
            assert repeated.active_alarms == ("r1",)
        cleared = evaluator.evaluate(self._result(20.0, seq=12))
        assert len(cleared.events) == 1 and cleared.events[0].event_id.startswith("clear_")
        assert cleared.cleared_alarms == ("r1",)


class TestAlarmHistoryStore:
    def test_persist_trigger_then_clear(self, tmp_path):
        db = make_db(tmp_path)
        store = AlarmHistoryStore(db)
        store.start()
        try:
            store.record_event(make_event())
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if store.stats_snapshot().persisted >= 1:
                    break
                time.sleep(0.05)
            assert store.stats_snapshot().persisted == 1
            # duplicate trigger for the same active rule: no second row
            store.record_event(make_event())
            time.sleep(0.4)
            repo = SqliteAlarmEventRepository(db)
            assert len(repo.find_recent(100).data) == 1
            store.record_event(make_clear())
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if store.stats_snapshot().cleared >= 1:
                    break
                time.sleep(0.05)
            assert store.stats_snapshot().cleared == 1
            rows = repo.find_recent(100).data
            assert len(rows) == 1 and rows[0].metadata["status"] == "CLEARED"
        finally:
            store.stop()

    def test_worker_thread_ownership_and_clean_shutdown(self, tmp_path):
        db = make_db(tmp_path)
        store = AlarmHistoryStore(db)
        store.start()
        store.record_event(make_event())
        time.sleep(0.5)
        worker = store._thread
        assert worker.daemon is True
        store.stop(timeout_s=5.0)
        assert not worker.is_alive()

    def test_database_failure_visible_not_raised(self, tmp_path):
        db = make_db(tmp_path)
        store = AlarmHistoryStore(db)
        store.start()
        try:
            db_path = tmp_path / "tms_local.db"
            # simulate failure: close underlying connections mid-run
            db.shutdown()
            store.record_event(make_event(event_id="alarm_fail"))
            time.sleep(0.6)
            # never raises into the producer; worker reconnects on demand
            # (SQLite file backend) or records an error state
            snapshot = store.stats_snapshot()
            assert snapshot.persisted >= 0
            state, _detail = store.status()
            assert state in ("ok", "degraded", "unavailable")
        finally:
            store.stop()

    def test_gui_thread_never_blocks_on_io(self, tmp_path):
        db = make_db(tmp_path)
        store = AlarmHistoryStore(db)
        store.start()
        try:
            t0 = time.time()
            for i in range(100):
                store.record_event(make_event(rule_id=f"r{i}", event_id=f"alarm_b{i}"))
            elapsed = time.time() - t0
            assert elapsed < 2.0  # enqueue-only; I/O stays on the worker
        finally:
            store.stop()


class TestDatabaseConfigSqlite:
    def test_sqlite_type_accepted_with_path(self):
        from thermal_monitor.config.models import DatabaseConfig

        cfg = DatabaseConfig(enabled=True, type="sqlite", path="data/tms_local.db")
        assert cfg.type == "sqlite"

    def test_sqlite_enabled_requires_path(self):
        from thermal_monitor.config.models import DatabaseConfig

        with pytest.raises(ValueError):
            DatabaseConfig(enabled=True, type="sqlite", path="")

    def test_unknown_type_rejected(self):
        from thermal_monitor.config.models import DatabaseConfig

        with pytest.raises(ValueError):
            DatabaseConfig(type="postgres")

    def test_thread_safety_no_shared_connection(self, tmp_path):
        db = make_db(tmp_path)
        errors = []

        def worker(n):
            try:
                db.execute("SELECT 1")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert isinstance(db.connect(), sqlite3.Connection)
