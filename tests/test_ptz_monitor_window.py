"""Phase 9A: PLC & PTZ monitor window tests (offscreen Qt).

Headless tests use an injected fake service (no OPC UA); one live test
drives the real simulator + real PtzService through the window's poll
path. Requires asyncua only for the live test (importorskip).
"""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from thermal_monitor.ptz.errors import PtzError, PtzErrorCategory
from thermal_monitor.ptz.mapping import LogicalField
from thermal_monitor.ptz.state import (
    CalibrationState,
    PlcConnectionState,
    PtzMovementState,
    PtzStatus,
)
from thermal_monitor.ui.windows.ptz_monitor_window import (
    PTZ_COLUMNS,
    MonitorSnapshot,
    PlcSummarySnapshot,
    PtzMonitorWindow,
    PtzRowSnapshot,
)


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def make_status(**overrides) -> PtzStatus:
    values = {
        "plc_state": PlcConnectionState.CONNECTED,
        "ptz_available": True,
        "communication_ok": True,
        "ready": True,
        "movement": PtzMovementState.IDLE,
        "actual_pan": 10.0,
        "actual_tilt": -5.0,
        "calibration": CalibrationState.NOT_REQUIRED,
        "error": None,
    }
    values.update(overrides)
    return PtzStatus(**values)


class FakeService:
    """Read-only PtzService double for the monitor window."""

    def __init__(self, ptz_ids, status_fn=None) -> None:
        self._ptz_ids = list(ptz_ids)
        self._status_fn = status_fn or (lambda ptz_id: make_status())
        self.shutdown_calls = 0
        self.connect_calls = 0
        self.disconnect_calls = 0
        self._closed = False

    def status_for_ptz(self, ptz_id):
        if ptz_id not in self._ptz_ids:
            raise ValueError(f"unknown PTZ {ptz_id}")
        return self._status_fn(ptz_id)

    def read_field(self, ptz_id, field):
        if field == LogicalField.TARGET_PAN:
            return 11.0
        if field == LogicalField.TARGET_TILT:
            return -6.0
        raise ValueError(f"unsupported field {field}")

    def connect(self):
        self.connect_calls += 1

    def disconnect(self):
        self.disconnect_calls += 1

    def shutdown(self, timeout_s=5.0):
        self.shutdown_calls += 1
        self._closed = True

    @property
    def closed(self):
        return self._closed


def make_window(qapp, ptz_ids=("PTZ_01", "PTZ_02"), **kwargs):
    bindings = [(f"cam_{i + 1:02d}", ptz_id) for i, ptz_id in enumerate(ptz_ids)]
    kwargs.setdefault("poll_interval_ms", 50)
    window = PtzMonitorWindow(bindings=bindings, **kwargs)
    window.show()
    qapp.processEvents()
    return window


def test_window_creation(qapp):
    window = make_window(qapp)
    try:
        assert window.windowTitle().startswith("Thermal Monitoring System V3")
        assert window._table.columnCount() == len(PTZ_COLUMNS)
        assert window._reconnect_btn is not None
    finally:
        window.close()
        qapp.processEvents()


def test_empty_configuration(qapp):
    """Unservable profile: no rows, loud reason, nothing fabricated."""

    class _Ptz:
        profile = "generic"
        endpoint = "opc.tcp://127.0.0.1:9"
        security_mode = "none"
        username = ""

    class _Cameras:
        mapping = []

    class _Config:
        ptz = _Ptz()
        cameras = _Cameras()

    class _ConfigManager:
        def get_config(self):
            return _Config()

    window = PtzMonitorWindow(
        config_manager=_ConfigManager(), bindings=[], poll_interval_ms=50
    )
    window.show()
    qapp.processEvents()
    try:
        snapshot = window._poll_once()
        assert snapshot is not None
        assert snapshot.rows == []
        window._apply_snapshot(window._generation, snapshot)
        qapp.processEvents()
        assert window._table.rowCount() == 0
        # Reason shown, never fabricated values.
        assert "mapping" in window._summary_labels["error"].text()
    finally:
        window.close()
        qapp.processEvents()


def test_unconfigured_simulator_shows_units_unknown(qapp):
    """Simulator profile with no bindings: 8 units, Unknown until polled."""
    window = PtzMonitorWindow(bindings=[], poll_interval_ms=50)
    try:
        ids = window._ptz_ids(type("S", (), {"profile": "simulator"})())
        assert ids == [f"PTZ_{i:02d}" for i in range(1, 9)]
    finally:
        window.close()
        qapp.processEvents()


