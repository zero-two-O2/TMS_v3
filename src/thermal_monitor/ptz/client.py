"""ptz.client -- OPC UA session foundation (Phase 3).

Layering (see ADR-005)::

    PtzCommand / PtzStatus        (models.py / state.py -- no imports here
    from the session into the model)
             |
    OpcUaSession                 (this module: session lifecycle, timeouts,
             |                    reconnect, error translation; PTZ-aware
    PtzMapping                   only through injected mapping objects)
             |
    OpcUaTransport               (protocol.py: HOW to communicate)
             |
    AsyncuaTransport | fake      (concrete backend, injected)

The session never blocks the GUI by itself: every transport call carries
an explicit bounded timeout, and reconnection runs on a private daemon
thread. Qt hosting (a future QObject wrapper) will call this same API
from a QThread -- no Qt import exists in this package so it stays
importable and testable headless.

No Siemens assumptions: node identity always comes from the injected
``PtzMapping`` (simulator now, Siemens later).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Optional

from thermal_monitor.ptz.errors import (
    PtzError,
    PtzErrorCategory,
    PtzValidationError,
)
from thermal_monitor.ptz.mapping import (
    LogicalField,
    PtzMapping,
    PtzMappingError,
)
from thermal_monitor.ptz.models import (
    MoveMode,
    PtzCommand,
    PtzLimits,
    VelocityMode,
    require_finite_value,
)
from thermal_monitor.ptz.protocol import (
    EventCallback,
    OpcUaNodeError,
    OpcUaTimeoutError,
    OpcUaTransport,
    OpcUaTransportError,
    PtzNodeDescriptor,
    SubscriptionHandle,
    TransportEvent,
    ValueCallback,
)
from thermal_monitor.ptz.state import PlcConnectionState

logger = logging.getLogger(__name__)


class OpcUaSecurity(str, Enum):
    """Authentication mode. The simulator uses ANONYMOUS; production
    configuration selects USERNAME or CERTIFICATE later without changing
    this API. Certificate infrastructure is NOT implemented here."""

    ANONYMOUS = "anonymous"
    USERNAME = "username"
    CERTIFICATE = "certificate"


@dataclass(frozen=True, slots=True)
class OpcUaClientConfig:
    """Session configuration. Defaults are transport-safe starting
    points, not production-tuned Siemens values."""

    endpoint: str = ""
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 3.0
    write_timeout_s: float = 3.0
    subscribe_timeout_s: float = 5.0
    disconnect_timeout_s: float = 3.0
    reconnect_max_attempts: int = 10
    reconnect_initial_backoff_s: float = 1.0
    reconnect_backoff_factor: float = 2.0
    reconnect_max_backoff_s: float = 30.0
    security: OpcUaSecurity = OpcUaSecurity.ANONYMOUS
    username: Optional[str] = None
    password: Optional[str] = None

    def __post_init__(self) -> None:
        for name in (
            "connect_timeout_s",
            "read_timeout_s",
            "write_timeout_s",
            "subscribe_timeout_s",
            "disconnect_timeout_s",
            "reconnect_initial_backoff_s",
            "reconnect_max_backoff_s",
        ):
            value = getattr(self, name)
            try:
                number = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise PtzValidationError(f"{name} must be numeric") from exc
            if not number > 0:
                raise PtzValidationError(f"{name} must be > 0, got {value!r}")
            object.__setattr__(self, name, number)
        if self.reconnect_max_attempts < 0:
            raise PtzValidationError("reconnect_max_attempts must be >= 0")
        if self.reconnect_backoff_factor < 1.0:
            raise PtzValidationError("reconnect_backoff_factor must be >= 1.0")
        if not isinstance(self.security, OpcUaSecurity):
            raise PtzValidationError("security must be an OpcUaSecurity")
        if self.security == OpcUaSecurity.USERNAME and not self.username:
            raise PtzValidationError("USERNAME security requires username")


ConnectionCallback = Callable[[PlcConnectionState, str], None]
"""``(state, detail)`` -- no Qt dependency; the future Qt wrapper adapts."""


def translate_error(exc: BaseException, context: str) -> PtzError:
    """Convert a transport/mapping failure into the Phase 2 contract."""
    context = context or "opc-ua"
    if isinstance(exc, OpcUaTimeoutError):
        return PtzError(
            code=f"{context}:timeout",
            message=f"OPC UA timeout in {context}: {exc}",
            category=PtzErrorCategory.COMMUNICATION,
        )
    if isinstance(exc, OpcUaNodeError):
        return PtzError(
            code=f"{context}:node",
            message=f"OPC UA node failure in {context}: {exc}",
            category=PtzErrorCategory.PLC,
        )
    if isinstance(exc, PtzMappingError):
        return PtzError(
            code=f"{context}:mapping",
            message=f"PTZ mapping failure in {context}: {exc}",
            category=PtzErrorCategory.UNKNOWN,
        )
    if isinstance(exc, OpcUaTransportError):
        return PtzError(
            code=f"{context}:transport",
            message=f"OPC UA transport failure in {context}: {exc}",
            category=PtzErrorCategory.COMMUNICATION,
        )
    if isinstance(exc, PtzValidationError):
        return PtzError(
            code=f"{context}:invalid",
            message=f"Invalid PTZ value in {context}: {exc}",
            category=PtzErrorCategory.COMMAND_REJECTED,
        )
    return PtzError(
        code=f"{context}:unknown",
        message=f"Unexpected failure in {context}: {exc!r}",
        category=PtzErrorCategory.UNKNOWN,
    )


def coerce_float(value: object, field_name: str) -> float:
    """Strict server-value conversion. Never invents zero for garbage."""
    if isinstance(value, bool):
        raise OpcUaNodeError(f"{field_name}: bool {value!r} is not a float")
    if not isinstance(value, (int, float)):
        raise OpcUaNodeError(
            f"{field_name}: expected numeric, got {type(value).__name__}"
        )
    try:
        return require_finite_value(field_name, value)
    except PtzValidationError as exc:
        raise OpcUaNodeError(f"{field_name}: {exc}") from exc


def coerce_bool(value: object, field_name: str) -> bool:
    """Strict server-value conversion for status flags."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise OpcUaNodeError(
        f"{field_name}: expected bool, got {value!r}"
    )


