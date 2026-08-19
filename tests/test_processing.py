"""Tests for processing pipeline and frame sources."""

from __future__ import annotations

import numpy as np
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from thermal_monitor.core.frame import Frame, FrameDescriptor, FramePayload, StreamMetadata, SyncInfo, SyncStatus
from thermal_monitor.core.models import (
    AnalysisConfig,
    PositionROIAssociation,
    ROIConfig,
    ROIGeometry,
    ROIShape,
    ROIStatistics,
    TemperatureLimits,
    TemperatureUnit,
)
from thermal_monitor.offline import OfflineFrameSource, open_offline_source, StreamFilter
from thermal_monitor.processing import (
    LiveFrameSource,
    ProcessingPipeline,
    SimpleProcessingPipeline,
    SyntheticFrameSource,
)
from thermal_monitor.storage.recording import RecordingWriteMetadata, RecordingWriter
from thermal_monitor.storage.database import Database
from thermal_monitor.processing.halcon import HalconROIAdapter


class MockHalconAdapter:
    """Mock HALCON adapter for unit testing without real HALCON."""

    def generate_regions(self, rois):
        return "mock_regions"

    def extract_statistics(self, regions, temperature_image, rois=None):
        # Simulate statistics for each ROI
        stats = []
        roi_list = rois if rois is not None else []
        for roi in roi_list:
            # Simple statistics based on the temperature image in the ROI region
            if roi.geometry.shape == ROIShape.RECTANGLE1:
                y1 = int(roi.geometry.parameters["y1"])
                x1 = int(roi.geometry.parameters["x1"])
                y2 = int(roi.geometry.parameters["y2"])
                x2 = int(roi.geometry.parameters["x2"])
                roi_data = temperature_image[y1:y2, x1:x2]
            else:
                roi_data = temperature_image.flatten()

            if roi_data.size > 0:
                stats.append(ROIStatistics(
                    roi_id=roi.roi_id,
                    roi_name=roi.name,
                    min_temp=float(np.min(roi_data)),
                    max_temp=float(np.max(roi_data)),
                    mean_temp=float(np.mean(roi_data)),
                    deviation=float(np.std(roi_data)),
                    unit=TemperatureUnit.CELSIUS,
                ))
            else:
                stats.append(ROIStatistics(
                    roi_id=roi.roi_id,
                    roi_name=roi.name,
                    min_temp=0.0,
                    max_temp=0.0,
                    mean_temp=0.0,
                    deviation=0.0,
                    unit=TemperatureUnit.CELSIUS,
                ))
        return stats

    def release_regions(self, regions):
        pass


class TestSyntheticFrameSource:
    def test_basic_generation(self):
        source = SyntheticFrameSource(camera_id="test_cam")
        frame = source.get_next_frame()

        assert frame.descriptor.camera_id == "test_cam"
        assert frame.descriptor.sequence == 0
        assert frame.payload.thermal is not None
        assert frame.payload.thermal.shape == (480, 640)

    def test_sequence_increments(self):
        source = SyntheticFrameSource(camera_id="test_cam")
        frame1 = source.get_next_frame()
        frame2 = source.get_next_frame()

        assert frame2.descriptor.sequence == frame1.descriptor.sequence + 1

    def test_seek(self):
        source = SyntheticFrameSource(camera_id="test_cam")
        source.get_next_frame()  # seq 0
        source.get_next_frame()  # seq 1
        assert source.seek(10)
        frame = source.get_next_frame()
        assert frame.descriptor.sequence == 10

    def test_with_visible(self):
        source = SyntheticFrameSource(camera_id="test_cam", include_visible=True)
        frame = source.get_next_frame()

        assert frame.payload.visible is not None
        assert frame.descriptor.visible.present is True

    def test_is_not_live(self):
        source = SyntheticFrameSource(camera_id="test_cam")
        assert source.is_live is False


