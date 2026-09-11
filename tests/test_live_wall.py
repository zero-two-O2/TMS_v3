"""Live-mode 3x3 wall tests: 8 camera tiles + 1 statistics panel.

Covers the 22 acceptance items with no hardware:
- structure (8 tiles, 16 feeds, 1 stats panel, 3x3, fixed mapping),
- behavior (disconnect stability, simultaneous feeds, 4:3, viewport
  geometry, no per-frame geometry work),
- hover (IR/VL identification, panel switching, restore on leave),
- isolation (theme keeps positions, no acquisition impact, no feed
  selector). Display-only synthetic frames.
"""

from __future__ import annotations

import inspect
import os
import pathlib
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication
from unittest.mock import Mock

from thermal_monitor.core.frame import (
    Frame,
    FrameDescriptor,
    FramePayload,
    StreamMetadata,
    SyncInfo,
    SyncStatus,
)
from thermal_monitor.core.models import (
    AnalysisResult,
    CameraIdentity,
    TemperatureUnit,
)
from thermal_monitor.processing.worker import ProcessingResult
from thermal_monitor.services.configuration import ConfigurationService


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _wall_and_tiles(qapp, n_cameras=8):
    from thermal_monitor.ui.windows.live_window import LiveModeWidget

    config_service = ConfigurationService()
    for i in range(1, n_cameras + 1):
        identity = CameraIdentity(camera_id=f"cam_{i}", serial_number=f"SN{i:03d}")
        config_service.set_camera_config(
            config_service.create_camera_config(
                identity=identity, name=f"192.168.42.{100 + i}"
            )
        )
    wall = LiveModeWidget(
        mode_service=Mock(), config_service=config_service, theme_manager=None
    )
    wall.on_mode_activated()
    QApplication.processEvents()
    return wall, list(wall._tiles)


def _settle_wall(wall, width: int, height: int):
    """Resize and pump the event loop until the refit converges."""
    wall.show()
    wall.resize(width, height)
    last = None
    for _ in range(100):
        QApplication.processEvents()
        if (wall.width(), wall.height()) != (width, height):
            continue
        wall._refit_wall()
        QApplication.processEvents()
        viewport = wall._scroll.viewport()
        feed = wall.compute_feed_size(viewport.width(), viewport.height())
        first = wall._tiles[0]._image_widget
        key = ((first.width(), first.height()), feed,
               (wall._tiles[0].width(), wall._tiles[0].height()))
        if key == last and (first.width(), first.height()) == feed:
            break
        last = key
    QApplication.processEvents()


def _close_wall(wall):
    try:
        wall.on_mode_deactivated()
    except Exception:
        pass
    for tile in list(getattr(wall, "_tiles", [])):
        for widget in (getattr(tile, "_image_widget", None), getattr(tile, "_vl_widget", None)):
            try:
                if widget is not None:
                    widget.close()
            except Exception:
                pass
    try:
        wall.close()
    except Exception:
        pass


def _result(camera_id: str, sequence: int, temp: float = 56.7) -> ProcessingResult:
    thermal = np.full((4, 4), 1000, dtype=np.uint16)
    thermal.setflags(write=False)
    visible = np.full((4, 8), 0xAB, dtype=np.uint8)
    visible.setflags(write=False)
    descriptor = FrameDescriptor(
        camera_id=camera_id,
        sequence=sequence,
        timestamp=1000.0 + sequence,
        monotonic_timestamp=time.perf_counter(),
        thermal=StreamMetadata(present=True, width=4, height=4, sequence=sequence),
        visible=StreamMetadata(present=True, width=4, height=4, sequence=sequence),
        sync=SyncInfo(status=SyncStatus.SYNCHRONIZED, time_delta=0.0),
        metadata={},
    )
    frame = Frame(
        descriptor=descriptor, payload=FramePayload(thermal=thermal, visible=visible)
    )
    temperature_image = np.full((4, 4), temp, dtype=np.float32)
    analysis = AnalysisResult(
        camera_id=camera_id,
        frame_sequence=sequence,
        frame_timestamp=1000.0 + sequence,
        overall_mean=temp,
        unit=TemperatureUnit.CELSIUS,
    )
    return ProcessingResult(
        frame=frame,
        analysis_result=analysis,
        alarm_result=None,
        processing_time_ms=5.0,
        temperature_image=temperature_image,
    )