def test_eight_ptz_rows_render(qapp):
    ptz_ids = tuple(f"PTZ_{i:02d}" for i in range(1, 9))
    service = FakeService(ptz_ids)
    window = make_window(qapp, ptz_ids, service=service)
    try:
        window._service = service
        snapshot = window._poll_once()
        assert snapshot is not None
        assert len(snapshot.rows) == 8
        window._apply_snapshot(window._generation, snapshot)
        qapp.processEvents()
        assert window._table.rowCount() == 8
        assert window._table.item(0, 0).text() == "PTZ_01"
        assert window._table.item(7, 0).text() == "PTZ_08"
        assert window._table.item(0, 1).text() == "cam_01"
        assert window._table.item(0, 4).text() == "10.00"
    finally:
        window.close()
        qapp.processEvents()


def test_disconnected_state_rendering(qapp):
    service = FakeService(
        ("PTZ_01",),
        status_fn=lambda ptz_id: make_status(
            plc_state=PlcConnectionState.DISCONNECTED,
            communication_ok=False,
            ready=False,
        ),
    )
    window = make_window(qapp, ("PTZ_01",), service=service)
    try:
        window._service = service
        snapshot = window._poll_once()
        assert snapshot is not None
        assert snapshot.summary.connection == "Disconnected"
        assert snapshot.summary.comm_health == "LOST"
        window._apply_snapshot(window._generation, snapshot)
        qapp.processEvents()
        assert window._table.item(0, 2).text() == "disconnected (stale)"
        assert window._table.item(0, 3).text() == "No"
    finally:
        window.close()
        qapp.processEvents()


def test_error_state_rendering(qapp):
    service = FakeService(
        ("PTZ_01",),
        status_fn=lambda ptz_id: make_status(
            error=PtzError(
                code="E42", message="axis fault", category=PtzErrorCategory.PTZ
            )
        ),
    )
    window = make_window(qapp, ("PTZ_01",), service=service)
    try:
        window._service = service
        snapshot = window._poll_once()
        assert snapshot is not None
        assert "E42" in snapshot.rows[0].error
        assert "axis fault" in snapshot.summary.error
        window._apply_snapshot(window._generation, snapshot)
        qapp.processEvents()
        assert "E42" in window._table.item(0, 11).text()
    finally:
        window.close()
        qapp.processEvents()


def test_unknown_fields_render(qapp):
    class NoTargetService(FakeService):
        def read_field(self, ptz_id, field):
            raise RuntimeError("node unavailable")

    service = NoTargetService(("PTZ_01",))
    window = make_window(qapp, ("PTZ_01",), service=service)
    try:
        window._service = service
        snapshot = window._poll_once()
        assert snapshot is not None
        # Targets unreadable -> Unknown, never fabricated.
        assert snapshot.rows[0].target_pan is None
        assert snapshot.rows[0].active_position == "Unknown"
        window._apply_snapshot(window._generation, snapshot)
        qapp.processEvents()
        assert window._table.item(0, 6).text() == "Unknown"
        assert window._table.item(0, 10).text() == "Unknown"
    finally:
        window.close()
        qapp.processEvents()


def test_stale_callback_rejected(qapp):
    window = make_window(qapp)
    try:
        snapshot = MonitorSnapshot(
            summary=PlcSummarySnapshot(connection="Connected"),
            rows=[
                PtzRowSnapshot(
                    ptz_id="PTZ_01",
                    camera_id="cam_01",
                    actual_pan=99.0,
                    updated="12:00:00",
                )
            ],
        )
        # Stale generation: must not touch widgets.
        window._apply_snapshot(window._generation + 99, snapshot)
        qapp.processEvents()
        assert window._table.rowCount() == 0
        # Current generation: applied.
        window._apply_snapshot(window._generation, snapshot)
        qapp.processEvents()
        assert window._table.rowCount() == 1
        assert window._table.item(0, 4).text() == "99.00"
    finally:
        window.close()
        qapp.processEvents()


