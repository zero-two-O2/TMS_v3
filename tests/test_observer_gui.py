"""Tests for Stage 7I: Multi-camera Observer GUI.

Covers the GUI-side path for N cameras (1..8):

    CameraRuntimeService
        -> per-camera ObserverService.result_ready (Qt signal bridge)
        -> per-camera CameraTile (GUI thread)

All tests use synthetic frames and a fake runtime service; no TV46L hardware
or HALCON is required.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from PyQt6.QtCore import QObject, pyqtSlot
from PyQt6.QtWidgets import QApplication, QWidget

from thermal_monitor.core.frame import (
    Frame,
    FrameDescriptor,
    FramePayload,
    StreamMetadata,
    SyncInfo,
    SyncStatus,
)
from thermal_monitor.core.models import (
    AnalysisConfig,
    AnalysisResult,
    CameraConfig,
    CameraIdentity,
    TemperatureUnit,
)
from thermal_monitor.processing import ProcessingResult
from thermal_monitor.processing.alarms import AlarmEvaluationResult
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.observer import ObserverService
from thermal_monitor.services.runtime import CameraRuntimeError
from thermal_monitor.ui.modes.observer import (
    CameraTile,
    CameraTileState,
    LiveThermalWidget,
    ObserverModeWidget,
    _columns_for_camera_count,
)


# ─── Helpers ────────────────────────────────────────────────────────────────────


def unique_camera(prefix: str) -> str:
    """Return a unique camera id to avoid shared-memory collisions."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def make_frame(camera_id: str, sequence: int, width: int = 16, height: int = 16) -> Frame:
    """Create a test frame with a known thermal payload pattern."""
    thermal = np.arange(sequence, sequence + width * height, dtype=np.uint16).reshape(height, width)
    thermal.setflags(write=False)

    thermal_meta = StreamMetadata(
        present=True,
        width=width,
        height=height,
        pixel_format="IR_Data",
        dtype="uint16",
        byte_count=thermal.nbytes,
        sequence=sequence * 1000,
        timestamp=1000.0 + sequence * 0.111,
        monotonic_timestamp=100.0 + sequence * 0.111,
        hardware_timestamp=1000.0 + sequence * 0.111,
    )
    visible_meta = StreamMetadata(present=False)
    sync = SyncInfo(status=SyncStatus.MISSING_VISIBLE)

    descriptor = FrameDescriptor(
        camera_id=camera_id,
        sequence=sequence,
        timestamp=1000.0 + sequence * 0.111,
        monotonic_timestamp=100.0 + sequence * 0.111,
        thermal=thermal_meta,
        visible=visible_meta,
        sync=sync,
        metadata={"grab_duration_s": 0.001},
    )
    return Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal, visible=None))


class FixedCalibrationProvider:
    """Calibration provider returning a known LUT: temp == raw value."""

    def __init__(self, lut: np.ndarray | None = None) -> None:
        if lut is None:
            lut = np.arange(65536, dtype=np.float32)
        self._lut = lut

    def get_calibration(self, camera_id: str) -> np.ndarray:
        return self._lut


def make_analysis_result(
    camera_id: str,
    sequence: int,
    *,
    overall_min: float | None = 20.0,
    overall_max: float | None = 30.0,
    overall_mean: float | None = 25.0,
    unit: TemperatureUnit = TemperatureUnit.CELSIUS,
) -> AnalysisResult:
    return AnalysisResult(
        camera_id=camera_id,
        frame_sequence=sequence,
        frame_timestamp=1000.0 + sequence * 0.111,
        roi_results={},
        overall_min=overall_min,
        overall_max=overall_max,
        overall_mean=overall_mean,
        unit=unit,
        processing_time_ms=2.5,
    )


def make_processing_result(
    camera_id: str,
    sequence: int,
    *,
    temperature_image: np.ndarray | None = None,
    alarm_result: AlarmEvaluationResult | None = None,
    analysis_result: AnalysisResult | None = None,
    frame: Frame | None = None,
    processing_time_ms: float = 2.5,
) -> ProcessingResult:
    frame = frame or make_frame(camera_id, sequence)
    if analysis_result is None:
        analysis_result = make_analysis_result(camera_id, sequence)
    return ProcessingResult(
        frame=frame,
        analysis_result=analysis_result,
        alarm_result=alarm_result,
        processing_time_ms=processing_time_ms,
        temperature_image=temperature_image,
    )