def _make_offline_test_frames(camera_id: str, count: int, base_temp: float = 100.0) -> list[Frame]:
    """Create test frames for offline source testing."""
    frames = []
    for i in range(count):
        thermal = np.full((10, 10), int(base_temp + i * 10), dtype=np.uint16)
        thermal.setflags(write=False)
        thermal_meta = StreamMetadata(
            present=True,
            width=10,
            height=10,
            pixel_format="IR_Data",
            bits_per_channel=16,
            dtype="uint16",
            byte_count=thermal.nbytes,
            sequence=i,
            timestamp=float(i),
            monotonic_timestamp=float(i),
        )
        descriptor = FrameDescriptor(
            camera_id=camera_id,
            sequence=i,
            timestamp=float(i),
            monotonic_timestamp=float(i),
            thermal=thermal_meta,
            visible=StreamMetadata(present=False),
            sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
        )
        frames.append(Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal)))
    return frames


def _write_test_recording(tmp_path: Path, recording_id: str, frames: list[Frame]) -> Path:
    """Write frames to a test recording using the new format."""
    cameras = list({f.descriptor.camera_id for f in frames})
    meta = RecordingWriteMetadata(
        recording_id=recording_id,
        cameras=cameras,
        streams={cam: ["IR"] for cam in cameras},
        camera_snapshots=[{"camera_id": cam} for cam in cameras],
        roi_snapshots=[],
        ptz_snapshots=[],
        calibration_snapshots=[],
        alarm_snapshots=[],
    )
    writer = RecordingWriter(tmp_path, meta, chunk_target_bytes=64 * 1024)
    writer.open()
    for frame in frames:
        writer.write_frame(frame)
    return writer.finalize()


