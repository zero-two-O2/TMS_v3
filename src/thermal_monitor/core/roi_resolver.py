"""
core.roi_resolver -- ROI resolution for camera positions.

Resolves the applicable ROI set for a given camera and PTZ position.
Pure domain logic, no HALCON, no SQL, no GUI.
"""

from __future__ import annotations

from typing import Sequence

from thermal_monitor.core.models import AnalysisConfig, ROIConfig


def resolve_rois(
    config: AnalysisConfig,
    camera_id: str,
    position_id: str,
) -> Sequence[ROIConfig]:
    """
    Resolve ROIs from an AnalysisConfig for a specific camera and position.

    Resolution order:
    1. Try position-specific ROIs (camera_id + position_id)
    2. Fall back to camera-wide ROIs (camera_id only, position_id = "default")
    3. Return empty sequence if none found

    Args:
        config: AnalysisConfig containing ROI definitions and position associations
        camera_id: Camera ID (must match config.camera_id)
        position_id: PTZ position ID to resolve ROIs for

    Returns:
        Sequence of enabled ROIConfig objects for the position
    """
    if config.camera_id != camera_id:
        return ()

    # Try position-specific ROIs first
    rois = config.get_rois_for_position(position_id)
    if rois:
        return tuple(roi for roi in rois if roi.enabled)

    # Fall back to "default" position
    if position_id != "default":
        rois = config.get_rois_for_position("default")
        if rois:
            return tuple(roi for roi in rois if roi.enabled)

    return ()


class CachedROIResolver:
    """ROI resolver with in-memory caching for repeated lookups."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], Sequence[ROIConfig]] = {}

    def resolve(self, config: AnalysisConfig, camera_id: str, position_id: str) -> Sequence[ROIConfig]:
        key = (camera_id, position_id)
        if key not in self._cache:
            self._cache[key] = resolve_rois(config, camera_id, position_id)
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