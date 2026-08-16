# Agent Instructions

## Current Repository State

- This is an architecture-phase Python 3.10 project; there is no runtime entrypoint, application command, dependency lockfile, lint/typecheck configuration, codegen, or CI workflow yet.
- The package uses the `src/` layout. Keep application code under `src/thermal_monitor/` and tests under `tests/`.
- `pyproject.toml` is the executable source for packaging and pytest settings: Python must be `>=3.10,<3.11`, and pytest discovers tests in `tests/` with `src` on `PYTHONPATH`.

## Commands

- Install the editable package with `python -m pip install -e .` using Python 3.10.
- Run the configured test suite with `python -m pytest`.
- Run a focused test with `python -m pytest path/to/test_file.py::test_name`; no tests currently exist.
- Do not invent lint, formatting, typecheck, build, migration, or deployment commands; none are configured in the repository.

## Architecture Boundaries

- The six package domains are `core`, `camera`, `processing`, `storage`, `offline`, and `ui`; add a new top-level domain only for a clear responsibility.
- `camera` owns HALCON/TV46L acquisition and hardware controls, not GUI, database, ROI, or alarm logic.
- `processing` consumes frame data and configuration to produce analysis results; it must not control cameras, update widgets, or mutate raw frames.
- `storage` owns SQL Server access, repositories, recording metadata, and raw recording files; keep SQL queries out of other modules.
- `offline` supplies saved raw data as a frame source and reuses the live processing pipeline rather than duplicating analysis algorithms.
- `ui` owns PyQt6 presentation and must not perform acquisition, heavy processing, or database operations.
- `core` owns lifecycle, state, commands/events, shared models, logging, and validated configuration without camera-, GUI-, or database-specific implementations.

## Frame Contract

- Treat published raw frames as immutable acquisition data. They must retain camera identity, per-camera monotonic sequence number, timestamp, thermal raw data, visible data, synchronization information, and acquisition metadata.
- Keep ROI definitions, temperature/statistics results, alarm state, display images, overlays, PTZ commands, and GUI state outside the raw frame as configuration or derived results.
- Acquisition owns frame creation/publication; consumers such as processing, UI, recording, and diagnostics must not control the acquisition loop, and slow consumers must not stop acquisition.
- Live and offline sources should produce the same frame contract so the same processing path can analyze both.

## Repository Hygiene

- Copy `.env.example` to a local `.env` only when environment configuration is needed; never commit secrets, `.env`, `.venv/`, logs, recordings/captures, generated reports, or local SQL Server database files.
- SQL Server schema changes belong in version-controlled migration scripts under `database/migrations/`; no migration runner is configured yet.

## V2 Reference Repository

`reference/TMS_v2/` contains the previous V2 implementation.

It is READ-ONLY reference material.

Never modify, delete, rename, move, or generate files inside `reference/TMS_v2/`.

Use V2 to recover:

- TV46L hardware behavior.
- HALCON implementation details.
- Thermal raw-frame handling.
- Visible-frame handling.
- Camera acquisition.
- Calibration.
- ROI algorithms.
- Temperature conversion.
- Alarm behavior.
- Camera controls.
- PTZ knowledge.
- Database knowledge.
- Diagnostic tools.
- Existing tests.
- Known hardware limitations.

Do not assume V2 architecture is correct.

Before reusing V2 functionality:

1. Locate the exact V2 implementation.
2. Understand what it actually does.
3. Determine whether it is tested or hardware-verified.
4. Determine whether it is production code, experimental code, or a diagnostic tool.
5. Decide whether V3 should keep, redesign, or discard it.

V2 source code must never be copied into V3 merely because it already exists.