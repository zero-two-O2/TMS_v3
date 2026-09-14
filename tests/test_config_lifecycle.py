"""
Tests for Configuration Mode camera lifecycle (Part 3).

Covers the explicit state machine, session generations, stale-frame
gating, safe camera switching, disconnect semantics and stress switching
— all with a fake runtime (no hardware, no child processes).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

from unittest.mock import patch

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication

import thermal_monitor.camera.source  # noqa: F401  (init camera package first)
from thermal_monitor.core.models import (
    AnalysisConfig,
    CameraConfig,
    CameraConnectionState,
    CameraIdentity,
)
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.lifecycle import (
    CameraSession,
    TeardownTimings,
    allowed_transition,
    can_connect,
    can_disconnect,
    can_start,
    can_stop,
    is_transitional,
)
from thermal_monitor.services.mode import ModeService
from thermal_monitor.ui.modes.thermal_render_worker import (
    RenderRequest,
    ThermalRenderWorker,
)
from thermal_monitor.ui.modes.vl_render_worker import VlRenderWorker, VlRenderRequest
from thermal_monitor.ui.windows.configuration_window import ConfigurationModeWidget


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def test_lifecycle_happy_path_transitions() -> None:
    path = [
        CameraConnectionState.DISCONNECTED,
        CameraConnectionState.CONNECTING,
        CameraConnectionState.CONNECTED,
        CameraConnectionState.STARTING,
        CameraConnectionState.ACQUIRING,
        CameraConnectionState.STOPPING,
        CameraConnectionState.CONNECTED,
        CameraConnectionState.DISCONNECTING,
        CameraConnectionState.DISCONNECTED,
    ]
    for current, target in zip(path, path[1:]):
        assert allowed_transition(current, target), f"{current} -> {target}"


def test_lifecycle_failure_path() -> None:
    assert allowed_transition(
        CameraConnectionState.ACQUIRING, CameraConnectionState.ERROR
    )
    assert allowed_transition(
        CameraConnectionState.ERROR, CameraConnectionState.DISCONNECTING
    )
    assert allowed_transition(
        CameraConnectionState.DISCONNECTING, CameraConnectionState.DISCONNECTED
    )


def test_lifecycle_illegal_transitions_rejected() -> None:
    # Button state alone must never drive these jumps.
    assert not allowed_transition(
        CameraConnectionState.DISCONNECTED, CameraConnectionState.ACQUIRING
    )
    assert not allowed_transition(
        CameraConnectionState.DISCONNECTED, CameraConnectionState.STARTING
    )
    assert not allowed_transition(
        CameraConnectionState.ACQUIRING, CameraConnectionState.CONNECTING
    )
    assert not allowed_transition(
        CameraConnectionState.DISCONNECTED, CameraConnectionState.DISCONNECTING
    )


def test_lifecycle_disconnect_accepted_while_starting_or_stopping() -> None:
    # The GUI never requires hidden cleanup steps before switching.
    for state in (
        CameraConnectionState.CONNECTED,
        CameraConnectionState.ACQUIRING,
        CameraConnectionState.STARTING,
        CameraConnectionState.STOPPING,
        CameraConnectionState.ERROR,
    ):
        assert can_disconnect(state), state


def test_lifecycle_guards() -> None:
    assert can_connect(CameraConnectionState.DISCONNECTED)
    assert can_connect(CameraConnectionState.ERROR)
    assert not can_connect(CameraConnectionState.ACQUIRING)
    assert can_start(CameraConnectionState.CONNECTED)
    assert not can_start(CameraConnectionState.ACQUIRING)
    assert not can_start(CameraConnectionState.DISCONNECTED)
    assert can_stop(CameraConnectionState.ACQUIRING)
    assert can_stop(CameraConnectionState.STARTING)
    assert not can_stop(CameraConnectionState.CONNECTED)
    assert is_transitional(CameraConnectionState.CONNECTING)
    assert is_transitional(CameraConnectionState.STARTING)
    assert is_transitional(CameraConnectionState.STOPPING)
    assert is_transitional(CameraConnectionState.DISCONNECTING)
    assert not is_transitional(CameraConnectionState.ACQUIRING)
    assert not is_transitional(CameraConnectionState.DISCONNECTED)


def test_session_generation_invalidates_old_tokens() -> None:
    session = CameraSession()
    session.renew("camA")
    assert session.accepts("camA", session.generation)
    old_generation = session.generation
    session.renew("camB")
    assert not session.accepts("camA", old_generation)
    assert not session.accepts("camA", None)  # camera mismatch even untagged
    assert session.accepts("camB", session.generation)
    session.renew("camA")  # reconnect same camera: old epoch still dead
    assert not session.accepts("camA", old_generation)


def test_teardown_timings_summary_reports_everything() -> None:
    timings = TeardownTimings(camera_id="camA", generation=7, pid=1234)
    timings.observer_stopped = True
    timings.processing_stopped = True
    timings.shm_detached = True
    timings.process_exited = True
    timings.finish()
    summary = timings.summary()
    for token in ("camA", "7", "1234", "observer_stopped=True",
                  "processing_stopped=True", "shm_detached=True",
                  "process_exit=True", "total="):
        assert token in summary


# ---------------------------------------------------------------------------
# Render-session gating
# ---------------------------------------------------------------------------


def test_thermal_render_worker_drops_old_camera_after_session() -> None:
    worker = ThermalRenderWorker()
    worker.set_session("camA")
    worker.submit(RenderRequest(np.zeros((4, 4)), sequence=3, camera_id="camA"))
    assert worker._pending is not None
    worker.set_session("camB")
    assert worker._pending is None  # old pending discarded
    # Old camera requests are dropped even with higher sequences.
    worker.submit(RenderRequest(np.zeros((4, 4)), sequence=99, camera_id="camA"))
    assert worker._pending is None
    # New camera restarts at sequence 0 and is accepted.
    worker.submit(RenderRequest(np.zeros((4, 4)), sequence=0, camera_id="camB"))
    assert worker._pending is not None
    worker.stop()


def test_vl_render_worker_drops_old_camera_after_session() -> None:
    worker = VlRenderWorker()
    worker.set_session("camA")
    worker.submit(VlRenderRequest(yuyv=np.zeros((4, 8), dtype=np.uint8), sequence=5, camera_id="camA"))
    assert worker._pending is not None
    worker.set_session("camB")
    worker.submit(VlRenderRequest(yuyv=np.zeros((4, 8), dtype=np.uint8), sequence=50, camera_id="camA"))
    assert worker._pending is None
    worker.submit(VlRenderRequest(yuyv=np.zeros((4, 8), dtype=np.uint8), sequence=0, camera_id="camB"))
    assert worker._pending is not None
    worker.stop()


# ---------------------------------------------------------------------------
# Widget harness with a fake runtime
# ---------------------------------------------------------------------------


class FakeObserver(QObject):
    result_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self, camera_id: str) -> None:
        super().__init__()
        self.camera_id = camera_id
        self.stopped = False
        self._session_generation: int | None = None

    @property
    def is_running(self) -> bool:
        return not self.stopped

    def stats(self):
        return None

    def stop(self, timeout: float = 5.0) -> None:
        self.stopped = True


class FakeRuntime:
    """In-process stand-in for CameraRuntimeService (no SHM/processes)."""

    def __init__(self) -> None:
        self.running: set[str] = set()
        self.observers: dict[str, FakeObserver] = {}
        self.starts: list[str] = []
        self.stops: list[str] = []
        self.observer_starts: list[str] = []
        self.observer_stops: list[str] = []
        self.fail_connect: set[str] = set()
        self._child_state: dict[str, object] = {}

    def is_camera_running(self, camera_id: str) -> bool:
        return camera_id in self.running

    def is_observer_running(self, camera_id: str) -> bool:
        observer = self.observers.get(camera_id)
        return observer is not None and not observer.stopped

    def start_camera(self, config, *, timeout=None):
        camera_id = config.identity.camera_id
        if camera_id in self.fail_connect:
            raise RuntimeError(f"simulated connect failure for {camera_id}")
        self.running.add(camera_id)
        self.starts.append(camera_id)
        return camera_id

    def stop_camera(self, camera_id: str, *, timeout=None) -> None:
        self.stops.append(camera_id)
        self.running.discard(camera_id)
        observer = self.observers.pop(camera_id, None)
        if observer is not None:
            observer.stop()

    def start_observer(self, camera_id: str, analysis_config=None, **kwargs):
        self.observer_starts.append(camera_id)
        observer = FakeObserver(camera_id)
        self.observers[camera_id] = observer
        return observer

    def stop_observer(self, camera_id: str) -> None:
        self.observer_stops.append(camera_id)
        observer = self.observers.pop(camera_id, None)
        if observer is not None:
            observer.stop()

    def observer_service(self, camera_id: str):
        return self.observers.get(camera_id)

    def camera_stats(self, camera_id: str):
        return None

    def process_pid(self, camera_id: str):
        return None

    def process_handle(self, camera_id: str):
        return None

    def is_process_alive(self, camera_id: str):
        return None

    def running_camera_ids(self) -> list[str]:
        return sorted(self.running)

    def acquisition_child_state(self, camera_id: str):
        """Status-channel probe: STREAMING while running unless overridden."""
        from thermal_monitor.camera.model import AcquisitionState

        if camera_id not in self.running:
            return None
        return self._child_state.get(camera_id, AcquisitionState.STREAMING)


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(autouse=True)
def no_modal_dialogs():
    """Modal dialogs would block headless tests forever; stub them out."""
    with patch(
        "thermal_monitor.ui.windows.configuration_window.QMessageBox"
    ) as mock_box:
        mock_box.StandardButton.Save = 1
        mock_box.StandardButton.Discard = 2
        mock_box.StandardButton.Cancel = 3
        yield mock_box


@pytest.fixture(autouse=True)
def isolated_dock_settings(monkeypatch):
    """Widget close persists dock layout; keep it out of real user settings."""
    import thermal_monitor.ui.windows.configuration_window as config_window_module

    monkeypatch.setattr(config_window_module, "_DOCK_SETTINGS_ORG", "TMS-Test-Org")
    monkeypatch.setattr(config_window_module, "_DOCK_SETTINGS_APP", "TMS-Test-App")
    from PyQt6.QtCore import QSettings

    QSettings("TMS-Test-Org", "TMS-Test-App").clear()
    yield
    QSettings("TMS-Test-Org", "TMS-Test-App").clear()


def _close_widget(widget: ConfigurationModeWidget) -> None:
    """Hide the widget and stop its persistent render workers."""
    try:
        widget._image_widget.close()
    except Exception:
        pass
    try:
        widget._vl_widget.close()
    except Exception:
        pass
    widget.close()


def _make_widget(qapp, runtime, camera_ids=("camA", "camB")) -> ConfigurationModeWidget:
    service = ConfigurationService()
    for camera_id in camera_ids:
        service.set_camera_config(
            CameraConfig(
                identity=CameraIdentity(camera_id=camera_id, serial_number=f"SN-{camera_id}")
            )
        )
        service.set_analysis_config(AnalysisConfig(camera_id=camera_id))
    widget = ConfigurationModeWidget(
        config_service=service, mode_service=ModeService(), runtime_service=runtime
    )
    widget.show()
    qapp.processEvents()
    return widget


def _drain_background(widget: ConfigurationModeWidget, qapp, timeout_s: float = 10.0) -> bool:
    """Pump the GUI event loop until the background op completes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if widget._bg_tag is None:
            qapp.processEvents()
            time.sleep(0.02)
            qapp.processEvents()
            return True
        time.sleep(0.01)
    return widget._bg_tag is None


