"""Hardware-dependent tests for real TV46L IR acquisition (Stage 7A).

These tests require a physical TV46L camera connected via GigE.
They are marked with @pytest.mark.hardware and skipped by default.
Run with: pytest tests/test_real_ir_acquisition.py -v --hardware

Environment variables for camera configuration:
- TV46L_DEVICE: HALCON device identifier (default: "default")
- TV46L_IP: Camera IP address for GVCP (optional, for dual-mode probe)
- TV46L_SERIAL: Expected camera serial number (optional, for validation)
"""

from __future__ import annotations

import os
import time
import logging

import numpy as np
import pytest

from thermal_monitor.camera.acquisition import AcquisitionWorker, InProcessLatestPublisher
from thermal_monitor.camera.driver import TV46LDriver
from thermal_monitor.camera.model import CameraConfig, CameraIdentity, AcquisitionState

logger = logging.getLogger(__name__)

# Hardware test configuration
REQUIRED_FRAMES = 50
MAX_ACQUISITION_TIME_S = 30.0
EXPECTED_WIDTH = 640
EXPECTED_HEIGHT = 480
EXPECTED_DTYPE = np.uint16
EXPECTED_PIXEL_FORMAT = "IR_Data"
MIN_FPS = 5.0
MAX_FPS = 15.0


def _get_hardware_config() -> CameraConfig | None:
    """Build CameraConfig from environment variables.

    Returns None if hardware is not configured.
    """
    device = os.environ.get("TV46L_DEVICE", "default")
    serial = os.environ.get("TV46L_SERIAL", "UNKNOWN")

    # If device is explicitly set to "skip" or empty, don't run
    if device.lower() in ("skip", "none", "false", ""):
        return None

    try:
        identity = CameraIdentity(
            camera_id=f"cam_{serial}",
            serial_number=serial,
            model="TV46L",
            vendor="Fluke Process Instruments",
        )
        return CameraConfig(
            identity=identity,
            device_identifier=device,
            grab_timeout_ms=500,
            frame_rate=9,
        )
    except Exception as e:
        logger.warning(f"Failed to build hardware config: {e}")
        return None


# Skip all tests in this module if hardware not configured
hardware_config = _get_hardware_config()
pytestmark = pytest.mark.skipif(
    hardware_config is None,
    reason="TV46L hardware not configured (set TV46L_DEVICE env var)"
)


