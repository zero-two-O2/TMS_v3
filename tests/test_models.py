"""Tests for core domain models."""

from __future__ import annotations

import pytest

from thermal_monitor.core.models import (
    # Camera
    CameraConfig,
    CameraConnectionState,
    CameraIdentity,
    CameraInfo,
    CameraStatus,
    PTZConfig,
    PTZLimits,
    PTZMode,
    PTZPosition,
    # Inspection
    AlarmCondition,
    AlarmEvent,
    AlarmRule,
    AlarmSeverity,
    AnalysisConfig,
    AnalysisResult,
    PositionROIAssociation,
    ROIConfig,
    ROIGeometry,
    ROIStatistics,
    ROIShape,
    ROIType,
    TemperatureLimits,
    TemperatureUnit,
    # System
    ApplicationState,
    RecordingConfig,
    RecordingMetadata,
    RecordingState,
    RecordingTrigger,
    SystemConfig,
    SystemStatus,
)

from thermal_monitor.core.modes import ApplicationMode


class TestCameraModels:
    def test_camera_identity(self):
        identity = CameraIdentity(
            camera_id="cam_001",
            serial_number="SN12345",
            model="TV46L",
            vendor="Fluke",
        )
        assert identity.camera_id == "cam_001"
        assert identity.serial_number == "SN12345"

    def test_camera_identity_requires_fields(self):
        with pytest.raises(ValueError):
            CameraIdentity(camera_id="", serial_number="SN12345")
        with pytest.raises(ValueError):
            CameraIdentity(camera_id="cam_001", serial_number="")

    def test_ptz_position(self):
        pos = PTZPosition(pan=45.0, tilt=-10.0, zoom=2.0, name="position_1", preset_id=1)
        assert pos.pan == 45.0
        assert pos.tilt == -10.0
        assert pos.zoom == 2.0

    def test_ptz_position_validation(self):
        with pytest.raises(ValueError):
            PTZPosition(pan=400.0)  # out of range
        with pytest.raises(ValueError):
            PTZPosition(tilt=100.0)  # out of range
        with pytest.raises(ValueError):
            PTZPosition(zoom=0.5)  # below minimum

    def test_ptz_limits(self):
        limits = PTZLimits(min_pan=-170, max_pan=170, min_tilt=-90, max_tilt=90, min_zoom=1, max_zoom=30)
        pos = PTZPosition(pan=0, tilt=0, zoom=1)
        assert limits.contains(pos) is True

        pos_out = PTZPosition(pan=200, tilt=0, zoom=1)
        assert limits.contains(pos_out) is False

    def test_ptz_limits_clamp(self):
        limits = PTZLimits(min_pan=-170, max_pan=170, min_tilt=-90, max_tilt=90, min_zoom=1, max_zoom=30)
        pos = PTZPosition(pan=200, tilt=0, zoom=50)
        clamped = limits.clamp(pos)
        assert clamped.pan == 170
        assert clamped.zoom == 30

    def test_ptz_config(self):
        pos1 = PTZPosition(pan=0, tilt=0, zoom=1, name="home", preset_id=0)
        pos2 = PTZPosition(pan=90, tilt=0, zoom=1, name="right", preset_id=1)
        config = PTZConfig(
            limits=PTZLimits(),
            default_position=pos1,
            preset_positions={0: pos1, 1: pos2},
        )
        assert config.get_preset(0) == pos1
        assert config.get_preset(1) == pos2
        assert config.get_preset(99) is None

    def test_ptz_config_with_preset(self):
        config = PTZConfig()
        pos = PTZPosition(pan=45, tilt=0, zoom=1, preset_id=2)
        new_config = config.with_preset(2, pos)
        assert new_config.get_preset(2) == pos
        # Original unchanged
        assert config.get_preset(2) is None

    def test_camera_config(self):
        identity = CameraIdentity(camera_id="cam_001", serial_number="SN12345")
        config = CameraConfig(
            identity=identity,
            name="Camera 1",
            thermal_enabled=True,
            visible_enabled=True,
        )
        assert config.identity.camera_id == "cam_001"
        assert config.name == "Camera 1"
        assert config.thermal_enabled is True
        assert config.visible_enabled is True

    def test_camera_status(self):
        status = CameraStatus(
            camera_id="cam_001",
            connection_state=CameraConnectionState.ACQUIRING,
            fps=9.0,
        )
        assert status.is_connected is True
        assert status.is_acquiring is True
        assert status.is_healthy is True

    def test_camera_status_not_connected(self):
        status = CameraStatus(
            camera_id="cam_001",
            connection_state=CameraConnectionState.DISCONNECTED,
        )
        assert status.is_connected is False
        assert status.is_acquiring is False
        assert status.is_healthy is False