def _make_result(camera_id: str, sequence: int):
    frame = SimpleNamespace(
        descriptor=SimpleNamespace(
            camera_id=camera_id,
            sequence=sequence,
            timestamp=1234.5,
            monotonic_timestamp=time.perf_counter(),
            thermal=SimpleNamespace(sequence=sequence, width=640, height=480),
            visible=SimpleNamespace(sequence=sequence),
        ),
        payload=SimpleNamespace(
            thermal=np.zeros((480, 640), dtype=np.uint16),
            visible=None,
        ),
    )
    return SimpleNamespace(
        frame=frame,
        analysis_result=None,
        alarm_result=None,
        processing_time_ms=1.0,
        temperature_image=np.full((480, 640), 25.0, dtype=np.float64),
    )


def test_stale_camera_results_ignored(qapp) -> None:
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        widget._activate_camera("camA", connect=False)
        assert widget._selected_camera_id == "camA"
        accepted = _make_result("camA", 0)
        widget._on_processing_result(accepted)
        assert widget._latest_result is accepted
        stale = _make_result("camB", 0)
        widget._on_processing_result(stale)
        assert widget._latest_result is accepted
        assert widget._stale_results_dropped >= 1
    finally:
        _close_widget(widget)


def test_old_generation_signals_ignored(qapp) -> None:
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        widget._activate_camera("camA", connect=False)
        old_generation = widget._session.generation
        widget._session.renew("camA")  # simulate a reconnect epoch
        # A late queued signal from the previous epoch's observer must die
        # in the slot — no event-queue flush, no sleeps.
        stale_observer = FakeObserver("camA")
        stale_observer._session_generation = old_generation
        stale_observer.result_ready.connect(
            widget._on_processing_result, Qt.ConnectionType.QueuedConnection
        )
        stale_observer.result_ready.emit(_make_result("camA", 0))
        qapp.processEvents()
        qapp.processEvents()
        assert widget._latest_result is None
        assert widget._stale_results_dropped >= 1
    finally:
        _close_widget(widget)