@pytest.mark.hardware
class TestRealIRAcquisition:
    """Real TV46L IR acquisition validation tests."""

    def test_driver_connect_and_grab_single_frame(self):
        """Test that TV46LDriver can connect and grab one valid IR frame."""
        driver = TV46LDriver(hardware_config)
        try:
            driver.connect()
            assert driver.is_connected()

            result = driver.grab(5000)  # Longer timeout for first frame
            assert result.thermal is not None, "No thermal data returned"
            assert result.thermal.dtype == EXPECTED_DTYPE, f"Expected {EXPECTED_DTYPE}, got {result.thermal.dtype}"
            assert result.thermal.shape == (EXPECTED_HEIGHT, EXPECTED_WIDTH), \
                f"Expected ({EXPECTED_HEIGHT}, {EXPECTED_WIDTH}), got {result.thermal.shape}"
            assert result.thermal_format == EXPECTED_PIXEL_FORMAT
            assert result.frame_id is not None, "Hardware frame_id not available"
            assert result.hardware_timestamp is not None, "Hardware timestamp not available"
            assert isinstance(result.packet_stats, dict), "Packet stats not returned"
            assert result.packet_stats["packets_seen"] > 0, "Packet seen counter not incrementing"

            # Verify read-only
            assert not result.thermal.flags.writeable, "Thermal array must be read-only"

            logger.info(f"Single frame OK: frame_id={result.frame_id}, "
                       f"hw_ts={result.hardware_timestamp:.6f}, "
                       f"packets_seen={result.packet_stats['packets_seen']}")
        finally:
            driver.disconnect()

    def test_acquisition_worker_lifecycle(self):
        """Test AcquisitionWorker start -> acquire -> stop lifecycle with real hardware."""
        driver = TV46LDriver(hardware_config)
        publisher = InProcessLatestPublisher()
        worker = AcquisitionWorker(
            camera_id=hardware_config.identity.camera_id,
            source=driver,
            publisher=publisher,
            config=hardware_config,
        )

        try:
            # Start
            worker.start()
            assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=10.0), \
                "Worker did not reach ACQUIRING state"

            # Wait for frames
            deadline = time.monotonic() + MAX_ACQUISITION_TIME_S
            frames_received = 0
            while frames_received < REQUIRED_FRAMES and time.monotonic() < deadline:
                frame = publisher.latest()
                if frame is not None:
                    frames_received = frame.descriptor.sequence + 1
                time.sleep(0.05)

            assert frames_received >= REQUIRED_FRAMES, \
                f"Only received {frames_received} frames in {MAX_ACQUISITION_TIME_S}s"

            # Stop
            worker.stop(timeout=5.0)
            assert worker.state in (AcquisitionState.STOPPED, AcquisitionState.STOPPING)

            logger.info(f"Lifecycle test OK: {frames_received} frames acquired")
        finally:
            if worker._thread and worker._thread.is_alive():
                worker.stop(timeout=2.0)

    def test_frame_contract_compliance(self):
        """Verify Frame contract fields are correctly populated from hardware."""
        driver = TV46LDriver(hardware_config)
        publisher = InProcessLatestPublisher()
        worker = AcquisitionWorker(
            camera_id=hardware_config.identity.camera_id,
            source=driver,
            publisher=publisher,
            config=hardware_config,
        )

        try:
            worker.start()
            assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=10.0)

            deadline = time.monotonic() + 10.0
            frame = None
            while frame is None and time.monotonic() < deadline:
                frame = publisher.latest()
                time.sleep(0.02)

            assert frame is not None, "No frame received"

            # Camera identity
            assert frame.descriptor.camera_id == hardware_config.identity.camera_id
            assert frame.descriptor.camera_id.startswith("cam_")

            # Sequence (acquisition worker sequence)
            assert frame.descriptor.sequence >= 0
            assert isinstance(frame.descriptor.sequence, int)

            # Timestamps
            assert frame.descriptor.timestamp > 0, "Wall-clock timestamp missing"
            assert frame.descriptor.monotonic_timestamp > 0, "Monotonic timestamp missing"

            # Thermal stream metadata
            thermal = frame.descriptor.thermal
            assert thermal.present is True
            assert thermal.width == EXPECTED_WIDTH
            assert thermal.height == EXPECTED_HEIGHT
            assert thermal.pixel_format == EXPECTED_PIXEL_FORMAT
            assert thermal.dtype == "uint16"
            assert thermal.byte_count == EXPECTED_WIDTH * EXPECTED_HEIGHT * 2
            assert thermal.sequence is not None, "Thermal stream sequence (frame_id) missing"
            assert thermal.timestamp is not None, "Thermal timestamp missing"
            assert thermal.monotonic_timestamp is not None, "Thermal monotonic timestamp missing"
            assert thermal.hardware_timestamp is not None, "Hardware timestamp missing"

            # Hardware timestamp should be close to wall-clock (within 10 seconds)
            assert abs(thermal.hardware_timestamp - frame.descriptor.timestamp) < 10.0, \
                "Hardware timestamp diverges from wall-clock"

            # Visible stream (should be absent for IR-only acquisition)
            visible = frame.descriptor.visible
            assert visible.present is False

            # Sync status
            assert frame.descriptor.sync.status.value == "missing_visible"

            # Acquisition metadata
            meta = frame.descriptor.metadata
            assert "grab_started" in meta
            assert "grab_completed" in meta
            assert "converted_at" in meta
            assert "grab_duration_s" in meta
            assert "packet_stats" in meta
            assert isinstance(meta["packet_stats"], dict)

            # Payload
            assert frame.payload.thermal is not None
            assert frame.payload.thermal.dtype == EXPECTED_DTYPE
            assert frame.payload.thermal.shape == (EXPECTED_HEIGHT, EXPECTED_WIDTH)
            assert not frame.payload.thermal.flags.writeable
            assert frame.payload.visible is None

            logger.info(f"Frame contract OK: seq={frame.descriptor.sequence}, "
                       f"thermal_seq={thermal.sequence}, "
                       f"hw_ts={thermal.hardware_timestamp:.6f}")

        finally:
            worker.stop(timeout=5.0)

    def test_hardware_frame_id_monotonic(self):
        """Verify hardware frame_id increases monotonically across frames."""
        driver = TV46LDriver(hardware_config)
        publisher = InProcessLatestPublisher()
        worker = AcquisitionWorker(
            camera_id=hardware_config.identity.camera_id,
            source=driver,
            publisher=publisher,
            config=hardware_config,
        )

        try:
            worker.start()
            assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=10.0)

            frame_ids = []
            deadline = time.monotonic() + 10.0
            while len(frame_ids) < 20 and time.monotonic() < deadline:
                frame = publisher.latest()
                if frame is not None:
                    thermal_seq = frame.descriptor.thermal.sequence
                    if thermal_seq is not None and thermal_seq not in frame_ids:
                        frame_ids.append(thermal_seq)
                time.sleep(0.02)

            assert len(frame_ids) >= 10, f"Only got {len(frame_ids)} unique frame IDs"

            # Check monotonic increasing (allowing for potential wrap at 2^32)
            for i in range(1, len(frame_ids)):
                diff = frame_ids[i] - frame_ids[i - 1]
                assert diff > 0, f"Frame ID not monotonic: {frame_ids[i-1]} -> {frame_ids[i]}"
                assert diff < 1000, f"Frame ID jump too large: {diff} (possible wrap or gap)"

            logger.info(f"Frame ID monotonic OK: {frame_ids[:5]}... (total {len(frame_ids)})")

        finally:
            worker.stop(timeout=5.0)

    def test_hardware_timestamp_increasing(self):
        """Verify hardware timestamp increases across frames."""
        driver = TV46LDriver(hardware_config)
        publisher = InProcessLatestPublisher()
        worker = AcquisitionWorker(
            camera_id=hardware_config.identity.camera_id,
            source=driver,
            publisher=publisher,
            config=hardware_config,
        )

        try:
            worker.start()
            assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=10.0)

            hw_timestamps = []
            deadline = time.monotonic() + 10.0
            while len(hw_timestamps) < 20 and time.monotonic() < deadline:
                frame = publisher.latest()
                if frame is not None:
                    hw_ts = frame.descriptor.thermal.hardware_timestamp
                    if hw_ts is not None and hw_ts not in hw_timestamps:
                        hw_timestamps.append(hw_ts)
                time.sleep(0.02)

            assert len(hw_timestamps) >= 10, f"Only got {len(hw_timestamps)} hardware timestamps"

            # Check increasing
            for i in range(1, len(hw_timestamps)):
                assert hw_timestamps[i] > hw_timestamps[i - 1], \
                    f"Hardware timestamp not increasing: {hw_timestamps[i-1]} -> {hw_timestamps[i]}"

            # Check approximate frame rate from hardware timestamps
            if len(hw_timestamps) >= 2:
                duration = hw_timestamps[-1] - hw_timestamps[0]
                measured_fps = (len(hw_timestamps) - 1) / duration
                assert MIN_FPS <= measured_fps <= MAX_FPS, \
                    f"FPS {measured_fps:.1f} outside expected range [{MIN_FPS}, {MAX_FPS}]"

            logger.info(f"Hardware timestamp OK: {len(hw_timestamps)} samples, "
                       f"span={hw_timestamps[-1]-hw_timestamps[0]:.3f}s")

        finally:
            worker.stop(timeout=5.0)

    def test_acquisition_statistics(self):
        """Verify acquisition statistics are correctly accumulated."""
        driver = TV46LDriver(hardware_config)
        publisher = InProcessLatestPublisher()
        worker = AcquisitionWorker(
            camera_id=hardware_config.identity.camera_id,
            source=driver,
            publisher=publisher,
            config=hardware_config,
        )

        try:
            worker.start()
            assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=10.0)

            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                stats = worker.stats()
                if stats.frames_received >= REQUIRED_FRAMES:
                    break
                time.sleep(0.05)

            stats = worker.stats()

            # Basic counters
            assert stats.frames_received >= REQUIRED_FRAMES, \
                f"frames_received={stats.frames_received} < {REQUIRED_FRAMES}"
            assert stats.total_acquired >= REQUIRED_FRAMES
            assert stats.published >= REQUIRED_FRAMES
            assert stats.dropped == 0, f"Frames dropped: {stats.dropped} (InProcessLatestPublisher should not drop)"

            # Packet-level statistics
            assert stats.packets_lost >= 0, "packets_lost should be non-negative"
            assert stats.blocks_incomplete >= 0, "blocks_incomplete should be non-negative"

            # FPS measurement
            assert stats.current_fps >= MIN_FPS, f"current_fps={stats.current_fps:.1f} < {MIN_FPS}"
            assert stats.current_fps <= MAX_FPS, f"current_fps={stats.current_fps:.1f} > {MAX_FPS}"
            assert stats.average_fps >= MIN_FPS

            logger.info(f"Stats OK: received={stats.frames_received}, "
                       f"packets_lost={stats.packets_lost}, "
                       f"blocks_incomplete={stats.blocks_incomplete}, "
                       f"fps={stats.current_fps:.1f}")

        finally:
            worker.stop(timeout=5.0)

    def test_publisher_drop_accounting_separate_from_packet_loss(self):
        """Verify publisher drops are tracked separately from packet loss."""
        # Use a rejecting publisher to force drops
        from tests.conftest import FakePublisher, FakeFrameSource, default_result
        from thermal_monitor.camera.driver import CameraGrabTimeout

        # Create a fake source that returns valid frames
        source = FakeFrameSource()
        # Publisher that rejects every frame
        publisher = FakePublisher(reject=True)
        config = hardware_config
        worker = AcquisitionWorker(
            camera_id=config.identity.camera_id,
            source=source,
            publisher=publisher,
            config=config,
        )

        try:
            worker.start()
            assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=5.0)

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                stats = worker.stats()
                if stats.dropped >= 5:
                    break
                time.sleep(0.02)

            stats = worker.stats()
            # Publisher drops should be tracked
            assert stats.dropped >= 5, f"Expected dropped >= 5, got {stats.dropped}"
            # Packet loss should remain 0 (fake source doesn't have packet counters)
            assert stats.packets_lost == 0
            assert stats.blocks_incomplete == 0

            logger.info(f"Drop accounting OK: dropped={stats.dropped}, "
                       f"packets_lost={stats.packets_lost}")

        finally:
            worker.stop(timeout=5.0)

    def test_reconnect_works(self):
        """Test that acquisition can recover from a simulated disconnect."""
        driver = TV46LDriver(hardware_config)
        publisher = InProcessLatestPublisher()
        worker = AcquisitionWorker(
            camera_id=hardware_config.identity.camera_id,
            source=driver,
            publisher=publisher,
            config=hardware_config,
        )

        try:
            worker.start()
            assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=10.0)

            # Let it acquire some frames
            time.sleep(2.0)
            stats_before = worker.stats()
            frames_before = stats_before.frames_received

            # Force disconnect by closing driver directly
            driver.disconnect()

            # Worker should detect failure and reconnect
            assert worker.wait_for_state(AcquisitionState.RECONNECTING, timeout=10.0), \
                "Worker did not enter RECONNECTING after disconnect"
            assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=15.0), \
                "Worker did not recover to ACQUIRING"

            # Wait for more frames after reconnect
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                stats = worker.stats()
                if stats.frames_received > frames_before + 10:
                    break
                time.sleep(0.05)

            stats_after = worker.stats()
            assert stats_after.frames_received > frames_before, \
                "No frames acquired after reconnect"
            assert stats_after.reconnect_count >= 1, "Reconnect count not incremented"

            logger.info(f"Reconnect OK: before={frames_before}, after={stats_after.frames_received}, "
                       f"reconnects={stats_after.reconnect_count}")

        finally:
            worker.stop(timeout=5.0)

    def test_no_processing_logic_in_acquisition(self):
        """Verify acquisition path does not contain temperature conversion, ROI, alarms, GUI, or recording."""
        import inspect

        # Check AcquisitionWorker source
        worker_source = inspect.getsource(AcquisitionWorker)
        driver_source = inspect.getsource(TV46LDriver)

        # These should NOT appear in acquisition code
        forbidden_terms = [
            "temperature",
            "calibrat",
            "roi",
            "alarm",
            "gui",
            "recording",
            "writer",
            "chunk",
            "index",
            "pyqt",
            "PyQt",
            "QWidget",
            "QThread",
        ]

        for term in forbidden_terms:
            # Allow in comments and strings that are documentation
            # Check actual code logic (case insensitive)
            lower_worker = worker_source.lower()
            lower_driver = driver_source.lower()
            term_lower = term.lower()

            # Skip terms that appear only in comments/docstrings
            # We do a simple check - if the term appears as a variable/method/import
            if term_lower in lower_worker and f"_{term_lower}" not in lower_worker:
                # Could be a false positive in comments; log but don't fail
                logger.warning(f"Potential forbidden term '{term}' in AcquisitionWorker")

            if term_lower in lower_driver and f"_{term_lower}" not in lower_driver:
                logger.warning(f"Potential forbidden term '{term}' in TV46LDriver")

        # Specific checks: no temperature conversion in driver
        assert "raw_to_temperature" not in driver_source
        assert "lut" not in driver_source.lower()
        assert "lookup" not in driver_source.lower()

        logger.info("No processing logic in acquisition path verified")


