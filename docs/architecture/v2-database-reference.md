# V2 Database Reference — Technical Recovery Report

## Purpose

Analyze V2 database-related implementation in `reference/TMS_v2/database/`, `halcon_roi_validation.py`, `setup_environment.py`, `configuration/`, and tests to document existing SQL Server schema, queries, connection patterns, and production vs validation code paths.

**Do NOT design V3 database yet. Do NOT create migrations.**

---

## 1. Database Manager (`database/database_manager.py`)

**Note:** File not found in V2 reference. Database access is distributed across validation tool and ROI persistence.

---

## 2. Validation Tool Database Access (`halcon_roi_validation.py`)

### 2.1 Configuration (Module-level Constants)
```python
# SQL Server connection (single source of truth)
DB_SERVER = os.environ.get("TM_SQL_SERVER", "localhost\\SQLEXPRESS")
DB_DATABASE = os.environ.get("TM_SQL_DATABASE", "ThermalMonitor")
DB_TRUSTED_CONNECTION = os.environ.get("TM_SQL_AUTH", "trusted").lower() != "sql"
DB_USERNAME = os.environ.get("TM_SQL_USERNAME", "")
DB_PASSWORD = os.environ.get("TM_SQL_PASSWORD", "")

# Driver preference order
DB_DRIVER_CANDIDATES = (
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
)

# Fallback server (SQL Auth, self-signed cert)
DB_FALLBACK_SERVER = os.environ.get("TM_SQL_FALLBACK_SERVER", "DESKTOP-4L5G45H")
DB_FALLBACK_DATABASE = os.environ.get("TM_SQL_FALLBACK_DATABASE", "ThermalMonitor")
DB_FALLBACK_USERNAME = os.environ.get("TM_SQL_FALLBACK_USERNAME", "sa")
DB_FALLBACK_PASSWORD = os.environ.get("THERMALMONITOR_SQL_PASSWORD", "")
DB_FALLBACK_TRUST_SERVER_CERTIFICATE = True

# Per-attempt timeout
DB_CONNECTION_TIMEOUT = 5  # seconds
```

### 2.2 Connection Strategy (`DatabaseRepository.connect()`)
1. **Resolve driver** — first installed driver from `DB_DRIVER_CANDIDATES`
2. **Primary attempt** — `DB_SERVER` with `Trusted_Connection=yes` (Windows Auth)
3. **Fallback attempt** — `DB_FALLBACK_SERVER` with SQL Auth (sa/password)
4. **Exactly two attempts** — no scanning, no retry loops
5. **On success** — probe schema (`_detect_*` methods), cache `db_source`

### 2.3 Connection String Builder
```python
parts = [
    f"DRIVER={{{driver}}}",
    f"SERVER={server}",
    f"DATABASE={database}",
]
if trusted:
    parts.append("Trusted_Connection=yes")
else:
    parts.append(f"UID={username}")
    parts.append(f"PWD={password}")
if trust_server_certificate:
    parts.append("TrustServerCertificate=yes")
```

---

## 3. Schema Detection (Read-Only Probes)

| Probe | Query | Purpose |
|-------|-------|---------|
| `_detect_alarm_camera_column` | `SELECT TOP 0 camera_id FROM dbo.alarm_settings` | Per-camera alarm limits? |
| `_detect_rois_position_column` | `SELECT TOP 0 position_id FROM dbo.rois` | Position-scoped ROIs? |
| `_detect_positions_table` | `SELECT TOP 0 id FROM dbo.camera_positions` | Positions table exists? |

Results cached in `DatabaseRepository` instance (`_has_camera_column`, etc.)

---

## 4. Tables & Queries

### 4.1 `dbo.cameras`
```sql
-- Load enabled cameras
SELECT id, camera_number, serial, ip, model, enabled
FROM dbo.cameras 
WHERE enabled = 1 
ORDER BY camera_number

-- Count disabled
SELECT COUNT(*) AS cnt FROM dbo.cameras WHERE enabled = 0
```

**Columns inferred:** `id` (PK), `camera_number`, `serial`, `ip`, `model`, `enabled`

### 4.2 `dbo.rois`
```sql
-- With position_id (migrated schema)
SELECT roi_name, y1, x1, y2, x2 
FROM dbo.rois 
WHERE camera_id = ? AND position_id = ? AND enabled = 1

-- Legacy fallback (position_id IS NULL)
SELECT roi_name, y1, x1, y2, x2 
FROM dbo.rois 
WHERE camera_id = ? AND position_id IS NULL AND enabled = 1

-- Without position_id (legacy schema)
SELECT roi_name, y1, x1, y2, x2 
FROM dbo.rois 
WHERE camera_id = ? AND enabled = 1
```

