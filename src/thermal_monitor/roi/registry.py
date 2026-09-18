"""roi.registry -- authoritative active-context store.

Single registry per application (do not create a second competing
active-position store). Writes are atomic snapshots guarded by
(session_generation, operation_id): an older workflow can never publish
after a newer one, and failures leave the previous context untouched.
"""

from __future__ import annotations

import threading
import time

from thermal_monitor.ptz.errors import PtzStateError
from thermal_monitor.roi.context import RoiActiveContext, inactive_context
from thermal_monitor.roi.errors import RoiStaleContextError


class RoiContextRegistry:
    """Thread-safe camera_id -> RoiActiveContext store."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._contexts: dict[str, RoiActiveContext] = {}
        self._operations: dict[str, str] = {}  # camera_id -> latest operation_id
        self._shutdown = False

    def publish(self, context: RoiActiveContext, *,
                current_session_generation: int | None = None) -> None:
        with self._lock:
            if self._shutdown:
                raise PtzStateError("RoiContextRegistry is shut down")
            if (current_session_generation is not None
                    and context.session_generation != current_session_generation):
                raise RoiStaleContextError(
                    f"stale session {context.session_generation} != "
                    f"{current_session_generation}; context rejected")
            latest_op = self._operations.get(context.camera_id)
            if latest_op is not None and context.operation_id:
                if context.operation_id < latest_op:
                    raise RoiStaleContextError(
                        f"stale operation {context.operation_id} < {latest_op}")
            if context.operation_id:
                self._operations[context.camera_id] = context.operation_id
            self._contexts[context.camera_id] = context

    def claim_operation(self, camera_id: str, operation_id: str) -> None:
        """Record a newly started operation so older completions are stale."""
        with self._lock:
            self._operations[camera_id] = operation_id

    def get(self, camera_id: str) -> RoiActiveContext | None:
        with self._lock:
            return self._contexts.get(camera_id)

    def invalidate(self, camera_id: str, *, session_generation: int = 0) -> None:
        with self._lock:
            self._contexts[camera_id] = inactive_context(camera_id, session_generation)

    def clear(self, camera_id: str) -> None:
        with self._lock:
            self._contexts.pop(camera_id, None)
            self._operations.pop(camera_id, None)

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            self._contexts.clear()
            self._operations.clear()


__all__ = ["RoiContextRegistry"]
