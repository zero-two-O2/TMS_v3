"""Simulator configuration. Every default is a SIMULATOR DEFAULT for
development/testing -- NOT a real machine specification. Real Siemens
limits, tolerance, and timing are unknown and live nowhere in this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field


SIMULATOR_NAMESPACE_URI = "urn:tms:ptz:sim"
SIMULATOR_DEFAULT_ENDPOINT = "opc.tcp://127.0.0.1:4840"
SIMULATOR_TEST_ENDPOINT = "opc.tcp://127.0.0.1:4841"


def default_ptz_ids(count: int = 8) -> tuple[str, ...]:
    """PTZ_01..PTZ_<count> instance identifiers."""
    return tuple(f"PTZ_{index:02d}" for index in range(1, count + 1))


@dataclass(frozen=True, slots=True)
class SimulatorConfig:
    """Frozen simulator behaviour. ``sys.path`` must include the repo root
    for ``thermal_monitor`` imports when run as ``__main__``."""

    ptz_ids: tuple[str, ...] = field(default_factory=default_ptz_ids)
    endpoint: str = SIMULATOR_DEFAULT_ENDPOINT
    namespace_uri: str = SIMULATOR_NAMESPACE_URI
    # Initial actual/target position (degrees). SIMULATOR DEFAULT.
    default_pan: float = 0.0
    default_tilt: float = 0.0
    # Accepted command envelope (degrees). SIMULATOR DEFAULT.
    min_pan: float = -170.0
    max_pan: float = 170.0
    min_tilt: float = -90.0
    max_tilt: float = 90.0
    # Velocity envelope (deg/s). SIMULATOR DEFAULT.
    default_velocity: float = 10.0
    min_velocity: float = 0.5
    max_velocity: float = 60.0
    # Position-reached tolerance (degrees, per axis). SIMULATOR DEFAULT.
    tolerance_pan: float = 0.1
    tolerance_tilt: float = 0.1
    # Simulation tick rate (Hz). SIMULATOR DEFAULT, not a hardware rate.
    update_hz: float = 20.0
    # Simulated calibration duration (s). SIMULATOR DEFAULT.
    calibration_duration_s: float = 2.0
    # Position applied when simulated calibration completes.
    # SIMULATOR BEHAVIOUR ONLY -- the real sequence is unknown.
    calibration_home_pan: float = 0.0
    calibration_home_tilt: float = 0.0
    # Whether the simulator starts out demanding calibration.
    calibration_required_initially: bool = True

    def __post_init__(self) -> None:
        if not self.ptz_ids:
            raise ValueError("ptz_ids must not be empty")
        if self.update_hz <= 0:
            raise ValueError("update_hz must be > 0")
        if self.calibration_duration_s < 0:
            raise ValueError("calibration_duration_s must be >= 0")
        if self.tolerance_pan < 0 or self.tolerance_tilt < 0:
            raise ValueError("tolerances must be >= 0")


__all__ = [
    "SIMULATOR_DEFAULT_ENDPOINT",
    "SIMULATOR_NAMESPACE_URI",
    "SIMULATOR_TEST_ENDPOINT",
    "SimulatorConfig",
    "default_ptz_ids",
]