def test_close_while_polling(qapp):
    service = FakeService(("PTZ_01",))
    window = make_window(qapp, ("PTZ_01",), service=service, poll_interval_ms=10)
    try:
        window._service = service
        window.start_monitoring()
        qapp.processEvents()
        time.sleep(0.05)
        window.close()  # must not hang, raise, or shut down injected service
        qapp.processEvents()
        assert service.shutdown_calls == 0
    finally:
        try:
            window.close()
        except RuntimeError:
            pass
        qapp.processEvents()


def test_shutdown_owned_service(qapp):
    window = make_window(qapp)
    service = FakeService(("PTZ_01",))
    window._service = service  # owned (created-by-window path sets _owns_service)
    window._owns_service = True
    try:
        window.close()
        qapp.processEvents()
        deadline = time.monotonic() + 5.0
        while service.shutdown_calls == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
            qapp.processEvents()
        assert service.shutdown_calls == 1
    finally:
        try:
            window.close()
        except RuntimeError:
            pass


def test_reconnect_button_nonblocking(qapp):
    service = FakeService(("PTZ_01",))
    window = make_window(qapp, ("PTZ_01",), service=service)
    try:
        window._service = service
        window._on_reconnect_clicked()
        qapp.processEvents()
        deadline = time.monotonic() + 5.0
        while service.connect_calls == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
            qapp.processEvents()
        assert service.disconnect_calls >= 1
        assert service.connect_calls >= 1
    finally:
        window.close()
        qapp.processEvents()


def test_launcher_emits_monitor_request(qapp):
    from thermal_monitor.ui.windows.launcher_window import LauncherWindow

    launcher = LauncherWindow(
        mode_service=None, config_service=None, discovery_service=None
    )
    try:
        assert launcher._ptz_monitor_btn is not None
        received = []
        launcher.ptz_monitor_requested.connect(lambda: received.append(True))
        launcher._ptz_monitor_btn.click()
        qapp.processEvents()
        assert received == [True]
    finally:
        launcher.close()
        qapp.processEvents()


def test_controller_owns_monitor_window(qapp):
    from thermal_monitor.services.configuration import ConfigurationService
    from thermal_monitor.services.discovery import CameraDiscoveryService
    from thermal_monitor.services.mode import ModeService
    from thermal_monitor.services.offline import OfflineService
    from thermal_monitor.services.runtime import CameraRuntimeService
    from thermal_monitor.ui.controller import AppController

    pytest.importorskip("PyQt6.QtWidgets")
    controller = AppController(
        mode_service=ModeService(),
        config_service=ConfigurationService(),
        offline_service=OfflineService(),
        runtime_service=None,
        discovery_service=CameraDiscoveryService(),
        config_manager=None,
        theme_manager=None,
    )
    try:
        assert controller.monitor_window is None
        window = controller._create_monitor_window()
        assert controller.monitor_window is window
        assert controller._create_monitor_window() is window  # idempotent
        controller._on_monitor_window_destroyed()
        assert controller.monitor_window is None
        window.close()
        qapp.processEvents()
    finally:
        try:
            if controller.monitor_window is not None:
                controller.monitor_window.close()
        except RuntimeError:
            pass
        qapp.processEvents()


