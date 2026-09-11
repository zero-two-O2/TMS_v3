"""ui.theme.themes -- Registry of built-in token-based themes."""

from __future__ import annotations

from thermal_monitor.ui.theme.themes.blue_engineering import BLUE_ENGINEERING
from thermal_monitor.ui.theme.themes.high_contrast import HIGH_CONTRAST
from thermal_monitor.ui.theme.themes.industrial_dark import INDUSTRIAL_DARK
from thermal_monitor.ui.theme.themes.industrial_light import INDUSTRIAL_LIGHT
from thermal_monitor.ui.theme.tokens import ThemeDefinition

#: Default theme applied when no configuration is present.
DEFAULT_THEME_NAME = "industrial_dark"

#: Built-in token-based themes, keyed by theme name.
BUILTIN_THEMES: dict[str, ThemeDefinition] = {
    INDUSTRIAL_DARK.name: INDUSTRIAL_DARK,
    INDUSTRIAL_LIGHT.name: INDUSTRIAL_LIGHT,
    BLUE_ENGINEERING.name: BLUE_ENGINEERING,
    HIGH_CONTRAST.name: HIGH_CONTRAST,
}


def get_builtin_theme(name: str) -> ThemeDefinition:
    """Return the built-in theme definition for *name* (KeyError if unknown)."""
    return BUILTIN_THEMES[name]


__all__ = [
    "BLUE_ENGINEERING",
    "BUILTIN_THEMES",
    "DEFAULT_THEME_NAME",
    "HIGH_CONTRAST",
    "INDUSTRIAL_DARK",
    "INDUSTRIAL_LIGHT",
    "get_builtin_theme",
]
