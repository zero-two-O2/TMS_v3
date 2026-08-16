# ADR-001: V3 Application Architecture

## Status

Accepted

## Date

2026-08-16

## Context

Thermal Monitoring System V2 became difficult to maintain because responsibilities were spread across too many files and duplicate implementations existed.

V3 will be redesigned from scratch.

The system must support:

- Up to 8 TV46L cameras.
- Thermal and visible feeds.
- Configuration and Observer operation.
- Offline analysis of saved raw camera data.
- PTZ control.
- Position-dependent ROIs.
- Temperature measurement.
- Alarm processing.
- Alarm-triggered raw recording.
- SQL Server.
- Shared-memory frame transport.
- Multiple developers working concurrently.
- Reproducible development environments.

## Primary Application Modes

V3 has two primary operating modes.

### Configuration Mode

Configuration Mode is used to configure and test the system.

Responsibilities include:

- Camera configuration.
- Camera connection.
- Camera controls.
- PTZ movement.
- Position configuration.
- ROI creation and editing.
- Alarm configuration.
- System configuration.
- Calibration.
- Diagnostics and testing.

Configuration Mode and Observer Mode must not be active simultaneously.

### Observer Mode

Observer Mode is the operational monitoring mode.

Responsibilities include:

- Displaying the feeds from up to 8 cameras.
- Displaying the current camera position.
- Loading the ROIs associated with the current position.
- Performing temperature analysis.
- Displaying alarm states.
- Triggering alarm recording.
- Showing system/camera status.

Observer Mode is not intended for configuration.

## Offline Analysis

Offline analysis is not a third primary application mode.

It is a data-source workflow that uses a configuration-style GUI to inspect previously saved camera data.

Offline operation must not require:

- Camera connection.
- Camera discovery.
- Live acquisition.
- PTZ hardware.

Offline data must be processed by the same processing/analysis system used by live operation wherever practical.

The conceptual architecture is:

Live:

Camera → Frame Source → Processing

Offline:

Saved Raw Data → Frame Source → Processing

The processing layer must not need to know whether a frame came from a live camera or a saved recording.

## Major Application Domains

V3 will initially contain six major application domains.

### Core

Owns system-wide functionality.

Examples:

- Application lifecycle.
- Application state.
- Commands.
- Events.
- Common data models.
- Logging.
- Validated application configuration.

Core must not contain camera-specific, GUI-specific, or database-specific implementation.

### Camera

Owns hardware interaction.

Examples:

- TV46L camera.
- HALCON acquisition.
- Camera connection.
- Camera reconnection.
- Thermal acquisition.
- Visible acquisition.
- Camera controls.
- NUC.
- Focus.
- PTZ hardware interface.

Camera code must not contain GUI logic, database queries, ROI analysis, or alarm logic.

### Processing

Owns analysis of acquired frames.

Examples:

- Calibration.
- Raw-to-temperature conversion.
- ROI processing.
- Statistics.
- Temperature analysis.
- Alarm evaluation.

Processing must operate on frame data and configuration/state supplied through defined interfaces.

Processing must not directly control cameras or update GUI widgets.

### Storage

Owns persistence.

Examples:

- SQL Server access.
- Database repositories.
- Recording metadata.
- Raw recording files.
- File storage.

Application modules must not scatter SQL queries throughout the codebase.

### Offline

Owns offline data access.

Examples:

- Reading raw recordings.
- Playback.
- Frame seeking.
- Offline sessions.
- Offline frame source.

Offline must not contain duplicate temperature/ROI algorithms.

### UI

Owns PyQt6 presentation.

Examples:

- Configuration interface.
- Observer interface.
- Offline interface.
- Widgets.
- Visualization.

UI must not directly perform camera acquisition, heavy processing, or database operations.

## Frame Ownership

Camera acquisition produces immutable frame data.

The frame is the primary data object shared between acquisition, processing, recording, diagnostics, and UI.

