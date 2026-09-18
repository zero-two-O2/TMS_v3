"""roi.errors -- typed errors for the Phase 10 ROI subsystem."""

from __future__ import annotations


class RoiError(Exception):
    """Base class for ROI domain errors."""


class RoiValidationError(RoiError):
    """Geometry, binding, or property validation failed."""


class RoiBindingError(RoiValidationError):
    """Camera + PTZ + position binding is missing or mismatched."""


class RoiStaleContextError(RoiError):
    """A result or edit targeted a superseded ROI context."""


class RoiPersistenceError(RoiError):
    """Repository / migration failure."""


__all__ = [
    "RoiError",
    "RoiValidationError",
    "RoiBindingError",
    "RoiStaleContextError",
    "RoiPersistenceError",
]
