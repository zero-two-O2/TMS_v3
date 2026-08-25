"""Bounded, off-GUI-thread thermal image renderer."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import numpy as np
from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtGui import QImage

logger = logging.getLogger(__name__)


def _lut(points: list[tuple[int, int, int]]) -> np.ndarray:
    values = np.asarray(points, dtype=np.float32)
    x = np.linspace(0.0, 1.0, len(values), dtype=np.float32)
    indices = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    return np.rint(np.column_stack([np.interp(indices, x, values[:, channel]) for channel in range(3)])).astype(np.uint8)


PALETTE_LUTS = {
    "temperature": _lut([(0, 0, 128), (0, 255, 255), (0, 255, 0), (255, 255, 0), (255, 128, 0), (255, 0, 0), (128, 0, 0)]),
    "iron": _lut([(0, 0, 0), (255, 0, 0), (255, 128, 0), (255, 255, 0), (255, 255, 128)]),
    "rainbow": _lut([(128, 0, 128), (0, 0, 255), (0, 255, 255), (0, 255, 0), (255, 255, 0), (255, 0, 0)]),
    "gray": np.repeat(np.arange(256, dtype=np.uint8)[:, None], 3, axis=1),
    "hot": _lut([(0, 0, 0), (255, 0, 0), (255, 255, 0), (255, 255, 255)]),
}


@dataclass(frozen=True)
class RenderRequest:
    temperature: np.ndarray
    sequence: int
    minimum: float | None = None
    maximum: float | None = None


class ThermalRenderWorker(QThread):
    """Persistent renderer with at most one in-flight and one pending frame."""

    rendered = pyqtSignal(object, object, float, float, int, object, object)
    render_error = pyqtSignal(str)

    def __init__(self, palette: str = "temperature", max_fps: float = 10.0, parent=None) -> None:
        super().__init__()
        if parent is not None:
            self.setParent(parent)
        self._palette = palette
        self._interval = 1.0 / max_fps
        self._condition = threading.Condition()
        self._pending: RenderRequest | None = None
        self._stopping = False
        self._last_sequence = -1
        self.dropped_frames = 0
        self.last_render_ms = 0.0
        self.last_palette_ms = 0.0

    def submit(self, request: RenderRequest) -> None:
        """Replace obsolete pending work; producer owns immutable input arrays."""
        with self._condition:
            if self._stopping or request.sequence <= self._last_sequence:
                return
            if self._pending is not None:
                self.dropped_frames += 1
            self._pending = request
            self._condition.notify()

    def set_palette(self, palette: str) -> None:
        with self._condition:
            self._palette = palette
            self._condition.notify()

    def stop(self) -> None:
        with self._condition:
            self._stopping = True
            self._pending = None
            self._condition.notify()
        self.wait()

    def run(self) -> None:
        logger.info("Thermal renderer started")
        while True:
            with self._condition:
                while self._pending is None and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    break
                request = self._pending
                self._pending = None
                palette = self._palette
            started = time.perf_counter()
            try:
                image, temperature, minimum, maximum, thumbnail, rgb, palette_ms = self._render(request, palette)
                self._last_sequence = request.sequence
                self.last_render_ms = (time.perf_counter() - started) * 1000.0
                self.last_palette_ms = palette_ms
                self.rendered.emit(image, temperature, minimum, maximum, request.sequence, thumbnail, rgb)
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
