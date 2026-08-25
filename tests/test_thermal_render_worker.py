"""Regression tests for bounded thermal rendering."""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtWidgets import QApplication

from thermal_monitor.ui.modes.thermal_render_worker import RenderRequest, ThermalRenderWorker


def _qapp():
    return QApplication.instance() or QApplication([])


def test_palette_render_is_vectorized_and_worker_owned():
    _qapp()
    worker = ThermalRenderWorker()
    owner = []
    original = worker._render

    def wrapped(request, palette):
        owner.append(threading.get_ident())
        return original(request, palette)

    worker._render = wrapped
    received = []
    worker.rendered.connect(lambda *args: received.append(args))
    worker.start()
    worker.submit(RenderRequest(np.arange(640 * 480, dtype=np.float32).reshape(480, 640), 1))
    deadline = time.monotonic() + 2
    while not received and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    worker.stop()
    assert owner and owner[0] != threading.get_ident()
    assert original(RenderRequest(np.zeros((2, 3), dtype=np.float32), 2), "gray")[0].size().width() == 3


def test_latest_pending_frame_replaces_old_frame():
    worker = ThermalRenderWorker(max_fps=1000)
    with worker._condition:
        worker._pending = RenderRequest(np.zeros((2, 2), dtype=np.float32), 1)
    worker.submit(RenderRequest(np.ones((2, 2), dtype=np.float32), 2))
    with worker._condition:
        assert worker._pending is not None
        assert worker._pending.sequence == 2
        assert worker.dropped_frames == 1


def test_renderer_shutdown_is_clean():
    worker = ThermalRenderWorker()
    worker.start()
    worker.stop()
    assert not worker.isRunning()
