"""
storage.repositories.ptz -- PTZ position repository implementation.
"""

from __future__ import annotations

from dataclasses import dataclass

from thermal_monitor.ptz.errors import PtzValidationError
from thermal_monitor.ptz.positions import PtzPosition
from thermal_monitor.storage.database import Database
from thermal_monitor.storage.repositories.base import BaseRepository, RepositoryResult


@dataclass
class PtzPositionRow:
    """Database row for ptz_positions table."""

    id: int
    position_id: str
    camera_id: str
    ptz_id: str
    name: str
    pan: float
    tilt: float
    velocity: float | None
    pan_velocity: float | None
    tilt_velocity: float | None
    roi_set_ref: str
    enabled: bool
    created_at: object = None
    updated_at: object = None


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


class PtzPositionRepository(BaseRepository[PtzPosition]):
    """Repository for persistent PTZ positions.

    Positions are owned by ``camera_id`` and record the ``ptz_id``
    resolved at save time. ROI data is never stored here; ``roi_set_ref``
    keys the existing ``position_roi_associations`` table.
    """

    def __init__(self, database: Database) -> None:
        super().__init__(database, "ptz_positions")

    def _get_columns(self) -> list[str]:
        return [
            "position_id", "camera_id", "ptz_id", "name",
            "pan", "tilt", "velocity", "pan_velocity", "tilt_velocity",
            "roi_set_ref", "enabled",
        ]

    def _to_entity(self, row: tuple) -> PtzPosition:
        r = PtzPositionRow(*row)
        return PtzPosition(
            position_id=r.position_id,
            camera_id=r.camera_id,
            ptz_id=r.ptz_id,
            name=r.name,
            pan=float(r.pan),
            tilt=float(r.tilt),
            velocity=_to_float(r.velocity),
            pan_velocity=_to_float(r.pan_velocity),
            tilt_velocity=_to_float(r.tilt_velocity),
            roi_set_ref=r.roi_set_ref or "",
            enabled=bool(r.enabled),
        )

    def _to_params(self, entity: PtzPosition) -> tuple:
        return (
            entity.position_id,
            entity.camera_id,
            entity.ptz_id,
            entity.name,
            entity.pan,
            entity.tilt,
            entity.velocity,
            entity.pan_velocity,
            entity.tilt_velocity,
            entity.roi_set_ref,
            1 if entity.enabled else 0,
        )

    def create_position(self, position: PtzPosition) -> RepositoryResult[PtzPosition]:
        """Insert a validated position. Fails on duplicate position_id."""
        try:
            self._validate(position)
        except PtzValidationError as exc:
            return RepositoryResult(success=False, error=str(exc))
        return self.insert(position)

    def get_position(self, position_id: str) -> RepositoryResult[PtzPosition | None]:
        """Fetch one position by stable ID."""
        result = self.find_all("position_id = ?", (position_id,))
        if not result.success:
            return RepositoryResult(success=False, error=result.error)
        data = result.data[0] if result.data else None
        return RepositoryResult(success=True, data=data)

    def list_positions_for_camera(
        self, camera_id: str, *, include_disabled: bool = True
    ) -> RepositoryResult[list[PtzPosition]]:
        """All positions owned by a camera, ordered by name."""
        if include_disabled:
            return self.find_all(
                "camera_id = ? ORDER BY name", (camera_id,)
            )
        return self.find_all(
            "camera_id = ? AND enabled = 1 ORDER BY name", (camera_id,)
        )

    def list_positions_for_ptz(
        self, ptz_id: str
    ) -> RepositoryResult[list[PtzPosition]]:
        """All positions recorded against a PTZ (binding-change audit)."""
        return self.find_all("ptz_id = ? ORDER BY camera_id, name", (ptz_id,))

    def update_position(self, position: PtzPosition) -> RepositoryResult[PtzPosition]:
        """Replace the stored row for ``position.position_id``."""
        try:
            self._validate(position)
        except PtzValidationError as exc:
            return RepositoryResult(success=False, error=str(exc))
        columns = self._get_columns()
        set_clause = ",".join(f"{col}=?" for col in columns if col != "position_id")
        raw = self._to_params(position)
        params = tuple(
            value for column, value in zip(columns, raw) if column != "position_id"
        ) + (position.position_id,)
        sql = f"UPDATE {self._table_name} SET {set_clause} WHERE position_id = ?"
        try:
            with self._db.transaction() as cursor:
                cursor.execute(sql, params)
                rows = cursor.rowcount
            if rows == 0:
                return RepositoryResult(
                    success=False,
                    error=f"Unknown position_id {position.position_id!r}",
                )
            return RepositoryResult(success=True, data=position, rows_affected=rows)
        except Exception as exc:
            return RepositoryResult(success=False, error=str(exc))

    def delete_position(self, position_id: str) -> RepositoryResult[int]:
        """Delete one position. ROI sets are never cascade-deleted."""
        return self.delete("position_id = ?", (position_id,))

    def associate_roi_set(
        self, position_id: str, roi_set_ref: str
    ) -> RepositoryResult[PtzPosition]:
        """Point a position at an ROI-set reference (no ROI duplication)."""
        current = self.get_position(position_id)
        if not current.success:
            return RepositoryResult(success=False, error=current.error)
        if current.data is None:
            return RepositoryResult(
                success=False, error=f"Unknown position_id {position_id!r}"
            )
        return self.update_position(
            current.data.with_updated(roi_set_ref=roi_set_ref or "")
        )

    def remove_roi_association(
        self, position_id: str
    ) -> RepositoryResult[PtzPosition]:
        """Clear the ROI-set reference; the ROI set itself is untouched."""
        return self.associate_roi_set(position_id, "")

    @staticmethod
    def _validate(position: PtzPosition) -> None:
        if not isinstance(position, PtzPosition):
            raise PtzValidationError("position must be a PtzPosition")
        # PtzPosition.__post_init__ already validated values on construction.
