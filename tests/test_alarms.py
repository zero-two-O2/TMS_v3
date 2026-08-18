"""Tests for alarm evaluation engine."""

from __future__ import annotations

import pytest

from thermal_monitor.core.models import (
    AlarmCondition,
    AlarmRule,
    AlarmSeverity,
    AnalysisConfig,
    AnalysisResult,
    PositionROIAssociation,
    ROIConfig,
    ROIGeometry,
    ROIStatistics,
    ROIShape,
    TemperatureLimits,
    TemperatureUnit,
)
from thermal_monitor.processing.alarms import (
    AlarmEvaluationResult,
    AlarmEvaluator,
    AlarmStateTracker,
    NullAlarmEvaluator,
)


class TestAlarmStateTracker:
    def test_initial_state_empty(self):
        tracker = AlarmStateTracker()
        assert tracker.get_active_rules() == set()
        assert tracker.is_active("rule_1") is False

    def test_activate_deactivate(self):
        tracker = AlarmStateTracker()
        rule = AlarmRule(
            rule_id="rule_1",
            roi_id="roi_1",
            condition=AlarmCondition.ABOVE,
            severity=AlarmSeverity.WARNING,
            threshold=80.0,
        )
        tracker.activate(rule, "cam_1")
        assert tracker.is_active("rule_1") is True
        assert tracker.get_active_rules() == {"rule_1"}

        info = tracker.get_rule_info("rule_1")
        assert info is not None
        severity, roi_id, camera_id = info
        assert severity == AlarmSeverity.WARNING
        assert roi_id == "roi_1"
        assert camera_id == "cam_1"

        was_active = tracker.deactivate("rule_1")
        assert was_active is True
        assert tracker.is_active("rule_1") is False

    def test_deactivate_inactive(self):
        tracker = AlarmStateTracker()
        was_active = tracker.deactivate("rule_1")
        assert was_active is False

    def test_clear(self):
        tracker = AlarmStateTracker()
        rule = AlarmRule(
            rule_id="rule_1",
            roi_id="roi_1",
            condition=AlarmCondition.ABOVE,
            severity=AlarmSeverity.WARNING,
            threshold=80.0,
        )
        tracker.activate(rule, "cam_1")
        tracker.clear()
        assert tracker.get_active_rules() == set()


