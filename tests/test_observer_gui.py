"""Tests for Stage 7E: Observer GUI integration.

Covers the complete GUI-side path:

    ProcessingConsumer (processing thread)
        -> ObserverService.result_ready (Qt signal bridge)
        -> ObserverModeWidget (GUI thread)

All tests use synthetic frames; no TV46L hardware or HALCON is required.
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

from thermal_monitor.camera.shm import create_ring_buffer_and_publisher
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
    TemperatureUnit,
)
from thermal_monitor.processing import ProcessingResult
from thermal_monitor.processing.alarms import AlarmEvaluationResult
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.observer import ObserverService
from thermal_monitor.ui.modes.observer import LiveThermalWidget, ObserverModeWidget


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


@pytest.fixture(scope="module")
def qapp():
    """Shared offscreen QApplication for the GUI tests."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def widget(qapp):
    """A widget wired to a fresh ObserverService."""
    service = ObserverService()
    w = ObserverModeWidget(ModeService(), ConfigurationService(), service)
    yield w
    w.close()


# ─── Widget construction and signal bridge ────────────────────────────────────

class TestObserverWidgetBasics:
    """Widget construction and the Qt signal bridge."""

    def test_observer_widget_created(self, qapp):
        """The observer widget can be constructed and is a QWidget."""
        widget = ObserverModeWidget(ModeService(), ConfigurationService(), ObserverService())
        assert isinstance(widget, QWidget)
        widget.close()

    def test_result_reaches_gui_via_signal_bridge(self, widget, qapp):
        """A result emitted by the service signal reaches the widget's slot."""
        result = make_processing_result("cam_bridge", 3)
        widget._observer_service.result_ready.emit(result)
        qapp.processEvents()
        assert widget._latest_result is result

    def test_consumer_thread_bridges_result_to_gui_thread(self, qapp):
        """ProcessingConsumer thread -> service signal -> GUI thread (queued)."""
        camera_id = unique_camera("cam_bridge_thread")
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=8)
        service = ObserverService()

        results: list = []
        threads: list = []

        class Spy(QObject):
            @pyqtSlot(object)
            def on_result(self, result):
                results.append(result)
                threads.append(threading.current_thread().name)

        spy = Spy()
        service.result_ready.connect(spy.on_result)
        main_thread_name = threading.current_thread().name

        try:
            service.start(
                camera_id,
                analysis_config=AnalysisConfig(camera_id=camera_id),
                calibration_provider=FixedCalibrationProvider(),
                ring_depth=8,
                thermal_width=16,
                thermal_height=16,
            )
            assert service.is_running

            publisher.publish(make_frame(camera_id, 0))
            deadline = time.monotonic() + 5.0
            while not results and time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(0.01)

            assert results, "no result was bridged to the GUI thread"
            assert all(t == main_thread_name for t in threads)
            assert results[0].frame.descriptor.sequence == 0
            assert results[0].temperature_image is not None
        finally:
            service.stop()
            ring.close()

    def test_activate_without_cameras_shows_status(self, widget, qapp):
        """Activating observer mode without cameras shows a status message."""
        widget.on_mode_activated()
        assert "No configured cameras" in widget._status_label.text()

    def test_deactivate_stops_service(self, widget, qapp):
        """Leaving observer mode stops the monitoring service."""
        assert not widget._observer_service.is_running
        widget.on_mode_deactivated()
        assert not widget._observer_service.is_running


# ─── Frame information display ────────────────────────────────────────────────

