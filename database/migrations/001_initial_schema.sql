-- V3 Initial Schema Migration
-- Creates all tables for the Thermal Monitoring System V3

-- Cameras table
CREATE TABLE cameras (
    id INT IDENTITY(1,1) PRIMARY KEY,
    camera_id NVARCHAR(100) NOT NULL UNIQUE,
    serial_number NVARCHAR(100) NOT NULL,
    model NVARCHAR(100) DEFAULT '',
    vendor NVARCHAR(100) DEFAULT '',
    firmware NVARCHAR(100) DEFAULT '',
    user_name NVARCHAR(100) DEFAULT '',
    name NVARCHAR(200) DEFAULT '',
    description NVARCHAR(MAX) DEFAULT '',
    enabled BIT NOT NULL DEFAULT 1,
    thermal_enabled BIT NOT NULL DEFAULT 1,
    visible_enabled BIT NOT NULL DEFAULT 0,
    device_identifier NVARCHAR(200) DEFAULT '',
    ip_address NVARCHAR(50) DEFAULT '',
    frame_rate INT NOT NULL DEFAULT 9,
    grab_timeout_ms INT NOT NULL DEFAULT 500,
    socket_buffer_size INT NOT NULL DEFAULT 1048576,
    num_buffers INT NOT NULL DEFAULT 8,
    stream_source_thermal NVARCHAR(100) DEFAULT 'IR_Data',
    thermal_bits_per_channel INT NOT NULL DEFAULT 16,
    stream_source_visible NVARCHAR(100) DEFAULT NULL,
    visible_bits_per_channel INT NOT NULL DEFAULT -1,
    consecutive_fail_limit INT NOT NULL DEFAULT 3,
    reconnect_interval_s FLOAT NOT NULL DEFAULT 3.0,
    reconnect_backoff_factor FLOAT NOT NULL DEFAULT 2.0,
    max_reconnect_attempts INT NOT NULL DEFAULT 10,
    -- PTZ limits
    ptz_min_pan FLOAT NOT NULL DEFAULT -170.0,
    ptz_max_pan FLOAT NOT NULL DEFAULT 170.0,
    ptz_min_tilt FLOAT NOT NULL DEFAULT -90.0,
    ptz_max_tilt FLOAT NOT NULL DEFAULT 90.0,
    ptz_min_zoom FLOAT NOT NULL DEFAULT 1.0,
    ptz_max_zoom FLOAT NOT NULL DEFAULT 30.0,
    -- PTZ default position
    ptz_default_pan FLOAT NOT NULL DEFAULT 0.0,
    ptz_default_tilt FLOAT NOT NULL DEFAULT 0.0,
    ptz_default_zoom FLOAT NOT NULL DEFAULT 1.0,
    ptz_mode NVARCHAR(20) NOT NULL DEFAULT 'manual',
    ptz_speed_pan FLOAT NOT NULL DEFAULT 10.0,
    ptz_speed_tilt FLOAT NOT NULL DEFAULT 10.0,
    ptz_speed_zoom FLOAT NOT NULL DEFAULT 5.0,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
);

CREATE INDEX IX_cameras_enabled ON cameras(enabled);
CREATE INDEX IX_cameras_serial ON cameras(serial_number);

GO

-- ROIs table
CREATE TABLE rois (
    id INT IDENTITY(1,1) PRIMARY KEY,
    camera_id NVARCHAR(100) NOT NULL,
    roi_id NVARCHAR(100) NOT NULL,
    name NVARCHAR(200) DEFAULT '',
    enabled BIT NOT NULL DEFAULT 1,
    shape NVARCHAR(20) NOT NULL DEFAULT 'rect',
    parameters_json NVARCHAR(MAX) NOT NULL,
    rotation FLOAT NOT NULL DEFAULT 0.0,
    temp_unit NVARCHAR(20) NOT NULL DEFAULT 'celsius',
    min_warning FLOAT NULL,
    max_warning FLOAT NULL,
    min_critical FLOAT NULL,
    max_critical FLOAT NULL,
    rate_of_change_limit FLOAT NULL,
    alarm_enabled BIT NOT NULL DEFAULT 1,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT UQ_rois_camera_roi UNIQUE (camera_id, roi_id),
);

CREATE INDEX IX_rois_camera ON rois(camera_id);

GO

-- Position-ROI Associations table
CREATE TABLE position_roi_associations (
    id INT IDENTITY(1,1) PRIMARY KEY,
    camera_id NVARCHAR(100) NOT NULL,
    position_id NVARCHAR(100) NOT NULL,
    position_name NVARCHAR(200) DEFAULT '',
    roi_ids_json NVARCHAR(MAX) NOT NULL,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT UQ_pos_roi_camera_pos UNIQUE (camera_id, position_id),
);

CREATE INDEX IX_pos_roi_camera ON position_roi_associations(camera_id);

GO

-- Alarm Rules table
CREATE TABLE alarm_rules (
    id INT IDENTITY(1,1) PRIMARY KEY,
    camera_id NVARCHAR(100) NOT NULL,
    roi_id NVARCHAR(100) NOT NULL,
    rule_id NVARCHAR(100) NOT NULL,
    condition NVARCHAR(20) NOT NULL,
    severity NVARCHAR(20) NOT NULL,
    threshold FLOAT NULL,
    threshold_low FLOAT NULL,
    threshold_high FLOAT NULL,
    unit NVARCHAR(20) NOT NULL DEFAULT 'celsius',
    enabled BIT NOT NULL DEFAULT 1,
    description NVARCHAR(MAX) DEFAULT '',
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT UQ_alarm_rules_camera_rule UNIQUE (camera_id, rule_id),
);