class TestInspectionModels:
    def test_roi_geometry_rectangle(self):
        geom = ROIGeometry(
            shape=ROIShape.RECTANGLE1,
            parameters={"y1": 100, "x1": 100, "y2": 300, "x2": 250},
        )
        assert geom.shape == ROIShape.RECTANGLE1
        bbox = geom.bounding_box()
        assert bbox == (100.0, 100.0, 300.0, 250.0)

    def test_roi_geometry_circle(self):
        geom = ROIGeometry(
            shape=ROIShape.CIRCLE,
            parameters={"center_y": 200, "center_x": 200, "radius": 50},
        )
        bbox = geom.bounding_box()
        assert bbox == (150.0, 150.0, 250.0, 250.0)

    def test_roi_geometry_contains_point(self):
        geom = ROIGeometry(
            shape=ROIShape.RECTANGLE1,
            parameters={"y1": 0, "x1": 0, "y2": 100, "x2": 100},
        )
        # Note: contains_point uses x, y (UI convention) but internally uses row/col
        assert geom.contains_point(50, 50) is True
        assert geom.contains_point(-10, 50) is False
        assert geom.contains_point(150, 50) is False

    def test_roi_geometry_invalid_rect(self):
        with pytest.raises(ValueError):
            ROIGeometry(shape=ROIShape.RECTANGLE1, parameters={"y1": 0, "x1": 0})  # missing y2/x2
        with pytest.raises(ValueError):
            ROIGeometry(shape=ROIShape.RECTANGLE1, parameters={"y1": 100, "x1": 0, "y2": 0, "x2": 100})  # y1 > y2
        with pytest.raises(ValueError):
            ROIGeometry(shape=ROIShape.RECTANGLE1, parameters={"y1": 0, "x1": 100, "y2": 100, "x2": 0})  # x1 > x2

    def test_temperature_limits(self):
        limits = TemperatureLimits(
            unit=TemperatureUnit.CELSIUS,
            min_warning=30.0,
            max_warning=80.0,
            min_critical=20.0,
            max_critical=100.0,
        )
        assert limits.evaluate(25.0) == AlarmSeverity.WARNING  # below min_warning
        assert limits.evaluate(90.0) == AlarmSeverity.WARNING  # above max_warning
        assert limits.evaluate(15.0) == AlarmSeverity.CRITICAL  # below min_critical
        assert limits.evaluate(110.0) == AlarmSeverity.CRITICAL  # above max_critical
        assert limits.evaluate(50.0) == AlarmSeverity.INFO  # within limits

    def test_temperature_limits_rate_of_change(self):
        limits = TemperatureLimits(
            unit=TemperatureUnit.CELSIUS,
            rate_of_change_limit=10.0,  # 10 deg/s
        )
        assert limits.evaluate(50.0, prev_temperature=40.0, dt=0.5) == AlarmSeverity.WARNING  # rate = 20 deg/s
        assert limits.evaluate(50.0, prev_temperature=40.0, dt=2.0) == AlarmSeverity.INFO  # rate = 5 deg/s

    def test_temperature_unit_conversion(self):
        c = TemperatureUnit.CELSIUS
        f = TemperatureUnit.FAHRENHEIT
        k = TemperatureUnit.KELVIN

        assert abs(TemperatureUnit.convert(0, c, f) - 32.0) < 0.01
        assert abs(TemperatureUnit.convert(100, c, f) - 212.0) < 0.01
        assert abs(TemperatureUnit.convert(0, c, k) - 273.15) < 0.01
        assert abs(TemperatureUnit.convert(32, f, c) - 0.0) < 0.01

    def test_roi_config(self):
        geom = ROIGeometry(shape=ROIShape.RECTANGLE1, parameters={"y1": 0, "x1": 0, "y2": 100, "x2": 100})
        limits = TemperatureLimits(max_warning=80.0)
        roi = ROIConfig(
            roi_id="roi_1",
            name="Hot Spot",
            geometry=geom,
            temperature_limits=limits,
        )
        assert roi.roi_id == "roi_1"
        assert roi.enabled is True

    def test_roi_config_requires_id(self):
        with pytest.raises(ValueError):
            ROIConfig(roi_id="")

    def test_position_roi_association(self):
        assoc = PositionROIAssociation(
            position_id="preset_1",
            position_name="Home Position",
            roi_ids=("roi_1", "roi_2"),
        )
        assert assoc.position_id == "preset_1"
        assert assoc.roi_ids == ("roi_1", "roi_2")

    def test_analysis_config(self):
        roi1 = ROIConfig(roi_id="roi_1", geometry=ROIGeometry(shape=ROIShape.RECTANGLE1, parameters={"y1": 0, "x1": 0, "y2": 100, "x2": 100}))
        roi2 = ROIConfig(roi_id="roi_2", geometry=ROIGeometry(shape=ROIShape.RECTANGLE1, parameters={"y1": 100, "x1": 100, "y2": 200, "x2": 200}))
        assoc = PositionROIAssociation(position_id="pos_1", roi_ids=("roi_1", "roi_2"))

        config = AnalysisConfig(
            camera_id="cam_001",
            rois={"roi_1": roi1, "roi_2": roi2},
            position_associations={"pos_1": assoc},
        )
        rois = config.get_rois_for_position("pos_1")
        assert len(rois) == 2
        assert rois[0].roi_id == "roi_1"
        assert rois[1].roi_id == "roi_2"

    def test_analysis_config_unknown_position(self):
        config = AnalysisConfig(camera_id="cam_001")
        rois = config.get_rois_for_position("unknown")
        assert rois == ()

    def test_roi_statistics(self):
        stats = ROIStatistics(
            roi_id="roi_1",
            roi_name="Test ROI",
            min_temp=20.0,
            max_temp=80.0,
            mean_temp=45.0,
            deviation=10.0,
        )
        assert stats.range_temp == 60.0
        assert stats.std_temp == 10.0  # alias for deviation

    def test_analysis_result(self):
        stats = ROIStatistics(
            roi_id="roi_1",
            roi_name="Test ROI",
            min_temp=20.0,
            max_temp=80.0,
            mean_temp=45.0,
            deviation=10.0,
        )
        result = AnalysisResult(
            camera_id="cam_001",
            frame_sequence=100,
            frame_timestamp=1234567890.0,
            roi_results={"roi_1": stats},
            processing_time_ms=5.0,
        )
        assert result.camera_id == "cam_001"
        assert result.frame_sequence == 100

    def test_alarm_rule_above(self):
        rule = AlarmRule(
            rule_id="rule_1",
            roi_id="roi_1",
            condition=AlarmCondition.ABOVE,
            severity=AlarmSeverity.WARNING,
            threshold=80.0,
        )
        assert rule.condition == AlarmCondition.ABOVE
        assert rule.threshold == 80.0

    def test_alarm_rule_range(self):
        rule = AlarmRule(
            rule_id="rule_1",
            roi_id="roi_1",
            condition=AlarmCondition.OUTSIDE_RANGE,
            severity=AlarmSeverity.CRITICAL,
            threshold=0,  # not used
            threshold_low=10.0,
            threshold_high=90.0,
        )
        assert rule.threshold_low == 10.0
        assert rule.threshold_high == 90.0

    def test_alarm_rule_range_validation(self):
        with pytest.raises(ValueError):
            AlarmRule(
                rule_id="rule_1",
                roi_id="roi_1",
                condition=AlarmCondition.OUTSIDE_RANGE,
                severity=AlarmSeverity.CRITICAL,
                threshold=0,
                threshold_low=90.0,
                threshold_high=10.0,  # low >= high
            )

    def test_alarm_event(self):
        event = AlarmEvent(
            event_id="evt_001",
            rule_id="rule_1",
            camera_id="cam_001",
            roi_id="roi_1",
            severity=AlarmSeverity.WARNING,
            measured_value=85.0,
            threshold_value=80.0,
            timestamp=1234567890.0,
            frame_sequence=100,
        )
        assert event.event_id == "evt_001"
        assert event.severity == AlarmSeverity.WARNING
        assert event.acknowledged is False