def add_camera_config(
    config_service: ConfigurationService,
    camera_id: str,
    *,
    enabled: bool = True,
    serial: str | None = None,
    name: str | None = None,
) -> CameraConfig:
    """Add an enabled/disabled application camera config."""
    cfg = CameraConfig(
        identity=CameraIdentity(
            camera_id=camera_id,
            serial_number=serial or f"SN_{camera_id}",
            model="TV46L-1-26010003@9Hz",
            vendor="Fluke Process Instruments",
        ),
        name=name or camera_id,
        enabled=enabled,
    )
    config_service.set_camera_config(cfg)
    return cfg


class FakeRuntimeService:
    """Synthetic CameraRuntimeService for GUI tests (no hardware/HALCON).

    Uses real (unstarted) ObserverService objects so the Qt queued signal
    bridge works exactly as in production; the GUI drives results by emitting
    on the service signal.
    """

    def __init__(self) -> None:
        self._running: dict[str, CameraConfig] = {}
        self._observers: dict[str, ObserverService] = {}
        self._start_failures: set[str] = set()
        self.start_camera_calls: list[str] = []
        self.start_observer_calls: list[str] = []
        self.stop_camera_calls: list[str] = []

    def fail_to_start(self, camera_id: str) -> None:
        self._start_failures.add(camera_id)

    def start_camera(self, config: CameraConfig, *, timeout: float | None = None) -> str:
        camera_id = config.identity.camera_id
        self.start_camera_calls.append(camera_id)
        if camera_id in self._start_failures:
            raise CameraRuntimeError(f"Acquisition failed for camera {camera_id}")
        self._running[camera_id] = config
        return camera_id

    def start_observer(
        self,
        camera_id: str,
        analysis_config: AnalysisConfig | None,
        *,
        calibration_provider=None,
    ) -> ObserverService:
        if analysis_config is None:
            raise ValueError("analysis_config is required")
        self.start_observer_calls.append(camera_id)
        if camera_id in self._start_failures:
            raise CameraRuntimeError(f"Observer failed for camera {camera_id}")
        observer = ObserverService()
        self._observers[camera_id] = observer
        return observer

    def stop_camera(self, camera_id: str, *, timeout: float = 5.0) -> None:
        self.stop_camera_calls.append(camera_id)
        self._running.pop(camera_id, None)
        self._observers.pop(camera_id, None)

    def stop_observer(self, camera_id: str) -> None:
        self._observers.pop(camera_id, None)

    def shutdown(self, *, timeout: float = 5.0) -> None:
        self._running.clear()
        self._observers.clear()

    def is_camera_running(self, camera_id: str) -> bool:
        return camera_id in self._running

    def is_observer_running(self, camera_id: str) -> bool:
        return camera_id in self._observers

    def running_camera_ids(self) -> list[str]:
        return list(self._running.keys())

    def observer_service(self, camera_id: str) -> ObserverService | None:
        return self._observers.get(camera_id)

    def camera_stats(self, camera_id: str):
        return None