def test_switch_camera_tears_down_old_pipeline(qapp) -> None:
    runtime = FakeRuntime()
    runtime.running.add("camA")
    widget = _make_widget(qapp, runtime)
    try:
        widget._activate_camera("camA", connect=False)
        # Simulate an acquiring camera A.
        runtime.running.add("camA")
        widget._set_lifecycle(CameraConnectionState.ACQUIRING)
        generation_before = widget._session.generation

        widget._activate_camera("camB", connect=False)
        # GUI shows the transition immediately without blocking.
        assert widget._lifecycle == CameraConnectionState.DISCONNECTING
        assert _drain_background(widget, qapp)
        assert widget._selected_camera_id == "camB"
        assert widget._lifecycle == CameraConnectionState.DISCONNECTED
        assert widget._session.generation == generation_before + 1
        # Old pipeline fully torn down exactly once.
        assert runtime.stops.count("camA") == 1
        assert "camA" not in runtime.running
        assert "camA" not in runtime.observers
        assert widget._observer is None
    finally:
        _close_widget(widget)


def test_no_duplicate_observers_on_restart(qapp) -> None:
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        widget._activate_camera("camA", connect=False)
        runtime.running.add("camA")  # connected transport behind CONNECTED
        widget._set_lifecycle(CameraConnectionState.CONNECTED)
        widget._on_start_acquisition()
        first = widget._observer
        assert first is not None
        assert widget._lifecycle == CameraConnectionState.ACQUIRING
        widget._on_stop_acquisition()
        assert widget._observer is None
        assert first.stopped
        widget._on_start_acquisition()
        second = widget._observer
        assert second is not None and second is not first
        assert len(runtime.observers) == 1
    finally:
        _close_widget(widget)


def test_disconnect_while_acquiring_auto_stops(qapp) -> None:
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        widget._activate_camera("camA", connect=False)
        runtime.running.add("camA")  # connected transport behind CONNECTED
        widget._set_lifecycle(CameraConnectionState.CONNECTED)
        widget._on_start_acquisition()
        assert widget._lifecycle == CameraConnectionState.ACQUIRING
        runtime.running.add("camA")

        widget._on_disconnect()
        assert widget._lifecycle == CameraConnectionState.DISCONNECTING
        assert _drain_background(widget, qapp)
        assert widget._lifecycle == CameraConnectionState.DISCONNECTED
        assert runtime.stops.count("camA") == 1
        assert widget._observer is None
    finally:
        _close_widget(widget)