class TestSystemModels:
    def test_recording_metadata(self):
        meta = RecordingMetadata(
            recording_id="rec_001",
            camera_id="cam_001",
            trigger=RecordingTrigger.ALARM,
            start_timestamp=1234567890.0,
            start_sequence=100,
        )
        assert meta.recording_id == "rec_001"
        assert meta.is_active is False
        assert meta.is_complete is False

    def test_recording_metadata_active(self):
        meta = RecordingMetadata(
            recording_id="rec_001",
            camera_id="cam_001",
            state=RecordingState.RECORDING,
        )
        assert meta.is_active is True

    def test_recording_config(self):
        config = RecordingConfig(
            camera_id="cam_001",
            pre_alarm_seconds=10.0,
            post_alarm_seconds=30.0,
        )
        assert config.pre_alarm_seconds == 10.0
        assert config.post_alarm_seconds == 30.0

    def test_recording_config_validation(self):
        with pytest.raises(ValueError):
            RecordingConfig(camera_id="cam_001", pre_alarm_seconds=-1.0)

    def test_system_config(self):
        config = SystemConfig(
            max_cameras=8,
            processing_interval_ms=100,
        )
        assert config.max_cameras == 8
        assert config.default_mode.name == "CONFIGURATION"

    def test_system_config_validation(self):
        with pytest.raises(ValueError):
            SystemConfig(max_cameras=0)
        with pytest.raises(ValueError):
            SystemConfig(processing_interval_ms=0)

    def test_system_status(self):
        status = SystemStatus(
            mode=ApplicationMode.OBSERVER,
            camera_count=4,
            active_camera_count=3,
            acquiring_camera_count=3,
        )
        assert status.is_healthy is True

    def test_system_status_unhealthy(self):
        status = SystemStatus(
            mode=ApplicationMode.OBSERVER,
            camera_count=4,
            active_camera_count=0,
            last_error="No cameras connected",
        )
        assert status.is_healthy is False

    def test_system_status_storage_usage(self):
        status = SystemStatus(
            storage_total_bytes=1000000000,
            storage_free_bytes=200000000,
        )
        assert abs(status.storage_usage_percent - 80.0) < 0.01

    def test_application_state(self):
        config = SystemConfig()
        status = SystemStatus()
        state = ApplicationState(
            config=config,
            mode=ApplicationMode.CONFIGURATION,
            status=status,
        )
        assert state.is_configuration_mode is True
        assert state.is_observer_mode is False
        assert state.is_offline_mode is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])