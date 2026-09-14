"""Live CONNECT & START ALL tests: configuration discovery to displayed feeds.

Covers the Live-mode startup acceptance items with no hardware, using a
fake runtime service behind the real ``CameraRuntimeService`` interface
(``start_camera`` / ``start_observer`` / ``is_camera_running`` /
``observer_service`` / ``camera_stats`` / ``stop_camera``) and real Qt
signals for ``result_ready`` / ``error_occurred``:

- items 1-2: configured cameras discovered; enabled/thermal filtering.
- item 3: configured-but-stopped is READY, never NOT CONFIGURED.
- items 4-5: CONNECT & START ALL starts every eligible camera on its
  fixed tile (Camera N -> Tile N).
- items 6, 9: repeated clicks never duplicate observers or connections.
- items 7-8: partial failure keeps healthy cameras live in place.
- items 10-11: IR and VL feeds both receive results.
- item 12: statistics and header reflect the real runtime state.
- items 13-14: startup never blocks the GUI thread.
- item 15: 8 cameras expose 16 feed targets (see test_live_wall.py).
- item 16: latest-frame-wins survives the connect path.
- item 17: theme switching keeps the started wall intact.
- item 18: 3x3 geometry is unchanged by startup.
- item 19: disconnect/reconnect preserves the tile position.
- item 20: RETRY FAILED restarts only failed cameras.
- states: READY / STARTING / LIVE / RECONNECTING / ERROR / NOT AVAILABLE.
"""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

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
from thermal_monitor.ui.windows.live_window import (
    FIXED_CAMERA_SLOTS,
    LiveModeWidget,
    LiveTileState,
)


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


# ─── Fakes (same interface shape as CameraRuntimeService/ObserverService) ──


class FakeObserver(QObject):
    """Qt-signal-compatible stand-in for ObserverService."""

    result_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self, camera_id: str = "") -> None:
        super().__init__()
        self._camera_id = camera_id
        self._processed = 0
        self._running = True

    @property
    def is_running(self) -> bool:
        return self._running

    def stats(self):
        self._processed += 0  # stats() itself never advances the counter
        return SimpleNamespace(
            frames_processed=self._processed,
            average_processing_time_ms=5.0,
            last_processed_at=time.time(),
        )

    def note_processed(self) -> None:
        self._processed += 1

    def stop(self, timeout=None) -> None:
        self._running = False


class FakeRuntime:
    """Thread-safe stand-in for CameraRuntimeService (no SHM/hardware)."""

    def __init__(self, fail_cameras=(), delay_s: float = 0.0) -> None:
        self._lock = threading.Lock()
        self._running: set[str] = set()
        self._observers: dict[str, FakeObserver] = {}
        self._fail = set(fail_cameras)
        self.delay_s = delay_s
        self.worker_state = "streaming"
        self.start_camera_calls: list[str] = []
        self.start_observer_calls: list[str] = []
        self.start_call_threads: list[int] = []

    def set_failing(self, camera_ids) -> None:
        with self._lock:
            self._fail = set(camera_ids)

    def start_camera(self, config):
        self.start_call_threads.append(threading.get_ident())
        if self.delay_s:
            time.sleep(self.delay_s)
        camera_id = config.identity.camera_id
        with self._lock:
            self.start_camera_calls.append(camera_id)
            if camera_id in self._fail:
                raise RuntimeError(f"connect failed for {camera_id}")
            self._running.add(camera_id)
        return camera_id

    def is_camera_running(self, camera_id: str) -> bool:
        with self._lock:
            return camera_id in self._running

    def start_observer(self, camera_id, analysis_config=None):
        with self._lock:
            self.start_observer_calls.append(camera_id)
            observer = self._observers.get(camera_id)
            if observer is not None and observer.is_running:
                return observer
            observer = FakeObserver(camera_id)
            self._observers[camera_id] = observer
            return observer

    def observer_service(self, camera_id):
        with self._lock:
            return self._observers.get(camera_id)

    def camera_stats(self, camera_id):
        with self._lock:
            if camera_id not in self._running:
                return None
        return SimpleNamespace(
            current_fps=9.0,
            average_fps=9.0,
            dropped=0,
            state=self.worker_state,
        )

    def stop_camera(self, camera_id, **kwargs) -> None:
        with self._lock:
            self._running.discard(camera_id)
            observer = self._observers.pop(camera_id, None)
        if observer is not None:
            observer.stop()


