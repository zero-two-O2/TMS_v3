-- 002 PTZ positions (Phase 6)
-- Persistent PTZ position records. A position belongs to a camera_id
-- (primary owner/filter) and records the ptz_id resolved at save time.
-- ROI data is never duplicated here: roi_set_ref keys the existing
-- position_roi_associations table (camera_id + position_id).

CREATE TABLE ptz_positions (
    id INT IDENTITY(1,1) PRIMARY KEY,
    position_id NVARCHAR(100) NOT NULL UNIQUE,
    camera_id NVARCHAR(100) NOT NULL,
    ptz_id NVARCHAR(100) NOT NULL,
    name NVARCHAR(200) NOT NULL DEFAULT '',
    pan FLOAT NOT NULL,
    tilt FLOAT NOT NULL,
    velocity FLOAT NULL,
    pan_velocity FLOAT NULL,
    tilt_velocity FLOAT NULL,
    roi_set_ref NVARCHAR(100) NOT NULL DEFAULT '',
    enabled BIT NOT NULL DEFAULT 1,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
);
GO
CREATE INDEX IX_ptz_positions_camera ON ptz_positions(camera_id);
GO
CREATE INDEX IX_ptz_positions_ptz ON ptz_positions(ptz_id);
GO
