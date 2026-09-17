-- 002_ptz_positions.sql -- Phase 9B local PTZ-position cache (SQLite).
-- Versioned migration: applied once, recorded in schema_version.
-- Mirrors the SQL Server ptz_positions table (002_ptz_positions.sql)
-- in SQLite dialect so offline/dev setups persist positions locally.

CREATE TABLE IF NOT EXISTS ptz_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT NOT NULL UNIQUE,
    camera_id TEXT NOT NULL,
    ptz_id TEXT NOT NULL,
    name TEXT NOT NULL,
    pan REAL NOT NULL,
    tilt REAL NOT NULL,
    zoom REAL NOT NULL DEFAULT 1.0,
    roi_set_ref TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_ptz_positions_camera
    ON ptz_positions (camera_id);
CREATE INDEX IF NOT EXISTS idx_ptz_positions_ptz
    ON ptz_positions (ptz_id);