**Columns inferred:** `roi_name`, `y1`, `x1`, `y2`, `x2`, `camera_id` (FK), `position_id` (FK, nullable), `enabled`

**Coordinate convention:** `(y1, x1, y2, x2)` — matches `Rectangle1ROI(row1, col1, row2, col2)`

### 4.3 `dbo.camera_positions`
```sql
SELECT id, camera_id, position_number, position_name
FROM dbo.camera_positions
WHERE camera_id = ? AND enabled = 1
ORDER BY position_number
```

**Columns inferred:** `id` (PK), `camera_id` (FK), `position_number`, `position_name`, `enabled`

### 4.4 `dbo.alarm_settings`
```sql
-- Per-camera (if column exists)
SELECT temperature_limit, enabled, use_max_temperature
FROM dbo.alarm_settings WHERE camera_id = ?

-- Global fallback
SELECT temperature_limit, enabled, use_max_temperature
FROM dbo.alarm_settings
```

**Columns inferred:** `temperature_limit`, `enabled`, `use_max_temperature`, `camera_id` (FK, nullable/optional)

### 4.5 `dbo.application_settings`
```sql
SELECT [key], [value] FROM dbo.application_settings
```
**Key-value store** for application config (mapped in `ConfigManager.CONFIG_TO_DB_KEY`)

---

## 5. Relationships

```
dbo.cameras (1) ─────< (N) dbo.camera_positions
    │                       │
    │                       └─< (N) dbo.rois (position_id)
    └───────────────────────< (N) dbo.rois (camera_id, legacy)
    
dbo.alarm_settings ──────────< (0..1) per camera (optional camera_id)
```

**Key constraints:**
- `camera_positions.camera_id` → `cameras.id`
- `rois.camera_id` → `cameras.id`
- `rois.position_id` → `camera_positions.id` (nullable, migrated)
- `alarm_settings.camera_id` → `cameras.id` (optional, schema-dependent)

---

## 6. Camera & Position Relationships

### 6.1 Camera Identity
- **Primary key:** `cameras.id` (integer)
- **Unique identifier:** `serial` (string, e.g., "HB25080011")
- **Display order:** `camera_number` (1, 2, 3, 4...)
- **Network:** `ip` (link-local, e.g., "169.254.91.157"), `model` (e.g., "TV46L-1-26010003@9Hz")

### 6.2 Position Scoping
- **Position identified by:** `camera_positions.id` (PK)
- **Per-camera position number:** `position_number` (1, 2, 3...)
- **ROIs belong to:** `(camera_id, position_id)` — position-scoped when schema supports it
- **Legacy fallback:** ROIs with `position_id IS NULL` apply to all positions

### 6.3 ROI-Camera-Position Mapping
```
Camera (serial) → Camera (id) → Position (id) → ROIs (enabled=1)
                    ↘ ROIs (position_id=NULL, legacy)
```

---

## 7. Alarm Configuration

### 7.1 Per-Camera vs Global
- **Per-camera:** `alarm_settings.camera_id` populated → camera-specific limits
- **Global:** Single row, no `camera_id` → applies to all cameras
- **Schema detection** at runtime determines which path used

### 7.2 Alarm Settings Columns
| Column | Type | Purpose |
|--------|------|---------|
| `temperature_limit` | float | Threshold value (°C) |
| `enabled` | bit | Global enable |
| `use_max_temperature` | bit | `1`=HIGH condition (max), `0`=LOW condition (min) |
| `camera_id` | int (nullable) | FK to cameras (optional) |