def test_failed_connection_cleanup(qapp) -> None:
    runtime = FakeRuntime()
    runtime.fail_connect.add("camB")
    widget = _make_widget(qapp, runtime)
    try:
        widget._activate_camera("camB", connect=True)
        assert _drain_background(widget, qapp)
        assert widget._lifecycle == CameraConnectionState.ERROR
        # Defensive cleanup ran; nothing left running.
        assert "camB" not in runtime.running
        assert "camB" not in runtime.observers
    finally:
        _close_widget(widget)


def test_repeated_switch_cycles_no_leak(qapp) -> None:
    """Stress: A -> B -> A ... 10 cycles, exactly one teardown each."""
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        current = "camA"
        widget._activate_camera(current, connect=False)
        for _ in range(10):
            target = "camB" if current == "camA" else "camA"
            runtime.running.add(current)
            widget._set_lifecycle(CameraConnectionState.ACQUIRING)
            widget._activate_camera(target, connect=False)
            assert _drain_background(widget, qapp)
            assert widget._selected_camera_id == target
            assert widget._lifecycle == CameraConnectionState.DISCONNECTED
            assert widget._observer is None
            current = target
        assert widget._selected_camera_id == "camA"
        assert len(runtime.running) == 0
        assert len(runtime.observers) == 0
        # One stop per abandoned camera, no duplicates.
        assert len(runtime.stops) == 10
    finally:
        _close_widget(widget)


def test_rapid_disconnect_reconnect_cycles(qapp) -> None:
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        for _ in range(5):
            widget._activate_camera("camA", connect=True,
                                    config=widget._config_service.get_camera_config("camA"))
            assert _drain_background(widget, qapp)
            assert widget._lifecycle == CameraConnectionState.CONNECTED
            runtime.running.add("camA")
            widget._set_lifecycle(CameraConnectionState.ACQUIRING)
            widget._on_disconnect()
            assert _drain_background(widget, qapp)
            assert widget._lifecycle == CameraConnectionState.DISCONNECTED
        assert len(runtime.running) == 0
        assert len(runtime.observers) == 0
    finally:
        _close_widget(widget)


def test_process_crash_recovery_switch_unaffected(qapp) -> None:
    """Camera A crashes mid-acquisition: ERROR, recoverable, B unaffected."""
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        widget._activate_camera(
            "camA",
            connect=True,
            config=widget._config_service.get_camera_config("camA"),
        )
        assert _drain_background(widget, qapp)
        assert widget._lifecycle == CameraConnectionState.CONNECTED
        widget._on_start_acquisition()
        assert widget._lifecycle == CameraConnectionState.ACQUIRING
        crashed_observer = widget._observer
        assert crashed_observer is not None

        # Simulate the child process dying under acquisition.
        runtime.running.discard("camA")
        crashed_observer.error_occurred.emit("simulated child crash")
        qapp.processEvents()
        qapp.processEvents()
        assert widget._lifecycle == CameraConnectionState.ERROR

        # Recover A in place.
        widget._activate_camera(
            "camA",
            connect=True,
            config=widget._config_service.get_camera_config("camA"),
        )
        assert _drain_background(widget, qapp)
        assert widget._lifecycle == CameraConnectionState.CONNECTED
        assert "camA" in runtime.running

        # Switch to B: A is torn down exactly once, B connects cleanly.
        runtime.running.add("camA")
        widget._set_lifecycle(CameraConnectionState.ACQUIRING)
        widget._activate_camera(
            "camB",
            connect=True,
            config=widget._config_service.get_camera_config("camB"),
        )
        assert _drain_background(widget, qapp)
        assert widget._selected_camera_id == "camB"
        assert widget._lifecycle == CameraConnectionState.CONNECTED
        assert "camA" not in runtime.running
        assert "camB" in runtime.running
        widget._on_start_acquisition()
        assert widget._lifecycle == CameraConnectionState.ACQUIRING
        assert len(runtime.observers) == 1
    finally:
        _close_widget(widget)


def test_start_enabled_when_connected_with_streaming_process(qapp) -> None:
    """Regression: CONNECTED + streaming transport enables Start.

    The child process streams from connect time, so is_camera_running()
    is True while merely CONNECTED. The button matrix must follow the
    lifecycle authority (CONNECTED -> Start enabled), never a transport
    probe. A real button click must reach the runtime, attach acquisition
    (STARTING -> ACQUIRING) and deliver the first frame to the GUI.
    """
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        # Simulate post-connect reality: transport streaming, no display.
        runtime.running.add("camA")
        widget._activate_camera("camA", connect=False)
        widget._set_lifecycle(CameraConnectionState.CONNECTED)
        widget._load_camera_config("camA")

        panel = widget._acq_panel
        assert panel._connection_state == CameraConnectionState.CONNECTED
        assert panel._start_btn.isEnabled()
        assert not panel._stop_btn.isEnabled()
        assert not panel._connect_btn.isEnabled()
        assert panel._disconnect_btn.isEnabled()

        # A real button click reaches the runtime and starts acquisition.
        panel._start_btn.click()
        qapp.processEvents()
        assert "camA" in runtime.observer_starts
        assert widget._lifecycle == CameraConnectionState.ACQUIRING
        assert widget._observer is not None

        # First valid frame reaches the GUI.
        widget._on_processing_result(_make_result("camA", 0))
        assert widget._latest_result is not None
        assert widget._first_frame_at is not None

        # Stop works after Start (ACQUIRING matrix row).
        assert panel._stop_btn.isEnabled()
        assert not panel._start_btn.isEnabled()
        panel._stop_btn.click()
        assert widget._lifecycle == CameraConnectionState.CONNECTED
        assert widget._observer is None
    finally:
        _close_widget(widget)