class _FakeEvent:
    """Real Qt event carrying only an enter/leave type."""

    def __new__(cls, event_type):
        return QEvent(event_type)


class TestLiveWallStructure:
    def test_exactly_eight_camera_tiles(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            assert len(tiles) == 8
        finally:
            _close_wall(wall)

    def test_exactly_sixteen_feeds(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            ir = [t._image_widget for t in tiles]
            vl = [t._vl_widget for t in tiles]
            assert len(ir) == 8 and len(vl) == 8
            assert len(ir + vl) == 16
        finally:
            _close_wall(wall)

    def test_exactly_one_statistics_panel(self, qapp):
        from thermal_monitor.ui.windows.live_window import LiveStatsPanel

        wall, tiles = _wall_and_tiles(qapp)
        try:
            assert isinstance(wall._stats_panel, LiveStatsPanel)
            found = wall.findChildren(LiveStatsPanel)
            assert len(found) == 1
        finally:
            _close_wall(wall)

    def test_three_columns(self, qapp):
        from thermal_monitor.ui.windows import live_window as lw

        wall, tiles = _wall_and_tiles(qapp)
        try:
            assert lw.GRID_COLUMNS == 3
            cols = {wall._grid_layout.getItemPosition(i)[1] for i in range(9)}
            assert cols == {0, 1, 2}
        finally:
            _close_wall(wall)

    def test_three_rows(self, qapp):
        from thermal_monitor.ui.windows import live_window as lw

        wall, tiles = _wall_and_tiles(qapp)
        try:
            assert lw.GRID_ROWS == 3
            rows = {wall._grid_layout.getItemPosition(i)[0] for i in range(9)}
            assert rows == {0, 1, 2}
        finally:
            _close_wall(wall)

    def test_camera_1_occupies_position_1(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            assert tiles[0].slot_index == 0
            assert tiles[0].camera_id == "cam_1"
            row, col, _, _ = wall._grid_layout.getItemPosition(0)
            assert (row, col) == (0, 0)
        finally:
            _close_wall(wall)

    def test_camera_8_occupies_position_8(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            assert tiles[7].slot_index == 7
            assert tiles[7].camera_id == "cam_8"
            row, col, _, _ = wall._grid_layout.getItemPosition(7)
            assert (row, col) == (2, 1)
        finally:
            _close_wall(wall)

    def test_statistics_occupies_position_9(self, qapp):
        from thermal_monitor.ui.windows import live_window as lw

        wall, tiles = _wall_and_tiles(qapp)
        try:
            assert (lw.STATS_GRID_ROW, lw.STATS_GRID_COL) == (2, 2)
            row, col, _, _ = wall._grid_layout.getItemPosition(8)
            assert (row, col) == (2, 2)
            assert wall._grid_layout.itemAtPosition(2, 2).widget() is wall._stats_panel
        finally:
            _close_wall(wall)

    def test_full_position_map(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            expected = {
                0: (0, 0), 1: (0, 1), 2: (0, 2),
                3: (1, 0), 4: (1, 1), 5: (1, 2),
                6: (2, 0), 7: (2, 1),
            }
            for slot, (row, col) in expected.items():
                got = wall._grid_layout.getItemPosition(slot)[:2]
                assert tuple(got) == (row, col), slot
                assert tiles[slot].camera_id == f"cam_{slot + 1}"
        finally:
            _close_wall(wall)

    def test_disconnect_does_not_reorder_tiles(self, qapp):
        from thermal_monitor.ui.windows.live_window import LiveTileState

        wall, tiles = _wall_and_tiles(qapp)
        try:
            before = [(t.slot_index, t.camera_id) for t in tiles]
            tiles[4].on_error("simulated link loss")
            QApplication.processEvents()
            after = [(t.slot_index, t.camera_id) for t in wall._tiles]
            assert before == after
            assert len(wall._tiles) == 8
            assert wall._tiles[4].camera_id == "cam_5"
            assert wall._tiles[4].state is LiveTileState.ERROR
            assert "CAM 5" in wall._tiles[4]._num_label.text()
            assert "POS 5" in wall._tiles[4]._pos_label.text()
            row, col, _, _ = wall._grid_layout.getItemPosition(4)
            assert (row, col) == (1, 1)
        finally:
            _close_wall(wall)

    def test_ir_and_vl_simultaneously_visible(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            for tile in tiles:
                assert tile._image_widget.isVisibleTo(tile)
                assert tile._vl_widget.isVisibleTo(tile)
            assert not hasattr(wall, "_feed_selector")
            for tile in tiles:
                assert not hasattr(tile, "_image_stack")
        finally:
            _close_wall(wall)

    def test_feed_aspect_ratio_is_4_to_3(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            _settle_wall(wall, 1920, 1080)
            for tile in tiles:
                for widget in (tile._image_widget, tile._vl_widget):
                    assert widget.height() == round(widget.width() * 3 / 4)
        finally:
            _close_wall(wall)

    def test_geometry_responds_to_viewport_size(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            _settle_wall(wall, 960, 600)
            small = [(t.width(), t.height()) for t in tiles]
            assert len(set(small)) == 1
            _settle_wall(wall, 1920, 1080)
            large = [(t.width(), t.height()) for t in tiles]
            assert len(set(large)) == 1
            assert large[0][0] > small[0][0]
            assert large[0][1] > small[0][1]
            feed = tiles[0]._image_widget
            assert feed.height() == round(feed.width() * 3 / 4)
            layout = wall._grid_layout
            for slot in range(8):
                row, col, _, _ = layout.getItemPosition(slot)
                assert (row, col) == (slot // 3, slot % 3)
        finally:
            _close_wall(wall)

    def test_geometry_unchanged_during_on_result(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            _settle_wall(wall, 1600, 900)
            before_tiles = [(t.width(), t.height()) for t in tiles]
            before_feeds = [(t._image_widget.width(), t._image_widget.height()) for t in tiles]
            for tile in tiles:
                if tile.camera_id is not None:
                    tile.on_result(_result(tile.camera_id, 3))
            QApplication.processEvents()
            after_tiles = [(t.width(), t.height()) for t in tiles]
            after_feeds = [(t._image_widget.width(), t._image_widget.height()) for t in tiles]
            assert before_tiles == after_tiles
            assert before_feeds == after_feeds
        finally:
            _close_wall(wall)

    def test_feed_size_formula_grows_with_viewport(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            wall.show()
            QApplication.processEvents()
            small = wall.compute_feed_size(960, 600)
            full_hd = wall.compute_feed_size(1920, 1040)
            qhd = wall.compute_feed_size(2560, 1400)
            uhd = wall.compute_feed_size(3840, 2120)
            for feed_w, feed_h in (small, full_hd, qhd, uhd):
                assert feed_h == round(feed_w * 3 / 4)
            assert small[0] < full_hd[0] < qhd[0] < uhd[0]
        finally:
            _close_wall(wall)

    def test_scroll_only_for_unusually_small_windows(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            _settle_wall(wall, 1920, 1080)
            QApplication.processEvents()
            assert wall._scroll.horizontalScrollBar().maximum() == 0
            assert wall._scroll.verticalScrollBar().maximum() == 0
            _settle_wall(wall, 500, 350)
            QApplication.processEvents()
            scrolled = (
                wall._scroll.horizontalScrollBar().maximum() > 0
                or wall._scroll.verticalScrollBar().maximum() > 0
            )
            assert scrolled
        finally:
            _close_wall(wall)

    def test_stats_panel_never_becomes_camera(self, qapp):
        wall, tiles = _wall_and_tiles(qapp, n_cameras=10)
        try:
            assert len(tiles) == 8
            assert tiles[7].camera_id == "cam_8"
            assert not hasattr(wall._stats_panel, "camera_id")
            assert not hasattr(wall._stats_panel, "set_camera")
            wall._refresh_header_and_panel()
            QApplication.processEvents()
            assert "8 of 10" in wall._stats_panel._sys_note.text()
        finally:
            _close_wall(wall)


class TestLiveWallHover:
    def test_hovering_ir_identifies_ir(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            tile = tiles[4]
            tile.eventFilter(tile._image_widget, _FakeEvent(QEvent.Type.Enter))
            QApplication.processEvents()
            assert wall._hovered == (4, "ir")
            assert wall._stats_panel.showing_camera
            assert "IR" in wall._stats_panel._cam_place.text()
        finally:
            _close_wall(wall)

    def test_hovering_vl_identifies_vl(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            tile = tiles[4]
            tile.eventFilter(tile._vl_widget, _FakeEvent(QEvent.Type.Enter))
            QApplication.processEvents()
            assert wall._hovered == (4, "vl")
            assert wall._stats_panel.showing_camera
            assert "VL" in wall._stats_panel._cam_place.text()
        finally:
            _close_wall(wall)

    def test_hovering_camera_updates_statistics(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            tiles[4].on_result(_result("cam_5", 12345, temp=54.3))
            QApplication.processEvents()
            wall._on_tile_hover(4, "ir")
            QApplication.processEvents()
            panel = wall._stats_panel
            assert panel.showing_camera
            assert "5" in panel._cam_id.text()
            assert "54.3" in panel._cam_temp.text()
            assert "12345" in panel._cam_perf.text()
        finally:
            _close_wall(wall)

    def test_leaving_wall_restores_system_statistics(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            wall._on_tile_hover(4, "vl")
            QApplication.processEvents()
            assert wall._stats_panel.showing_camera
            wall._on_tile_hover(None, None)
            QApplication.processEvents()
            assert not wall._stats_panel.showing_camera
            assert wall._hovered is None
        finally:
            _close_wall(wall)

    def test_feed_leave_keeps_camera_view(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            tile = tiles[2]
            tile.eventFilter(tile._image_widget, _FakeEvent(QEvent.Type.Enter))
            tile.eventFilter(tile._image_widget, _FakeEvent(QEvent.Type.Leave))
            QApplication.processEvents()
            assert wall._hovered == (2, None)
            assert wall._stats_panel.showing_camera
            assert "Camera 3" in wall._stats_panel._cam_id.text()
        finally:
            _close_wall(wall)

    def test_hover_events_are_not_consumed(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            tile = tiles[0]
            assert tile.eventFilter(tile._image_widget, _FakeEvent(QEvent.Type.Enter)) is False
            assert tile.eventFilter(tile._vl_widget, _FakeEvent(QEvent.Type.Leave)) is False
        finally:
            _close_wall(wall)

    def test_hover_with_live_data_keeps_geometry(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            _settle_wall(wall, 1920, 1080)
            for tile in tiles:
                if tile.camera_id is not None:
                    tile.on_result(_result(tile.camera_id, 3))
            QApplication.processEvents()
            wall._refresh_header_and_panel()
            QApplication.processEvents()
            before_grid = (wall._grid_widget.width(), wall._grid_widget.height())
            wall._on_tile_hover(4, "ir")
            QApplication.processEvents()
            wall._on_tile_hover(None, None)
            QApplication.processEvents()
            after_grid = (wall._grid_widget.width(), wall._grid_widget.height())
            assert before_grid == after_grid
            assert wall._scroll.horizontalScrollBar().maximum() == 0
            assert wall._scroll.verticalScrollBar().maximum() == 0
        finally:
            _close_wall(wall)


class TestLiveWallThemeIsolation:
    def test_theme_switch_keeps_tile_identity(self, qapp):
        from thermal_monitor.ui.theme import ThemeManager

        wall, tiles = _wall_and_tiles(qapp)
        try:
            before = [(t.camera_id, t._name_label.text(), t.slot_index) for t in tiles]
            manager = ThemeManager(None)
            manager.set_theme("industrial_light")
            manager.apply_and_refresh(qapp)
            manager.set_theme("blue_engineering")
            manager.apply_and_refresh(qapp)
            after = [(t.camera_id, t._name_label.text(), t.slot_index) for t in wall._tiles]
            assert before == after
        finally:
            _close_wall(wall)

    def test_theme_switch_keeps_camera_positions(self, qapp):
        from thermal_monitor.ui.theme import ThemeManager

        wall, tiles = _wall_and_tiles(qapp)
        try:
            _settle_wall(wall, 1600, 900)
            before_pos = [wall._grid_layout.getItemPosition(i)[:2] for i in range(9)]
            before_ids = [(t.camera_id, t.slot_index) for t in tiles]
            manager = ThemeManager(None)
            for name in ("high_contrast", "industrial_dark", "blue_engineering"):
                manager.set_theme(name)
                manager.apply_and_refresh(qapp)
            _settle_wall(wall, 1600, 900)
            after_pos = [wall._grid_layout.getItemPosition(i)[:2] for i in range(9)]
            assert before_pos == after_pos
            assert [(t.camera_id, t.slot_index) for t in wall._tiles] == before_ids
            row, col, _, _ = wall._grid_layout.getItemPosition(8)
            assert (row, col) == (2, 2)
        finally:
            _close_wall(wall)

    def test_theme_switch_does_not_restart_acquisition(self, qapp):
        from thermal_monitor.ui.theme import ThemeManager

        class StrictRuntime:
            """Only read-only stats accessors; lifecycle calls fail loudly."""

            def __init__(self):
                self.lifecycle_calls: list[str] = []

            def is_camera_running(self, camera_id):
                return True

            def observer_service(self, camera_id):
                return None

            def camera_stats(self, camera_id):
                return None

            def start_camera(self, config):
                self.lifecycle_calls.append("start_camera")
                raise AssertionError("theme switch must not start cameras")

            def start_observer(self, camera_id, analysis_config=None):
                self.lifecycle_calls.append("start_observer")
                raise AssertionError("theme switch must not start observers")

            def stop_camera(self, camera_id, **kwargs):
                self.lifecycle_calls.append("stop_camera")
                raise AssertionError("theme switch must not stop cameras")

        wall, tiles = _wall_and_tiles(qapp)
        try:
            runtime = StrictRuntime()
            wall._runtime_service = runtime
            tiles_before = list(wall._tiles)
            manager = ThemeManager(None)
            manager.set_theme("high_contrast")
            manager.apply_and_refresh(qapp)
            for _ in range(30):
                QApplication.processEvents()
            wall._poll_stats()
            QApplication.processEvents()
            assert runtime.lifecycle_calls == []
            assert list(wall._tiles) == tiles_before
            assert len(wall._tiles) == 8
        finally:
            wall._runtime_service = None
            _close_wall(wall)

    def test_theme_switch_does_not_restart_render_workers(self, qapp):
        from thermal_monitor.ui.theme import ThemeManager

        wall, tiles = _wall_and_tiles(qapp)
        try:
            workers = [
                (t._image_widget._render_worker, t._vl_widget._worker) for t in tiles
            ]
            assert all(a.isRunning() and b.isRunning() for a, b in workers)
            manager = ThemeManager(None)
            manager.set_theme("industrial_light")
            manager.apply_and_refresh(qapp)
            QApplication.processEvents()
            assert [(t._image_widget._render_worker, t._vl_widget._worker) for t in tiles] == workers
            assert all(a.isRunning() and b.isRunning() for a, b in workers)
        finally:
            _close_wall(wall)

    def test_no_global_ir_vl_selector_exists(self, qapp):
        from PyQt6.QtWidgets import QComboBox

        wall, tiles = _wall_and_tiles(qapp)
        try:
            assert wall.findChild(QComboBox) is None
            assert wall.feed_mode == "both"
            assert all(t.feed_mode == "both" for t in tiles)
        finally:
            _close_wall(wall)

    def test_all_positions_stable_across_resizes(self, qapp):
        wall, tiles = _wall_and_tiles(qapp)
        try:
            _settle_wall(wall, 1280, 800)
            before = [wall._grid_layout.getItemPosition(i)[:2] for i in range(9)]
            _settle_wall(wall, 2560, 1400)
            _settle_wall(wall, 1920, 1080)
            after = [wall._grid_layout.getItemPosition(i)[:2] for i in range(9)]
            assert before == after
            assert [t.camera_id for t in wall._tiles] == [f"cam_{i}" for i in range(1, 9)]
        finally:
            _close_wall(wall)


class TestLiveWallArchitectureGuards:
    LIVE_SOURCE = (
        pathlib.Path(__file__).resolve().parent.parent
        / "src" / "thermal_monitor" / "ui" / "windows" / "live_window.py"
    ).read_text(encoding="utf-8")

    def test_no_per_frame_stylesheet_application(self, qapp):
        from thermal_monitor.ui.windows import live_window as lw

        assert "setStyleSheet(" not in self.LIVE_SOURCE
        update_source = inspect.getsource(lw.LiveCameraTile._update_display)
        for marker in ("setStyleSheet(", "setProperty", "set_status", "set_tile_state", "repolish"):
            assert marker not in update_source
        result_source = inspect.getsource(lw.LiveCameraTile.on_result)
        assert "setStyleSheet(" not in result_source

    def test_no_per_frame_geometry_work(self, qapp):
        from thermal_monitor.ui.windows import live_window as lw

        for func in (
            lw.LiveCameraTile.on_result,
            lw.LiveCameraTile._update_display,
            lw.LiveModeWidget._on_tile_hover,
            lw.LiveStatsPanel.show_camera,
            lw.LiveStatsPanel.update_system,
        ):
            source = inspect.getsource(func)
            for marker in (
                "setFixedSize",
                "setMinimumSize",
                "setMaximumSize",
                "resize(",
                "updateGeometry",
                "invalidate(",
                "addWidget",
                "removeWidget",
            ):
                assert marker not in source, (func.__name__, marker)

    def test_no_new_acquisition_path(self):
        for forbidden in (
            "AcquisitionWorker",
            "CustomTV46LDriver",
            "SharedMemory",
            "GVCP",
            "GVSP",
            "RecordingConsumer",
            "CalibrationProvider",
            "perform_nuc",
            "set_focus_mm",
        ):
            assert forbidden not in self.LIVE_SOURCE

    def test_latest_frame_wins_intact(self, qapp):
        from thermal_monitor.ui.windows.live_window import LiveCameraTile

        tile = LiveCameraTile(0, camera_id="cam_1", name="cam_1", serial="SN001")
        try:
            tile.on_result(_result("cam_1", 10))
            assert tile._image_widget._last_submitted_sequence == 10
            # Stale frame is refused by the feed widget: no display regress.
            tile.on_result(_result("cam_1", 5))
            assert tile._image_widget._last_submitted_sequence == 10
            assert tile.frames_received == 2
        finally:
            tile._image_widget.close()
            tile._vl_widget.close()
