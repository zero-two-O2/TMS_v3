"""Per-camera acquisition processes and their control protocol.

The child owns the camera driver, GVSP receiver, acquisition worker, and SHM
publisher. The parent receives only lifecycle/status messages and attaches to
the producer-owned SHM ring as a consumer.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from thermal_monitor.camera.acquisition import AcquisitionWorker
from thermal_monitor.camera.model import AcquisitionState, AcquisitionStats, CameraConfig
from thermal_monitor.camera.shm import (
    attach_ring_buffer_and_consumer,
    create_frame_publisher_for_camera,
)
from thermal_monitor.camera.tv46_custom import CustomTV46LDriver


logger = logging.getLogger(__name__)


class HandleLifecycleState(Enum):
    """Explicit parent-side lifecycle for one camera child process.

    RUNNING -> STOPPING -> CHILD_STOPPED -> SHM_RELEASED -> PIPES_CLOSED -> STOPPED

    Every public operation checks this state BEFORE touching the process,
    pipes, or SHM objects. Once shutdown begins (STOPPING), readers get the
    last-known terminal snapshot instead of touching closing handles; this
    replaces the old ``_stopped`` flag + object-existence checks that left
    a race between ``poll()/recv()`` (stats timer, GUI thread) and
    ``pipe.close()`` (teardown, background thread).
    """

    RUNNING = "running"
    STOPPING = "stopping"
    CHILD_STOPPED = "child_stopped"
    SHM_RELEASED = "shm_released"
    PIPES_CLOSED = "pipes_closed"
    STOPPED = "stopped"

    @property
    def is_terminal_read(self) -> bool:
        """True when pipes/SHM must no longer be touched (serve snapshot)."""
        return self in (
            HandleLifecycleState.STOPPING,
            HandleLifecycleState.CHILD_STOPPED,
            HandleLifecycleState.SHM_RELEASED,
            HandleLifecycleState.PIPES_CLOSED,
            HandleLifecycleState.STOPPED,
        )


@dataclass(frozen=True, slots=True)
class CameraProcessStatus:
    camera_id: str
    state: AcquisitionState
    error: str | None = None
    stats: AcquisitionStats | None = None


def _camera_process_main(
    camera_config: CameraConfig,
    ring_depth: int,
    status_pipe,
    command_pipe,
) -> None:
    ring = None
    worker = None
    try:
        ring, publisher = create_frame_publisher_for_camera(
            camera_config, ring_depth=ring_depth, dual_feed=True
        )
        source = CustomTV46LDriver(camera_config)
        worker = AcquisitionWorker(
            camera_config.identity.camera_id,
            source,
            publisher,
            camera_config,
        )
        status_pipe.send(("ring_ready", camera_config.identity.camera_id))
        worker.start()
        last_state = None
        last_stats_at = 0.0
        while True:
            now = time.monotonic()
            state = worker.state
            if state != last_state or now - last_stats_at >= 1.0:
                stats = worker.stats()
                status_pipe.send(("status", CameraProcessStatus(
                    camera_id=camera_config.identity.camera_id,
                    state=state,
                    error=stats.last_error,
                    stats=stats,
                )))
                last_state = state
                last_stats_at = now
            if command_pipe.poll(0.05):
                command = command_pipe.recv()
                if command == "stop":
                    break
                if command == "reconnect":
                    worker.stop(timeout=2.0)
                    worker.start()
                elif isinstance(command, tuple) and command[0] == "nuc":
                    source.perform_nuc()
                elif isinstance(command, tuple) and command[0] == "focus":
                    source.set_focus_mm(int(command[1]))
                elif command == "shutdown":
                    break
                elif isinstance(command, tuple) and command[0] == "request":
                    _, method_name, args, kwargs = command
                    try:
                        value = getattr(source, method_name)(*args, **kwargs)
                        status_pipe.send(("response", True, value))
                    except Exception as exc:
                        status_pipe.send(("response", False, str(exc)))
    except Exception as exc:
        try:
            status_pipe.send(("status", CameraProcessStatus(
                camera_id=camera_config.identity.camera_id,
                state=AcquisitionState.FAILED,
                error=str(exc),
            )))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if worker is not None:
            try:
                worker.stop(timeout=3.0)
            except Exception:
                pass
        if ring is not None:
            try:
                ring.close()
            except Exception:
                pass
        try:
            status_pipe.send(("stopped", camera_config.identity.camera_id))
        except (BrokenPipeError, EOFError, OSError):
            pass
        status_pipe.close()
        command_pipe.close()


class CameraProcessHandle:
    """Parent-side handle for one camera process and its SHM consumer."""

    def __init__(self, camera_config: CameraConfig, ring_depth: int = 32) -> None:
        self.camera_config = camera_config
        self.camera_id = camera_config.identity.camera_id
        self._ring_depth = ring_depth
        self._status_parent, status_child = mp.Pipe(False)
        self._command_parent, command_child = mp.Pipe()
        self._process = mp.Process(
            target=_camera_process_main,
            args=(camera_config, ring_depth, status_child, command_child),
            name=f"ThermalCamera-{self.camera_id}",
            daemon=True,
        )
        self._status: CameraProcessStatus | None = None
        # Lifecycle/concurrency: ONE threading.RLock serializes every
        # pipe/SHM/process touch (poll, request, stop, terminate, close).
        # NOTE: this must be a threading lock, not multiprocessing.RLock:
        # it guards parent-side *threads* (GUI stats timer vs. background
        # teardown), never cross-process state. An mp.RLock is semaphore
        # backed and misbehaves when the child dies mid-wait.
        self._lifecycle_lock = threading.RLock()
        self._lifecycle_state = HandleLifecycleState.RUNNING
        self._terminal_state = AcquisitionState.CONNECTING
        self._terminal_error: str | None = None
        self.ring = None
        self.consumer = None

    @property
    def process(self) -> mp.Process:
        return self._process

    @property
    def pid(self) -> int | None:
        """OS PID of the camera child process (None before start)."""
        return self._process.pid

    @property
    def state(self) -> AcquisitionState:
        self.poll()
        if self._status is None:
            return AcquisitionState.CONNECTING
        return self._status.state

    def start(self, timeout: float = 10.0) -> None:
        with self._lifecycle_lock:
            if self._lifecycle_state is not HandleLifecycleState.RUNNING:
                raise RuntimeError(
                    f"camera process handle {self.camera_id} is not startable "
                    f"(state={self._lifecycle_state.value})"
                )
            if self._process.is_alive():
                raise RuntimeError(
                    f"camera process {self.camera_id} already running"
                )
        self._process.start()
        logger.info(
            "Camera %s: child process starting pid=%s py_thread=%s",
            self.camera_id,
            self._process.pid,
            threading.get_ident(),
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.poll()
            with self._lifecycle_lock:
                if self.ring is None:
                    self._drain_until_ring_ready()
                ring_ready = self.ring is not None
            if ring_ready:
                break
            if not self._process.is_alive():
                raise RuntimeError(f"camera process {self.camera_id} exited during startup")
            time.sleep(0.01)
        with self._lifecycle_lock:
            ring_ready = self.ring is not None
        if not ring_ready:
            self.stop(timeout=1.0)
            raise TimeoutError(f"camera process {self.camera_id} did not publish SHM")

    @property
    def lifecycle_state(self) -> HandleLifecycleState:
        """Current parent-side lifecycle state (never touches pipes)."""
        with self._lifecycle_lock:
            return self._lifecycle_state

    def _terminal_snapshot(self) -> CameraProcessStatus:
        """Last-known status served after shutdown begins (no pipe touch)."""
        return CameraProcessStatus(
            camera_id=self.camera_id,
            state=self._terminal_state,
            error=self._terminal_error,
        )

    def _remember_status(self, status: CameraProcessStatus | None) -> None:
        if status is not None:
            self._terminal_state = status.state
            self._terminal_error = status.error

    def _drain_until_ring_ready(self) -> None:
        # Callers must hold _lifecycle_lock and have verified RUNNING state:
        # pipe reads and pipe close are mutually exclusive by construction.
        try:
            while self._status_parent.poll():
                kind, payload = self._status_parent.recv()
                if kind == "ring_ready":
                    self.ring, self.consumer = attach_ring_buffer_and_consumer(
                        self.camera_id,
                        f"process_observer_{self.camera_id}_{self._process.pid}",
                        depth=self._ring_depth,
                        dual_feed=True,
                    )
                elif kind == "status":
                    self._status = payload
                    self._remember_status(payload)
        except (EOFError, OSError):
            # Peer went away mid-drain (teardown won the race or child
            # died): keep the last-known snapshot; never propagate a
            # pipe error out of a non-blocking drain.
            logger.debug(
                "Camera %s: status pipe drained during peer exit; "
                "keeping last-known state=%s",
                self.camera_id,
                self._terminal_state.value,
            )

    def poll(self) -> CameraProcessStatus | None:
        """Drain pending status messages; safe to call from any thread.

        Serialized against stop()/close via _lifecycle_lock. After shutdown
        begins, returns the last-known terminal snapshot WITHOUT touching
        the (possibly closed) pipes.
        """
        with self._lifecycle_lock:
            if self._lifecycle_state.is_terminal_read:
                return self._terminal_snapshot()
            try:
                self._drain_until_ring_ready()
                while self._status_parent.poll():
                    kind, payload = self._status_parent.recv()
                    if kind == "status":
                        self._status = payload
                        self._remember_status(payload)
                    elif kind == "ring_ready":
                        self.ring, self.consumer = attach_ring_buffer_and_consumer(
                            self.camera_id,
                            f"process_observer_{self.camera_id}_{self._process.pid}",
                            depth=self._ring_depth,
                            dual_feed=True,
                        )
            except (EOFError, OSError):
                logger.debug(
                    "Camera %s: status pipe closed during poll; "
                    "serving terminal state=%s",
                    self.camera_id,
                    self._terminal_state.value,
                )
                return self._terminal_snapshot()
            except Exception:
                logger.debug(
                    "Camera %s: poll failed", self.camera_id, exc_info=True
                )
                return self._status
            return self._status

    def stats(self) -> AcquisitionStats | None:
        status = self.poll()
        return status.stats if status is not None else None

    def is_alive(self) -> bool:
        return self._process.is_alive() and self.state not in (
            AcquisitionState.FAILED,
            AcquisitionState.STOPPED,
        )

    def integrity_stats(self):
        return None

    def wait_for_state(self, expected: AcquisitionState, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state is expected:
                return True
            if not self._process.is_alive() and self.state is not expected:
                return False
            time.sleep(0.02)
        return self.state is expected

    def send(self, command: Any) -> None:
        """Best-effort command send; no-op once shutdown has begun."""
        with self._lifecycle_lock:
            if self._lifecycle_state.is_terminal_read:
                return
            try:
                if self._process.is_alive():
                    self._command_parent.send(command)
            except (EOFError, OSError):
                logger.debug(
                    "Camera %s: command pipe closed during send",
                    self.camera_id,
                )
            except Exception:
                logger.debug(
                    "Camera %s: send failed", self.camera_id, exc_info=True
                )

    def request(self, method_name: str, *args, **kwargs):
        # State check + send are atomic under the lifecycle lock; the
        # bounded response wait below re-acquires the lock per poll so a
        # concurrent stop() can never close the pipe mid-recv(). If
        # shutdown begins mid-wait, abort cleanly instead of blocking.
        with self._lifecycle_lock:
            if self._lifecycle_state.is_terminal_read:
                raise RuntimeError(
                    f"Camera {self.camera_id} process is shutting down; "
                    f"request {method_name!r} aborted"
                )
            try:
                if self._process.is_alive():
                    self._command_parent.send(("request", method_name, args, kwargs))
                else:
                    raise RuntimeError(
                        f"Camera {self.camera_id} process is not alive"
                    )
            except (EOFError, OSError) as exc:
                raise RuntimeError(
                    f"Camera {self.camera_id} command pipe closed"
                ) from exc
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            with self._lifecycle_lock:
                if self._lifecycle_state.is_terminal_read:
                    raise RuntimeError(
                        f"Camera {self.camera_id} shut down during request "
                        f"{method_name!r}"
                    )
                try:
                    if self._status_parent.poll():
                        kind, payload, *rest = self._status_parent.recv()
                        if kind == "response":
                            ok = payload
                            value = rest[0] if rest else None
                            if ok:
                                return value
                            raise RuntimeError(value)
                        if kind == "status":
                            self._status = payload
                            self._remember_status(payload)
                        elif kind == "ring_ready" and self.ring is None:
                            self.ring, self.consumer = attach_ring_buffer_and_consumer(
                                self.camera_id,
                                f"process_observer_{self.camera_id}_{self._process.pid}",
                                depth=self._ring_depth,
                                dual_feed=True,
                            )
                    else:
                        # No message ready: release the lock while waiting
                        # so stop()/close can proceed; re-check state next
                        # iteration.
                        pass
                except (EOFError, OSError) as exc:
                    raise RuntimeError(
                        f"Camera {self.camera_id} status pipe closed during "
                        f"request {method_name!r}"
                    ) from exc
            time.sleep(0.02)
        raise TimeoutError(f"camera process request timed out: {method_name}")

    def stop(self, timeout: float = 5.0) -> None:
        """Bounded shutdown: polite stop, then escalate to terminate.

        Genuinely idempotent AND serialized: concurrent callers (background
        teardown, Phase-3 verify, app shutdown) funnel through
        _lifecycle_lock and the second one returns immediately. The blocking
        child join runs WITHOUT the lock so stats polling keeps serving the
        terminal snapshot instead of freezing; pipe/SHM close runs WITH the
        lock so no poll()/recv() can interleave with close().
        Must be called off the Qt GUI thread (see CameraRuntimeService).
        """
        with self._lifecycle_lock:
            if self._lifecycle_state is not HandleLifecycleState.RUNNING:
                return
            self._lifecycle_state = HandleLifecycleState.STOPPING
        caller_thread = threading.get_ident()
        logger.info(
            "Camera %s: handle stop begin state=stopping pid=%s py_thread=%s",
            self.camera_id,
            self._process.pid,
            caller_thread,
        )
        try:
            self.send("stop")
        except Exception:
            pass
        try:
            self._process.join(timeout)
        except Exception:
            pass
        escalated = False
        try:
            if self._process.is_alive():
                escalated = True
                logger.warning(
                    "Camera %s: child ignored stop; terminating pid=%s "
                    "py_thread=%s",
                    self.camera_id,
                    self._process.pid,
                    caller_thread,
                )
                self._process.terminate()
                self._process.join(1.0)
        except Exception:
            pass
        with self._lifecycle_lock:
            self._lifecycle_state = HandleLifecycleState.CHILD_STOPPED
            self._remember_status(self._status)
            if self._terminal_state not in (
                AcquisitionState.STOPPED,
                AcquisitionState.FAILED,
            ):
                self._terminal_state = AcquisitionState.STOPPED
            if self.ring is not None:
                try:
                    self.ring.close()
                except Exception:
                    pass
                self.ring = None
                self.consumer = None
            self._lifecycle_state = HandleLifecycleState.SHM_RELEASED
            for pipe in (self._status_parent, self._command_parent):
                try:
                    pipe.close()
                except Exception:
                    pass
            self._lifecycle_state = HandleLifecycleState.PIPES_CLOSED
            self._lifecycle_state = HandleLifecycleState.STOPPED
        logger.info(
            "Camera %s: handle stop complete state=stopped escalated=%s "
            "py_thread=%s",
            self.camera_id,
            escalated,
            caller_thread,
        )

    def wait_for_exit(self, timeout: float) -> bool:
        """Bounded poll for child exit WITHOUT touching pipes or SHM.

        Safe to call after stop()/close from any thread (used by Phase-3
        verification instead of probing the closed handle). Uses only the
        OS process object; never blocks longer than ``timeout``.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if not self._process.is_alive():
                    return True
            except Exception:
                return True
            time.sleep(0.02)
        try:
            return not self._process.is_alive()
        except Exception:
            return True

    def terminate(self) -> None:
        """Escalation-only terminate, serialized via the lifecycle lock.

        No-op unless the handle is still RUNNING/STOPPING with a live
        child. Teardown paths should prefer stop(); this exists so Phase-3
        verification never calls ``process.terminate()`` directly on a
        handle another thread may be closing.
        """
        with self._lifecycle_lock:
            if self._lifecycle_state not in (
                HandleLifecycleState.RUNNING,
                HandleLifecycleState.STOPPING,
            ):
                return
            try:
                alive = self._process.is_alive()
            except Exception:
                return
            if alive:
                try:
                    logger.warning(
                        "Camera %s: escalation terminate pid=%s py_thread=%s",
                        self.camera_id,
                        self._process.pid,
                        threading.get_ident(),
                    )
                    self._process.terminate()
                except Exception:
                    pass


