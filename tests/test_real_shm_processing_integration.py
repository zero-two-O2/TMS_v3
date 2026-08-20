"""Hardware-dependent validation of the full IR acquisition -> SHM -> processing + recording path (Stage 7D).

Chain under test:
    TV46LDriver -> AcquisitionWorker -> SharedMemoryPublisher
        -> SharedMemoryRingBuffer
            -> ProcessingConsumer -> SimpleProcessingPipeline -> ProcessingResult
            -> RecordingConsumer -> RecordingWriter -> disk   (independent, same producer)

These tests require a physical TV46L camera connected via GigE and the real
MVTec HALCON Python binding. They are marked @pytest.mark.hardware and are
skipped automatically when either is unavailable (the development laptop has
neither and will skip this module cleanly).

Environment variables:
    TV46L_DEVICE:                  HALCON device identifier (default: "default")
    TV46L_SERIAL:                  Expected camera serial number (optional)
    TV46L_CALIBRATION_FILE:        Optional path to a real calibration blob.
                                   When set and loadable, CPU conversion uses the
                                   real calibration LUT; otherwise an identity
                                   LUT (temperature == raw) is used so the
                                   conversion path is still exercised on real
                                   frames deterministically.
    TV46L_PROCESSING_SUSTAINED_S:  Duration in seconds for the optional sustained
                                   processing test (default 0 = skipped; use >= 30)

The short integration test is bounded (~10 s). Sustained acquisition/recording is
validated separately in test_real_shm_recording_integration.py.
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
from thermal_monitor.core.models import AnalysisConfig
from thermal_monitor.offline import RecordingReader, RecordingStatus
from thermal_monitor.processing.consumer import create_processing_consumer
from thermal_monitor.processing.temperature import CachingCalibrationProvider
from thermal_monitor.services.recording import ContinuousRecordingManager

logger = logging.getLogger(__name__)

EXPECTED_WIDTH = 640
EXPECTED_HEIGHT = 480
EXPECTED_DTYPE = np.uint16
RING_DEPTH = 32
MIN_FPS = 5.0
MAX_FPS = 20.0

REQUIRED_FRAMES = 60
PROCESSING_BUDGET_S = 25.0
CHUNK_TARGET_BYTES = 8 * 1024 * 1024

_SUSTAINED_S = float(os.environ.get("TV46L_PROCESSING_SUSTAINED_S", "0") or 0)


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


class _IdentityCalibrationProvider:
    """Calibration provider returning an identity LUT (temperature == raw).

    Used as a deterministic fallback when no real calibration file is supplied;
    exercises the same vectorized CPU LUT conversion path on real frames.
    """

    def __init__(self) -> None:
        self._lut = np.arange(65536, dtype=np.float32)

    def get_calibration(self, camera_id: str) -> np.ndarray:
        return self._lut


def _make_calibration_provider(camera_id: str):
    """Prefer the real calibration blob; fall back to the identity LUT."""
    file = os.environ.get("TV46L_CALIBRATION_FILE")
    if file and os.path.exists(file):
        provider = CachingCalibrationProvider(calibration_file_map={camera_id: file})
        if provider.get_calibration(camera_id) is not None:
            logger.info("Using real calibration LUT from %s", file)
            return provider
        logger.warning("Calibration file %s did not load; using identity LUT", file)
    return _IdentityCalibrationProvider()


class _ResultCollector:
    """Captures lightweight per-result metadata plus the most recent full result.

    Storing lightweight tuples keeps memory bounded even for the sustained run
    (results otherwise hold 640x480 uint16 + float32 images per frame).
    """

    def __init__(self) -> None:
        self.records: list[tuple] = []
        self.last: object | None = None
        self.count = 0

    def __call__(self, result) -> None:
        temp = result.temperature_image
        thermal = result.frame.payload.thermal
        self.records.append((
            result.frame.descriptor.camera_id,
            result.frame.descriptor.sequence,
            temp is not None,
            tuple(temp.shape) if temp is not None else None,
            str(temp.dtype) if temp is not None else None,
            thermal is not None,
            tuple(thermal.shape) if thermal is not None else None,
            str(thermal.dtype) if thermal is not None else None,
        ))
        self.last = result
        self.count += 1


def _run_real_processing_chain(tmp_path, duration_s: float, required_frames: int) -> dict:
    config = hardware_config
    camera_id = f"{config.identity.camera_id}_7d_{uuid.uuid4().hex[:8]}"

    ring, publisher = create_ring_buffer_and_publisher(
        camera_id, width=EXPECTED_WIDTH, height=EXPECTED_HEIGHT, depth=RING_DEPTH
    )
    driver = TV46LDriver(config)
    worker = AcquisitionWorker(camera_id, driver, publisher, config)

    calibration_provider = _make_calibration_provider(camera_id)
    collector = _ResultCollector()

    ring_proc, processing_consumer = create_processing_consumer(
        camera_id=camera_id,
        analysis_config=AnalysisConfig(camera_id=camera_id),
        calibration_provider=calibration_provider,
        consumer_name=f"proc_{camera_id}",
        result_callback=collector,
        ring_depth=RING_DEPTH,
        thermal_width=EXPECTED_WIDTH,
        thermal_height=EXPECTED_HEIGHT,
    )

    manager = ContinuousRecordingManager(
        output_dir=tmp_path,
        ring_depth=RING_DEPTH,
        chunk_target_bytes=CHUNK_TARGET_BYTES,
        thermal_width=EXPECTED_WIDTH,
        thermal_height=EXPECTED_HEIGHT,
    )

    ring_rec = None
    active_recorder = None
    try:
        ring_rec, active_recorder = manager.start_recording(
            camera_id,
            f"rec_{camera_id}",
            camera_snapshots=[{"camera_id": camera_id}],
        )

        processing_consumer.start()
        worker.start()
        assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=15.0), \
            "Worker did not reach ACQUIRING state"

        t0 = time.monotonic()
        got_enough = processing_consumer.wait_for_frames(required_frames, timeout=duration_s)
        elapsed = time.monotonic() - t0
        assert got_enough, (
            f"Only {processing_consumer.stats().frames_processed} frames processed "
            f"in {duration_s:.1f}s (need {required_frames})"
        )

        # Recording must keep up on the same producer independently
        rec_caught_up = active_recorder.wait_for_frames(required_frames, timeout=duration_s)
        assert rec_caught_up, (
            f"Only {active_recorder.stats().frames_written} frames recorded in "
            f"{duration_s:.1f}s (need {required_frames})"
        )

        worker_stats = worker.stats()  # captured while still ACQUIRING
        processing_stats = processing_consumer.stats()
        processing_consumer.stop(timeout=5.0)
        rec_stats = manager.stop_all()[camera_id]
        rec_dir = active_recorder._writer.recording_dir
        active_recorder = None
        worker.stop(timeout=5.0)

        ring_rec.close()
        ring_rec = None
        ring_proc.close()
        ring_proc = None
        ring.close()
        ring = None

        return {
            "camera_id": camera_id,
            "elapsed_s": elapsed,
            "worker_stats": worker_stats,
            "processing_stats": processing_stats,
            "rec_stats": rec_stats,
            "rec_dir": rec_dir,
            "collector": collector,
        }
    finally:
        if active_recorder is not None:
            try:
                manager.abort_all()
            except Exception:
                pass
        try:
            processing_consumer.stop(timeout=2.0)
        except Exception:
            pass
        try:
            worker.stop(timeout=2.0)
        except Exception:
            pass
        if ring_rec is not None:
            try:
                ring_rec.close()
            except Exception:
                pass
        if ring_proc is not None:
            try:
                ring_proc.close()
            except Exception:
                pass
        if ring is not None:
            try:
                ring.close()
            except Exception:
                pass


def _assert_processing_and_recording(m: dict) -> None:
    camera_id = m["camera_id"]
    worker_stats = m["worker_stats"]
    processing_stats = m["processing_stats"]
    rec_stats = m["rec_stats"]
    collector = m["collector"]
    elapsed = m["elapsed_s"]
    frames = collector.count

    assert processing_stats.frames_processed >= 1
    assert processing_stats.frames_failed == 0, f"processing failed frames: {processing_stats.frames_failed}"
    assert processing_stats.errors == 0, f"processing errors: {processing_stats.errors}"
    assert frames == processing_stats.frames_processed

    # Real 640x480 uint16 IR frames + CPU temperature conversion on every result
    sequences = []
    for camera_id_rec, seq, has_temp, temp_shape, temp_dtype, has_th, th_shape, th_dtype in collector.records:
        assert camera_id_rec == camera_id, "camera_id not preserved into ProcessingResult"
        assert has_th, "frame has no thermal payload"
        assert th_shape == (EXPECTED_HEIGHT, EXPECTED_WIDTH), f"thermal shape {th_shape}"
        assert th_dtype == str(np.dtype(EXPECTED_DTYPE)), f"thermal dtype {th_dtype}"
        assert has_temp, "temperature_image missing after CPU conversion"
        assert temp_shape == (EXPECTED_HEIGHT, EXPECTED_WIDTH), f"temperature_image shape {temp_shape}"
        assert temp_dtype == str(np.dtype(np.float32)), f"temperature_image dtype {temp_dtype}"
        sequences.append(seq)

    assert sequences == sorted(sequences), "processing sequences out of order"
    assert len(sequences) == len(set(sequences)), "duplicate processing sequences"

    # Deep-check the most recent full ProcessingResult
    last = collector.last
    assert last is not None
    assert last.frame.descriptor.camera_id == camera_id
    assert last.frame.payload.thermal is not None
    assert not last.frame.payload.thermal.flags.writeable
    assert last.analysis_result.camera_id == camera_id
    assert last.analysis_result.frame_sequence == last.frame.descriptor.sequence
    assert last.temperature_image is not None
    assert last.temperature_image.shape == (EXPECTED_HEIGHT, EXPECTED_WIDTH)
    assert last.temperature_image.dtype == np.float32
    assert last.processing_time_ms >= 0

    # Processing FPS measurable and bounded by the camera rate
    fps = frames / max(elapsed, 1e-9)
    assert MIN_FPS <= fps <= MAX_FPS, f"processing FPS out of range: {fps:.2f}"
    assert processing_stats.average_processing_time_ms > 0

    # Acquisition never stopped because processing was running
    assert worker_stats.state == AcquisitionState.ACQUIRING, \
        f"worker state {worker_stats.state} while processing"
    assert worker_stats.dropped == 0, "producer dropped frames while processing"
    assert worker_stats.published >= frames
    assert worker_stats.average_fps >= MIN_FPS

    # Recording continued independently on the same producer
    assert rec_stats.frames_written >= 1
    reader = RecordingReader(m["rec_dir"])
    assert reader.status == RecordingStatus.COMPLETE
    assert reader.frame_count == rec_stats.frames_written
    recorded_sequences = [out.descriptor.sequence for out in reader.iterate()]
    assert recorded_sequences == list(range(rec_stats.frames_written)), \
        "recorded sequences not contiguous"
    for out in reader.iterate():
        assert out.descriptor.camera_id == camera_id, "recorded camera_id mismatch"
        assert out.payload.thermal is not None
        assert out.payload.thermal.dtype == EXPECTED_DTYPE
        assert out.payload.thermal.shape == (EXPECTED_HEIGHT, EXPECTED_WIDTH)
        assert not out.payload.thermal.flags.writeable

    verify = reader.verify()
    assert verify.status == RecordingStatus.COMPLETE
    assert len(verify.failures) == 0, f"CRC failures: {verify.failures}"

    logger.info(
        "Processing+Recording OK: camera=%s processed=%d recorded=%d fps=%.2f "
        "avg_proc_ms=%.2f packets_lost=%d producer_dropped=%d ring_overwritten=%d",
        camera_id, frames, rec_stats.frames_written, fps,
        processing_stats.average_processing_time_ms,
        worker_stats.packets_lost, worker_stats.dropped, rec_stats.ring_overwritten,
    )


@pytest.mark.hardware
def test_real_ir_ring_to_processing_and_recording(tmp_path):
    """Real TV46L -> SHM -> ProcessingConsumer + RecordingConsumer (bounded ~10 s)."""
    m = _run_real_processing_chain(tmp_path, PROCESSING_BUDGET_S, REQUIRED_FRAMES)
    _assert_processing_and_recording(m)


@pytest.mark.hardware
def test_sustained_processing(tmp_path):
    """Optional sustained processing+recording run (TV46L_PROCESSING_SUSTAINED_S >= 30)."""
    if _SUSTAINED_S < 30:
        pytest.skip(
            f"TV46L_PROCESSING_SUSTAINED_S={_SUSTAINED_S} < 30; "
            "set TV46L_PROCESSING_SUSTAINED_S=35 to run the sustained processing validation"
        )
    required = max(REQUIRED_FRAMES, int(_SUSTAINED_S * 3.5))
    m = _run_real_processing_chain(tmp_path, _SUSTAINED_S + 10.0, required)
    assert m["processing_stats"].frames_processed >= required
    assert m["rec_stats"].frames_written >= required
    _assert_processing_and_recording(m)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])