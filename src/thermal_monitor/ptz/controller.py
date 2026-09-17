"""ptz.controller -- per-PTZ command workflow over OpcUaSession (Phase 5).

Owns, for one PTZ instance: gating against authoritative status, command
validation, dispatch (logical fields + command strobe), movement
completion monitoring, stop / clear-error / calibration workflows, and
operation bookkeeping. No threads owned here: every wait is a bounded
monotonic-deadline poll, safe to call from any worker thread (including
a future QThread host). No Qt, no camera, no database imports.

The COMMAND strobe values are a SIMULATOR PROTOCOL detail, isolated
behind :class:`CommandStrobe` so the Siemens handshake can replace them
without touching command logic.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from thermal_monitor.ptz.client import (
    OpcUaSession,
    coerce_bool,
    coerce_float,
    translate_error,
)
from thermal_monitor.ptz.errors import (
    PtzError,
    PtzErrorCategory,
    PtzValidationError,
)
from thermal_monitor.ptz.mapping import LogicalField, PtzMapping
from thermal_monitor.ptz.models import (
    MoveMode,
    PtzCommand,
    PtzLimits,
    PtzTolerance,
    VelocityMode,
    require_finite_value,
    within_tolerance,
)
from thermal_monitor.ptz.state import (
    CalibrationState,
    PlcConnectionState,
    PtzMovementState,
    PtzStatus,
)

logger = logging.getLogger(__name__)

_OPERATION_IDS = itertools.count(1)


class PtzOperationState(str, Enum):
    """Lifecycle of one tracked PTZ operation."""

    ACCEPTED = "accepted"  # validated, dispatch pending
    ACKNOWLEDGED = "acknowledged"  # server accepted the OPC UA writes
    MOVING = "moving"  # authoritative MOVING observed
    REACHED = "reached"  # authoritative reached + within tolerance
    FAILED = "failed"  # timeout / loss / rejection / PTZ error
    CANCELLED = "cancelled"  # stopped, superseded, or shut down


@dataclass(frozen=True, slots=True)
class PtzOperation:
    """Immutable snapshot of one operation for UI display / tracking."""

    operation_id: str
    ptz_id: str
    target_pan: float
    target_tilt: float
    state: PtzOperationState = PtzOperationState.ACCEPTED
    started_monotonic: float = 0.0
    finished_monotonic: float = 0.0
    actual_pan: Optional[float] = None
    actual_tilt: Optional[float] = None
    movement: PtzMovementState = PtzMovementState.IDLE
    error: Optional[PtzError] = None
    note: str = ""

    @property
    def elapsed_s(self) -> float:
        end = self.finished_monotonic or time.monotonic()
        return max(0.0, end - self.started_monotonic)

    @property
    def terminal(self) -> bool:
        return self.state in (
            PtzOperationState.REACHED,
            PtzOperationState.FAILED,
            PtzOperationState.CANCELLED,
        )


class PtzCommandError(RuntimeError):
    """Raised for synchronous command failures; carries structured detail."""

    def __init__(self, error: PtzError, operation: Optional[PtzOperation] = None) -> None:
        super().__init__(f"{error.code}: {error.message}")
        self.error = error
        self.operation = operation


class CommandStrobe:
    """Issues command-strobe writes. SIMULATOR PROTOCOL default values.

    The real Siemens handshake is UNKNOWN; a future implementation
    replaces these three methods without touching command logic.
    """

    MOVE = 1
    STOP = 2
    CLEAR_ERROR = 3

    def strobe_move(
        self, session: OpcUaSession, mapping: PtzMapping, ptz_id: str
    ) -> None:
        """Signal "execute staged target"."""
        session.write_node(mapping.resolve(LogicalField.COMMAND, ptz_id), self.MOVE)

    def strobe_stop(
        self, session: OpcUaSession, mapping: PtzMapping, ptz_id: str
    ) -> None:
        """Signal "abort motion in place"."""
        session.write_node(mapping.resolve(LogicalField.COMMAND, ptz_id), self.STOP)

    def strobe_clear_error(
        self, session: OpcUaSession, mapping: PtzMapping, ptz_id: str
    ) -> None:
        """Signal "clear latched error"."""
        session.write_node(
            mapping.resolve(LogicalField.COMMAND, ptz_id), self.CLEAR_ERROR
        )


def _new_operation(ptz_id: str, command: PtzCommand) -> PtzOperation:
    now = time.monotonic()
    return PtzOperation(
        operation_id=f"ptzop-{next(_OPERATION_IDS):06d}",
        ptz_id=ptz_id,
        target_pan=command.pan,
        target_tilt=command.tilt,
        state=PtzOperationState.ACCEPTED,
        started_monotonic=now,
    )


def _finish(operation: PtzOperation, **changes) -> PtzOperation:
    values = {
        "operation_id": operation.operation_id,
        "ptz_id": operation.ptz_id,
        "target_pan": operation.target_pan,
        "target_tilt": operation.target_tilt,
        "state": operation.state,
        "started_monotonic": operation.started_monotonic,
        "finished_monotonic": operation.finished_monotonic,
        "actual_pan": operation.actual_pan,
        "actual_tilt": operation.actual_tilt,
        "movement": operation.movement,
        "error": operation.error,
        "note": operation.note,
    }
    values.update(changes)
    if values["state"] in (
        PtzOperationState.REACHED,
        PtzOperationState.FAILED,
        PtzOperationState.CANCELLED,
    ):
        values["finished_monotonic"] = time.monotonic()
    return PtzOperation(**values)


class PtzController:
    """Command workflow for exactly one ``ptz_id``.

    Thread-safe for concurrent callers (per-PTZ lock guards operation
    bookkeeping only -- never held across network I/O). Latest MOVE wins:
    dispatching while an operation is active supersedes it (marked
    CANCELLED); the simulator behaves the same way.
    """

    def __init__(
        self,
        ptz_id: str,
        session: OpcUaSession,
        mapping: PtzMapping,
        *,
        limits: Optional[PtzLimits] = None,
        tolerance: Optional[PtzTolerance] = None,
        strobe: Optional[CommandStrobe] = None,
        poll_interval_s: float = 0.05,
        move_timeout_s: float = 30.0,
        calibration_timeout_s: float = 60.0,
    ) -> None:
        if not ptz_id or not ptz_id.strip():
            raise PtzValidationError("ptz_id is required")
        if poll_interval_s <= 0:
            raise PtzValidationError("poll_interval_s must be > 0")
        if move_timeout_s <= 0:
            raise PtzValidationError("move_timeout_s must be > 0")
        if calibration_timeout_s <= 0:
            raise PtzValidationError("calibration_timeout_s must be > 0")
        self._ptz_id = ptz_id
        self._session = session
        self._mapping = mapping
        self._limits = limits
        self._tolerance = tolerance or PtzTolerance(pan=0.1, tilt=0.1)
        self._strobe = strobe or CommandStrobe()
        self._poll_interval_s = poll_interval_s
        self._move_timeout_s = move_timeout_s
        self._calibration_timeout_s = calibration_timeout_s
        self._lock = threading.RLock()
        self._operations: dict[str, PtzOperation] = {}
        self._active_id: Optional[str] = None
        self._last_good: Optional[PtzStatus] = None
        self._last_success_monotonic: float = 0.0

    @property
    def ptz_id(self) -> str:
        return self._ptz_id

    # -- status ----------------------------------------------------------

    def read_status(self) -> PtzStatus:
        """Assemble an authoritative snapshot from live node reads.

        On communication failure returns the last-known status degraded
        (``communication_ok=False``) instead of raising, so UI polling
        never throws; dispatch paths use :meth:`read_status_strict`.
        """
        try:
            status = self._read_status_live()
        except Exception as exc:
            logger.debug(
                "[PTZ-CTRL] %s status read failed: %s", self._ptz_id, exc
            )
            with self._lock:
                last = self._last_good
            if last is None:
                return PtzStatus(plc_state=self._session.state)
            return PtzStatus(
                plc_state=self._session.state,
                ptz_available=False,
                communication_ok=False,
                ready=False,
                movement=last.movement,
                actual_pan=last.actual_pan,
                actual_tilt=last.actual_tilt,
                calibration=last.calibration,
                error=last.error,
            )
        with self._lock:
            self._last_good = status
            self._last_success_monotonic = time.monotonic()
        return status

    def read_status_strict(self) -> PtzStatus:
        """Live status or raise :class:`PtzCommandError` (COMMUNICATION)."""
        try:
            status = self._read_status_live()
        except Exception as exc:
            error = translate_error(exc, f"{self._ptz_id}:status")
            raise PtzCommandError(error) from exc
        with self._lock:
            self._last_good = status
            self._last_success_monotonic = time.monotonic()
        return status

    def _read_status_live(self) -> PtzStatus:
        ptz_id = self._ptz_id
        raw = {
            f: self._session.read_field(self._mapping, f, ptz_id)
            for f in (
                LogicalField.ACTUAL_PAN,
                LogicalField.ACTUAL_TILT,
                LogicalField.MOVING,
                LogicalField.POSITION_REACHED,
                LogicalField.READY,
                LogicalField.ERROR,
                LogicalField.ERROR_CODE,
                LogicalField.CALIBRATION_REQUIRED,
                LogicalField.CALIBRATION_ACTIVE,
                LogicalField.CALIBRATION_COMPLETE,
            )
        }
        actual_pan = coerce_float(raw[LogicalField.ACTUAL_PAN], "actual_pan")
        actual_tilt = coerce_float(raw[LogicalField.ACTUAL_TILT], "actual_tilt")
        moving = coerce_bool(raw[LogicalField.MOVING], "moving")
        reached = coerce_bool(raw[LogicalField.POSITION_REACHED], "position_reached")
        ready = coerce_bool(raw[LogicalField.READY], "ready")
        error_flag = coerce_bool(raw[LogicalField.ERROR], "error")
        error_code = raw[LogicalField.ERROR_CODE]
        cal_required = coerce_bool(
            raw[LogicalField.CALIBRATION_REQUIRED], "calibration_required"
        )
        cal_active = coerce_bool(
            raw[LogicalField.CALIBRATION_ACTIVE], "calibration_active"
        )
        cal_complete = coerce_bool(
            raw[LogicalField.CALIBRATION_COMPLETE], "calibration_complete"
        )
        movement = (
            PtzMovementState.ERROR
            if error_flag
            else (
                PtzMovementState.MOVING
                if moving
                else (
                    PtzMovementState.POSITION_REACHED
                    if reached
                    else PtzMovementState.IDLE
                )
            )
        )
        calibration = (
            CalibrationState.ACTIVE
            if cal_active
            else (
                CalibrationState.COMPLETE
                if cal_complete
                else (
                    CalibrationState.REQUIRED if cal_required else CalibrationState.NOT_REQUIRED
                )
            )
        )
        error = None
        if error_flag:
            try:
                code = int(error_code)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                code = -1
            error = PtzError(
                code=f"{ptz_id}:ptz-{code}",
                message=f"PTZ reported error code={code}",
                category=PtzErrorCategory.PTZ,
            )
        plc_state = self._session.state
        return PtzStatus(
            plc_state=plc_state,
            ptz_available=True,
            communication_ok=plc_state == PlcConnectionState.CONNECTED,
            ready=ready,
            movement=movement,
            actual_pan=actual_pan,
            actual_tilt=actual_tilt,
            calibration=calibration,
            error=error,
        )

    # -- gating ----------------------------------------------------------

    def _require_accepting(self, status: PtzStatus, action: str) -> None:
        ptz_id = self._ptz_id
        if status.plc_state != PlcConnectionState.CONNECTED or not status.communication_ok:
            raise PtzCommandError(
                PtzError(
                    code=f"{ptz_id}:{action}:not-connected",
                    message=f"Cannot {action}: PLC session is {status.plc_state.value}",
                    category=PtzErrorCategory.COMMUNICATION,
                )
            )
        if status.error is not None:
            raise PtzCommandError(
                PtzError(
                    code=f"{ptz_id}:{action}:ptz-error",
                    message=f"Cannot {action}: {status.error.message}",
                    category=PtzErrorCategory.PTZ,
                )
            )
        if status.calibration == CalibrationState.ACTIVE:
            raise PtzCommandError(
                PtzError(
                    code=f"{ptz_id}:{action}:calibration-active",
                    message=f"Cannot {action}: calibration active",
                    category=PtzErrorCategory.CALIBRATION,
                )
            )
        if not status.ready or not status.ptz_available:
            raise PtzCommandError(
                PtzError(
                    code=f"{ptz_id}:{action}:not-ready",
                    message=f"Cannot {action}: PTZ not ready",
                    category=PtzErrorCategory.PTZ,
                )
            )

    # -- movement --------------------------------------------------------

    def move_absolute(
        self,
        pan: float,
        tilt: float,
        *,
        velocity: Optional[float] = None,
        pan_velocity: Optional[float] = None,
        tilt_velocity: Optional[float] = None,
        velocity_mode: VelocityMode = VelocityMode.SINGLE,
        request_id: str = "",
        wait: bool = True,
        timeout_s: Optional[float] = None,
    ) -> PtzOperation:
        """Validate, dispatch, and optionally await an absolute move.

        Raises :class:`PtzCommandError` for every failure mode, including
        invalid values (category COMMAND_REJECTED).
        """
        try:
            command = PtzCommand(
                pan=require_finite_value("pan", pan),
                tilt=require_finite_value("tilt", tilt),
                velocity_mode=velocity_mode,
                velocity=velocity,
                pan_velocity=pan_velocity,
                tilt_velocity=tilt_velocity,
                move_mode=MoveMode.ABSOLUTE,
                request_id=request_id,
            )
        except PtzValidationError as exc:
            raise PtzCommandError(
                PtzError(
                    code=f"{self._ptz_id}:move:invalid",
                    message=str(exc),
                    category=PtzErrorCategory.COMMAND_REJECTED,
                )
            ) from exc
        return self.dispatch(command, wait=wait, timeout_s=timeout_s)

    def move_relative(
        self,
        delta_pan: float,
        delta_tilt: float,
        *,
        velocity: Optional[float] = None,
        pan_velocity: Optional[float] = None,
        tilt_velocity: Optional[float] = None,
        velocity_mode: VelocityMode = VelocityMode.SINGLE,
        request_id: str = "",
        wait: bool = True,
        timeout_s: Optional[float] = None,
    ) -> PtzOperation:
        """Resolve increments against authoritative actual position, then
        dispatch the resulting absolute command. Never sends relative
        values to the protocol boundary; rejects when actual position is
        unavailable or communication is unhealthy."""
        delta_pan = require_finite_value("delta_pan", delta_pan)
        delta_tilt = require_finite_value("delta_tilt", delta_tilt)
        status = self.read_status_strict()
        if not status.communication_ok or not status.ptz_available:
            raise PtzCommandError(
                PtzError(
                    code=f"{self._ptz_id}:relative:unavailable",
                    message="Cannot move relative: authoritative actual "
                    "position unavailable",
                    category=PtzErrorCategory.COMMUNICATION,
                )
            )
        try:
            relative = PtzCommand(
                pan=delta_pan,
                tilt=delta_tilt,
                velocity_mode=velocity_mode,
                velocity=velocity,
                pan_velocity=pan_velocity,
                tilt_velocity=tilt_velocity,
                move_mode=MoveMode.RELATIVE,
                request_id=request_id,
            )
        except PtzValidationError as exc:
            raise PtzCommandError(
                PtzError(
                    code=f"{self._ptz_id}:relative:invalid",
                    message=str(exc),
                    category=PtzErrorCategory.COMMAND_REJECTED,
                )
            ) from exc
        return self.dispatch(
            relative.to_absolute(status.actual_pan, status.actual_tilt),
            wait=wait,
            timeout_s=timeout_s,
        )

    def dispatch(
        self,
        command: PtzCommand,
        *,
        wait: bool = True,
        timeout_s: Optional[float] = None,
    ) -> PtzOperation:
        """Dispatch an ABSOLUTE command; optionally await completion."""
        if command.move_mode != MoveMode.ABSOLUTE:
            raise PtzCommandError(
                PtzError(
                    code=f"{self._ptz_id}:dispatch:relative",
                    message="Controller boundary accepts absolute commands "
                    "only; resolve via PtzCommand.to_absolute first",
                    category=PtzErrorCategory.COMMAND_REJECTED,
                )
            )
        try:
            command.validate(self._limits)
        except PtzValidationError as exc:
            raise PtzCommandError(
                PtzError(
                    code=f"{self._ptz_id}:dispatch:invalid",
                    message=str(exc),
                    category=PtzErrorCategory.COMMAND_REJECTED,
                )
            ) from exc
        status = self.read_status_strict()
        try:
            self._require_accepting(status, "move")
        except PtzCommandError as exc:
            raise self._attach_rejected(command, exc) from exc
        operation = _new_operation(self._ptz_id, command)
        self._register_active(operation, note="superseded by newer MOVE")
        try:
            self._session.write_command(
                self._mapping, self._ptz_id, command, self._limits
            )
        except Exception as exc:
            return self._fail_active(
                operation, translate_error(exc, f"{self._ptz_id}:dispatch")
            )
        # Simulator convention: mirror SINGLE velocity onto the axis
        # nodes so the server stays in SINGLE mode (deviating axis values
        # arm PER_AXIS). Siemens mapping will define its own rule later.
        if command.velocity_mode == VelocityMode.SINGLE:
            assert command.velocity is not None
            for axis_field in (
                LogicalField.PAN_VELOCITY,
                LogicalField.TILT_VELOCITY,
            ):
                try:
                    self._session.write_node(
                        self._mapping.resolve(axis_field, self._ptz_id),
                        command.velocity,
                    )
                except Exception as exc:
                    return self._fail_active(
                        operation,
                        translate_error(exc, f"{self._ptz_id}:dispatch"),
                    )
        try:
            self._strobe.strobe_move(self._session, self._mapping, self._ptz_id)
        except Exception as exc:
            return self._fail_active(
                operation, translate_error(exc, f"{self._ptz_id}:strobe")
            )
        operation = self._update_active(
            operation.operation_id,
            state=PtzOperationState.ACKNOWLEDGED,
            movement=PtzMovementState.IDLE,
        )
        if not wait:
            return operation
        return self._await_reached(
            operation, timeout_s if timeout_s is not None else self._move_timeout_s
        )

    # -- stop / clear ------------------------------------------------------

    def stop(self, *, timeout_s: float = 5.0) -> PtzOperation:
        """Abort motion; confirms MOVING clears (bounded)."""
        status = self.read_status_strict()
        self._cancel_active("STOP issued")
        if status.plc_state != PlcConnectionState.CONNECTED:
            raise PtzCommandError(
                PtzError(
                    code=f"{self._ptz_id}:stop:not-connected",
                    message="Cannot stop: PLC session not connected",
                    category=PtzErrorCategory.COMMUNICATION,
                )
            )
        try:
            self._strobe.strobe_stop(self._session, self._mapping, self._ptz_id)
        except Exception as exc:
            raise PtzCommandError(
                translate_error(exc, f"{self._ptz_id}:stop")
            ) from exc
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            current = self.read_status()
            if not current.moving:
                return self._track_ephemeral(
                    PtzOperationState.CANCELLED,
                    current,
                    note="stopped",
                )
            time.sleep(self._poll_interval_s)
        raise PtzCommandError(
            PtzError(
                code=f"{self._ptz_id}:stop:timeout",
                message="STOP did not clear MOVING in time",
                category=PtzErrorCategory.MOVEMENT_TIMEOUT,
            )
        )

    def clear_error(self, *, timeout_s: float = 5.0) -> PtzStatus:
        """Clear a latched PTZ error; confirms the flag clears (bounded)."""
        try:
            self._strobe.strobe_clear_error(
                self._session, self._mapping, self._ptz_id
            )
        except Exception as exc:
            raise PtzCommandError(
                translate_error(exc, f"{self._ptz_id}:clear-error")
            ) from exc
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            current = self.read_status_strict()
            if current.error is None:
                return current
            time.sleep(self._poll_interval_s)
        raise PtzCommandError(
            PtzError(
                code=f"{self._ptz_id}:clear-error:timeout",
                message="Error flag did not clear in time",
                category=PtzErrorCategory.PTZ,
            )
        )

    # -- calibration -------------------------------------------------------

    def request_calibration(
        self, *, wait: bool = True, timeout_s: Optional[float] = None
    ) -> PtzStatus:
        """Request calibration; optionally await COMPLETE (bounded).

        Policy: calibration while MOVING is rejected explicitly; movement
        while calibration is ACTIVE is rejected by gating.
        """
        status = self.read_status_strict()
        if (
            status.plc_state != PlcConnectionState.CONNECTED
            or not status.communication_ok
        ):
            raise PtzCommandError(
                PtzError(
                    code=f"{self._ptz_id}:calibration:not-connected",
                    message="Cannot calibrate: PLC session not connected",
                    category=PtzErrorCategory.COMMUNICATION,
                )
            )
        if status.error is not None:
            raise PtzCommandError(
                PtzError(
                    code=f"{self._ptz_id}:calibration:ptz-error",
                    message=f"Cannot calibrate: {status.error.message}",
                    category=PtzErrorCategory.PTZ,
                )
            )
        if status.moving:
            raise PtzCommandError(
                PtzError(
                    code=f"{self._ptz_id}:calibration:moving",
                    message="Cannot calibrate while moving: STOP first",
                    category=PtzErrorCategory.CALIBRATION,
                )
            )
        try:
            self._session.write_node(
                self._mapping.resolve(
                    LogicalField.CALIBRATION_REQUEST, self._ptz_id
                ),
                True,
            )
        except Exception as exc:
            raise PtzCommandError(
                translate_error(exc, f"{self._ptz_id}:calibration")
            ) from exc
        if not wait:
            return self.read_status()
        return self._await_calibration(
            timeout_s if timeout_s is not None else self._calibration_timeout_s
        )

    # -- operation tracking --------------------------------------------------

    def get_operation(self, operation_id: str) -> Optional[PtzOperation]:
        with self._lock:
            return self._operations.get(operation_id)

    def active_operation(self) -> Optional[PtzOperation]:
        with self._lock:
            if self._active_id is None:
                return None
            return self._operations.get(self._active_id)

    def wait_for_operation(
        self, operation_id: str, timeout_s: float
    ) -> PtzOperation:
        """Await a previously dispatched (``wait=False``) operation."""
        with self._lock:
            operation = self._operations.get(operation_id)
        if operation is None:
            raise PtzCommandError(
                PtzError(
                    code=f"{self._ptz_id}:wait:unknown-operation",
                    message=f"Unknown operation {operation_id}",
                    category=PtzErrorCategory.UNKNOWN,
                )
            )
        if operation.terminal:
            return operation
        return self._await_reached(operation, timeout_s)

    # -- internals -----------------------------------------------------------

    def _register_active(
        self, operation: PtzOperation, note: str
    ) -> None:
        with self._lock:
            previous = (
                self._operations.get(self._active_id)
                if self._active_id is not None
                else None
            )
            if previous is not None and not previous.terminal:
                self._operations[previous.operation_id] = _finish(
                    previous, state=PtzOperationState.CANCELLED, note=note
                )
            self._operations[operation.operation_id] = operation
            self._active_id = operation.operation_id

    def _update_active(self, operation_id: str, **changes) -> PtzOperation:
        with self._lock:
            current = self._operations.get(operation_id)
            if current is None:  # pragma: no cover - defensive
                raise PtzCommandError(
                    PtzError(
                        code=f"{self._ptz_id}:internal:lost-operation",
                        message="Operation lost during dispatch",
                        category=PtzErrorCategory.UNKNOWN,
                    )
                )
            updated = _finish(current, **changes)
            self._operations[operation_id] = updated
            return updated

    def _fail_active(self, operation: PtzOperation, error: PtzError) -> PtzOperation:
        failed = self._update_active(operation.operation_id, state=PtzOperationState.FAILED, error=error)
        raise PtzCommandError(error, failed)

    def _attach_rejected(
        self, command: PtzCommand, exc: PtzCommandError
    ) -> PtzCommandError:
        operation = _new_operation(self._ptz_id, command)
        rejected = _finish(operation, state=PtzOperationState.FAILED, error=exc.error)
        with self._lock:
            self._operations[rejected.operation_id] = rejected
        return PtzCommandError(exc.error, rejected)

    def _cancel_active(self, note: str) -> None:
        with self._lock:
            if self._active_id is None:
                return
            current = self._operations.get(self._active_id)
            if current is not None and not current.terminal:
                self._operations[current.operation_id] = _finish(
                    current, state=PtzOperationState.CANCELLED, note=note
                )
            self._active_id = None

    def _track_ephemeral(
        self, state: PtzOperationState, status: PtzStatus, note: str
    ) -> PtzOperation:
        operation = PtzOperation(
            operation_id=f"ptzop-{next(_OPERATION_IDS):06d}",
            ptz_id=self._ptz_id,
            target_pan=status.actual_pan,
            target_tilt=status.actual_tilt,
            state=state,
            started_monotonic=time.monotonic(),
            finished_monotonic=time.monotonic(),
            actual_pan=status.actual_pan,
            actual_tilt=status.actual_tilt,
            movement=status.movement,
            note=note,
        )
        with self._lock:
            self._operations[operation.operation_id] = operation
        return operation

    def _await_reached(
        self, operation: PtzOperation, timeout_s: float
    ) -> PtzOperation:
        if timeout_s <= 0:
            raise PtzValidationError("timeout_s must be > 0")
        deadline = time.monotonic() + timeout_s
        seen_moving = False
        while True:
            if self._is_superseded(operation.operation_id):
                return self._update_active(
                    operation.operation_id,
                    state=PtzOperationState.CANCELLED,
                    note="superseded by newer MOVE",
                )
            try:
                status = self._read_status_live()
            except Exception as exc:
                if self._session.state != PlcConnectionState.CONNECTED:
                    return self._update_active(
                        operation.operation_id,
                        state=PtzOperationState.FAILED,
                        error=translate_error(
                            exc, f"{self._ptz_id}:move:connection-lost"
                        ),
                    )
                if time.monotonic() >= deadline:
                    return self._update_active(
                        operation.operation_id,
                        state=PtzOperationState.FAILED,
                        error=PtzError(
                            code=f"{self._ptz_id}:move:timeout",
                            message="Movement timed out",
                            category=PtzErrorCategory.MOVEMENT_TIMEOUT,
                        ),
                    )
                time.sleep(self._poll_interval_s)
                continue
            with self._lock:
                self._last_good = status
                self._last_success_monotonic = time.monotonic()
            if status.error is not None:
                return self._update_active(
                    operation.operation_id,
                    state=PtzOperationState.FAILED,
                    actual_pan=status.actual_pan,
                    actual_tilt=status.actual_tilt,
                    movement=status.movement,
                    error=PtzError(
                        code=f"{self._ptz_id}:move:ptz-error",
                        message=status.error.message,
                        category=PtzErrorCategory.PTZ,
                    ),
                )
            if status.calibration == CalibrationState.ACTIVE:
                return self._update_active(
                    operation.operation_id,
                    state=PtzOperationState.FAILED,
                    error=PtzError(
                        code=f"{self._ptz_id}:move:calibration-active",
                        message="Calibration became active during movement",
                        category=PtzErrorCategory.CALIBRATION,
                    ),
                )
            if status.moving:
                seen_moving = True
                operation = self._update_active(
                    operation.operation_id,
                    state=PtzOperationState.MOVING,
                    actual_pan=status.actual_pan,
                    actual_tilt=status.actual_tilt,
                    movement=status.movement,
                )
            reached_flag = status.position_reached
            in_tol = within_tolerance(
                status.actual_pan,
                status.actual_tilt,
                operation.target_pan,
                operation.target_tilt,
                self._tolerance,
            )
            # Require BOTH the authoritative flag and our own tolerance
            # check; never trust a flag outside tolerance.
            if reached_flag and in_tol and (seen_moving or in_tol):
                return self._update_active(
                    operation.operation_id,
                    state=PtzOperationState.REACHED,
                    actual_pan=status.actual_pan,
                    actual_tilt=status.actual_tilt,
                    movement=status.movement,
                )
            if time.monotonic() >= deadline:
                return self._update_active(
                    operation.operation_id,
                    state=PtzOperationState.FAILED,
                    actual_pan=status.actual_pan,
                    actual_tilt=status.actual_tilt,
                    movement=status.movement,
                    error=PtzError(
                        code=f"{self._ptz_id}:move:timeout",
                        message="Movement timed out",
                        category=PtzErrorCategory.MOVEMENT_TIMEOUT,
                    ),
                )
            time.sleep(self._poll_interval_s)

    def _await_calibration(self, timeout_s: float) -> PtzStatus:
        if timeout_s <= 0:
            raise PtzValidationError("timeout_s must be > 0")
        deadline = time.monotonic() + timeout_s
        seen_active = False
        while True:
            try:
                status = self._read_status_live()
            except Exception as exc:
                if self._session.state != PlcConnectionState.CONNECTED:
                    raise PtzCommandError(
                        translate_error(exc, f"{self._ptz_id}:calibration:lost")
                    ) from exc
                if time.monotonic() >= deadline:
                    raise PtzCommandError(
                        PtzError(
                            code=f"{self._ptz_id}:calibration:timeout",
                            message="Calibration timed out",
                            category=PtzErrorCategory.CALIBRATION,
                        )
                    ) from None
                time.sleep(self._poll_interval_s)
                continue
            with self._lock:
                self._last_good = status
                self._last_success_monotonic = time.monotonic()
            if status.calibration == CalibrationState.ACTIVE:
                seen_active = True
            elif seen_active:
                if status.error is not None:
                    raise PtzCommandError(
                        PtzError(
                            code=f"{self._ptz_id}:calibration:failed",
                            message=status.error.message,
                            category=PtzErrorCategory.CALIBRATION,
                        )
                    )
                if status.calibration == CalibrationState.COMPLETE:
                    return status
                # ACTIVE cleared without COMPLETE and without error:
                # treat COMPLETE-equivalent only when ready and no error.
                if status.ready and status.error is None:
                    return status
                raise PtzCommandError(
                    PtzError(
                        code=f"{self._ptz_id}:calibration:incomplete",
                        message="Calibration ended without COMPLETE",
                        category=PtzErrorCategory.CALIBRATION,
                    )
                )
            if time.monotonic() >= deadline:
                raise PtzCommandError(
                    PtzError(
                        code=f"{self._ptz_id}:calibration:timeout",
                        message="Calibration timed out",
                        category=PtzErrorCategory.CALIBRATION,
                    )
                )
            time.sleep(self._poll_interval_s)

    def _is_superseded(self, operation_id: str) -> bool:
        # An in-flight waiter is done when its operation is no longer the
        # active one: superseded by a newer MOVE (different id) or
        # cancelled via STOP/shutdown (active id cleared to None).
        with self._lock:
            return self._active_id != operation_id


__all__ = [
    "CommandStrobe",
    "PtzCommandError",
    "PtzController",
    "PtzOperation",
    "PtzOperationState",
]
