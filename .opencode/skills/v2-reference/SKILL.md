---
name: v2-reference
description: Use reference/TMS_v2 as a read-only technical reference when designing or implementing TMS V3. Recover proven TV46L, HALCON, acquisition, calibration, ROI, temperature, alarm, PTZ, database, diagnostic, and testing knowledge without copying V2 architecture.
---

# TMS V2 Reference Skill

## Location

The V2 repository is located at:

reference/TMS_v2/

It is READ-ONLY reference material.

Never modify files inside this directory.

## Purpose

Use V2 to recover technical knowledge that V3 must not lose.

Do not treat V2 architecture as the V3 architecture.

## Priority Reference Areas

When investigating V2, prioritize:

### Camera

reference/TMS_v2/camera/

Use for:

- TV46L acquisition.
- HALCON acquisition.
- Camera connection.
- Camera configuration.
- Camera controls.
- Focus.
- NUC.
- Thermal stream.
- Visible stream.
- Camera identity.

### Calibration

reference/TMS_v2/calibration/

Use for:

- TV46L calibration format.
- Calibration parsing.
- Raw-to-temperature conversion.
- LUT generation.

### ROI Engine

reference/TMS_v2/roi_engine/

Use for:

- ROI geometry.
- HALCON regions.
- Statistics.
- ROI caching.
- Temperature measurement.
- Existing performance approaches.

### Processing

reference/TMS_v2/processing/

Use for:

- Existing processing pipelines.
- Temperature processing.
- Frame transformations.
- Existing processing assumptions.

### Observation

reference/TMS_v2/observation/

Use for:

- Observer behavior.
- Position-dependent ROI behavior.
- Alarm processing integration.
- Existing observation flow.

### Alarm

reference/TMS_v2/alarm/

Use for:

- Alarm state machine.
- Alarm conditions.
- Hysteresis.
- Delay.
- Acknowledgement.
- Alarm history.

### Application

reference/TMS_v2/app/

Use for:

- Application startup.
- Lifecycle.
- Existing orchestration.
- Window management.

### Tests

reference/TMS_v2/tests/

Use to determine:

- What V2 actually tested.
- Existing test utilities.
- Synthetic data generation.
- Validation approaches.
- Known limitations.

## Secondary Reference Areas

Inspect when relevant:

reference/TMS_v2/gui/
reference/TMS_v2/configuration/
reference/TMS_v2/database/
reference/TMS_v2/config/
reference/TMS_v2/tools/
reference/TMS_v2/utilities/
reference/TMS_v2/docs/

## Important Documentation

Inspect when relevant:

reference/TMS_v2/CODING.md
reference/TMS_v2/DESIGN.md
reference/TMS_v2/AGENTS.md
reference/TMS_v2/docs/

These describe V2 decisions and constraints.

They are evidence, not automatic V3 requirements.

## Avoid Blind Reuse

Before carrying anything from V2 into V3:

1. Find the exact implementation.
2. Determine what it actually does.
3. Determine whether it is tested.
4. Determine whether it was tested against real TV46L hardware.
5. Determine whether it is production code or a standalone diagnostic tool.
6. Identify known limitations.
7. Decide whether V3 should KEEP, REDESIGN, REWRITE, or DISCARD it.

## V2 Duplicates

Be especially careful with duplicate V2 implementations.

Do not assume the first matching class is the production implementation.

Trace:

- imports
- callers
- application startup
- tests
- hardware validation

before deciding which implementation represents the actual behavior.

## Hardware Evidence

Do not claim hardware support merely because code exists.

Distinguish:

- Implemented.
- Unit tested.
- Integration tested.
- Hardware tested.
- Hardware behavior unknown.

## V3 Rule

V3 is a new architecture.

V2 provides technical knowledge and proven algorithms.

V2 file organization, class organization, threading design, configuration structure, and application architecture must not be copied without an explicit V3 architectural decision.