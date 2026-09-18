"""Phase 11 ownership tests: camera+position ROI isolation and schema contract.

- ROI for Camera A + Position 1 is returned ONLY there.
- Missing/foreign ownership rejected at save and load boundaries.
- Duplicate ids rejected; position delete removes its ROIs atomically.
- SQLite and SQL Server roi_definitions contracts match.
"""

import re
from pathlib import Path

import pytest

from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.errors import RoiBindingError, RoiPersistenceError
from thermal_monitor.roi.geometry import SpotGeometry
from thermal_monitor.roi.loading import load_rois_for_position
from thermal_monitor.roi.models import RoiDefinition
from thermal_monitor.roi.repository import RoiDefinitionRepository
from thermal_monitor.storage.sqlite_database import SqliteConfig, SqliteDatabase


def _roi(roi_id="roi_1", camera_id="cam_A", ptz_id="PTZ_01", position_id="pos_1"):
    return RoiDefinition(roi_id=roi_id, camera_id=camera_id, ptz_id=ptz_id,
                         position_id=position_id, object_type=RoiObjectType.SPOT,
                         geometry=SpotGeometry(row=5.0, col=6.0))


@pytest.fixture
def db(tmp_path):
    database = SqliteDatabase(SqliteConfig(path=str(tmp_path / "own.db")))
    database.connect()
    database.run_migrations(Path("database/migrations/sqlite"))
    return database


class _Pos:
    def __init__(self, position_id, camera_id, ptz_id="PTZ_01"):
        self.position_id = position_id
        self.camera_id = camera_id
        self.ptz_id = ptz_id


def test_roi_visible_only_for_exact_owner(db):
    repo = RoiDefinitionRepository(db)
    repo.create(_roi())
    assert [r.roi_id for r in repo.list_for_position("cam_A", "pos_1")] == ["roi_1"]
    assert repo.list_for_position("cam_A", "pos_2") == []
    assert repo.list_for_position("cam_B", "pos_1") == []


def test_loader_enforces_owner_matrix(db):
    repo = RoiDefinitionRepository(db)
    repo.create(_roi())
    positions = {"pos_1": _Pos("pos_1", "cam_A")}
    context, rois = load_rois_for_position(
        "cam_A", "pos_1", 1, 1, position_provider=positions.get,
        roi_repository=repo)
    assert [r.roi_id for r in rois] == ["roi_1"]
    assert context.roi_ids == ("roi_1",)
    with pytest.raises(RoiBindingError):
        load_rois_for_position("cam_A", "pos_2", 1, 1,
                               position_provider=positions.get,
                               roi_repository=repo)
    with pytest.raises(RoiBindingError):
        load_rois_for_position("cam_B", "pos_1", 1, 1,
                               position_provider=positions.get,
                               roi_repository=repo)


def test_missing_position_id_rejected():
    with pytest.raises(RoiBindingError):
        RoiDefinition(roi_id="r", camera_id="cam_A", ptz_id="p",
                      position_id="", object_type=RoiObjectType.SPOT,
                      geometry=SpotGeometry(row=1, col=1))


def test_position_from_another_camera_rejected_at_load(db):
    repo = RoiDefinitionRepository(db)
    repo.create(_roi())
    positions = {"pos_1": _Pos("pos_1", "cam_B", "PTZ_09")}
    with pytest.raises(RoiBindingError):
        load_rois_for_position("cam_A", "pos_1", 1, 1,
                               position_provider=positions.get,
                               roi_repository=repo)


def test_duplicate_roi_id_rejected(db):
    repo = RoiDefinitionRepository(db)
    repo.create(_roi())
    with pytest.raises(RoiPersistenceError):
        repo.create(_roi())


def test_position_delete_removes_rois_atomically(db):
    repo = RoiDefinitionRepository(db)
    repo.create(_roi("r1", position_id="pos_1"))
    repo.create(_roi("r2", position_id="pos_1"))
    repo.create(_roi("r3", position_id="pos_2"))
    with db.transaction() as cursor:
        cursor.execute(
            "DELETE FROM roi_definitions WHERE camera_id = ? AND position_id = ?",
            ("cam_A", "pos_1"))
        assert cursor.rowcount == 2
        cursor.execute(
            "DELETE FROM ptz_positions WHERE position_id = ? AND camera_id = ?",
            ("pos_1", "cam_A"))
    assert repo.list_for_position("cam_A", "pos_1") == []
    assert [r.roi_id for r in repo.list_for_position("cam_A", "pos_2")] == ["r3"]
    assert repo.delete_for_position("cam_A", "pos_2") == 1
    assert repo.list_for_position("cam_A", "pos_2") == []


def _create_columns(path: Path, table: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"CREATE TABLE (?:IF NOT EXISTS )?{table}\s*\((.*?)\);",
                      text, re.IGNORECASE | re.DOTALL)
    assert match, f"{table} not found in {path.name}"
    body = match.group(1)
    columns = []
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.upper().startswith(
                ("CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK")):
            continue
        columns.append(line.split()[0].strip("[]\""))
    return columns


def test_sqlite_sqlserver_schema_contracts_match():
    sqlite_dir = Path("database/migrations/sqlite")
    create = _create_columns(sqlite_dir / "004_roi_position_binding.sql",
                            "roi_definitions")
    alter_text = (sqlite_dir / "005_roi_audit_timestamps.sql").read_text(
        encoding="utf-8")
    added = re.findall(r"ADD COLUMN\s+(\w+)", alter_text, re.IGNORECASE)
    sqlite = create + added
    sqlserver = _create_columns(
        Path("database/migrations/003_roi_position_binding.sql"),
        "roi_definitions")
    norm = lambda cols: [c.lower() for c in cols]
    assert norm(sqlite) == norm(sqlserver)
    for required in ("roi_id", "camera_id", "ptz_id", "position_id",
                     "object_type", "geometry_json", "schema_version"):
        assert required in norm(sqlite)
