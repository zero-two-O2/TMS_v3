# V3 Foundation Architecture

This document describes the V3 application foundation architecture built around stable interfaces, independent of the final TV46L acquisition implementation.

## Overview

The V3 foundation provides a camera-independent architecture where all application domains (Configuration, Observer, Offline, ROI, PTZ, Alarm, Processing, Recording, Storage) can be developed and tested without requiring:
- Real TV46L hardware
- HALCON runtime
- GVSP
- Shared-memory transport
- GPU
- GUI

## Domain Boundaries

```
┌─────────────────────────────────────────────────────────────────┐
│                        UI (PyQt6)                                │
│  Configuration UI  │  Observer UI  │  Offline UI                │
└──────────────────────────┬──────────────────────────────────────┘
                           │ Calls services
┌──────────────────────────▼──────────────────────────────────────┐
│                     Application Services                         │
│  ModeService  │  ConfigurationService  │  AnalysisService       │
│  AlarmService │  RecordingService      │  OfflineService        │
└──────────────────────────┬──────────────────────────────────────┘
                           │ Uses domain models
┌──────────────────────────▼──────────────────────────────────────┐
│                        Core Domain Models                          │
│  core.models.camera    core.models.inspection    core.models.system │
└──────────────────────────┬──────────────────────────────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│  Processing   │  │   Storage     │  │   Offline     │
│  Pipeline     │  │  Repositories │  │  FrameSource  │
│  FrameSource  │  │  SQL Server   │  │  Recording    │
└───────────────┘  └───────────────┘  └───────────────┘
          │                │                │
          ▼                ▼                ▼
┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│  Camera       │  │  Database     │  │  Recording    │
│  Acquisition  │  │  (SQL Server) │  │  Files        │
└───────────────┘  └───────────────┘  └───────────────┘
```

## Application Modes

### Configuration Mode
- Live camera operation allowed
- Camera configuration allowed
- PTZ configuration allowed
- ROI editing allowed
- System configuration allowed
- PTZ manual control allowed

### Observer Mode
- Live camera operation allowed
- Observation only
- ROI editing disabled
- Configuration editing disabled
- PTZ manual configuration disabled
- Alarm observation allowed

### Offline Mode
- No camera connection required
- No live acquisition
- Saved recordings/data are the frame source
- Analysis still possible
- Playback possible
- Camera/PTZ hardware operations unavailable

**Transitions**: All modes can transition to each other except self-transitions. Configuration ↔ Observer bidirectional. Both can go to Offline. Offline can return to either.

## Frame Contract (ADR-002)

The immutable `Frame` is the primary data object shared between acquisition, processing, recording, diagnostics, and UI.

```
Frame
├── FrameDescriptor (metadata, identity, payload references)
│   ├── camera_id: str
│   ├── sequence: int (monotonic per camera)
│   ├── timestamp: float (wall clock)
│   ├── monotonic_timestamp: float (perf_counter)
│   ├── thermal: StreamMetadata
│   ├── visible: StreamMetadata
│   ├── sync: SyncInfo
│   └── metadata: Mapping
└── FramePayload (bulk pixel data - read-only NumPy arrays)
    ├── thermal: np.ndarray | None
    └── visible: np.ndarray | None
```

**Key principles**:
- Raw frames are immutable acquisition data
- Derived data (ROI, temperature, alarm, display) stays outside the frame
- Same frame contract for live, shared-memory, and offline sources

## Processing Pipeline

```
FrameSource (protocol)
    │
    ▼
ProcessingPipeline (abstract)
    │
    ├── SimpleProcessingPipeline (CPU reference)
    │   ├── CalibrationProvider (protocol)
    │   └── TemperatureConverter (protocol)
    │
    ▼
AnalysisResult
    │
    ▼
AlarmEvaluator
    │
    ▼
AlarmEvent
```

**FrameSource implementations**:
- `LiveFrameSource` - wraps acquisition publisher
- `OfflineFrameSource` - loads from recording files
- `SyntheticFrameSource` - generates test frames

## Alarm System

```
AnalysisResult
    │
    ▼
AlarmEvaluator (per camera)
    │
    ├── AlarmStateTracker (tracks active alarms)
    │
    ▼
AlarmEvaluationResult
    ├── events: AlarmEvent[]
    ├── active_alarms: rule_ids
    └── cleared_alarms: rule_ids
```

**Alarm rules support**:
- Threshold: ABOVE, BELOW
- Range: OUTSIDE_RANGE, INSIDE_RANGE
- Rate of change: RATE_OF_CHANGE
- Severity: INFO, WARNING, CRITICAL

## Recording System

```
AlarmEvent
    │
    ▼
Recorder (per camera)
    ├── RollingFrameBuffer (pre-alarm history)
    │
    ▼
RecordingSink (protocol)
    ├── FileRecordingSink (.tmsrec format)
    └── NullRecordingSink (testing)
```

