"""
core.frame_integrity -- diagnostic frame-integrity instrumentation (no hardware).

Intermittent thermal distortion triage: prove whether a coherent acquisition
frame survives the acquisition -> SHM -> consumer-snapshot path unchanged.

Design constraints (deliberate):

* Diagnostic-only. All sampling is OFF unless explicitly enabled per
  component (``integrity_diag=True``) or via ``TMS_FRAME_INTEGRITY_DIAG=1``.
  Production overhead when disabled is one boolean check per frame.
* Sampled, never per-frame hashing in production. The cadence is
  ``TMS_FRAME_INTEGRITY_EVERY`` (default every 30th worker sequence).
* No SHM wire-format change. Acquisition samples and consumer snapshots are
  correlated through this process-global registry keyed by
  ``(camera_id, worker_sequence)``. The current SHM transport is only ever
  used intra-process (acquisition thread -> consumer threads of the same
  process), so a bounded in-memory registry is sufficient for triage. A
  future multi-process transport would need the checksum carried in the
  descriptor itself.
* No Focus / NUC / GVCP / discovery involvement.

Decisive experiment this enables (same hardware frame id)::

    acquisition crc32 (post-GVSP, pre-SHM) == consumer-snapshot crc32 ?

  mismatch  -> corruption between acquisition and the consumer snapshot
               (SHM publication / snapshot path is suspect).
  match but displayed image distorted -> acquisition and SHM are coherent;
               investigate temperature conversion, palette rendering, widget.
"""

from __future__ import annotations

import logging
import os
import threading
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)

DIAG_ENV_VAR = "TMS_FRAME_INTEGRITY_DIAG"
EVERY_ENV_VAR = "TMS_FRAME_INTEGRITY_EVERY"
DEFAULT_SAMPLE_EVERY = 30
MAX_SAMPLES = 1024

_TRUE_VALUES = {"1", "true", "yes", "on", "checksum"}


def integrity_diag_enabled(override: bool | None = None) -> bool:
    """Whether integrity sampling is enabled (explicit flag wins over env)."""
    if override is not None:
        return bool(override)
    return os.environ.get(DIAG_ENV_VAR, "").strip().lower() in _TRUE_VALUES


def integrity_sample_every(override: int | None = None) -> int:
    """Sample one frame per N worker sequences (minimum 1)."""
    if override is not None:
        try:
            return max(1, int(override))
        except (TypeError, ValueError):
            return DEFAULT_SAMPLE_EVERY
    try:
        return max(1, int(os.environ.get(EVERY_ENV_VAR, str(DEFAULT_SAMPLE_EVERY))))
    except (TypeError, ValueError):
        return DEFAULT_SAMPLE_EVERY


def compute_thermal_crc(thermal: np.ndarray | None) -> int | None:
    """CRC32 of the raw thermal payload bytes (None when no thermal data).

    Read-only safe: never mutates the input. Non-contiguous inputs are
    copied once by ``ascontiguousarray`` so the checksum is layout-stable.
    """
    if thermal is None:
        return None
    try:
        contiguous = np.ascontiguousarray(thermal)
        return zlib.crc32(contiguous.view(np.uint8)) & 0xFFFFFFFF
    except Exception:
        logger.debug("compute_thermal_crc failed", exc_info=True)
        return None


@dataclass(frozen=True, slots=True)
class IntegritySample:
    """One sampled checksum event (acquisition or snapshot side)."""

    camera_id: str
    sequence: int  # acquisition worker sequence (SHM sequence)
    hw_frame_id: int | None  # hardware/GVSP frame id (thermal stream)
    crc32: int
    wall_ts: float
    mono_ts: float


@dataclass(slots=True)
class IntegrityMismatch:
    """Details of the most recent acquisition/snapshot checksum mismatch."""

    camera_id: str
    sequence: int
    hw_frame_id: int | None
    acquisition_crc32: int | None
    snapshot_crc32: int | None
    wall_ts: float = 0.0


@dataclass(slots=True)
class IntegrityStats:
    """Aggregate integrity counters (optionally filtered to one camera)."""

    camera_id: str | None = None
    acquisitions_sampled: int = 0
    snapshots_checked: int = 0
    snapshots_matched: int = 0
    snapshots_mismatched: int = 0
    snapshots_unsampled: int = 0  # snapshot sampled without an acquisition sample
    last_mismatch: IntegrityMismatch | None = None


