"""
ui.theme -- ThemeManager for centralized UI theming.

Receives UI configuration from ConfigurationManager and provides
theme-aware colors, stylesheets, and styling utilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QColor
from PyQt6.QtCore import Qt

from thermal_monitor.config import ConfigurationManager


@dataclass(frozen=True, slots=True)
class ThemeColors:
    """Resolved theme colors for a specific theme variant."""
    primary: str
    primary_hover: str
    primary_pressed: str
    primary_disabled_bg: str
    primary_disabled_text: str
    secondary: str
    secondary_hover: str
    secondary_pressed: str
    secondary_disabled_bg: str
    secondary_disabled_text: str
    accent: str
    accent_hover: str
    accent_pressed: str
    warning: str
    danger: str
    disabled: str
    text_primary: str
    text_secondary: str
    text_muted: str
    title: str
    background: str
    panel: str
    border: str
    alarm: str
    success: str
    info: str


@dataclass(frozen=True, slots=True)
class LiveTileColors:
    """Colors for live camera tile states."""
    starting_bg: str
    starting_text: str
    running_bg: str
    running_text: str
    error_bg: str
    error_text: str
    not_available_bg: str
    not_available_text: str
    disabled_bg: str
    disabled_text: str


class ThemeManager:
    """Centralized theme manager for the application.

    Responsibilities:
    - Resolve active theme (light/dark/system)
    - Expose configured colors
    - Generate common stylesheet fragments
    - Apply application-wide stylesheet
    - Provide status/alarm colors
    - Provide camera tile state styling
    """

    def __init__(self, config_manager: ConfigurationManager) -> None:
        self._config_manager = config_manager
        self._config = config_manager.get_config().ui
        self._colors_cache: dict[str, ThemeColors] = {}
        self._live_tile_colors: LiveTileColors | None = None
        self._resolve_theme_colors()

    def _resolve_theme_colors(self) -> None:
        """Resolve colors for all theme variants."""
        colors = self._config.colors

        # Light theme colors (from config)
        self._colors_cache["light"] = ThemeColors(
            primary=colors.primary,
            primary_hover=colors.primary_hover,
            primary_pressed=colors.primary_pressed,
            primary_disabled_bg=colors.primary_disabled_bg,
            primary_disabled_text=colors.primary_disabled_text,
            secondary=colors.secondary,
            secondary_hover=colors.secondary_hover,
            secondary_pressed=colors.secondary_pressed,
            secondary_disabled_bg=colors.secondary_disabled_bg,
            secondary_disabled_text=colors.secondary_disabled_text,
            accent=colors.accent,
            accent_hover=colors.accent_hover,
            accent_pressed=colors.accent_pressed,
            warning=colors.warning,
            danger=colors.danger,
            disabled=colors.disabled,
            text_primary=colors.text_primary,
            text_secondary=colors.text_secondary,
            text_muted=colors.text_muted,
            title=colors.title,
            background=colors.background,
            panel=colors.panel,
            border=colors.border,
            alarm=colors.alarm,
            success=colors.success,
            info=colors.info,
        )

        # Dark theme colors (inverted/adapted from light)
        self._colors_cache["dark"] = ThemeColors(
            primary=self._lighten(colors.primary, 0.3),
            primary_hover=self._lighten(colors.primary_hover, 0.3),
            primary_pressed=self._darken(colors.primary_pressed, 0.2),
            primary_disabled_bg=self._darken(colors.primary_disabled_bg, 0.4),
            primary_disabled_text=self._darken(colors.primary_disabled_text, 0.4),
            secondary=self._lighten(colors.secondary, 0.3),
            secondary_hover=self._lighten(colors.secondary_hover, 0.3),
            secondary_pressed=self._darken(colors.secondary_pressed, 0.2),
            secondary_disabled_bg=self._darken(colors.secondary_disabled_bg, 0.4),
            secondary_disabled_text=self._darken(colors.secondary_disabled_text, 0.4),
            accent=self._lighten(colors.accent, 0.3),
            accent_hover=self._lighten(colors.accent_hover, 0.3),
            accent_pressed=self._darken(colors.accent_pressed, 0.2),
            warning=self._lighten(colors.warning, 0.2),
            danger=self._lighten(colors.danger, 0.2),
            disabled=self._darken(colors.disabled, 0.2),
            text_primary=self._invert_hex(colors.text_primary),
            text_secondary=self._invert_hex(colors.text_secondary),
            text_muted=self._invert_hex(colors.text_muted),
            title=self._lighten(colors.title, 0.2),
            background=self._invert_hex(colors.background),
            panel=self._darken(colors.panel, 0.15),
            border=self._darken(colors.border, 0.2),
            alarm=self._lighten(colors.alarm, 0.2),
            success=self._lighten(colors.success, 0.2),
            info=self._lighten(colors.info, 0.2),
        )

        # System theme uses light by default (Qt handles system theme)
        self._colors_cache["system"] = self._colors_cache["light"]

        # Live tile colors
        self._live_tile_colors = LiveTileColors(
            starting_bg=self._colors_cache["light"].warning,
            starting_text="#FFFFFF",
            running_bg=self._colors_cache["light"].success,
            running_text="#FFFFFF",
            error_bg=self._colors_cache["light"].danger,
            error_text="#FFFFFF",
            not_available_bg=self._colors_cache["light"].disabled,
            not_available_text="#FFFFFF",
            disabled_bg="#9E9E9E",
            disabled_text="#FFFFFF",
        )

    @staticmethod
    def _invert_hex(hex_color: str) -> str:
        """Invert a hex color."""
        hex_color = hex_color.lstrip('#')
        r = 255 - int(hex_color[0:2], 16)
        g = 255 - int(hex_color[2:4], 16)
        b = 255 - int(hex_color[4:6], 16)
        return f"#{r:02X}{g:02X}{b:02X}"

    @staticmethod
    def _lighten(hex_color: str, factor: float) -> str:
        """Lighten a hex color by factor (0-1)."""
        hex_color = hex_color.lstrip('#')
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        r = int(r + (255 - r) * factor)
        g = int(g + (255 - g) * factor)
        b = int(b + (255 - b) * factor)
        return f"#{r:02X}{g:02X}{b:02X}"

    @staticmethod
    def _darken(hex_color: str, factor: float) -> str:
        """Darken a hex color by factor (0-1)."""
        hex_color = hex_color.lstrip('#')
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        r = int(r * (1 - factor))
        g = int(g * (1 - factor))
        b = int(b * (1 - factor))
        return f"#{r:02X}{g:02X}{b:02X}"

    def _get_active_theme(self) -> str:
        """Get the active theme name."""
        theme = self._config.theme
        if theme == "system":
            # Check system palette (simplified - Qt6 handles this)
            return "light"
        return theme

    def colors(self) -> ThemeColors:
        """Get resolved colors for the active theme."""
        return self._colors_cache[self._get_active_theme()]

    def colors_for(self, theme: str) -> ThemeColors:
        """Get resolved colors for a specific theme."""
        return self._colors_cache.get(theme, self._colors_cache["light"])

    def live_tile_colors(self) -> LiveTileColors:
        """Get colors for live camera tile states."""
        return self._live_tile_colors

    # --- Color accessor methods for backward compatibility ---

    def background(self) -> str:
        return self.colors().background

    def surface(self) -> str:
        return self.colors().panel

    def text(self) -> str:
        return self.colors().text_primary

    def text_secondary(self) -> str:
        return self.colors().text_secondary

    def text_muted(self) -> str:
        return self.colors().text_muted

    def border(self) -> str:
        return self.colors().border

    def accent(self) -> str:
        return self.colors().accent

    def success(self) -> str:
        return self.colors().success

    def warning(self) -> str:
        return self.colors().warning

    def error(self) -> str:
        return self.colors().danger

    def info(self) -> str:
        return self.colors().info

    def primary(self) -> str:
        return self.colors().primary

    def primary_hover(self) -> str:
        return self.colors().primary_hover

    def primary_pressed(self) -> str:
        return self.colors().primary_pressed

    def secondary(self) -> str:
        return self.colors().secondary

    def secondary_hover(self) -> str:
        return self.colors().secondary_hover

    def secondary_pressed(self) -> str:
        return self.colors().secondary_pressed

    def disabled_text(self) -> str:
        return self.colors().disabled

    def title(self) -> str:
        return self.colors().title

    def alarm(self) -> str:
        return self.colors().alarm

    # --- Live tile state colors ---

    def live_tile_starting(self) -> str:
        return self.warning()

    def live_tile_running(self) -> str:
        return self.success()

    def live_tile_error(self) -> str:
        return self.error()

    def live_tile_unavailable(self) -> str:
        return self.disabled_text()

    def live_tile_disabled(self) -> str:
        return "#9E9E9E"

    def live_tile_starting_bg(self) -> str:
        return self._live_tile_colors.starting_bg

    def live_tile_running_bg(self) -> str:
        return self._live_tile_colors.running_bg

    def live_tile_error_bg(self) -> str:
        return self._live_tile_colors.error_bg

    def live_tile_unavailable_bg(self) -> str:
        return self._live_tile_colors.not_available_bg

    def live_tile_disabled_bg(self) -> str:
        return self._live_tile_colors.disabled_bg

    # --- Stylesheet generation ---

    def base_stylesheet(self) -> str:
        """Generate base application stylesheet."""
        c = self.colors()
        return f"""
            QWidget {{
                color: {c.text_primary};
                background-color: {c.background};
                font-family: "Segoe UI", "Arial", sans-serif;
                font-size: 13px;
            }}
            QMainWindow {{
                background-color: {c.background};
            }}
            QGroupBox {{
                border: 1px solid {c.border};
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 12px;
                font-weight: bold;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                color: {c.text_primary};
            }}
            QLabel {{
                color: {c.text_primary};
            }}
            QPushButton {{
                background-color: {c.panel};
                color: {c.text_primary};
                border: 1px solid {c.border};
                border-radius: 4px;
                padding: 6px 12px;
                min-height: 24px;
            }}
            QPushButton:hover {{
                background-color: {c.border};
                border-color: {c.accent};
            }}
            QPushButton:pressed {{
                background-color: {c.accent};
                color: white;
            }}
            QPushButton:disabled {{
                background-color: {c.disabled};
                color: {c.text_muted};
                border-color: {c.border};
            }}
            QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
                background-color: {c.background};
                color: {c.text_primary};
                border: 1px solid {c.border};
                border-radius: 4px;
                padding: 4px 8px;
                selection-background-color: {c.primary};
            }}
            QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
                border-color: {c.primary};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 20px;
            }}
            QComboBox::down-arrow {{
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid {c.text_primary};
                margin-right: 5px;
            }}
            QTableWidget {{
                background-color: {c.background};
                alternate-background-color: {c.panel};
                border: 1px solid {c.border};
                gridline-color: {c.border};
                selection-background-color: {c.primary};
                selection-color: white;
            }}
            QHeaderView::section {{
                background-color: {c.panel};
                color: {c.text_primary};
                border: 1px solid {c.border};
                padding: 6px;
                font-weight: bold;
            }}
            QTreeWidget {{
                background-color: {c.background};
                border: 1px solid {c.border};
                selection-background-color: {c.primary};
                selection-color: white;
            }}
            QTreeWidget::item {{
                padding: 4px;
            }}
            QScrollArea {{
                border: none;
                background-color: {c.background};
            }}
            QScrollBar:vertical {{
                background-color: {c.panel};
                width: 12px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background-color: {c.border};
                border-radius: 6px;
                min-height: 30px;
            }}
            QScrollBar::handle:vertical:hover {{
                background-color: {c.text_muted};
            }}
            QScrollBar:horizontal {{
                background-color: {c.panel};
                height: 12px;
                border: none;
            }}
            QScrollBar::handle:horizontal {{
                background-color: {c.border};
                border-radius: 6px;
                min-width: 30px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background-color: {c.text_muted};
            }}
            QStatusBar {{
                background-color: {c.panel};
                color: {c.text_secondary};
                border-top: 1px solid {c.border};
            }}
            QToolBar {{
                background-color: {c.panel};
                border-bottom: 1px solid {c.border};
                spacing: 4px;
            }}
            QToolBar QToolButton {{
                background-color: transparent;
                border: 1px solid transparent;
                border-radius: 4px;
                padding: 6px 12px;
            }}
            QToolBar QToolButton:hover {{
                background-color: {c.border};
            }}
            QToolBar QToolButton:checked {{
                background-color: {c.primary};
                color: white;
            }}
            QTabWidget::pane {{
                border: 1px solid {c.border};
                background-color: {c.background};
            }}
            QTabBar::tab {{
                background-color: {c.panel};
                color: {c.text_primary};
                border: 1px solid {c.border};
                border-bottom: none;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                padding: 8px 16px;
                margin-right: 2px;
            }}
            QTabBar::tab:selected {{
                background-color: {c.background};
                border-bottom: 1px solid {c.background};
            }}
            QTabBar::tab:hover:!selected {{
                background-color: {c.border};
            }}
            QProgressBar {{
                border: 1px solid {c.border};
                border-radius: 4px;
                background-color: {c.panel};
                text-align: center;
            }}
            QProgressBar::chunk {{
                background-color: {c.primary};
                border-radius: 3px;
            }}
            QSlider::groove:horizontal {{
                border: 1px solid {c.border};
                height: 8px;
                background: {c.panel};
                border-radius: 4px;
            }}
            QSlider::handle:horizontal {{
                background: {c.primary};
                border: 1px solid {c.primary};
                width: 16px;
                margin: -5px 0;
                border-radius: 8px;
            }}
            QSlider::handle:horizontal:hover {{
                background: {c.primary_hover};
            }}
            QCheckBox {{
                spacing: 8px;
            }}
            QCheckBox::indicator {{
                width: 18px;
                height: 18px;
                border: 1px solid {c.border};
                border-radius: 3px;
                background-color: {c.background};
            }}
            QCheckBox::indicator:checked {{
                background-color: {c.primary};
                border-color: {c.primary};
                image: url(data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMTIiIGhlaWdodD0iOSIgdmlld0JveD0iMCAwIDEyIDkiIGZpbGw9Im5vbmUiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+CjxwYXRoIGQ9Ik0xIDQuNUw0LjUgOEwxMSAxIiBzdHJva2U9IndoaXRlIiBzdHJva2Utd2lkdGg9IjIiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCIvPgo8L3N2Zz4=);
            }}
            QMessageBox {{
                background-color: {c.background};
            }}
            QMessageBox QLabel {{
                color: {c.text_primary};
            }}
            QDialog {{
                background-color: {c.background};
            }}
        """

    def primary_button_stylesheet(self) -> str:
        """Stylesheet for primary action buttons."""
        c = self.colors()
        return f"""
            QPushButton {{
                font-size: 14px;
                font-weight: bold;
                background-color: {c.primary};
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
            }}
            QPushButton:hover {{
                background-color: {c.primary_hover};
            }}
            QPushButton:pressed {{
                background-color: {c.primary_pressed};
            }}
            QPushButton:disabled {{
                background-color: {c.primary_disabled_bg};
                color: {c.primary_disabled_text};
            }}
        """

    def secondary_button_stylesheet(self) -> str:
        """Stylesheet for secondary action buttons."""
        c = self.colors()
        return f"""
            QPushButton {{
                font-size: 14px;
                font-weight: bold;
                background-color: {c.secondary};
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
            }}
            QPushButton:hover {{
                background-color: {c.secondary_hover};
            }}
            QPushButton:pressed {{
                background-color: {c.secondary_pressed};
            }}
            QPushButton:disabled {{
                background-color: {c.secondary_disabled_bg};
                color: {c.secondary_disabled_text};
            }}
        """

    def accent_button_stylesheet(self) -> str:
        """Stylesheet for accent action buttons."""
        c = self.colors()
        return f"""
            QPushButton {{
                font-size: 14px;
                font-weight: bold;
                background-color: {c.accent};
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
            }}
            QPushButton:hover {{
                background-color: {c.accent_hover};
            }}
            QPushButton:pressed {{
                background-color: {c.accent_pressed};
            }}
            QPushButton:disabled {{
                background-color: {c.disabled};
                color: {c.text_muted};
            }}
        """

    def title_stylesheet(self, font_size: int = 28) -> str:
        """Stylesheet for title labels."""
        c = self.colors()
        return f"font-size: {font_size}px; font-weight: bold; color: {c.title};"

    def subtitle_stylesheet(self, font_size: int = 16) -> str:
        """Stylesheet for subtitle labels."""
        c = self.colors()
        return f"font-size: {font_size}px; color: {c.text_secondary};"

    def status_stylesheet(self, font_size: int = 12) -> str:
        """Stylesheet for status labels."""
        c = self.colors()
        return f"color: {c.text_secondary}; font-size: {font_size}px;"

    def table_header_stylesheet(self) -> str:
        """Stylesheet for table headers."""
        c = self.colors()
        return f"""
            QHeaderView::section {{
                background-color: {c.panel};
                color: {c.text_primary};
                border: 1px solid {c.border};
                padding: 6px;
                font-weight: bold;
            }}
        """

    def apply(self, app: QApplication) -> None:
        """Apply theme to the entire application."""
        app.setStyleSheet(self.base_stylesheet())

    # --- Window configuration ---

    def window_config(self) -> dict:
        """Get window configuration from UI config."""
        w = self._config.windows
        return {
            "start_maximized": w.start_maximized,
            "launcher_min_width": w.launcher_min_width,
            "launcher_min_height": w.launcher_min_height,
            "live_min_width": w.live_min_width,
            "live_min_height": w.live_min_height,
            "config_min_width": w.config_min_width,
            "config_min_height": w.config_min_height,
            "config_image_min_width": w.config_image_min_width,
            "config_image_min_height": w.config_image_min_height,
            "offline_min_width": w.offline_min_width,
            "offline_min_height": w.offline_min_height,
        }

    # --- Live mode configuration ---

    def live_config(self) -> dict:
        """Get live mode configuration."""
        l = self._config.live
        return {
            "tile_gap": l.tile_gap,
            "columns": l.columns,
            "rows": l.rows,
            "show_camera_name": l.show_camera_name,
            "show_serial": l.show_serial,
            "show_fps": l.show_fps,
            "show_temperature": l.show_temperature,
        }

    # --- Display configuration ---

    def display_config(self) -> dict:
        """Get display configuration."""
        d = self._config.display
        return {
            "default_palette": d.default_palette,
            "default_zoom": d.default_zoom,
            "auto_range": d.auto_range,
            "min_temperature": d.min_temperature,
            "max_temperature": d.max_temperature,
        }

    # --- Offline playback configuration ---

    def offline_playback_config(self) -> dict:
        """Get offline playback configuration."""
        p = self._config_manager.get_config().offline.playback
        return {
            "default_speed": p.default_speed,
            "speed_min": p.speed_min,
            "speed_max": p.speed_max,
        }

    def refresh(self) -> None:
        """Refresh theme from configuration (call after config changes)."""
        self._config = self._config_manager.get_config().ui
        self._resolve_theme_colors()


__all__ = ["ThemeManager", "ThemeColors", "LiveTileColors"]