"""PTZ simulation engine: time-based movement, calibration, error injection.

One engine, N independent per-PTZ states. Deterministic: ``tick(dt)``
advances the physics with an explicit delta so unit tests drive time
without sleeping; the OPC UA server passes real monotonic deltas.

SIMULATOR PROTOCOL (development-only, NOT the Siemens handshake):

* Write TARGET_PAN / TARGET_TILT / VELOCITY (or PAN/TILT_VELOCITY), then
  write COMMAND = 1 (MOVE) to execute. Latest target wins: a MOVE issued
  while moving replaces the target (no command queue).
* COMMAND = 2 (STOP) aborts motion in place.
* COMMAND = 3 (CLEAR_ERROR) clears a latched simulator error.
* Write CALIBRATION_REQUEST = True to start SIMULATED calibration.
  Movement commands issued while calibration is active are rejected
  (never queued).
"""

from __future__ import annotations

import logging
import threading

from thermal_monitor.ptz.mapping import LogicalField

from tools.ptz_plc_simulator.simulator_config import SimulatorConfig
from tools.ptz_plc_simulator.simulator_state import (
    ERR_CALIBRATION_ACTIVE,
    ERR_CALIBRATION_FAILED,
    ERR_INJECTED_FAULT,
    ERR_INVALID_TARGET,
    ERR_INVALID_VELOCITY,
    ERR_NOT_READY,
    NO_ERROR,
    SimulatorPtzState,
)

logger = logging.getLogger(__name__)

# SIMULATOR PROTOCOL command codes (COMMAND node, INT). Development-only.
CMD_IDLE = 0
CMD_MOVE = 1
CMD_STOP = 2
CMD_CLEAR_ERROR = 3


def _approach(current: float, target: float, step: float) -> float:
    """Move ``current`` toward ``target`` by at most ``step`` (no overshoot)."""
    if current < target:
        return min(current + step, target)
    if current > target:
        return max(current - step, target)
    return current


