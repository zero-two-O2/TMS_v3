"""
processing.alarms -- Alarm evaluation engine.

Evaluates AnalysisResults against configured alarm rules and generates AlarmEvents.
Independent of acquisition, GUI, and database layers.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from thermal_monitor.core.models import (
    AlarmCondition,
    AlarmEvent,
    AlarmRule,
    AlarmSeverity,
    AnalysisConfig,
    AnalysisResult,
    ROIStatistics,
    TemperatureUnit,
)


@dataclass(frozen=True, slots=True)
class AlarmEvaluationResult:
    """Result of evaluating alarms for one frame."""

    frame_sequence: int
    frame_timestamp: float
    camera_id: str
    events: tuple[AlarmEvent, ...] = field(default_factory=tuple)
    active_alarms: tuple[str, ...] = field(default_factory=tuple)  # rule_ids currently active
    cleared_alarms: tuple[str, ...] = field(default_factory=tuple)  # rule_ids that were cleared


class AlarmStateTracker:
    """Tracks active alarm state for each rule.

    Maintains which alarms are currently active to detect new alarms
    and alarm clearances.
    """

    def __init__(self) -> None:
        self._active_rules: set[str] = set()
        self._rule_severity: dict[str, AlarmSeverity] = {}
        self._rule_roi: dict[str, str] = {}
        self._rule_camera: dict[str, str] = {}

    def is_active(self, rule_id: str) -> bool:
        return rule_id in self._active_rules

    def get_active_rules(self) -> set[str]:
        return self._active_rules.copy()

    def activate(self, rule: AlarmRule, camera_id: str) -> None:
        self._active_rules.add(rule.rule_id)
        self._rule_severity[rule.rule_id] = rule.severity
        self._rule_roi[rule.rule_id] = rule.roi_id
        self._rule_camera[rule.rule_id] = camera_id

    def deactivate(self, rule_id: str) -> bool:
        """Deactivate a rule. Returns True if it was active."""
        was_active = rule_id in self._active_rules
        self._active_rules.discard(rule_id)
        self._rule_severity.pop(rule_id, None)
        self._rule_roi.pop(rule_id, None)
        self._rule_camera.pop(rule_id, None)
        return was_active

    def get_rule_info(self, rule_id: str) -> tuple[AlarmSeverity, str, str] | None:
        """Get (severity, roi_id, camera_id) for a rule."""
        if rule_id not in self._active_rules:
            return None
        return (
            self._rule_severity[rule_id],
            self._rule_roi[rule_id],
            self._rule_camera[rule_id],
        )

    def clear(self) -> None:
        self._active_rules.clear()
        self._rule_severity.clear()
        self._rule_roi.clear()
        self._rule_camera.clear()


class AlarmEvaluator:
    """Evaluates AnalysisResults against AlarmRules to generate AlarmEvents.

    The evaluator is stateless except for the AlarmStateTracker which
    tracks which alarms are currently active. It does not write to
    databases or update UI - it only produces domain events.
    """

    def __init__(
        self,
        config: AnalysisConfig,
        event_callback: Optional[Callable[[AlarmEvent], None]] = None,
        state_tracker: Optional[AlarmStateTracker] = None,
    ) -> None:
        self._config = config
        self._event_callback = event_callback
        self._state_tracker = state_tracker or AlarmStateTracker()

    @property
    def config(self) -> AnalysisConfig:
        return self._config

    @property
    def state_tracker(self) -> AlarmStateTracker:
        return self._state_tracker

    def evaluate(self, result: AnalysisResult) -> AlarmEvaluationResult:
        """Evaluate all alarm rules against an AnalysisResult.

        Args:
            result: AnalysisResult containing per-ROI statistics.

        Returns:
            AlarmEvaluationResult with new/cleared alarm events.
        """
        events: list[AlarmEvent] = []
        newly_active: list[str] = []
        newly_cleared: list[str] = []

        # Get all rules from config (if any)
        rules = self._get_rules_from_config()
        if not rules:
            # No rules configured - check for cleared alarms
            for active_rule_id in self._state_tracker.get_active_rules():
                if self._state_tracker.deactivate(active_rule_id):
                    info = self._state_tracker.get_rule_info(active_rule_id)
                    if info:
                        severity, roi_id, camera_id = info
                        events.append(self._create_cleared_event(
                            active_rule_id, camera_id, roi_id, severity, result
                        ))
                        newly_cleared.append(active_rule_id)
            return AlarmEvaluationResult(
                frame_sequence=result.frame_sequence,
                frame_timestamp=result.frame_timestamp,
                camera_id=result.camera_id,
                events=tuple(events),
                active_alarms=tuple(self._state_tracker.get_active_rules()),
                cleared_alarms=tuple(newly_cleared),
            )

        # Evaluate each rule
        for rule in rules:
            if not rule.enabled:
                continue

            roi_stats = result.roi_results.get(rule.roi_id)
            if roi_stats is None:
                # ROI not in result - if alarm was active, clear it
                if self._state_tracker.is_active(rule.rule_id):
                    if self._state_tracker.deactivate(rule.rule_id):
                        events.append(self._create_cleared_event(
                            rule.rule_id, result.camera_id, rule.roi_id, rule.severity, result
                        ))
                        newly_cleared.append(rule.rule_id)
                continue

            # Evaluate condition
            severity = self._evaluate_rule(rule, roi_stats)
            was_active = self._state_tracker.is_active(rule.rule_id)

            if severity != AlarmSeverity.INFO:
                # Alarm condition met
                if not was_active:
                    # New alarm
                    self._state_tracker.activate(rule, result.camera_id)
                    event = self._create_alarm_event(rule, roi_stats, severity, result)
                    events.append(event)
                    newly_active.append(rule.rule_id)
                    if self._event_callback:
                        try:
                            self._event_callback(event)
                        except Exception:
                            pass  # Callback errors should not crash evaluation
                # else: already active, no new event
            else:
                # Condition not met - clear if was active
                if was_active:
                    if self._state_tracker.deactivate(rule.rule_id):
                        events.append(self._create_cleared_event(
                            rule.rule_id, result.camera_id, rule.roi_id, rule.severity, result
                        ))
                        newly_cleared.append(rule.rule_id)

        return AlarmEvaluationResult(
            frame_sequence=result.frame_sequence,
            frame_timestamp=result.frame_timestamp,
            camera_id=result.camera_id,
            events=tuple(events),
            active_alarms=tuple(self._state_tracker.get_active_rules()),
            cleared_alarms=tuple(newly_cleared),
        )

    def _get_rules_from_config(self) -> list[AlarmRule]:
        """Extract alarm rules from AnalysisConfig."""
        return list(self._config.alarm_rules.values())

    def _evaluate_rule(self, rule: AlarmRule, stats: ROIStatistics) -> AlarmSeverity:
        """Evaluate a single rule against ROI statistics."""
        # Proven V2 behavior: HIGH alarm uses maximum temperature
        if rule.condition == AlarmCondition.ABOVE:
            measured = stats.max_temp
        elif rule.condition == AlarmCondition.BELOW:
            measured = stats.min_temp
        elif rule.condition in (AlarmCondition.OUTSIDE_RANGE, AlarmCondition.INSIDE_RANGE):
            measured = stats.mean_temp
        else:
            measured = stats.mean_temp  # fallback

        # Convert threshold to ROI unit if needed
        threshold = rule.threshold
        threshold_low = rule.threshold_low
        threshold_high = rule.threshold_high

        if rule.unit != stats.unit:
            if threshold is not None:
                threshold = TemperatureUnit.convert(threshold, rule.unit, stats.unit)
            if threshold_low is not None:
                threshold_low = TemperatureUnit.convert(threshold_low, rule.unit, stats.unit)
            if threshold_high is not None:
                threshold_high = TemperatureUnit.convert(threshold_high, rule.unit, stats.unit)

        if rule.condition == AlarmCondition.ABOVE:
            if measured >= threshold:
                return rule.severity
        elif rule.condition == AlarmCondition.BELOW:
            if measured <= threshold:
                return rule.severity
        elif rule.condition == AlarmCondition.OUTSIDE_RANGE:
            if measured <= threshold_low or measured >= threshold_high:
                return rule.severity
        elif rule.condition == AlarmCondition.INSIDE_RANGE:
            if threshold_low < measured < threshold_high:
                return rule.severity
        elif rule.condition == AlarmCondition.RATE_OF_CHANGE:
            # Rate of change would need previous frame data
            # For now, delegate to TemperatureLimits if available
            pass

        return AlarmSeverity.INFO

    def _create_alarm_event(
        self,
        rule: AlarmRule,
        stats: ROIStatistics,
        severity: AlarmSeverity,
        result: AnalysisResult,
    ) -> AlarmEvent:
        """Create a new alarm event."""
        event_id = f"alarm_{uuid.uuid4().hex[:12]}"

        # Use the appropriate measured value based on condition
        if rule.condition == AlarmCondition.ABOVE:
            measured_value = stats.max_temp
        elif rule.condition == AlarmCondition.BELOW:
            measured_value = stats.min_temp
        else:
            measured_value = stats.mean_temp

        return AlarmEvent(
            event_id=event_id,
            rule_id=rule.rule_id,
            camera_id=result.camera_id,
            roi_id=rule.roi_id,
            severity=severity,
            measured_value=measured_value,
            threshold_value=rule.threshold if rule.threshold is not None else 0.0,
            timestamp=result.frame_timestamp,
            frame_sequence=result.frame_sequence,
            position_id=result.metadata.get("position_id") if isinstance(result.metadata.get("position_id"), str) else None,
        )

    def _create_cleared_event(
        self,
        rule_id: str,
        camera_id: str,
        roi_id: str,
        severity: AlarmSeverity,
        result: AnalysisResult,
    ) -> AlarmEvent:
        """Create an alarm cleared event."""
        event_id = f"clear_{uuid.uuid4().hex[:12]}"
        return AlarmEvent(
            event_id=event_id,
            rule_id=rule_id,
            camera_id=camera_id,
            roi_id=roi_id,
            severity=AlarmSeverity.INFO,  # Cleared events are INFO severity
            measured_value=0.0,
            threshold_value=0.0,
            timestamp=result.frame_timestamp,
            frame_sequence=result.frame_sequence,
            acknowledged=False,
        )

    def update_config(self, config: AnalysisConfig) -> None:
        """Update the analysis configuration."""
        self._config = config

    def reset(self) -> None:
        """Clear all alarm state."""
        self._state_tracker.clear()

    def get_active_alarms(self) -> list[tuple[str, AlarmSeverity, str, str]]:
        """Get list of active alarms as (rule_id, severity, roi_id, camera_id)."""
        active = []
        for rule_id in self._state_tracker.get_active_rules():
            info = self._state_tracker.get_rule_info(rule_id)
            if info:
                active.append((rule_id, info[0], info[1], info[2]))
        return active


class NullAlarmEvaluator:
    """Null evaluator that never generates alarms (for testing)."""

    def evaluate(self, result: AnalysisResult) -> AlarmEvaluationResult:
        return AlarmEvaluationResult(
            frame_sequence=result.frame_sequence,
            frame_timestamp=result.frame_timestamp,
            camera_id=result.camera_id,
        )

    def reset(self) -> None:
        pass

    def get_active_alarms(self) -> list:
        return []