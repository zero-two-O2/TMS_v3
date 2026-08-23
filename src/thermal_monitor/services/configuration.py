"""
services.configuration -- Configuration service for managing system configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from thermal_monitor.core.models import (
    AnalysisConfig,
    CameraConfig,
    CameraIdentity,
    PositionROIAssociation,
    RecordingConfig,
    ROIConfig,
    ROIGeometry,
    ROIShape,
    SystemConfig,
    TemperatureLimits,
    TemperatureUnit,
)
from thermal_monitor.config import ROIConfig as ConfigROIConfig, RecordingConfig as ConfigRecordingConfig


@dataclass
class ConfigurationService:
    """Application-level service for configuration management.

    Provides a unified interface for accessing and modifying all
    system configurations. Delegates to repositories for persistence.
    """

    system_config: SystemConfig = field(default_factory=SystemConfig)
    camera_configs: dict[str, CameraConfig] = field(default_factory=dict)
    analysis_configs: dict[str, AnalysisConfig] = field(default_factory=dict)
    recording_configs: dict[str, RecordingConfig] = field(default_factory=dict)

    # Default configurations from config.yaml (injected at startup)
    _roi_defaults: ConfigROIConfig | None = field(default=None, repr=False)
    _recording_defaults: ConfigRecordingConfig | None = field(default=None, repr=False)

    # Change callbacks
    _camera_change_callbacks: list[Callable[[str, CameraConfig], None]] = field(default_factory=list)
    _analysis_change_callbacks: list[Callable[[str, AnalysisConfig], None]] = field(default_factory=list)
    _system_change_callbacks: list[Callable[[SystemConfig], None]] = field(default_factory=list)
    _recording_change_callbacks: list[Callable[[str, RecordingConfig], None]] = field(default_factory=list)

    def set_defaults(self, roi_defaults: ConfigROIConfig, recording_defaults: ConfigRecordingConfig) -> None:
        """Set default configurations from config.yaml."""
        self._roi_defaults = roi_defaults
        self._recording_defaults = recording_defaults

    # --- Camera Configuration ---

    def get_camera_config(self, camera_id: str) -> CameraConfig | None:
        return self.camera_configs.get(camera_id)

    def get_all_camera_configs(self) -> list[CameraConfig]:
        return list(self.camera_configs.values())

    def set_camera_config(self, config: CameraConfig) -> None:
        self.camera_configs[config.identity.camera_id] = config
        self._notify_camera_change(config.identity.camera_id, config)

    def remove_camera_config(self, camera_id: str) -> bool:
        if camera_id in self.camera_configs:
            del self.camera_configs[camera_id]
            return True
        return False

    def add_camera_change_callback(self, callback: Callable[[str, CameraConfig], None]) -> None:
        self._camera_change_callbacks.append(callback)

    def remove_camera_change_callback(self, callback: Callable[[str, CameraConfig], None]) -> None:
        try:
            self._camera_change_callbacks.remove(callback)
        except ValueError:
            pass

    def _notify_camera_change(self, camera_id: str, config: CameraConfig) -> None:
        for cb in self._camera_change_callbacks:
            try:
                cb(camera_id, config)
            except Exception:
                pass

    # --- Analysis Configuration ---

    def get_analysis_config(self, camera_id: str) -> AnalysisConfig | None:
        return self.analysis_configs.get(camera_id)

    def get_all_analysis_configs(self) -> list[AnalysisConfig]:
        return list(self.analysis_configs.values())

    def set_analysis_config(self, config: AnalysisConfig) -> None:
        self.analysis_configs[config.camera_id] = config
        self._notify_analysis_change(config.camera_id, config)

    def remove_analysis_config(self, camera_id: str) -> bool:
        if camera_id in self.analysis_configs:
            del self.analysis_configs[camera_id]
            return True
        return False

    def add_analysis_change_callback(self, callback: Callable[[str, AnalysisConfig], None]) -> None:
        self._analysis_change_callbacks.append(callback)

    def remove_analysis_change_callback(self, callback: Callable[[str, AnalysisConfig], None]) -> None:
        try:
            self._analysis_change_callbacks.remove(callback)
        except ValueError:
            pass

    def _notify_analysis_change(self, camera_id: str, config: AnalysisConfig) -> None:
        for cb in self._analysis_change_callbacks:
            try:
                cb(camera_id, config)
            except Exception:
                pass

    # --- Recording Configuration ---

    def get_recording_config(self, camera_id: str) -> RecordingConfig | None:
        return self.recording_configs.get(camera_id)

    def get_all_recording_configs(self) -> list[RecordingConfig]:
        return list(self.recording_configs.values())

    def set_recording_config(self, config: RecordingConfig) -> None:
        self.recording_configs[config.camera_id] = config
        self._notify_recording_change(config.camera_id, config)

    def remove_recording_config(self, camera_id: str) -> bool:
        if camera_id in self.recording_configs:
            del self.recording_configs[camera_id]
            return True
        return False

    def add_recording_change_callback(self, callback: Callable[[str, RecordingConfig], None]) -> None:
        self._recording_change_callbacks.append(callback)

    def remove_recording_change_callback(self, callback: Callable[[str, RecordingConfig], None]) -> None:
        try:
            self._recording_change_callbacks.remove(callback)
        except ValueError:
            pass

    def _notify_recording_change(self, camera_id: str, config: RecordingConfig) -> None:
        for cb in self._recording_change_callbacks:
            try:
                cb(camera_id, config)
            except Exception:
                pass

    # --- System Configuration ---

    def update_system_config(self, config: SystemConfig) -> None:
        self.system_config = config
        self._notify_system_change(config)

    def add_system_change_callback(self, callback: Callable[[SystemConfig], None]) -> None:
        self._system_change_callbacks.append(callback)

    def remove_system_change_callback(self, callback: Callable[[SystemConfig], None]) -> None:
        try:
            self._system_change_callbacks.remove(callback)
        except ValueError:
            pass

    def _notify_system_change(self, config: SystemConfig) -> None:
        for cb in self._system_change_callbacks:
            try:
                cb(config)
            except Exception:
                pass

    # --- Convenience factory methods ---

    def create_camera_identity(
        self,
        camera_id: str,
        serial_number: str,
        model: str = "",
        vendor: str = "",
        firmware: str = "",
        user_name: str = "",
    ) -> CameraIdentity:
        return CameraIdentity(
            camera_id=camera_id,
            serial_number=serial_number,
            model=model,
            vendor=vendor,
            firmware=firmware,
            user_name=user_name,
        )

    def create_camera_config(
        self,
        identity: CameraIdentity,
        name: str = "",
        description: str = "",
        thermal_enabled: bool = True,
        visible_enabled: bool = False,
    ) -> CameraConfig:
        return CameraConfig(
            identity=identity,
            name=name,
            description=description,
            thermal_enabled=thermal_enabled,
            visible_enabled=visible_enabled,
        )

    def create_roi_config(
        self,
        roi_id: str,
        name: str = "",
        shape: ROIShape = ROIShape.RECTANGLE1,
        parameters: dict | None = None,
        unit: TemperatureUnit = TemperatureUnit.CELSIUS,
        min_warning: float | None = None,
        max_warning: float | None = None,
        min_critical: float | None = None,
        max_critical: float | None = None,
    ) -> ROIConfig:
        if parameters is None:
            if self._roi_defaults is not None:
                defaults_dict = self._roi_defaults.defaults
                if shape.value in defaults_dict:
                    parameters = defaults_dict[shape.value]
                else:
                    parameters = self._get_fallback_roi_params(shape)
            else:
                parameters = self._get_fallback_roi_params(shape)

        geometry = ROIGeometry(shape=shape, parameters=parameters)
        limits = TemperatureLimits(
            unit=unit,
            min_warning=min_warning,
            max_warning=max_warning,
            min_critical=min_critical,
            max_critical=max_critical,
        )
        return ROIConfig(
            roi_id=roi_id,
            name=name,
            geometry=geometry,
            temperature_limits=limits,
        )

    def _get_fallback_roi_params(self, shape: ROIShape) -> dict:
        """Fallback ROI parameters if config is not available."""
        if shape == ROIShape.RECTANGLE1:
            return {"y1": 0.0, "x1": 0.0, "y2": 100.0, "x2": 100.0}
        elif shape == ROIShape.RECTANGLE2:
            return {"center_y": 0.0, "center_x": 0.0, "phi": 0.0, "length1": 50.0, "length2": 50.0}
        elif shape == ROIShape.CIRCLE:
            return {"center_y": 0.0, "center_x": 0.0, "radius": 50.0}
        elif shape == ROIShape.ELLIPSE:
            return {"center_y": 0.0, "center_x": 0.0, "phi": 0.0, "radius1": 50.0, "radius2": 30.0}
        elif shape == ROIShape.POLYGON:
            return {"points": [(0.0, 0.0), (100.0, 0.0), (50.0, 100.0)]}
        return {}

    def create_position_roi_association(
        self,
        position_id: str,
        position_name: str = "",
        roi_ids: list[str] | None = None,
    ) -> PositionROIAssociation:
        return PositionROIAssociation(
            position_id=position_id,
            position_name=position_name,
            roi_ids=tuple(roi_ids or []),
        )

    def create_analysis_config(
        self,
        camera_id: str,
        rois: dict[str, ROIConfig] | None = None,
        position_associations: dict[str, PositionROIAssociation] | None = None,
    ) -> AnalysisConfig:
        return AnalysisConfig(
            camera_id=camera_id,
            rois=rois or {},
            position_associations=position_associations or {},
        )

    def create_recording_config(
        self,
        camera_id: str,
        enabled: bool = True,
        pre_alarm_seconds: float | None = None,
        post_alarm_seconds: float | None = None,
    ) -> RecordingConfig:
        if self._recording_defaults is not None:
            pre = pre_alarm_seconds if pre_alarm_seconds is not None else self._recording_defaults.pre_alarm_seconds
            post = post_alarm_seconds if post_alarm_seconds is not None else self._recording_defaults.post_alarm_seconds
        else:
            pre = pre_alarm_seconds if pre_alarm_seconds is not None else 10.0
            post = post_alarm_seconds if post_alarm_seconds is not None else 30.0
        return RecordingConfig(
            camera_id=camera_id,
            enabled=enabled,
            pre_alarm_seconds=pre,
            post_alarm_seconds=post,
        )