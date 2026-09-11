"""Bounded, off-GUI-thread YUYV visible-light renderer (Stage 8E).

Mirrors :mod:`thermal_monitor.ui.modes.thermal_render_worker`: at most one
in-flight and one pending frame, newer sequences replace stale pending work
(latest-frame-wins, stale frames dropped, no unbounded queue). Conversion
(YUYV -> RGB) runs here, never in acquisition and never on the GUI thread.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import numpy as np
from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtGui import QImage

from thermal_monitor.core.frame_latency import (
    get_default_tracker as _latency_tracker,
    latency_enabled as _latency_enabled,
)

from thermal_monitor.ui.modes.vl_convert import yuyv_to_rgb

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VlRenderRequest:
    yuyv: np.ndarray  # owned copy, (H, W*2) uint8
    sequence: int  # worker (ring) sequence for latest-wins gating
    hw_sequence: int | None = None  # hardware frame ID (correlation display)
    camera_id: str | None = None  # latency/display-age correlation (trace only)
    acq_mono_ns: int | None = None  # acquisition monotonic timestamp, ns (trace only)


class VlRenderWorker(QThread):
    """Persistent VL renderer with latest-wins semantics."""

    rendered = pyqtSignal(object, int, object)  # QImage, sequence, hw_sequence
    render_error = pyqtSignal(str)

    def __init__(self, max_fps: float = 20.0, parent=None) -> None:
        # Display throttle only (see ThermalRenderWorker): pending depth 1
        # already gives latest-wins; 20 fps keeps the throttle floor inside
        # the acquisition-to-display budget at 9 fps input.
        super().__init__()
        if parent is not None:
            self.setParent(parent)
        self._interval = 1.0 / max_fps
        self._condition = threading.Condition()
        self._pending: VlRenderRequest | None = None
        self._stopping = False
        self._last_sequence = -1
        self.dropped_frames = 0
        self.last_render_ms = 0.0

    def submit(self, request: VlRenderRequest) -> None:
        """Replace obsolete pending work; caller must pass owned arrays."""
        with self._condition:
            if self._stopping or request.sequence <= self._last_sequence:
                return
            if self._pending is not None:
                self.dropped_frames += 1
            self._pending = request
            self._condition.notify()

    def stop(self) -> None:
        with self._condition:
            self._stopping = True
            self._pending = None
            self._condition.notify()
        self.wait()

    def run(self) -> None:
        logger.info("VL renderer started")
        while True:
            with self._condition:
                while self._pending is None and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    break
                request = self._pending
                self._pending = None
            started = time.perf_counter()
            if request.camera_id is not None and _latency_enabled():
                _latency_tracker().note_stage(
                    request.camera_id + "#vl", request.sequence, "render_start", time.perf_counter_ns()
                )
            try:
                rgb = yuyv_to_rgb(request.yuyv)
                image = QImage(
                    rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0],
                    QImage.Format.Format_RGB888,
                ).copy()
                self._last_sequence = request.sequence
                self.last_render_ms = (time.perf_counter() - started) * 1000.0
                if request.camera_id is not None and _latency_enabled():
                    _latency_tracker().note_stage(
                        request.camera_id + "#vl", request.sequence, "render_done", time.perf_counter_ns()
                    )
                self.rendered.emit(image, request.sequence, request.hw_sequence)
            except Exception as exc:  # Rendering must not affect acquisition.
                logger.exception("VL renderer failed")
                self.render_error.emit(str(exc))
            elapsed = time.perf_counter() - started
            if elapsed < self._interval:
                time.sleep(self._interval - elapsed)
        logger.info("VL renderer stopped")


__all__ = ["VlRenderRequest", "VlRenderWorker"]
