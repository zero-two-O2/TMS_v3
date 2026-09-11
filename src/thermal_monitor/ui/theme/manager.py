"""
ui.theme.manager -- Central ThemeManager for the TMS V3 GUI.

The manager owns:

- the registry of named themes (built-in token themes plus the legacy
  config-driven ``light`` / ``dark`` / ``system`` themes),
- the active theme (from ``config.yaml`` ``ui.theme`` or runtime
  override via :meth:`set_theme` / :meth:`apply_theme`),
- the single application-wide stylesheet (see
  :mod:`thermal_monitor.ui.theme.stylesheet`),
- semantic color accessors so the rare widget that needs a literal
  color (e.g. table-item foregrounds) still reads it centrally.

Theme switching is a pure GUI operation: applying a theme performs one
``QApplication.setStyleSheet()`` call plus a widget repolish.  It never
recreates camera widgets, restarts acquisition/processing/workers, or
touches SHM, recording, calibration, NUC, focus, or frame timing.

Backward compatibility: the historic API (``colors()``,
``primary()``/``success()``/... accessors, ``*_button_stylesheet()``,
``apply(app)``, ``window_config()``, ``live_config()``,
``display_config()``, ``offline_playback_config()``, ``refresh()``) is
fully preserved.  ``from thermal_monitor.ui.theme import ThemeManager``
keeps working; the old ``ui/theme.py`` module became this package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from thermal_monitor.ui.theme.stylesheet import build_stylesheet
from thermal_monitor.ui.theme.themes import BUILTIN_THEMES, DEFAULT_THEME_NAME
from thermal_monitor.ui.theme.tokens import ThemeDefinition


# ---------------------------------------------------------------------------
# Legacy data structures (kept for backward compatibility)
# ---------------------------------------------------------------------------


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

    @classmethod
    def from_definition(cls, definition: ThemeDefinition) -> ThemeColors:
        """Project a token definition onto the legacy color structure."""
        return cls(
            primary=definition.primary,
            primary_hover=definition.primary_hover,
            primary_pressed=definition.primary_pressed,
            primary_disabled_bg=definition.primary_disabled_bg,
            primary_disabled_text=definition.primary_disabled_text,
            secondary=definition.secondary,
            secondary_hover=definition.secondary_hover,
            secondary_pressed=definition.secondary_pressed,
            secondary_disabled_bg=definition.secondary_disabled_bg,
            secondary_disabled_text=definition.secondary_disabled_text,
            accent=definition.accent,
            accent_hover=definition.accent_hover,
            accent_pressed=definition.accent_pressed,
            warning=definition.warning,
            danger=definition.danger,
            disabled=definition.disabled,
            text_primary=definition.text,
            text_secondary=definition.text_secondary,
            text_muted=definition.muted_text,
            title=definition.title,
            background=definition.background,
            panel=definition.surface,
            border=definition.border,
            alarm=definition.alarm,
            success=definition.success,
            info=definition.info,
        )


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


#: Legacy config-driven themes (derived from ``config.ui.colors``).
LEGACY_THEMES = ("light", "dark", "system")


class ThemeManager:
    """Centralized theme manager for the application.

    Responsibilities:
    - Resolve active theme (registry theme or legacy light/dark/system)
    - Expose configured colors and semantic tokens
    - Generate the single application-wide stylesheet
    - Apply/switch themes at runtime (pure GUI operation)
    """

    #: All theme names this manager understands.
    KNOWN_THEMES: tuple[str, ...] = (
        "industrial_dark",
        "industrial_light",
        "blue_engineering",
        "high_contrast",
        *LEGACY_THEMES,
    )

    def __init__(self, config_manager=None) -> None:
        self._config_manager = config_manager
        self._config = (
            config_manager.get_config().ui if config_manager is not None else None
        )
        self._explicit_theme: str | None = None
        self._colors_cache: dict[str, ThemeColors] = {}
        self._live_tile_colors: LiveTileColors | None = None
        self._resolve_theme_colors()

    # ------------------------------------------------------------------
    # Theme registry / selection
    # ------------------------------------------------------------------

    @classmethod
    def available_themes(cls) -> list[str]:
        """Return all available theme names."""
        return list(cls.KNOWN_THEMES)

    @classmethod
    def builtin_themes(cls) -> dict[str, ThemeDefinition]:
        """Return the built-in token-based theme definitions."""
        return dict(BUILTIN_THEMES)

    @classmethod
    def theme_display_names(cls) -> dict[str, str]:
        """Return ``{theme_name: display_name}`` for theme pickers."""
        names: dict[str, str] = {
            name: definition.display_name
            for name, definition in BUILTIN_THEMES.items()
        }
        names.update(
            {"light": "Light (legacy)", "dark": "Dark (legacy)", "system": "System (legacy)"}
        )
        return names

    @classmethod
    def is_builtin_theme(cls, name: str) -> bool:
        """Return True for token-based registry themes."""
        return name in BUILTIN_THEMES

    @property
    def theme_name(self) -> str:
        """Return the active theme name."""
        if self._explicit_theme is not None:
            return self._explicit_theme
        return self._get_active_theme()

    def theme_definition(self) -> ThemeDefinition | None:
        """Return the token definition for the active theme.

        Returns None for the legacy config-driven themes (``light``,
        ``dark``, ``system``), whose colors come from ``config.ui.colors``.
        """
        return BUILTIN_THEMES.get(self.theme_name)

    def set_theme(self, name: str) -> str:
        """Switch the active theme (runtime override, not persisted).

        Returns the previous theme name.  Call :meth:`apply` (plus
        :func:`refresh_all_widgets` for dynamic-property widgets) to
        update a running GUI.  Raises ValueError for unknown names.
        """
        if name not in self.KNOWN_THEMES:
            raise ValueError(
                f"Unknown theme {name!r}; available: {', '.join(self.KNOWN_THEMES)}"
            )
        previous = self.theme_name
        self._explicit_theme = name
        self._resolve_theme_colors()
        return previous

    @classmethod
    def apply_theme(
        cls, name: str, app=None, config_manager=None
    ) -> ThemeManager:
        """Create a manager for *name* and apply it (convenience entry point).

        Equivalent to ``ThemeManager.apply("industrial_dark")`` style usage::

            ThemeManager.apply_theme("industrial_dark")

        Uses ``QApplication.instance()`` when *app* is omitted.  Note that
        widgets holding their own manager instance should switch via
        :meth:`set_theme` on the shared instance so their color accessors
        follow; this helper is for bootstrap, tests, and simple hosts.
        """
        from PyQt6.QtWidgets import QApplication

        manager = cls(config_manager=config_manager)
        manager.set_theme(name)
        manager.apply(app or QApplication.instance())
        return manager

    # ------------------------------------------------------------------
    # Color resolution
    # ------------------------------------------------------------------

    def _resolve_theme_colors(self) -> None:
        """Resolve colors for all theme variants."""
        # Built-in token themes project directly onto ThemeColors.
        for name, definition in BUILTIN_THEMES.items():
            self._colors_cache[name] = ThemeColors.from_definition(definition)

        if self._config is not None:
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

            light = self._colors_cache["light"]
        else:
            # No configuration: legacy names fall back to the industrial
            # light tokens so every accessor still works.
            fallback = ThemeColors.from_definition(
                BUILTIN_THEMES["industrial_light"]
            )
            self._colors_cache.setdefault("light", fallback)
            self._colors_cache.setdefault("dark", self._colors_cache["industrial_dark"])
            self._colors_cache.setdefault("system", fallback)
            light = self._colors_cache["light"]

        # Live tile colors follow the *active* theme so tile badges always
        # match the surrounding chrome.
        active = self._colors_cache.get(self.theme_name, light)
        self._live_tile_colors = LiveTileColors(
            starting_bg=active.warning,
            starting_text="#FFFFFF",
            running_bg=active.success,
            running_text="#FFFFFF",
            error_bg=active.danger,
            error_text="#FFFFFF",
            not_available_bg=active.disabled,
            not_available_text="#FFFFFF",
            disabled_bg="#9E9E9E",
            disabled_text="#FFFFFF",
        )

    @staticmethod
    def _invert_hex(hex_color: str) -> str:
        """Invert a hex color."""
        hex_color = hex_color.lstrip("#")
        r = 255 - int(hex_color[0:2], 16)
        g = 255 - int(hex_color[2:4], 16)
        b = 255 - int(hex_color[4:6], 16)
        return f"#{r:02X}{g:02X}{b:02X}"

    @staticmethod
    def _lighten(hex_color: str, factor: float) -> str:
        """Lighten a hex color by factor (0-1)."""
        hex_color = hex_color.lstrip("#")
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
        hex_color = hex_color.lstrip("#")
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        r = int(r * (1 - factor))
        g = int(g * (1 - factor))
        b = int(b * (1 - factor))
        return f"#{r:02X}{g:02X}{b:02X}"

    def _get_active_theme(self) -> str:
        """Get the active theme name."""
        if self._config is None:
            return DEFAULT_THEME_NAME
        theme = self._config.theme
        if theme == "system":
            # Check system palette (simplified - Qt6 handles this)
            return "light"
        return theme

    def colors(self) -> ThemeColors:
        """Get resolved colors for the active theme."""
        return self._colors_cache[self.theme_name]

    def colors_for(self, theme: str) -> ThemeColors:
        """Get resolved colors for a specific theme."""
        return self._colors_cache.get(theme, self._colors_cache["light"])

    def token(self, name: str, default: str = "") -> str:
        """Return a raw design token for the active built-in theme.

        Falls back to the matching legacy color (or *default*) for the
        legacy config-driven themes.
        """
        definition = self.theme_definition()
        if definition is not None:
            try:
                return definition.token(name)
            except (AttributeError, KeyError):
                return default
        legacy = {
            "background": self.colors().background,
            "surface": self.colors().panel,
            "surface_alt": self.colors().panel,
            "border": self.colors().border,
            "text": self.colors().text_primary,
            "text_secondary": self.colors().text_secondary,
            "muted_text": self.colors().text_muted,
        }
        return legacy.get(name, default)

    def live_tile_colors(self) -> LiveTileColors:
        """Get colors for live camera tile states."""
        assert self._live_tile_colors is not None
        return self._live_tile_colors

    # --- Color accessor methods for backward compatibility ---

    def background(self) -> str:
        return self.colors().background

    def surface(self) -> str:
        return self.colors().panel

    def surface_alt(self) -> str:
        definition = self.theme_definition()
        if definition is not None:
            return definition.surface_alt
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

    def success_bg(self) -> str:
        definition = self.theme_definition()
        if definition is not None:
            return definition.success_bg
        return self.colors().success

    def warning(self) -> str:
        return self.colors().warning

    def warning_bg(self) -> str:
        definition = self.theme_definition()
        if definition is not None:
            return definition.warning_bg
        return self.colors().warning

    def error(self) -> str:
        return self.colors().danger

    def danger_bg(self) -> str:
        definition = self.theme_definition()
        if definition is not None:
            return definition.danger_bg
        return self.colors().danger

    def info(self) -> str:
        return self.colors().info

    def info_bg(self) -> str:
        definition = self.theme_definition()
        if definition is not None:
            return definition.info_bg
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

    def camera_connected(self) -> str:
        definition = self.theme_definition()
        if definition is not None:
            return definition.camera_connected
        return self.colors().success

    def camera_disconnected(self) -> str:
        definition = self.theme_definition()
        if definition is not None:
            return definition.camera_disconnected
        return self.colors().disabled

    def camera_warning(self) -> str:
        definition = self.theme_definition()
        if definition is not None:
            return definition.camera_warning
        return self.colors().warning

    def camera_error(self) -> str:
        definition = self.theme_definition()
        if definition is not None:
            return definition.camera_error
        return self.colors().danger

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
        assert self._live_tile_colors is not None
        return self._live_tile_colors.starting_bg

    def live_tile_running_bg(self) -> str:
        assert self._live_tile_colors is not None
        return self._live_tile_colors.running_bg

    def live_tile_error_bg(self) -> str:
        assert self._live_tile_colors is not None
        return self._live_tile_colors.error_bg

    def live_tile_unavailable_bg(self) -> str:
        assert self._live_tile_colors is not None
        return self._live_tile_colors.not_available_bg

    def live_tile_disabled_bg(self) -> str:
        assert self._live_tile_colors is not None
        return self._live_tile_colors.disabled_bg

    # --- Stylesheet generation ---

    def base_stylesheet(self) -> str:
        """Generate the base application stylesheet for the active theme."""
        definition = self.theme_definition()
        if definition is not None:
            return build_stylesheet(definition)
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
            QPushButton[variant="primary"] {{
                font-size: 14px;
                font-weight: bold;
                background-color: {c.primary};
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
            }}
            QPushButton[variant="primary"]:hover {{
                background-color: {c.primary_hover};
            }}
            QPushButton[variant="primary"]:pressed {{
                background-color: {c.primary_pressed};
            }}
            QPushButton[variant="primary"]:disabled {{
                background-color: {c.primary_disabled_bg};
                color: {c.primary_disabled_text};
            }}
            QPushButton[variant="secondary"] {{
                font-size: 14px;
                font-weight: bold;
                background-color: {c.secondary};
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
            }}
            QPushButton[variant="secondary"]:hover {{
                background-color: {c.secondary_hover};
            }}
            QPushButton[variant="secondary"]:pressed {{
                background-color: {c.secondary_pressed};
            }}
            QPushButton[variant="secondary"]:disabled {{
                background-color: {c.secondary_disabled_bg};
                color: {c.secondary_disabled_text};
            }}
            QPushButton[variant="accent"] {{
                font-size: 14px;
                font-weight: bold;
                background-color: {c.accent};
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
            }}
            QPushButton[variant="accent"]:hover {{
                background-color: {c.accent_hover};
            }}
            QPushButton[variant="accent"]:pressed {{
                background-color: {c.accent_pressed};
            }}
            QPushButton[variant="accent"]:disabled {{
                background-color: {c.disabled};
                color: {c.text_muted};
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
        """Stylesheet for primary action buttons (kept for compatibility)."""
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
        """Stylesheet for secondary action buttons (kept for compatibility)."""
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
        """Stylesheet for accent action buttons (kept for compatibility)."""
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
        """Stylesheet for title labels (kept for compatibility)."""
        c = self.colors()
        return f"font-size: {font_size}px; font-weight: bold; color: {c.title};"

    def subtitle_stylesheet(self, font_size: int = 16) -> str:
        """Stylesheet for subtitle labels (kept for compatibility)."""
        c = self.colors()
        return f"font-size: {font_size}px; color: {c.text_secondary};"

    def status_stylesheet(self, font_size: int = 12) -> str:
        """Stylesheet for status labels (kept for compatibility)."""
        c = self.colors()
        return f"color: {c.text_secondary}; font-size: {font_size}px;"

    def table_header_stylesheet(self) -> str:
        """Stylesheet for table headers (kept for compatibility)."""
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

    def apply(self, app) -> None:
        """Apply the active theme to the entire application.

        Single stylesheet call on the QApplication: cheap, and it touches
        neither acquisition, processing, SHM, recording, nor frame timing.
        """
        if app is None:
            return
        app.setStyleSheet(self.base_stylesheet())

    def apply_and_refresh(self, app=None) -> int:
        """Apply the active theme and repolish all widgets.

        Returns the number of refreshed widgets.  Still a pure GUI-style
        operation (see :func:`refresh_all_widgets`).
        """
        from thermal_monitor.ui.theme.properties import refresh_all_widgets

        if app is None:
            from PyQt6.QtWidgets import QApplication

            app = QApplication.instance()
        self.apply(app)
        return refresh_all_widgets(app)

    # --- Window configuration ---

    def window_config(self) -> dict:
        """Get window configuration from UI config."""
        if self._config is None:
            return {
                "start_maximized": True,
                "launcher_min_width": 1000,
                "launcher_min_height": 700,
                "live_min_width": 640,
                "live_min_height": 480,
                "config_min_width": 1000,
                "config_min_height": 700,
                "config_image_min_width": 480,
                "config_image_min_height": 360,
                "offline_min_width": 640,
                "offline_min_height": 480,
            }
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
        if self._config is None:
            return {
                "tile_gap": 8,
                "columns": 2,
                "rows": 4,
                "show_camera_name": True,
                "show_serial": True,
                "show_fps": True,
                "show_temperature": True,
            }
        ll = self._config.live
        return {
            "tile_gap": ll.tile_gap,
            "columns": ll.columns,
            "rows": ll.rows,
            "show_camera_name": ll.show_camera_name,
            "show_serial": ll.show_serial,
            "show_fps": ll.show_fps,
            "show_temperature": ll.show_temperature,
        }

    # --- Display configuration ---

    def display_config(self) -> dict:
        """Get display configuration."""
        if self._config is None:
            return {
                "default_palette": "temperature",
                "default_zoom": "Fit to Window",
                "auto_range": True,
                "min_temperature": -20.0,
                "max_temperature": 1200.0,
            }
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
        if self._config_manager is None:
            return {"default_speed": 1.0, "speed_min": 0.1, "speed_max": 10.0}
        p = self._config_manager.get_config().offline.playback
        return {
            "default_speed": p.default_speed,
            "speed_min": p.speed_min,
            "speed_max": p.speed_max,
        }

    def refresh(self) -> None:
        """Refresh theme from configuration (call after config changes).

        A runtime override set via :meth:`set_theme` is preserved.
        """
        if self._config_manager is not None:
            self._config = self._config_manager.get_config().ui
        self._resolve_theme_colors()


__all__ = ["ThemeManager", "ThemeColors", "LiveTileColors", "LEGACY_THEMES"]
