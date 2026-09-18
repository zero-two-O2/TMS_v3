"""roi.io -- versioned ROI collection import/export.

Format ``tms-roi-collection`` v1: one camera+PTZ+position owner, a list
of ROI payloads (same schema as serialization.roi_to_dict). Import
validates everything BEFORE any repository write; callers commit
all-or-nothing via ``RoiDefinitionRepository.replace_all`` (REPLACE) or
individual ``create`` calls (MERGE, skips duplicate ids).

Ownership is never remapped silently: a payload whose owner differs
from the target is rejected.
"""

from __future__ import annotations

from thermal_monitor.roi.editor import MAX_ROIS_PER_POSITION
from thermal_monitor.roi.errors import RoiBindingError, RoiValidationError
from thermal_monitor.roi.serialization import roi_from_dict, roi_to_dict

FORMAT = "tms-roi-collection"
VERSION = 1


def export_collection(*, camera_id: str, ptz_id: str, position_id: str,
                      rois: list) -> dict:
    for roi in rois:
        if (roi.camera_id != camera_id or roi.ptz_id != ptz_id
                or roi.position_id != position_id):
            raise RoiBindingError(
                f"ROI {roi.roi_id!r} does not belong to "
                f"{camera_id}/{ptz_id}/{position_id}; refusing export")
    return {"format": FORMAT, "version": VERSION, "camera_id": camera_id,
            "ptz_id": ptz_id, "position_id": position_id,
            "rois": [roi_to_dict(roi) for roi in rois]}


def preview_collection(payload: dict) -> dict:
    """Validate and summarize without constructing repository writes."""
    owner, items = _validate_envelope(payload)
    return {"camera_id": owner[0], "ptz_id": owner[1],
            "position_id": owner[2], "count": len(items),
            "roi_ids": [item["roi_id"] for item in items]}


def import_collection(payload: dict, *, camera_id: str, ptz_id: str,
                      position_id: str, existing_ids: set[str] | None = None,
                      mode: str = "merge") -> list:
    """Validate a payload against the target owner; return new definitions.

    Raises before returning anything partial. MERGE skips ids already in
    ``existing_ids``; REPLACE expects the caller to confirm first and
    commit via ``replace_all``.
    """
    if mode not in ("merge", "replace"):
        raise RoiValidationError(f"unknown import mode {mode!r}")
    owner, items = _validate_envelope(payload)
    if tuple(owner) != (camera_id, ptz_id, position_id):
        raise RoiBindingError(
            f"import owner {owner} does not match target "
            f"({camera_id}, {ptz_id}, {position_id}); refusing import")
    existing_ids = existing_ids or set()
    out = []
    seen: set[str] = set()
    for item in items:
        roi = roi_from_dict(item)  # geometry/binding/schema validation
        if (roi.camera_id != camera_id or roi.ptz_id != ptz_id
                or roi.position_id != position_id):
            raise RoiBindingError(
                f"ROI {roi.roi_id!r} owner mismatch inside a matching payload")
        if roi.roi_id in seen:
            raise RoiValidationError(f"duplicate ROI id {roi.roi_id!r} in import")
        seen.add(roi.roi_id)
        if mode == "merge" and roi.roi_id in existing_ids:
            continue
        out.append(roi)
    if mode == "replace" and len(out) > MAX_ROIS_PER_POSITION:
        raise RoiValidationError("import exceeds per-position object limit")
    if mode == "merge" and len(existing_ids) + len(out) > MAX_ROIS_PER_POSITION:
        raise RoiValidationError("import would exceed per-position object limit")
    return out


def _validate_envelope(payload: dict):
    if not isinstance(payload, dict):
        raise RoiValidationError("import payload must be a mapping")
    if payload.get("format") != FORMAT:
        raise RoiValidationError(
            f"unsupported import format {payload.get('format')!r}")
    if payload.get("version") != VERSION:
        raise RoiValidationError(
            f"unsupported import version {payload.get('version')!r}")
    for key in ("camera_id", "ptz_id", "position_id"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise RoiValidationError(f"import envelope.{key} is required")
    items = payload.get("rois")
    if not isinstance(items, list):
        raise RoiValidationError("import envelope.rois must be a list")
    if len(items) > MAX_ROIS_PER_POSITION:
        raise RoiValidationError("import exceeds per-position object limit")
    return ((payload["camera_id"], payload["ptz_id"], payload["position_id"]),
            items)


__all__ = ["FORMAT", "VERSION", "export_collection", "preview_collection",
           "import_collection"]