### 7.3 ConfigManager Mapping (`configuration/config.py`)
```python
CONFIG_TO_DB_KEY = {
    "camera.fps": "camera_fps",
    "camera.reconnect_seconds": "camera_reconnect_seconds",
    "camera.nuc_duration_seconds": "nuc_duration_seconds",
    "camera.nuc_grab_retry_interval_ms": "nuc_grab_retry_interval_ms",
    "camera.grab_timeout_before_reconnect_seconds": "grab_timeout_before_reconnect_seconds",
    "alarm.enabled": "alarm_enabled",
    "alarm.temperature_limit": "alarm_temperature_limit",
    "alarm.use_max_temperature": "alarm_use_max_temperature",
    "nuc.auto_enabled": "auto_nuc_enabled",
    "nuc.interval_seconds": "nuc_interval_seconds",
    "focus.default_focus_mm": "focus_default_focus_mm",
    "focus.coarse_step_mm": "focus_coarse_step_mm",
    "focus.fine_step_mm": "focus_fine_step_mm",
    "display.palette": "display_palette",
    "display.default_zoom": "display_default_zoom",
}
```
**Precedence:** SQL `application_settings` > `config.json` > built-in defaults

---

## 8. Alarm History/Events

**Not found in V2 SQL schema.** 
- `alarm/history.py` maintains **in-memory** `AlarmHistory` per `AlarmManager`
- `halcon_roi_validation.py` saves **alarm snapshots as PNG files** (`alarm_snapshots/` directory)
- No persistent SQL alarm event log in validation tool

---

## 9. Application/System Settings

### 9.1 `dbo.application_settings`
Key-value table for runtime configuration:
- **Keys:** Mapped from `ConfigManager.CONFIG_TO_DB_KEY` (see Section 7.3)
- **Values:** Stored as strings, coerced at read time (`_coerce_value()`)
- **Cached at connect** (`_app_settings` dict)

### 9.2 ConfigManager Precedence
```python
def get(section, key):
    value = config_json[section][key]
    if db.connected:
        db_key = CONFIG_TO_DB_KEY.get(f"{section}.{key}")
        if db_key:
            db_value = db.get_application_setting(db_key)
            if db_value is not None:
                return _coerce_value(db_value, value)
    return value
```

---

## 10. Recording Metadata

**Not found in V2 SQL schema.**
- `roi/recording_settings.py` exists but no SQL persistence
- Validation tool saves frames to ring buffer (`FrameBufferEntry.temp_numpy`)
- Snapshots saved as PNG files with metadata in filename

---

## 11. pyodbc Usage Patterns

### 11.1 Connection
```python
conn = pyodbc.connect(
    conn_str,
    timeout=DB_CONNECTION_TIMEOUT,
    autocommit=True
)
```

### 11.2 Read Queries (`_fetch_all()`)
```python
with conn.cursor() as cursor:
    cursor.execute(query, params)
    columns = [d[0] for d in cursor.description]
    rows = cursor.fetchall()
    return [dict(zip(columns, row)) for row in rows]
```

### 11.3 Error Handling
```python
try:
    return self._fetch_all(query, params)
except Exception as exc:
    self.last_error = str(exc)
    logger.exception("SQL query failed")
    return []
```

### 11.4 Credential Sanitization
```python
def _sanitize_sql_error(exc):
    message = str(exc)
    for secret in (DB_PASSWORD, DB_FALLBACK_PASSWORD):
        if secret:
            message = message.replace(secret, "***")
    return message
```

---

## 12. Production Code vs Validation Tool Code

| Component | Production App | Validation Tool (`halcon_roi_validation.py`) |
|-----------|----------------|---------------------------------------------|
| **Camera Discovery** | `app/application.py:_discover_and_register_cameras()` (inline HALCON) | `CameraDiscovery` class (reused) |
| **ROI Loading** | `roi/persistence/repository.py` (JSON files) | `DatabaseRepository.load_rois()` (SQL) |
| **Position Loading** | `roi/persistence/repository.py` (JSON) | `DatabaseRepository.load_camera_positions()` (SQL) |
| **Alarm Config** | `ConfigManager` → `application_settings` (SQL) | `ConfigManager` + `DatabaseRepository.load_alarm_settings()` (SQL) |
| **Camera Config** | `ConfigManager` (config.json + SQL) | `ConfigManager` (config.json + SQL) |
| **Alarm Evaluation** | `alarm/manager.py` + `ObservationRuntime` | `CameraWorker._alarm_manager` (simplified) |
| **Frame Persistence** | Not implemented | Ring buffer + PNG snapshots |

**Key divergence:** Production uses **JSON file repository** for ROIs; Validation tool uses **SQL Server** directly.

---

## 13. Setup Environment (`setup_environment.py`)

### 13.1 Database Initialization
```python
def initialize_database():
    # Creates thermal_monitor.db (SQLite) for local dev
    # Schema: cameras, camera_positions, rois, alarm_settings, application_settings
    # Not SQL Server — local SQLite for development
```

