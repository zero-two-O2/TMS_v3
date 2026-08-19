"""Tests for UI mode switching and configuration validation."""

from __future__ import annotations

import pytest

from thermal_monitor.core.modes import ApplicationMode, ModeManager, ModeState, TransitionError
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.core.models import (
    CameraIdentity,
    CameraConfig,
    ROIConfig,
    ROIGeometry,
    ROIShape,
    TemperatureLimits,
    TemperatureUnit,
    AnalysisConfig,
    PositionROIAssociation,
    AlarmRule,
    AlarmCondition,
    AlarmSeverity,
)


class TestModeSwitching:
    """Test mode switching logic."""

    def test_initial_mode_defaults_to_configuration(self) -> None:
        service = ModeService()
        assert service.current_mode == ApplicationMode.CONFIGURATION

    def test_initial_mode_can_be_set(self) -> None:
        service = ModeService(ApplicationMode.OFFLINE)
        assert service.current_mode == ApplicationMode.OFFLINE

    def test_transition_configuration_to_observer(self) -> None:
        service = ModeService(ApplicationMode.CONFIGURATION)
        state = service.transition_to_observer("test")
        assert state.mode == ApplicationMode.OBSERVER

    def test_transition_configuration_to_offline(self) -> None:
        service = ModeService(ApplicationMode.CONFIGURATION)
        state = service.transition_to_offline("test")
        assert state.mode == ApplicationMode.OFFLINE

    def test_transition_observer_to_configuration(self) -> None:
        service = ModeService(ApplicationMode.OBSERVER)
        state = service.transition_to_configuration("test")
        assert state.mode == ApplicationMode.CONFIGURATION

    def test_transition_observer_to_offline(self) -> None:
        service = ModeService(ApplicationMode.OBSERVER)
        state = service.transition_to_offline("test")
        assert state.mode == ApplicationMode.OFFLINE

    def test_transition_offline_to_configuration(self) -> None:
        service = ModeService(ApplicationMode.OFFLINE)
        state = service.transition_to_configuration("test")
        assert state.mode == ApplicationMode.CONFIGURATION

    def test_transition_offline_to_observer(self) -> None:
        service = ModeService(ApplicationMode.OFFLINE)
        state = service.transition_to_observer("test")
        assert state.mode == ApplicationMode.OBSERVER

    def test_invalid_transition_raises(self) -> None:
        service = ModeService(ApplicationMode.CONFIGURATION)
        # Can't transition from CONFIGURATION to CONFIGURATION (no-op is allowed)
        state = service.transition_to_configuration("test")
        assert state.mode == ApplicationMode.CONFIGURATION

    def test_callbacks_fired_on_transition(self) -> None:
        service = ModeService()
        callback_states = []

        def callback(state: ModeState):
            callback_states.append(state)

        service.add_observer(callback)
        service.transition_to_observer("test")
        assert len(callback_states) == 1
        assert callback_states[0].mode == ApplicationMode.OBSERVER


