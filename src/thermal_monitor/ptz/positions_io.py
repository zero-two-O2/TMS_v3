"""ptz.positions_io -- versioned position import/export (Phase 7).

JSON document format ``tms-ptz-positions/v1`` carrying full position
records (IDs, camera/PTZ references, coordinates, metadata, ROI-set
references) without ROI blobs. Import validates everything before
committing, runs bulk persistence in one transaction, and enforces an
explicit conflict policy. No executable content, no SQL in the format.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from thermal_monitor.ptz.errors import PtzValidationError
from thermal_monitor.ptz.positions import PtzPosition

FORMAT_TYPE = "tms-ptz-positions"
FORMAT_VERSION = "v1"


class ConflictPolicy(str, Enum):
    """Bulk-import conflict handling. The caller chooses explicitly."""

    REJECT = "reject"  # fail the import on any ID/name conflict
    SKIP = "skip"  # keep existing records, import the rest
    REPLACE = "replace"  # overwrite existing records (needs confirmation)


@dataclass(frozen=True, slots=True)
class ImportPreview:
    """Validation result before committing anything."""

    valid: bool
    to_create: int = 0
    to_replace: int = 0
    to_skip: int = 0
    errors: tuple = ()
    warnings: tuple = ()
    # Human-readable conflicting records ("name (id)"), for REPLACE
    # confirmation dialogs. Populated even when valid=False.
    conflicts: tuple = ()


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Committed bulk-import outcome."""

    created: int = 0
    replaced: int = 0
    skipped: int = 0
    errors: tuple = ()
    committed: bool = False