@pytest.fixture(scope="module")
def qapp():
    """Shared offscreen QApplication for the GUI tests."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def make_widget(runtime_service: FakeRuntimeService, *camera_ids: str) -> ObserverModeWidget:
    """Build a widget with the given enabled cameras pre-configured."""
    config = ConfigurationService()
    for cid in camera_ids:
        add_camera_config(config, cid)
    return ObserverModeWidget(
        ModeService(), config, runtime_service=runtime_service
    )


# ─── Grid policy ─────────────────────────────────────────────────────────────

class TestGridPolicy:
    def test_column_policy(self):
        assert _columns_for_camera_count(0) == 1
        assert _columns_for_camera_count(1) == 1
        assert _columns_for_camera_count(2) == 2
        assert _columns_for_camera_count(3) == 2
        assert _columns_for_camera_count(4) == 2
        assert _columns_for_camera_count(5) == 3
        assert _columns_for_camera_count(6) == 3
        assert _columns_for_camera_count(8) == 4


# ─── Required tests: camera counts ───────────────────────────────────────────

class TestObserverDisplaysCounts:
    def test_observer_displays_one_camera(self, qapp):
        rt = FakeRuntimeService()
        widget = make_widget(rt, "cam_1")
        widget.on_mode_activated()
        qapp.processEvents()
        assert len(widget._tiles) == 1
        assert "cam_1" in widget._tiles
        widget.close()

    def test_observer_displays_two_cameras(self, qapp):
        rt = FakeRuntimeService()
        widget = make_widget(rt, "cam_1", "cam_2")
        widget.on_mode_activated()
        qapp.processEvents()
        assert len(widget._tiles) == 2
        widget.close()

    def test_observer_displays_four_cameras(self, qapp):
        rt = FakeRuntimeService()
        widget = make_widget(rt, *[f"cam_{i}" for i in range(4)])
        widget.on_mode_activated()
        qapp.processEvents()
        assert len(widget._tiles) == 4
        widget.close()

    def test_observer_displays_eight_cameras(self, qapp):
        rt = FakeRuntimeService()
        widget = make_widget(rt, *[f"cam_{i}" for i in range(8)])
        widget.on_mode_activated()
        qapp.processEvents()
        assert len(widget._tiles) == 8
        # 8 cameras -> 4 columns
        assert _columns_for_camera_count(8) == 4
        widget.close()


# ─── Required tests: isolation & lifecycle ───────────────────────────────────

class TestCameraIsolation:
    def test_each_camera_tile_receives_only_its_camera_results(self, qapp):
        rt = FakeRuntimeService()
        widget = make_widget(rt, "cam_a", "cam_b")
        widget.on_mode_activated()
        qapp.processEvents()

        obs_a = rt.observer_service("cam_a")
        obs_b = rt.observer_service("cam_b")
        obs_a.result_ready.emit(make_processing_result("cam_a", 1))
        qapp.processEvents()

        assert widget._tiles["cam_a"].frames_received == 1
        assert widget._tiles["cam_b"].frames_received == 0

        obs_b.result_ready.emit(make_processing_result("cam_b", 2))
        qapp.processEvents()
        assert widget._tiles["cam_b"].frames_received == 1
        assert widget._tiles["cam_a"].frames_received == 1
        widget.close()

    def test_camera_one_failure_does_not_stop_camera_two(self, qapp):
        rt = FakeRuntimeService()
        rt.fail_to_start("cam_b")
        widget = make_widget(rt, "cam_a", "cam_b")
        widget.on_mode_activated()
        qapp.processEvents()

        # Camera B failed; camera A still has a live tile.
        assert widget._tiles["cam_b"].state is CameraTileState.ERROR
        obs_a = rt.observer_service("cam_a")
        assert obs_a is not None
        for seq in range(3):
            obs_a.result_ready.emit(make_processing_result("cam_a", seq))
        qapp.processEvents()
        assert widget._tiles["cam_a"].frames_received == 3
        assert widget._tiles["cam_a"].state is CameraTileState.RUNNING
        widget.close()

    def test_camera_two_failure_does_not_stop_camera_one(self, qapp):
        rt = FakeRuntimeService()
        rt.fail_to_start("cam_a")
        widget = make_widget(rt, "cam_a", "cam_b")
        widget.on_mode_activated()
        qapp.processEvents()

        assert widget._tiles["cam_a"].state is CameraTileState.ERROR
        obs_b = rt.observer_service("cam_b")
        for seq in range(3):
            obs_b.result_ready.emit(make_processing_result("cam_b", seq))
        qapp.processEvents()
        assert widget._tiles["cam_b"].frames_received == 3
        assert widget._tiles["cam_b"].state is CameraTileState.RUNNING
        widget.close()

    def test_camera_error_isolated_to_tile(self, qapp):
        rt = FakeRuntimeService()
        rt.fail_to_start("cam_b")
        widget = make_widget(rt, "cam_a", "cam_b")
        widget.on_mode_activated()
        qapp.processEvents()
        assert widget._tiles["cam_b"].error_message is not None
        assert widget._tiles["cam_a"].error_message is None
        widget.close()


class TestCameraLifecycle:
    def test_disabled_camera_is_not_started(self, qapp):
        rt = FakeRuntimeService()
        config = ConfigurationService()
        add_camera_config(config, "cam_on", enabled=True)
        add_camera_config(config, "cam_off", enabled=False)
        widget = ObserverModeWidget(ModeService(), config, runtime_service=rt)
        widget.on_mode_activated()
        qapp.processEvents()
        assert "cam_on" in rt.start_camera_calls
        assert "cam_off" not in rt.start_camera_calls
        assert "cam_off" not in widget._tiles
        widget.close()

    def test_enabled_cameras_are_started(self, qapp):
        rt = FakeRuntimeService()
        widget = make_widget(rt, "cam_1", "cam_2", "cam_3")
        widget.on_mode_activated()
        qapp.processEvents()
        assert set(rt.start_camera_calls) == {"cam_1", "cam_2", "cam_3"}
        assert set(rt.start_observer_calls) == {"cam_1", "cam_2", "cam_3"}
        widget.close()

    def test_observer_empty_state(self, qapp):
        rt = FakeRuntimeService()
        config = ConfigurationService()  # no cameras
        widget = ObserverModeWidget(ModeService(), config, runtime_service=rt)
        widget.on_mode_activated()
        qapp.processEvents()
        assert len(widget._tiles) == 0
        assert widget._empty_label.isVisibleTo(widget)
        assert "Running: 0" in widget._summary_label.text()
        widget.close()

    def test_observer_cleanup_removes_tiles(self, qapp):
        rt = FakeRuntimeService()
        widget = make_widget(rt, "cam_1", "cam_2")
        widget.on_mode_activated()
        qapp.processEvents()
        assert len(widget._tiles) == 2
        widget.on_mode_deactivated()
        qapp.processEvents()
        assert len(widget._tiles) == 0
        assert widget._empty_label.isVisibleTo(widget)
        widget.close()

    def test_observer_mode_exit_stops_runtime(self, qapp):
        rt = FakeRuntimeService()
        widget = make_widget(rt, "cam_1", "cam_2")
        widget.on_mode_activated()
        qapp.processEvents()
        widget.on_mode_deactivated()
        qapp.processEvents()
        assert set(rt.stop_camera_calls) == {"cam_1", "cam_2"}
        widget.close()


# ─── Required tests: tile updates ────────────────────────────────────────────

class TestCameraTileUpdates:
    def test_camera_tile_updates_temperature_image(self, qapp):
        tile = CameraTile("cam_img")
        temp = np.arange(256, dtype=np.float32).reshape(16, 16)
        tile.on_result(make_processing_result("cam_img", 1, temperature_image=temp))
        assert tile._image_widget.display_array is not None
        assert tile._image_widget.display_array.shape == (16, 16)
        assert tile._image_widget.display_array.dtype == np.uint8

    def test_camera_tile_copies_temperature_buffer(self, qapp):
        tile = CameraTile("cam_copy")
        temp = (np.arange(256, dtype=np.float32).reshape(16, 16)) * 0.5
        tile.on_result(make_processing_result("cam_copy", 1, temperature_image=temp))
        disp = tile._image_widget.display_array
        assert disp is not None
        assert not np.shares_memory(disp, temp)
        pixel_before = int(disp[0, 0])
        temp[0, 0] = 1e6
        assert int(disp[0, 0]) == pixel_before

    def test_camera_tile_updates_fps(self, qapp):
        tile = CameraTile("cam_fps")
        tile.set_state(CameraTileState.RUNNING)
        tile.set_stats(fps=9.5, processed_frames=10, avg_processing_ms=3.2, dropped_frames=0)
        assert tile._fps_label.text() == "9.5"
        assert "10" in tile._proc_label.text()
        assert "3.2" in tile._proc_label.text()

    def test_camera_tile_updates_sequence(self, qapp):
        tile = CameraTile("cam_seq")
        tile.on_result(make_processing_result("cam_seq", 42))
        assert tile.latest_sequence == 42
        assert tile._sequence_label.text() == "42"


# ─── Legacy single-observer path (display-only) ─────────────────────────────

class TestLegacySingleObserver:
    def test_legacy_single_observer_binds_one_tile(self, qapp):
        service = ObserverService()
        config = ConfigurationService()
        add_camera_config(config, "cam_legacy")
        widget = ObserverModeWidget(ModeService(), config, observer_service=service)
        widget.on_mode_activated()
        qapp.processEvents()
        assert len(widget._tiles) == 1
        assert "cam_legacy" in widget._tiles
        service.result_ready.emit(make_processing_result("cam_legacy", 1))
        qapp.processEvents()
        assert widget._tiles["cam_legacy"].frames_received == 1
        widget.close()

    def test_result_reaches_gui_via_signal_bridge(self, qapp):
        service = ObserverService()
        config = ConfigurationService()
        add_camera_config(config, "cam_bridge")
        widget = ObserverModeWidget(ModeService(), config, observer_service=service)
        widget.on_mode_activated()
        qapp.processEvents()
        result = make_processing_result("cam_bridge", 3)
        service.result_ready.emit(result)
        qapp.processEvents()
        assert widget._tiles["cam_bridge"].latest_sequence == 3

    def test_activate_without_cameras_shows_status(self, qapp):
        service = ObserverService()
        widget = ObserverModeWidget(ModeService(), ConfigurationService(), observer_service=service)
        widget.on_mode_activated()
        assert "No configured cameras" in widget._summary_label.text()


# ─── Reused LiveThermalWidget contract ───────────────────────────────────────

class TestLiveThermalWidgetReuse:
    def test_missing_temperature_image_handled(self, qapp):
        w = LiveThermalWidget()
        frame = make_frame("cam_none", 1)
        frame = Frame(descriptor=frame.descriptor, payload=FramePayload(thermal=None, visible=None))
        w.set_frame(None, frame)
        assert w.display_array is None

    def test_live_thermal_widget_clear(self, qapp):
        w = LiveThermalWidget()
        w.set_frame(np.zeros((4, 4), dtype=np.float32), make_frame("cam_clear", 0))
        assert w.display_array is not None
        w.clear()
        assert w.display_array is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
