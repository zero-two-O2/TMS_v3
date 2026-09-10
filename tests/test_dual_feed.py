"""Stage 8D dual-feed tests: combined IR+VL from GVSP block to consumers.

Covers the Part 1 required list at unit/integration level (hardware soak,
parity and NUC are separate phases):
parsed payload, IR/VL extraction, malformed + truncated rejection,
IR/VL correlation, latest-frame + timeout behavior (already covered for the
shared reassembly path in test_tv46_gvsp.py), worker integration, SHM
integration, consumer integration, and dual-feed recording round-trip.

VL contract (canonical): raw YUYV packing of the YUV422_8 family,
(480, 1280) uint8, pixel_format "YUV422_8", same hardware frame ID as the IR
plane from the same GVSP block. No RGB or display conversion anywhere in
this path.
"""

from __future__ import annotations

import struct
import time
import uuid

import numpy as np
import pytest

from thermal_monitor.camera.acquisition import (
    AcquisitionWorker,
    InProcessLatestPublisher,
)
from thermal_monitor.camera.model import CameraConfig, CameraIdentity
from thermal_monitor.camera.shm import create_ring_buffer_and_publisher
from thermal_monitor.camera.tv46_custom import CustomTV46LDriver
from thermal_monitor.camera.tv46_gvsp import (
    COMBINED_BYTES,
    IR_BYTES,
    LEADER_BYTES,
    VL_BYTES,
    parse_combined_payload,
)
from thermal_monitor.core.frame import (
    Frame,
    FrameDescriptor,
    FramePayload,
    StreamMetadata,
    SyncInfo,
    SyncStatus,
)
from thermal_monitor.core.models import AnalysisConfig


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _combined_payload(ir_first: int = 0x1234, vl_fill: int = 0xAB) -> bytes:
    payload = bytearray(COMBINED_BYTES)
    payload[LEADER_BYTES : LEADER_BYTES + 2] = struct.pack("<H", ir_first)
    vl_start = LEADER_BYTES + IR_BYTES
    payload[vl_start : vl_start + VL_BYTES] = bytes([vl_fill]) * VL_BYTES
    return bytes(payload)


class FakeGVCP:
    def __init__(self, registers=None):
        self.registers = dict(registers or {})
        self.writes: list[tuple[int, int]] = []

    def connect(self):
        return True

    def close(self):
        pass

    def control_switchover(self, key=2):
        return True

    def read_register(self, address):
        return self.registers.get(address, 0)

    def write_register(self, address, value):
        self.writes.append((address, value))
        self.registers[address] = value
        return True

    def read_memory(self, address, length):
        return b"\x00" * length

    def heartbeat_ping(self):
        return True


def _registers() -> dict:
    return {0x0D08: 10000, 0x0D18: 0xC0A82A64, 0x0D00: 54321, 0x0D04: 1500, 0x10A110: 3}


class FakeReceiver:
    def __init__(self, blocks=()):
        self._blocks = list(blocks)

    def start(self):
        return 50001

    def stop(self):
        pass

    def get_block_with_id(self, timeout=5.0):
        if self._blocks:
            return self._blocks.pop(0)
        time.sleep(min(timeout, 0.05))
        return None

    def get_stats(self):
        return {
            "packets_received": 100,
            "packets_dropped": 0,
            "duplicate_packets": 0,
            "blocks_incomplete": 0,
            "block_timeouts": 0,
            "blocks_completed": 1,
        }


def _driver(blocks):
    cfg = CameraConfig(
        identity=CameraIdentity(camera_id="cam_dual", serial_number="SN-D"),
        device_identifier="x",
        ip_address="192.168.42.100",
    )
    return CustomTV46LDriver(
        cfg,
        camera_ip="192.168.42.100",
        local_ip="192.168.42.100",
        gvcp_factory=lambda: FakeGVCP(registers=_registers()),
        receiver_factory=lambda: FakeReceiver(blocks=list(blocks)),
    )


def _dual_frame(camera_id: str, sequence: int, ir_value: int = 1000) -> Frame:
    thermal = np.full((480, 640), ir_value, dtype=np.uint16)
    thermal.setflags(write=False)
    visible = np.full((480, 1280), 0xAB, dtype=np.uint8)
    visible.setflags(write=False)
    descriptor = FrameDescriptor(
        camera_id=camera_id,
        sequence=sequence,
        timestamp=1000.0 + sequence,
        monotonic_timestamp=100.0 + sequence,
        thermal=StreamMetadata(
            present=True, width=640, height=480, pixel_format="IR_Data",
            dtype="uint16", byte_count=thermal.nbytes, sequence=sequence,
        ),
        visible=StreamMetadata(
            present=True, width=1280, height=480, pixel_format="YUV422_8",
            dtype="uint8", byte_count=visible.nbytes, sequence=sequence,
        ),
        sync=SyncInfo(status=SyncStatus.SYNCHRONIZED, time_delta=0.0),
        metadata={},
    )
    return Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal, visible=visible))


