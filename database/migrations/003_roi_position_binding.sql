-- 003 ROI position binding (Phase 10, SQL Server)
-- Every ROI/analysis object is owned by exactly one
-- (camera_id, ptz_id, position_id) triple. The legacy `rois` table
-- (camera-global, HALCON-shape only) is left untouched; the new
-- typed model lives in `roi_definitions` alongside the existing
-- position_roi_associations table.

CREATE TABLE roi_definitions (
    id INT IDENTITY(1,1) PRIMARY KEY,
    roi_id NVARCHAR(100) NOT NULL UNIQUE,
    camera_id NVARCHAR(100) NOT NULL,
    ptz_id NVARCHAR(100) NOT NULL,
    position_id NVARCHAR(100) NOT NULL,
    object_type NVARCHAR(40) NOT NULL,
    geometry_json NVARCHAR(MAX) NOT NULL,
    name NVARCHAR(200) DEFAULT '',
    enabled BIT NOT NULL DEFAULT 1,
    visible BIT NOT NULL DEFAULT 1,
    created_at FLOAT NOT NULL DEFAULT 0.0,
    updated_at FLOAT NOT NULL DEFAULT 0.0,
    schema_version INT NOT NULL DEFAULT 1,
    analysis_config_json NVARCHAR(MAX) NOT NULL DEFAULT '{}',
    alarm_rule_ref NVARCHAR(100) NOT NULL DEFAULT '',
    display_json NVARCHAR(MAX) NOT NULL DEFAULT '{}',
    created_at_dt DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at_dt DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT UQ_roi_definitions_camera_position_roi UNIQUE (camera_id, position_id, roi_id),
);
GO
CREATE INDEX IX_roi_definitions_camera_position ON roi_definitions(camera_id, position_id);
GO
CREATE INDEX IX_roi_definitions_ptz ON roi_definitions(ptz_id);
GO