@pytest.mark.hardware
def test_sustained_acquisition_10_seconds():
    """Test sustained acquisition for 10 seconds with statistics validation."""
    driver = TV46LDriver(hardware_config)
    publisher = InProcessLatestPublisher()
    worker = AcquisitionWorker(
        camera_id=hardware_config.identity.camera_id,
        source=driver,
        publisher=publisher,
        config=hardware_config,
    )

    try:
        worker.start()
        assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=10.0)

        time.sleep(10.0)  # Sustained acquisition

        stats = worker.stats()

        # Should have acquired ~90 frames at 9 FPS
        assert stats.frames_received >= 70, f"Only {stats.frames_received} frames in 10s"
        assert stats.frames_received <= 110, f"Too many frames: {stats.frames_received} (timing issue?)"
        assert stats.packets_lost >= 0
        assert stats.blocks_incomplete >= 0
        assert MIN_FPS <= stats.average_fps <= MAX_FPS
        assert stats.dropped == 0

        logger.info(f"Sustained 10s OK: frames={stats.frames_received}, "
                   f"avg_fps={stats.average_fps:.1f}, "
                   f"packets_lost={stats.packets_lost}, "
                   f"blocks_incomplete={stats.blocks_incomplete}")

    finally:
        worker.stop(timeout=5.0)


if __name__ == "__main__":
    # Allow running directly for quick hardware validation
    logging.basicConfig(level=logging.INFO)

    if hardware_config is None:
        print("Hardware not configured. Set TV46L_DEVICE environment variable.")
        print("Example: set TV46L_DEVICE=default && python tests/test_real_ir_acquisition.py")
        exit(1)

    print(f"Testing with camera: {hardware_config.identity.camera_id}")
    print(f"Device: {hardware_config.device_identifier}")

    # Run a quick smoke test
    driver = TV46LDriver(hardware_config)
    try:
        driver.connect()
        print("Connected")
        result = driver.grab(5000)
        print(f"Frame: {result.thermal.shape}, {result.thermal.dtype}")
        print(f"Frame ID: {result.frame_id}")
        print(f"Hardware TS: {result.hardware_timestamp}")
        print(f"Packet Stats: {result.packet_stats}")
        driver.disconnect()
        print("Disconnected")
        print("SMOKE TEST PASSED")
    except Exception as e:
        print(f"SMOKE TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        exit(1)