class TestOfflineFrameSource:
    """Tests for the new OfflineFrameSource (recording directory based)."""

    def test_sequential_access(self, tmp_path):
        frames = _make_offline_test_frames("offline_cam", 5)
        rec_dir = _write_test_recording(tmp_path, "rec_seq", frames)

        source = open_offline_source(rec_dir)
        for i in range(5):
            frame = source.get_next_frame()
            assert frame is not None
            assert frame.descriptor.sequence == i

        # Exhausted
        assert source.get_next_frame() is None
        source.close()

    def test_seek(self, tmp_path):
        frames = _make_offline_test_frames("offline_cam", 5)
        rec_dir = _write_test_recording(tmp_path, "rec_seek", frames)

        source = open_offline_source(rec_dir)
        assert source.seek(3)
        frame = source.get_next_frame()
        assert frame.descriptor.sequence == 3

        assert source.seek(0)
        frame = source.get_next_frame()
        assert frame.descriptor.sequence == 0
        source.close()

    def test_seek_invalid(self, tmp_path):
        frames = _make_offline_test_frames("offline_cam", 5)
        rec_dir = _write_test_recording(tmp_path, "rec_seek_inv", frames)

        source = open_offline_source(rec_dir)
        assert source.seek(99) is False
        source.close()

    def test_get_latest(self, tmp_path):
        frames = _make_offline_test_frames("offline_cam", 5)
        rec_dir = _write_test_recording(tmp_path, "rec_latest", frames)

        source = open_offline_source(rec_dir)
        latest = source.get_latest_frame()
        assert latest.descriptor.sequence == 4
        source.close()

    def test_is_not_live(self, tmp_path):
        frames = _make_offline_test_frames("offline_cam", 1)
        rec_dir = _write_test_recording(tmp_path, "rec_live", frames)

        source = open_offline_source(rec_dir)
        assert source.is_live is False
        source.close()

    def test_len_and_iter(self, tmp_path):
        frames = _make_offline_test_frames("offline_cam", 5)
        rec_dir = _write_test_recording(tmp_path, "rec_len", frames)

        source = open_offline_source(rec_dir)
        assert len(source) == 5
        count = sum(1 for _ in source)
        assert count == 5
        source.close()

    def test_reset(self, tmp_path):
        frames = _make_offline_test_frames("offline_cam", 5)
        rec_dir = _write_test_recording(tmp_path, "rec_reset", frames)

        source = open_offline_source(rec_dir)
        source.get_next_frame()  # seq 0
        source.get_next_frame()  # seq 1
        source.reset()
        frame = source.get_next_frame()
        assert frame.descriptor.sequence == 0
        source.close()

    def test_first_and_current(self, tmp_path):
        frames = _make_offline_test_frames("offline_cam", 5)
        rec_dir = _write_test_recording(tmp_path, "rec_first", frames)

        source = open_offline_source(rec_dir)
        first = source.first()
        assert first.descriptor.sequence == 0

        # Advance a bit
        source.get_next_frame()  # seq 1
        source.get_next_frame()  # seq 2
        current = source.current()
        assert current.descriptor.sequence == 2
        source.close()

    def test_seek_timestamp(self, tmp_path):
        frames = _make_offline_test_frames("offline_cam", 5)
        rec_dir = _write_test_recording(tmp_path, "rec_ts", frames)

        source = open_offline_source(rec_dir)
        assert source.seek_timestamp(2.5)  # Should find sequence 3 (timestamp 3.0)
        frame = source.current()
        assert frame.descriptor.sequence == 3

        # Seek before first
        source.seek_timestamp(-1.0)
        frame = source.current()
        assert frame.descriptor.sequence == 0
        source.close()

    def test_seek_sequence(self, tmp_path):
        frames = _make_offline_test_frames("offline_cam", 5)
        rec_dir = _write_test_recording(tmp_path, "rec_seq2", frames)

        source = open_offline_source(rec_dir)
        assert source.seek_sequence("offline_cam", 1, 3)  # stream_type=1 (IR), sequence=3
        frame = source.current()
        assert frame.descriptor.sequence == 3
        source.close()

    def test_multi_camera_filter(self, tmp_path):
        frames_a = _make_offline_test_frames("cam_a", 3, base_temp=100.0)
        frames_b = _make_offline_test_frames("cam_b", 2, base_temp=200.0)
        all_frames = frames_a + frames_b
        rec_dir = _write_test_recording(tmp_path, "rec_multi", all_frames)

        # Read all
        source_all = open_offline_source(rec_dir)
        assert len(source_all) == 5
        source_all.close()

        # Filter to cam_a
        source_a = open_offline_source(rec_dir, camera_id="cam_a")
        assert len(source_a) == 3
        for frame in source_a:
            assert frame.descriptor.camera_id == "cam_a"
        source_a.close()

        # Filter to cam_b
        source_b = open_offline_source(rec_dir, camera_id="cam_b")
        assert len(source_b) == 2
        for frame in source_b:
            assert frame.descriptor.camera_id == "cam_b"
        source_b.close()

    def test_stream_filter_ir(self, tmp_path):
        # Create frames with alternating IR/VL
        frames = []
        for i in range(4):
            thermal = np.full((8, 8), 100 + i, dtype=np.uint16)
            thermal.setflags(write=False)
            if i % 2 == 0:
                # IR frame
                thermal_meta = StreamMetadata(
                    present=True, width=8, height=8, pixel_format="IR_Data",
                    bits_per_channel=16, dtype="uint16", byte_count=thermal.nbytes,
                    sequence=i // 2, timestamp=float(i), monotonic_timestamp=float(i),
                )
                visible_meta = StreamMetadata(present=False)
                sync = SyncInfo(status=SyncStatus.MISSING_VISIBLE)
            else:
                # VL frame
                vl_data = np.zeros((8, 8, 2), dtype=np.uint8)
                vl_data.setflags(write=False)
                thermal_meta = StreamMetadata(present=False)
                visible_meta = StreamMetadata(
                    present=True, width=8, height=8, pixel_format="YUV422_8",
                    bits_per_channel=8, dtype="uint8", byte_count=vl_data.nbytes,
                    sequence=i // 2, timestamp=float(i), monotonic_timestamp=float(i),
                )
                sync = SyncInfo(status=SyncStatus.MISSING_THERMAL)

            descriptor = FrameDescriptor(
                camera_id="test_cam",
                sequence=i // 2,
                timestamp=float(i),
                monotonic_timestamp=float(i),
                thermal=thermal_meta,
                visible=visible_meta,
                sync=sync,
            )
            if i % 2 == 0:
                frames.append(Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal)))
            else:
                frames.append(Frame(descriptor=descriptor, payload=FramePayload(visible=vl_data)))

        rec_dir = _write_test_recording(tmp_path, "rec_streams", frames)

        # Filter IR only
        source_ir = open_offline_source(rec_dir, stream_filter=StreamFilter.IR)
        ir_count = 0
        for frame in source_ir:
            assert frame.payload.thermal is not None
            assert frame.payload.visible is None
            ir_count += 1
        assert ir_count == 2
        source_ir.close()

        # Filter VL only
        source_vl = open_offline_source(rec_dir, stream_filter=StreamFilter.VL)
        vl_count = 0
        for frame in source_vl:
            assert frame.payload.visible is not None
            assert frame.payload.thermal is None
            vl_count += 1
        assert vl_count == 2
        source_vl.close()


