"""ui.theme -- Centralized theme system for the TMS V3 GUI.

Widgets declare semantic styling via dynamic properties (``variant`` /
``status`` / ``role`` / ``tileState`` / ``dirty`` -- see
:mod:`thermal_monitor.ui.theme.properties`); all colors and metrics live
in :mod:`thermal_monitor.ui.theme.tokens` theme definitions and are
rendered into a single application stylesheet by
:mod:`thermal_monitor.ui.theme.stylesheet`.
"""

from __future__ import annotations

from thermal_monitor.ui.theme.manager import (
    LEGACY_THEMES,
    LiveTileColors,
    ThemeColors,
    ThemeManager,
)
from thermal_monitor.ui.theme.properties import (
    refresh_all_widgets,
    repolish,
    set_dirty,
    set_role,
    set_status,
    set_tile_state,
    set_variant,
)
from thermal_monitor.ui.theme.stylesheet import build_stylesheet
from thermal_monitor.ui.theme.themes import (
    BUILTIN_THEMES,
    DEFAULT_THEME_NAME,
    get_builtin_theme,
)
from thermal_monitor.ui.theme.menu import (
    SETTINGS_MENU_TITLE,
    THEME_MENU_ORDER,
    THEME_MENU_TITLE,
    ThemeMenuController,
    available_menu_themes,
    theme_display_name,
)
from thermal_monitor.ui.theme.tokens import (
    BUTTON_VARIANTS,
    LABEL_ROLES,
    STATUS_VALUES,
    TILE_STATES,
    ThemeDefinition,
    ThemeMetrics,
)


def apply_theme(name: str, app=None) -> ThemeManager:
    """Apply the named theme (module-level convenience entry point)."""
    return ThemeManager.apply_theme(name, app=app)


__all__ = [
    "BUTTON_VARIANTS",
    "BUILTIN_THEMES",
    "DEFAULT_THEME_NAME",
    "LABEL_ROLES",
    "LEGACY_THEMES",
    "SETTINGS_MENU_TITLE",
    "STATUS_VALUES",
    "THEME_MENU_ORDER",
    "THEME_MENU_TITLE",
    "TILE_STATES",
    "LiveTileColors",
    "ThemeColors",
    "ThemeDefinition",
    "ThemeManager",
    "ThemeMenuController",
    "ThemeMetrics",
    "apply_theme",
    "available_menu_themes",
    "build_stylesheet",
    "get_builtin_theme",
    "refresh_all_widgets",
    "repolish",
    "set_dirty",
    "set_role",
    "set_status",
    "set_tile_state",
    "set_variant",
    "theme_display_name",
]
