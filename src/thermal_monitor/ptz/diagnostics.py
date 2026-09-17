"""ptz.diagnostics -- hardware acceptance verification runner (Phase 7).

Read-only verification by default against any reachable OPC UA endpoint
through the existing ``PtzService`` + mapping path. Movement checks run
only with explicit operator confirmation. Every check logs timestamp,
PTZ ID, operation ID and outcome; nothing is ever labelled passed
without executing.

Simulator acceptance and hardware acceptance share this runner; the
report always records which backend was exercised so simulator results
can never be mistaken for Siemens verification.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from thermal_monitor.ptz.controller import PtzOperationState
from thermal_monitor.ptz.mapping import LogicalField

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    """One verification outcome. ``passed=None`` means not executed."""

    name: str
    passed: Optional[bool]
    detail: str = ""
    ptz_id: str = ""
    operation_id: str = ""
    timestamp: float = 0.0


@dataclass
class DiagnosticsReport:
    """Ordered check outcomes for one backend run."""

    backend: str
    endpoint: str
    checks: list[DiagnosticCheck] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.passed is True)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c.passed is False)

    @property
    def skipped(self) -> int:
        return sum(1 for c in self.checks if c.passed is None)

    def summary(self) -> str:
        return (
            f"diagnostics backend={self.backend} endpoint={self.endpoint} "
            f"passed={self.passed} failed={self.failed} skipped={self.skipped}"
        )


def _check(
    report: DiagnosticsReport,
    name: str,
    ptz_id: str,
    func: Callable[[], str],
    operation_id: str = "",
) -> None:
    """Execute one check; record pass/fail explicitly, never assume."""
    try:
        detail = func()
    except Exception as exc:
        report.checks.append(
            DiagnosticCheck(
                name=name, passed=False, detail=str(exc)[:300],
                ptz_id=ptz_id, operation_id=operation_id,
                timestamp=time.time(),
            )
        )
        logger.warning(
            "[PTZ-DIAG] %s ptz=%s FAIL %s", name, ptz_id, str(exc)[:200]
        )
        return
    report.checks.append(
        DiagnosticCheck(
            name=name, passed=True, detail=str(detail)[:300],
            ptz_id=ptz_id, operation_id=operation_id,
            timestamp=time.time(),
        )
    )
    logger.info("[PTZ-DIAG] %s ptz=%s PASS %s", name, ptz_id, str(detail)[:200])


def run_read_only_checks(
    service,
    mapping,
    camera_id: str,
    ptz_id: str,
    *,
    backend: str,
    endpoint: str,
) -> DiagnosticsReport:
    """Connection, namespace mapping, reads, datatypes, identity, status."""
    report = DiagnosticsReport(backend=backend, endpoint=endpoint)

    def _connect() -> str:
        service.connect()
        return f"session={service._session.state.value}"

    _check(report, "connect", ptz_id, _connect)

    def _resolve() -> str:
        fields = sorted(f.value for f in mapping.available_fields(ptz_id))
        if not fields:
            raise RuntimeError("mapping exposes no fields")
        return f"{len(fields)} fields"

    _check(report, "namespace_resolution", ptz_id, _resolve)

    for fname, conv in (
        ("actual_pan", float),
        ("actual_tilt", float),
        ("moving", bool),
        ("position_reached", bool),
        ("ready", bool),
        ("error_code", int),
    ):
        logical = LogicalField(fname)

        def _read(logical=logical, fname=fname, conv=conv) -> str:
            value = service._session.read_field(mapping, logical, ptz_id)
            if isinstance(value, bool) != (conv is bool):
                if conv is bool and not isinstance(value, bool):
                    raise TypeError(f"{fname}={value!r} is not boolean")
                if conv is not bool and (
                    isinstance(value, bool) or not isinstance(value, (int, float))
                ):
                    raise TypeError(f"{fname}={value!r} is not numeric")
            return f"{fname}={value!r}"

        _check(report, f"read_{fname}", ptz_id, _read)

    def _status() -> str:
        status = service.get_status(camera_id)
        return (
            f"plc={status.plc_state.value} ready={status.ready} "
            f"actual=({status.actual_pan:.2f},{status.actual_tilt:.2f})"
        )

    _check(report, "status_snapshot", ptz_id, _status)
    logger.info("[PTZ-DIAG] %s", report.summary())
    return report


def run_movement_checks(
    service,
    camera_id: str,
    ptz_id: str,
    *,
    backend: str,
    endpoint: str,
    target_pan: float,
    target_tilt: float,
    velocity: float,
    timeout_s: float = 30.0,
    confirmed: bool = False,
) -> DiagnosticsReport:
    """Movement verification. Requires explicit ``confirmed=True`` plus
    pre-confirmed limits (checked by the caller/operator, not here)."""
    report = DiagnosticsReport(backend=backend, endpoint=endpoint)
    if not confirmed:
        report.checks.append(
            DiagnosticCheck(
                name="movement",
                passed=None,
                detail="skipped: operator confirmation required",
                ptz_id=ptz_id,
                timestamp=time.time(),
            )
        )
        return report

    holder: dict = {}

    def _move() -> str:
        operation = service.move_absolute(
            camera_id, target_pan, target_tilt, velocity=velocity,
            timeout_s=timeout_s,
        )
        holder["operation_id"] = operation.operation_id
        if operation.state != PtzOperationState.REACHED:
            raise RuntimeError(f"movement ended in {operation.state.value}")
        return f"op={operation.operation_id} reached"

    _check(report, "movement", ptz_id, _move,
           operation_id=holder.get("operation_id", ""))

    def _stop() -> str:
        operation = service.stop(camera_id)
        return f"stop={operation.state.value}"

    _check(report, "stop", ptz_id, _stop)
    logger.info("[PTZ-DIAG] %s", report.summary())
    return report


def main(argv: Optional[list[str]] = None) -> int:
    """Minimal CLI: ``python -m thermal_monitor.ptz.diagnostics --help``."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="opc.tcp://127.0.0.1:4840")
    parser.add_argument("--ptz-id", default="PTZ_01")
    parser.add_argument("--backend", default="simulator")
    parser.add_argument("--confirm-movement", action="store_true")
    parser.add_argument("--target-pan", type=float, default=10.0)
    parser.add_argument("--target-tilt", type=float, default=0.0)
    parser.add_argument("--velocity", type=float, default=10.0)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from thermal_monitor.ptz.client import (
        AsyncuaTransport,
        OpcUaClientConfig,
        OpcUaSession,
    )
    from thermal_monitor.ptz.mapping import SimulatorPtzMapping
    from thermal_monitor.ptz.models import PtzStationBinding
    from thermal_monitor.ptz.service import PtzService

    camera_id = "diag_cam"
    mapping = SimulatorPtzMapping(ptz_ids=(args.ptz_id,))
    session = OpcUaSession(
        OpcUaClientConfig(endpoint=args.endpoint), AsyncuaTransport()
    )
    service = PtzService(session, mapping)
    service.register_binding(
        PtzStationBinding(camera_id=camera_id, ptz_id=args.ptz_id)
    )
    read_only = run_read_only_checks(
        service, mapping, camera_id, args.ptz_id,
        backend=args.backend, endpoint=args.endpoint,
    )
    movement = run_movement_checks(
        service, camera_id, args.ptz_id,
        backend=args.backend, endpoint=args.endpoint,
        target_pan=args.target_pan, target_tilt=args.target_tilt,
        velocity=args.velocity, confirmed=args.confirm_movement,
    )
    service.shutdown()
    print(read_only.summary())
    print(movement.summary())
    return 0 if (read_only.failed == 0 and movement.failed == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DiagnosticCheck",
    "DiagnosticsReport",
    "main",
    "run_movement_checks",
    "run_read_only_checks",
]