def command_to_fields(command: PtzCommand) -> dict[LogicalField, float]:
    """Prove a logical command maps onto logical OPC UA fields.

    Absolute commands only -- relative resolution stays in the
    controller via ``PtzCommand.to_absolute``. The future command/strobe
    handshake is UNKNOWN and intentionally absent here.
    """
    if command.move_mode != MoveMode.ABSOLUTE:
        raise PtzValidationError(
            "command_to_fields requires an absolute PtzCommand; "
            "resolve RELATIVE via PtzCommand.to_absolute first"
        )
    fields: dict[LogicalField, float] = {
        LogicalField.TARGET_PAN: command.pan,
        LogicalField.TARGET_TILT: command.tilt,
    }
    if command.velocity_mode == VelocityMode.SINGLE:
        assert command.velocity is not None
        fields[LogicalField.VELOCITY] = command.velocity
    else:
        assert command.pan_velocity is not None
        assert command.tilt_velocity is not None
        fields[LogicalField.PAN_VELOCITY] = command.pan_velocity
        fields[LogicalField.TILT_VELOCITY] = command.tilt_velocity
    return fields


@dataclass
class _SubscriptionRecord:
    mapping: PtzMapping
    field: LogicalField
    ptz_id: str
    callback: ValueCallback
    handle: Optional[SubscriptionHandle] = None


