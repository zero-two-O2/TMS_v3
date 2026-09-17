"""ptz.station -- configuration-to-service bridging helpers (Phase 6).

Pure functions over primitive values so ``ptz`` never imports the
application config layer. The UI/configuration layer supplies values
from ``CameraMappingConfig``/``PTZConfig``; this module validates and
converts them into Phase 2/5 domain objects.

Ownership note: a saved position belongs to a ``camera_id`` (primary
filter) and records the ``ptz_id`` resolved at save time. If a binding
later changes, old positions stay listed under the camera with their
stored ``ptz_id`` visible, and Go To refuses when the live binding no
longer matches (explicit error, never silent cross-PTZ motion).
"""

from __future__ import annotations

from typing import Optional

from thermal_monitor.ptz.errors import PtzValidationError
from thermal_monitor.ptz.models import (
    PtzLimits,
    PtzStationBinding,
    PtzTolerance,
)
from thermal_monitor.ptz.service import PtzServiceConfig


def resolve_binding(camera_id: str, ptz_id: str) -> Optional[PtzStationBinding]:
    """Build a station binding, or None when no PTZ is configured.

    Blank ``ptz_id`` means "no PTZ configured" (distinct from
    "PTZ disconnected"). Never defaults to another PTZ.
    """
    if not camera_id or not camera_id.strip():
        raise PtzValidationError("camera_id is required")
    if ptz_id is None or not ptz_id.strip():
        return None
    return PtzStationBinding(camera_id=camera_id, ptz_id=ptz_id.strip())


def merge_limits(
    min_pan: float,
    max_pan: float,
    min_tilt: float,
    max_tilt: float,
    min_velocity: float,
    max_velocity: float,
    *,
    override_min_pan: Optional[float] = None,
    override_max_pan: Optional[float] = None,
    override_min_tilt: Optional[float] = None,
    override_max_tilt: Optional[float] = None,
) -> PtzLimits:
    """Combine global PTZ limits with optional per-camera overrides."""
    return PtzLimits(
        min_pan=override_min_pan if override_min_pan is not None else min_pan,
        max_pan=override_max_pan if override_max_pan is not None else max_pan,
        min_tilt=override_min_tilt if override_min_tilt is not None else min_tilt,
        max_tilt=override_max_tilt if override_max_tilt is not None else max_tilt,
        min_velocity=min_velocity,
        max_velocity=max_velocity,
    )


def build_service_config(
    *,
    limits: Optional[PtzLimits] = None,
    tolerance_pan: float = 0.5,
    tolerance_tilt: float = 0.5,
    move_timeout_s: float = 30.0,
    calibration_timeout_s: float = 120.0,
    monitor_interval_s: float = 0.5,
) -> PtzServiceConfig:
    """Build a service config from validated configuration values."""
    return PtzServiceConfig(
        limits=limits,
        tolerance=PtzTolerance(pan=tolerance_pan, tilt=tolerance_tilt),
        move_timeout_s=move_timeout_s,
        calibration_timeout_s=calibration_timeout_s,
        monitor_interval_s=monitor_interval_s,
    )


def resolve_endpoint(global_endpoint: str, override_endpoint: str) -> str:
    """Per-camera endpoint override wins; empty means not configured."""
    override = (override_endpoint or "").strip()
    if override:
        return override
    return (global_endpoint or "").strip()


__all__ = [
    "build_service_config",
    "merge_limits",
    "resolve_binding",
    "resolve_endpoint",
]
