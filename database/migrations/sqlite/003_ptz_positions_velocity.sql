-- 003_ptz_positions_velocity.sql -- align SQLite ptz_positions with the
-- canonical schema (migrations/002_ptz_positions.sql, SQL Server) and
-- with PtzPositionRepository's positional row mapping.
--
-- Background: the SQLite 002 mirror diverged -- it created a `zoom`
-- column the repository never writes and omitted
-- velocity/pan_velocity/tilt_velocity, so every position save failed
-- with "no column named velocity". Column ORDER also matters: reads
-- use SELECT * mapped positionally.
--
-- This rebuild preserves existing rows (velocities backfill NULL, the
-- divergent `zoom` value is dropped as the repository never used it).

CREATE TABLE IF NOT EXISTS ptz_positions_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT NOT NULL UNIQUE,
    camera_id TEXT NOT NULL,
    ptz_id TEXT NOT NULL,
    name TEXT NOT NULL,
    pan REAL NOT NULL,
    tilt REAL NOT NULL,
    velocity REAL,
    pan_velocity REAL,
    tilt_velocity REAL,
    roi_set_ref TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0
);

INSERT OR IGNORE INTO ptz_positions_new
    (position_id, camera_id, ptz_id, name, pan, tilt,
     roi_set_ref, enabled, created_at, updated_at)
    SELECT position_id, camera_id, ptz_id, name, pan, tilt,
     roi_set_ref, enabled, created_at, updated_at
    FROM ptz_positions;

DROP TABLE ptz_positions;

ALTER TABLE ptz_positions_new RENAME TO ptz_positions;

CREATE INDEX IF NOT EXISTS idx_ptz_positions_camera
    ON ptz_positions (camera_id);
CREATE INDEX IF NOT EXISTS idx_ptz_positions_ptz
    ON ptz_positions (ptz_id);