# ─── Payload parsing ─────────────────────────────────────────────────────────


class TestCombinedPayload:
    def test_ir_and_vl_extraction(self):
        frame = parse_combined_payload(_combined_payload(0x1234, 0xAB), 7)
        assert frame.block_id == 7
        assert frame.ir.shape == (480, 640) and frame.ir.dtype == np.uint16
        assert int(frame.ir[0, 0]) == 0x1234
        assert frame.vl.shape == (480, 1280) and frame.vl.dtype == np.uint8
        assert int(frame.vl[0, 0]) == 0xAB
        assert int(frame.vl[-1, -1]) == 0xAB

    def test_truncated_payload_rejected(self):
        with pytest.raises(ValueError):
            parse_combined_payload(bytes(COMBINED_BYTES - 100), 1)
        with pytest.raises(ValueError):
            parse_combined_payload(bytes(COMBINED_BYTES + 1), 1)
        with pytest.raises(ValueError):
            parse_combined_payload(bytes(COMBINED_BYTES + 5), 1)

    def test_malformed_payload_rejected(self):
        with pytest.raises(ValueError):
            parse_combined_payload(b"", 1)


# ─── Worker correlation ──────────────────────────────────────────────────────


class TestWorkerCorrelation:
    def test_one_block_yields_one_correlated_frame(self):
        driver = _driver([(41, _combined_payload(0x1234, 0xAB))])
        publisher = InProcessLatestPublisher()
        worker = AcquisitionWorker(
            "cam_dual",
            driver,
            publisher,
            CameraConfig(
                identity=CameraIdentity(camera_id="cam_dual", serial_number="SN-D")
            ),
        )
        worker.start()
        try:
            deadline = time.time() + 5.0
            while publisher.latest() is None and time.time() < deadline:
                time.sleep(0.02)
            frame = publisher.latest()
            stats = worker.stats()
        finally:
            worker.stop()
        assert frame is not None and stats.published >= 1
        # Same-block correlation: identical hardware sequence on both planes.
        assert frame.descriptor.thermal.sequence == 41
        assert frame.descriptor.visible.sequence == 41
        assert frame.descriptor.visible.present is True
        assert frame.descriptor.visible.pixel_format == "YUV422_8"
        assert frame.descriptor.sync.status == SyncStatus.SYNCHRONIZED
        # Payloads are the exact planes from the block.
        assert int(frame.payload.thermal[0, 0]) == 0x1234
        assert int(frame.payload.visible[0, 0]) == 0xAB
        assert frame.payload.visible.shape == (480, 1280)

    def test_correlation_holds_across_blocks(self):
        blocks = [(i, _combined_payload(100 + i, i % 256)) for i in range(3)]
        driver = _driver(blocks)
        publisher = InProcessLatestPublisher()
        worker = AcquisitionWorker(
            "cam_dual",
            driver,
            publisher,
            CameraConfig(
                identity=CameraIdentity(camera_id="cam_dual", serial_number="SN-D")
            ),
        )
        worker.start()
        try:
            deadline = time.time() + 5.0
            while worker.stats().published < 3 and time.time() < deadline:
                time.sleep(0.02)
            frame = publisher.latest()
            published = worker.stats().published
        finally:
            worker.stop()
        assert published == 3
        assert frame is not None
        hw = frame.descriptor.thermal.sequence
        assert frame.descriptor.visible.sequence == hw
        assert int(frame.payload.thermal[0, 0]) == 100 + hw
        assert int(frame.payload.visible[0, 0]) == hw % 256


# ─── SHM round-trip ──────────────────────────────────────────────────────────


class TestShmDualFeed:
    def test_dual_frame_round_trips_byte_exact(self):
        camera_id = f"camdual_{uuid.uuid4().hex[:8]}"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, dual_feed=True)
        try:
            frame = _dual_frame(camera_id, 9, ir_value=4321)
            result = publisher.publish(frame)
            assert result.accepted is True
            consumer = ring.consumer("test")
            try:
                view = consumer.latest()
                assert view is not None
                assert np.array_equal(view.thermal(), frame.payload.thermal)
                assert np.array_equal(view.visible(), frame.payload.visible)
                assert view.visible().shape == (480, 1280)
                assert view.descriptor.visible.sequence == 9
                assert view.descriptor.sync.status == SyncStatus.SYNCHRONIZED
            finally:
                consumer.close()
        finally:
            ring.close()

    def test_ir_only_frame_into_dual_ring(self):
        """Backward compatibility: a visible=None frame publishes fine."""
        camera_id = f"camdual_{uuid.uuid4().hex[:8]}"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, dual_feed=True)
        try:
            thermal = np.zeros((480, 640), dtype=np.uint16)
            thermal.setflags(write=False)
            frame = Frame(
                descriptor=FrameDescriptor(
                    camera_id=camera_id,
                    sequence=0,
                    timestamp=1.0,
                    monotonic_timestamp=1.0,
                    thermal=StreamMetadata(present=True, width=640, height=480),
                    visible=StreamMetadata(present=False),
                    sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
                    metadata={},
                ),
                payload=FramePayload(thermal=thermal, visible=None),
            )
            assert publisher.publish(frame).accepted is True
            consumer = ring.consumer("test")
            try:
                view = consumer.latest()
                assert view is not None
                assert view.visible() is None
                assert view.descriptor.visible.present is False
            finally:
                consumer.close()
        finally:
            ring.close()

    def test_dual_frame_into_ir_only_ring_fails_loudly(self):
        """Contract guard: no silent truncation when layouts mismatch."""
        camera_id = f"camdual_{uuid.uuid4().hex[:8]}"
        ring, publisher = create_ring_buffer_and_publisher(camera_id)
        try:
            with pytest.raises(Exception):
                publisher.publish(_dual_frame(camera_id, 0))
        finally:
            ring.close()


