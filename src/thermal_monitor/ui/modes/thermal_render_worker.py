"""Bounded, off-GUI-thread thermal image renderer."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import numpy as np
from PyQt6.QtCore import QThread, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QImage

from thermal_monitor.core.frame_latency import (
    get_default_tracker as _latency_tracker,
    latency_enabled as _latency_enabled,
)

logger = logging.getLogger(__name__)


# Authoritative palette LUTs live in thermal_monitor.ui.palettes (single
# source of truth for rendering + previews). Re-exported here so existing
# imports (observer_image, tests) keep working unchanged.
from thermal_monitor.ui.palettes import PALETTE_LUTS


@dataclass(frozen=True)
class RenderRequest:
    temperature: np.ndarray
    sequence: int  # acquisition worker sequence (latest-wins gating)
    minimum: float | None = None
    maximum: float | None = None
    hw_sequence: int | None = None  # hardware/GVSP frame id (trace only)
    camera_id: str | None = None  # latency/display-age correlation (trace only)
    acq_mono_ns: int | None = None  # acquisition monotonic timestamp, ns (trace only)


class ThermalRenderWorker(QThread):
    """Persistent renderer with at most one in-flight and one pending frame."""

    rendered = pyqtSignal(object, object, float, float, int, object, object)
    latest_ready = pyqtSignal()
    render_error = pyqtSignal(str)

    def __init__(self, palette: str = "temperature", max_fps: float = 20.0, parent=None) -> None:
        # Display throttle only: pending depth 1 already gives latest-wins.
        # 20 fps keeps the throttle floor (~50 ms) inside the <100 ms
        # acquisition-to-display budget at 9 fps input; input rate, not this
        # cap, sets the actual render rate.
        super().__init__()
        if parent is not None:
            self.setParent(parent)
        self._palette = palette
        self._interval = 1.0 / max_fps
        self._condition = threading.Condition()
        self._pending: RenderRequest | None = None
        self._pending_epoch: int = 0
        self._latest_output = None
        self._latest_notification_pending = False
        self._stopping = False
        self._last_sequence = -1
        self._last_hw_sequence: int | None = None
        # Render epoch: bumped by set_session(). An in-flight render that
        # started before the session change is discarded instead of
        # emitted, so the old camera's last frame can never paint over
        # the new camera.
        self._epoch: int = 0
        # Camera-session gate: only requests carrying the current session's
        # camera_id are rendered. Reset on every camera switch so the new
        # camera's sequence numbering (which restarts at 0) is accepted
        # while any late request from the old camera is dropped.
        self._session_camera_id: str | None = None
        self.dropped_frames = 0
        self.submitted_frames = 0
        self.rendered_frames = 0
        self.last_render_ms = 0.0
        self.last_palette_ms = 0.0

    def submit(self, request: RenderRequest) -> None:
        """Replace obsolete pending work; producer owns immutable input arrays.

        Requests from a previous camera session are dropped: a session
        change resets the accepted sequence baseline, and any request that
        still carries the old camera_id can never match the new session.
        """
        with self._condition:
            if self._stopping or request.sequence <= self._last_sequence:
                return
            if (
                self._session_camera_id is not None
                and request.camera_id is not None
                and request.camera_id != self._session_camera_id
            ):
                return
            if self._pending is not None:
                self.dropped_frames += 1
            self._pending = request
            self._pending_epoch = self._epoch
            self.submitted_frames += 1
            self._condition.notify()

    def set_session(self, camera_id: str | None) -> None:
        """Begin a new camera session: accept that camera's frames from sequence 0.

        Pending work from the old session is discarded; in-flight rendering
        finishes harmlessly because the widget applies outputs only for the
        current session.
        """
        with self._condition:
            self._session_camera_id = camera_id
            self._epoch += 1
            self._last_sequence = -1
            self._last_hw_sequence = None
            self._pending = None
            self._latest_output = None
            self._latest_notification_pending = False

    def set_palette(self, palette: str) -> None:
        with self._condition:
            self._palette = palette
            self._condition.notify()

    def request_stop(self) -> None:
        """Request renderer shutdown WITHOUT waiting (transition fast path).

        Sets the stop flag and wakes the thread, then returns immediately
        so the GUI thread never blocks behind a mode transition. The
        thread quits itself on render completion; the widget's
        ``destroyed`` handler reaps it with :meth:`stop_bounded`.
        Safe to call from any thread, including ``destroyed`` handlers
        (the extra ``QObject*`` argument is ignored).
        """
        try:
            with self._condition:
                self._stopping = True
                self._pending = None
                self._latest_output = None
                self._latest_notification_pending = False
                self._condition.notify()
        except RuntimeError:
            pass  # C++ object already gone; nothing to stop

    def _on_destroyed(self, _obj: object = None) -> None:
        """Safe ``destroyed``-signal target: bounded reap, never a hang.

        ``destroyed(QObject*)`` must never deliver its argument into a
        wait call (that raises TypeError inside teardown). This slot
        accepts and ignores the argument and reaps with a bounded wait;
        on timeout the thread is left to quit itself on render
        completion. Never blocks the event loop beyond ``timeout_ms``.
        """
        try:
            self.stop_bounded(500)
        except RuntimeError:
            pass  # C++ object already gone; nothing to reap

    def stop(self, *args) -> None:
        """Request renderer shutdown and wait for the thread to finish.

        Waits indefinitely: prefer :meth:`request_stop` on the visible
        transition path and :meth:`stop_bounded` from ``destroyed``
        handlers (see :meth:`_on_destroyed`). Extra positional arguments
        (e.g. the ``QObject*`` from ``destroyed``) are accepted and
        ignored so a direct connection can never raise TypeError inside
        teardown and cascade into a native crash.
        """
        with self._condition:
            self._stopping = True
            self._pending = None
            self._latest_output = None
            self._latest_notification_pending = False
            self._condition.notify()
        self.wait()

    def stop_bounded(self, timeout_ms: int) -> bool:
        """Request shutdown and wait at most ``timeout_ms``.

        Returns True when the thread finished. A timeout expiry is logged
        loudly and never silent; the thread is left to quit itself on
        render completion. Never connect this to ``destroyed`` directly.
        """
        with self._condition:
            self._stopping = True
            self._pending = None
            self._latest_output = None
            self._latest_notification_pending = False
            self._condition.notify()
        if self.wait(timeout_ms):
            return True
        logger.warning(
            "Thermal renderer thread still running after %d ms; "
            "leaving it to quit itself on render completion",
            timeout_ms,
        )
        return False

    def take_latest_output(self):
        """Take the newest completed render without queuing old images."""
        with self._condition:
            output = self._latest_output
            self._latest_output = None
            self._latest_notification_pending = False
            return output

    def run(self) -> None:
        logger.info("Thermal renderer started")
        while True:
            with self._condition:
                while self._pending is None and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    break
                request = self._pending
                request_epoch = self._pending_epoch
                self._pending = None
                palette = self._palette
            started = time.perf_counter()
            if request.camera_id is not None and _latency_enabled():
                _latency_tracker().note_stage(
                    request.camera_id, request.sequence, "render_start", time.perf_counter_ns()
                )
            try:
                image, temperature, minimum, maximum, thumbnail, rgb, palette_ms = self._render(request, palette)
                # Single locked commit: re-check the epoch together with the
                # output store so a session change racing the render tail
                # cannot leave obsolete output behind.
                with self._condition:
                    if request_epoch != self._epoch or self._stopping:
                        # Session changed (or shutdown) while rendering:
                        # drop the obsolete output instead of emitting it.
                        self.dropped_frames += 1
                        continue
                    self._last_sequence = request.sequence
                    self._last_hw_sequence = request.hw_sequence
                    self._latest_output = (
                        image,
                        temperature,
                        minimum,
                        maximum,
                        request.sequence,
                        thumbnail,
                        rgb,
                    )
                    notify = not self._latest_notification_pending
                    self._latest_notification_pending = True
                self.rendered_frames += 1
                if request.camera_id is not None and _latency_enabled():
                    _latency_tracker().note_stage(
                        request.camera_id, request.sequence, "render_done", time.perf_counter_ns()
                    )
                self.last_render_ms = (time.perf_counter() - started) * 1000.0
                self.last_palette_ms = palette_ms
                if logger.isEnabledFor(logging.DEBUG) and (
                    self.rendered_frames == 1 or self.rendered_frames % 90 == 0
                ):
                    logger.debug(
                        "thermal render seq=%d hw=%s render_ms=%.1f submitted=%d rendered=%d dropped=%d",
                        request.sequence,
                        request.hw_sequence,
                        self.last_render_ms,
                        self.submitted_frames,
                        self.rendered_frames,
                        self.dropped_frames,
                    )
                if notify:
                    self.latest_ready.emit()
                self.rendered.emit(
                    image,
                    temperature,
                    minimum,
                    maximum,
                    request.sequence,
                    thumbnail,
                    rgb,
                )
            except Exception as exc:  # Rendering must not affect acquisition.
                logger.exception("Thermal renderer failed")
                self.render_error.emit(str(exc))
            elapsed = time.perf_counter() - started
            if elapsed < self._interval:
                time.sleep(self._interval - elapsed)
        logger.info("Thermal renderer stopped")

    def _render(self, request: RenderRequest, palette: str):
        source = np.asarray(request.temperature)
        if source.ndim != 2:
            raise ValueError("thermal temperature image must be two-dimensional")
        finite = np.isfinite(source)
        minimum = request.minimum if request.minimum is not None else (float(np.min(source[finite])) if np.any(finite) else 0.0)
        maximum = request.maximum if request.maximum is not None else (float(np.max(source[finite])) if np.any(finite) else 1.0)
        if maximum <= minimum:
            maximum = minimum + 1.0
        normalized = np.clip((source - minimum) / (maximum - minimum), 0.0, 1.0)
        normalized[~finite] = 0.0
        indices = np.rint(normalized * 255.0).astype(np.uint8)
        palette_started = time.perf_counter()
        rgb = PALETTE_LUTS.get(palette, PALETTE_LUTS["gray"])[indices]
        palette_ms = (time.perf_counter() - palette_started) * 1000.0
        rgb = np.ascontiguousarray(rgb)
        image = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format.Format_RGB888).copy()
        thumbnail = image.scaled(240, 160, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
        return image, source, minimum, maximum, thumbnail, rgb, palette_ms


__all__ = ["RenderRequest", "ThermalRenderWorker", "PALETTE_LUTS"]
