"""Phase 9D: PTZ hardening regression tests.

Covers the two defects proven by live-simulator acceptance:
1. Monitor outage backoff (service.py): dead-link polling slows down
   (bounded, interruptible) instead of spamming per-read transport
   warnings at full rate; degraded statuses are still delivered and
   recovery stays snappy.
2. Simulator read-request log spam (__main__): the asyncua per-Read
   INFO logger is quieted to WARNING; errors stay visible.
"""

from __future__ import annotations

import logging
import time

from thermal_monitor.ptz.service import PtzService

from test_ptz_service import CAM_A, PTZ_A, make_service, wait_for


class TestOutageBackoff:
    def test_backoff_bounded(self):
        assert PtzService._OUTAGE_BACKOFF_MAX_S == 2.0

    def test_degraded_still_delivered_during_outage(self):
        service, transport = make_service()
        seen: list = []
        service.add_status_listener(lambda ptz_id, status: seen.append(status))
        service.start_monitoring()
        try:
            assert wait_for(
                lambda: service.cached_status(CAM_A) is not None, timeout_s=3.0
            )
            transport.read_error = Exception("link dead")
            assert wait_for(
                lambda: (
                    service.cached_status(CAM_A) is not None
                    and service.cached_status(CAM_A).communication_ok is False
                ),
                timeout_s=5.0,
            )
            assert any(s.communication_ok is False for s in seen)
        finally:
            transport.read_error = None
            service.stop_monitoring()
            service.shutdown()

    def test_stop_prompt_during_backoff(self):
        """stop_monitoring must not wait out the backoff (Event.wait)."""
        service, transport = make_service()
        service.start_monitoring()
        try:
            assert wait_for(
                lambda: service.cached_status(CAM_A) is not None, timeout_s=3.0
            )
            transport.read_error = Exception("link dead")
            time.sleep(0.5)  # enter outage backoff
            t0 = time.monotonic()
            service.stop_monitoring(timeout_s=5.0)
            assert time.monotonic() - t0 < 4.0
        finally:
            transport.read_error = None
            service.shutdown()

    def test_recovery_resumes_after_outage(self):
        service, transport = make_service()
        service.start_monitoring()
        try:
            assert wait_for(
                lambda: service.cached_status(CAM_A) is not None, timeout_s=3.0
            )
            transport.read_error = Exception("link dead")
            assert wait_for(
                lambda: service.cached_status(CAM_A).communication_ok is False,
                timeout_s=5.0,
            )
            transport.read_error = None
            assert wait_for(
                lambda: service.cached_status(CAM_A).communication_ok is True,
                timeout_s=5.0,
            )
        finally:
            service.stop_monitoring()
            service.shutdown()


class TestPtzPositionsSchema:
    """Live defect: 'Save failed: table ptz_positions has no column
    named velocity'. The SQLite 002 mirror diverged from canonical
    SQL Server 002 (extra `zoom`, missing velocity columns, wrong
    order for the positional SELECT * mapping), and base insert's
    T-SQL SCOPE_IDENTITY probe fails on SQLite."""

    def _migrations(self):
        from pathlib import Path

        return Path(__file__).resolve().parents[1] / "database" / "migrations" / "sqlite"

    def _db(self, tmp_path, name="t.db"):
        from thermal_monitor.storage.sqlite_database import (
            SqliteConfig,
            SqliteDatabase,
        )

        db = SqliteDatabase(SqliteConfig(path=str(tmp_path / name)))
        db.connect()
        return db

    def test_migration_adds_velocity_columns_in_repo_order(self, tmp_path):
        db = self._db(tmp_path)
        try:
            done = db.run_migrations(self._migrations())
            assert [p.name for p in done][-1] == "004_roi_position_binding.sql"
            cols = [r[1] for r in db.fetch_all("PRAGMA table_info(ptz_positions)")]
            assert cols == ["id", "position_id", "camera_id", "ptz_id", "name",
                            "pan", "tilt", "velocity", "pan_velocity",
                            "tilt_velocity", "roi_set_ref", "enabled",
                            "created_at", "updated_at"]
        finally:
            db.shutdown()

    def test_legacy_rows_preserved_with_null_velocities(self, tmp_path):
        import sqlite3

        from thermal_monitor.storage.repositories.ptz import PtzPositionRepository

        path = tmp_path / "legacy.db"
        con = sqlite3.connect(str(path))
        con.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
        con.execute("INSERT INTO schema_version VALUES (1, 0.0)")
        # Legacy 002 shape (zoom, no velocity columns) with one row.
        con.execute("CREATE TABLE ptz_positions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "position_id TEXT NOT NULL UNIQUE, camera_id TEXT NOT NULL,"
                    "ptz_id TEXT NOT NULL, name TEXT NOT NULL, pan REAL NOT NULL,"
                    "tilt REAL NOT NULL, zoom REAL NOT NULL DEFAULT 1.0,"
                    "roi_set_ref TEXT, enabled INTEGER NOT NULL DEFAULT 1,"
                    "created_at REAL NOT NULL DEFAULT 0.0, updated_at REAL NOT NULL DEFAULT 0.0)")
        con.execute("INSERT INTO ptz_positions (position_id, camera_id, ptz_id, name, pan, tilt)"
                    " VALUES ('p9','cam_HB25100001','PTZ_08','Old',1.0,2.0)")
        con.commit()
        con.close()
        db = self._db(tmp_path, "legacy.db")
        try:
            db.run_migrations(self._migrations())
            repo = PtzPositionRepository(db)
            got = repo.get_position("p9")
            assert got.success and got.data is not None
            assert (got.data.pan, got.data.tilt) == (1.0, 2.0)
            assert got.data.velocity is None
        finally:
            db.shutdown()

    def test_position_crud_roundtrip_on_sqlite(self, tmp_path):
        from thermal_monitor.ptz.positions import PtzPosition
        from thermal_monitor.storage.repositories.ptz import PtzPositionRepository

        db = self._db(tmp_path)
        try:
            db.run_migrations(self._migrations())
            repo = PtzPositionRepository(db)
            pos = PtzPosition(position_id="p1", camera_id="cam_HB25100001",
                              ptz_id="PTZ_08", name="Furnace",
                              pan=20.0, tilt=10.0, velocity=10.0)
            created = repo.create_position(pos)
            assert created.success, created.error
            got = repo.get_position("p1")
            assert got.success and got.data is not None
            assert (got.data.pan, got.data.tilt, got.data.velocity) == (20.0, 10.0, 10.0)
            assert "PTZ_08" in (repo.list_positions_for_camera("cam_HB25100001").data[0].ptz_id,)
            dup = repo.create_position(pos)
            assert not dup.success  # duplicate position_id rejected
            renamed = repo.update_position(PtzPosition(
                position_id="p1", camera_id="cam_HB25100001", ptz_id="PTZ_08",
                name="Furnace2", pan=21.0, tilt=11.0, velocity=10.0))
            assert renamed.success, renamed.error
            assert repo.get_position("p1").data.name == "Furnace2"
            assert repo.delete_position("p1").success
            assert repo.get_position("p1").data is None
        finally:
            db.shutdown()


class TestSimulatorLogQuiet:
    def test_uaprocessor_quieted_to_warning(self):
        from tools.ptz_plc_simulator.__main__ import _quiet_asyncua_poll_logging

        logger = logging.getLogger("asyncua.server.uaprocessor")
        old = logger.level
        logger.setLevel(logging.INFO)
        try:
            _quiet_asyncua_poll_logging()
            assert logger.level == logging.WARNING
        finally:
            logger.setLevel(old)