class FrameIntegrityRegistry:
    """Thread-safe bounded store correlating acquisition and snapshot CRCs."""

    def __init__(self, max_samples: int = MAX_SAMPLES) -> None:
        self._max_samples = max(1, max_samples)
        self._lock = threading.Lock()
        self._acquisitions: OrderedDict[tuple[str, int], IntegritySample] = OrderedDict()
        self._stats: dict[str, IntegrityStats] = {}
        self._last_mismatch: IntegrityMismatch | None = None

    def _for(self, camera_id: str) -> IntegrityStats:
        stats = self._stats.get(camera_id)
        if stats is None:
            stats = IntegrityStats(camera_id=camera_id)
            self._stats[camera_id] = stats
        return stats

    def record_acquisition(
        self,
        camera_id: str,
        sequence: int,
        hw_frame_id: int | None,
        crc32: int,
        wall_ts: float,
        mono_ts: float,
    ) -> None:
        """Store one acquisition-side checksum sample."""
        with self._lock:
            self._acquisitions[(camera_id, sequence)] = IntegritySample(
                camera_id=camera_id,
                sequence=sequence,
                hw_frame_id=hw_frame_id,
                crc32=crc32,
                wall_ts=wall_ts,
                mono_ts=mono_ts,
            )
            while len(self._acquisitions) > self._max_samples:
                self._acquisitions.popitem(last=False)
            self._for(camera_id).acquisitions_sampled += 1

    def record_snapshot(
        self,
        camera_id: str,
        sequence: int,
        hw_frame_id: int | None,
        crc32: int,
        wall_ts: float,
        mono_ts: float,
    ) -> bool | None:
        """Correlate one consumer-snapshot checksum.

        Returns True on match, False on mismatch, None when no acquisition
        sample exists for ``(camera_id, sequence)`` (e.g. sampling cadence
        mismatch, registry eviction, or consumer started mid-stream).
        """
        with self._lock:
            stats = self._for(camera_id)
            sample = self._acquisitions.get((camera_id, sequence))
            stats.snapshots_checked += 1
            if sample is None:
                stats.snapshots_unsampled += 1
                return None
            if sample.crc32 == crc32:
                stats.snapshots_matched += 1
                return True
            stats.snapshots_mismatched += 1
            mismatch = IntegrityMismatch(
                camera_id=camera_id,
                sequence=sequence,
                hw_frame_id=hw_frame_id,
                acquisition_crc32=sample.crc32,
                snapshot_crc32=crc32,
                wall_ts=wall_ts,
            )
            stats.last_mismatch = mismatch
            self._last_mismatch = mismatch
            return False

    def acquisition_sample(self, camera_id: str, sequence: int) -> IntegritySample | None:
        """Return the stored acquisition sample for one frame, if present."""
        with self._lock:
            return self._acquisitions.get((camera_id, sequence))

    def stats(self, camera_id: str | None = None) -> IntegrityStats:
        """Aggregate stats, optionally filtered to one camera (a copy)."""
        with self._lock:
            if camera_id is not None:
                existing = self._stats.get(camera_id)
                if existing is None:
                    return IntegrityStats(camera_id=camera_id)
                return IntegrityStats(
                    camera_id=camera_id,
                    acquisitions_sampled=existing.acquisitions_sampled,
                    snapshots_checked=existing.snapshots_checked,
                    snapshots_matched=existing.snapshots_matched,
                    snapshots_mismatched=existing.snapshots_mismatched,
                    snapshots_unsampled=existing.snapshots_unsampled,
                    last_mismatch=existing.last_mismatch,
                )
            total = IntegrityStats(camera_id=None)
            for existing in self._stats.values():
                total.acquisitions_sampled += existing.acquisitions_sampled
                total.snapshots_checked += existing.snapshots_checked
                total.snapshots_matched += existing.snapshots_matched
                total.snapshots_mismatched += existing.snapshots_mismatched
                total.snapshots_unsampled += existing.snapshots_unsampled
            total.last_mismatch = self._last_mismatch
            return total

    def reset(self, camera_id: str | None = None) -> None:
        """Clear samples and counters (all cameras, or one)."""
        with self._lock:
            if camera_id is None:
                self._acquisitions.clear()
                self._stats.clear()
                self._last_mismatch = None
                return
            for key in [k for k in self._acquisitions if k[0] == camera_id]:
                del self._acquisitions[key]
            self._stats.pop(camera_id, None)
            if self._last_mismatch is not None and self._last_mismatch.camera_id == camera_id:
                self._last_mismatch = None


_DEFAULT_REGISTRY = FrameIntegrityRegistry()


def get_default_registry() -> FrameIntegrityRegistry:
    """Process-global registry used when a component is not given one."""
    return _DEFAULT_REGISTRY


__all__ = [
    "DEFAULT_SAMPLE_EVERY",
    "DIAG_ENV_VAR",
    "EVERY_ENV_VAR",
    "FrameIntegrityRegistry",
    "IntegrityMismatch",
    "IntegritySample",
    "IntegrityStats",
    "compute_thermal_crc",
    "get_default_registry",
    "integrity_diag_enabled",
    "integrity_sample_every",
]
