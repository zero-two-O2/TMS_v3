-- 005_roi_audit_timestamps.sql -- Phase 11 SQLite alignment.
-- Mirrors the created_at_dt / updated_at_dt audit columns of SQL
-- Server migration 003_roi_position_binding.sql so both contracts
-- carry the same column set. Existing epoch float columns
-- (created_at / updated_at) remain the application source of truth;
-- these defaulted columns are server-managed audit metadata.
-- Idempotent under re-run only via schema_version (version 5).

ALTER TABLE roi_definitions ADD COLUMN created_at_dt REAL NOT NULL DEFAULT 0.0;
ALTER TABLE roi_definitions ADD COLUMN updated_at_dt REAL NOT NULL DEFAULT 0.0;
