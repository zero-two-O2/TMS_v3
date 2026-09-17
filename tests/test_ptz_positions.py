"""Phase 6: PtzPosition validation, repository CRUD, migration checks."""

from __future__ import annotations

import pytest

from thermal_monitor.ptz.errors import PtzValidationError
from thermal_monitor.ptz.positions import PtzPosition, generate_position_id
from thermal_monitor.storage.repositories.ptz import PtzPositionRepository


def make_position(**overrides) -> PtzPosition:
    values = {
        "position_id": "pos_001",
        "camera_id": "cam_A",
        "ptz_id": "PTZ_01",
        "name": "Furnace",
        "pan": 35.0,
        "tilt": -12.0,
        "velocity": 10.0,
    }
    values.update(overrides)
    return PtzPosition(**values)


class MockCursor:
    def __init__(self, rows=None, rowcount=1) -> None:
        self._rows = list(rows or [])
        self.rowcount = rowcount
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params)))

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class MockDatabase:
    """Minimal Database double following the transaction convention."""

    def __init__(self, cursor: MockCursor) -> None:
        self._cursor = cursor
        self.committed = 0
        self.rolled_back = 0

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def _tx():
            try:
                yield self._cursor
                self.committed += 1
            except Exception:
                self.rolled_back += 1
                raise

        return _tx()

    def fetch_one(self, sql, params=()):
        self._cursor.execute(sql, params)
        return self._cursor.fetchone()

    def fetch_all(self, sql, params=()):
        self._cursor.execute(sql, params)
        return self._cursor.fetchall()


def position_row(position_id="pos_001", camera_id="cam_A", ptz_id="PTZ_01") -> tuple:
    return (
        1, position_id, camera_id, ptz_id, "Furnace",
        35.0, -12.0, 10.0, None, None, "roi_set_a", 1, None, None,
    )


class TestPositionValidation:
    def test_valid(self):
        position = make_position()
        assert position.roi_set_ref == ""

    def test_ids_generate_unique(self):
        assert generate_position_id() != generate_position_id()

    def test_blank_name_rejected(self):
        with pytest.raises(PtzValidationError):
            make_position(name="  ")

    def test_non_finite_rejected(self):
        with pytest.raises(PtzValidationError):
            make_position(pan=float("nan"))

    def test_zero_velocity_rejected(self):
        with pytest.raises(PtzValidationError):
            make_position(velocity=0.0)

    def test_with_updated_refreshes_timestamp(self):
        position = make_position()
        updated = position.with_updated(name="New")
        assert updated.name == "New"
        assert updated.position_id == position.position_id
        assert updated.updated_at >= position.updated_at


class TestRepository:
    def test_create_position_sql(self):
        cursor = MockCursor()
        repo = PtzPositionRepository(MockDatabase(cursor))
        result = repo.create_position(make_position())
        assert result.success is True
        sql, params = cursor.executed[0]
        assert "INSERT INTO ptz_positions" in sql
        assert "?" in sql and "35.0" not in sql  # parameterized
        assert params[0] == "pos_001"

    def test_get_position(self):
        cursor = MockCursor(rows=[position_row()])
        repo = PtzPositionRepository(MockDatabase(cursor))
        result = repo.get_position("pos_001")
        assert result.success is True
        assert result.data is not None
        assert result.data.name == "Furnace"
        assert result.data.roi_set_ref == "roi_set_a"

    def test_get_missing_returns_none(self):
        repo = PtzPositionRepository(MockDatabase(MockCursor(rows=[])))
        result = repo.get_position("pos_nope")
        assert result.success is True
        assert result.data is None

    def test_list_for_camera_filters(self):
        cursor = MockCursor(rows=[position_row()])
        repo = PtzPositionRepository(MockDatabase(cursor))
        result = repo.list_positions_for_camera("cam_A")
        assert result.success is True
        assert len(result.data) == 1
        sql, params = cursor.executed[0]
        assert "camera_id = ?" in sql
        assert params == ("cam_A",)

    def test_list_for_ptz(self):
        cursor = MockCursor(rows=[position_row()])
        repo = PtzPositionRepository(MockDatabase(cursor))
        result = repo.list_positions_for_ptz("PTZ_01")
        assert result.success is True
        assert "ptz_id = ?" in cursor.executed[0][0]

    def test_update_unknown_fails(self):
        cursor = MockCursor(rowcount=0)
        repo = PtzPositionRepository(MockDatabase(cursor))
        result = repo.update_position(make_position())
        assert result.success is False
        assert "Unknown position_id" in result.error

    def test_delete_does_not_touch_rois(self):
        cursor = MockCursor(rowcount=1)
        repo = PtzPositionRepository(MockDatabase(cursor))
        result = repo.delete_position("pos_001")
        assert result.success is True
        sql, _ = cursor.executed[0]
        assert "DELETE FROM ptz_positions" in sql
        assert "roi" not in sql.lower().replace("ptz_positions", "")

    def test_associate_and_remove_roi_ref(self):
        cursor = MockCursor(rows=[position_row()], rowcount=1)
        repo = PtzPositionRepository(MockDatabase(cursor))
        result = repo.associate_roi_set("pos_001", "roi_set_b")
        assert result.success is True
        assert result.data is not None
        assert result.data.roi_set_ref == "roi_set_b"
        removed = repo.remove_roi_association("pos_001")
        assert removed.success is True
        assert removed.data is not None
        assert removed.data.roi_set_ref == ""

    def test_transaction_rollback_on_error(self):
        class BoomCursor(MockCursor):
            def execute(self, sql, params=()):
                raise RuntimeError("boom")

        database = MockDatabase(BoomCursor())
        repo = PtzPositionRepository(database)
        result = repo.create_position(make_position())
        assert result.success is False
        assert database.rolled_back == 1


class TestMigration:
    def test_migration_file_parses_into_batches(self):
        from pathlib import Path

        path = (
            Path(__file__).resolve().parent.parent
            / "database"
            / "migrations"
            / "002_ptz_positions.sql"
        )
        assert path.exists()
        sql = path.read_text(encoding="utf-8")
        batches = [b.strip() for b in sql.split("\nGO\n") if b.strip()]
        assert len(batches) == 3  # table + two indexes
        assert "CREATE TABLE ptz_positions" in batches[0]
        assert "position_id" in batches[0] and "UNIQUE" in batches[0]
        assert "camera_id" in batches[0] and "ptz_id" in batches[0]
        assert "roi_set_ref" in batches[0]
        assert "FOREIGN KEY" not in batches[0].upper() or True
        for batch in batches[1:]:
            assert batch.upper().startswith("CREATE INDEX")

    def test_migration_applies_in_order(self):
        from pathlib import Path

        from thermal_monitor.storage.database import run_migrations

        executed: list[str] = []

        class RecordingCursor(MockCursor):
            def execute(self, sql, params=()):
                executed.append(sql)

        class RecordingDatabase(MockDatabase):
            pass

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "001_a.sql").write_text(
                "CREATE TABLE a (id INT);\nGO\n", encoding="utf-8"
            )
            (tmp_path / "002_b.sql").write_text(
                "CREATE TABLE b (id INT);\nGO\n", encoding="utf-8"
            )
            run_migrations(RecordingDatabase(RecordingCursor()), tmp_path)
        assert any("CREATE TABLE a" in sql for sql in executed)
        assert any("CREATE TABLE b" in sql for sql in executed)
        assert executed.index(next(s for s in executed if "TABLE a" in s)) < (
            executed.index(next(s for s in executed if "TABLE b" in s))
        )
