# ADR-002: V3 Frame Model

## Status

Proposed

## Date

2026-08-16

## Context

V3 must support:

- Up to 8 TV46L cameras.
- Thermal/IR data.
- Visible data.
- Full-rate acquisition.
- IR/visible synchronization.
- Shared-memory frame transport.
- Alarm-triggered raw recording.
- Offline analysis of saved raw data.
- Multiple independent consumers of the same acquired data.

V2 did not establish one consistent frame/data model. V3 must define the frame contract before implementing acquisition, shared memory, processing, recording, or offline playback.

## Frame Principle

The V3 frame is the immutable representation of acquired camera data.

A frame contains acquired data and acquisition metadata.

It must not contain processing results or GUI state.

## Logical Frame Structure

A V3 frame contains:

    Frame
    ├── camera_id
    ├── sequence_number
    ├── timestamp
    ├── thermal
    ├── visible
    ├── synchronization
    └── metadata

## Camera Identity

`camera_id` identifies the camera that produced the frame.

The camera identity should be stable across IP address changes.

The preferred identity is the camera serial number or another hardware-unique identifier.

IP address must not be treated as the permanent identity of a camera.

## Sequence Number

Every acquired frame must have a monotonically increasing sequence number within its camera stream.

Purpose:

- Detect dropped frames.
- Detect duplicated frames.
- Detect acquisition interruptions.
- Correlate frames between subsystems.
- Diagnose performance problems.
- Correlate recorded data.

The sequence number must be assigned by the acquisition layer.

## Timestamp

Every frame must contain a timestamp generated as close to acquisition as practical.

The timestamp must use a consistent time representation throughout the application.

The timestamp is required for:

- Recording.
- Alarm events.
- Offline playback.
- IR/visible synchronization.
- Diagnostics.
- Performance measurement.

## Thermal Data

The thermal component must preserve the raw thermal/radiometric data obtained from the camera.

The raw thermal data must not be converted to an 8-bit display image before storage or transport.

Derived temperature images are processing results and are not the original raw frame.

The frame must retain enough information to allow offline processing using the same analysis pipeline as live operation.

## Visible Data

The frame may contain the corresponding visible-camera data.

Visible data must remain separate from thermal data.

The visible component should contain its own:

- Image data.
- Timestamp.
- Sequence information where available.
- Metadata.

The architecture must not assume that thermal and visible frames are inherently identical in timing.

## IR/Visible Synchronization

Thermal and visible frames must be treated as separate streams that can be associated.

The synchronization information should allow the system to determine the relationship between the two frames.

Conceptually:

    Frame
    ├── Thermal
    │   ├── data
    │   ├── timestamp
    │   └── sequence
    │
    ├── Visible
    │   ├── data
    │   ├── timestamp
    │   └── sequence
    │
    └── Synchronization
        ├── time_delta
        └── status

Possible synchronization states include:

- SYNCHRONIZED
- ACCEPTABLE
- DEGRADED
- MISSING_THERMAL
- MISSING_VISIBLE
- UNKNOWN

The exact synchronization tolerance will be determined from actual camera behavior and hardware testing.

## Metadata

Metadata may include information required to interpret the frame.

Examples:

- Camera model.
- Sensor information.
- Resolution.
- Pixel format.
- Acquisition mode.
- Exposure information where applicable.
- Camera configuration identifiers.
- Hardware timestamps where available.
- Acquisition status.

Metadata must not be used as a replacement for proper application state or database configuration.

## Derived Data

The following are NOT part of the raw frame:

- ROI definitions.
- ROI temperature values.
- Maximum temperature.
- Minimum temperature.
- Average temperature.
- Alarm state.
- Alarm event.
- Colormap.
- GUI overlay.
- Display image.
- PTZ commands.
- GUI state.

These are derived or external state.

## Processing

The processing pipeline consumes frames.

Conceptually:

    Frame
      │
      ├── Calibration
      │
      ├── Temperature Conversion
      │
      ├── ROI Processing
      │
      ├── Statistics
      │
      └── Alarm Evaluation

Processing produces separate result objects.

The processing layer must not modify the original raw frame.

## Frame and Position

A frame may be associated with the camera's position at acquisition time.

However, position is configuration/state and not part of the immutable camera pixel data.

The system must capture the relevant position information when recording an alarm event.

At minimum, recording context should preserve:

- Position identifier.
- Pan.
- Tilt.
- Zoom where applicable.

## Frame and ROI

ROIs are configuration associated with a camera position.

The frame itself does not permanently contain the ROI definitions.

Processing obtains the applicable ROI configuration based on:

    Camera
      +
    Current Position
      +
    Configuration Version

This allows the same raw frame to be analyzed later using the configuration that existed at the time of acquisition or using a different configuration during offline analysis.

## Frame Immutability

Once a frame has been published by acquisition, its raw data must be treated as immutable.

Consumers must not modify the shared raw data.

If a consumer needs a modified representation, it creates a derived buffer.

Examples:

    Raw Thermal
        ↓
    Temperature Array

    Raw Thermal
        ↓
    Display Image

    Raw Thermal
        ↓
    Analysis Result

## Frame Ownership

The acquisition layer owns the creation and publication of frames.

Consumers do not own the camera or acquisition process.

Potential consumers include:

- Processing.
- Observer UI.
- Recorder.
- Diagnostics.
- Offline storage pipeline.

A slow consumer must not stop camera acquisition.

## Frame Transport

The frame transport mechanism will use shared memory or another zero/minimal-copy mechanism where practical.

The exact implementation is intentionally deferred to ADR-003.

The transport must support:

- Multiple consumers.
- Bounded memory.
- Frame sequence tracking.
- Latest-frame access.
- Frame-drop detection.
- Independent consumer speeds.
- Safe producer/consumer synchronization.

## Live Frame Source

Live operation will use:

    TV46L Camera
          ↓
    Acquisition Layer
          ↓
    Frame
          ↓
    Shared Frame Transport

The processing and UI layers must not directly grab frames from the camera.

## Offline Frame Source

Offline operation will use:

    Raw Recording
          ↓
    Offline Frame Source
          ↓
    Frame
          ↓
    Same Processing Pipeline

The processing system must not need separate live and offline temperature/ROI algorithms.

## Recording

Alarm recording must preserve enough raw information to reconstruct the acquired frames later.

A recording must preserve, where applicable:

- Thermal raw data.
- Visible data.
- Frame timestamps.
- Sequence numbers.
- Camera identity.
- Synchronization information.
- Relevant metadata.
- Position context.
- ROI/configuration context.
- Alarm context.

The exact raw file/container format is deferred to a separate architecture decision.

## Performance Requirements

The frame model must support the expected multi-camera workload.

The design must account for:

- Up to 8 cameras.
- Thermal frame size.
- Visible frame size.
- Thermal FPS.
- Visible FPS.
- Total memory bandwidth.
- Shared-memory capacity.
- Number of consumers.
- Recording bandwidth.

These values must be measured using the actual TV46L hardware before finalizing the shared-memory buffer sizes.

## Decisions Deferred

The following are intentionally NOT decided by this ADR:

- Shared-memory implementation.
- Ring-buffer size.
- Number of buffers.
- Raw file format.
- Compression.
- Thermal frame byte layout.
- Visible frame byte layout.
- Exact synchronization tolerance.
- GPU memory transport.
- CPU/GPU frame ownership.
- Recording filesystem layout.

These will be decided after measuring the actual camera output.

## Architectural Rule

All V3 subsystems must use the defined Frame contract.

No subsystem may invent its own incompatible representation of a camera frame.