class PtzSimulationEngine:
    """Owns all PTZ instances and advances them with explicit time steps."""

    def __init__(self, config: SimulatorConfig) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._states: dict[str, SimulatorPtzState] = {}
        for ptz_id in config.ptz_ids:
            self._states[ptz_id] = SimulatorPtzState(
                ptz_id=ptz_id,
                target_pan=config.default_pan,
                target_tilt=config.default_tilt,
                actual_pan=config.default_pan,
                actual_tilt=config.default_tilt,
                velocity=config.default_velocity,
                pan_velocity=config.default_velocity,
                tilt_velocity=config.default_velocity,
                calibration_required=config.calibration_required_initially,
            )

    # -- introspection ---------------------------------------------------

    @property
    def config(self) -> SimulatorConfig:
        return self._config

    @property
    def ptz_ids(self) -> tuple[str, ...]:
        return tuple(self._states.keys())

    def state(self, ptz_id: str) -> SimulatorPtzState:
        """Direct mutable handle (tests + server wiring only)."""
        try:
            return self._states[ptz_id]
        except KeyError as exc:
            raise KeyError(f"Unknown PTZ ID {ptz_id!r}") from exc

    def snapshot(self, ptz_id: str) -> dict[LogicalField, object]:
        """Status field values for OPC UA publication / assertions."""
        st = self.state(ptz_id)
        with self._lock:
            return {
                LogicalField.ACTUAL_PAN: st.actual_pan,
                LogicalField.ACTUAL_TILT: st.actual_tilt,
                LogicalField.MOVING: st.moving,
                LogicalField.POSITION_REACHED: st.position_reached,
                LogicalField.READY: st.ready,
                LogicalField.ERROR: st.error,
                LogicalField.ERROR_CODE: st.error_code,
                LogicalField.CALIBRATION_REQUIRED: st.calibration_required,
                LogicalField.CALIBRATION_ACTIVE: st.calibration_active,
                LogicalField.CALIBRATION_COMPLETE: st.calibration_complete,
            }

    # -- command path ----------------------------------------------------

    def set_target(
        self,
        ptz_id: str,
        pan: float,
        tilt: float,
        *,
        velocity: float | None = None,
        pan_velocity: float | None = None,
        tilt_velocity: float | None = None,
    ) -> None:
        """Stage a target. Takes effect on the next MOVE strobe."""
        st = self.state(ptz_id)
        with self._lock:
            st.target_pan = float(pan)
            st.target_tilt = float(tilt)
            if velocity is not None:
                st.velocity = float(velocity)
                st.use_per_axis_velocity = False
            if pan_velocity is not None or tilt_velocity is not None:
                if pan_velocity is not None:
                    st.pan_velocity = float(pan_velocity)
                if tilt_velocity is not None:
                    st.tilt_velocity = float(tilt_velocity)
                st.use_per_axis_velocity = True

    def strobe_command(self, ptz_id: str, code: int) -> bool:
        """Process a SIMULATOR PROTOCOL command. Returns True if accepted."""
        st = self.state(ptz_id)
        with self._lock:
            if code == CMD_MOVE:
                return self._start_move(st)
            if code == CMD_STOP:
                self._stop(st)
                return True
            if code == CMD_CLEAR_ERROR:
                self._clear_error(st)
                return True
            st.last_rejection = f"unknown command code {code!r}"
            logger.warning(
                "[PTZ-SIM] %s rejected unknown command code=%r", ptz_id, code
            )
            return False

    def request_calibration(self, ptz_id: str) -> bool:
        """Start SIMULATED calibration. Returns True if accepted."""
        st = self.state(ptz_id)
        with self._lock:
            if st.calibration_active:
                st.last_rejection = "calibration already active"
                return False
            if st.error:
                st.last_rejection = "calibration rejected: error latched"
                logger.warning("[PTZ-SIM] %s %s", ptz_id, st.last_rejection)
                return False
            st.calibration_active = True
            st.calibration_elapsed_s = 0.0
            st.ready = False
            st.moving = False
            st.position_reached = False
            st.last_rejection = ""
            logger.info("[PTZ-SIM] %s calibration_started", ptz_id)
            return True

    # -- test hooks -------------------------------------------------------

    def inject_ptz_error(self, ptz_id: str, code: int = ERR_INJECTED_FAULT) -> None:
        st = self.state(ptz_id)
        with self._lock:
            self._latch(st, code, f"injected fault code={code}")
            logger.info("[PTZ-SIM] %s error code=%s", ptz_id, code)

    def clear_error(self, ptz_id: str) -> None:
        with self._lock:
            self._clear_error(self.state(ptz_id))

    def inject_motion_freeze(self, ptz_id: str, frozen: bool) -> None:
        """Timeout scenario: command accepted, MOVING held, never converges."""
        with self._lock:
            self.state(ptz_id).freeze_motion = frozen

    def inject_calibration_failure(self, ptz_id: str, armed: bool) -> None:
        with self._lock:
            self.state(ptz_id).fail_next_calibration = armed

    def set_enabled(self, ptz_id: str, enabled: bool) -> None:
        """PTZ not-ready scenario (READY=FALSE, commands rejected)."""
        st = self.state(ptz_id)
        with self._lock:
            st.enabled = enabled
            if not enabled:
                st.ready = False
                st.moving = False
            elif not st.error and not st.calibration_active:
                st.ready = True

    # -- time step --------------------------------------------------------

    def tick(self, dt_s: float) -> None:
        """Advance every PTZ by ``dt_s`` seconds. One PTZ failing never
        affects the others."""
        if dt_s < 0:
            raise ValueError("dt_s must be >= 0")
        with self._lock:
            states = list(self._states.values())
        for st in states:
            try:
                with self._lock:
                    self._tick_one(st, dt_s)
            except Exception:
                logger.exception("[PTZ-SIM] %s tick failed", st.ptz_id)

    # -- internals ---------------------------------------------------------

    def _latch(self, st: SimulatorPtzState, code: int, reason: str) -> None:
        st.error = True
        st.error_code = code
        st.ready = False
        st.moving = False
        st.last_rejection = reason
        logger.info("[PTZ-SIM] %s error code=%s %s", st.ptz_id, code, reason)

    def _clear_error(self, st: SimulatorPtzState) -> None:
        st.error = False
        st.error_code = NO_ERROR
        st.last_rejection = ""
        if st.enabled and not st.calibration_active:
            st.ready = True
        logger.info("[PTZ-SIM] %s error cleared", st.ptz_id)

    def _reject(self, st: SimulatorPtzState, code: int, reason: str) -> bool:
        self._latch(st, code, reason)
        return False

    def _start_move(self, st: SimulatorPtzState) -> bool:
        cfg = self._config
        if st.calibration_active:
            return self._reject(
                st, ERR_CALIBRATION_ACTIVE, "move rejected: calibration active"
            )
        if st.error:
            st.last_rejection = "move rejected: error latched"
            logger.warning("[PTZ-SIM] %s %s", st.ptz_id, st.last_rejection)
            return False
        if not st.enabled or not st.ready:
            return self._reject(st, ERR_NOT_READY, "move rejected: PTZ not ready")
        if not (cfg.min_pan <= st.target_pan <= cfg.max_pan) or not (
            cfg.min_tilt <= st.target_tilt <= cfg.max_tilt
        ):
            return self._reject(
                st,
                ERR_INVALID_TARGET,
                f"move rejected: target ({st.target_pan}, {st.target_tilt}) "
                "outside simulator envelope",
            )
        velocities = (
            (st.pan_velocity, st.tilt_velocity)
            if st.use_per_axis_velocity
            else (st.velocity, st.velocity)
        )
        if any(not (cfg.min_velocity <= v <= cfg.max_velocity) for v in velocities):
            return self._reject(
                st,
                ERR_INVALID_VELOCITY,
                f"move rejected: velocity {velocities} outside simulator envelope",
            )
        st.moving = True
        st.position_reached = False
        st.last_rejection = ""
        logger.info(
            "[PTZ-SIM] %s command target_pan=%s target_tilt=%s %s",
            st.ptz_id,
            st.target_pan,
            st.target_tilt,
            f"pan_velocity={st.pan_velocity} tilt_velocity={st.tilt_velocity}"
            if st.use_per_axis_velocity
            else f"velocity={st.velocity}",
        )
        return True

    def _stop(self, st: SimulatorPtzState) -> None:
        st.moving = False
        logger.info("[PTZ-SIM] %s stopped", st.ptz_id)

    def _tick_one(self, st: SimulatorPtzState, dt_s: float) -> None:
        cfg = self._config
        if st.calibration_active:
            self._tick_calibration(st, dt_s)
            return
        if not st.moving or st.freeze_motion:
            return
        if st.use_per_axis_velocity:
            pan_step = st.pan_velocity * dt_s
            tilt_step = st.tilt_velocity * dt_s
        else:
            pan_step = tilt_step = st.velocity * dt_s
        st.actual_pan = _approach(st.actual_pan, st.target_pan, pan_step)
        st.actual_tilt = _approach(st.actual_tilt, st.target_tilt, tilt_step)
        if abs(st.actual_pan - st.target_pan) <= cfg.tolerance_pan and abs(
            st.actual_tilt - st.target_tilt
        ) <= cfg.tolerance_tilt:
            st.actual_pan = st.target_pan
            st.actual_tilt = st.target_tilt
            st.moving = False
            st.position_reached = True
            logger.info("[PTZ-SIM] %s position_reached", st.ptz_id)
        else:
            logger.debug(
                "[PTZ-SIM] %s moving pan=%s tilt=%s",
                st.ptz_id,
                st.actual_pan,
                st.actual_tilt,
            )

    def _tick_calibration(self, st: SimulatorPtzState, dt_s: float) -> None:
        st.calibration_elapsed_s += dt_s
        if st.calibration_elapsed_s < self._config.calibration_duration_s:
            return
        st.calibration_active = False
        if st.fail_next_calibration:
            st.fail_next_calibration = False
            self._latch(st, ERR_CALIBRATION_FAILED, "simulated calibration failed")
            return
        st.calibration_required = False
        st.calibration_complete = True
        st.ready = st.enabled and not st.error
        st.actual_pan = self._config.calibration_home_pan
        st.actual_tilt = self._config.calibration_home_tilt
        st.target_pan = st.actual_pan
        st.target_tilt = st.actual_tilt
        st.moving = False
        st.position_reached = True
        logger.info("[PTZ-SIM] %s calibration_complete", st.ptz_id)


__all__ = [
    "CMD_CLEAR_ERROR",
    "CMD_IDLE",
    "CMD_MOVE",
    "CMD_STOP",
    "PtzSimulationEngine",
]