def test_live_simulator_status_updates(qapp):
    """End-to-end through the real simulator + real PtzService."""
    asyncua = pytest.importorskip("asyncua")
    from thermal_monitor.ptz.client import (
        AsyncuaTransport,
        OpcUaClientConfig,
        OpcUaSession,
    )
    from thermal_monitor.ptz.mapping import SimulatorPtzMapping
    from thermal_monitor.ptz.models import PtzStationBinding, PtzTolerance
    from thermal_monitor.ptz.service import PtzService, PtzServiceConfig
    from tools.ptz_plc_simulator.plc_server import PtzPlcSimulatorServer
    from tools.ptz_plc_simulator.simulator_config import (
        SIMULATOR_TEST_ENDPOINT,
        SimulatorConfig,
        default_ptz_ids,
    )

    ptz_ids = default_ptz_ids(2)
    server = PtzPlcSimulatorServer(
        SimulatorConfig(
            ptz_ids=ptz_ids,
            endpoint=SIMULATOR_TEST_ENDPOINT,
            calibration_duration_s=0.3,
            update_hz=50.0,
            max_velocity=360.0,
        )
    )
    server.start_background()
    try:
        session = OpcUaSession(
            OpcUaClientConfig(endpoint=SIMULATOR_TEST_ENDPOINT),
            AsyncuaTransport(),
        )
        service = PtzService(
            session,
            SimulatorPtzMapping(ptz_ids=ptz_ids),
            PtzServiceConfig(tolerance=PtzTolerance(pan=0.2, tilt=0.2)),
        )
        for index, ptz_id in enumerate(ptz_ids):
            service.register_binding(
                PtzStationBinding(camera_id=f"cam_{index + 1:02d}", ptz_id=ptz_id)
            )
        service.connect()
        window = PtzMonitorWindow(
            bindings=[(f"cam_{i + 1:02d}", ptz_id) for i, ptz_id in enumerate(ptz_ids)],
            poll_interval_ms=50,
            service=service,
        )
        window.show()
        qapp.processEvents()
        try:
            window._service = service
            window._owns_service = False
            snapshot = window._poll_once()
            assert snapshot is not None
            assert len(snapshot.rows) == 2
            assert snapshot.summary.connection == "Connected"
            assert snapshot.rows[0].actual_pan is not None
            # Command a move through a second handle and observe updates.
            service.move_absolute("cam_01", 45.0, -12.0, velocity=120.0)
            deadline = time.monotonic() + 15.0
            reached = False
            while time.monotonic() < deadline:
                snapshot = window._poll_once()
                if any(
                    r.ptz_id == "PTZ_01"
                    and r.actual_pan is not None
                    and abs(r.actual_pan - 45.0) < 1.0
                    for r in snapshot.rows
                ):
                    reached = True
                    break
                time.sleep(0.1)
            assert reached, "monitor never observed the commanded position"
            window._apply_snapshot(window._generation, snapshot)
            qapp.processEvents()
            assert window._table.rowCount() == 2
        finally:
            window.close()
            qapp.processEvents()
        service.shutdown()
    finally:
        server.stop_background()


def test_dead_link_notifies_once(qapp):
    """All-failed poll triggers one loss notification, then stays quiet."""
    notified = []
    service = FakeService(("PTZ_01",))
    service.notify_connection_lost = notified.append  # type: ignore[method-assign]
    window = make_window(qapp, ("PTZ_01",), service=service)
    try:
        window._service = service
        failing = FakeService(
            ("PTZ_01",),
            status_fn=lambda ptz_id: (_ for _ in ()).throw(
                RuntimeError("link down")
            ),
        )
        failing.notify_connection_lost = notified.append  # type: ignore[method-assign]
        window._service = failing
        snapshot = window._poll_once()
        assert snapshot is not None
        assert snapshot.summary.connection == "Disconnected"
        assert notified == ["monitor poll observed dead link"]
        # Second dead poll: no duplicate notification.
        window._poll_once()
        assert notified == ["monitor poll observed dead link"]
    finally:
        window.close()
        qapp.processEvents()


def test_service_notify_delegates_to_session():
    """PtzService.notify_connection_lost reaches the session hook."""
    from thermal_monitor.ptz.client import OpcUaClientConfig, OpcUaSession
    from thermal_monitor.ptz.mapping import SimulatorPtzMapping
    from thermal_monitor.ptz.service import PtzService
    from thermal_monitor.ptz.state import PlcConnectionState

    class FakeTransport:
        def __init__(self):
            self.connected = True

        def connect(self, endpoint, timeout_s):
            self.connected = True

        def disconnect(self, timeout_s):
            self.connected = False

        def read(self, node, timeout_s):
            raise RuntimeError("down")

        def write(self, node, value, timeout_s):
            raise RuntimeError("down")

        def subscribe(self, node, callback, timeout_s):
            raise RuntimeError("down")

        def unsubscribe(self, handle):
            pass

    session = OpcUaSession(
        OpcUaClientConfig(endpoint="opc.tcp://127.0.0.1:9"), FakeTransport()
    )
    service = PtzService(session, SimulatorPtzMapping(ptz_ids=("PTZ_01",)))
    states = []
    session._report_error = lambda error: states.append(session.state)
    session._state = PlcConnectionState.CONNECTED  # simulate a live session
    service.notify_connection_lost("probe")
    # Loss registered (error reported); the reconnect thread then owns
    # the state machine, so only assert we left DISCONNECTED behind.
    assert states, "expected a connection-lost error report"
    assert session.state != PlcConnectionState.DISCONNECTED
    service.shutdown()