CREATE INDEX IX_alarm_rules_camera ON alarm_rules(camera_id);
CREATE INDEX IX_alarm_rules_roi ON alarm_rules(roi_id);
CREATE INDEX IX_alarm_rules_enabled ON alarm_rules(enabled);

GO

-- Analysis Configs table
CREATE TABLE analysis_configs (
    id INT IDENTITY(1,1) PRIMARY KEY,
    camera_id NVARCHAR(100) NOT NULL UNIQUE,
    default_emissivity FLOAT NOT NULL DEFAULT 0.95,
    ambient_temperature FLOAT NOT NULL DEFAULT 25.0,
    distance FLOAT NOT NULL DEFAULT 1.0,
    humidity FLOAT NOT NULL DEFAULT 50.0,
    reflected_temperature FLOAT NOT NULL DEFAULT 20.0,
    unit NVARCHAR(20) NOT NULL DEFAULT 'celsius',
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
);

GO

-- Alarm Events table
CREATE TABLE alarm_events (
    id INT IDENTITY(1,1) PRIMARY KEY,
    event_id NVARCHAR(100) NOT NULL UNIQUE,
    rule_id NVARCHAR(100) NOT NULL,
    camera_id NVARCHAR(100) NOT NULL,
    roi_id NVARCHAR(100) NOT NULL,
    severity NVARCHAR(20) NOT NULL,
    measured_value FLOAT NOT NULL,
    threshold_value FLOAT NOT NULL,
    timestamp FLOAT NOT NULL,
    frame_sequence INT NOT NULL,
    position_id NVARCHAR(100) NULL,
    acknowledged BIT NOT NULL DEFAULT 0,
    acknowledged_at FLOAT NULL,
    acknowledged_by NVARCHAR(100) NULL,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
);

CREATE INDEX IX_alarm_events_camera ON alarm_events(camera_id);
CREATE INDEX IX_alarm_events_timestamp ON alarm_events(timestamp);
CREATE INDEX IX_alarm_events_acknowledged ON alarm_events(acknowledged);
CREATE INDEX IX_alarm_events_rule ON alarm_events(rule_id);

GO

-- Recordings table
CREATE TABLE recordings (
    id INT IDENTITY(1,1) PRIMARY KEY,
    recording_id NVARCHAR(100) NOT NULL UNIQUE,
    camera_id NVARCHAR(100) NOT NULL,
    trigger NVARCHAR(20) NOT NULL,
    state NVARCHAR(20) NOT NULL,
    start_timestamp FLOAT NOT NULL,
    end_timestamp FLOAT NULL,
    start_sequence INT NOT NULL,
    end_sequence INT NULL,
    pre_alarm_frames INT NOT NULL DEFAULT 0,
    post_alarm_frames INT NOT NULL DEFAULT 0,
    alarm_event_id NVARCHAR(100) NULL,
    position_id NVARCHAR(100) NULL,
    roi_config_hash NVARCHAR(100) NULL,
    file_path NVARCHAR(500) NULL,
    file_size_bytes BIGINT NOT NULL DEFAULT 0,
    frame_count INT NOT NULL DEFAULT 0,
    duration_seconds FLOAT NOT NULL DEFAULT 0.0,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
);

CREATE INDEX IX_recordings_camera ON recordings(camera_id);
CREATE INDEX IX_recordings_state ON recordings(state);
CREATE INDEX IX_recordings_start_ts ON recordings(start_timestamp);

GO

-- Recording Configs table
CREATE TABLE recording_configs (
    id INT IDENTITY(1,1) PRIMARY KEY,
    camera_id NVARCHAR(100) NOT NULL UNIQUE,
    enabled BIT NOT NULL DEFAULT 1,
    pre_alarm_seconds FLOAT NOT NULL DEFAULT 10.0,
    post_alarm_seconds FLOAT NOT NULL DEFAULT 30.0,
    max_duration_seconds FLOAT NOT NULL DEFAULT 300.0,
    max_file_size_mb INT NOT NULL DEFAULT 500,
    storage_path NVARCHAR(500) DEFAULT '',
    compression_enabled BIT NOT NULL DEFAULT 0,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
);

GO

-- System Config table (key-value store)
CREATE TABLE system_config (
    id INT IDENTITY(1,1) PRIMARY KEY,
    config_key NVARCHAR(200) NOT NULL UNIQUE,
    config_value NVARCHAR(MAX) NOT NULL,
    description NVARCHAR(MAX) NULL,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
);

-- Insert default system configuration
INSERT INTO system_config (config_key, config_value, description) VALUES
('application_name', 'Thermal Monitoring System V3', 'Application name'),
('version', '3.0.0', 'Application version'),
('default_mode', 'configuration', 'Default application mode'),
('max_cameras', '8', 'Maximum number of cameras'),
('camera_discovery_enabled', 'true', 'Enable camera discovery'),
('camera_discovery_interval_seconds', '30', 'Camera discovery interval'),
('processing_enabled', 'true', 'Enable frame processing'),
('processing_interval_ms', '100', 'Minimum processing interval'),
('alarm_evaluation_enabled', 'true', 'Enable alarm evaluation'),
('alarm_cooldown_seconds', '5', 'Alarm cooldown period'),
('max_alarm_history', '10000', 'Maximum alarm history entries'),
('recording_enabled', 'true', 'Enable recording'),
('database_connection_string', '', 'SQL Server connection string'),
('recording_storage_path', '', 'Recording storage path'),
('offline_storage_path', '', 'Offline storage path'),
('log_level', 'INFO', 'Log level'),
('log_file_path', '', 'Log file path'),
('log_max_size_mb', '100', 'Log file max size'),
('log_backup_count', '5', 'Log backup count'),
('bind_address', '0.0.0.0', 'HTTP bind address'),
('http_port', '8080', 'HTTP port');

GO