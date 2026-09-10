"""Loopback proof for the field failure: focus/NUC stuck forever.

Reproduces the reported production symptom (Focus jams at "Reading...",
NUC jams at "NUC Running...") without hardware by running the REAL
production chain against a fake TV46L UDP server on loopback:

    FocusWorker / NucWorker (real QThread)
      -> CameraRuntimeService (real)
        -> CustomTV46LDriver (real, streaming via scripted GVSP blocks)
          -> GVCPClient (real UDP to 127.0.0.1:3956)
            -> FakeTV46L (answers like the proven standalone behavior)

The fake answers stream registers instantly and the NUC register with one
PENDING_ACK + final ack (the GigE Vision sequence the real camera uses
for action registers). This proves:

* workers actually execute and their signals reach the UI slots,
* focus read/write + NUC complete with the stream running,
* repeated NUC works and stream config is verified each time,
* a dead camera fails LOUDLY and QUICKLY (no infinite "Reading..."),
* worker threads terminate (no destroy-while-running).

Acquisition/GVSP parsing itself is covered elsewhere; the scripted GVSP
blocks here only keep the worker in STREAMING during control ops.
"""

from __future__ import annotations

import os
import socket
import struct
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication

from thermal_monitor.camera.tv46_custom import CustomTV46LDriver
from thermal_monitor.camera.tv46_gvcp import (
    GVCPCommand,
    GVCP_PORT,
    REG_ACQUISITION_START,
    REG_CCP,
    REG_FOCUS_CURRENT,
    REG_FOCUS_MAX,
    REG_FOCUS_MIN,
    REG_FOCUS_SET,
    REG_FUSION_SELECTOR,
    REG_HEARTBEAT_TIMEOUT,
    REG_NUC_COMMAND,
    REG_PACKET_DELAY,
    REG_SCDA0,
    REG_SCP0,
    REG_SCPS0,
    GVCPClient,
)
from thermal_monitor.camera.tv46_gvsp import COMBINED_BYTES, LEADER_BYTES
from thermal_monitor.camera.model import CameraConfig, CameraIdentity
from thermal_monitor.config import CamerasConfig, RecordingConfig, StorageConfig, SystemConfig
from thermal_monitor.core.models import CameraConfig as AppCameraConfig
from thermal_monitor.core.models import CameraIdentity as AppCameraIdentity
from thermal_monitor.services.runtime import CameraRuntimeService
from thermal_monitor.ui.widgets.image_acquisition_panel import ImageAcquisitionPanel
from thermal_monitor.ui.windows.configuration_window import FocusWorker, NucWorker

CAMERA_IP = "127.0.0.1"
LOCAL_IP = "127.0.0.1"


class FakeTV46L:
    """Minimal TV46L GVCP responder on loopback (proven wire behavior)."""

    def __init__(self, storm_regs: "set[int] | None" = None, storm_count: int = 100) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((LOCAL_IP, GVCP_PORT))
        self.sock.settimeout(0.1)
        self.registers = {
            REG_CCP: 0,
            REG_HEARTBEAT_TIMEOUT: 30000,
            REG_SCDA0: 0,
            REG_SCP0: 0,
            REG_SCPS0: 1500,
            REG_PACKET_DELAY: 10000,
            REG_FUSION_SELECTOR: 3,
            REG_ACQUISITION_START: 0,
            REG_FOCUS_SET: 1000,
            REG_FOCUS_CURRENT: 1000,
            REG_FOCUS_MIN: 150,
            REG_FOCUS_MAX: 1000000,
            REG_NUC_COMMAND: 0,
        }
        # Registers that answer with endless PENDING (never a final ack):
        # the client must fail loudly and bounded instead of hanging.
        self.storm_regs = set(storm_regs or ())
        self.storm_count = storm_count
        self.nuc_pending_once = True
        self.running = True
        self.requests = 0
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> "FakeTV46L":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.running = False
        self.thread.join(timeout=2.0)
        self.sock.close()

    def _ack(self, req_id: int, ack_cmd: int, payload: bytes = b"") -> bytes:
        return struct.pack(">HHHH", 0, ack_cmd, len(payload), req_id) + payload

    def _loop(self) -> None:
        while self.running:
            try:
                data, addr = self.sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) < 8:
                continue
            cmd = struct.unpack(">H", data[2:4])[0]
            req_id = struct.unpack(">H", data[6:8])[0]
            self.requests += 1
            if cmd == GVCPCommand.READ_REG and len(data) >= 12:
                address = struct.unpack(">I", data[8:12])[0]
                if address in self.storm_regs:
                    for _ in range(self.storm_count):
                        if not self.running:
                            return
                        self.sock.sendto(
                            self._ack(
                                req_id, GVCPCommand.PENDING_ACK, struct.pack(">H", 100)
                            ),
                            addr,
                        )
                        time.sleep(0.05)
                    continue  # never a final ack: client must fail bounded
                value = self.registers.get(address, 0)
                self.sock.sendto(
                    self._ack(req_id, GVCPCommand.READ_REG_ACK, struct.pack(">I", value)),
                    addr,
                )
            elif cmd == GVCPCommand.WRITE_REG and len(data) >= 16:
                address, value = struct.unpack(">II", data[8:16])
                if address == REG_NUC_COMMAND and self.nuc_pending_once:
                    # Action register: PENDING first, final ack after execute.
                    self.nuc_pending_once = False
                    self.sock.sendto(
                        self._ack(
                            req_id, GVCPCommand.PENDING_ACK, struct.pack(">H", 200)
                        ),
                        addr,
                    )
                    time.sleep(0.25)
                    self.registers[address] = value
                    self.sock.sendto(self._ack(req_id, GVCPCommand.WRITE_REG_ACK), addr)
                else:
                    self.registers[address] = value
                    if address == REG_FOCUS_SET:
                        self.registers[REG_FOCUS_CURRENT] = value
                    self.sock.sendto(self._ack(req_id, GVCPCommand.WRITE_REG_ACK), addr)


