"""ptz.protocol -- OPC UA transport interfaces (Phase 3).

Dependency-light contracts only. The logical PTZ model (``models.py`` /
``state.py``) never imports this module; the session (``client.py``) and
the mappings (``mapping.py``) build on it. No Qt, no asyncua import here:
concrete transports are injected, so unit tests use a fake/in-memory
backend and the package imports without PyQt6 or asyncua installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol


@dataclass(frozen=True, slots=True)
class PtzNodeDescriptor:
    """Identifies one OPC UA node without leaking syntax into callers.

    ``node_id`` is opaque to the application (e.g. ``ns=2;s=PTZ_01.TargetPan``
    for the simulator, or whatever the future Siemens mapping uses).
    """

    node_id: str
    datatype: str
    access: str
    logical_name: str = ""

    def __post_init__(self) -> None:
        if not self.node_id or not self.node_id.strip():
            raise ValueError("PtzNodeDescriptor.node_id is required")
        if not self.datatype or not self.datatype.strip():
            raise ValueError("PtzNodeDescriptor.datatype is required")


class TransportEvent(str, Enum):
    """Connection lifecycle events emitted by a transport."""

    CONNECTED = "connected"
    CONNECTION_LOST = "connection_lost"
    RECONNECTING = "reconnecting"
    DISCONNECTED = "disconnected"
    FAILED = "failed"


class OpcUaTransportError(RuntimeError):
    """Base transport failure (connection, node, protocol)."""


class OpcUaTimeoutError(OpcUaTransportError):
    """A bounded OPC UA operation exceeded its timeout."""


class OpcUaNodeError(OpcUaTransportError):
    """Node unavailable, bad status, or unexpected datatype."""


ValueCallback = Callable[[PtzNodeDescriptor, object], None]
"""Called with the authoritative server value on subscription updates."""

EventCallback = Callable[[TransportEvent, str], None]
"""Called as ``(event, detail)`` on transport lifecycle changes."""


@dataclass(frozen=True, slots=True)
class SubscriptionHandle:
    """Opaque token for one monitored node; the session restores it."""

    node: PtzNodeDescriptor
    token: str


class OpcUaTransport(Protocol):
    """Generic OPC UA transport. Knows HOW to communicate, never WHAT.

    All potentially blocking operations take an explicit timeout in
    seconds and must raise :class:`OpcUaTimeoutError` when it expires.
    Implementations must be safe to drive from a background thread; the
    session serialises calls.
    """

    def connect(self, endpoint: str, timeout_s: float) -> None:
        """Establish the session. Raises on failure/timeout."""
        ...  # pragma: no cover - interface

    def disconnect(self, timeout_s: float) -> None:
        """Release the session. Must not raise when already disconnected."""
        ...  # pragma: no cover - interface

    @property
    def is_connected(self) -> bool:
        """True while the underlying session is usable."""
        ...  # pragma: no cover - interface

    def read(
        self, node: PtzNodeDescriptor, timeout_s: float
    ) -> object:
        """Read one node. Raises :class:`OpcUaNodeError` for missing nodes,
        bad status, or unexpected datatypes; :class:`OpcUaTimeoutError`
        on timeout."""
        ...  # pragma: no cover - interface

    def write(
        self, node: PtzNodeDescriptor, value: object, timeout_s: float
    ) -> None:
        """Write one node. A success means only that the server accepted
        the value -- never that the PTZ reached a target."""
        ...  # pragma: no cover - interface

    def subscribe(
        self,
        node: PtzNodeDescriptor,
        callback: ValueCallback,
        timeout_s: float,
    ) -> SubscriptionHandle:
        """Monitor one node; server changes invoke ``callback``."""
        ...  # pragma: no cover - interface

    def unsubscribe(self, handle: SubscriptionHandle) -> None:
        """Stop monitoring. Must not raise for unknown handles."""
        ...  # pragma: no cover - interface


__all__ = [
    "EventCallback",
    "OpcUaNodeError",
    "OpcUaTimeoutError",
    "OpcUaTransport",
    "OpcUaTransportError",
    "PtzNodeDescriptor",
    "SubscriptionHandle",
    "TransportEvent",
    "ValueCallback",
]
