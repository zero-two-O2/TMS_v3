# TMS V3 Architecture Audit - Stage 7I

## Current Window Architecture

**File**: `src/thermal_monitor/ui/main_window.py` (MainWindow class)

- Single `QMainWindow` with `QStackedWidget` central widget
- 4 mode widgets added to stack:
  - Index 0: `LauncherWidget` (LAUNCHER)
  - Index 1: `LiveModeWidget` (LIVE)
  - Index 2: `ConfigurationModeWidget` (CONFIGURATION)
  - Index 3: `OfflineModeWidget` (OFFLINE)
- Mode switching via toolbar buttons and menu bar
- `ModeService` notifies `MainWindow` of mode changes
- `MainWindow._update_ui_for_mode()` switches stacked widget index

**Problem**: All modes are pages in one window. Requirement is separate top-level windows.

## Current Mode Architecture

**Files**: 
- `src/thermal_monitor/core/modes.py` - `ApplicationMode`, `ModeManager`, `ModeCapabilities`
- `src/thermal_monitor/services/mode.py` - `ModeService`

- 4 modes: `LAUNCHER`, `LIVE`, `CONFIGURATION`, `OFFLINE`
- `ModeCapabilities` matrix defines per-mode permissions
- Valid transitions (from `ModeManager._VALID_TRANSITIONS`):
  - LAUNCHER → LIVE, CONFIGURATION, OFFLINE
  - LIVE → CONFIGURATION, OFFLINE
  - CONFIGURATION → LIVE, OFFLINE
  - OFFLINE → LIVE, CONFIGURATION
- Mutual exclusion LIVE ↔ CONFIGURATION only enforced in `MainWindow` toolbar buttons (`_request_live_mode`, `_request_configuration_mode`), not at service level

## Current Configuration Implementation

**File**: `src/thermal_monitor/ui/modes/configuration.py` - `ConfigurationModeWidget`

- Tabbed interface with 8 tabs: Identity, ROIs, PTZ Positions, Alarms, Recording, Calibration, System
- Camera selector at top (combo box + prev/next buttons)
- Camera identity tab: read-only fields from discovery (serial, model, vendor, firmware, IP, device ID)
- Editable fields: name, description, enabled, thermal/visible enabled, acquisition settings (FPS, reconnect, NUC, grab timeout), display settings (palette, zoom)
- ROI tab: position selector, ROI tree, ROI editor with geometry + temperature limits + alarm enabled
- PTZ/Alarm/Recording/Calibration/System tabs: mostly stubbed with "TODO"
- Uses `ConfigurationService` for config persistence and change callbacks

## Existing ROI/Analysis Capabilities

**Files**:
- `src/thermal_monitor/core/models/inspection.py` - `ROIShape` (RECTANGLE1, RECTANGLE2, CIRCLE, ELLIPSE, POLYGON), `ROIGeometry`, `ROIConfig`, `TemperatureLimits`, `AlarmRule`, `AlarmCondition`, `AlarmSeverity`, `AnalysisConfig`, `PositionROIAssociation`
- `src/thermal_monitor/processing/pipeline.py` - `SimpleProcessingPipeline` with HALCON ROI statistics
- `src/thermal_monitor/processing/alarms.py` - `AlarmEvaluator`, `AlarmStateTracker`
- `src/thermal_monitor/services/analysis.py` - `AnalysisService`
- `src/thermal_monitor/services/alarm.py` - `AlarmService`

**Capabilities**:
- 5 ROI shapes with HALCON-backed statistics (min, max, mean, std dev, range)
- Temperature limits per ROI (min/max warning/critical, rate of change)
- Alarm rules per ROI (ABOVE, BELOW, OUTSIDE_RANGE, INSIDE_RANGE, RATE_OF_CHANGE)
- Alarm severities: INFO, WARNING, CRITICAL
- Position-based ROI associations (PTZ positions)
- Alarm state tracking (active/cleared events)

## Existing Camera Lifecycle Ownership

**File**: `src/thermal_monitor/services/runtime.py` - `CameraRuntimeService`

- Owns per-camera `CameraRuntime` dataclass containing:
  - `driver_config` (mapped from app config)
  - `ring` (SharedMemoryRingBuffer)
  - `publisher` (FramePublisher)
  - `worker` (AcquisitionWorker)
  - `observer` (ObserverService) - optional
  - `recording` (RecordingConsumer) - optional
- Methods:
  - `start_camera(config)` - creates full producer path, verifies ACQUIRING
  - `stop_camera(camera_id)` - deterministic teardown: observer → recording → worker → ring
  - `start_observer(camera_id, analysis_config)` - attaches ObserverService to running camera's ring
  - `stop_observer(camera_id)` - stops observer, acquisition continues
  - `start_recording/stop_recording` - optional recording consumer
  - `is_camera_running`, `camera_stats`, `observer_service`, `running_camera_ids`

**Key Principle**: GUI never touches TV46LDriver, HALCON, AcquisitionWorker, SHM ring, RecordingConsumer internals.

## Existing Live Image Path

**Flow**: TV46L → AcquisitionWorker → SHM Ring → ProcessingConsumer → ObserverService.result_ready (Qt queued signal) → CameraTile.on_result → LiveThermalWidget.set_frame

- `LiveCameraTile` (in `live.py`) and `CameraTile` (in `observer.py`) both use `LiveThermalWidget`
- `LiveThermalWidget` copies temperature buffer before constructing QImage (critical for thread safety)
- `ProcessingResult` contains: frame, analysis_result, alarm_result, processing_time_ms, temperature_image