class CyclingReceiver:
    """Endless scripted GVSP blocks so the worker stays STREAMING."""

    def __init__(self) -> None:
        payload = bytearray(COMBINED_BYTES)
        payload[LEADER_BYTES : LEADER_BYTES + 2] = struct.pack("<H", 0x1234)
        self._payload = bytes(payload)
        self._block = 0
        self.stopped = False

    def start(self):
        return 50001

    def stop(self):
        self.stopped = True

    def get_block_with_id(self, timeout=5.0):
        self._block += 1
        return (self._block & 0xFFFF, self._payload)

    def get_stats(self):
        return {
            "packets_received": self._block * 10,
            "packets_dropped": 0,
            "duplicate_packets": 0,
            "blocks_incomplete": 0,
            "block_timeouts": 0,
            "blocks_completed": self._block,
        }


def _driver_config() -> CameraConfig:
    return CameraConfig(
        identity=CameraIdentity(camera_id="cam_loop", serial_number="SN_LOOP"),
        device_identifier="gvcp:SN_LOOP",
        ip_address=CAMERA_IP,
    )


def _make_driver() -> CustomTV46LDriver:
    return CustomTV46LDriver(
        _driver_config(),
        camera_ip=CAMERA_IP,
        local_ip=LOCAL_IP,
        gvcp_factory=lambda: GVCPClient(CAMERA_IP, local_ip=LOCAL_IP),
        receiver_factory=CyclingReceiver,
    )


def _app_camera(camera_id: str) -> AppCameraConfig:
    return AppCameraConfig(
        identity=AppCameraIdentity(
            camera_id=camera_id,
            serial_number="SN_LOOP",
            model="TV46L",
            vendor="Fluke",
        ),
        name=CAMERA_IP,
        metadata={
            "ip_address": CAMERA_IP,
            "grab_timeout_ms": 500,
            "consecutive_fail_limit": 10,
            "reconnect_interval_s": 0.05,
            "reconnect_backoff_factor": 1.0,
            "max_reconnect_attempts": 2,
        },
    )


def _service() -> CameraRuntimeService:
    return CameraRuntimeService(
        cameras_config=CamerasConfig(),
        system_config=SystemConfig(),
        recording_config=RecordingConfig(),
        storage_config=StorageConfig(),
        source_factory=lambda cfg: CustomTV46LDriver(
            cfg,
            local_ip=LOCAL_IP,
            gvcp_factory=lambda: GVCPClient(CAMERA_IP, local_ip=LOCAL_IP),
            receiver_factory=CyclingReceiver,
        ),
    )


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def camera():
    fake = FakeTV46L().start()
    time.sleep(0.05)
    yield fake
    fake.stop()


