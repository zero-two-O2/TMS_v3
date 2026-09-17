"""OPC UA front-end for the TMS_v3 PTZ/PLC Simulator.

Exposes exactly the node set defined by the Phase 3
``SimulatorPtzMapping`` (the single authoritative mapping -- node IDs are
never duplicated here) under ``urn:tms:ptz:sim``. One asyncio loop hosts
both the asyncua server and the simulation tick; command edges
(COMMAND, CALIBRATION_REQUEST) are polled each tick and the engine
snapshot is published back to the status nodes.

Requires the ``ptz`` extra (asyncua). Headless: no Qt, no camera, no DB.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from thermal_monitor.ptz.mapping import (
    LogicalField,
    NodeAccess,
    NodeDataType,
    SimulatorPtzMapping,
)

from tools.ptz_plc_simulator.ptz_simulator import (
    CMD_CLEAR_ERROR,
    CMD_IDLE,
    CMD_MOVE,
    CMD_STOP,
    PtzSimulationEngine,
)
from tools.ptz_plc_simulator.simulator_config import SimulatorConfig

logger = logging.getLogger(__name__)

try:
    from asyncua import Server, ua
except ImportError as exc:  # pragma: no cover - import guard
    raise RuntimeError(
        "asyncua is required for the PTZ simulator. "
        "Install with: pip install thermal-monitoring-system[ptz]"
    ) from exc


_VARIANT = {
    NodeDataType.FLOAT: ua.VariantType.Double,
    NodeDataType.BOOL: ua.VariantType.Boolean,
    # Simulator INT choice: Int64 so plain Python ints round-trip
    # without client-side variant plumbing. The Phase 3 mapping labels
    # this abstractly as "int"; the real Siemens datatype is unknown.
    NodeDataType.INT: ua.VariantType.Int64,
    NodeDataType.STRING: ua.VariantType.String,
}

# Command-input fields staged into the engine every tick.
_COMMAND_INPUTS = (
    LogicalField.TARGET_PAN,
    LogicalField.TARGET_TILT,
    LogicalField.VELOCITY,
    LogicalField.PAN_VELOCITY,
    LogicalField.TILT_VELOCITY,
)


class PtzPlcSimulatorServer:
    """asyncua server + simulation tick with sync start/stop for embedding."""

    def __init__(
        self,
        config: SimulatorConfig,
        mapping: SimulatorPtzMapping | None = None,
    ) -> None:
        self._config = config
        self._mapping = mapping or SimulatorPtzMapping(ptz_ids=config.ptz_ids)
        self.engine = PtzSimulationEngine(config)
        self._server: Server | None = None
        self._nodes: dict[tuple[str, LogicalField], object] = {}
        self._last_command: dict[str, int] = {}
        self._last_cal_request: dict[str, bool] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_event: asyncio.Event | None = None
        self._listening = False

    # -- embedding API ---------------------------------------------------

    def start_background(self, timeout_s: float = 10.0) -> None:
        """Start the server + tick loop on a daemon thread (tests/dev)."""
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="PtzSimServer", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout_s):
            raise TimeoutError("simulator server did not start in time")

    def stop_background(self, timeout_s: float = 10.0) -> None:
        """Deterministic shutdown: stop tick, stop server, join thread.

        Idempotent: safe to call twice (e.g. explicit test shutdown
        followed by fixture teardown)."""
        loop = self._loop
        if (
            loop is not None
            and self._stop_event is not None
            and not loop.is_closed()
        ):
            try:
                loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass
        thread = self._thread
        if thread is not None and thread.ident != threading.get_ident():
            thread.join(timeout_s)
            if thread.is_alive():
                logger.warning("[PTZ-SIM] server thread did not stop in time")
        self._thread = None
        self._loop = None

    def stop_listening(self, timeout_s: float = 10.0) -> None:
        """Communication-loss scenario: stop the OPC UA endpoint while the
        engine keeps simulating. Resume with :meth:`start_listening`."""
        future = asyncio.run_coroutine_threadsafe(
            self._set_listening(False), self._require_loop()
        )
        future.result(timeout_s)

    def start_listening(self, timeout_s: float = 10.0) -> None:
        """Resume the OPC UA endpoint after :meth:`stop_listening`."""
        future = asyncio.run_coroutine_threadsafe(
            self._set_listening(True), self._require_loop()
        )
        future.result(timeout_s)

    @property
    def endpoint(self) -> str:
        return self._config.endpoint

    async def serve_forever(self) -> None:
        """Run the server until cancelled (``__main__`` entry point)."""
        await self._serve()

    # -- asyncio internals -------------------------------------------------

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("simulator server is not running")
        return self._loop

    def _run(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._server = Server()
        await self._server.init()
        self._server.set_endpoint(self._config.endpoint)
        namespace_index = await self._server.register_namespace(
            self._config.namespace_uri
        )
        expected = self._mapping.namespace_index
        if namespace_index != expected:
            raise RuntimeError(
                f"Simulator namespace index mismatch: server={namespace_index} "
                f"mapping={expected}. STOP: node IDs would diverge from "
                "SimulatorPtzMapping."
            )
        await self._build_nodes()
        await self._set_listening(True)
        logger.info(
            "[PTZ-SIM] started endpoint=%s namespace=%s ptz_count=%d",
            self._config.endpoint,
            self._config.namespace_uri,
            len(self._config.ptz_ids),
        )
        self._ready.set()
        try:
            await self._tick_loop()
        finally:
            await self._set_listening(False)
            logger.info("[PTZ-SIM] stopped")

    async def _build_nodes(self) -> None:
        assert self._server is not None
        objects = self._server.get_objects_node()
        for ptz_id in self._config.ptz_ids:
            parent = await objects.add_object(
                f"ns={self._mapping.namespace_index};s={ptz_id}", ptz_id
            )
            snapshot = self.engine.snapshot(ptz_id)
            state = self.engine.state(ptz_id)
            for field in self._mapping.available_fields(ptz_id):
                descriptor = self._mapping.resolve(field, ptz_id)
                datatype = NodeDataType(descriptor.datatype)
                access = NodeAccess(descriptor.access)
                initial = self._initial_value(field, snapshot, state)
                node = await parent.add_variable(
                    descriptor.node_id,
                    field.value,
                    initial,
                    varianttype=_VARIANT[datatype],
                )
                if access in (NodeAccess.WRITE, NodeAccess.READ_WRITE):
                    await node.set_writable()
                self._nodes[(ptz_id, field)] = node
            self._last_command[ptz_id] = CMD_IDLE
            self._last_cal_request[ptz_id] = False

    def _initial_value(
        self, field: LogicalField, snapshot: dict, state
    ) -> object:
        cfg = self._config
        defaults = {
            LogicalField.TARGET_PAN: cfg.default_pan,
            LogicalField.TARGET_TILT: cfg.default_tilt,
            LogicalField.VELOCITY: cfg.default_velocity,
            LogicalField.PAN_VELOCITY: cfg.default_velocity,
            LogicalField.TILT_VELOCITY: cfg.default_velocity,
            LogicalField.COMMAND: CMD_IDLE,
            LogicalField.CALIBRATION_REQUEST: False,
        }
        if field in defaults:
            return defaults[field]
        return snapshot[field]

    async def _tick_loop(self) -> None:
        assert self._loop is not None and self._stop_event is not None
        period = 1.0 / self._config.update_hz
        last = self._loop.time()
        while not self._stop_event.is_set():
            now = self._loop.time()
            dt = max(0.0, now - last)
            last = now
            try:
                await self._tick_once(dt)
            except Exception:
                logger.exception("[PTZ-SIM] tick failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=period)
            except asyncio.TimeoutError:
                pass

    async def _tick_once(self, dt_s: float) -> None:
        for ptz_id in self._config.ptz_ids:
            await self._ingest_commands(ptz_id)
        self.engine.tick(dt_s)
        for ptz_id in self._config.ptz_ids:
            await self._publish(ptz_id)

    async def _ingest_commands(self, ptz_id: str) -> None:
        state = self.engine.state(ptz_id)

        def _read(field: LogicalField, default: object = 0.0):
            node = self._nodes[(ptz_id, field)]
            return node.read_value()

        target_pan = await _read(LogicalField.TARGET_PAN)
        target_tilt = await _read(LogicalField.TARGET_TILT)
        velocity = await _read(LogicalField.VELOCITY)
        pan_velocity = await _read(LogicalField.PAN_VELOCITY)
        tilt_velocity = await _read(LogicalField.TILT_VELOCITY)
        self.engine.set_target(
            ptz_id,
            float(target_pan),
            float(target_tilt),
            velocity=float(velocity),
            pan_velocity=float(pan_velocity),
            tilt_velocity=float(tilt_velocity),
        )
        # PER_AXIS is armed when the client deviates either axis velocity
        # from the shared VELOCITY value (see command_to_fields).
        state.use_per_axis_velocity = bool(
            float(pan_velocity) != float(velocity)
            or float(tilt_velocity) != float(velocity)
        )
        command = int(await _read(LogicalField.COMMAND))
        if command != self._last_command[ptz_id]:
            self._last_command[ptz_id] = command
            if command != CMD_IDLE:
                accepted = self.engine.strobe_command(ptz_id, command)
                logger.debug(
                    "[PTZ-SIM] %s command=%s accepted=%s",
                    ptz_id,
                    command,
                    accepted,
                )
                if command in (CMD_MOVE, CMD_STOP, CMD_CLEAR_ERROR):
                    await self._nodes[(ptz_id, LogicalField.COMMAND)].set_value(
                        CMD_IDLE
                    )
                    self._last_command[ptz_id] = CMD_IDLE
        cal_request = bool(
            await _read(LogicalField.CALIBRATION_REQUEST, False)
        )
        if cal_request and not self._last_cal_request[ptz_id]:
            self.engine.request_calibration(ptz_id)
            await self._nodes[
                (ptz_id, LogicalField.CALIBRATION_REQUEST)
            ].set_value(False)
        self._last_cal_request[ptz_id] = cal_request

    async def _publish(self, ptz_id: str) -> None:
        snapshot = self.engine.snapshot(ptz_id)
        for field, value in snapshot.items():
            node = self._nodes[(ptz_id, field)]
            descriptor = self._mapping.resolve(field, ptz_id)
            # Explicit variants: asyncua infers Int64 from Python int,
            # which must match the created node type exactly.
            await node.set_value(
                ua.Variant(value, _VARIANT[NodeDataType(descriptor.datatype)])
            )

    async def _set_listening(self, listening: bool) -> None:
        if self._server is None:
            return
        if listening and not self._listening:
            await self._server.start()
            self._listening = True
            logger.info("[PTZ-SIM] listening endpoint=%s", self._config.endpoint)
        elif not listening and self._listening:
            await self._server.stop()
            self._listening = False
            logger.info("[PTZ-SIM] not listening")


__all__ = ["PtzPlcSimulatorServer"]