def test_reconcile_lifecycle_on_mode_activation(qapp) -> None:
    """Reactivation corrects display/transport drift without transitions."""
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        widget._activate_camera("camA", connect=False)

        # Display detached (mode switch) but transport alive -> CONNECTED.
        runtime.running.add("camA")
        widget._set_lifecycle(CameraConnectionState.ACQUIRING)
        widget._reconcile_lifecycle()
        assert widget._lifecycle == CameraConnectionState.CONNECTED

        # Transport died unexpectedly while acquiring -> ERROR.
        runtime.running.discard("camA")
        widget._set_lifecycle(CameraConnectionState.ACQUIRING)
        widget._reconcile_lifecycle()
        assert widget._lifecycle == CameraConnectionState.ERROR

        # Transport died while connected -> DISCONNECTED.
        widget._set_lifecycle(CameraConnectionState.CONNECTED)
        widget._reconcile_lifecycle()
        assert widget._lifecycle == CameraConnectionState.DISCONNECTED

        # Transitional states are never touched by reconciliation.
        widget._set_lifecycle(CameraConnectionState.DISCONNECTING)
        widget._reconcile_lifecycle()
        assert widget._lifecycle == CameraConnectionState.DISCONNECTING
    finally:
        _close_widget(widget)


def test_start_stop_button_matrix(qapp) -> None:
    """Full lifecycle -> button matrix (Phase 6 control UX contract)."""
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        # (connect, disconnect, start, stop) per lifecycle state.
        expected = {
            CameraConnectionState.DISCONNECTED: (True, False, False, False),
            CameraConnectionState.CONNECTING: (False, False, False, False),
            CameraConnectionState.CONNECTED: (False, True, True, False),
            CameraConnectionState.STARTING: (False, True, False, False),
            CameraConnectionState.ACQUIRING: (False, True, False, True),
            CameraConnectionState.STOPPING: (False, True, False, False),
            CameraConnectionState.DISCONNECTING: (False, False, False, False),
            CameraConnectionState.ERROR: (False, True, False, False),
        }
        panel = widget._acq_panel
        for state, (connect, disconnect, start, stop) in expected.items():
            widget._lifecycle = state
            widget._apply_lifecycle_to_ui()
            assert panel._connect_btn.isEnabled() == connect, state
            assert panel._disconnect_btn.isEnabled() == disconnect, state
            assert panel._start_btn.isEnabled() == start, state
            assert panel._stop_btn.isEnabled() == stop, state
    finally:
        _close_widget(widget)


def test_double_start_creates_no_duplicate_consumer(qapp) -> None:
    """A second Start while acquiring is ignored: one observer, one consumer."""
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        widget._activate_camera("camA", connect=False)
        runtime.running.add("camA")  # connected transport behind CONNECTED
        widget._set_lifecycle(CameraConnectionState.CONNECTED)
        widget._on_start_acquisition()
        first = widget._observer
        assert first is not None
        widget._on_start_acquisition()  # ignored: can_start(ACQUIRING) is False
        assert widget._observer is first
        assert len(runtime.observer_starts) == 1
        assert len(runtime.observers) == 1
    finally:
        _close_widget(widget)


def test_disconnect_queued_during_transition(qapp) -> None:
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        widget._activate_camera("camA", connect=False)
        widget._set_lifecycle(CameraConnectionState.STARTING)
        # Simulate a transition owning the pipeline: disconnect must be
        # accepted (tests 24/25) and queued, never overlapped.
        assert can_disconnect(CameraConnectionState.STARTING)
        assert can_disconnect(CameraConnectionState.STOPPING)
        widget._pending_disconnect = True
        widget._process_pending_camera_request()
        assert widget._pending_disconnect is False
        # The queued disconnect ran as a bounded background teardown.
        assert _drain_background(widget, qapp)
        assert widget._lifecycle == CameraConnectionState.DISCONNECTED
    finally:
        _close_widget(widget)


def test_start_refused_without_transport(qapp) -> None:
    """Start with no camera process: truthful refusal, no observer attempt.

    This is the reported failure made honest: lifecycle CONNECTED but the
    parent registry is empty (child died / was torn down externally).
    Start must refuse with "not connected" and correct the drift instead
    of attempting the observer ("No running camera ...").
    """
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        widget._activate_camera("camA", connect=False)
        widget._set_lifecycle(CameraConnectionState.CONNECTED)
        widget._load_camera_config("camA")
        assert widget._acq_panel._start_btn.isEnabled()

        widget._acq_panel._start_btn.click()
        qapp.processEvents()
        assert widget._observer is None
        assert not runtime.observer_starts
        assert widget._lifecycle == CameraConnectionState.DISCONNECTED
        assert "no longer connected" in widget._status_label.text()
    finally:
        _close_widget(widget)