def _run_worker(qapp, worker, done_signal: str, timeout_s: float = 15.0):
    """Run a worker in a real QThread; return (payloads, failures, thread)."""
    done: list = []
    failed: list = []
    thread = QThread()
    worker.moveToThread(thread)
    getattr(worker, done_signal).connect(lambda *a: done.append(a))
    worker.failed.connect(lambda *a: failed.append(a))
    getattr(worker, done_signal).connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.started.connect(worker.run)
    thread.start()
    deadline = time.time() + timeout_s
    while not done and not failed and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    thread.wait(5000)
    return done, failed, thread


class TestFocusOverLoopback:
    def test_focus_read_while_streaming(self, qapp, camera):
        service = _service()
        camera_id = "cam_loop_focus_read"
        try:
            service.start_camera(_app_camera(camera_id))
            assert service.is_camera_running(camera_id)
            worker = FocusWorker(service, camera_id, None)
            done, failed, thread = _run_worker(qapp, worker, "read_finished")
            assert not failed, f"focus read failed: {failed}"
            assert done == [(camera_id, 150, 1000000, 1000)]
            assert thread.isFinished()
        finally:
            service.shutdown()

    def test_focus_write_and_readback(self, qapp, camera):
        service = _service()
        camera_id = "cam_loop_focus_write"
        try:
            service.start_camera(_app_camera(camera_id))
            worker = FocusWorker(service, camera_id, 2500)
            done, failed, thread = _run_worker(qapp, worker, "write_finished")
            assert not failed, f"focus write failed: {failed}"
            assert done == [(camera_id, 2500, 2500)]
            assert camera.registers[REG_FOCUS_CURRENT] == 2500
            assert thread.isFinished()
        finally:
            service.shutdown()

    def test_panel_reaches_ok_state(self, qapp, camera):
        service = _service()
        camera_id = "cam_loop_focus_panel"
        panel = ImageAcquisitionPanel()
        try:
            service.start_camera(_app_camera(camera_id))
            panel.set_focus_enabled(True)
            panel.set_focus_busy("Reading…")
            worker = FocusWorker(service, camera_id, None)
            worker.read_finished.connect(
                lambda _cid, vmin, vmax, cur: (
                    panel.set_focus_enabled(True),
                    panel.set_focus_state(cur, vmin, vmax),
                )
            )
            done, failed, thread = _run_worker(qapp, worker, "read_finished")
            assert done and not failed
            assert panel._focus_current_label.text() == "1000 mm"
            assert "Ready" in panel._focus_status_label.text()
        finally:
            service.shutdown()


class TestNucOverLoopback:
    def test_nuc_completes_with_pending_and_stream_verified(self, qapp, camera):
        service = _service()
        camera_id = "cam_loop_nuc"
        try:
            service.start_camera(_app_camera(camera_id))
            worker = NucWorker(service, camera_id)
            done, failed, thread = _run_worker(qapp, worker, "finished")
            assert not failed, f"NUC failed: {failed}"
            assert len(done) == 1 and done[0][0] == camera_id
            assert done[0][1] >= 0.0
            assert service.is_camera_running(camera_id)
            assert thread.isFinished()
        finally:
            service.shutdown()

    def test_nuc_repeated_three_times(self, qapp, camera):
        service = _service()
        camera_id = "cam_loop_nuc3"
        try:
            service.start_camera(_app_camera(camera_id))
            for _ in range(3):
                result = service.perform_nuc(camera_id)
                assert result["stream_config"]["packet_delay"] == 10000
                assert result["stream_config"]["fusion_selector"] == 3
            assert service.is_camera_running(camera_id)
        finally:
            service.shutdown()

    def test_panel_reaches_completed_state(self, qapp, camera):
        service = _service()
        camera_id = "cam_loop_nuc_panel"
        panel = ImageAcquisitionPanel()
        try:
            service.start_camera(_app_camera(camera_id))
            panel.set_nuc_enabled(True)
            panel.set_nuc_busy("NUC running…")
            worker = NucWorker(service, camera_id)
            worker.finished.connect(
                lambda _cid, dur: (
                    panel.set_nuc_enabled(True),
                    panel.set_nuc_result(dur),
                )
            )
            done, failed, thread = _run_worker(qapp, worker, "finished")
            assert done and not failed
            assert "OK" in panel._nuc_status_label.text()
        finally:
            service.shutdown()


