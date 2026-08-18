"""
services.alarm -- Alarm service for coordinating alarm evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from thermal_monitor.core.models import (
    AlarmEvent,
    AlarmSeverity,
    AnalysisConfig,
    AnalysisResult,
)
from thermal_monitor.processing.alarms import AlarmEvaluator, AlarmEvaluationResult, AlarmStateTracker


@dataclass
class AlarmService:
    """Application-level service for coordinating alarm evaluation.

    Manages alarm evaluators for each camera and coordinates
    alarm evaluation with analysis results.
    """

    evaluators: dict[str, AlarmEvaluator] = dataclasses.field(default_factory=dict)
    state_trackers: dict[str, AlarmStateTracker] = dataclasses.field(default_factory=dict)
    _event_callbacks: dict[str, list[Callable[[AlarmEvent], None]]] = dataclasses.field(default_factory=dict)
    _evaluation_callbacks: dict[str, list[Callable[[AlarmEvaluationResult], None]]] = dataclasses.field(default_factory=dict)

    def __init__(self) -> None:
        pass

    def get_evaluator(self, camera_id: str) -> AlarmEvaluator | None:
        return self.evaluators.get(camera_id)

    def get_all_evaluators(self) -> list[AlarmEvaluator]:
        return list(self.evaluators.values())

    def create_evaluator(self, config: AnalysisConfig) -> AlarmEvaluator:
        evaluator = AlarmEvaluator(config=config)
        self.evaluators[config.camera_id] = evaluator
        return evaluator

    def remove_evaluator(self, camera_id: str) -> bool:
        if camera_id in self.evaluators:
            del self.evaluators[camera_id]
            return True
        return False

    def get_state_tracker(self, camera_id: str) -> AlarmStateTracker | None:
        return self.state_trackers.get(camera_id)

    def evaluate(self, result: AnalysisResult, config: AnalysisConfig) -> AlarmEvaluationResult:
        """Evaluate alarms for an analysis result."""
        evaluator = self.evaluators.get(result.camera_id)
        if evaluator is None:
            evaluator = AlarmEvaluator(config=config)
            self.evaluators[result.camera_id] = evaluator

        eval_result = evaluator.evaluate(result)
        self._notify_event(eval_result)
        self._notify_evaluation(eval_result)
        return eval_result

    def evaluate_with_stored_config(self, result: AnalysisResult) -> AlarmEvaluationResult:
        """Evaluate using the evaluator's stored config."""
        evaluator = self.evaluators.get(result.camera_id)
        if evaluator is None:
            raise ValueError(f"No alarm evaluator for camera {result.camera_id}")
        eval_result = evaluator.evaluate(result)
        self._notify_event(eval_result)
        self._notify_evaluation(eval_result)
        return eval_result

    def get_active_alarms(self, camera_id: str) -> list[tuple[str, AlarmSeverity, str, str]]:
        evaluator = self.evaluators.get(camera_id)
        if evaluator is not None:
            return evaluator.get_active_alarms()
        return []

    def get_all_active_alarms(self) -> dict[str, list[tuple[str, AlarmSeverity, str, str]]]:
        return {cam_id: self.get_active_alarms(cam_id) for cam_id in self.evaluators}

    def add_event_callback(self, camera_id: str, callback: Callable[[AlarmEvent], None]) -> None:
        if camera_id not in self._event_callbacks:
            self._event_callbacks[camera_id] = []
        self._event_callbacks[camera_id].append(callback)

    def remove_event_callback(self, camera_id: str, callback: Callable[[AlarmEvent], None]) -> None:
        if camera_id in self._event_callbacks:
            try:
                self._event_callbacks[camera_id].remove(callback)
            except ValueError:
                pass

    def add_evaluation_callback(self, camera_id: str, callback: Callable[[AlarmEvaluationResult], None]) -> None:
        if camera_id not in self._evaluation_callbacks:
            self._evaluation_callbacks[camera_id] = []
        self._evaluation_callbacks[camera_id].append(callback)

    def remove_evaluation_callback(self, camera_id: str, callback: Callable[[AlarmEvaluationResult], None]) -> None:
        if camera_id in self._evaluation_callbacks:
            try:
                self._evaluation_callbacks[camera_id].remove(callback)
            except ValueError:
                pass

    def _notify_event(self, eval_result: AlarmEvaluationResult) -> None:
        for event in eval_result.events:
            callbacks = self._event_callbacks.get(event.camera_id, [])
            for cb in callbacks:
                try:
                    cb(event)
                except Exception:
                    pass

    def _notify_evaluation(self, eval_result: AlarmEvaluationResult) -> None:
        callbacks = self._evaluation_callbacks.get(eval_result.camera_id, [])
        for cb in callbacks:
            try:
                cb(eval_result)
            except Exception:
                pass

    def reset_camera(self, camera_id: str) -> None:
        """Reset alarm state for a camera."""
        if camera_id in self.evaluators:
            self.evaluators[camera_id].reset()
        if camera_id in self.state_trackers:
            self.state_trackers[camera_id].clear()

    def reset_all(self) -> None:
        """Reset all alarm state."""
        for evaluator in self.evaluators.values():
            evaluator.reset()
        for tracker in self.state_trackers.values():
            tracker.clear()