"""ptz.positions -- persistent PTZ position domain model (Phase 6).

A position belongs to a ``camera_id`` (primary owner/filter) and records
the ``ptz_id`` resolved at save time for binding-change traceability.
ROI data is never duplicated: only the ``roi_set_ref`` (a
``position_id`` key into the existing ``position_roi_associations``
table / ``PositionROIAssociation`` model) is stored.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Optional

from thermal_monitor.ptz.errors import PtzValidationError
from thermal_monitor.ptz.models import require_finite_value


def generate_position_id() -> str:
    """Stable unique position identifier."""
    return f"pos_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True, slots=True)
class PtzPosition:
    """One saved absolute PTZ target with ROI-set reference."""

    position_id: str
    camera_id: str
    ptz_id: str
    name: str
    pan: float
    tilt: float
    velocity: Optional[float] = None
    pan_velocity: Optional[float] = None
    tilt_velocity: Optional[float] = None
    roi_set_ref: str = ""
    enabled: bool = True
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        for label, value in (
            ("position_id", self.position_id),
            ("camera_id", self.camera_id),
            ("ptz_id", self.ptz_id),
            ("name", self.name),
        ):
            if not isinstance(value, str) or not value.strip():
                raise PtzValidationError(f"PtzPosition.{label} is required")
        object.__setattr__(self, "pan", require_finite_value("pan", self.pan))
        object.__setattr__(self, "tilt", require_finite_value("tilt", self.tilt))
        if self.velocity is not None:
            object.__setattr__(
                self, "velocity", require_finite_value("velocity", self.velocity)
            )
            if self.velocity <= 0:
                raise PtzValidationError("PtzPosition.velocity must be > 0")
        for label in ("pan_velocity", "tilt_velocity"):
            value = getattr(self, label)
            if value is not None:
                object.__setattr__(
                    self, label, require_finite_value(label, value)
                )
                if getattr(self, label) <= 0:
                    raise PtzValidationError(f"PtzPosition.{label} must be > 0")
        if not isinstance(self.roi_set_ref, str):
            raise PtzValidationError("PtzPosition.roi_set_ref must be a string")
        for stamp in ("created_at", "updated_at"):
            value = getattr(self, stamp)
            if not isinstance(value, (int, float)) or value < 0:
                raise PtzValidationError(f"PtzPosition.{stamp} must be >= 0")

    def with_updated(self, **changes) -> "PtzPosition":
        """Copy with changes applied and ``updated_at`` refreshed."""
        values = {
            "position_id": self.position_id,
            "camera_id": self.camera_id,
            "ptz_id": self.ptz_id,
            "name": self.name,
            "pan": self.pan,
            "tilt": self.tilt,
            "velocity": self.velocity,
            "pan_velocity": self.pan_velocity,
            "tilt_velocity": self.tilt_velocity,
            "roi_set_ref": self.roi_set_ref,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": time.time(),
        }
        values.update(changes)
        return PtzPosition(**values)


def check_position_binding(position: PtzPosition, active_ptz_id: str) -> None:
    """Refuse Go To when a position's stored PTZ no longer matches the
    live binding. Never silently drive another PTZ."""
    if position.ptz_id != active_ptz_id:
        raise PtzValidationError(
            f"Refusing Go To: position {position.position_id!r} belongs to "
            f"{position.ptz_id}, active PTZ is {active_ptz_id}."
        )


__all__ = ["PtzPosition", "check_position_binding", "generate_position_id"]
