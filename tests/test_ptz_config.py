"""Phase 6: PTZ configuration bindings (YAML parse/validate/save)."""

from __future__ import annotations

import pytest
import yaml

from thermal_monitor.config.manager import ConfigurationManager, ConfigurationError
from thermal_monitor.config.models import CameraMappingConfig, PTZConfig
from thermal_monitor.ptz.station import (
    build_service_config,
    merge_limits,
    resolve_binding,
    resolve_endpoint,
)


def _manager_with_yaml(tmp_path, snippet: dict) -> ConfigurationManager:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(snippet), encoding="utf-8")
    return ConfigurationManager(config_path=path)


class TestMappingValidation:
    def test_valid_binding_fields(self):
        entry = CameraMappingConfig(
            camera_id="cam_A",
            serial_number="SN-A",
            ptz_id="PTZ_01",
            ptz_endpoint="opc.tcp://127.0.0.1:4840",
            ptz_min_pan=-90.0,
            ptz_max_pan=90.0,
        )
        assert entry.ptz_id == "PTZ_01"

    def test_missing_ptz_is_backward_compatible(self):
        entry = CameraMappingConfig(camera_id="cam_A", serial_number="SN-A")
        assert entry.ptz_id == ""
        assert entry.ptz_endpoint == ""
        assert resolve_binding(entry.camera_id, entry.ptz_id) is None

    def test_blank_ptz_means_not_configured(self):
        assert resolve_binding("cam_A", "") is None
        assert resolve_binding("cam_A", "   ") is None

    def test_valid_binding_resolves(self):
        binding = resolve_binding("cam_A", "PTZ_01")
        assert binding is not None
        assert (binding.camera_id, binding.ptz_id) == ("cam_A", "PTZ_01")

    def test_duplicate_camera_ids_rejected(self, tmp_path):
        with pytest.raises(ConfigurationError):
            _manager_with_yaml(
                tmp_path,
                {
                    "cameras": {
                        "mapping": [
                            {"camera_id": "cam_A", "serial_number": "SN-A"},
                            {"camera_id": "cam_A", "serial_number": "SN-B"},
                        ]
                    }
                },
            )

    def test_invalid_limit_override_rejected(self):
        with pytest.raises(ValueError):
            CameraMappingConfig(
                camera_id="cam_A",
                serial_number="SN-A",
                ptz_min_pan=90.0,
                ptz_max_pan=-90.0,
            )


class TestPtzSection:
    def test_defaults_backward_compatible(self):
        config = PTZConfig()
        assert config.endpoint == ""
        assert config.velocity_mode == "single"
        assert config.tolerance_pan == 0.5

    def test_invalid_velocity_mode_rejected(self):
        with pytest.raises(ValueError):
            PTZConfig(velocity_mode="spiral")

    def test_negative_tolerance_rejected(self):
        with pytest.raises(ValueError):
            PTZConfig(tolerance_pan=-1.0)

    def test_yaml_roundtrip(self, tmp_path):
        manager = _manager_with_yaml(
            tmp_path,
            {
                "ptz": {
                    "endpoint": "opc.tcp://127.0.0.1:4840",
                    "velocity_mode": "per_axis",
                    "tolerance_pan": 0.2,
                },
                "cameras": {
                    "mapping": [
                        {
                            "camera_id": "cam_A",
                            "serial_number": "SN-A",
                            "ptz_id": "PTZ_01",
                        }
                    ]
                },
            },
        )
        config = manager.get_config()
        assert config.ptz.endpoint == "opc.tcp://127.0.0.1:4840"
        assert config.ptz.velocity_mode == "per_axis"
        assert config.cameras.mapping[0].ptz_id == "PTZ_01"

    def test_save_mapping_preserves_ptz(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "cameras": {
                        "mapping": [
                            {"camera_id": "cam_A", "serial_number": "SN-A"}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        manager = ConfigurationManager(config_path=path)
        manager.save_camera_mapping(
            CameraMappingConfig(
                camera_id="cam_A", serial_number="SN-A", ptz_id="PTZ_01"
            )
        )
        reloaded = ConfigurationManager(config_path=path)
        assert reloaded.get_config().cameras.mapping[0].ptz_id == "PTZ_01"


class TestStationHelpers:
    def test_merge_limits_with_overrides(self):
        limits = merge_limits(-170.0, 170.0, -90.0, 90.0, 0.5, 60.0)
        assert (limits.min_pan, limits.max_pan) == (-170.0, 170.0)
        narrowed = merge_limits(
            -170.0, 170.0, -90.0, 90.0, 0.5, 60.0, override_min_pan=-45.0
        )
        assert narrowed.min_pan == -45.0
        assert narrowed.max_pan == 170.0

    def test_endpoint_resolution(self):
        assert resolve_endpoint("global", "") == "global"
        assert resolve_endpoint("global", "override") == "override"
        assert resolve_endpoint("", "") == ""

    def test_service_config_builder(self):
        service_config = build_service_config(tolerance_pan=0.2)
        assert service_config.tolerance.pan == 0.2
        assert service_config.move_timeout_s > 0