class CameraProcessManager:
    """Owns independent camera processes without image IPC."""

    def __init__(self, ring_depth: int = 32) -> None:
        self._ring_depth = ring_depth
        self._handles: dict[str, CameraProcessHandle] = {}

    def start_camera(self, camera_config: CameraConfig, timeout: float = 10.0) -> CameraProcessHandle:
        camera_id = camera_config.identity.camera_id
        existing = self._handles.get(camera_id)
        if existing is not None and existing.process.is_alive():
            return existing
        handle = CameraProcessHandle(camera_config, self._ring_depth)
        handle.start(timeout=timeout)
        self._handles[camera_id] = handle
        return handle

    def start_all(
        self, camera_configs: list[CameraConfig], timeout: float = 10.0, stagger_s: float = 0.15
    ) -> dict[str, CameraProcessHandle]:
        started: dict[str, CameraProcessHandle] = {}
        for index, camera_config in enumerate(camera_configs):
            started[camera_config.identity.camera_id] = self.start_camera(
                camera_config, timeout=timeout
            )
            if index + 1 < len(camera_configs) and stagger_s > 0:
                time.sleep(stagger_s)
        return started

    def stop_camera(self, camera_id: str, timeout: float = 5.0) -> None:
        handle = self._handles.pop(camera_id, None)
        if handle is not None:
            handle.stop(timeout)

    def restart_camera(self, camera_config: CameraConfig, timeout: float = 10.0) -> CameraProcessHandle:
        self.stop_camera(camera_config.identity.camera_id)
        return self.start_camera(camera_config, timeout)

    def restart_failed(
        self, camera_configs: dict[str, CameraConfig], timeout: float = 10.0
    ) -> dict[str, CameraProcessHandle]:
        restarted: dict[str, CameraProcessHandle] = {}
        for camera_id, handle in list(self._handles.items()):
            if not handle.is_alive():
                config = camera_configs.get(camera_id)
                if config is not None:
                    restarted[camera_id] = self.restart_camera(config, timeout)
        return restarted

    def stop_all(self, timeout: float = 5.0) -> None:
        for camera_id in list(self._handles):
            self.stop_camera(camera_id, timeout)

    def handle(self, camera_id: str) -> CameraProcessHandle | None:
        return self._handles.get(camera_id)


__all__ = ["CameraProcessHandle", "CameraProcessManager", "CameraProcessStatus"]
