"""
processing.halcon.region_cache -- worker-owned HALCON region cache.

One RegionCache instance lives on one HalconROIAdapter, which itself is
owned by one SimpleProcessingPipeline on one consumer thread. HObjects
are never shared across threads, cameras, or positions.

Cache identity per shape entry:
    (camera_id, position_id, context_generation, shape)
plus a geometry fingerprint compared on every lookup. A context change
(camera/position/generation) makes stored entries unreachable; a
geometry change rebuilds only the affected shape group.

Lifecycle: lookup -> store (on miss) -> invalidate_* -> clear.
HALCON objects are released by dropping the Python reference (the
binding frees the handle via reference counting; explicit clear_obj()
on a live wrapper double-frees -- HALCON error #4051).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from thermal_monitor.core.models import ROIShape
from thermal_monitor.processing.halcon.grouped import ShapeGroup


@dataclass(slots=True)
class CacheStats:
    """Lifetime diagnostics for one RegionCache."""

    hits: int = 0
    misses: int = 0
    builds: int = 0
    rebuilds: int = 0
    evictions: int = 0
    stale_rejections: int = 0


@dataclass(slots=True)
class _Entry:
    fingerprint: tuple[Any, ...]
    regions: Any


class RegionCache:
    """Thread-local HALCON region store with per-shape dirty rebuild."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, int, ROIShape], _Entry] = {}
        self._stats = CacheStats()

    @property
    def stats(self) -> CacheStats:
        return self._stats

    @staticmethod
    def _key(group: ShapeGroup) -> tuple[str, str, int, ROIShape]:
        return (group.camera_id, group.position_id,
                group.context_generation, group.shape)

    def lookup(self, group: ShapeGroup) -> Any | None:
        """Return cached regions on fingerprint match, else None.

        Context mismatch (different key) and geometry mismatch (same
        key, different fingerprint) both miss; only geometry mismatches
        count as rebuilds, context changes evict (see prune_context).
        """
        entry = self._entries.get(self._key(group))
        if entry is None:
            self._stats.misses += 1
            return None
        if entry.fingerprint != group.fingerprint:
            self._stats.misses += 1
            self._stats.rebuilds += 1
            return None
        self._stats.hits += 1
        return entry.regions

    def store(self, group: ShapeGroup, regions: Any) -> None:
        """Store (or replace) the region tuple for a group."""
        key = self._key(group)
        if key in self._entries:
            self._stats.evictions += 1
            old = self._entries.pop(key)
            del old
        self._entries[key] = _Entry(
            fingerprint=group.fingerprint, regions=regions)
        self._stats.builds += 1

    def prune_context(self, *, camera_id: str, position_id: str,
                      context_generation: int) -> int:
        """Drop entries not belonging to the active context.

        Returns the number of evicted entries. Called with the
        pipeline's current context so Position A regions can never be
        reused for Position B.
        """
        doomed = [k for k in self._entries
                  if k[0] != camera_id or k[1] != position_id
                  or k[2] != context_generation]
        for key in doomed:
            old = self._entries.pop(key)
            del old
        self._stats.evictions += len(doomed)
        return len(doomed)

    def invalidate_camera(self, camera_id: str) -> int:
        doomed = [k for k in self._entries if k[0] == camera_id]
        for key in doomed:
            old = self._entries.pop(key)
            del old
        self._stats.evictions += len(doomed)
        return len(doomed)

    def invalidate_position(self, camera_id: str, position_id: str) -> int:
        doomed = [k for k in self._entries
                  if k[0] == camera_id and k[1] == position_id]
        for key in doomed:
            old = self._entries.pop(key)
            del old
        self._stats.evictions += len(doomed)
        return len(doomed)

    def clear(self) -> None:
        """Release all cached regions (e.g. on shutdown)."""
        keys = list(self._entries)
        for key in keys:
            old = self._entries.pop(key)
            del old
        self._stats.evictions += len(keys)

    def __len__(self) -> int:
        return len(self._entries)


__all__ = ["RegionCache", "CacheStats"]
