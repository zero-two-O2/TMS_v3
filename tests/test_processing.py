"""Tests for processing pipeline and frame sources."""

from __future__ import annotations

import numpy as np
import pytest
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
from thermal_monitor.processing import (
    LiveFrameSource,
    OfflineFrameSource,
    ProcessingPipeline,
    SimpleProcessingPipeline,
    SyntheticFrameSource,
)
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


class TestOfflineFrameSource:
    def create_test_frames(self, count: int) -> list[Frame]:
        frames = []
        for i in range(count):
            thermal = np.zeros((10, 10), dtype=np.uint16)
            thermal.setflags(write=False)
            thermal_meta = StreamMetadata(present=True, width=10, height=10)
            descriptor = FrameDescriptor(
                camera_id="offline_cam",
                sequence=i,
                timestamp=float(i),
                monotonic_timestamp=float(i),
                thermal=thermal_meta,
                visible=StreamMetadata(present=False),
                sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
            )
            frames.append(Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal)))
        return frames

    def test_sequential_access(self):
        frames = self.create_test_frames(5)
        source = OfflineFrameSource(camera_id="offline_cam", frames=frames)

        for i in range(5):
            frame = source.get_next_frame()
            assert frame is not None
            assert frame.descriptor.sequence == i

        # Exhausted
        assert source.get_next_frame() is None

    def test_seek(self):
        frames = self.create_test_frames(5)
        source = OfflineFrameSource(camera_id="offline_cam", frames=frames)

        assert source.seek(3)
        frame = source.get_next_frame()
        assert frame.descriptor.sequence == 3

        assert source.seek(0)
        frame = source.get_next_frame()
        assert frame.descriptor.sequence == 0

    def test_seek_invalid(self):
        frames = self.create_test_frames(5)
        source = OfflineFrameSource(camera_id="offline_cam", frames=frames)

        assert source.seek(99) is False

    def test_get_latest(self):
        frames = self.create_test_frames(5)
        source = OfflineFrameSource(camera_id="offline_cam", frames=frames)

        latest = source.get_latest_frame()
        assert latest.descriptor.sequence == 4

    def test_is_not_live(self):
        frames = self.create_test_frames(1)
        source = OfflineFrameSource(camera_id="offline_cam", frames=frames)
        assert source.is_live is False

    def test_len_and_iter(self):
        frames = self.create_test_frames(5)
        source = OfflineFrameSource(camera_id="offline_cam", frames=frames)

        assert len(source) == 5
        count = sum(1 for _ in source)
        assert count == 5

    def test_reset(self):
        frames = self.create_test_frames(5)
        source = OfflineFrameSource(camera_id="offline_cam", frames=frames)

        source.get_next_frame()  # seq 0
        source.get_next_frame()  # seq 1
        source.reset()
        frame = source.get_next_frame()
        assert frame.descriptor.sequence == 0


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

    @pytest.fixture
    def mock_database(self):
        """Create a mock database for testing."""
        from unittest.mock import MagicMock
        db = MagicMock(spec=Database)
        return db

    def test_basic_processing(self, mock_database):
        config = self.create_analysis_config()
        pipeline = SimpleProcessingPipeline(config=config, database=mock_database, halcon_adapter=MockHalconAdapter())
        frame = self.create_test_frame(0)

        result = pipeline.process_frame(frame)

        assert result.camera_id == "test_cam"
        assert result.frame_sequence == 0
        assert "roi_1" in result.roi_results

    def test_multiple_frames(self, mock_database):
        config = self.create_analysis_config()
        pipeline = SimpleProcessingPipeline(config=config, database=mock_database, halcon_adapter=MockHalconAdapter())

        frames = [self.create_test_frame(i) for i in range(3)]
        results = pipeline.process_frames(frames)

        assert len(results) == 3
        assert all(r.frame_sequence == i for i, r in enumerate(results))

    def test_stats_tracking(self, mock_database):
        config = self.create_analysis_config()
        pipeline = SimpleProcessingPipeline(config=config, database=mock_database, halcon_adapter=MockHalconAdapter())
        frame = self.create_test_frame(0)

        pipeline.process_frame(frame)
        stats = pipeline.stats

        assert stats.frames_processed == 1
        assert stats.last_frame_sequence == 0
        assert stats.average_processing_time_ms > 0

    def test_no_thermal_data(self, mock_database):
        config = self.create_analysis_config()
        pipeline = SimpleProcessingPipeline(config=config, database=mock_database)

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

    def test_disabled_roi(self, mock_database):
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
        pipeline = SimpleProcessingPipeline(config=config, database=mock_database)
        frame = self.create_test_frame(0)

        result = pipeline.process_frame(frame)
        assert len(result.roi_results) == 0

    def test_update_config(self, mock_database):
        config1 = self.create_analysis_config()
        pipeline = SimpleProcessingPipeline(config=config1, database=mock_database)

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


class TestFrameSourceProtocol:
    def test_synthetic_implements_protocol(self):
        source = SyntheticFrameSource(camera_id="test")
        # Should have all required methods
        assert hasattr(source, "get_next_frame")
        assert hasattr(source, "get_latest_frame")
        assert hasattr(source, "seek")
        assert hasattr(source, "camera_id")
        assert hasattr(source, "is_live")

    def test_offline_implements_protocol(self):
        frames = []
        thermal = np.zeros((10, 10), dtype=np.uint16)
        thermal.setflags(write=False)
        thermal_meta = StreamMetadata(present=True, width=10, height=10)
        descriptor = FrameDescriptor(
            camera_id="test",
            sequence=0,
            timestamp=1.0,
            monotonic_timestamp=1.0,
            thermal=thermal_meta,
            visible=StreamMetadata(present=False),
            sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
        )
        frames.append(Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal)))

        source = OfflineFrameSource(camera_id="test", frames=frames)
        assert hasattr(source, "get_next_frame")
        assert hasattr(source, "get_latest_frame")
        assert hasattr(source, "seek")
        assert hasattr(source, "camera_id")
        assert hasattr(source, "is_live")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])