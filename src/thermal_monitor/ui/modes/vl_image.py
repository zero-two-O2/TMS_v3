"""Live visible-light image widget (Stage 8E).

Displays the raw VL plane of the same hardware frame shown thermally:
the caller feeds ``(yuyv, worker_sequence, hw_sequence)`` triples taken from
one processing result, so IR/VL correlation is preserved by construction.
Rendering (YUYV -> RGB) happens in :class:`VlRenderWorker`, off the GUI
thread; this widget only paints the latest QImage.
"""

from __future__ import annotations

import time

import numpy as np
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QImage, QPainter, QFont
from PyQt6.QtWidgets import QWidget

from thermal_monitor.core.frame_latency import (
    get_default_tracker as _latency_tracker,
    latency_enabled as _latency_enabled,
)
from thermal_monitor.ui.modes.vl_render_worker import VlRenderRequest, VlRenderWorker


class VlImageWidget(QWidget):
    """Displays the live VL image for one camera feed."""

    render_error = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._display_image: QImage | None = None
        self._sequence: int | None = None
        self._hw_sequence: int | None = None
        self._has_vl = False
        self._worker = VlRenderWorker(parent=self)
        self._worker.latest_ready.connect(self._on_latest_ready)
        self._worker.render_error.connect(self.render_error.emit)
        # Bounded reap (never an unbounded wait, never a TypeError from
        # the destroyed(QObject*) argument) — see VlRenderWorker.
        self.destroyed.connect(self._worker._on_destroyed)
        self._worker.start()
        # seq -> (camera_id, hw_sequence, acq_mono_ns), bounded by pruning.
        self._pending_meta: dict[int, tuple[str | None, int | None, int | None]] = {}
        self._last_sequence: int = -1
        # Camera-session gate (same contract as LiveThermalWidget).
        self._session_camera_id: str | None = None
        self.setMinimumSize(320, 240)

    def set_session(self, camera_id: str | None) -> None:
        """Begin a new camera session on this widget (see LiveThermalWidget)."""
        self._session_camera_id = camera_id
        self._last_sequence = -1
        self._pending_meta.clear()
        self._worker.set_session(camera_id)
        self.clear()

    def set_frame(
        self,
        yuyv: np.ndarray | None,
        sequence: int,
        hw_sequence: "int | None" = None,
        camera_id: str | None = None,
        acq_mono_ns: int | None = None,
    ) -> None:
        """Submit a VL plane for display (latest-wins; stale dropped).

        The plane is copied: SHM frame views are transient and must never
        be retained by the GUI thread.
        """
        if yuyv is None:
            self._has_vl = False
            self.update()
            return
        if self._session_camera_id is not None and camera_id is not None:
            if camera_id != self._session_camera_id:
                return  # stale plane from a previous camera session
            if sequence <= self._last_sequence:
                return  # stale sequence within the current session
        self._last_sequence = sequence
        self._has_vl = True
        owned = np.ascontiguousarray(yuyv, dtype=np.uint8).copy()
        if _latency_enabled():
            self._pending_meta[sequence] = (camera_id, hw_sequence, acq_mono_ns)
        self._worker.submit(VlRenderRequest(yuyv=owned, sequence=sequence, hw_sequence=hw_sequence, camera_id=camera_id, acq_mono_ns=acq_mono_ns))

    @pyqtSlot(object, int, object)
    def _on_rendered(self, image: QImage, sequence: int, hw_sequence: "int | None") -> None:
        self._display_image = image
        self._sequence = sequence
        self._hw_sequence = hw_sequence
        if _latency_enabled():
            meta = self._pending_meta.pop(sequence, None)
            for old in [s for s in self._pending_meta if s <= sequence]:
                del self._pending_meta[old]
            if meta is not None and meta[0] is not None:
                _latency_tracker().note_displayed(
                    meta[0] + "#vl", sequence, meta[1], meta[2], time.perf_counter_ns()
                )
        self.update()

    @pyqtSlot()
    def _on_latest_ready(self) -> None:
        output = self._worker.take_latest_output()
        if output is not None:
            self._on_rendered(*output)

    def clear(self) -> None:
        self._display_image = None
        self._sequence = None
        self._hw_sequence = None
        self._has_vl = False
        # Same reconnect baseline reset as LiveThermalWidget.clear.
        self._last_sequence = -1
        self._pending_meta.clear()
        self.update()

    def prepare_for_transition(self) -> None:
        """Detach this widget from the live frame path WITHOUT blocking.

        Drops the camera session and requests renderer shutdown,
        returning immediately (see LiveThermalWidget). The ``destroyed``
        handler reaps the thread with a bounded wait.
        """
        try:
            self.set_session(None)
        except RuntimeError:
            pass  # already torn down
        try:
            self._worker.request_stop()
        except RuntimeError:
            pass

    def wait_for_renderer(self, timeout_ms: int = 200) -> bool:
        """Bounded reap of the render thread (transition second pass).

        See LiveThermalWidget.wait_for_renderer: all siblings are woken
        first, then all are joined, so Qt can never delete a
        still-running QThread during window teardown.
        """
        try:
            return bool(self._worker.stop_bounded(timeout_ms))
        except RuntimeError:
            return True  # already stopped / C++ object gone

    def close(self) -> None:
        try:
            self._worker.request_stop()
            self._worker.stop_bounded(500)
        except RuntimeError:
            pass  # already stopped / never started under test harnesses

    def __del__(self) -> None:
        worker = getattr(self, "_worker", None)
        if worker is not None:
            try:
                worker.request_stop()
            except RuntimeError:
                pass

    def closeEvent(self, event) -> None:
        self.close()
        super().closeEvent(event)

    @property
    def last_sequence(self) -> int | None:
        return self._sequence

    @property
    def last_hw_sequence(self) -> "int | None":
        return self._hw_sequence

    @property
    def has_image(self) -> bool:
        return self._display_image is not None

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        if self._display_image is None:
            painter.setPen(Qt.GlobalColor.gray)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "VL unavailable" if not self._has_vl else "Waiting for VL…")
            return
        scaled = self._display_image.scaled(
            self.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        painter.drawImage(x, y, scaled)
        if self._hw_sequence is not None:
            painter.setPen(Qt.GlobalColor.white)
            painter.setFont(QFont("monospace", 8))
            painter.drawText(6, self.height() - 8, f"VL hw:{self._hw_sequence}")


__all__ = ["VlImageWidget"]