class OpcUaSession:
    """One OPC UA session shared by all PTZ instances on an endpoint.

    Generic transport + injected mapping: ``ptz_id`` selects the tag set,
    so PTZ_01..PTZ_08 never need per-instance clients. A second endpoint
    (future multi-PLC hardware) means a second session, same class.
    """

    def __init__(
        self,
        config: OpcUaClientConfig,
        transport: OpcUaTransport,
        *,
        on_connection_changed: Optional[ConnectionCallback] = None,
        on_error: Optional[Callable[[PtzError], None]] = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._on_connection_changed = on_connection_changed
        self._on_error = on_error
        self._lock = threading.RLock()
        self._state = PlcConnectionState.DISCONNECTED
        self._subscriptions: dict[str, _SubscriptionRecord] = {}
        self._reconnect_thread: Optional[threading.Thread] = None
        self._reconnect_stop = threading.Event()
        self._shutdown = False

    # -- state ---------------------------------------------------------

    @property
    def state(self) -> PlcConnectionState:
        with self._lock:
            return self._state

    def _set_state(self, state: PlcConnectionState, detail: str = "") -> None:
        with self._lock:
            if self._state == state and state not in (
                PlcConnectionState.RECONNECTING,
            ):
                return
            self._state = state
        logger.info("[OPC-UA] state=%s %s", state.value, detail)
        callback = self._on_connection_changed
        if callback is not None:
            try:
                callback(state, detail)
            except Exception:  # never let UI callbacks break the session
                logger.exception("[OPC-UA] connection callback failed")

    def _report_error(self, error: PtzError) -> None:
        logger.warning("[OPC-UA] %s %s", error.code, error.message)
        callback = self._on_error
        if callback is not None:
            try:
                callback(error)
            except Exception:
                logger.exception("[OPC-UA] error callback failed")

    # -- lifecycle -----------------------------------------------------

    def connect(self) -> None:
        """Establish the session with a bounded timeout."""
        with self._lock:
            if self._shutdown:
                raise OpcUaTransportError("session is shut down")
            endpoint = self._config.endpoint
        if not endpoint:
            raise PtzMappingError("OPC UA endpoint is not configured")
        self._set_state(PlcConnectionState.CONNECTING, f"endpoint={endpoint}")
        logger.info("[OPC-UA] connecting endpoint=%s", endpoint)
        try:
            self._transport.connect(endpoint, self._config.connect_timeout_s)
        except Exception as exc:
            self._set_state(PlcConnectionState.ERROR, str(exc)[:200])
            error = translate_error(exc, "connect")
            self._report_error(error)
            raise OpcUaTransportError(error.message) from exc
        self._set_state(PlcConnectionState.CONNECTED, f"endpoint={endpoint}")
        logger.info("[OPC-UA] connected")

    def disconnect(self) -> None:
        """Release the session. Never raises when already disconnected."""
        self._stop_reconnect_thread()
        try:
            self._transport.disconnect(self._config.disconnect_timeout_s)
        except Exception as exc:
            logger.warning("[OPC-UA] disconnect issue: %s", exc)
        finally:
            self._set_state(PlcConnectionState.DISCONNECTED, "")

    def shutdown(self, timeout_s: float = 5.0) -> None:
        """Deterministic bounded shutdown: stop reconnect, drop
        subscriptions best-effort, disconnect, join the worker."""
        with self._lock:
            self._shutdown = True
        self._stop_reconnect_thread(timeout_s=timeout_s)
        with self._lock:
            records = list(self._subscriptions.values())
            self._subscriptions.clear()
        for record in records:
            if record.handle is not None:
                try:
                    self._transport.unsubscribe(record.handle)
                except Exception:
                    pass
        try:
            self._transport.disconnect(self._config.disconnect_timeout_s)
        except Exception:
            pass
        self._set_state(PlcConnectionState.DISCONNECTED, "shutdown")

    # -- generic transport API (no PTZ semantics) -----------------------

    def read_node(self, node: PtzNodeDescriptor) -> object:
        """Read one node with the configured read timeout."""
        try:
            return self._transport.read(node, self._config.read_timeout_s)
        except Exception as exc:
            self._on_transport_failure(exc, f"read:{node.logical_name}")
            raise

    def write_node(self, node: PtzNodeDescriptor, value: object) -> None:
        """Write one node. Success = server accepted the value only."""
        try:
            self._transport.write(node, value, self._config.write_timeout_s)
        except Exception as exc:
            self._on_transport_failure(exc, f"write:{node.logical_name}")
            raise
        logger.debug(
            "[OPC-UA] write field=%s value=%r", node.logical_name, value
        )

    def subscribe(
        self,
        mapping: PtzMapping,
        field: LogicalField,
        ptz_id: str,
        callback: ValueCallback,
    ) -> SubscriptionHandle:
        """Monitor one mapped field; restored automatically on reconnect."""
        node = mapping.resolve(field, ptz_id)
        handle = self._transport.subscribe(
            node, callback, self._config.subscribe_timeout_s
        )
        key = f"{ptz_id}.{field.value}"
        with self._lock:
            self._subscriptions[key] = _SubscriptionRecord(
                mapping=mapping,
                field=field,
                ptz_id=ptz_id,
                callback=callback,
                handle=handle,
            )
        logger.info("[OPC-UA] subscription established ptz=%s field=%s", ptz_id, field.value)
        return handle

    def unsubscribe(self, handle: SubscriptionHandle) -> None:
        with self._lock:
            for key, record in list(self._subscriptions.items()):
                if record.handle is not None and record.handle.token == handle.token:
                    del self._subscriptions[key]
        try:
            self._transport.unsubscribe(handle)
        except Exception:
            pass

    # -- PTZ-aware helpers (mapping-driven, still no controller logic) --

    def read_field(
        self, mapping: PtzMapping, field: LogicalField, ptz_id: str
    ) -> object:
        """Resolve through the mapping, then read."""
        try:
            node = mapping.resolve(field, ptz_id)
        except Exception as exc:
            self._report_error(translate_error(exc, "resolve"))
            raise
        logger.debug("[PTZ-MAP] resolved ptz=%s field=%s", ptz_id, field.value)
        return self.read_node(node)

    def write_command(
        self,
        mapping: PtzMapping,
        ptz_id: str,
        command: PtzCommand,
        limits: Optional[PtzLimits] = None,
    ) -> dict[LogicalField, float]:
        """Validate and write a logical command's fields.

        Returns the written field set. A success means the server
        accepted the values -- position-reached authority stays with
        later status reads (never inferred here).
        """
        command.validate(limits)
        fields = command_to_fields(command)
        for logical_field, value in fields.items():
            self.write_node(mapping.resolve(logical_field, ptz_id), value)
        return fields

    # -- connection loss + reconnect ------------------------------------

    def notify_connection_lost(self, detail: str = "") -> None:
        """Hook for transport-driven loss detection."""
        with self._lock:
            if self._state in (
                PlcConnectionState.DISCONNECTED,
                PlcConnectionState.ERROR,
            ):
                return
            if self._shutdown:
                self._set_state(PlcConnectionState.DISCONNECTED, detail)
                return
        logger.warning("[OPC-UA] connection lost %s", detail)
        self._set_state(PlcConnectionState.COMMUNICATION_LOST, detail)
        self._report_error(
            PtzError(
                code="session:connection-lost",
                message=f"OPC UA connection lost: {detail}",
                category=PtzErrorCategory.COMMUNICATION,
            )
        )
        self._start_reconnect_thread()

    def _on_transport_failure(self, exc: Exception, context: str) -> None:
        self._report_error(translate_error(exc, context))

    def _start_reconnect_thread(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            existing = self._reconnect_thread
            if existing is not None and existing.is_alive():
                return
            self._reconnect_stop.clear()
            thread = threading.Thread(
                target=self._reconnect_loop,
                name="PtzOpcUaReconnect",
                daemon=True,
            )
            self._reconnect_thread = thread
        thread.start()

    def _stop_reconnect_thread(self, timeout_s: float = 5.0) -> None:
        with self._lock:
            thread = self._reconnect_thread
        if thread is None:
            return
        self._reconnect_stop.set()
        if threading.get_ident() == thread.ident:
            return  # never join self
        thread.join(timeout=max(0.0, timeout_s))
        if thread.is_alive():
            logger.warning("[OPC-UA] reconnect worker did not stop in time")

    def _reconnect_loop(self) -> None:
        """Background bounded reconnect with backoff; restores state."""
        max_attempts = self._config.reconnect_max_attempts
        backoff = self._config.reconnect_initial_backoff_s
        attempt = 0
        self._set_state(PlcConnectionState.RECONNECTING, "attempt=0")
        while not self._reconnect_stop.is_set():
            attempt += 1
            if max_attempts > 0 and attempt > max_attempts:
                logger.warning("[OPC-UA] reconnect attempts exhausted")
                self._set_state(PlcConnectionState.ERROR, "reconnect exhausted")
                self._report_error(
                    PtzError(
                        code="session:reconnect-exhausted",
                        message="OPC UA reconnect attempts exhausted",
                        category=PtzErrorCategory.COMMUNICATION,
                    )
                )
                return
            logger.info("[OPC-UA] reconnecting attempt=%d", attempt)
            self._set_state(
                PlcConnectionState.RECONNECTING, f"attempt={attempt}"
            )
            try:
                self._transport.connect(
                    self._config.endpoint, self._config.connect_timeout_s
                )
            except Exception as exc:
                logger.info(
                    "[OPC-UA] reconnect attempt=%d failed: %s", attempt, exc
                )
                backoff = min(
                    backoff * self._config.reconnect_backoff_factor,
                    self._config.reconnect_max_backoff_s,
                )
                self._reconnect_stop.wait(min(backoff, 5.0))
                continue
            self._restore_subscriptions()
            self._set_state(PlcConnectionState.CONNECTED, "reconnected")
            logger.info("[OPC-UA] reconnected")
            return

    def _restore_subscriptions(self) -> None:
        with self._lock:
            records = list(self._subscriptions.values())
        for record in records:
            if self._reconnect_stop.is_set():
                return
            try:
                node = record.mapping.resolve(record.field, record.ptz_id)
                handle = self._transport.subscribe(
                    node, record.callback, self._config.subscribe_timeout_s
                )
                record.handle = handle
            except Exception as exc:
                logger.warning(
                    "[OPC-UA] subscription restore failed ptz=%s field=%s: %s",
                    record.ptz_id,
                    record.field.value,
                    exc,
                )


class AsyncuaTransport:
    """Concrete :class:`OpcUaTransport` backed by the ``asyncua`` package.

    Runs a private event loop on a daemon thread and exposes a blocking
    API with explicit timeouts, so Qt workers (and tests with a real
    local server) call it like any other transport. ``asyncua`` is
    imported lazily: importing this module never requires it.
    """

    def __init__(self) -> None:
        try:
            import asyncua  # noqa: F401
        except ImportError as exc:
            raise OpcUaTransportError(
                "asyncua is required for AsyncuaTransport. "
                "Install with: pip install thermal-monitoring-system[ptz]"
            ) from exc
        import asyncio
        import concurrent.futures

        self._asyncio = asyncio
        self._loop = asyncio.new_event_loop()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="PtzOpcUaLoop"
        )
        self._client: object = None
        self._lock = threading.RLock()
        self._loop_thread = threading.Thread(
            target=self._run_loop, name="PtzOpcUaLoop", daemon=True
        )
        self._loop_thread.start()

    # -- loop plumbing ---------------------------------------------------

    def _run_loop(self) -> None:
        self._asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run(self, coro, timeout_s: float):
        future = self._asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout_s)
        except TimeoutError as exc:
            raise OpcUaTimeoutError(f"operation timed out after {timeout_s}s") from exc
        except Exception as exc:
            raise self._wrap(exc) from exc

    @staticmethod
    def _wrap(exc: Exception) -> OpcUaTransportError:
        message = str(exc)
        if "timeout" in type(exc).__name__.lower() or "timeout" in message.lower():
            return OpcUaTimeoutError(message)
        return OpcUaTransportError(message)

    # -- OpcUaTransport ----------------------------------------------------

    def connect(self, endpoint: str, timeout_s: float) -> None:
        from asyncua import Client

        async def _connect() -> None:
            client = Client(endpoint)
            await client.connect()
            with self._lock:
                self._client = client

        self._run(_connect(), timeout_s)

    def disconnect(self, timeout_s: float) -> None:
        async def _disconnect() -> None:
            with self._lock:
                client = self._client
                self._client = None
            if client is not None:
                try:
                    await client.disconnect()  # type: ignore[union-attr]
                except Exception:
                    pass

        try:
            self._run(_disconnect(), timeout_s)
        except OpcUaTransportError:
            pass

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._client is not None

    def _node(self, node: PtzNodeDescriptor):
        with self._lock:
            client = self._client
        if client is None:
            raise OpcUaTransportError("not connected")
        return client.get_node(node.node_id)  # type: ignore[union-attr]

    def read(self, node: PtzNodeDescriptor, timeout_s: float) -> object:
        opc_node = self._node(node)

        async def _read() -> object:
            try:
                return await opc_node.read_value()
            except Exception as exc:
                raise OpcUaNodeError(
                    f"node {node.node_id}: {exc}"
                ) from exc

        return self._run(_read(), timeout_s)

    def write(self, node: PtzNodeDescriptor, value: object, timeout_s: float) -> None:
        opc_node = self._node(node)

        async def _write() -> None:
            try:
                await opc_node.write_value(value)
            except Exception as exc:
                raise OpcUaTransportError(
                    f"node {node.node_id}: {exc}"
                ) from exc

        self._run(_write(), timeout_s)

    def subscribe(
        self, node: PtzNodeDescriptor, callback: ValueCallback, timeout_s: float
    ) -> SubscriptionHandle:
        from asyncua import Subscription

        opc_node = self._node(node)

        class _Handler:
            def datachange_notification(self, _node, value, _data) -> None:  # noqa: ANN001, ANN202
                try:
                    callback(node, value)
                except Exception:
                    logger.exception("[OPC-UA] subscription callback failed")

        async def _subscribe() -> SubscriptionHandle:
            with self._lock:
                client = self._client
            subscription = await client.create_subscription(100, _Handler())  # type: ignore[union-attr]
            await subscription.subscribe_data_change(opc_node)
            return SubscriptionHandle(
                node=node, token=f"asyncua-{id(subscription)}"
            )

        handle = self._run(_subscribe(), timeout_s)
        return handle

    def unsubscribe(self, handle: SubscriptionHandle) -> None:
        # The server-side monitored item is released on disconnect;
        # explicit per-item deletion is a Phase 4 refinement once the
        # simulator exercises this path.
        logger.debug("[OPC-UA] unsubscribe %s", handle.token)


__all__ = [
    "AsyncuaTransport",
    "ConnectionCallback",
    "OpcUaClientConfig",
    "OpcUaSecurity",
    "OpcUaSession",
    "coerce_bool",
    "coerce_float",
    "command_to_fields",
    "translate_error",
]