# ─── Consumer + recording ────────────────────────────────────────────────────


class FixedCalibrationProvider:
    def get_calibration(self, camera_id: str):
        return np.arange(65536, dtype=np.float32)


class NullAlarmEvaluator:
    def evaluate(self, *args, **kwargs):
        return None


class TestDualConsumerIntegration:
    def test_processing_consumer_on_dual_ring(self):
        from thermal_monitor.processing.consumer import create_processing_consumer

        camera_id = f"camdual_{uuid.uuid4().hex[:8]}"
        ring, publisher = create_ring_buffer_and_publisher(camera_id, dual_feed=True)
        results: list = []
        try:
            ring2, consumer = create_processing_consumer(
                camera_id=camera_id,
                analysis_config=AnalysisConfig(camera_id=camera_id),
                calibration_provider=FixedCalibrationProvider(),
                alarm_evaluator=NullAlarmEvaluator(),
                result_callback=results.append,
                thermal_width=640,
                thermal_height=480,
                visible_width=1280,
                visible_height=480,
                visible_dtype=np.dtype(np.uint8),
            )
            try:
                consumer.start()
                assert publisher.publish(_dual_frame(camera_id, 3)).accepted is True
                assert consumer.wait_for_frames(1, timeout=5.0)
                consumer.stop()
                assert len(results) == 1
                assert results[0].frame.descriptor.sequence == 3
                assert results[0].temperature_image is not None
            finally:
                consumer.close()
                ring2.close()
        finally:
            ring.close()

    def test_dual_frame_recording_round_trip(self, tmp_path):
        from thermal_monitor.storage.recording import RecordingWriteMetadata
        from thermal_monitor.storage.recording.writer import RecordingWriter
        from thermal_monitor.offline import open_offline_source

        camera_id = f"camdual_{uuid.uuid4().hex[:8]}"
        frames = [_dual_frame(camera_id, i, ir_value=500 + i) for i in range(2)]
        meta = RecordingWriteMetadata(
            recording_id="rec_dual",
            cameras=[camera_id],
            streams={camera_id: ["IR", "VL"]},
            camera_snapshots=[{"camera_id": camera_id}],
            roi_snapshots=[],
            ptz_snapshots=[],
            calibration_snapshots=[],
            alarm_snapshots=[],
        )
        writer = RecordingWriter(tmp_path, meta, chunk_target_bytes=64 * 1024)
        writer.open()
        for frame in frames:
            writer.write_frame(frame)
        rec_dir = writer.finalize()

        source = open_offline_source(rec_dir)
        try:
            loaded = []
            while True:
                frame = source.get_next_frame()
                if frame is None:
                    break
                loaded.append(frame)
            # Recording format stores IR and VL as separate per-stream
            # records (existing design): 2 frames x 2 streams = 4 records.
            # Correlation is preserved via matching stream sequences.
            assert len(loaded) == 4
            ir_frames = [f for f in loaded if f.payload.thermal is not None]
            vl_frames = [f for f in loaded if f.payload.visible is not None]
            assert len(ir_frames) == 2 and len(vl_frames) == 2
            for i, frame in enumerate(sorted(ir_frames, key=lambda f: f.descriptor.thermal.sequence)):
                assert int(np.asarray(frame.payload.thermal)[0, 0]) == 500 + i
            for frame in vl_frames:
                assert frame.payload.visible.shape == (480, 1280)
                assert np.array_equal(
                    np.asarray(frame.payload.visible),
                    np.full((480, 1280), 0xAB, dtype=np.uint8),
                )
            ir_seqs = sorted(f.descriptor.thermal.sequence for f in ir_frames)
            vl_seqs = sorted(f.descriptor.visible.sequence for f in vl_frames)
            assert ir_seqs == vl_seqs == [0, 1]
        finally:
            source.close()
