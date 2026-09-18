"""Phase 10G: ROI persistence tests (SQLite fresh/migrate/CRUD/rollback)."""

from pathlib import Path

import pytest

from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.errors import RoiBindingError, RoiPersistenceError
from thermal_monitor.roi.geometry import RectangleGeometry, SpotGeometry
from thermal_monitor.roi.models import RoiDefinition
from thermal_monitor.roi.repository import RoiDefinitionRepository
from thermal_monitor.storage.sqlite_database import SqliteConfig, SqliteDatabase


def _roi(roi_id="roi_1", position_id="pos_A", camera_id="cam_1", ptz_id="ptz_1"):
    return RoiDefinition(roi_id=roi_id, camera_id=camera_id, ptz_id=ptz_id,
                         position_id=position_id, object_type=RoiObjectType.SPOT,
                         geometry=SpotGeometry(row=5.0, col=6.0), name="S1")


@pytest.fixture
def db(tmp_path):
    database = SqliteDatabase(SqliteConfig(path=str(tmp_path / "roi_test.db")))
    database.connect()
    database.run_migrations(Path("database/migrations/sqlite"))
    return database


def test_fresh_migration_creates_table(db):
    rows = db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='roi_definitions'")
    assert len(rows) == 1
    assert 4 in db.applied_versions


def test_migration_upgrade_preserves_existing(tmp_path):
    path = tmp_path / "upgrade.db"
    database = SqliteDatabase(SqliteConfig(path=str(path)))
    database.connect()
    # Simulate an older database with only migrations 001-003 applied.
    scripts = sorted(Path("database/migrations/sqlite").glob("*.sql"))[:3]
    for script in scripts:
        with database.transaction() as cursor:
            cursor.executescript(script.read_text(encoding="utf-8"))
    import time as _time
    with database.transaction() as cursor:
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
        for version in (1, 2, 3):
            cursor.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, _time.time()))
    database.execute(
        "INSERT INTO ptz_positions (position_id, camera_id, ptz_id, name, pan, tilt) "
        "VALUES (?, ?, ?, ?, ?, ?)", ("pos_A", "cam_1", "ptz_1", "A", 0.0, 0.0))
    done = database.run_migrations(Path("database/migrations/sqlite"))
    assert [p.name for p in done] == ["004_roi_position_binding.sql",
                                      "005_roi_audit_timestamps.sql"]
    row = database.fetch_one(
        "SELECT position_id FROM ptz_positions WHERE position_id = ?", ("pos_A",))
    assert row[0] == "pos_A"


def test_crud_round_trip(db):
    repo = RoiDefinitionRepository(db)
    created = repo.create(_roi())
    assert created.roi_id == "roi_1"
    fetched = repo.get("roi_1")
    assert fetched == created
    updated = fetched.with_updated(name="renamed")
    repo.update(updated)
    assert repo.get("roi_1").name == "renamed"
    assert repo.delete("roi_1") == 1
    assert repo.get("roi_1") is None


def test_camera_position_filtering(db):
    repo = RoiDefinitionRepository(db)
    repo.create(_roi("r1", position_id="pos_A"))
    repo.create(_roi("r2", position_id="pos_B"))
    assert [r.roi_id for r in repo.list_for_position("cam_1", "pos_A")] == ["r1"]
    assert [r.roi_id for r in repo.list_for_position("cam_1", "pos_B")] == ["r2"]
    assert repo.list_for_position("cam_OTHER", "pos_A") == []


def test_duplicate_rejected(db):
    repo = RoiDefinitionRepository(db)
    repo.create(_roi())
    with pytest.raises(RoiPersistenceError):
        repo.create(_roi())


def test_bulk_replace_transactional_rollback(db):
    repo = RoiDefinitionRepository(db)
    repo.create(_roi("keep", position_id="pos_A"))
    good = _roi("new_1", position_id="pos_A")
    # A foreign ROI must abort the whole replacement (rollback keeps "keep").
    foreign = RoiDefinition(roi_id="foreign", camera_id="cam_1", ptz_id="ptz_1",
                            position_id="pos_OTHER", object_type=RoiObjectType.SPOT,
                            geometry=SpotGeometry(row=1, col=1))
    with pytest.raises(RoiBindingError):
        repo.replace_all("cam_1", "pos_A", "ptz_1", [good, foreign])
    assert [r.roi_id for r in repo.list_for_position("cam_1", "pos_A")] == ["keep"]


def test_bulk_replace_success(db):
    repo = RoiDefinitionRepository(db)
    repo.create(_roi("old", position_id="pos_A"))
    repo.replace_all("cam_1", "pos_A", "ptz_1",
                     [_roi("n1", position_id="pos_A"), _roi("n2", position_id="pos_A")])
    assert sorted(r.roi_id for r in repo.list_for_position("cam_1", "pos_A")) == ["n1", "n2"]


def test_update_cannot_change_ownership(db):
    repo = RoiDefinitionRepository(db)
    repo.create(_roi())
    stored = repo.get("roi_1")
    tampered = RoiDefinition(
        roi_id=stored.roi_id, camera_id="cam_1", ptz_id="ptz_1",
        position_id="pos_OTHER", object_type=stored.object_type,
        geometry=stored.geometry, name=stored.name)
    with pytest.raises(RoiBindingError):
        repo.update(tampered)


def test_geometry_versioned_json(db):
    repo = RoiDefinitionRepository(db)
    roi = RoiDefinition(roi_id="rect", camera_id="cam_1", ptz_id="ptz_1",
                        position_id="pos_A", object_type=RoiObjectType.RECTANGLE,
                        geometry=RectangleGeometry(row1=1, col1=2, row2=10, col2=20))
    repo.create(roi)
    assert repo.get("rect") == roi