### 13.2 SQL Scripts
- `database/schema.sql` — SQLite schema for local dev
- No SQL Server migration scripts found in V2

---

## 14. Test Database Usage

| Test | Database Used |
|------|---------------|
| `test_roi_persistence.py` | JSON repository (files) |
| `test_roi_engine_integration.py` | JSON repository (tmp_path) |
| `test_validation_harness.py` | SQLite (`thermal_monitor.db`) |
| `halcon_roi_validation.py` | SQL Server (production) |

---

## 15. V2 Database Knowledge Worth Carrying into V3

### Schema Patterns
1. **Camera-centric hierarchy** — `cameras` → `positions` → `rois` with `camera_id` FK everywhere
2. **Position-scoped ROIs** — `position_id` on `rois` enables per-PTZ-position ROI sets
3. **Legacy fallback** — `position_id NULL` ROIs apply globally (migration-friendly)
4. **Alarm settings** — Optional `camera_id` supports both global and per-camera limits
5. **Application settings as KV** — `application_settings` table for config override

### Connection Patterns
6. **Driver resolution** — Ordered candidate list, pick first installed
7. **Dual-auth strategy** — Windows Auth primary, SQL Auth fallback
8. **Per-attempt timeout** — 5s prevents hang on dead server
9. **Schema detection** — Read-only probes for optional columns/tables
10. **Credential sanitization** — Passwords scrubbed from error logs

### Configuration
11. **Three-tier config** — SQL > JSON > defaults (ConfigManager)
12. **Environment variable overrides** — Server, database, auth, credentials
13. **Fallback server** — Separate credentials for disaster recovery

### Validation Tool Proven Patterns
14. **Ring buffer for alarm snapshots** — `deque(maxlen=30)` of `FrameBufferEntry` (temp + stats)
15. **2-second re-trigger suppression** — Per `(camera, position, roi)` key
16. **Background snapshot worker** — Separate thread for PNG saving
17. **GigE stream stats** — `[Stream]GevStream*` counters with prefix fallback

---

## 16. V2 Database Problems That V3 Must Avoid

| Problem | V2 Evidence | V3 Fix |
|---------|-------------|--------|
| **Two ROI persistence paths** | JSON (prod) vs SQL (validation) | Single source of truth (SQL) |
| **No migration system** | `roi/persistence/migration/` exists but only v1; no runner | Versioned migrations with runner |
| **No alarm event persistence** | In-memory `AlarmHistory` only | `alarm_events` table with indexes |
| **No recording metadata** | Missing entirely | `recordings` table + file metadata |
| **Hardcoded fallback server** | `DESKTOP-4L5G45H` in validation tool | Configurable via env |
| **Schema detection at runtime** | `_detect_*` probes on every connect | Migration version table |
| **Credential in connection string** | `PWD=` in plain text | Key vault / secret manager |
| **No connection pooling** | New connection per `DatabaseRepository` | Pool (e.g., `pyodbc` pooling or SQLAlchemy) |
| **Autocommit=True everywhere** | No transaction support | Explicit transactions for writes |
| **SQLite for dev, SQL Server for prod** | `setup_environment.py` creates SQLite | Same engine (SQL Server LocalDB or container) |

---

## 17. Exact File/Function Reference

| Component | File | Key Functions |
|-----------|------|---------------|
| **Validation DB** | `halcon_roi_validation.py` | `DatabaseRepository` (connect, _fetch_all, load_cameras, load_rois, load_camera_positions, load_alarm_settings, load_application_settings, _detect_*) |
| **Config Manager** | `configuration/config.py` | `ConfigManager.get()`, `_coerce_value()`, `CONFIG_TO_DB_KEY` |
| **Settings** | `configuration/settings.py` | `Settings` dataclass (DB_* constants in validation tool) |
| **ROI Persistence** | `roi/persistence/repository.py` | `JSONROIRepository.save_all()`, `load_all()`, `save()` |
| **ROI Schema** | `roi/persistence/schema.py` | `ROISchema`, `ProjectSchema` |
| **Alarm Settings** | `roi/alarm_settings.py` | `ROIAlarmSettings`, `ROIAlarmCondition` |
| **Setup** | `setup_environment.py` | `initialize_database()`, `create_schema()` |
| **Observation Runtime** | `observation/runtime.py` | `ObservationRuntime.add_camera()`, `_load_position()` |

---

(End of database reference report)