"""
tests.test_theme -- Tests for ThemeManager.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QApplication

from thermal_monitor.config import ConfigurationManager, create_config_manager
from thermal_monitor.ui.theme import ThemeManager, ThemeColors, LiveTileColors


class TestThemeManager:
    """Tests for ThemeManager."""

    def test_default_theme_is_industrial_dark(self) -> None:
        """Default theme is industrial_dark."""
        config_manager = create_config_manager()
        theme = ThemeManager(config_manager)

        assert theme.theme_name == "industrial_dark"
        colors = theme.colors()
        assert isinstance(colors, ThemeColors)
        assert colors.background == "#15181D"
        assert colors.panel == "#1E232A"
        assert colors.text_primary == "#E8EAED"

    def test_light_theme_loads(self) -> None:
        """Legacy light theme loads correctly."""
        config_manager = create_config_manager()
        theme = ThemeManager(config_manager)

        # Should have light theme colors
        colors = theme.colors_for("light")
        assert isinstance(colors, ThemeColors)
        assert colors.background == "#FFFFFF"
        assert colors.panel == "#F5F5F5"
        assert colors.primary == "#2E7D32"

    def test_dark_theme_loads(self) -> None:
        """Dark theme loads correctly."""
        config_manager = create_config_manager()
        config = config_manager.get_config()
        # Manually test dark theme by creating a modified UI config
        from dataclasses import replace
        new_ui = replace(config.ui, theme="dark")
        new_config = replace(config, ui=new_ui)
        theme = ThemeManager(config_manager)
        theme._config = new_ui
        theme._resolve_theme_colors()

        colors = theme.colors()
        assert isinstance(colors, ThemeColors)
        # Dark theme should have inverted/adjusted colors
        assert colors.background != "#FFFFFF"
        assert colors.text_primary != "#212121"

    def test_system_theme_fallback_works(self) -> None:
        """System theme falls back to light theme."""
        config_manager = create_config_manager()
        config = config_manager.get_config()
        from dataclasses import replace
        new_ui = replace(config.ui, theme="system")
        new_config = replace(config, ui=new_ui)
        theme = ThemeManager(config_manager)
        theme._config = new_ui
        theme._resolve_theme_colors()

        colors = theme.colors()
        assert isinstance(colors, ThemeColors)
        # System should use light theme colors
        assert colors.background == "#FFFFFF"

    def test_theme_can_be_applied_to_qapplication(self) -> None:
        """Theme can be applied to QApplication."""
        app = QApplication.instance() or QApplication([])
        config_manager = create_config_manager()
        theme = ThemeManager(config_manager)

        # Should not raise an exception
        theme.apply(app)

    def test_repeated_theme_application_is_safe(self) -> None:
        """Applying theme multiple times is safe."""
        app = QApplication.instance() or QApplication([])
        config_manager = create_config_manager()
        theme = ThemeManager(config_manager)

        theme.apply(app)
        theme.apply(app)
        theme.apply(app)
        # Should not raise an exception

    def test_configured_window_dimensions_are_applied(self) -> None:
        """Window configuration from config is accessible."""
        config_manager = create_config_manager()
        theme = ThemeManager(config_manager)

        window_config = theme.window_config()
        assert "launcher_min_width" in window_config
        assert "launcher_min_height" in window_config
        assert "live_min_width" in window_config
        assert "live_min_height" in window_config
        assert "config_min_width" in window_config
        assert "config_min_height" in window_config
        assert "offline_min_width" in window_config
        assert "offline_min_height" in window_config
        assert window_config["start_maximized"] is True
        assert window_config["launcher_min_width"] == 1000
        assert window_config["launcher_min_height"] == 700

    def test_start_maximized_is_respected(self) -> None:
        """start_maximized configuration is respected."""
        config_manager = create_config_manager()
        config = config_manager.get_config()
        from dataclasses import replace
        new_windows = replace(config.ui.windows, start_maximized=False)
        new_ui = replace(config.ui, windows=new_windows)
        new_config = replace(config, ui=new_ui)
        theme = ThemeManager(config_manager)
        theme._config = new_ui

        window_config = theme.window_config()
        assert window_config["start_maximized"] is False

        new_windows2 = replace(config.ui.windows, start_maximized=True)
        new_ui2 = replace(config.ui, windows=new_windows2)
        new_config2 = replace(config, ui=new_ui2)
        theme2 = ThemeManager(config_manager)
        theme2._config = new_ui2
        window_config2 = theme2.window_config()
        assert window_config2["start_maximized"] is True

    def test_live_config_is_accessible(self) -> None:
        """Live mode configuration is accessible."""
        config_manager = create_config_manager()
        theme = ThemeManager(config_manager)

        live_config = theme.live_config()
        assert "tile_gap" in live_config
        assert "columns" in live_config
        assert "rows" in live_config
        assert live_config["columns"] == 4
        assert live_config["rows"] == 2

    def test_display_config_is_accessible(self) -> None:
        """Display configuration is accessible."""
        config_manager = create_config_manager()
        theme = ThemeManager(config_manager)

        display_config = theme.display_config()
        assert "default_palette" in display_config
        assert "default_zoom" in display_config
        assert "auto_range" in display_config
        assert "min_temperature" in display_config
        assert "max_temperature" in display_config

    def test_offline_playback_config_is_accessible(self) -> None:
        """Offline playback configuration is accessible."""
        config_manager = create_config_manager()
        theme = ThemeManager(config_manager)

        pb_config = theme.offline_playback_config()
        assert "default_speed" in pb_config
        assert "speed_min" in pb_config
        assert "speed_max" in pb_config
        assert pb_config["default_speed"] == 1.0
        assert pb_config["speed_min"] == 0.1
        assert pb_config["speed_max"] == 10.0

    def test_live_tile_colors_are_resolved(self) -> None:
        """Live tile state colors are resolved from the active theme."""
        config_manager = create_config_manager()
        theme = ThemeManager(config_manager)

        tile_colors = theme.live_tile_colors()
        assert isinstance(tile_colors, LiveTileColors)
        # Active theme is industrial_dark: tile badges follow its tokens.
        assert tile_colors.running_bg == "#4CAF50"
        assert tile_colors.error_bg == "#DF5C5C"
        assert tile_colors.not_available_bg == "#5A636E"
        assert tile_colors.starting_bg == "#E8A33D"

    def test_color_accessor_methods_work(self) -> None:
        """Individual color accessor methods work."""
        config_manager = create_config_manager()
        theme = ThemeManager(config_manager)

        # Test all accessor methods (active theme: industrial_dark)
        assert theme.background() == "#15181D"
        assert theme.surface() == "#1E232A"
        assert theme.text() == "#E8EAED"
        assert theme.text_secondary() == "#A7B0BC"
        assert theme.text_muted() == "#7C8592"
        assert theme.border() == "#343C47"
        assert theme.accent() == "#D98E2B"
        assert theme.success() == "#4CAF50"
        assert theme.warning() == "#E8A33D"
        assert theme.error() == "#DF5C5C"
        assert theme.info() == "#5AA9E6"
        assert theme.primary() == "#43A047"
        assert theme.primary_hover() == "#4CAF50"
        assert theme.primary_pressed() == "#2E7D32"
        assert theme.secondary() == "#1E88E5"
        assert theme.secondary_hover() == "#42A5F5"
        assert theme.secondary_pressed() == "#1565C0"
        assert theme.disabled_text() == "#5A636E"
        assert theme.title() == "#F0F2F5"
        assert theme.alarm() == "#E05B5B"

    def test_live_tile_color_accessors_work(self) -> None:
        """Live tile color accessor methods work."""
        config_manager = create_config_manager()
        theme = ThemeManager(config_manager)

        # Test live tile color accessors - these map to text colors
        assert theme.live_tile_starting() == theme.warning()
        assert theme.live_tile_running() == theme.success()
        assert theme.live_tile_error() == theme.error()
        assert theme.live_tile_unavailable() == theme.disabled_text()
        assert theme.live_tile_disabled() == "#9E9E9E"

        # Background colors follow the active (industrial_dark) theme
        assert theme.live_tile_starting_bg() == "#E8A33D"
        assert theme.live_tile_running_bg() == "#4CAF50"
        assert theme.live_tile_error_bg() == "#DF5C5C"
        assert theme.live_tile_unavailable_bg() == "#5A636E"
        assert theme.live_tile_disabled_bg() == "#9E9E9E"

    def test_stylesheet_generation_works(self) -> None:
        """Stylesheet generation methods work."""
        config_manager = create_config_manager()
        theme = ThemeManager(config_manager)

        base = theme.base_stylesheet()
        assert "QWidget" in base
        assert "QPushButton" in base
        assert "QGroupBox" in base
        assert "QTableWidget" in base

        primary_btn = theme.primary_button_stylesheet()
        assert "background-color" in primary_btn
        assert "#43A047" in primary_btn

        secondary_btn = theme.secondary_button_stylesheet()
        assert "background-color" in secondary_btn
        assert "#1E88E5" in secondary_btn

        accent_btn = theme.accent_button_stylesheet()
        assert "background-color" in accent_btn
        assert "#D98E2B" in accent_btn

        title_style = theme.title_stylesheet(24)
        assert "font-size: 24px" in title_style
        assert "font-weight: bold" in title_style
        assert "#F0F2F5" in title_style

    def test_theme_refresh_works(self) -> None:
        """Theme refresh updates from configuration (legacy light theme)."""
        config_manager = create_config_manager()
        theme = ThemeManager(config_manager)
        theme.set_theme("light")

        original_primary = theme.primary()
        assert original_primary == "#2E7D32"

        # Change config and refresh
        config = config_manager.get_config()
        from dataclasses import replace
        new_colors = replace(config.ui.colors, primary="#FF0000")
        new_ui = replace(config.ui, colors=new_colors)
        new_config = replace(config, ui=new_ui)
        config_manager.get_config = lambda: new_config
        theme.refresh()
        assert theme.primary() == "#FF0000"

    def test_colors_for_specific_theme(self) -> None:
        """Can get colors for specific theme variant."""
        config_manager = create_config_manager()
        theme = ThemeManager(config_manager)

        light_colors = theme.colors_for("light")
        assert light_colors.background == "#FFFFFF"

        dark_colors = theme.colors_for("dark")
        assert dark_colors.background != "#FFFFFF"  # Should be inverted

        industrial = theme.colors_for("industrial_dark")
        assert industrial.background == "#15181D"

        # Unknown theme falls back to light
        unknown_colors = theme.colors_for("unknown")
        assert unknown_colors.background == "#FFFFFF"
