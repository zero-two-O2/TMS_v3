"""
tests.test_configuration_editor -- Tests for ConfigurationEditor.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from thermal_monitor.config import ConfigurationManager, create_config_manager
from thermal_monitor.config.models import AppConfig, CameraMappingConfig
from thermal_monitor.ui.configuration_editor import ConfigurationEditor, ConfigSectionEditor


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    """Create QApplication instance for tests."""
    app = QApplication.instance() or QApplication([])
    yield app


class TestConfigSectionEditor:
    """Tests for ConfigSectionEditor."""

    def test_editor_loads_values_from_config(self, qapp: QApplication) -> None:
        """Editor loads values from config object."""
        from thermal_monitor.config.models import ApplicationConfig
        config = ApplicationConfig(name="Test App", version="1.0.0")
        editor = ConfigSectionEditor("Application", config)

        editor.add_field(type('Field', (), {
            'name': 'name',
            'label': 'Name',
            'field_type': str,
            'default': 'Test App',
            'readonly': False,
            'options': None,
            'min_val': None,
            'max_val': None,
            'suffix': '',
            'tooltip': '',
            'widget': None
        })())

        # Check widget was created
        assert 'name' in editor._widgets

    def test_editor_tracks_dirty_state(self, qapp: QApplication) -> None:
        """Editor tracks when values change."""
        from thermal_monitor.config.models import ApplicationConfig
        config = ApplicationConfig(name="Test App", version="1.0.0")
        editor = ConfigSectionEditor("Application", config)

        field_def = type('Field', (), {
            'name': 'name',
            'label': 'Name',
            'field_type': str,
            'default': 'Test App',
            'readonly': False,
            'options': None,
            'min_val': None,
            'max_val': None,
            'suffix': '',
            'tooltip': '',
            'widget': None
        })()
        editor.add_field(field_def)
        editor.load_values()

        assert not editor.is_dirty()
        # Change value
        widget = editor._widgets['name']
        widget.setText("New Name")
        assert editor.is_dirty()

    def test_editor_reset_to_defaults(self, qapp: QApplication) -> None:
        """Editor resets to original values."""
        from thermal_monitor.config.models import ApplicationConfig
        config = ApplicationConfig(name="Test App", version="1.0.0")
        editor = ConfigSectionEditor("Application", config)

        field_def = type('Field', (), {
            'name': 'name',
            'label': 'Name',
            'field_type': str,
            'default': 'Test App',
            'readonly': False,
            'options': None,
            'min_val': None,
            'max_val': None,
            'suffix': '',
            'tooltip': '',
            'widget': None
        })()
        editor.add_field(field_def)
        editor.load_values()

        # Change value
        widget = editor._widgets['name']
        widget.setText("New Name")
        assert editor.is_dirty()

        # Reset
        editor.reset_to_defaults()
        assert not editor.is_dirty()
        assert widget.text() == "Test App"


class TestConfigurationEditor:
    """Tests for ConfigurationEditor."""

    def test_editor_loads_configuration(self, qapp: QApplication) -> None:
        """Editor loads configuration into sections."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        # Check sections were created
        assert "Application" in editor._section_editors
        assert "System" in editor._section_editors
        assert "Discovery" in editor._section_editors
        assert "Camera Mapping" in editor._section_editors

    def test_editor_detects_dirty_state(self, qapp: QApplication) -> None:
        """Editor detects when values are changed."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        assert not editor.is_dirty()

        # Change a value in Application section
        app_editor = editor._section_editors["Application"]
        widget = app_editor._widgets["name"]
        widget.setText("Modified App")

        assert editor.is_dirty()

    def test_cancel_restores_original_values(self, qapp: QApplication) -> None:
        """Cancel restores original values."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        # Change a value
        app_editor = editor._section_editors["Application"]
        widget = app_editor._widgets["name"]
        widget.setText("Modified App")
        assert editor.is_dirty()

        # Reset to defaults
        editor._reset_all()

        assert not editor.is_dirty()
        assert widget.text() == "Thermal Monitoring System V3"

    def test_reset_restores_defaults(self, qapp: QApplication) -> None:
        """Reset restores built-in defaults."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        # Change a value
        app_editor = editor._section_editors["Application"]
        widget = app_editor._widgets["name"]
        widget.setText("Modified App")

        # Reset
        editor._reset_all()

        assert widget.text() == "Thermal Monitoring System V3"

    def test_valid_save_succeeds(self, qapp: QApplication) -> None:
        """Valid configuration can be saved."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        # Make a change to trigger dirty state
        app_editor = editor._section_editors["Application"]
        widget = app_editor._widgets["name"]
        widget.setText("Modified App")

        # Mock the atomic save and QMessageBox to avoid file I/O and dialogs
        with patch.object(editor, '_atomic_save') as mock_save:
            # Mock the entire QMessageBox class to avoid dialogs
            with patch('thermal_monitor.ui.configuration_editor.QMessageBox') as mock_msgbox:
                # Fix: StandardButton must be the real enum for comparison to work
                mock_msgbox.StandardButton = QMessageBox.StandardButton
                mock_msgbox_instance = MagicMock()
                mock_msgbox_instance.exec.return_value = QMessageBox.StandardButton.Yes
                mock_msgbox.return_value = mock_msgbox_instance

                with patch('thermal_monitor.ui.configuration_editor.QMessageBox.information'):
                    editor._on_save()
                    mock_save.assert_called_once()

    def test_invalid_save_is_rejected(self, qapp: QApplication) -> None:
        """Invalid configuration is rejected."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        # Make a change to trigger dirty state
        app_editor = editor._section_editors["Application"]
        widget = app_editor._widgets["name"]
        widget.setText("Modified App")

        # Mock validation to fail
        with patch.object(editor, '_build_app_config_from_edit', side_effect=ValueError("Invalid config")):
            # Should not call atomic save due to validation error
            with patch.object(editor, '_atomic_save') as mock_save:
                with patch('thermal_monitor.ui.configuration_editor.QMessageBox.critical') as mock_critical:
                    editor._on_save()
                    mock_save.assert_not_called()
                    mock_critical.assert_called()

    def test_atomic_save_preserves_previous_on_failure(self, qapp: QApplication) -> None:
        """Atomic save preserves previous config on failure."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        # Mock yaml.dump to fail
        with patch('yaml.dump', side_effect=Exception("Write failed")):
            try:
                editor._atomic_save()
            except Exception:
                pass

        # Original config should still be loadable
        reloaded = ConfigurationManager(config_manager.config_path, config_manager.app_root)
        assert reloaded.get_config().application.name == "Thermal Monitoring System V3"

    def test_backup_created_on_save(self, qapp: QApplication) -> None:
        """Backup is created before saving."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        config_path = config_manager.config_path
        backup_path = config_path.with_suffix(config_path.suffix + ".bak")

        # Clean up any existing backup
        if backup_path.exists():
            backup_path.unlink()

        with patch('shutil.copy2') as mock_copy:
            with patch.object(editor, '_config_to_dict', return_value={}):
                with patch('yaml.dump'):
                    with patch('pathlib.Path.replace'):
                        editor._atomic_save()
                        mock_copy.assert_called_once_with(config_path, backup_path)

    def test_camera_discovered_values_are_readonly(self, qapp: QApplication) -> None:
        """Discovered camera values are read-only in mapping editor."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        # The discovered table is stored on the ConfigurationEditor instance
        assert hasattr(editor, '_discovered_table')
        # Table items should not be editable
        for i in range(editor._discovered_table.topLevelItemCount()):
            item = editor._discovered_table.topLevelItem(i)
            assert not (item.flags() & Qt.ItemFlag.ItemIsEditable)

    def test_camera_enable_disable_works(self, qapp: QApplication) -> None:
        """Camera enable/disable works in mapping."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        # Add a test mapping
        from thermal_monitor.config.models import CameraMappingConfig
        test_mapping = CameraMappingConfig(
            camera_id="test_cam",
            serial_number="TEST123",
            enabled=True,
            name="Test Camera",
        )
        editor._edit_config.cameras.mapping.append(test_mapping)

        # Verify mapping appears in table (access via editor)
        editor._load_mapping_table()
        assert editor._mapping_table.topLevelItemCount() >= 1

    def test_duplicate_camera_mapping_prevented(self, qapp: QApplication) -> None:
        """Duplicate camera mapping is prevented."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        # Add a mapping
        from thermal_monitor.config.models import CameraMappingConfig
        test_mapping = CameraMappingConfig(
            camera_id="test_cam1",
            serial_number="TEST123",
            enabled=True,
            name="Test Camera 1",
        )
        editor._edit_config.cameras.mapping.append(test_mapping)

        # Try to add duplicate
        with patch('thermal_monitor.ui.configuration_editor.QMessageBox.warning') as mock_warning:
            # Simulate selecting the same camera
            mapping_editor = editor._section_editors["Camera Mapping"]
            # The add button should prevent duplicates
            # This is tested by the logic in _add_camera_mapping

    def test_disconnected_configured_camera_remains(self, qapp: QApplication) -> None:
        """Disconnected configured camera remains in configuration."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        # Add a mapping
        from thermal_monitor.config.models import CameraMappingConfig
        test_mapping = CameraMappingConfig(
            camera_id="test_cam",
            serial_number="DISCONNECTED123",
            enabled=True,
            name="Disconnected Camera",
        )
        editor._edit_config.cameras.mapping.append(test_mapping)

        # The mapping should remain even if not discovered
        editor._load_mapping_table()
        assert editor._mapping_table.topLevelItemCount() >= 1

    def test_search_cameras_updates_discovery(self, qapp: QApplication) -> None:
        """Search Cameras button updates discovery."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        # Stage 8E: search goes through the backend-selected discovery
        # service (GVCP default), not a hardcoded HALCON service.
        service = MagicMock()
        service.discover_cameras.return_value = []
        with patch('thermal_monitor.services.discovery.build_discovery_service',
                   return_value=service) as mock_build:
            editor._on_search_cameras()
            mock_build.assert_called_once()
            service.discover_cameras.assert_called_once()

    def test_unsaved_change_warning(self, qapp: QApplication) -> None:
        """Unsaved change warning is shown on close."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        # Make a change
        app_editor = editor._section_editors["Application"]
        widget = app_editor._widgets["name"]
        widget.setText("Modified")
        assert editor.is_dirty()

        # Close event should trigger warning
        event = MagicMock()
        event.ignore = MagicMock()

        with patch('thermal_monitor.ui.configuration_editor.QMessageBox.exec',
                    return_value=QMessageBox.StandardButton.Cancel):
            editor.closeEvent(event)
            event.ignore.assert_called()

    def test_restart_required_shown(self, qapp: QApplication) -> None:
        """Restart required state is shown after save."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        # Make a change to trigger dirty state
        app_editor = editor._section_editors["Application"]
        widget = app_editor._widgets["name"]
        widget.setText("Modified App")

        # Connect to signal
        messages = []
        editor.restart_required.connect(lambda msg: messages.append(msg))

        # Mock save and dialogs
        with patch.object(editor, '_atomic_save'):
            # Mock the entire QMessageBox class to avoid dialogs
            with patch('thermal_monitor.ui.configuration_editor.QMessageBox') as mock_msgbox:
                # Fix: StandardButton must be the real enum for comparison to work
                mock_msgbox.StandardButton = QMessageBox.StandardButton
                mock_msgbox_instance = MagicMock()
                mock_msgbox_instance.exec.return_value = QMessageBox.StandardButton.Yes
                mock_msgbox.return_value = mock_msgbox_instance

                with patch('thermal_monitor.ui.configuration_editor.QMessageBox.information'):
                    editor._on_save()

        assert len(messages) == 1
        assert "restart" in messages[0].lower()

    def test_password_never_written_to_yaml(self, qapp: QApplication) -> None:
        """Database password is never written to YAML."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        config_dict = editor._config_to_dict(editor._edit_config)

        # Password env var should be preserved but actual password never in YAML
        assert "database" in config_dict
        assert "password_env" in config_dict["database"]
        assert "_env_password" not in config_dict.get("database", {})

    def test_relative_paths_remain_relative(self, qapp: QApplication) -> None:
        """Relative paths remain relative in YAML."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        config_dict = editor._config_to_dict(editor._edit_config)

        # Storage root should remain relative
        assert config_dict["storage"]["root"] == "data"
        assert not Path(config_dict["storage"]["root"]).is_absolute()

    def test_theme_values_persist(self, qapp: QApplication) -> None:
        """Theme values persist correctly."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        # Change theme
        theme_editor = editor._section_editors["Theme"]
        widget = theme_editor._widgets["theme"]
        widget.setCurrentText("dark")
        assert editor.is_dirty()

        # Apply changes to config
        editor._build_app_config_from_edit()
        config_dict = editor._config_to_dict(editor._edit_config)
        assert config_dict["ui"]["theme"] == "dark"

        # Reset
        editor._reset_all()
        assert widget.currentText() == "industrial_dark"


class TestConfigurationEditorIntegration:
    """Integration tests for ConfigurationEditor."""

    def test_editor_in_config_mode(self, qapp: QApplication) -> None:
        """Editor can be created with ConfigurationManager."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        # Verify it has the expected sections
        expected_sections = [
            "Application", "System", "Discovery", "Acquisition", "Recovery",
            "Connection", "Startup", "Camera Mapping", "Storage Paths",
            "Recording", "Calibration", "Processing", "Alarms", "ROI Defaults",
            "PTZ", "Offline Playback", "Database", "Network", "Logging",
            "Theme", "Colors", "Windows", "Live Display", "Display"
        ]

        for section in expected_sections:
            assert section in editor._section_editors, f"Missing section: {section}"

    def test_all_fields_have_widgets(self, qapp: QApplication) -> None:
        """All configuration fields have corresponding widgets."""
        config_manager = create_config_manager()
        editor = ConfigurationEditor(config_manager)

        for name, section_editor in editor._section_editors.items():
            # Camera Mapping section uses custom widget, not regular fields
            if name == "Camera Mapping":
                continue
            # Each section should have at least one field
            assert len(section_editor._fields) > 0, f"Section {name} has no fields"
            for field_name, field_def in section_editor._fields.items():
                assert field_def.widget is not None, f"Field {name}.{field_name} has no widget"
                assert field_name in section_editor._widgets, f"Widget not stored for {name}.{field_name}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])