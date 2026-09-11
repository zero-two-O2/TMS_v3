"""Stage 8E Phase 5 tests: live VL display (conversion, worker, widget, wall).

No hardware required. Proves: YUYV->RGB conversion correctness, worker
latest-wins + lifecycle, widget correlation bookkeeping + disconnect,
tile feed-mode switching, wall-wide IR/VL toggle. GUI work stays
off the acquisition path (synthetic YUYV planes only).
"""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from thermal_monitor.ui.modes.vl_convert import yuyv_to_rgb
from thermal_monitor.ui.modes.vl_render_worker import VlRenderRequest, VlRenderWorker
from thermal_monitor.ui.modes.vl_image import VlImageWidget


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _yuyv_pair(y0: int, u: int, y1: int, v: int) -> np.ndarray:
    return np.array([[y0, u, y1, v]], dtype=np.uint8)


def _gray_plane(y: int, height: int = 4, width_pairs: int = 4) -> np.ndarray:
    row = [y, 128] * width_pairs
    return np.array([row for _ in range(height)], dtype=np.uint8)


class TestYuyvConversion:
    def test_black_and_white(self):
        black = yuyv_to_rgb(_yuyv_pair(16, 128, 16, 128))
        assert black.shape == (1, 2, 3)  # one pair = two pixels
        assert black.dtype == np.uint8
        np.testing.assert_array_equal(black[0, 0], [0, 0, 0])
        np.testing.assert_array_equal(black[0, 1], [0, 0, 0])
        white = yuyv_to_rgb(_yuyv_pair(235, 128, 235, 128))
        np.testing.assert_array_equal(white[0, 0], [255, 255, 255])
        np.testing.assert_array_equal(white[0, 1], [255, 255, 255])

    def test_neutral_gray_pair(self):
        rgb = yuyv_to_rgb(_yuyv_pair(128, 128, 128, 128))
        # c=112 -> (298*112+128)>>8 = 130
        np.testing.assert_array_equal(rgb[0, 0], [130, 130, 130])
        np.testing.assert_array_equal(rgb[0, 1], [130, 130, 130])

    def test_shape_dtype_contiguous(self):
        rgb = yuyv_to_rgb(_gray_plane(100, height=480, width_pairs=640))
        assert rgb.shape == (480, 640, 3)
        assert rgb.dtype == np.uint8
        assert rgb.flags["C_CONTIGUOUS"]

    def test_invalid_inputs_rejected(self):
        with pytest.raises(ValueError):
            yuyv_to_rgb(np.zeros((4, 5), dtype=np.uint8))  # odd width
        with pytest.raises(ValueError):
            yuyv_to_rgb(np.zeros((4, 4, 3), dtype=np.uint8))  # 3-D
        with pytest.raises(ValueError):
            yuyv_to_rgb(np.zeros((4, 4), dtype=np.uint16))  # wrong dtype


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        QApplication.processEvents()
        time.sleep(0.01)
    return predicate()


class TestVlRenderWorker:
    def test_renders_newest_and_drops_stale(self, qapp):
        worker = VlRenderWorker(max_fps=1000.0)
        try:
            got: list = []
            worker.rendered.connect(lambda *a: got.append(a))
            worker.start()
            worker.submit(VlRenderRequest(yuyv=_gray_plane(50), sequence=5, hw_sequence=50))
            assert _wait_until(lambda: len(got) >= 1)
            # Stale sequence is refused outright (latest-wins at submit).
            before = worker.dropped_frames
            worker.submit(VlRenderRequest(yuyv=_gray_plane(60), sequence=3, hw_sequence=30))
            time.sleep(0.2)
            QApplication.processEvents()
            assert worker.dropped_frames == before  # refused, not queued-then-dropped
            assert got[-1][1] == 5 and got[-1][2] == 50
            assert worker.last_render_ms >= 0
        finally:
            worker.stop()
        assert not worker.isRunning()

    def test_bad_input_reports_error_not_crash(self, qapp):
        worker = VlRenderWorker(max_fps=1000.0)
        try:
            errors: list = []
            worker.render_error.connect(errors.append)
            worker.start()
            worker.submit(VlRenderRequest(
                yuyv=np.zeros((4, 5), dtype=np.uint8), sequence=1, hw_sequence=1
            ))
            assert _wait_until(lambda: len(errors) >= 1)
        finally:
            worker.stop()


class TestVlImageWidget:
    def test_frame_shows_with_correlated_sequence(self, qapp):
        widget = VlImageWidget()
        try:
            widget.set_frame(_gray_plane(100, height=480, width_pairs=640), 7, hw_sequence=77)
            assert _wait_until(lambda: widget.has_image)
            assert widget.last_sequence == 7
            assert widget.last_hw_sequence == 77
        finally:
            widget.close()

    def test_none_frame_shows_placeholder(self, qapp):
        widget = VlImageWidget()
        try:
            widget.set_frame(None, 0)
            QApplication.processEvents()
            assert not widget.has_image
        finally:
            widget.close()

    def test_clear_and_close(self, qapp):
        widget = VlImageWidget()
        try:
            widget.set_frame(_gray_plane(100), 4, hw_sequence=4)
            assert _wait_until(lambda: widget.has_image)
            widget.clear()
            assert not widget.has_image
            assert widget.last_sequence is None
        finally:
            widget.close()


class TestLiveWallFeedMode:
    def test_tile_shows_both_feeds_simultaneously(self, qapp):
        from thermal_monitor.ui.windows.live_window import LiveCameraTile

        tile = LiveCameraTile(0, theme_manager=None)
        try:
            assert tile.feed_mode == "both"
            # Both feed widgets exist and are visible side by side.
            assert tile._image_widget.isVisibleTo(tile)
            assert tile._vl_widget.isVisibleTo(tile)
            # Compatibility shim accepts historic values and keeps both.
            tile.set_feed_mode("vl")
            assert tile.feed_mode == "both"
            tile.set_feed_mode("ir")
            assert tile.feed_mode == "both"
            with pytest.raises(ValueError):
                tile.set_feed_mode("bogus")
        finally:
            tile._image_widget.close()
            tile._vl_widget.close()

    def test_wall_shows_both_feeds_on_all_tiles(self, qapp):
        from unittest.mock import Mock

        from thermal_monitor.ui.windows.live_window import LiveModeWidget

        wall = LiveModeWidget(
            mode_service=Mock(), config_service=Mock(), theme_manager=None
        )
        try:
            assert wall.feed_mode == "both"
            assert all(t.feed_mode == "both" for t in wall._tiles)
            assert all(t._image_widget.isVisibleTo(t) for t in wall._tiles)
            assert all(t._vl_widget.isVisibleTo(t) for t in wall._tiles)
            # No global IR/VL toggle on the simultaneous wall.
            assert not hasattr(wall, "_feed_selector")
        finally:
            for tile in wall._tiles:
                tile._image_widget.close()
                tile._vl_widget.close()
            wall.close()
