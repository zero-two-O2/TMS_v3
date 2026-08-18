"""
processing.roi_resolver -- ROI resolution for camera positions.

Resolves the applicable ROI set for a given camera and PTZ position.
Pure domain logic, no HALCON, no SQL, no GUI.
"""

from __future__ import annotations

from typing import Sequence

from thermal_monitor.core.models import AnalysisConfig, ROIConfig
from thermal_monitor.storage.repositories.roi import ROIRepository, PositionROIRepository
from thermal_monitor.storage.database import Database


class ROIResolver:
    """Resolves ROI configurations for a camera at a specific position."""

    def __init__(self, database: Database) -> None:
        self._roi_repo = ROIRepository(database)
        self._pos_roi_repo = PositionROIRepository(database)

    def resolve(self, camera_id: str, position_id: str) -> Sequence[ROIConfig]:
        """
        Get all enabled ROIs for a camera at a specific position.

        Resolution order:
        1. Try position-specific ROIs (camera_id + position_id)
        2. Fall back to camera-wide ROIs (camera_id only)
        3. Return empty sequence if none found
        """
        # Try position-specific ROIs first
        result = self._roi_repo.find_by_camera_and_position(camera_id, position_id)
        if result.success and result.data:
            return tuple(roi for roi in result.data if roi.enabled)

        # Fall back to camera-wide ROIs
        result = self._roi_repo.find_by_camera_id(camera_id)
        if result.success and result.data:
            return tuple(roi for roi in result.data if roi.enabled)

        return ()

    def resolve_from_config(self, config: AnalysisConfig, position_id: str) -> Sequence[ROIConfig]:
        """Resolve ROIs from an already-loaded AnalysisConfig."""
        return config.get_rois_for_position(position_id)


class CachedROIResolver:
    """ROIResolver with in-memory caching for repeated lookups."""

    def __init__(self, database: Database) -> None:
        self._resolver = ROIResolver(database)
        self._cache: dict[tuple[str, str], Sequence[ROIConfig]] = {}

    def resolve(self, camera_id: str, position_id: str) -> Sequence[ROIConfig]:
        key = (camera_id, position_id)
        if key not in self._cache:
            self._cache[key] = self._resolver.resolve(camera_id, position_id)
        return self._cache[key]

    def invalidate(self, camera_id: str | None = None, position_id: str | None = None) -> None:
        """Invalidate cache entries."""
        if camera_id is None and position_id is None:
            self._cache.clear()
        else:
            keys_to_remove = [
                k for k in self._cache
                if (camera_id is None or k[0] == camera_id)
                and (position_id is None or k[1] == position_id)
            ]
            for k in keys_to_remove:
                del self._cache[k]