"""
ui.theme.themes.industrial_dark -- Default industrial dark theme.

Dark neutral surfaces, restrained amber/green/blue emphasis, clear status
colors.  Designed for factory-floor thermal monitoring: high readability,
compact controls, no gradients, no shadows, minimal corner radius.
"""

from __future__ import annotations

from thermal_monitor.ui.theme.tokens import DEFAULT_METRICS, ThemeDefinition


INDUSTRIAL_DARK = ThemeDefinition(
    name="industrial_dark",
    display_name="Industrial Dark",
    description=(
        "Default factory-floor theme: dark neutral panels, restrained "
        "status colors, compact industrial controls."
    ),
    is_dark=True,
    # surfaces & text
    background="#15181D",
    surface="#1E232A",
    surface_alt="#262D36",
    border="#343C47",
    border_strong="#4A5462",
    text="#E8EAED",
    text_secondary="#A7B0BC",
    muted_text="#7C8592",
    title="#F0F2F5",
    # primary (live/go) -- industrial green
    primary="#43A047",
    primary_hover="#4CAF50",
    primary_pressed="#2E7D32",
    primary_disabled_bg="#263026",
    primary_disabled_text="#5F6F62",
    # secondary (configuration) -- steel blue
    secondary="#1E88E5",
    secondary_hover="#42A5F5",
    secondary_pressed="#1565C0",
    secondary_disabled_bg="#232E38",
    secondary_disabled_text="#5E7386",
    # accent (offline/special actions) -- safety amber
    accent="#D98E2B",
    accent_hover="#EAA63E",
    accent_pressed="#A96A1B",
    # semantic states
    success="#4CAF50",
    success_bg="#1D3323",
    warning="#E8A33D",
    warning_bg="#3A2C15",
    danger="#DF5C5C",
    danger_bg="#3E2323",
    info="#5AA9E6",
    info_bg="#1D2F3D",
    disabled="#5A636E",
    alarm="#E05B5B",
    focus_ring="#5AA9E6",
    # domain states
    camera_connected="#4CAF50",
    camera_disconnected="#6B7480",
    camera_warning="#E8A33D",
    camera_error="#DF5C5C",
    thermal_hot="#FF6E40",
    thermal_cold="#40C4FF",
    overlay_roi="#00FF7F",
    metrics=DEFAULT_METRICS,
)


__all__ = ["INDUSTRIAL_DARK"]