## What Can Be Reused Unchanged

1. **Backend Services** (no changes needed):
   - `CameraRuntimeService` - full lifecycle management
   - `CameraDiscoveryService` - one-time discovery
   - `ConfigurationService` - config management with callbacks
   - `ObserverService` - consumer bridge to GUI
   - `OfflineService` - offline playback sessions
   - `AnalysisService` / `AlarmService` - processing coordination
   - `SimpleProcessingPipeline` - frame processing
   - `AlarmEvaluator` - alarm evaluation

2. **Data Models** (no changes needed):
   - All `core.models` (CameraConfig, AnalysisConfig, ROIConfig, AlarmRule, etc.)
   - `core.frame` (Frame, FrameDescriptor, FramePayload)
   - `core.modes` (ApplicationMode, ModeCapabilities)

3. **UI Components** (can be reused/adapted):
   - `LiveThermalWidget` - safe thermal image display
   - `LiveCameraTile` / `CameraTile` - tile UI with stats
   - `ROIEditorWidget` - ROI geometry + limits editor
   - `OfflineImageWidget` - offline frame display

4. **Processing Path** (no changes):
   - SHM ring buffer
   - ProcessingConsumer
   - Temperature conversion pipeline
   - HALCON ROI statistics

## Exact Files That Need Modification

### New Files to Create:
1. `src/thermal_monitor/ui/windows/launcher_window.py` - LauncherWindow (QMainWindow)
2. `src/thermal_monitor/ui/windows/live_window.py` - LiveWindow (QMainWindow)
3. `src/thermal_monitor/ui/windows/configuration_window.py` - ConfigurationWindow (QMainWindow)
4. `src/thermal_monitor/ui/windows/offline_window.py` - OfflineWindow (QMainWindow)
5. `src/thermal_monitor/ui/controller.py` - AppController (window lifecycle manager)
6. `src/thermal_monitor/ui/windows/__init__.py`

### Files to Modify:
1. `src/thermal_monitor/ui/app.py` - Replace MainWindow with AppController
2. `src/thermal_monitor/services/mode.py` - Add mutual exclusion logic (Live ↔ Configuration)
3. `src/thermal_monitor/core/modes.py` - Update transition matrix if needed
4. `src/thermal_monitor/ui/modes/launcher.py` - Adapt LauncherWidget for LauncherWindow
5. `src/thermal_monitor/ui/modes/live.py` - Adapt LiveModeWidget for LiveWindow
6. `src/thermal_monitor/ui/modes/configuration.py` - Major redesign for ThermoView-style layout
7. `src/thermal_monitor/ui/modes/offline.py` - Adapt OfflineModeWidget for OfflineWindow

### Files to Potentially Remove/Deprecate:
- `src/thermal_monitor/ui/main_window.py` - Replaced by separate windows
- `src/thermal_monitor/ui/modes/observer.py` - Legacy observer mode (not in 3-mode design)

## Genuine Backend Gaps Identified

1. **Configuration Window Live Feed**: Needs to subscribe to `ObserverService.result_ready` for selected camera (similar to Live tiles). No new backend needed - just wire existing `ObserverService` from `CameraRuntimeService`.

2. **Acquisition Controls in Configuration**: CameraConfig.metadata already has frame_rate, reconnect_interval_s, nuc_duration_s, grab_timeout_ms. Need to expose these in UI and persist via ConfigurationService.

3. **Thermal Display Controls**: Palette selection exists in CameraConfig.metadata. Auto/manual range needs temperature min/max from analysis_result. Zoom/fit-to-window is pure UI.

4. **PTZ Configuration**: PTZConfig model exists but PTZ tab is stubbed. Driver-level PTZ not yet implemented in TV46LDriver.

5. **Camera Switching in Configuration**: Need to stop previous camera's observer and start new camera's observer via CameraRuntimeService.

## Proposed New Window Ownership/Lifecycle

```
ThermalMonitorApp (QApplication wrapper)
    │
    ├── AppController (owns window lifecycle)
    │       │
    │       ├── LauncherWindow (QMainWindow) - starts maximized, auto-discovery
    │       ├── LiveWindow (QMainWindow) - fixed 8-camera wall, owns LiveModeWidget
    │       ├── ConfigurationWindow (QMainWindow) - ThermoView layout, owns ConfigurationModeWidget
    │       └── OfflineWindow (QMainWindow) - independent, owns OfflineModeWidget
    │
    └── Shared Services (injected into windows):
            CameraDiscoveryService
            CameraRuntimeService
            ConfigurationService
            OfflineService
            ModeService (with mutual exclusion)
```

**Mutual Exclusion Rules** (enforced by AppController):
- LIVE + CONFIGURATION = FORBIDDEN (modal warning)
- LIVE + OFFLINE = ALLOWED
- CONFIGURATION + OFFLINE = ALLOWED
- OFFLINE alone = ALLOWED

**Window Lifecycle**:
- App starts → LauncherWindow.showMaximized()
- Launcher → LIVE: LauncherWindow.hide(), LiveWindow.showMaximized()
- Live closes → LiveWindow.close(), LauncherWindow.showMaximized()
- Launcher → CONFIGURATION: LauncherWindow.hide(), ConfigurationWindow.showMaximized()
- Config closes → ConfigurationWindow.close(), LauncherWindow.showMaximized()
- Launcher → OFFLINE: OfflineWindow.showMaximized() (Launcher stays or hides)
- Offline closes → OfflineWindow.close() (Launcher unaffected)