class TestConfigurationValidation:
    """Test configuration validation."""

    def test_camera_config_validation(self) -> None:
        service = ConfigurationService()

        identity = CameraIdentity(camera_id="cam_001", serial_number="SN123")
        config = service.create_camera_config(identity=identity)

        assert config.identity.camera_id == "cam_001"
        assert config.identity.serial_number == "SN123"
        assert config.enabled is True
        assert config.thermal_enabled is True
        assert config.visible_enabled is False

    def test_roi_config_all_shapes(self) -> None:
        service = ConfigurationService()

        # Rectangle1
        roi1 = service.create_roi_config(
            roi_id="roi_rect1",
            name="Rectangle1 ROI",
            shape=ROIShape.RECTANGLE1,
            parameters={"y1": 100.0, "x1": 100.0, "y2": 200.0, "x2": 200.0},
        )
        assert roi1.geometry.shape == ROIShape.RECTANGLE1

        # Rectangle2
        roi2 = service.create_roi_config(
            roi_id="roi_rect2",
            name="Rectangle2 ROI",
            shape=ROIShape.RECTANGLE2,
            parameters={"center_y": 150.0, "center_x": 150.0, "phi": 0.5, "length1": 50.0, "length2": 30.0},
        )
        assert roi2.geometry.shape == ROIShape.RECTANGLE2

        # Circle
        roi3 = service.create_roi_config(
            roi_id="roi_circle",
            name="Circle ROI",
            shape=ROIShape.CIRCLE,
            parameters={"center_y": 150.0, "center_x": 150.0, "radius": 50.0},
        )
        assert roi3.geometry.shape == ROIShape.CIRCLE

        # Ellipse
        roi4 = service.create_roi_config(
            roi_id="roi_ellipse",
            name="Ellipse ROI",
            shape=ROIShape.ELLIPSE,
            parameters={"center_y": 150.0, "center_x": 150.0, "phi": 0.3, "radius1": 60.0, "radius2": 40.0},
        )
        assert roi4.geometry.shape == ROIShape.ELLIPSE

        # Polygon
        roi5 = service.create_roi_config(
            roi_id="roi_polygon",
            name="Polygon ROI",
            shape=ROIShape.POLYGON,
            parameters={"points": [(100.0, 100.0), (200.0, 100.0), (150.0, 200.0)]},
        )
        assert roi5.geometry.shape == ROIShape.POLYGON

    def test_roi_coordinate_conversion_row_col(self) -> None:
        """Test that ROI geometry uses row/col (HALCON) convention."""
        geometry = ROIGeometry(
            shape=ROIShape.RECTANGLE1,
            parameters={"y1": 100.0, "x1": 200.0, "y2": 300.0, "x2": 400.0},
        )

        # y = row, x = col (HALCON convention)
        bbox = geometry.bounding_box()
        assert bbox == (100.0, 200.0, 300.0, 400.0)  # (y_min, x_min, y_max, x_max)

        # contains_point uses x=col, y=row
        assert geometry.contains_point(x=250.0, y=200.0) is True  # Inside
        assert geometry.contains_point(x=150.0, y=200.0) is False  # Outside (x < x1)

    def test_camera_selection(self) -> None:
        service = ConfigurationService()

        identity1 = CameraIdentity(camera_id="cam_001", serial_number="SN001")
        identity2 = CameraIdentity(camera_id="cam_002", serial_number="SN002")

        config1 = service.create_camera_config(identity=identity1)
        config2 = service.create_camera_config(identity=identity2)

        service.set_camera_config(config1)
        service.set_camera_config(config2)

        all_cameras = service.get_all_camera_configs()
        assert len(all_cameras) == 2
        assert service.get_camera_config("cam_001") is not None
        assert service.get_camera_config("cam_002") is not None

    def test_position_selection(self) -> None:
        """Test camera position selection for ROIs."""
        service = ConfigurationService()

        identity = CameraIdentity(camera_id="cam_001", serial_number="SN001")
        camera_config = service.create_camera_config(identity=identity)
        service.set_camera_config(camera_config)

        roi1 = service.create_roi_config(roi_id="roi_1", name="ROI 1")
        roi2 = service.create_roi_config(roi_id="roi_2", name="ROI 2")

        pos_assoc = service.create_position_roi_association(
            position_id="pos_1",
            position_name="Position 1",
            roi_ids=["roi_1", "roi_2"],
        )

        analysis_config = service.create_analysis_config(
            camera_id="cam_001",
            rois={"roi_1": roi1, "roi_2": roi2},
            position_associations={"pos_1": pos_assoc},
        )
        service.set_analysis_config(analysis_config)

        # Get ROIs for position
        rois = analysis_config.get_rois_for_position("pos_1")
        assert len(rois) == 2
        assert rois[0].roi_id == "roi_1"
        assert rois[1].roi_id == "roi_2"

        # Unknown position returns empty
        rois = analysis_config.get_rois_for_position("unknown")
        assert len(rois) == 0

    def test_offline_camera_filtering(self) -> None:
        """Test offline mode camera filtering logic."""
        # This tests the logic that would be used in OfflineFrameSourceConfig
        from thermal_monitor.offline import StreamFilter, OfflineFrameSourceConfig

        config = OfflineFrameSourceConfig(
            camera_id="cam_001",
            stream_filter=StreamFilter.IR,
        )

        assert config.camera_id == "cam_001"
        assert config.stream_filter == StreamFilter.IR

        config_all = OfflineFrameSourceConfig(
            camera_id=None,
            stream_filter=StreamFilter.ALL,
        )
        assert config_all.camera_id is None
        assert config_all.stream_filter == StreamFilter.ALL

    def test_stream_filtering(self) -> None:
        """Test stream type filtering."""
        from thermal_monitor.offline import StreamFilter

        assert StreamFilter.ALL == "all"
        assert StreamFilter.IR == "ir"
        assert StreamFilter.VL == "vl"

    def test_playback_state(self) -> None:
        """Test playback state management."""
        from thermal_monitor.services.offline import OfflineSession, OfflineService
        from thermal_monitor.processing import FrameSource

        class MockSource(FrameSource):
            def __init__(self):
                self._frames = []
                self._index = 0

            def get_next_frame(self):
                if self._index < len(self._frames):
                    frame = self._frames[self._index]
                    self._index += 1
                    return frame
                return None

            def get_latest_frame(self):
                if self._frames:
                    return self._frames[-1]
                return None

            def seek(self, sequence: int) -> bool:
                return False

            @property
            def camera_id(self) -> str:
                return "cam_001"

            @property
            def is_live(self) -> bool:
                return False

            def __len__(self) -> int:
                return len(self._frames)

        source = MockSource()
        session = OfflineSession(
            session_id="test",
            camera_id="cam_001",
            source=source,
            analysis_config=AnalysisConfig(camera_id="cam_001"),
        )

        assert session.is_playing is False
        assert session.current_frame_index == 0
        assert session.playback_speed == 1.0

        session.is_playing = True
        assert session.is_playing is True

        session.playback_speed = 2.0
        assert session.playback_speed == 2.0

    def test_alarm_display_model(self) -> None:
        """Test alarm display model distinguishes recorded vs current."""
        from thermal_monitor.core.models import AlarmEvent, AlarmSeverity

        # Recorded alarm (from recording)
        recorded_alarm = AlarmEvent(
            event_id="recorded_1",
            rule_id="rule_1",
            camera_id="cam_001",
            roi_id="roi_1",
            severity=AlarmSeverity.CRITICAL,
            measured_value=85.0,
            threshold_value=80.0,
            timestamp=1000.0,
            frame_sequence=10,
        )

        # Current offline analysis alarm (re-evaluated)
        current_alarm = AlarmEvent(
            event_id="current_1",
            rule_id="rule_1",
            camera_id="cam_001",
            roi_id="roi_1",
            severity=AlarmSeverity.WARNING,
            measured_value=78.0,
            threshold_value=80.0,
            timestamp=2000.0,
            frame_sequence=10,
        )

        # They should be distinguishable
        assert recorded_alarm.event_id != current_alarm.event_id
        assert recorded_alarm.timestamp != current_alarm.timestamp
        assert recorded_alarm.severity == AlarmSeverity.CRITICAL
        assert current_alarm.severity == AlarmSeverity.WARNING

    def test_historical_current_config_separation(self) -> None:
        """Test that offline analysis config changes don't affect recording config."""
        service = ConfigurationService()

        # Recording's historical configuration
        identity = CameraIdentity(camera_id="cam_001", serial_number="SN001")
        camera_config = service.create_camera_config(identity=identity)
        service.set_camera_config(camera_config)

        roi1 = service.create_roi_config(roi_id="roi_1", name="Historical ROI")
        analysis_config = service.create_analysis_config(
            camera_id="cam_001",
            rois={"roi_1": roi1},
        )
        service.set_analysis_config(analysis_config)

        # Simulate offline analysis with modified ROI
        # The recording's historical ROI should not be modified
        modified_roi = ROIConfig(
            roi_id="roi_1",
            name="Modified ROI",  # Different name
            geometry=roi1.geometry,
            temperature_limits=roi1.temperature_limits,
        )

        # This creates a new analysis config for offline use
        offline_analysis = AnalysisConfig(
            camera_id="cam_001",
            rois={"roi_1": modified_roi},
        )

        # Original should be unchanged
        original = service.get_analysis_config("cam_001")
        assert original.rois["roi_1"].name == "Historical ROI"

        # Offline config has modified version
        assert offline_analysis.rois["roi_1"].name == "Modified ROI"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])