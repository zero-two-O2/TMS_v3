"""
Tests for the TMS V3 configuration infrastructure.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from thermal_monitor.config import (
    ConfigurationManager,
    ConfigurationError,
    create_config_manager,
    AppConfig,
    DatabaseConfig,
    CamerasConfig,
    LoggingConfig,
    UIConfig,
    PTZLimitsConfig,
    StorageConfig,
    CameraAcquisitionConfig,
    OfflinePlaybackConfig,
)


class TestConfigurationManager:
    """Test ConfigurationManager basic functionality."""

    def test_default_config_loads(self):
        """Test that default configuration loads without a YAML file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            manager = ConfigurationManager(config_path=config_path, create_default=False)
            config = manager.get_config()

            assert isinstance(config, AppConfig)
            assert config.application.name == "Thermal Monitoring System V3"
            assert config.database.enabled is False
            assert config.cameras.acquisition.target_fps == 9
            assert config.storage.root == "data"

    def test_yaml_config_loads(self):
        """Test loading configuration from YAML file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            config_path = config_dir / "config.yaml"

            config_data = {
                "application": {"name": "Test App", "default_mode": "LIVE"},
                "database": {"enabled": True, "host": "localhost", "port": 1433},
                "cameras": {"acquisition": {"target_fps": 15}},
            }
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            manager = ConfigurationManager(config_path=config_path)
            config = manager.get_config()

            assert config.application.name == "Test App"
            assert config.application.default_mode == "LIVE"
            assert config.database.enabled is True
            assert config.database.host == "localhost"
            assert config.database.port == 1433
            assert config.cameras.acquisition.target_fps == 15

    def test_missing_config_uses_defaults_in_production(self):
        """Test that missing config uses built-in defaults in production mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            # Don't create the file, don't allow default creation, force production mode
            manager = ConfigurationManager(config_path=config_path, create_default=False, app_root=tmpdir)
            config = manager.get_config()
            # Should use built-in defaults
            assert config.application.name == "Thermal Monitoring System V3"
            assert config.database.enabled is False
            assert config.cameras.acquisition.target_fps == 9

    def test_missing_config_creates_default_in_dev(self):
        """Test that missing config creates default in dev mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            manager = ConfigurationManager(config_path=config_path, create_default=True)
            assert config_path.exists()
            config = manager.get_config()
            assert config.application.name == "Thermal Monitoring System V3"

    def test_malformed_yaml_handled(self):
        """Test that malformed YAML raises ConfigurationError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_path.write_text("invalid: yaml: [unclosed")

            with pytest.raises(ConfigurationError, match="Invalid YAML"):
                ConfigurationManager(config_path=config_path)

    def test_invalid_database_port_rejected(self):
        """Test that invalid database port is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_data = {"database": {"port": 70000}}
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            with pytest.raises(ConfigurationError, match="database.port must be between 1 and 65535"):
                ConfigurationManager(config_path=config_path)

    def test_invalid_fps_rejected(self):
        """Test that invalid FPS is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_data = {"cameras": {"acquisition": {"target_fps": 0}}}
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            with pytest.raises(ConfigurationError, match="target_fps must be > 0"):
                ConfigurationManager(config_path=config_path)

    def test_invalid_logging_level_rejected(self):
        """Test that invalid logging level is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_data = {"logging": {"level": "INVALID"}}
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            with pytest.raises(ConfigurationError, match="logging.level must be one of"):
                ConfigurationManager(config_path=config_path)

    def test_invalid_ui_theme_rejected(self):
        """Test that invalid UI theme is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_data = {"ui": {"theme": "invalid"}}
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            with pytest.raises(ConfigurationError, match="ui.theme must be"):
                ConfigurationManager(config_path=config_path)

    def test_path_resolution_relative(self):
        """Test relative storage path resolution."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_data = {"storage": {"root": "mydata", "recordings": "recordings"}}
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            manager = ConfigurationManager(config_path=config_path, app_root=tmpdir)
            storage_root = manager.resolve_storage_root()
            recordings_path = manager.resolve_storage_path("recordings")

            assert storage_root == Path(tmpdir) / "mydata"
            assert recordings_path == Path(tmpdir) / "mydata" / "recordings"

    def test_path_resolution_absolute(self):
        """Test absolute storage path is preserved."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            # Use a Windows-compatible absolute path
            absolute_root = str(Path(tmpdir).drive + "/absolute/path/data")
            config_data = {"storage": {"root": absolute_root}}
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            manager = ConfigurationManager(config_path=config_path)
            storage_root = manager.resolve_storage_root()

            assert storage_root == Path(absolute_root)

    def test_tms_db_password_env_override(self):
        """Test TMS_DB_PASSWORD environment variable override."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_data = {"database": {"password_env": "TMS_DB_PASSWORD"}}
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            os.environ["TMS_DB_PASSWORD"] = "secret123"
            try:
                manager = ConfigurationManager(config_path=config_path)
                password = manager.get_database_password()
                assert password == "secret123"
            finally:
                del os.environ["TMS_DB_PASSWORD"]

    def test_secrets_not_in_error_messages(self):
        """Test that secrets are not exposed in error messages."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_data = {"database": {"port": -1}}
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            with pytest.raises(ConfigurationError) as exc_info:
                ConfigurationManager(config_path=config_path)
            error_msg = str(exc_info.value)
            assert "secret" not in error_msg.lower()
            assert "password" not in error_msg.lower()

    def test_env_override_precedence(self):
        """Test environment variable takes precedence over YAML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_data = {"logging": {"level": "DEBUG"}}
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            os.environ["TMS_LOG_LEVEL"] = "WARNING"
            try:
                manager = ConfigurationManager(config_path=config_path)
                assert manager.get_config().logging.level == "WARNING"
            finally:
                del os.environ["TMS_LOG_LEVEL"]

    def test_yaml_precedence_over_defaults(self):
        """Test YAML takes precedence over built-in defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_data = {"cameras": {"acquisition": {"target_fps": 25}}}
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            manager = ConfigurationManager(config_path=config_path)
            assert manager.get_config().cameras.acquisition.target_fps == 25

    def test_defaults_used_when_yaml_absent(self):
        """Test built-in defaults used when YAML field is absent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_data = {"application": {"name": "Custom"}}
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            manager = ConfigurationManager(config_path=config_path)
            config = manager.get_config()
            assert config.application.name == "Custom"
            assert config.application.default_mode == "CONFIGURATION"  # default

    def test_multiple_camera_mappings_load(self):
        """Test multiple camera mappings load correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_data = {
                "cameras": {
                    "mapping": [
                        {"camera_id": "cam_01", "serial_number": "SN001", "enabled": True, "name": "Cam 1"},
                        {"camera_id": "cam_02", "serial_number": "SN002", "enabled": False, "name": "Cam 2"},
                    ]
                }
            }
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            manager = ConfigurationManager(config_path=config_path)
            mapping = manager.get_config().cameras.mapping

            assert len(mapping) == 2
            assert mapping[0].camera_id == "cam_01"
            assert mapping[0].serial_number == "SN001"
            assert mapping[0].enabled is True
            assert mapping[1].camera_id == "cam_02"
            assert mapping[1].enabled is False

    def test_empty_camera_mapping_valid(self):
        """Test empty camera mapping is valid."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_data = {"cameras": {"mapping": []}}
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            manager = ConfigurationManager(config_path=config_path)
            mapping = manager.get_config().cameras.mapping
            assert mapping == []

    def test_ptz_range_validation(self):
        """Test PTZ min/max validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_data = {"ptz": {"limits": {"min_pan": 10.0, "max_pan": 5.0}}}
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            with pytest.raises(ConfigurationError, match="min_pan.*must be < max_pan"):
                ConfigurationManager(config_path=config_path)

    def test_default_config_no_customer_values(self):
        """Test default config contains no customer-specific values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            manager = ConfigurationManager(config_path=config_path, create_default=True)
            config = manager.get_config()

            # Check no real IPs
            assert config.database.host == ""
            assert config.database.name == ""

            # Check no real camera serials
            for mapping in config.cameras.mapping:
                assert "HB25" not in mapping.serial_number

            # Check no real storage paths
            assert config.storage.root == "data"

            # Check no passwords in config
            assert config.database.password_env == "TMS_DB_PASSWORD"

    def test_factory_function(self):
        """Test create_config_manager factory function."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            with open(config_path, "w") as f:
                yaml.dump({"application": {"name": "Factory Test"}}, f)

            manager = create_config_manager(config_path=config_path)
            assert manager.get_config().application.name == "Factory Test"

    def test_dev_mode_detection(self):
        """Test development mode detection."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_path.write_text("application:\n  name: Dev Test\n")

            manager = ConfigurationManager(config_path=config_path)
            assert manager.is_dev is True

    def test_config_path_property(self):
        """Test config_path property returns correct path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_path.write_text("application:\n  name: Path Test\n")

            manager = ConfigurationManager(config_path=config_path)
            assert manager.config_path == config_path.resolve()

    def test_app_root_property(self):
        """Test app_root property returns correct path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_path.write_text("application:\n  name: Root Test\n")

            manager = ConfigurationManager(config_path=config_path, app_root=tmpdir)
            assert manager.app_root == Path(tmpdir).resolve()

    def test_resolve_calibration_path(self):
        """Test calibration path resolution."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_data = {
                "storage": {"root": "mydata"},
                "calibration": {"default_file": "calibration/custom_blob.txt"},
            }
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            manager = ConfigurationManager(config_path=config_path, app_root=tmpdir)
            cal_path = manager.resolve_calibration_path()

            assert cal_path == Path(tmpdir) / "mydata" / "calibration" / "custom_blob.txt"

    def test_resolve_log_path(self):
        """Test log path resolution."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_data = {
                "storage": {"root": "mydata", "logs": "logs"},
                "logging": {"file_path": "logs/app.log"},
            }
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            manager = ConfigurationManager(config_path=config_path, app_root=tmpdir)
            log_path = manager.resolve_log_path()

            assert log_path == Path(tmpdir) / "mydata" / "logs" / "app.log"

    def test_resolve_log_path_none_when_empty(self):
        """Test log path returns None when not configured."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_data = {"logging": {"file_path": ""}}
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            manager = ConfigurationManager(config_path=config_path)
            log_path = manager.resolve_log_path()
            assert log_path is None

    def test_database_password_env_custom(self):
        """Test custom password environment variable name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_data = {"database": {"password_env": "MY_CUSTOM_DB_PASSWORD"}}
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            os.environ["MY_CUSTOM_DB_PASSWORD"] = "customsecret"
            try:
                manager = ConfigurationManager(config_path=config_path)
                password = manager.get_database_password()
                assert password == "customsecret"
            finally:
                del os.environ["MY_CUSTOM_DB_PASSWORD"]


class TestConfigurationModels:
    """Test configuration model validation directly."""

    def test_database_config_validation(self):
        """Test DatabaseConfig validation."""
        # Valid
        DatabaseConfig(port=1433)
        DatabaseConfig(port=1)
        DatabaseConfig(port=65535)

        # Invalid
        with pytest.raises(ValueError, match="between 1 and 65535"):
            DatabaseConfig(port=0)
        with pytest.raises(ValueError, match="between 1 and 65535"):
            DatabaseConfig(port=65536)

    def test_camera_acquisition_validation(self):
        """Test CameraAcquisitionConfig validation."""
        CameraAcquisitionConfig(target_fps=1)
        with pytest.raises(ValueError, match="target_fps must be > 0"):
            CameraAcquisitionConfig(target_fps=0)
        with pytest.raises(ValueError, match="target_fps must be > 0"):
            CameraAcquisitionConfig(target_fps=-1)

    def test_logging_config_validation(self):
        """Test LoggingConfig validation."""
        LoggingConfig(level="DEBUG")
        LoggingConfig(level="INFO")
        LoggingConfig(level="WARNING")
        LoggingConfig(level="ERROR")
        LoggingConfig(level="CRITICAL")
        with pytest.raises(ValueError, match="logging.level must be one of"):
            LoggingConfig(level="INVALID")

    def test_ui_config_validation(self):
        """Test UIConfig validation."""
        UIConfig(theme="light")
        UIConfig(theme="dark")
        UIConfig(theme="system")
        with pytest.raises(ValueError, match="ui.theme must be"):
            UIConfig(theme="invalid")

    def test_ptz_limits_validation(self):
        """Test PTZLimitsConfig validation."""
        PTZLimitsConfig(min_pan=-10, max_pan=10)
        with pytest.raises(ValueError, match="min_pan.*must be < max_pan"):
            PTZLimitsConfig(min_pan=10, max_pan=5)
        with pytest.raises(ValueError, match="min_tilt.*must be < max_tilt"):
            PTZLimitsConfig(min_tilt=10, max_tilt=5)
        with pytest.raises(ValueError, match="min_zoom.*must be < max_zoom"):
            PTZLimitsConfig(min_zoom=10, max_zoom=5)

    def test_storage_config_resolution(self):
        """Test StorageConfig path resolution."""
        storage = StorageConfig(root="relative/path", recordings="recordings")
        app_root = Path("C:/app")  # Windows-compatible absolute path

        root = storage.resolve_root(app_root)
        assert root == Path("C:/app/relative/path")

        rec = storage.resolve_subpath(app_root, "recordings")
        assert rec == Path("C:/app/relative/path/recordings")

        # Absolute subpath preserved
        abs_path = storage.resolve_subpath(app_root, "D:/absolute/path")
        assert abs_path == Path("D:/absolute/path")

    def test_offline_playback_validation(self):
        """Test OfflinePlaybackConfig validation."""
        OfflinePlaybackConfig(speed_min=0.1, speed_max=10.0)
        with pytest.raises(ValueError, match="speed_min must be > 0"):
            OfflinePlaybackConfig(speed_min=0, speed_max=10.0)
        with pytest.raises(ValueError, match="speed_max.*must be > speed_min"):
            OfflinePlaybackConfig(speed_min=5.0, speed_max=5.0)


class TestPathResolution:
    """Test path resolution in various deployment scenarios."""

    def test_dev_mode_path_resolution(self):
        """Test path resolution in development mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            project_root.mkdir()
            config_dir = project_root / "config"
            config_dir.mkdir()
            config_path = config_dir / "config.yaml"
            config_data = {"storage": {"root": "data"}}
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            manager = ConfigurationManager(config_path=config_path, app_root=project_root)
            storage_root = manager.resolve_storage_root()

            assert storage_root == project_root / "data"
            assert manager.is_dev is True

    def test_absolute_storage_root(self):
        """Test absolute storage root is used as-is."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "config.yaml"
            config_path.parent.mkdir()
            # Use a Windows-compatible absolute path
            absolute_root = str(Path(tmpdir).drive + "/mnt/storage")
            config_data = {"storage": {"root": absolute_root}}
            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            manager = ConfigurationManager(config_path=config_path)
            storage_root = manager.resolve_storage_root()

            assert storage_root == Path(absolute_root)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])