# ─── Helpers ────────────────────────────────────────────────────────────────


def _make_wall(qapp, n_cameras=8, runtime=None, configure=None):
    config_service = ConfigurationService()
    for i in range(1, n_cameras + 1):
        identity = CameraIdentity(camera_id=f"cam_{i}", serial_number=f"SN{i:03d}")
        config = config_service.create_camera_config(
            identity=identity, name=f"192.168.42.{100 + i}"
        )
        if configure is not None:
            config = configure(i, config)
        config_service.set_camera_config(config)
    wall = LiveModeWidget(
        mode_service=Mock(), config_service=config_service,
        runtime_service=runtime, theme_manager=None,
    )
    wall.on_mode_activated()
    QApplication.processEvents()
    return wall


def _pump(rounds: int = 30) -> None:
    for _ in range(rounds):
        QApplication.processEvents()
        time.sleep(0.005)


def _wait_startup_done(wall, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while wall._startup_worker is not None and wall._startup_worker.isRunning():
        if time.monotonic() > deadline:
            raise TimeoutError("startup worker did not finish")
        QApplication.processEvents()
        time.sleep(0.01)
    _pump()


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


def _emit_all_results(runtime: FakeRuntime, sequence: int = 1) -> None:
    for camera_id, observer in list(runtime._observers.items()):
        observer.result_ready.emit(_result(camera_id, sequence))
        observer.note_processed()
    _pump()


def _close_wall(wall) -> None:
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


# ─── Items 1-3: discovery, filtering, READY vs NOT CONFIGURED ───────────────


class TestConnectAllDiscovery:
    def test_configured_cameras_assigned_to_fixed_slots(self, qapp):
        """Items 1+5: every configured camera lands on its fixed tile."""
        wall = _make_wall(qapp, n_cameras=8, runtime=FakeRuntime())
        try:
            assert len(wall._camera_to_slot) == 8
            for slot in range(FIXED_CAMERA_SLOTS):
                tile = wall._tiles[slot]
                assert tile.camera_id == f"cam_{slot + 1}"
                assert tile.slot_index == slot
                row, col, _, _ = wall._grid_layout.getItemPosition(slot)
                assert (row, col) == (slot // 3, slot % 3)
        finally:
            _close_wall(wall)

    def test_disabled_cameras_are_filtered_out(self, qapp):
        """Item 2: enabled=False cameras never reach a Live slot."""
        from dataclasses import replace

        def configure(i, config):
            if i == 3:
                return replace(config, enabled=False)
            return config

        wall = _make_wall(qapp, n_cameras=8, runtime=FakeRuntime(), configure=configure)
        try:
            assert "cam_3" not in wall._camera_to_slot
            # cam_4 slides into slot 2; tile 7 keeps its position, unassigned.
            assert wall._tiles[2].camera_id == "cam_4"
            assert wall._tiles[7].camera_id is None
            assert wall._tiles[7].state is LiveTileState.NOT_AVAILABLE
        finally:
            _close_wall(wall)

    def test_thermal_disabled_cameras_are_filtered_out(self, qapp):
        """Item 2: thermal_enabled=False cameras never reach a Live slot."""
        from dataclasses import replace

        def configure(i, config):
            if i in (1, 8):
                return replace(config, thermal_enabled=False)
            return config

        wall = _make_wall(qapp, n_cameras=8, runtime=FakeRuntime(), configure=configure)
        try:
            assert "cam_1" not in wall._camera_to_slot
            assert "cam_8" not in wall._camera_to_slot
            assert len(wall._camera_to_slot) == 6
        finally:
            _close_wall(wall)

    def test_configured_but_stopped_is_ready(self, qapp):
        """Item 3: a configured camera is READY, never NOT CONFIGURED."""
        wall = _make_wall(qapp, n_cameras=8, runtime=FakeRuntime())
        try:
            for tile in wall._tiles:
                assert tile.state is LiveTileState.READY
                assert tile._name_label.text() != "Not configured"
            assert "No cameras configured" not in wall._summary_label.text()
            assert "READY" in wall._summary_label.text()
        finally:
            _close_wall(wall)

    def test_no_cameras_means_not_available_everywhere(self, qapp):
        """Items 3+10: zero configs show NOT AVAILABLE and a dead button."""
        wall = _make_wall(qapp, n_cameras=0, runtime=FakeRuntime())
        try:
            assert all(t.state is LiveTileState.NOT_AVAILABLE for t in wall._tiles)
            assert wall._summary_label.text() == "No cameras configured"
            assert wall._connect_button.text() == "NO CAMERAS"
            assert not wall._connect_button.isEnabled()
        finally:
            _close_wall(wall)

    def test_header_and_stats_before_connection(self, qapp):
        """Items 11+12: header/stats show zeros, never fake liveness."""
        wall = _make_wall(qapp, n_cameras=8, runtime=FakeRuntime())
        try:
            summary = wall._summary_label.text()
            assert "Connected: 0/8" in summary
            assert "Status: READY" in summary
            panel = wall._stats_panel
            assert "Configured: 8" in panel._sys_cams.text()
            assert "Connected: 0" in panel._sys_cams.text()
            assert "Running: 0" in panel._sys_cams.text()
            assert "Failed: 0" in panel._sys_cams.text()
            assert "IR: 0/8" in panel._sys_feeds.text()
            assert "VL: 0/8" in panel._sys_feeds.text()
        finally:
            _close_wall(wall)


# ─── Items 4-5, 10-12, 15: CONNECT & START ALL happy path ───────────────────


class TestConnectAllHappyPath:
    def _started_wall(self, qapp, runtime):
        wall = _make_wall(qapp, n_cameras=8, runtime=runtime)
        wall._on_connect_clicked()
        _wait_startup_done(wall)
        return wall

    def test_connect_all_starts_every_camera(self, qapp):
        """Item 4: one click starts all eligible cameras via the runtime."""
        runtime = FakeRuntime()
        wall = self._started_wall(qapp, runtime)
        try:
            assert sorted(runtime.start_camera_calls) == [f"cam_{i}" for i in range(1, 9)]
            assert sorted(runtime.start_observer_calls) == [f"cam_{i}" for i in range(1, 9)]
            assert all(t.state is LiveTileState.STARTING for t in wall._tiles)
        finally:
            _close_wall(wall)

    def test_feeds_go_live_and_stats_update(self, qapp):
        """Items 10+11+12: IR+VL results flip tiles LIVE; stats follow."""
        runtime = FakeRuntime()
        wall = self._started_wall(qapp, runtime)
        try:
            _emit_all_results(runtime)
            wall._poll_stats()
            _pump()
            assert all(t.state is LiveTileState.RUNNING for t in wall._tiles)
            assert all(t._ir_live and t._vl_live for t in wall._tiles)
            assert wall._connect_button.text() == "ALL CAMERAS LIVE"
            summary = wall._summary_label.text()
            assert "Connected: 8/8" in summary
            assert "72" in summary  # 8 cameras x 9 fps
            assert "Status: NORMAL" in summary
            panel = wall._stats_panel
            assert "Configured: 8" in panel._sys_cams.text()
            assert "Connected: 8" in panel._sys_cams.text()
            assert "Running: 8" in panel._sys_cams.text()
            assert "Failed: 0" in panel._sys_cams.text()
            assert "IR: 8/8" in panel._sys_feeds.text()
            assert "VL: 8/8" in panel._sys_feeds.text()
            assert "72.0" in panel._sys_fps.text()
        finally:
            _close_wall(wall)

    def test_fixed_positions_survive_startup(self, qapp):
        """Item 5: Camera N stays on Tile N through connect and results."""
        runtime = FakeRuntime()
        wall = self._started_wall(qapp, runtime)
        try:
            before = [wall._grid_layout.getItemPosition(i)[:2] for i in range(9)]
            _emit_all_results(runtime)
            wall._poll_stats()
            _pump()
            for slot in range(8):
                assert wall._tiles[slot].camera_id == f"cam_{slot + 1}"
            after = [wall._grid_layout.getItemPosition(i)[:2] for i in range(9)]
            assert before == after
        finally:
            _close_wall(wall)

    def test_sixteen_feed_targets_for_eight_cameras(self, qapp):
        """Item 15: 8 cameras expose 8 IR + 8 VL feed widgets."""
        runtime = FakeRuntime()
        wall = self._started_wall(qapp, runtime)
        try:
            _emit_all_results(runtime)
            _pump()
            ir = [t._image_widget for t in wall._tiles]
            vl = [t._vl_widget for t in wall._tiles]
            assert len(ir) + len(vl) == 16
            assert all(t.frames_received == 1 for t in wall._tiles)
        finally:
            _close_wall(wall)


# ─── Items 6, 9, 16: idempotency and signal-once ────────────────────────────


class TestConnectAllIdempotent:
    def test_repeated_clicks_do_not_duplicate(self, qapp):
        """Item 6: double-click starts each camera/observer exactly once."""
        runtime = FakeRuntime(delay_s=0.05)
        wall = _make_wall(qapp, n_cameras=8, runtime=runtime)
        try:
            wall._on_connect_clicked()
            wall._on_connect_clicked()  # while STARTING: must be ignored
            _wait_startup_done(wall)
            wall._on_connect_clicked()  # while LIVE: nothing left to queue
            _pump()
            for i in range(1, 9):
                assert runtime.start_camera_calls.count(f"cam_{i}") == 1
                assert runtime.start_observer_calls.count(f"cam_{i}") == 1
        finally:
            _close_wall(wall)

    def test_result_signal_connected_exactly_once(self, qapp):
        """Item 9: one emission reaches the tile exactly once, even after re-ready."""
        runtime = FakeRuntime()
        wall = _make_wall(qapp, n_cameras=8, runtime=runtime)
        try:
            wall._on_connect_clicked()
            _wait_startup_done(wall)
            tile = wall._tiles[0]
            before = tile.frames_received
            runtime._observers["cam_1"].result_ready.emit(_result("cam_1", 7))
            _pump()
            assert tile.frames_received == before + 1
            # Simulate a duplicate ready notification for the same camera.
            wall._on_startup_camera_ready("cam_1", wall._startup_token)
            _pump()
            runtime._observers["cam_1"].result_ready.emit(_result("cam_1", 8))
            _pump()
            assert tile.frames_received == before + 2
            assert runtime.start_observer_calls.count("cam_1") == 1
        finally:
            _close_wall(wall)

    def test_latest_frame_wins_survives_connect(self, qapp):
        """Item 16: stale sequences are still dropped after CONNECT ALL."""
        runtime = FakeRuntime()
        wall = _make_wall(qapp, n_cameras=8, runtime=runtime)
        try:
            wall._on_connect_clicked()
            _wait_startup_done(wall)
            tile = wall._tiles[0]
            tile.on_result(_result("cam_1", 10))
            assert tile._image_widget._last_submitted_sequence == 10
            tile.on_result(_result("cam_1", 5))
            assert tile._image_widget._last_submitted_sequence == 10
            assert tile.frames_received == 2
        finally:
            _close_wall(wall)


# ─── Items 7-8, 19-20: partial failure and retry ────────────────────────────


class TestConnectAllPartialFailure:
    def _failed_wall(self, qapp, runtime):
        wall = _make_wall(qapp, n_cameras=8, runtime=runtime)
        wall._on_connect_clicked()
        _wait_startup_done(wall)
        _emit_all_results(runtime)
        wall._poll_stats()
        _pump()
        return wall

    def test_partial_failure_keeps_others_live_in_place(self, qapp):
        """Items 7+8: one failure degrades the wall; nobody moves."""
        runtime = FakeRuntime(fail_cameras={"cam_3"})
        wall = self._failed_wall(qapp, runtime)
        try:
            failed_tile = wall._tiles[2]
            assert failed_tile.camera_id == "cam_3"
            assert failed_tile.state is LiveTileState.ERROR
            row, col, _, _ = wall._grid_layout.getItemPosition(2)
            assert (row, col) == (0, 2)
            for slot in [0, 1, 3, 4, 5, 6, 7]:
                assert wall._tiles[slot].state is LiveTileState.RUNNING
            assert wall._connect_button.text() == "RETRY FAILED"
            assert wall._connect_button.isEnabled()
            summary = wall._summary_label.text()
            assert "Connected: 7/8" in summary
            assert "DEGRADED" in summary
            assert "Failed: 1" in wall._stats_panel._sys_cams.text()
        finally:
            _close_wall(wall)

    def test_retry_failed_restarts_only_failed(self, qapp):
        """Item 20: RETRY FAILED revives cam_3 without touching healthy tiles."""
        runtime = FakeRuntime(fail_cameras={"cam_3"})
        wall = self._failed_wall(qapp, runtime)
        try:
            runtime.set_failing(set())
            wall._on_connect_clicked()
            _wait_startup_done(wall)
            _emit_all_results(runtime)
            wall._poll_stats()
            _pump()
            assert wall._tiles[2].state is LiveTileState.RUNNING
            assert wall._tiles[2].camera_id == "cam_3"
            assert runtime.start_camera_calls.count("cam_3") == 2
            for i in [1, 2, 4, 5, 6, 7, 8]:
                assert runtime.start_camera_calls.count(f"cam_{i}") == 1
            assert wall._connect_button.text() == "ALL CAMERAS LIVE"
        finally:
            _close_wall(wall)

    def test_disconnect_reconnect_preserves_tile(self, qapp):
        """Item 19: error then retry keeps Camera 5 on Tile 5."""
        runtime = FakeRuntime()
        wall = _make_wall(qapp, n_cameras=8, runtime=runtime)
        try:
            wall._on_connect_clicked()
            _wait_startup_done(wall)
            _emit_all_results(runtime)
            _pump()
            assert wall._tiles[4].state is LiveTileState.RUNNING
            # Simulate a mid-run link loss on one camera.
            runtime.set_failing({"cam_5"})
            runtime._running.discard("cam_5")
            wall._tiles[4].on_error("simulated link loss")
            _pump()
            assert wall._tiles[4].camera_id == "cam_5"
            assert wall._tiles[4].state is LiveTileState.ERROR
            row, col, _, _ = wall._grid_layout.getItemPosition(4)
            assert (row, col) == (1, 1)
            # Reconnect only the failed camera.
            runtime.set_failing(set())
            wall._on_connect_clicked()
            _wait_startup_done(wall)
            _emit_all_results(runtime)
            _pump()
            assert wall._tiles[4].camera_id == "cam_5"
            assert wall._tiles[4].state is LiveTileState.RUNNING
            assert runtime.start_camera_calls.count("cam_5") == 2
            assert runtime.start_camera_calls.count("cam_1") == 1
        finally:
            _close_wall(wall)


# ─── Items 13-14, 17-18 + RECONNECTING state ────────────────────────────────


class TestConnectAllResponsiveness:
    def test_startup_runs_off_the_gui_thread(self, qapp):
        """Items 13+14: blocking connects run in the worker; GUI stays live."""
        gui_thread = threading.get_ident()
        runtime = FakeRuntime(delay_s=0.05)
        wall = _make_wall(qapp, n_cameras=8, runtime=runtime)
        try:
            wall._on_connect_clicked()
            # The click returns while the worker is still connecting, with
            # the button already reflecting progress (GUI never blocked).
            assert wall._wants_startup()
            assert wall._connect_button.text() == "STARTING..."
            assert not wall._connect_button.isEnabled()
            QApplication.processEvents()  # responsive mid-startup
            _wait_startup_done(wall)
            assert runtime.start_call_threads, "no camera was started"
            assert all(t != gui_thread for t in runtime.start_call_threads)
            assert len(runtime.start_camera_calls) == 8
        finally:
            _close_wall(wall)

    def test_geometry_unchanged_by_startup(self, qapp):
        """Item 18: connect + results never move or resize the 3x3 wall."""
        runtime = FakeRuntime()
        wall = _make_wall(qapp, n_cameras=8, runtime=runtime)
        try:
            wall.show()
            wall.resize(1600, 900)
            _pump()
            wall._refit_wall()
            _pump()
            before_pos = [wall._grid_layout.getItemPosition(i)[:2] for i in range(9)]
            before_feeds = [
                (t._image_widget.width(), t._image_widget.height()) for t in wall._tiles
            ]
            wall._on_connect_clicked()
            _wait_startup_done(wall)
            _emit_all_results(runtime)
            wall._poll_stats()
            _pump()
            assert [wall._grid_layout.getItemPosition(i)[:2] for i in range(9)] == before_pos
            assert [
                (t._image_widget.width(), t._image_widget.height()) for t in wall._tiles
            ] == before_feeds
        finally:
            _close_wall(wall)

    def test_theme_switch_keeps_started_wall(self, qapp):
        """Item 17: theme changes keep positions, cameras and button state."""
        from thermal_monitor.ui.theme import ThemeManager

        runtime = FakeRuntime()
        wall = _make_wall(qapp, n_cameras=8, runtime=runtime)
        try:
            wall._on_connect_clicked()
            _wait_startup_done(wall)
            _emit_all_results(runtime)
            _pump()
            before = [(t.camera_id, t.slot_index) for t in wall._tiles]
            manager = ThemeManager(None)
            for name in ("high_contrast", "industrial_dark", "blue_engineering"):
                manager.set_theme(name)
                manager.apply_and_refresh(qapp)
            _pump()
            assert [(t.camera_id, t.slot_index) for t in wall._tiles] == before
            assert [t.state for t in wall._tiles] == [LiveTileState.RUNNING] * 8
            row, col, _, _ = wall._grid_layout.getItemPosition(8)
            assert (row, col) == (2, 2)
        finally:
            _close_wall(wall)

    def test_worker_reconnecting_surfaces_on_tile(self, qapp):
        """States: driver RECONNECTING mirrors to the tile; results heal it."""
        runtime = FakeRuntime()
        wall = _make_wall(qapp, n_cameras=8, runtime=runtime)
        try:
            wall._on_connect_clicked()
            _wait_startup_done(wall)
            _emit_all_results(runtime)
            _pump()
            runtime.worker_state = "reconnecting"
            wall._poll_stats()
            _pump()
            assert all(t.state is LiveTileState.RECONNECTING for t in wall._tiles)
            assert "DEGRADED" in wall._summary_label.text()
            assert "Reconnecting: 8" in wall._stats_panel._sys_cams.text()
            # Recovery: worker streaming again, next result returns to LIVE
            # with the last images kept (never cleared, never moved).
            runtime.worker_state = "streaming"
            wall._poll_stats()
            _pump()
            assert all(t.state is LiveTileState.STARTING for t in wall._tiles)
            _emit_all_results(runtime, sequence=2)
            _pump()
            assert all(t.state is LiveTileState.RUNNING for t in wall._tiles)
        finally:
            _close_wall(wall)

    def test_reconnecting_vocab_in_theme(self, qapp):
        """States: reconnecting is styled centrally for every theme."""
        from thermal_monitor.ui.theme import BUILTIN_THEMES, build_stylesheet
        from thermal_monitor.ui.theme.tokens import STATUS_VALUES, TILE_STATES

        assert "reconnecting" in STATUS_VALUES
        assert "reconnecting" in TILE_STATES
        for name, definition in BUILTIN_THEMES.items():
            sheet = build_stylesheet(definition)
            assert '[status="reconnecting"]' in sheet, name
            assert '[tileState="reconnecting"]' in sheet, name
