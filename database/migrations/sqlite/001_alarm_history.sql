-- 001_alarm_history.sql -- Phase 9B local alarm-history schema (SQLite).
-- Versioned migration: applied once, recorded in schema_version.
-- Mirrors the SQL Server alarm_events table (001_initial_schema.sql)
-- in SQLite dialect. No credentials stored here.

CREATE TABLE IF NOT EXISTS alarm_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    rule_id TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    roi_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    measured_value REAL NOT NULL DEFAULT 0.0,
    threshold_value REAL NOT NULL DEFAULT 0.0,
    timestamp REAL NOT NULL,
    frame_sequence INTEGER NOT NULL DEFAULT 0,
    position_id TEXT,
    ptz_id TEXT,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    acknowledged_at REAL,
    acknowledged_by TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    cleared_at REAL,
    description TEXT
);

CREATE INDEX IF NOT EXISTS idx_alarm_events_timestamp
    ON alarm_events (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_alarm_events_camera
    ON alarm_events (camera_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_alarm_events_rule
    ON alarm_events (rule_id, status);