def test_start_refused_when_child_not_streaming(qapp) -> None:
    """Start with a non-STREAMING child: refused, ERROR, no observer."""
    from thermal_monitor.camera.model import AcquisitionState

    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        widget._activate_camera("camA", connect=False)
        runtime.running.add("camA")
        runtime._child_state["camA"] = AcquisitionState.FAILED
        widget._set_lifecycle(CameraConnectionState.CONNECTED)
        widget._load_camera_config("camA")

        widget._acq_panel._start_btn.click()
        qapp.processEvents()
        assert widget._observer is None
        assert not runtime.observer_starts
        assert widget._lifecycle == CameraConnectionState.ERROR
    finally:
        _close_widget(widget)


def test_start_without_connect_is_inert(qapp) -> None:
    """DISCONNECTED offers no Start: button disabled and handler inert."""
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        widget._activate_camera("camA", connect=False)
        assert widget._lifecycle == CameraConnectionState.DISCONNECTED
        assert not widget._acq_panel._start_btn.isEnabled()
        widget._on_start_acquisition()  # direct call also refuses silently
        assert widget._observer is None
        assert not runtime.observer_starts
    finally:
        _close_widget(widget)


def test_stop_keeps_process_reusable(qapp) -> None:
    """Stop detaches display only: transport survives, Start works again."""
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        widget._activate_camera("camA", connect=False)
        runtime.running.add("camA")
        widget._set_lifecycle(CameraConnectionState.CONNECTED)
        widget._on_start_acquisition()
        assert widget._lifecycle == CameraConnectionState.ACQUIRING

        widget._on_stop_acquisition()
        assert widget._lifecycle == CameraConnectionState.CONNECTED
        assert widget._observer is None
        assert "camA" in runtime.running  # child process reusable
        assert widget._acq_panel._start_btn.isEnabled()

        widget._on_start_acquisition()
        assert widget._lifecycle == CameraConnectionState.ACQUIRING
        assert widget._observer is not None
    finally:
        _close_widget(widget)


def test_liveness_tick_corrects_dead_transport(qapp) -> None:
    """The 1 Hz watch follows child death without any click."""
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        widget._activate_camera("camA", connect=False)
        runtime.running.add("camA")
        widget._set_lifecycle(CameraConnectionState.CONNECTED)
        widget._on_start_acquisition()
        assert widget._lifecycle == CameraConnectionState.ACQUIRING

        # Child dies out from under acquisition (crash / external teardown).
        runtime.running.discard("camA")
        runtime.observers.pop("camA", None)
        widget._update_stats()
        assert widget._lifecycle == CameraConnectionState.ERROR

        # Same for a merely-connected camera: back to DISCONNECTED.
        runtime.running.discard("camA")
        widget._set_lifecycle(CameraConnectionState.CONNECTED)
        widget._update_stats()
        assert widget._lifecycle == CameraConnectionState.DISCONNECTED
    finally:
        _close_widget(widget)


def test_counters_reset_on_new_session(qapp) -> None:
    """Frame/FPS counters never show the previous session's numbers."""
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        widget._activate_camera("camA", connect=False)
        widget._status_frames.setText("Frames: 878")
        widget._status_fps.setText("FPS: 9.0")
        widget._activate_camera("camB", connect=False)
        assert widget._status_frames.text() == "Frames: —"
        assert widget._status_fps.text() == "FPS: —"
        # Disconnected sessions paint no transport numbers either.
        widget._update_stats()
        assert widget._status_frames.text() == "Frames: —"
    finally:
        _close_widget(widget)


def test_connect_dialog_dismiss_restores_ui(qapp) -> None:
    """Dismissing Connect without selection releases the CONNECTING hold."""
    from PyQt6.QtWidgets import QDialog

    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        widget._activate_camera("camA", connect=False)
        # Simulate the Connect button's CONNECTING hold on the panels.
        widget._toolbar.set_connection_state(CameraConnectionState.CONNECTING)
        widget._acq_panel.set_connection_state(CameraConnectionState.CONNECTING)
        assert not widget._acq_panel._connect_btn.isEnabled()

        widget._on_connect_dialog_finished(QDialog.DialogCode.Rejected)
        assert widget._acq_panel._connection_state == CameraConnectionState.DISCONNECTED
        assert widget._acq_panel._connect_btn.isEnabled()
        # Selection text is preserved; the CONNECTING freeze is gone.
        assert widget._status_label.text() == "Camera: camA"

        # Accepted takes the camera_selected path: no interference.
        widget._toolbar.set_connection_state(CameraConnectionState.CONNECTING)
        widget._acq_panel.set_connection_state(CameraConnectionState.CONNECTING)
        widget._on_connect_dialog_finished(QDialog.DialogCode.Accepted)
        assert widget._acq_panel._connection_state == CameraConnectionState.CONNECTING
    finally:
        _close_widget(widget)


def test_double_connect_second_queues(qapp) -> None:
    """Two rapid Connects serialize: one running entry, CONNECTED end state."""
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime)
    try:
        config = widget._config_service.get_camera_config("camA")
        widget._activate_camera("camA", connect=True, config=config)
        widget._activate_camera("camA", connect=True, config=config)
        assert _drain_background(widget, qapp)
        # The queued duplicate also drains through the same pipeline.
        assert _drain_background(widget, qapp)
        assert widget._lifecycle == CameraConnectionState.CONNECTED
        assert "camA" in runtime.running
        assert widget._pending_switch is None
    finally:
        _close_widget(widget)