class TestSimpleProcessingPipeline:
    def create_test_frame(self, sequence: int = 0) -> Frame:
        thermal = np.zeros((100, 100), dtype=np.uint16)
        # Add a hot region in the center (rows 40-60, cols 40-60)
        thermal[40:60, 40:60] = 30000
        thermal.setflags(write=False)

        thermal_meta = StreamMetadata(
            present=True,
            width=100,
            height=100,
            pixel_format="IR_Data",
            bits_per_channel=16,
            dtype="uint16",
            byte_count=thermal.nbytes,
        )
        descriptor = FrameDescriptor(
            camera_id="test_cam",
            sequence=sequence,
            timestamp=1234567890.0 + sequence,
            monotonic_timestamp=1234567890.0 + sequence,
            thermal=thermal_meta,
            visible=StreamMetadata(present=False),
            sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
        )
        return Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal))

    def create_analysis_config(self) -> AnalysisConfig:
        roi = ROIConfig(
            roi_id="roi_1",
            name="Center ROI",
            geometry=ROIGeometry(
                shape=ROIShape.RECTANGLE1,
                parameters={"y1": 40, "x1": 40, "y2": 60, "x2": 60},
            ),
            temperature_limits=TemperatureLimits(
                unit=TemperatureUnit.CELSIUS,
                max_warning=100.0,
            ),
        )
        assoc = PositionROIAssociation(
            position_id="default",
            roi_ids=("roi_1",),
        )
        return AnalysisConfig(
            camera_id="test_cam",
            rois={"roi_1": roi},
            position_associations={"default": assoc},
        )

    def test_basic_processing(self):
        config = self.create_analysis_config()
        pipeline = SimpleProcessingPipeline(config=config, halcon_adapter=MockHalconAdapter())
        frame = self.create_test_frame(0)

        result = pipeline.process_frame(frame)

        assert result.camera_id == "test_cam"
        assert result.frame_sequence == 0
        assert "roi_1" in result.roi_results

    def test_multiple_frames(self):
        config = self.create_analysis_config()
        pipeline = SimpleProcessingPipeline(config=config, halcon_adapter=MockHalconAdapter())

        frames = [self.create_test_frame(i) for i in range(3)]
        results = pipeline.process_frames(frames)

        assert len(results) == 3
        assert all(r.frame_sequence == i for i, r in enumerate(results))

    def test_stats_tracking(self):
        config = self.create_analysis_config()
        pipeline = SimpleProcessingPipeline(config=config, halcon_adapter=MockHalconAdapter())
        frame = self.create_test_frame(0)

        pipeline.process_frame(frame)
        stats = pipeline.stats

        assert stats.frames_processed == 1
        assert stats.last_frame_sequence == 0
        assert stats.average_processing_time_ms > 0

    def test_no_thermal_data(self):
        config = self.create_analysis_config()
        pipeline = SimpleProcessingPipeline(config=config)

        thermal = np.zeros((10, 10), dtype=np.uint16)
        thermal.setflags(write=False)
        thermal_meta = StreamMetadata(present=True, width=10, height=10)
        descriptor = FrameDescriptor(
            camera_id="test_cam",
            sequence=0,
            timestamp=1.0,
            monotonic_timestamp=1.0,
            thermal=thermal_meta,
            visible=StreamMetadata(present=False),
            sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
        )
        frame = Frame(descriptor=descriptor, payload=FramePayload(thermal=None))  # No thermal

        result = pipeline.process_frame(frame)
        assert len(result.roi_results) == 0

    def test_disabled_roi(self):
        roi = ROIConfig(
            roi_id="roi_1",
            name="Disabled ROI",
            enabled=False,
            geometry=ROIGeometry(shape=ROIShape.RECTANGLE1, parameters={"y1": 0, "x1": 0, "y2": 10, "x2": 10}),
        )
        config = AnalysisConfig(
            camera_id="test_cam",
            rois={"roi_1": roi},
        )
        pipeline = SimpleProcessingPipeline(config=config)
        frame = self.create_test_frame(0)

        result = pipeline.process_frame(frame)
        assert len(result.roi_results) == 0

    def test_update_config(self):
        config1 = self.create_analysis_config()
        pipeline = SimpleProcessingPipeline(config=config1)

        roi2 = ROIConfig(
            roi_id="roi_2",
            name="New ROI",
            geometry=ROIGeometry(shape=ROIShape.RECTANGLE1, parameters={"y1": 0, "x1": 0, "y2": 10, "x2": 10}),
        )
        config2 = AnalysisConfig(
            camera_id="test_cam",
            rois={"roi_2": roi2},
        )
        pipeline.update_config(config2)

        assert pipeline.config.rois["roi_2"].name == "New ROI"
        assert "roi_1" not in pipeline.config.rois

    def test_offline_source_with_pipeline(self, tmp_path):
        """Test that OfflineFrameSource works with SimpleProcessingPipeline."""
        frames = []
        for i in range(3):
            thermal = np.zeros((100, 100), dtype=np.uint16)
            thermal[40:60, 40:60] = 20000 + i * 5000
            thermal.setflags(write=False)
            thermal_meta = StreamMetadata(
                present=True, width=100, height=100, pixel_format="IR_Data",
                bits_per_channel=16, dtype="uint16", byte_count=thermal.nbytes,
                sequence=i, timestamp=float(i), monotonic_timestamp=float(i),
            )
            descriptor = FrameDescriptor(
                camera_id="test_cam",
                sequence=i,
                timestamp=float(i),
                monotonic_timestamp=float(i),
                thermal=thermal_meta,
                visible=StreamMetadata(present=False),
                sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
            )
            frames.append(Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal)))

        rec_dir = _write_test_recording(tmp_path, "rec_pipeline", frames)

        config = self.create_analysis_config()
        pipeline = SimpleProcessingPipeline(config=config, halcon_adapter=MockHalconAdapter())

        source = open_offline_source(rec_dir)
        results = []
        while True:
            frame = source.get_next_frame()
            if frame is None:
                break
            results.append(pipeline.process_frame(frame))
        source.close()

        assert len(results) == 3
        for i, result in enumerate(results):
            assert result.frame_sequence == i
            assert "roi_1" in result.roi_results


class TestFrameSourceProtocol:
    def test_synthetic_implements_protocol(self):
        source = SyntheticFrameSource(camera_id="test")
        # Should have all required methods
        assert hasattr(source, "get_next_frame")
        assert hasattr(source, "get_latest_frame")
        assert hasattr(source, "seek")
        assert hasattr(source, "camera_id")
        assert hasattr(source, "is_live")

    def test_offline_implements_protocol(self, tmp_path):
        frames = _make_offline_test_frames("test", 2)
        rec_dir = _write_test_recording(tmp_path, "rec_protocol", frames)

        source = open_offline_source(rec_dir)
        assert hasattr(source, "get_next_frame")
        assert hasattr(source, "get_latest_frame")
        assert hasattr(source, "seek")
        assert hasattr(source, "seek_timestamp")
        assert hasattr(source, "seek_sequence")
        assert hasattr(source, "first")
        assert hasattr(source, "current")
        assert hasattr(source, "camera_id")
        assert hasattr(source, "is_live")
        source.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])