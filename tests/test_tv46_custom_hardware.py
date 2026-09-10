"""Hardware validation gates for the custom TV46L driver (Stage 8B.5/8B.6).

Requires ONE physical TV46L reachable over GigE. Skipped by default; run:

    set TV46L_CUSTOM_IP=192.168.42.11 && python -m pytest tests/test_tv46_custom_hardware.py -v

Optional: TV46L_CUSTOM_NUC=1 to also run the custom one-step NUC gate
(8B.7 records stream silence/recovery; it does NOT choose the final NUC).

These tests NEVER synthesise frames: every assertion is over data actually
received from the camera. HALCON tests are untouched and remain the parity
reference (see tests/test_real_ir_acquisition.py).
"""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

from thermal_monitor.camera.acquisition import AcquisitionWorker, InProcessLatestPublisher
from thermal_monitor.camera.model import CameraConfig, CameraIdentity
from thermal_monitor.camera.tv46_custom import CustomTV46LDriver

pytestmark = pytest.mark.hardware

CUSTOM_IP = os.environ.get("TV46L_CUSTOM_IP", "")
RUN_NUC_GATE = os.environ.get("TV46L_CUSTOM_NUC", "") == "1"

requires_camera = pytest.mark.skipif(
    not CUSTOM_IP, reason="TV46L custom hardware not configured (set TV46L_CUSTOM_IP)"
)


def _config() -> CameraConfig:
    return CameraConfig(
        identity=CameraIdentity(
            camera_id="cam_custom_hw",
            serial_number="HW",
            model="TV46L",
            vendor="Fluke Process Instruments",
        ),
        device_identifier="custom-hw",
        ip_address=CUSTOM_IP,
        grab_timeout_ms=1000,
    )


@requires_camera
class TestCustomSingleCameraGates:
    """Stage 8B.5: discovery/connection, startup, 9 FPS, completeness, IDs."""

    def test_1_discovery_connection_first_frame(self):
        from thermal_monitor.camera.tv46_gvcp import GVCPClient

        gvcp = GVCPClient(CUSTOM_IP)
        gvcp.connect()
        try:
            info = gvcp.discover_once()
            assert info.ip_address
        finally:
            gvcp.close()

        driver = CustomTV46LDriver(_config(), camera_ip=CUSTOM_IP)
        driver.connect()
        try:
            assert driver.is_connected()
            result = driver.grab(5000)
            assert result.thermal is not None
            assert result.thermal.shape == (480, 640)
            assert result.thermal.dtype == np.uint16
        finally:
            driver.disconnect()

    def test_2_continuous_9fps_completeness_and_ids(self):
        driver = CustomTV46LDriver(_config(), camera_ip=CUSTOM_IP)
        driver.connect()
        try:
            duration_s = 20.0
            frames = 0
            first_id = None
            last_id = None
            start = time.perf_counter()
            deadline = start + duration_s
            while time.perf_counter() < deadline:
                result = driver.grab(2000)
                assert result.thermal is not None
                assert result.thermal.shape == (480, 640)
                frame_id = result.frame_id
                assert frame_id is not None
                if first_id is None:
                    first_id = frame_id
                if last_id is not None:
                    assert frame_id > last_id, "frame IDs must progress monotonically"
                last_id = frame_id
                frames += 1
            elapsed = time.perf_counter() - start
            fps = frames / elapsed
            status = driver.get_status()
            print(
                f"\n[CUSTOM-HW] frames={frames} elapsed={elapsed:.1f}s fps={fps:.2f} "
                f"first_id={first_id} last_id={last_id} stats={status}"
            )
            # Nominal 9 FPS; wide gate -- the measurement is the deliverable.
            assert 5.0 <= fps <= 12.0, f"measured {fps:.2f} FPS outside gate"
            assert status["blocks_incomplete"] == 0, status
        finally:
            driver.disconnect()

    def test_3_reconnect_and_clean_shutdown(self):
        driver = CustomTV46LDriver(_config(), camera_ip=CUSTOM_IP)
        driver.connect()
        try:
            before = driver.grab(5000)
            assert before.thermal is not None
            driver.reopen()
            after = driver.grab(5000)
            assert after.thermal is not None
            assert after.frame_id is not None and after.frame_id != before.frame_id
        finally:
            driver.disconnect()
        assert not driver.is_connected()

    def test_4_worker_integration_publishes_ir_frames(self):
        driver = CustomTV46LDriver(_config(), camera_ip=CUSTOM_IP)
        publisher = InProcessLatestPublisher()
        worker = AcquisitionWorker("cam_custom_hw", driver, publisher, _config())
        worker.start()
        try:
            deadline = time.time() + 15.0
            while publisher.latest() is None and time.time() < deadline:
                time.sleep(0.05)
            frame = publisher.latest()
            assert frame is not None
            assert frame.payload.thermal is not None
            # Stage 8D dual-feed: same-block VL correlated by frame ID.
            assert frame.payload.visible is not None
            assert frame.payload.visible.shape == (480, 1280)
            assert (
                frame.descriptor.visible.sequence
                == frame.descriptor.thermal.sequence
            )
            stats = worker.stats()
            print(f"\n[CUSTOM-HW] worker stats={stats}")
            assert stats.published >= 1
        finally:
            worker.stop()


@requires_camera
@pytest.mark.skipif(not RUN_NUC_GATE, reason="Set TV46L_CUSTOM_NUC=1 to run the NUC gate")
class TestCustomNucGate:
    """Stage 8B.7: measure the custom one-step NUC (no verdict yet)."""

    def test_custom_nuc_silence_and_recovery(self):
        driver = CustomTV46LDriver(_config(), camera_ip=CUSTOM_IP)
        driver.connect()
        try:
            assert driver.grab(5000).thermal is not None
            before = driver.read_stream_config()
            t0 = time.monotonic()
            driver.perform_nuc()
            # First post-NUC frame marks recovery; record the silence.
            result = driver.grab(30000)
            silence = time.monotonic() - t0
            assert result.thermal is not None
            after = driver.verify_stream_config(before["packet_delay"])
            print(
                f"\n[CUSTOM-HW-NUC] silence={silence:.1f}s before={before} after={after}"
            )
        finally:
            driver.disconnect()