# ---------------------------------------------------------------------------
# Runtime-level feed semantics (real service, synthetic in-process source)
# ---------------------------------------------------------------------------


def _synthetic_runtime():
    import uuid

    from thermal_monitor.config import (
        CamerasConfig,
        RecordingConfig,
        StorageConfig,
        SystemConfig,
    )
    from thermal_monitor.services.runtime import CameraRuntimeService
    from tests.test_runtime_service import ThrottledFrameSource

    service = CameraRuntimeService(
        cameras_config=CamerasConfig(),
        system_config=SystemConfig(),
        recording_config=RecordingConfig(),
        storage_config=StorageConfig(),
        source_factory=lambda cfg: ThrottledFrameSource(),
    )
    camera_id = f"cfgtest_{uuid.uuid4().hex[:8]}"
    return service, camera_id


def _synthetic_config(camera_id: str):
    from thermal_monitor.core.models import CameraConfig, CameraIdentity

    return CameraConfig(
        identity=CameraIdentity(camera_id=camera_id, serial_number=f"SN_{camera_id}"),
        name="192.168.1.99",
    )


def test_runtime_connect_creates_no_observer() -> None:
    """CONNECT registers transport only; no observer/consumer is attached."""
    service, camera_id = _synthetic_runtime()
    try:
        service.start_camera(_synthetic_config(camera_id), timeout=10.0)
        assert service.is_camera_running(camera_id)
        assert not service.is_observer_running(camera_id)
        assert service.observer_service(camera_id) is None
    finally:
        service.shutdown(timeout=5.0)


def test_runtime_reconnect_after_disconnect_resumes() -> None:
    """DISCONNECT then CONNECT brings the same camera back to STREAMING."""
    service, camera_id = _synthetic_runtime()
    try:
        service.start_camera(_synthetic_config(camera_id), timeout=10.0)
        assert service.is_camera_running(camera_id)
        service.stop_camera(camera_id, timeout=5.0)
        assert not service.is_camera_running(camera_id)
        service.start_camera(_synthetic_config(camera_id), timeout=10.0)
        assert service.is_camera_running(camera_id)
        stats = service.camera_stats(camera_id)
        assert stats is not None and stats.frames_received >= 0
    finally:
        service.shutdown(timeout=5.0)


def test_runtime_observer_delivers_first_frame() -> None:
    """START (observer attach) on a connected camera yields results."""
    from thermal_monitor.core.models import AnalysisConfig
    from tests.test_runtime_service import FixedCalibrationProvider

    service, camera_id = _synthetic_runtime()
    try:
        service.start_camera(_synthetic_config(camera_id), timeout=10.0)
        analysis = AnalysisConfig(camera_id=camera_id)
        observer = service.start_observer(
            camera_id,
            analysis_config=analysis,
            calibration_provider=FixedCalibrationProvider(),
        )
        # Bridge results directly (no Qt needed at runtime level).
        consumer = observer._consumer
        assert consumer is not None
        assert consumer.wait_for_frames(1, timeout=10.0), \
            "no frame reached the processing consumer"
        assert consumer.stats().frames_processed >= 1
    finally:
        service.shutdown(timeout=5.0)


# ---------------------------------------------------------------------------
# Stale-ID hunt: real combo selection + real button clicks (section 10)
# ---------------------------------------------------------------------------


def _combo_select(widget: ConfigurationModeWidget, camera_id: str) -> None:
    """Drive the toolbar combo exactly like a user click (emits signal)."""
    assert widget._toolbar.select_camera_by_id(camera_id), camera_id


def _assert_selection_invariant(widget: ConfigurationModeWidget) -> None:
    """selected == toolbar == panel identity (the section-3 invariant)."""
    selected = widget._selected_camera_id
    toolbar = widget._toolbar._camera_combo.currentData()
    panel_identity = widget._acq_panel._selected_camera_identity
    panel_id = getattr(panel_identity, "camera_id", None)
    assert toolbar == selected, f"toolbar={toolbar} selected={selected}"
    assert panel_id == selected, f"panel={panel_id} selected={selected}"


def _connect_selected(widget, qapp, runtime) -> None:
    """Connect whatever is currently selected (not a hardcoded camera)."""
    selected = widget._selected_camera_id
    config = widget._config_service.get_camera_config(selected)
    runtime.running.add(selected)  # transport behind the UI state
    widget._set_lifecycle(CameraConnectionState.CONNECTED)
    widget._load_camera_config(selected)
    _assert_selection_invariant(widget)
    assert config is not None


def test_click_path_ABA_start_uses_A(qapp) -> None:
    """A selected, B selected, A selected, click Start -> runtime gets A."""
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime, camera_ids=("camA", "camB"))
    try:
        _combo_select(widget, "camA")
        _assert_selection_invariant(widget)
        _connect_selected(widget, qapp, runtime)
        widget._acq_panel._start_btn.click()
        qapp.processEvents()
        assert runtime.observer_starts[-1] == "camA"

        _combo_select(widget, "camB")
        assert _drain_background(widget, qapp)
        _assert_selection_invariant(widget)

        _combo_select(widget, "camA")
        assert _drain_background(widget, qapp)
        _assert_selection_invariant(widget)
        _connect_selected(widget, qapp, runtime)

        before = len(runtime.observer_starts)
        widget._acq_panel._start_btn.click()
        qapp.processEvents()
        assert len(runtime.observer_starts) == before + 1
        assert runtime.observer_starts[-1] == "camA"
        _assert_selection_invariant(widget)
    finally:
        _close_widget(widget)


