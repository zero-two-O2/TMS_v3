"""Per-camera acquisition processes and their control protocol.

The child owns the camera driver, GVSP receiver, acquisition worker, and SHM
publisher. The parent receives only lifecycle/status messages and attaches to
the producer-owned SHM ring as a consumer.
"""

from __future__ import annotations

import multiprocessing as mp
import time
from dataclasses import dataclass
from typing import Any

from thermal_monitor.camera.acquisition import AcquisitionWorker
from thermal_monitor.camera.model import AcquisitionState, AcquisitionStats, CameraConfig
from thermal_monitor.camera.shm import (
    attach_ring_buffer_and_consumer,
    create_frame_publisher_for_camera,
)
from thermal_monitor.camera.tv46_custom import CustomTV46LDriver


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
        self._request_lock = mp.RLock()
        self.ring = None
        self.consumer = None

    @property
    def process(self) -> mp.Process:
        return self._process

    @property
    def state(self) -> AcquisitionState:
        self.poll()
        if self._status is None:
            return AcquisitionState.CONNECTING
        return self._status.state

    def start(self, timeout: float = 10.0) -> None:
        self._process.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.poll()
            if self.ring is None:
                self._drain_until_ring_ready()
            if self.ring is not None:
                break
            if not self._process.is_alive():
                raise RuntimeError(f"camera process {self.camera_id} exited during startup")
            time.sleep(0.01)
        if self.ring is None:
            self.stop(timeout=1.0)
            raise TimeoutError(f"camera process {self.camera_id} did not publish SHM")

    def _drain_until_ring_ready(self) -> None:
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

    def poll(self) -> CameraProcessStatus | None:
        self._drain_until_ring_ready()
        while self._status_parent.poll():
            kind, payload = self._status_parent.recv()
            if kind == "status":
                self._status = payload
            elif kind == "ring_ready":
                self.ring, self.consumer = attach_ring_buffer_and_consumer(
                    self.camera_id,
                    f"process_observer_{self.camera_id}_{self._process.pid}",
                    depth=self._ring_depth,
                    dual_feed=True,
                )
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
        if self._process.is_alive():
            self._command_parent.send(command)

    def request(self, method_name: str, *args, **kwargs):
        with self._request_lock:
            self.send(("request", method_name, args, kwargs))
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if self._status_parent.poll(0.05):
                    kind, payload, *rest = self._status_parent.recv()
                    if kind == "response":
                        ok = payload
                        value = rest[0] if rest else None
                        if ok:
                            return value
                        raise RuntimeError(value)
                    if kind == "status":
                        self._status = payload
                    elif kind == "ring_ready" and self.ring is None:
                        self.ring, self.consumer = attach_ring_buffer_and_consumer(
                            self.camera_id,
                            f"process_observer_{self.camera_id}_{self._process.pid}",
                            depth=self._ring_depth,
                            dual_feed=True,
                        )
            raise TimeoutError(f"camera process request timed out: {method_name}")

    def stop(self, timeout: float = 5.0) -> None:
        self.send("stop")
        self._process.join(timeout)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(1.0)
        if self.ring is not None:
            self.ring.close()
            self.ring = None
            self.consumer = None
        self._status_parent.close()
        self._command_parent.close()


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
