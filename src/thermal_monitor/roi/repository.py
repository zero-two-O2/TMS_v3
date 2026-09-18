"""roi.repository -- CRUD over the versioned ROI tables.

Works against both backends through the shared (connect/transaction/
execute/fetch_*) surface. Uses explicit column lists (never SELECT *)
so column order from migrations cannot silently corrupt mapping.
"""

from __future__ import annotations

import json
import time

from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.errors import RoiPersistenceError
from thermal_monitor.roi.geometry import geometry_from_dict
from thermal_monitor.roi.models import RoiDefinition
from thermal_monitor.roi.serialization import roi_from_dict, roi_to_dict
from thermal_monitor.roi.validation import check_roi_binding

ROI_COLUMNS = [
    "roi_id", "camera_id", "ptz_id", "position_id", "object_type",
    "geometry_json", "name", "enabled", "visible",
    "created_at", "updated_at", "schema_version",
    "analysis_config_json", "alarm_rule_ref", "display_json",
]


class RoiDefinitionRepository:
    """Camera+position-scoped ROI persistence."""

    def __init__(self, database) -> None:  # Database | SqliteDatabase (structural)
        self._db = database

    def _is_sqlite(self) -> bool:
        return type(self._db).__name__ == "SqliteDatabase"

    def _ph(self) -> str:
        return "?"

    def create(self, roi: RoiDefinition) -> RoiDefinition:
        payload = roi_to_dict(roi)
        cols = ",".join(ROI_COLUMNS)
        placeholders = ",".join(["?"] * len(ROI_COLUMNS))
        sql = f"INSERT INTO roi_definitions ({cols}) VALUES ({placeholders})"
        params = (
            payload["roi_id"], payload["camera_id"], payload["ptz_id"],
            payload["position_id"], payload["object_type"],
            json.dumps(payload["geometry"]), payload["name"],
            1 if payload["enabled"] else 0, 1 if payload["visible"] else 0,
            payload["created_at"], payload["updated_at"],
            payload["schema_version"], json.dumps(payload["analysis_config"]),
            payload["alarm_rule_ref"], json.dumps(payload["display"]))
        try:
            with self._db.transaction() as cursor:
                cursor.execute(sql, params)
        except Exception as exc:
            raise RoiPersistenceError(f"ROI insert failed: {exc}") from exc
        return roi

    def list_for_position(self, camera_id: str, position_id: str) -> list[RoiDefinition]:
        cols = ",".join(ROI_COLUMNS)
        sql = (f"SELECT {cols} FROM roi_definitions "
               "WHERE camera_id = ? AND position_id = ? ORDER BY roi_id")
        try:
            rows = self._db.fetch_all(sql, (camera_id, position_id))
        except Exception as exc:
            raise RoiPersistenceError(f"ROI list failed: {exc}") from exc
        out: list[RoiDefinition] = []
        for row in rows:
            data = dict(zip(ROI_COLUMNS, tuple(row)))
            out.append(self._row_to_entity(data))
        return out

    def get(self, roi_id: str) -> RoiDefinition | None:
        cols = ",".join(ROI_COLUMNS)
        try:
            row = self._db.fetch_one(
                f"SELECT {cols} FROM roi_definitions WHERE roi_id = ?", (roi_id,))
        except Exception as exc:
            raise RoiPersistenceError(f"ROI get failed: {exc}") from exc
        if row is None:
            return None
        return self._row_to_entity(dict(zip(ROI_COLUMNS, tuple(row))))

    def update(self, roi: RoiDefinition) -> RoiDefinition:
        existing = self.get(roi.roi_id)
        if existing is None:
            raise RoiPersistenceError(f"ROI {roi.roi_id!r} does not exist")
        check_roi_binding(roi, camera_id=existing.camera_id, ptz_id=existing.ptz_id,
                          position_id=existing.position_id)
        payload = roi_to_dict(roi)
        payload["updated_at"] = time.time()
        sql = ("UPDATE roi_definitions SET object_type = ?, geometry_json = ?, "
               "name = ?, enabled = ?, visible = ?, updated_at = ?, "
               "analysis_config_json = ?, alarm_rule_ref = ?, display_json = ? "
               "WHERE roi_id = ?")
        params = (payload["object_type"], json.dumps(payload["geometry"]),
                  payload["name"], 1 if payload["enabled"] else 0,
                  1 if payload["visible"] else 0, payload["updated_at"],
                  json.dumps(payload["analysis_config"]),
                  payload["alarm_rule_ref"], json.dumps(payload["display"]),
                  payload["roi_id"])
        try:
            with self._db.transaction() as cursor:
                cursor.execute(sql, params)
        except Exception as exc:
            raise RoiPersistenceError(f"ROI update failed: {exc}") from exc
        return roi_from_dict(payload)

    def delete(self, roi_id: str) -> int:
        try:
            with self._db.transaction() as cursor:
                cursor.execute("DELETE FROM roi_definitions WHERE roi_id = ?", (roi_id,))
                return cursor.rowcount if cursor.rowcount is not None else 0
        except Exception as exc:
            raise RoiPersistenceError(f"ROI delete failed: {exc}") from exc

    def replace_all(self, camera_id: str, position_id: str,
                    ptz_id: str, rois: list[RoiDefinition]) -> list[RoiDefinition]:
        """Transactional bulk replacement for import workflows."""
        for roi in rois:
            check_roi_binding(roi, camera_id=camera_id, ptz_id=ptz_id,
                              position_id=position_id)
        try:
            with self._db.transaction() as cursor:
                cursor.execute(
                    "DELETE FROM roi_definitions WHERE camera_id = ? AND position_id = ?",
                    (camera_id, position_id))
                for roi in rois:
                    payload = roi_to_dict(roi)
                    cols = ",".join(ROI_COLUMNS)
                    placeholders = ",".join(["?"] * len(ROI_COLUMNS))
                    cursor.execute(
                        f"INSERT INTO roi_definitions ({cols}) VALUES ({placeholders})",
                        (payload["roi_id"], payload["camera_id"], payload["ptz_id"],
                         payload["position_id"], payload["object_type"],
                         json.dumps(payload["geometry"]), payload["name"],
                         1 if payload["enabled"] else 0, 1 if payload["visible"] else 0,
                         payload["created_at"], payload["updated_at"],
                         payload["schema_version"],
                         json.dumps(payload["analysis_config"]),
                         payload["alarm_rule_ref"], json.dumps(payload["display"])))
        except Exception as exc:
            raise RoiPersistenceError(f"ROI bulk replace failed: {exc}") from exc
        return list(rois)

    def _row_to_entity(self, data: dict) -> RoiDefinition:
        return roi_from_dict({
            "roi_id": data["roi_id"], "camera_id": data["camera_id"],
            "ptz_id": data["ptz_id"], "position_id": data["position_id"],
            "object_type": data["object_type"],
            "geometry": json.loads(data["geometry_json"] or "{}"),
            "name": data.get("name", "") or "",
            "enabled": bool(data.get("enabled", 1)),
            "visible": bool(data.get("visible", 1)),
            "created_at": float(data.get("created_at") or 0.0),
            "updated_at": float(data.get("updated_at") or 0.0),
            "schema_version": int(data.get("schema_version") or 1),
            "analysis_config": json.loads(data.get("analysis_config_json") or "{}"),
            "alarm_rule_ref": data.get("alarm_rule_ref") or "",
            "display": json.loads(data.get("display_json") or "{}"),
        })


__all__ = ["ROI_COLUMNS", "RoiDefinitionRepository"]