The initial frame model must support:

- Camera ID.
- Sequence number.
- Timestamp.
- Thermal raw data.
- Visible data.
- Thermal metadata.
- Visible metadata.
- IR/visible synchronization information.
- Acquisition status.

Derived information such as:

- ROI definitions.
- Temperature results.
- Alarm state.
- GUI overlays.

must not be embedded into the raw frame.

## Data Flow

The intended high-level data flow is:

Camera
→ Acquisition
→ Frame Transport
→ Processing / Recording / Diagnostics / UI

The GUI must consume results or frame data.

The GUI must never be responsible for controlling the acquisition loop.

## Shared Memory

V3 will use shared-memory-based frame transport for high-rate frame delivery.

The exact implementation is intentionally not defined by this ADR.

The shared-memory design will be specified separately after the frame model and actual camera data sizes/rates are established.

The design should support:

- Multiple consumers.
- Minimal frame copying.
- Bounded memory usage.
- Latest-frame access.
- Frame sequence tracking.
- Dropped-frame monitoring.
- Independent consumers.

## Alarm Recording

Recording is primarily an alarm-evidence mechanism.

Recording will save raw camera frames and important contextual information.

Recorded information should include, where applicable:

- Camera ID.
- Timestamp.
- Frame sequence.
- Thermal raw data.
- Visible data.
- Camera position.
- ROI configuration.
- Alarm configuration.
- Alarm event information.
- Relevant camera metadata.
- IR/visible synchronization information.

Alarm recording should support pre-alarm and post-alarm data where practical.

## Database

SQL Server is the only supported application database.

SQL queries must be isolated behind the storage/data-access layer.

SQL Server will store persistent system information such as:

- Camera configuration.
- Camera identity.
- Positions.
- PTZ configuration.
- ROIs.
- Alarm configuration.
- Alarm history/events.
- System configuration.
- Recording metadata.

Large raw camera recordings will be stored on the filesystem rather than directly inside SQL Server unless a later architectural decision changes this.

## Repository Structure

V3 will intentionally use a small number of major domains.

Initial structure:

    src/
        thermal_monitor/
            core/
            camera/
            processing/
            storage/
            offline/
            ui/

Additional directories may be introduced only when a clear responsibility requires them.

The project must avoid unnecessary:

- Managers.
- Factories.
- Services.
- Adapters.
- Providers.
- Interfaces.
- Utility modules.

A new file should represent a meaningful independent responsibility.

## Modularity

Modules must communicate through stable data models, commands, events, and interfaces.

The architecture should allow different developers to work independently on:

- Camera.
- Processing.
- Storage.
- Offline.
- UI.

without requiring developers to modify unrelated modules.

## Environment

The Python virtual environment is local to each development machine.

`.venv/` must never be committed to Git.

The repository will contain:

- Python version requirements.
- Dependency definitions.
- Locked dependency versions.
- Environment setup scripts.
- Environment verification scripts.
- `.env.example`.

Machine-specific secrets and configuration must not be committed.

## Git Rules

The repository must not contain:

- `.venv`.
- Secrets.
- `.env`.
- Runtime logs.
- Recordings.
- Camera captures.
- Generated reports.
- Local database files.
- Machine-specific generated artifacts.

SQL Server database schema must be version-controlled through SQL migration scripts.

## Architectural Principles

1. Acquisition must be independent of GUI rendering.
2. Processing must not block acquisition.
3. GUI must not perform heavy processing.
4. Raw frames must remain immutable.
5. Live and offline processing should use the same processing pipeline.
6. Camera failures must not unnecessarily stop other cameras.
7. SQL access must be isolated.
8. Configuration must have a controlled source of truth.
9. Shared-memory transport must avoid unnecessary frame copies.
10. V3 must remain modular without unnecessary file fragmentation.
11. Machine-specific environments must never be committed.
12. Important architectural decisions must be documented.