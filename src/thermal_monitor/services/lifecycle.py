"""
services.lifecycle -- explicit camera connect/disconnect state machine.

Configuration Mode lifecycle (GUI-observable, no Qt dependency):

    DISCONNECTED
        |
    CONNECTING   (camera process starting, GVCP connect, SHM attach)
        |
    CONNECTED    (control ready, not displaying)
        |
    STARTING     (observer/consumer attaching)
        |
    ACQUIRING    (frames -> processing -> rendering)
        |
    STOPPING     (observer/consumer detaching, display draining)
        |
    CONNECTED
        |
    DISCONNECTING (safe shutdown of everything for this camera)
        |
    DISCONNECTED

Failure from any transitional/acquiring state lands in ERROR/RECOVERING;
ERROR always offers a bounded path back to DISCONNECTED.

Every transition is explicit via :func:`allowed_transition`. Button state
alone is never the lifecycle authority.

Session generations make stale data structurally undisplayable: each new
camera session bumps a monotonic generation, and every frame/result/render
request is accepted only when its ``(camera_id, generation)`` matches the
currently selected session.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from thermal_monitor.core.models import CameraConnectionState


# States that accept a Disconnect request (safe shutdown from anywhere).
_DISCONNECTABLE = frozenset(
    {
        CameraConnectionState.CONNECTED,
        CameraConnectionState.ACQUIRING,
        CameraConnectionState.DEGRADED,
        CameraConnectionState.RECONNECTING,
        CameraConnectionState.ERROR,
        CameraConnectionState.STARTING,
        CameraConnectionState.STOPPING,
        CameraConnectionState.DISCONNECTING,
    }
)

# States in which a new Connect may be issued.
_CONNECTABLE = frozenset({CameraConnectionState.DISCONNECTED, CameraConnectionState.ERROR})

# States in which acquisition Start may be issued.
_STARTABLE = frozenset(
    {
        CameraConnectionState.CONNECTED,
        CameraConnectionState.ERROR,
    }
)

# States in which acquisition Stop may be issued.
_STOPPABLE = frozenset(
    {
        CameraConnectionState.ACQUIRING,
        CameraConnectionState.STARTING,
    }
)

# Explicit transition table: current -> allowed next states.
_TRANSITIONS: dict[CameraConnectionState, frozenset[CameraConnectionState]] = {
    CameraConnectionState.DISCONNECTED: frozenset(
        {CameraConnectionState.CONNECTING, CameraConnectionState.DISCONNECTED}
    ),
    CameraConnectionState.CONNECTING: frozenset(
        {
            CameraConnectionState.CONNECTED,
            CameraConnectionState.DISCONNECTING,
            CameraConnectionState.ERROR,
            CameraConnectionState.DISCONNECTED,
        }
    ),
    CameraConnectionState.CONNECTED: frozenset(
        {
            CameraConnectionState.STARTING,
            CameraConnectionState.DISCONNECTING,
            CameraConnectionState.DISCONNECTED,
            CameraConnectionState.ERROR,
        }
    ),
    CameraConnectionState.STARTING: frozenset(
        {
            CameraConnectionState.ACQUIRING,
            CameraConnectionState.STOPPING,
            CameraConnectionState.DISCONNECTING,
            CameraConnectionState.ERROR,
            CameraConnectionState.CONNECTED,
        }
    ),
    CameraConnectionState.ACQUIRING: frozenset(
        {
            CameraConnectionState.STOPPING,
            CameraConnectionState.DISCONNECTING,
            CameraConnectionState.ERROR,
            CameraConnectionState.DEGRADED,
            CameraConnectionState.RECONNECTING,
        }
    ),
    CameraConnectionState.STOPPING: frozenset(
        {
            CameraConnectionState.CONNECTED,
            CameraConnectionState.DISCONNECTING,
            CameraConnectionState.ERROR,
        }
    ),
    CameraConnectionState.DISCONNECTING: frozenset(
        {CameraConnectionState.DISCONNECTED, CameraConnectionState.ERROR}
    ),
    CameraConnectionState.ERROR: frozenset(
        {
            CameraConnectionState.DISCONNECTING,
            CameraConnectionState.DISCONNECTED,
            CameraConnectionState.CONNECTING,
            CameraConnectionState.CONNECTED,
        }
    ),
    CameraConnectionState.DEGRADED: frozenset(
        {
            CameraConnectionState.ACQUIRING,
            CameraConnectionState.DISCONNECTING,
            CameraConnectionState.ERROR,
            CameraConnectionState.STOPPING,
        }
    ),
    CameraConnectionState.RECONNECTING: frozenset(
        {
            CameraConnectionState.CONNECTED,
            CameraConnectionState.ACQUIRING,
            CameraConnectionState.DISCONNECTING,
            CameraConnectionState.ERROR,
        }
    ),
}


def allowed_transition(
    current: CameraConnectionState, target: CameraConnectionState
) -> bool:
    """True when ``current -> target`` is an explicit lifecycle transition."""
    if current == target:
        return True
    return target in _TRANSITIONS.get(current, frozenset())


def can_connect(state: CameraConnectionState) -> bool:
    """True when a Connect request may be issued in ``state``."""
    return state in _CONNECTABLE


def can_start(state: CameraConnectionState) -> bool:
    """True when acquisition Start may be issued in ``state``."""
    return state in _STARTABLE


def can_stop(state: CameraConnectionState) -> bool:
    """True when acquisition Stop may be issued in ``state``."""
    return state in _STOPPABLE


def can_disconnect(state: CameraConnectionState) -> bool:
    """True when Disconnect (safe shutdown) may be issued in ``state``.

    Disconnect is accepted from every live state, including STARTING and
    STOPPING: the GUI never requires the user to finish a hidden step
    before switching cameras.
    """
    return state in _DISCONNECTABLE


def is_transitional(state: CameraConnectionState) -> bool:
    """True while a bounded background operation owns the camera."""
    return state in (
        CameraConnectionState.CONNECTING,
        CameraConnectionState.STARTING,
        CameraConnectionState.STOPPING,
        CameraConnectionState.DISCONNECTING,
    )


@dataclass(slots=True)
class CameraSession:
    """Identity token for one camera selection epoch.

    ``generation`` is bumped every time the selected camera changes or a
    reconnect cycle begins. Consumers accept a frame/result/render request
    only when both ``camera_id`` and ``generation`` match the current
    session, which makes displaying stale data structurally impossible
    without flushing event queues or sleeping for drains.
    """

    camera_id: str | None = None
    generation: int = 0

    def renew(self, camera_id: str | None) -> "CameraSession":
        """Start a new session epoch, invalidating all older tokens."""
        self.camera_id = camera_id
        self.generation += 1
        return self

    def accepts(self, camera_id: str | None, generation: int | None) -> bool:
        """True when a tagged payload belongs to this session."""
        if self.camera_id is None:
            return False
        if camera_id != self.camera_id:
            return False
        if generation is not None and generation != self.generation:
            return False
        return True


@dataclass(slots=True)
class TeardownTimings:
    """Bounded per-phase durations (ms) for one safe camera shutdown.

    Every phase runs off the GUI thread with its own timeout; a phase that
    exceeds its bound is escalated (never slept through) and recorded here.
    """

    camera_id: str = ""
    generation: int = 0
    pid: int | None = None
    observer_stop_ms: float = 0.0
    processing_stop_ms: float = 0.0
    acquisition_stop_ms: float = 0.0
    shm_detach_ms: float = 0.0
    process_exit_ms: float = 0.0
    total_ms: float = 0.0
    observer_stopped: bool = False
    processing_stopped: bool = False
    shm_detached: bool = False
    process_exited: bool = False
    escalated: bool = False
    started_at: float = field(default_factory=time.perf_counter)

    def finish(self) -> "TeardownTimings":
        self.total_ms = (time.perf_counter() - self.started_at) * 1000.0
        return self

    def summary(self) -> str:
        return (
            f"CAMERA SESSION END camera={self.camera_id} generation={self.generation} "
            f"pid={self.pid} observer_stopped={self.observer_stopped} "
            f"processing_stopped={self.processing_stopped} shm_detached={self.shm_detached} "
            f"process_exit={self.process_exited} escalated={self.escalated} "
            f"observer={self.observer_stop_ms:.0f}ms acquisition={self.acquisition_stop_ms:.0f}ms "
            f"shm={self.shm_detach_ms:.0f}ms childexit={self.process_exit_ms:.0f}ms "
            f"total={self.total_ms:.0f}ms"
        )


__all__ = [
    "CameraSession",
    "TeardownTimings",
    "allowed_transition",
    "can_connect",
    "can_disconnect",
    "can_start",
    "can_stop",
    "is_transitional",
]