class TestAlarmEvaluator:
    def create_test_result(
        self,
        sequence: int = 0,
        roi_mean: float = 50.0,
        roi_id: str = "roi_1",
    ) -> AnalysisResult:
        stats = ROIStatistics(
            roi_id=roi_id,
            roi_name="Test ROI",
            min_temp=roi_mean - 5,
            max_temp=roi_mean + 5,
            mean_temp=roi_mean,
            deviation=2.0,
            unit=TemperatureUnit.CELSIUS,
        )
        return AnalysisResult(
            camera_id="cam_1",
            frame_sequence=sequence,
            frame_timestamp=1234567890.0 + sequence,
            roi_results={roi_id: stats},
        )

    def create_config_with_rule(self, rule: AlarmRule) -> AnalysisConfig:
        roi = ROIConfig(
            roi_id="roi_1",
            name="Test ROI",
            geometry=ROIGeometry(shape=ROIShape.RECTANGLE1, parameters={"y1": 0, "x1": 0, "y2": 10, "x2": 10}),
        )
        assoc = PositionROIAssociation(position_id="default", roi_ids=("roi_1",))
        config = AnalysisConfig(
            camera_id="cam_1",
            rois={"roi_1": roi},
            position_associations={"default": assoc},
            alarm_rules={rule.rule_id: rule},
        )
        return config

    def test_no_rules_no_alarms(self):
        config = AnalysisConfig(camera_id="cam_1")
        evaluator = AlarmEvaluator(config=config)
        result = self.create_test_result(0, 100.0)

        eval_result = evaluator.evaluate(result)

        assert len(eval_result.events) == 0
        assert len(eval_result.active_alarms) == 0

    def test_above_threshold_creates_alarm(self):
        rule = AlarmRule(
            rule_id="rule_1",
            roi_id="roi_1",
            condition=AlarmCondition.ABOVE,
            severity=AlarmSeverity.WARNING,
            threshold=80.0,
        )
        config = self.create_config_with_rule(rule)
        evaluator = AlarmEvaluator(config=config)

        # Below threshold - no alarm
        result = self.create_test_result(0, 50.0)
        eval_result = evaluator.evaluate(result)
        assert len(eval_result.events) == 0
        assert len(eval_result.active_alarms) == 0

        # Above threshold - alarm created
        result = self.create_test_result(1, 90.0)
        eval_result = evaluator.evaluate(result)
        assert len(eval_result.events) == 1
        assert eval_result.events[0].severity == AlarmSeverity.WARNING
        assert eval_result.events[0].rule_id == "rule_1"
        assert len(eval_result.active_alarms) == 1

        # Stay above - no new event
        result = self.create_test_result(2, 95.0)
        eval_result = evaluator.evaluate(result)
        assert len(eval_result.events) == 0
        assert len(eval_result.active_alarms) == 1

    def test_below_threshold_clears_alarm(self):
        rule = AlarmRule(
            rule_id="rule_1",
            roi_id="roi_1",
            condition=AlarmCondition.ABOVE,
            severity=AlarmSeverity.WARNING,
            threshold=80.0,
        )
        config = self.create_config_with_rule(rule)
        evaluator = AlarmEvaluator(config=config)

        # Trigger alarm
        result = self.create_test_result(0, 90.0)
        evaluator.evaluate(result)
        assert len(evaluator.state_tracker.get_active_rules()) == 1

        # Drop below - alarm cleared
        result = self.create_test_result(1, 50.0)
        eval_result = evaluator.evaluate(result)
        assert len(eval_result.events) == 1
        assert eval_result.events[0].severity == AlarmSeverity.INFO  # Clear event
        assert "rule_1" in eval_result.cleared_alarms
        assert len(eval_result.active_alarms) == 0

    def test_below_threshold_creates_alarm(self):
        rule = AlarmRule(
            rule_id="rule_1",
            roi_id="roi_1",
            condition=AlarmCondition.BELOW,
            severity=AlarmSeverity.CRITICAL,
            threshold=20.0,
        )
        config = self.create_config_with_rule(rule)
        evaluator = AlarmEvaluator(config=config)

        result = self.create_test_result(0, 15.0)
        eval_result = evaluator.evaluate(result)
        assert len(eval_result.events) == 1
        assert eval_result.events[0].severity == AlarmSeverity.CRITICAL

    def test_outside_range_creates_alarm(self):
        rule = AlarmRule(
            rule_id="rule_1",
            roi_id="roi_1",
            condition=AlarmCondition.OUTSIDE_RANGE,
            severity=AlarmSeverity.WARNING,
            threshold=0,  # Not used
            threshold_low=10.0,
            threshold_high=90.0,
        )
        config = self.create_config_with_rule(rule)
        evaluator = AlarmEvaluator(config=config)

        # Inside range - no alarm
        result = self.create_test_result(0, 50.0)
        eval_result = evaluator.evaluate(result)
        assert len(eval_result.events) == 0

        # Below low - alarm
        result = self.create_test_result(1, 5.0)
        eval_result = evaluator.evaluate(result)
        assert len(eval_result.events) == 1
        assert len(eval_result.active_alarms) == 1

        # Above high - already active, no new event
        result = self.create_test_result(2, 100.0)
        eval_result = evaluator.evaluate(result)
        assert len(eval_result.events) == 0  # No new event, already active
        assert len(eval_result.active_alarms) == 1

    def test_inside_range_creates_alarm(self):
        rule = AlarmRule(
            rule_id="rule_1",
            roi_id="roi_1",
            condition=AlarmCondition.INSIDE_RANGE,
            severity=AlarmSeverity.WARNING,  # INFO is treated as "no alarm"
            threshold=0,
            threshold_low=40.0,
            threshold_high=60.0,
        )
        config = self.create_config_with_rule(rule)
        evaluator = AlarmEvaluator(config=config)

        # Outside - no alarm
        result = self.create_test_result(0, 30.0)
        eval_result = evaluator.evaluate(result)
        assert len(eval_result.events) == 0

        # Inside - alarm
        result = self.create_test_result(1, 50.0)
        eval_result = evaluator.evaluate(result)
        assert len(eval_result.events) == 1

    def test_disabled_rule_no_alarm(self):
        rule = AlarmRule(
            rule_id="rule_1",
            roi_id="roi_1",
            condition=AlarmCondition.ABOVE,
            severity=AlarmSeverity.WARNING,
            threshold=80.0,
            enabled=False,
        )
        config = self.create_config_with_rule(rule)
        evaluator = AlarmEvaluator(config=config)

        result = self.create_test_result(0, 90.0)
        eval_result = evaluator.evaluate(result)
        assert len(eval_result.events) == 0

    def test_missing_roi_clears_active_alarm(self):
        rule = AlarmRule(
            rule_id="rule_1",
            roi_id="roi_1",
            condition=AlarmCondition.ABOVE,
            severity=AlarmSeverity.WARNING,
            threshold=80.0,
        )
        config = self.create_config_with_rule(rule)
        evaluator = AlarmEvaluator(config=config)

        # Trigger alarm
        result = self.create_test_result(0, 90.0)
        evaluator.evaluate(result)
        assert len(evaluator.state_tracker.get_active_rules()) == 1

        # Result without ROI
        result = AnalysisResult(
            camera_id="cam_1",
            frame_sequence=1,
            frame_timestamp=1234567891.0,
            roi_results={},
        )
        eval_result = evaluator.evaluate(result)
        assert len(eval_result.cleared_alarms) == 1
        assert len(eval_result.active_alarms) == 0

    def test_event_callback(self):
        rule = AlarmRule(
            rule_id="rule_1",
            roi_id="roi_1",
            condition=AlarmCondition.ABOVE,
            severity=AlarmSeverity.WARNING,
            threshold=80.0,
        )
        config = self.create_config_with_rule(rule)
        callback_events: list = []

        def callback(event):
            callback_events.append(event)

        evaluator = AlarmEvaluator(config=config, event_callback=callback)

        result = self.create_test_result(0, 90.0)
        evaluator.evaluate(result)

        assert len(callback_events) == 1
        assert callback_events[0].rule_id == "rule_1"

    def test_callback_exception_does_not_crash(self):
        rule = AlarmRule(
            rule_id="rule_1",
            roi_id="roi_1",
            condition=AlarmCondition.ABOVE,
            severity=AlarmSeverity.WARNING,
            threshold=80.0,
        )
        config = self.create_config_with_rule(rule)

        def bad_callback(event):
            raise RuntimeError("callback error")

        evaluator = AlarmEvaluator(config=config, event_callback=bad_callback)

        # Should not raise
        result = self.create_test_result(0, 90.0)
        evaluator.evaluate(result)

    def test_reset(self):
        rule = AlarmRule(
            rule_id="rule_1",
            roi_id="roi_1",
            condition=AlarmCondition.ABOVE,
            severity=AlarmSeverity.WARNING,
            threshold=80.0,
        )
        config = self.create_config_with_rule(rule)
        evaluator = AlarmEvaluator(config=config)

        result = self.create_test_result(0, 90.0)
        evaluator.evaluate(result)
        assert len(evaluator.state_tracker.get_active_rules()) == 1

        evaluator.reset()
        assert len(evaluator.state_tracker.get_active_rules()) == 0

    def test_get_active_alarms(self):
        rule = AlarmRule(
            rule_id="rule_1",
            roi_id="roi_1",
            condition=AlarmCondition.ABOVE,
            severity=AlarmSeverity.WARNING,
            threshold=80.0,
        )
        config = self.create_config_with_rule(rule)
        evaluator = AlarmEvaluator(config=config)

        result = self.create_test_result(0, 90.0)
        evaluator.evaluate(result)

        active = evaluator.get_active_alarms()
        assert len(active) == 1
        rule_id, severity, roi_id, camera_id = active[0]
        assert rule_id == "rule_1"
        assert severity == AlarmSeverity.WARNING
        assert roi_id == "roi_1"
        assert camera_id == "cam_1"

    def test_different_roi_no_interference(self):
        rule1 = AlarmRule(
            rule_id="rule_1",
            roi_id="roi_1",
            condition=AlarmCondition.ABOVE,
            severity=AlarmSeverity.WARNING,
            threshold=80.0,
        )
        rule2 = AlarmRule(
            rule_id="rule_2",
            roi_id="roi_2",
            condition=AlarmCondition.ABOVE,
            severity=AlarmSeverity.CRITICAL,
            threshold=80.0,
        )
        roi1 = ROIConfig(
            roi_id="roi_1",
            name="ROI 1",
            geometry=ROIGeometry(shape=ROIShape.RECTANGLE1, parameters={"y1": 0, "x1": 0, "y2": 10, "x2": 10}),
        )
        roi2 = ROIConfig(
            roi_id="roi_2",
            name="ROI 2",
            geometry=ROIGeometry(shape=ROIShape.RECTANGLE1, parameters={"y1": 20, "x1": 20, "y2": 30, "x2": 30}),
        )
        assoc = PositionROIAssociation(position_id="default", roi_ids=("roi_1", "roi_2"))
        config = AnalysisConfig(
            camera_id="cam_1",
            rois={"roi_1": roi1, "roi_2": roi2},
            position_associations={"default": assoc},
            alarm_rules={rule1.rule_id: rule1, rule2.rule_id: rule2},
        )
        evaluator = AlarmEvaluator(config=config)

        # Trigger only rule1
        stats1 = ROIStatistics(roi_id="roi_1", roi_name="ROI 1", min_temp=90, max_temp=90, mean_temp=90, deviation=0, unit=TemperatureUnit.CELSIUS)
        stats2 = ROIStatistics(roi_id="roi_2", roi_name="ROI 2", min_temp=50, max_temp=50, mean_temp=50, deviation=0, unit=TemperatureUnit.CELSIUS)
        result = AnalysisResult(
            camera_id="cam_1",
            frame_sequence=0,
            frame_timestamp=1234567890.0,
            roi_results={"roi_1": stats1, "roi_2": stats2},
        )
        eval_result = evaluator.evaluate(result)
        assert len(eval_result.active_alarms) == 1
        assert "rule_1" in eval_result.active_alarms
        assert "rule_2" not in eval_result.active_alarms


class TestNullAlarmEvaluator:
    def test_never_creates_alarms(self):
        evaluator = NullAlarmEvaluator()
        result = AnalysisResult(
            camera_id="cam_1",
            frame_sequence=0,
            frame_timestamp=1234567890.0,
            roi_results={},
        )
        eval_result = evaluator.evaluate(result)
        assert len(eval_result.events) == 0
        assert len(eval_result.active_alarms) == 0

    def test_reset_noop(self):
        evaluator = NullAlarmEvaluator()
        evaluator.reset()  # Should not raise


class TestAlarmEvaluationResult:
    def test_contains_expected_fields(self):
        result = AlarmEvaluationResult(
            frame_sequence=1,
            frame_timestamp=1234567890.0,
            camera_id="cam_1",
            events=(),
            active_alarms=("rule_1",),
            cleared_alarms=(),
        )
        assert result.frame_sequence == 1
        assert result.camera_id == "cam_1"
        assert result.active_alarms == ("rule_1",)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])