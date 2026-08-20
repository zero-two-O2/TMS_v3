"""Hardware-dependent validation of the full IR acquisition -> SHM ring -> recording path (Stage 7B/7C).

Chain under test:
    TV46LDriver -> AcquisitionWorker -> SharedMemoryPublisher
        -> SharedMemoryRingBuffer -> RecordingConsumer -> RecordingWriter -> disk

These tests require a physical TV46L camera connected via GigE and the real
MVTec HALCON Python binding. They are marked @pytest.mark.hardware and are
skipped automatically when either is unavailable (the development laptop has
neither and will skip this module cleanly).

Environment variables:
    TV46L_DEVICE:      HALCON device identifier (default: "default")
    TV46L_SERIAL:      Expected camera serial number (optional)
    TV46L_SUSTAINED_S: Duration in seconds for test_sustained_recording_60s
                       (default 0 = skipped; use >= 60 to run)

This validates the IR-only foundation only. No VL/dual-feed, GPU processing,
temperature conversion, ROI/alarm, or GUI involvement.
"""

from __future__ import annotations

import logging
import os
import time
import uuid

import numpy as np
import pytest

from thermal_monitor.camera.acquisition import AcquisitionWorker, AcquisitionState
from thermal_monitor.camera.driver import TV46LDriver
from thermal_monitor.camera.model import CameraConfig, CameraIdentity
from thermal_monitor.camera.shm import create_ring_buffer_and_publisher
from thermal_monitor.offline import RecordingReader, RecordingStatus
from thermal_monitor.services.recording import ContinuousRecordingManager

logger = logging.getLogger(__name__)

EXPECTED_WIDTH = 640
EXPECTED_HEIGHT = 480
EXPECTED_DTYPE = np.uint16
RING_DEPTH = 32
MIN_FPS = 5.0
MAX_FPS = 15.0

REQUIRED_FRAMES = 60
ACQUISITION_BUDGET_S = 30.0
CHUNK_TARGET_BYTES = 8 * 1024 * 1024

_SUSTAINED_S = float(os.environ.get("TV46L_SUSTAINED_S", "0") or 0)


def _get_hardware_config() -> CameraConfig | None:
    device = os.environ.get("TV46L_DEVICE", "default")
    serial = os.environ.get("TV46L_SERIAL", "UNKNOWN")

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
    except Exception as exc:
        logger.warning("Failed to build hardware config: %s", exc)
        return None


def _has_real_halcon() -> bool:
    try:
        import halcon as ha
    except Exception:
        return False
    return hasattr(ha, "open_framegrabber") and hasattr(ha, "grab_image_async")


hardware_config = _get_hardware_config()
pytestmark = pytest.mark.skipif(
    hardware_config is None or not _has_real_halcon(),
    reason="TV46L hardware / MVTec HALCON not available (set TV46L_DEVICE and install MVTec HALCON)",
)


def _run_real_chain(tmp_path, duration_s: float, required_frames: int) -> tuple:
    config = hardware_config
    camera_id = f"{config.identity.camera_id}_7b_{uuid.uuid4().hex[:8]}"

    ring, publisher = create_ring_buffer_and_publisher(
        camera_id, width=EXPECTED_WIDTH, height=EXPECTED_HEIGHT, depth=RING_DEPTH
    )
    driver = TV46LDriver(config)
    worker = AcquisitionWorker(camera_id, driver, publisher, config)
    manager = ContinuousRecordingManager(
        output_dir=tmp_path,
        ring_depth=RING_DEPTH,
        chunk_target_bytes=CHUNK_TARGET_BYTES,
        thermal_width=EXPECTED_WIDTH,
        thermal_height=EXPECTED_HEIGHT,
    )

    active_consumer = None
    try:
        ring_attached, active_consumer = manager.start_recording(
            camera_id,
            f"rec_{camera_id}",
            camera_snapshots=[{"camera_id": camera_id}],
        )

        worker.start()
        assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=15.0), \
            "Worker did not reach ACQUIRING state"

        got_enough = active_consumer.wait_for_frames(required_frames, timeout=duration_s)
        assert got_enough, (
            f"Only {active_consumer.stats().frames_written} frames recorded in "
            f"{duration_s}s (need {required_frames})"
        )

        worker_stats = worker.stats()
        rec_stats = manager.stop_all()[camera_id]
        rec_dir = active_consumer._writer.recording_dir
        active_consumer = None

        worker.stop(timeout=5.0)
        ring.close()
        ring = None

        return camera_id, worker_stats, rec_stats, rec_dir
    finally:
        if active_consumer is not None:
            try:
                manager.abort_all()
            except Exception:
                pass
        try:
            worker.stop(timeout=2.0)
        except Exception:
            pass
        try:
            ring_attached.close()
        except Exception:
            pass
        if ring is not None:
            try:
                ring.close()
            except Exception:
                pass