class TestFrameDisplay:
    def test_camera_id_displayed(self, widget, qapp):
        widget._observer_service.result_ready.emit(make_processing_result("cam_alpha", 1))
        qapp.processEvents()
        assert widget._camera_label.text() == "cam_alpha"

    def test_sequence_displayed(self, widget, qapp):
        widget._observer_service.result_ready.emit(make_processing_result("cam_seq", 42))
        qapp.processEvents()
        assert widget._sequence_label.text() == "42"

    def test_timestamp_displayed(self, widget, qapp):
        widget._observer_service.result_ready.emit(make_processing_result("cam_ts", 1))
        qapp.processEvents()
        assert widget._timestamp_label.text() == "1000.111"

    def test_repeated_callbacks_no_crash(self, widget, qapp):
        """Many back-to-back results do not crash the widget."""
        for seq in range(200):
            widget._observer_service.result_ready.emit(make_processing_result("cam_repeat", seq))
        qapp.processEvents()
        assert widget._frames_label.text() == "200"
        assert widget._sequence_label.text() == "199"
        assert widget._latest_result.frame.descriptor.sequence == 199


# ─── Image display ────────────────────────────────────────────────────────────

class TestImageDisplay:
    def test_temperature_image_displayed(self, widget, qapp):
        temp = np.arange(256, dtype=np.float32).reshape(16, 16)
        widget._observer_service.result_ready.emit(
            make_processing_result("cam_img", 1, temperature_image=temp)
        )
        qapp.processEvents()
        assert widget._image_widget.display_array is not None
        assert widget._image_widget.display_array.shape == (16, 16)
        assert widget._image_widget.display_array.dtype == np.uint8

    def test_raw_thermal_displayed_without_temperature_image(self, widget, qapp):
        """Without a temperature image the raw uint16 thermal is displayed."""
        widget._observer_service.result_ready.emit(
            make_processing_result("cam_raw", 1, temperature_image=None)
        )
        qapp.processEvents()
        assert widget._image_widget.display_array is not None
        assert widget._image_widget.display_array.shape == (16, 16)

    def test_missing_temperature_image_handled(self, widget, qapp):
        """A frame with no thermal payload shows a placeholder without crashing."""
        frame = make_frame("cam_none", 1)
        frame = Frame(
            descriptor=frame.descriptor,
            payload=FramePayload(thermal=None, visible=None),
        )
        widget._observer_service.result_ready.emit(
            make_processing_result("cam_none", 1, temperature_image=None, frame=frame)
        )
        qapp.processEvents()
        assert widget._image_widget.display_array is None

    def test_temperature_copied_before_qimage(self, widget, qapp):
        """The display buffer is a copy; mutating the source cannot affect it."""
        temp = (np.arange(256, dtype=np.float32).reshape(16, 16)) * 0.5
        widget._observer_service.result_ready.emit(
            make_processing_result("cam_copy", 1, temperature_image=temp)
        )
        qapp.processEvents()

        disp = widget._image_widget.display_array
        assert disp is not None
        assert not np.shares_memory(disp, temp)

        pixel_before = int(disp[0, 0])
        temp[0, 0] = 1e6  # mutate the source after display
        assert int(disp[0, 0]) == pixel_before

    def test_live_thermal_widget_clear(self, qapp):
        """LiveThermalWidget.clear resets the display."""
        w = LiveThermalWidget()
        w.set_frame(np.zeros((4, 4), dtype=np.float32), make_frame("cam_clear", 0))
        assert w.display_array is not None
        w.clear()
        assert w.display_array is None


# ─── Temperature display ──────────────────────────────────────────────────────

class TestTemperatureDisplay:
    def test_temperature_displayed(self, widget, qapp):
        result = make_processing_result(
            "cam_temp",
            1,
            analysis_result=make_analysis_result(
                "cam_temp", 1, overall_min=10.0, overall_max=30.0, overall_mean=20.0
            ),
        )
        widget._observer_service.result_ready.emit(result)
        qapp.processEvents()
        assert widget._temp_label.text() == "°C"
        assert widget._temp_min.text() == "10.0"
        assert widget._temp_max.text() == "30.0"
        assert widget._temp_mean.text() == "20.0"

    def test_missing_analysis_result_handled(self, widget, qapp):
        frame = make_frame("cam_noar", 1)
        result = ProcessingResult(
            frame=frame,
            analysis_result=None,
            alarm_result=None,
            processing_time_ms=0.0,
            temperature_image=None,
        )
        widget._observer_service.result_ready.emit(result)
        qapp.processEvents()
        assert widget._temp_min.text() == "—"
        assert widget._temp_max.text() == "—"
        assert widget._temp_mean.text() == "—"

    def test_empty_analysis_result_handled(self, widget, qapp):
        result = make_processing_result(
            "cam_empty",
            1,
            analysis_result=make_analysis_result(
                "cam_empty", 1, overall_min=None, overall_max=None, overall_mean=None
            ),
        )
        widget._observer_service.result_ready.emit(result)
        qapp.processEvents()
        assert widget._temp_min.text() == "No thermal data"


