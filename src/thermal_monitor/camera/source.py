"""
camera.source -- frame-producer contract for acquisition (Stage 8G).

This module owns the acquisition-side producer interface and its error
taxonomy. It has no HALCON, GVCP, GVSP, threading, or GUI dependencies.

The custom TV46L driver (:mod:`thermal_monitor.camera.tv46_custom`)
implements :class:`FrameSource`; :class:`AcquisitionWorker` drives it.
Offline playback sources implement the same protocol so live and offline
share one processing path.

Stage 8G: HALCON acquisition (``TV46LDriver``) is removed. This module is
the single home for the contract previously defined in the deleted
``camera.driver`` module.
"""

from __future__ import annotations

from typing import Protocol

from thermal_monitor.camera.model import (
    CameraValidationResult,
)

#: Timeout (ms) for the mandatory first-frame gate in ``reopen()``.
FIRST_FRAME_TIMEOUT_MS = 5000


class CameraConnectionError(RuntimeError):
    """Failed to open/configure the camera control or stream path."""


class CameraGrabError(RuntimeError):
    """A grab failed for a reason other than a timeout."""


class CameraGrabTimeout(CameraGrabError):
    """A grab did not return within the configured timeout."""


class FrameSource(Protocol):
    """Interface between acquisition orchestration and a frame producer.

    The worker depends only on this protocol so the acquisition loop can be
    tested with a fake source and reused for offline playback.
    """

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def grab(self, timeout_ms: int): ...

    def is_connected(self) -> bool: ...

    def reopen(self) -> None: ...

    def validate_registers(
        self, expected_scda_ip: str = "", expected_fusion_value: int = 3
    ) -> CameraValidationResult:
        """Validate camera registers match expected configuration.

        Called during CONTROL_READY → STREAM_CONFIGURED → FUSION_READY
        transitions. Returns a CameraValidationResult indicating which
        checks passed/failed.
        """
        ...


__all__ = [
    "FIRST_FRAME_TIMEOUT_MS",
    "CameraConnectionError",
    "CameraGrabError",
    "CameraGrabTimeout",
    "FrameSource",
]
