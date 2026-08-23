"""
ui.modes.observer_image -- Reusable live thermal image widget.

Extracted from the observer mode so it can be embedded in each
:class:`~thermal_monitor.ui.modes.observer.CameraTile`.  The source temperature
array is copied into a fresh uint8 display buffer before the QImage is
constructed, so the widget never retains a reference to the shared processing
result array.  A later mutation of the source cannot affect the displayed pixels.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPainter, QColor, QFont
from PyQt6.QtWidgets import QWidget

import numpy as np


class LiveThermalWidget(QWidget):
    """Displays the live thermal image (temperature image or raw thermal)."""

    def __init__(self) -> None:
        super().__init__()
        self._temperature_image: np.ndarray | None = None
        self._raw_thermal: np.ndarray | None = None
        self._display_image: QImage | None = None
        self._display_array: np.ndarray | None = None
        self.setMinimumSize(320, 240)

    @property
    def display_array(self) -> np.ndarray | None:
        """The copied uint8 display buffer (test/inspection hook)."""
        return self._display_array

    def set_frame(self, temperature_image: np.ndarray | None, frame) -> None:
        """Update the displayed image from a processed frame."""
        self._temperature_image = temperature_image
        self._raw_thermal = frame.payload.thermal if frame is not None else None
        self._rebuild_display()
        self.update()

    def clear(self) -> None:
        self._temperature_image = None
        self._raw_thermal = None
        self._display_image = None
        self._display_array = None
        self.update()

    def _rebuild_display(self) -> None:
        src = self._temperature_image
        if src is None:
            src = self._raw_thermal
        if src is None:
            self._display_image = None
            self._display_array = None
            return

        src = np.asarray(src)
        if src.ndim != 2:
            self._display_image = None
            self._display_array = None
            return

        finite = np.isfinite(src)
        if not np.any(finite):
            display = np.zeros(src.shape, dtype=np.uint8)
        else:
            lo = float(src[finite].min())
            hi = float(src[finite].max())
            if hi <= lo:
                hi = lo + 1.0
            normalized = np.clip((src - lo) / (hi - lo), 0.0, 1.0)
            normalized[~finite] = 0.0
            display = (normalized * 255.0).astype(np.uint8)

        self._display_array = display  # fresh array, no view of the source
        h, w = display.shape
        self._display_image = QImage(
            display.data, w, h, display.strides[0], QImage.Format.Format_Grayscale8
        ).copy()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(20, 20, 20))

        if self._display_image is not None:
            scaled = self._display_image.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawImage(x, y, scaled)
        else:
            painter.setPen(QColor(150, 150, 150))
            painter.setFont(QFont("Segoe UI", 12))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No live data")


__all__ = ["LiveThermalWidget"]