# ─── Alarm display ────────────────────────────────────────────────────────────

class TestAlarmDisplay:
    def test_alarm_state_displayed(self, widget, qapp):
        alarm_result = AlarmEvaluationResult(
            frame_sequence=1,
            frame_timestamp=1000.0,
            camera_id="cam_alarm",
            events=(),
            active_alarms=("rule_hi",),
            cleared_alarms=(),
        )
        widget._observer_service.result_ready.emit(
            make_processing_result("cam_alarm", 1, alarm_result=alarm_result)
        )
        qapp.processEvents()
        assert widget._alarm_count_label.text() == "1"
        assert "rule_hi" in widget._alarm_label.text()

    def test_no_active_alarms(self, widget, qapp):
        alarm_result = AlarmEvaluationResult(
            frame_sequence=1,
            frame_timestamp=1000.0,
            camera_id="cam_ok",
            events=(),
            active_alarms=(),
            cleared_alarms=(),
        )
        widget._observer_service.result_ready.emit(
            make_processing_result("cam_ok", 1, alarm_result=alarm_result)
        )
        qapp.processEvents()
        assert widget._alarm_count_label.text() == "0"
        assert "No active alarms" in widget._alarm_label.text()

    def test_missing_alarm_result_handled(self, widget, qapp):
        widget._observer_service.result_ready.emit(
            make_processing_result("cam_noalarm", 1, alarm_result=None)
        )
        qapp.processEvents()
        assert widget._alarm_count_label.text() == "0"
        assert "No alarm evaluation" in widget._alarm_label.text()


# ─── Processing status ────────────────────────────────────────────────────────

class TestProcessingStatus:
    def test_processing_status_updates(self, widget, qapp):
        for seq in range(3):
            widget._observer_service.result_ready.emit(
                make_processing_result("cam_proc", seq, processing_time_ms=1.5)
            )
        qapp.processEvents()
        assert widget._frames_label.text() == "3"
        assert "1.5 ms" in widget._proc_time_label.text()

    def test_status_label_updates_to_live(self, widget, qapp):
        widget._observer_service.result_ready.emit(make_processing_result("cam_live", 7))
        qapp.processEvents()
        assert "cam_live" in widget._status_label.text()
        assert "seq 7" in widget._status_label.text()


# ─── Shutdown ─────────────────────────────────────────────────────────────────

class TestShutdown:
    def test_shutdown_leaves_no_threads(self, qapp):
        """Stopping the service leaves no processing consumer threads behind."""
        camera_id = unique_camera("cam_obs_shutdown")
        ring, publisher = create_ring_buffer_and_publisher(camera_id, width=16, height=16, depth=8)
        service = ObserverService()

        try:
            service.start(
                camera_id,
                analysis_config=AnalysisConfig(camera_id=camera_id),
                calibration_provider=FixedCalibrationProvider(),
                ring_depth=8,
                thermal_width=16,
                thermal_height=16,
            )
            assert service.is_running

            publisher.publish(make_frame(camera_id, 0))
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                qapp.processEvents()
                stats = service.stats()
                if stats is not None and stats.frames_processed >= 1:
                    break
                time.sleep(0.01)
            assert service.stats().frames_processed >= 1
        finally:
            service.stop()
            ring.close()

        assert not service.is_running
        names = [t.name for t in threading.enumerate()]
        assert not any(f"ProcessingConsumer-{camera_id}" in n for n in names)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])