def test_click_path_AB_start_uses_B(qapp) -> None:
    """A selected, B selected, click Start -> runtime gets B, never A."""
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime, camera_ids=("camA", "camB"))
    try:
        _combo_select(widget, "camA")
        _connect_selected(widget, qapp, runtime)
        widget._acq_panel._start_btn.click()
        qapp.processEvents()
        assert runtime.observer_starts[-1] == "camA"
        widget._on_stop_acquisition()

        _combo_select(widget, "camB")
        assert _drain_background(widget, qapp)
        _assert_selection_invariant(widget)
        _connect_selected(widget, qapp, runtime)

        before = len(runtime.observer_starts)
        widget._acq_panel._start_btn.click()
        qapp.processEvents()
        assert len(runtime.observer_starts) == before + 1
        requested = runtime.observer_starts[-1]
        assert requested == "camB", f"STALE ID: Start used {requested} for camB"
        assert requested == widget._selected_camera_id
        _assert_selection_invariant(widget)
    finally:
        _close_widget(widget)


def test_invariant_holds_across_config_churn(qapp) -> None:
    """Config refreshes/fps edits never move or stale the selection."""
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime, camera_ids=("camA", "camB"))
    try:
        _combo_select(widget, "camB")
        _assert_selection_invariant(widget)
        # Simulate fps/metadata edits exactly like the panel handlers do.
        for fps in (9, 15, 9):
            config = widget._config_service.get_camera_config("camB")
            metadata = dict(config.metadata or {})
            metadata["frame_rate"] = fps
            from thermal_monitor.core.models import CameraConfig

            widget._config_service.set_camera_config(
                CameraConfig(
                    identity=config.identity,
                    name=config.name,
                    description=config.description,
                    enabled=config.enabled,
                    thermal_enabled=config.thermal_enabled,
                    visible_enabled=config.visible_enabled,
                    ptz_config=config.ptz_config,
                    tags=config.tags,
                    metadata=metadata,
                )
            )
            qapp.processEvents()
            _assert_selection_invariant(widget)
        assert widget._selected_camera_id == "camB"
    finally:
        _close_widget(widget)


def test_queued_switch_window_never_starts_old_camera(qapp) -> None:
    """Switch queued behind teardown: Start cannot fire for the old camera."""
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime, camera_ids=("camA", "camB"))
    try:
        _combo_select(widget, "camA")
        _connect_selected(widget, qapp, runtime)
        widget._acq_panel._start_btn.click()
        qapp.processEvents()
        assert widget._lifecycle == CameraConnectionState.ACQUIRING

        # Switch while acquiring: teardown owns the pipeline (bg busy).
        _combo_select(widget, "camB")
        assert widget._bg_busy()
        # A Start click in this window must not start ANY camera.
        before = len(runtime.observer_starts)
        widget._acq_panel._start_btn.click()
        qapp.processEvents()
        assert len(runtime.observer_starts) == before
        assert widget._observer is None or widget._observer.stopped
        # After convergence the selection is exactly B.
        assert _drain_background(widget, qapp)
        assert widget._selected_camera_id == "camB"
        _assert_selection_invariant(widget)
    finally:
        _close_widget(widget)


def test_no_phantom_switch_on_config_refresh(qapp) -> None:
    """Rebuilding the camera list never moves a non-first selection.

    Regression for the proven stale-ID source: populating the combo
    used to auto-select index 0 mid-rebuild and emit a phantom
    camera_selected, hijacking the widget selection (and Start) to the
    wrong camera. Config churn must be selection-neutral.
    """
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime, camera_ids=("camA", "camB", "camC"))
    try:
        _combo_select(widget, "camC")  # non-first entry: phantom would jump to camA
        generation = widget._session.generation
        for _ in range(3):
            widget._refresh_camera_list()
            qapp.processEvents()
        assert widget._selected_camera_id == "camC"
        assert widget._session.generation == generation
        assert widget._bg_tag is None
        assert runtime.stops == []
        _assert_selection_invariant(widget)
    finally:
        _close_widget(widget)


def test_start_refused_on_identity_mismatch(qapp) -> None:
    """Toolbar/panel divergence from authority: refuse, log, correct."""
    runtime = FakeRuntime()
    widget = _make_widget(qapp, runtime, camera_ids=("camA", "camB"))
    try:
        _combo_select(widget, "camA")
        _connect_selected(widget, qapp, runtime)
        # Force a view divergence without touching the authority.
        widget._toolbar.blockSignals(True)
        try:
            assert widget._toolbar.select_camera_by_id("camB")
        finally:
            widget._toolbar.blockSignals(False)
        assert widget._selected_camera_id == "camA"
        assert widget._toolbar._camera_combo.currentData() == "camB"

        widget._acq_panel._start_btn.click()
        qapp.processEvents()
        # Refused: no observer for either camera, nothing started.
        assert widget._observer is None
        assert not runtime.observer_starts
        assert widget._lifecycle == CameraConnectionState.CONNECTED
        assert "mismatch" in widget._status_label.text()
        # Corrected: views re-asserted from the authority.
        assert widget._toolbar._camera_combo.currentData() == "camA"
        _assert_selection_invariant(widget)
    finally:
        _close_widget(widget)