class TestRuntimeDirectDiagnose:
    def test_diagnose_focus_returns_real_values_while_streaming(self, camera):
        """Runtime-direct probe (§4): same app, same driver, no UI involved."""
        service = _service()
        camera_id = "cam_loop_diag"
        try:
            service.start_camera(_app_camera(camera_id))
            result = service.diagnose_focus(camera_id)
            assert result["camera_id"] == camera_id
            assert result["camera_ip"] == CAMERA_IP
            assert (result["min"], result["max"], result["current"]) == (
                150,
                1000000,
                1000,
            )
            assert result["op_id"].startswith("FOCUS-DIAG-")
            assert result["elapsed_s"] >= 0.0
            assert service.is_camera_running(camera_id)
        finally:
            service.shutdown()

    def test_worker_keeps_caller_op_id(self, qapp):
        """The UI-minted op ID must travel unchanged into the worker."""
        from thermal_monitor.ui.windows.configuration_window import FocusWorker

        worker = FocusWorker(object(), "cam_x", None, op_id="FOCUS-READ-001")
        assert worker._op_id == "FOCUS-READ-001"
        auto = FocusWorker(object(), "cam_x", None)
        assert auto._op_id.startswith("FOCUS-READ-")


class TestControlFailureIsLoud:
    def test_dead_camera_fails_fast_with_reason(self, qapp):
        """No fake camera: every control op must fail LOUDLY and QUICKLY."""
        service = CameraRuntimeService(
            cameras_config=CamerasConfig(),
            system_config=SystemConfig(),
            recording_config=RecordingConfig(),
            storage_config=StorageConfig(),
            source_factory=lambda cfg: CustomTV46LDriver(
                cfg,
                local_ip=LOCAL_IP,
                gvcp_factory=lambda: GVCPClient(
                    "127.0.0.2", local_ip=LOCAL_IP, timeout=0.2
                ),
                receiver_factory=CyclingReceiver,
            ),
        )
        camera_id = "cam_loop_dead"
        started = time.monotonic()
        try:
            service.start_camera(_app_camera(camera_id))
            # 127.0.0.2 refuses connection: start may fail at GVCP connect.
            # Either outcome is fine as long as it is fast and explicit.
            worker = FocusWorker(service, camera_id, None)
            done, failed, thread = _run_worker(qapp, worker, "read_finished")
            assert not done
            assert failed, "dead camera must produce an explicit failure, not silence"
            assert thread.isFinished()
        except Exception as exc:
            assert "GVCP" in str(exc) or "control" in str(exc).lower()
        finally:
            service.shutdown()
        assert time.monotonic() - started < 60.0

    def test_focus_register_storm_fails_loudly_never_silent(self, qapp):
        """A focus register that PENDINGs forever must surface as an ERROR.

        Field mode: one 0x20A1xx register misbehaves while the rest (and
        NUC/stream registers) answer normally. The focus op must fail with
        the PENDING reason, the panel must leave "Reading...", and the
        stream must keep running.
        """
        from thermal_monitor.camera.tv46_gvcp import REG_FOCUS_CURRENT as _CUR

        fake = FakeTV46L(storm_regs={_CUR}).start()
        time.sleep(0.05)
        service = CameraRuntimeService(
            cameras_config=CamerasConfig(),
            system_config=SystemConfig(),
            recording_config=RecordingConfig(),
            storage_config=StorageConfig(),
            source_factory=lambda cfg: CustomTV46LDriver(
                cfg,
                local_ip=LOCAL_IP,
                gvcp_factory=lambda: GVCPClient(
                    CAMERA_IP, local_ip=LOCAL_IP, timeout=0.2, max_pending_wait_s=0.5
                ),
                receiver_factory=CyclingReceiver,
            ),
        )
        camera_id = "cam_loop_storm"
        panel = ImageAcquisitionPanel()
        started = time.monotonic()
        try:
            service.start_camera(_app_camera(camera_id))
            assert service.is_camera_running(camera_id)
            panel.set_focus_enabled(True)
            panel.set_focus_busy("Reading…")
            worker = FocusWorker(service, camera_id, None)
            worker.failed.connect(panel.set_focus_error)
            done, failed, thread = _run_worker(qapp, worker, "read_finished")
            assert not done, "stormed register must not produce a value"
            assert failed, "stormed register must produce an explicit failure"
            assert "PENDING" in failed[0][1], f"reason must name PENDING: {failed}"
            assert thread.isFinished()
            assert "Error" in panel._focus_status_label.text()
            assert "Reading" not in panel._focus_status_label.text()
            # Control storm must not kill acquisition.
            assert service.is_camera_running(camera_id)
        finally:
            service.shutdown()
            fake.stop()
        assert time.monotonic() - started < 30.0