def _assert_recording(camera_id, worker_stats, rec_stats, rec_dir, sustained: bool = False) -> None:
    expected_frames = rec_stats.frames_written
    assert expected_frames >= 1

    reader = RecordingReader(rec_dir)
    assert reader.status == RecordingStatus.COMPLETE
    assert reader.frame_count == expected_frames
    assert reader.frame_count == rec_stats.frames_written
    assert reader.chunk_count > 1

    sequences = [out.descriptor.sequence for out in reader.iterate()]
    if sustained:
        assert all(b > a for a, b in zip(sequences, sequences[1:])), \
            "Recorded sequences out of order"
    else:
        assert sequences == list(range(expected_frames)), \
            "Recorded acquisition sequences are not contiguous 0..N-1"

    for out in reader.iterate():
        assert out.descriptor.camera_id == camera_id, "Recorded camera_id mismatch"
        assert out.payload.thermal is not None
        assert out.payload.thermal.dtype == EXPECTED_DTYPE
        assert out.payload.thermal.shape == (EXPECTED_HEIGHT, EXPECTED_WIDTH)
        assert out.payload.thermal.nbytes == EXPECTED_WIDTH * EXPECTED_HEIGHT * 2
        assert not out.payload.thermal.flags.writeable

    first = reader.read_frame(reader.entries[0]).payload.thermal
    assert first.min() != first.max(), "Recorded thermal payload has no signal"

    meta = reader.read_frame(reader.entries[0]).descriptor.metadata
    assert "packet_stats" in meta and isinstance(meta["packet_stats"], dict)
    assert "grab_duration_s" in meta

    verify = reader.verify()
    assert verify.status == RecordingStatus.COMPLETE
    assert verify.records_verified == expected_frames
    assert len(verify.failures) == 0, f"CRC failures: {verify.failures}"

    assert worker_stats.packets_lost >= 0
    assert worker_stats.blocks_incomplete >= 0
    assert worker_stats.dropped == 0, "Producer dropped frames during recording"
    assert worker_stats.published >= expected_frames
    assert worker_stats.average_fps >= MIN_FPS, "Producer FPS collapsed (consumer blocking?)"

    assert rec_stats.frames_written == expected_frames
    assert rec_stats.writer_dropped == 0
    assert rec_stats.ring_overwritten >= 0
    assert rec_stats.ring_gaps >= 0
    assert rec_stats.ring_stale >= 0
    assert rec_stats.ring_invalid >= 0

    logger.info(
        "Recording OK: camera=%s frames=%d chunks=%d crc=%d "
        "packets_lost=%d producer_dropped=%d ring_overwritten=%d avg_fps=%.2f",
        camera_id, reader.frame_count, reader.chunk_count, verify.records_verified,
        worker_stats.packets_lost, worker_stats.dropped, rec_stats.ring_overwritten,
        worker_stats.average_fps,
    )


@pytest.mark.hardware
def test_real_ir_ring_to_recording(tmp_path):
    camera_id, worker_stats, rec_stats, rec_dir = _run_real_chain(
        tmp_path, ACQUISITION_BUDGET_S, REQUIRED_FRAMES
    )
    _assert_recording(camera_id, worker_stats, rec_stats, rec_dir)


@pytest.mark.hardware
def test_sustained_recording_60s(tmp_path):
    if _SUSTAINED_S < 60:
        pytest.skip(
            f"TV46L_SUSTAINED_S={_SUSTAINED_S} < 60; "
            "set TV46L_SUSTAINED_S=65 to run the >=60s sustained recording validation"
        )
    required = max(REQUIRED_FRAMES, int(_SUSTAINED_S * 3.5))
    camera_id, worker_stats, rec_stats, rec_dir = _run_real_chain(
        tmp_path, _SUSTAINED_S + 10.0, required
    )
    assert rec_stats.frames_written >= required
    _assert_recording(camera_id, worker_stats, rec_stats, rec_dir, sustained=True)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])