def export_positions(
    positions: list[PtzPosition],
    *,
    camera_id: str = "",
    ptz_id: str = "",
    exported_by: str = "",
) -> str:
    """Serialize positions to a versioned JSON document."""
    payload = {
        "format": FORMAT_TYPE,
        "version": FORMAT_VERSION,
        "exported_at": time.time(),
        "exported_by": exported_by,
        "camera_id": camera_id,
        "ptz_id": ptz_id,
        "positions": [
            {
                "position_id": p.position_id,
                "camera_id": p.camera_id,
                "ptz_id": p.ptz_id,
                "name": p.name,
                "pan": p.pan,
                "tilt": p.tilt,
                "velocity": p.velocity,
                "pan_velocity": p.pan_velocity,
                "tilt_velocity": p.tilt_velocity,
                "roi_set_ref": p.roi_set_ref,
                "enabled": p.enabled,
            }
            for p in positions
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _parse_document(text: str) -> tuple[dict, list[str]]:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, [f"invalid JSON: {exc}"]
    if not isinstance(document, dict):
        return {}, ["document must be a JSON object"]
    errors: list[str] = []
    if document.get("format") != FORMAT_TYPE:
        errors.append(f"unsupported format {document.get('format')!r}")
    if document.get("version") != FORMAT_VERSION:
        errors.append(f"unsupported version {document.get('version')!r}")
    positions = document.get("positions")
    if not isinstance(positions, list):
        errors.append("document.positions must be a list")
    return document, errors


def _parse_position(raw: object, index: int) -> tuple[Optional[PtzPosition], list[str]]:
    if not isinstance(raw, dict):
        return None, [f"positions[{index}]: must be an object"]
    errors: list[str] = []
    for key in ("position_id", "camera_id", "ptz_id", "name", "pan", "tilt"):
        if key not in raw:
            errors.append(f"positions[{index}]: missing required field {key!r}")
    if errors:
        return None, errors
    try:
        position = PtzPosition(
            position_id=str(raw["position_id"]),
            camera_id=str(raw["camera_id"]),
            ptz_id=str(raw["ptz_id"]),
            name=str(raw["name"]),
            pan=raw["pan"],
            tilt=raw["tilt"],
            velocity=raw.get("velocity"),
            pan_velocity=raw.get("pan_velocity"),
            tilt_velocity=raw.get("tilt_velocity"),
            roi_set_ref=str(raw.get("roi_set_ref", "") or ""),
            enabled=bool(raw.get("enabled", True)),
        )
    except (PtzValidationError, TypeError, ValueError) as exc:
        return None, [f"positions[{index}]: invalid values: {exc}"]
    return position, []


def preview_import(
    text: str,
    existing_ids: set[str],
    existing_names: dict[tuple[str, str], str],
    *,
    policy: ConflictPolicy = ConflictPolicy.REJECT,
    expected_camera_id: str = "",
    expected_ptz_id: str = "",
    known_roi_refs: Optional[set[str]] = None,
) -> tuple[ImportPreview, list[PtzPosition]]:
    """Validate a document against current state. No persistence here.

    ``existing_names`` maps ``(camera_id, name)`` to ``position_id``.
    """
    document, errors = _parse_document(text)
    if errors:
        return ImportPreview(valid=False, errors=tuple(errors)), []
    raws = document.get("positions", [])
    if len(raws) > 1000:
        return (
            ImportPreview(valid=False, errors=("import too large (>1000 positions)",)),
            [],
        )
    positions: list[PtzPosition] = []
    seen_ids: set[str] = set()
    to_create = to_replace = to_skip = 0
    warnings: list[str] = []
    conflicts: list[str] = []
    for index, raw in enumerate(raws):
        position, parse_errors = _parse_position(raw, index)
        if parse_errors:
            errors.extend(parse_errors)
            continue
        assert position is not None
        if position.position_id in seen_ids:
            errors.append(f"positions[{index}]: duplicate ID in file")
            continue
        seen_ids.add(position.position_id)
        if expected_camera_id and position.camera_id != expected_camera_id:
            errors.append(
                f"positions[{index}]: camera {position.camera_id!r} "
                f"does not match expected {expected_camera_id!r}"
            )
            continue
        if expected_ptz_id and position.ptz_id != expected_ptz_id:
            errors.append(
                f"positions[{index}]: PTZ {position.ptz_id!r} "
                f"does not match expected {expected_ptz_id!r}"
            )
            continue
        if known_roi_refs is not None and position.roi_set_ref:
            if position.roi_set_ref not in known_roi_refs:
                warnings.append(
                    f"positions[{index}]: unknown ROI-set reference "
                    f"{position.roi_set_ref!r} (kept as metadata)"
                )
        id_conflict = position.position_id in existing_ids
        name_conflict = (position.camera_id, position.name) in existing_names
        if id_conflict or name_conflict:
            conflicts.append(f"{position.name} ({position.position_id})")
            if policy == ConflictPolicy.REJECT:
                errors.append(
                    f"positions[{index}]: conflicts with existing record "
                    f"({position.position_id!r}/{position.name!r})"
                )
                continue
            if policy == ConflictPolicy.SKIP:
                to_skip += 1
                continue
            to_replace += 1
        else:
            to_create += 1
        positions.append(position)
    valid = not errors
    return (
        ImportPreview(
            valid=valid,
            to_create=to_create if valid else 0,
            to_replace=to_replace if valid else 0,
            to_skip=to_skip,
            errors=tuple(errors),
            warnings=tuple(warnings),
            conflicts=tuple(conflicts),
        ),
        positions if valid else [],
    )


def commit_import(
    repository,
    positions: list[PtzPosition],
    *,
    policy: ConflictPolicy = ConflictPolicy.REJECT,
) -> ImportResult:
    """Persist preview-accepted positions in one transaction.

    Rolls back everything on the first persistence failure (all-or-
    nothing); per-record skips from SKIP policy are not failures.
    """
    created = replaced = skipped = 0
    try:
        with repository._db.transaction() as cursor:
            for position in positions:
                existing = repository.get_position(position.position_id)
                if not existing.success:
                    raise RuntimeError(existing.error or "lookup failed")
                if existing.data is not None:
                    if policy == ConflictPolicy.SKIP:
                        skipped += 1
                        continue
                    if policy == ConflictPolicy.REPLACE:
                        _update_in(cursor, repository, position)
                        replaced += 1
                        continue
                    raise RuntimeError(
                        f"conflicting position {position.position_id!r}"
                    )
                _insert_in(cursor, repository, position)
                created += 1
    except Exception as exc:
        return ImportResult(errors=(str(exc),), committed=False)
    return ImportResult(
        created=created, replaced=replaced, skipped=skipped, committed=True
    )


def _insert_in(cursor, repository, position: PtzPosition) -> None:
    columns = repository._get_columns()
    placeholders = ",".join(["?"] * len(columns))
    cursor.execute(
        f"INSERT INTO {repository.table_name} "
        f"({','.join(columns)}) VALUES ({placeholders})",
        repository._to_params(position),
    )


def _update_in(cursor, repository, position: PtzPosition) -> None:
    columns = [c for c in repository._get_columns() if c != "position_id"]
    params = tuple(
        value
        for column, value in zip(repository._get_columns(), repository._to_params(position))
        if column != "position_id"
    ) + (position.position_id,)
    cursor.execute(
        f"UPDATE {repository.table_name} SET "
        + ",".join(f"{col}=?" for col in columns)
        + " WHERE position_id = ?",
        params,
    )


__all__ = [
    "ConflictPolicy",
    "FORMAT_TYPE",
    "FORMAT_VERSION",
    "ImportPreview",
    "ImportResult",
    "commit_import",
    "export_positions",
    "preview_import",
]
