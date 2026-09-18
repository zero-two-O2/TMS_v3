-- 004_roi_position_binding.sql -- Phase 10 local ROI store (SQLite).
-- Mirrors database/migrations/003_roi_position_binding.sql in SQLite
-- dialect. Legacy `rois` semantics are untouched; the typed model
-- lives in `roi_definitions` keyed by (camera_id, position_id).

CREATE TABLE IF NOT EXISTS roi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    roi_id TEXT NOT NULL UNIQUE,
    camera_id TEXT NOT NULL,
    ptz_id TEXT NOT NULL,
    position_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    geometry_json TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    visible INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0,
    schema_version INTEGER NOT NULL DEFAULT 1,
    analysis_config_json TEXT NOT NULL DEFAULT '{}',
    alarm_rule_ref TEXT NOT NULL DEFAULT '',
    display_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (camera_id, position_id, roi_id)
);

CREATE INDEX IF NOT EXISTS idx_roi_definitions_camera_position
    ON roi_definitions (camera_id, position_id);
CREATE INDEX IF NOT EXISTS idx_roi_definitions_ptz
    ON roi_definitions (ptz_id);