**Recording metadata** preserves:
- Camera ID, recording ID, timestamps, sequences
- PTZ position, ROI configuration hash
- Alarm event information
- File path, frame count, duration

## SQL Server Storage

```
Repositories (per domain)
    │
    ├── CameraRepository
    ├── ROIRepository
    ├── PositionROIRepository
    ├── AlarmRuleRepository
    ├── AnalysisConfigRepository
    ├── AlarmEventRepository
    ├── RecordingRepository
    ├── RecordingConfigRepository
    └── SystemConfigRepository
```

**Schema** (database/migrations/001_initial_schema.sql):
- `cameras` - Camera identity, config, PTZ settings
- `rois` - ROI geometry, temperature limits
- `position_roi_associations` - PTZ position ↔ ROI mapping
- `alarm_rules` - Alarm rule definitions
- `analysis_configs` - Per-camera analysis settings
- `alarm_events` - Alarm event history
- `recordings` - Recording metadata
- `recording_configs` - Per-camera recording settings
- `system_config` - Application key-value store

## Offline Architecture

```
LIVE:                    OFFLINE:
TV46L                    Saved Raw Data
   │                        │
   ▼                        ▼
FrameSource            OfflineFrameSource
   │                        │
   └──────────┬─────────────┘
              ▼
         ProcessingPipeline (SAME)
              │
              ▼
         AnalysisResult
```

OfflineFrameSource loads frames from `.tmsrec` files and implements the same `FrameSource` protocol as live sources.

## Services Layer

| Service | Responsibility |
|---------|----------------|
| `ModeService` | Mode transitions, capability checking |
| `ConfigurationService` | Camera, analysis, recording, system config |
| `AnalysisService` | Processing pipelines, frame sources |
| `AlarmService` | Alarm evaluation, state tracking |
| `RecordingService` | Recorders, alarm-triggered recording |
| `OfflineService` | Playback sessions, synthetic sources |

## Acquisition Boundary

The acquisition implementation (HALCON vs native GVSP) is isolated behind:
- `FrameSource` protocol (for frame consumption)
- `FramePublisher` protocol (for frame publication)
- `CameraConfig` in `camera.model` (acquisition-specific tuning)

The rest of the application never depends on HALCON or GVSP.

## GPU Boundary

GPU acceleration can be added later by implementing:
- `GPUProcessingBackend` implementing `ProcessingPipeline`
- `GPUTemperatureConverter` implementing `TemperatureConverter`

No domain models need to change.

## Python Environment

- Python >=3.10,<3.11
- Dependencies in `pyproject.toml`
- No global package dependencies
- HALCON documented separately (vendor installation)
- Unit tests run without HALCON

## Test Strategy

- 241 tests (226 passing, 15 skipped for pyodbc)
- All tests use synthetic frames
- No hardware, HALCON, SQL Server, GPU, or GUI required
- Integration tests separated (require pyodbc)

## Files Created

### Core
- `src/thermal_monitor/core/modes.py` - ApplicationMode, ModeManager, ModeCapabilities
- `src/thermal_monitor/core/models/camera.py` - CameraIdentity, CameraConfig, PTZConfig, CameraStatus
- `src/thermal_monitor/core/models/inspection.py` - ROIConfig, ROIGeometry, TemperatureLimits, AlarmRule, AlarmEvent, AnalysisConfig, AnalysisResult
- `src/thermal_monitor/core/models/system.py` - SystemConfig, SystemStatus, RecordingConfig, RecordingMetadata

### Processing
- `src/thermal_monitor/processing/pipeline.py` - ProcessingPipeline, SimpleProcessingPipeline, FrameSource, CalibrationProvider, TemperatureConverter
- `src/thermal_monitor/processing/sources.py` - LiveFrameSource, OfflineFrameSource, SyntheticFrameSource
- `src/thermal_monitor/processing/alarms.py` - AlarmEvaluator, AlarmStateTracker, AlarmEvaluationResult

### Storage
- `src/thermal_monitor/storage/recording.py` - Recorder, RollingFrameBuffer, RecordingSink, FileRecordingSink
- `src/thermal_monitor/storage/database.py` - Database, DatabaseConfig, run_migrations
- `src/thermal_monitor/storage/repositories/` - Camera, ROI, PositionROI, AlarmRule, AnalysisConfig, AlarmEvent, Recording, RecordingConfig, SystemConfig repositories

### Services
- `src/thermal_monitor/services/mode.py` - ModeService
- `src/thermal_monitor/services/configuration.py` - ConfigurationService
- `src/thermal_monitor/services/analysis.py` - AnalysisService
- `src/thermal_monitor/services/alarm.py` - AlarmService
- `src/thermal_monitor/services/recording.py` - RecordingService
- `src/thermal_monitor/services/offline.py` - OfflineService

### Migrations
- `database/migrations/001_initial_schema.sql` - Complete V3 schema