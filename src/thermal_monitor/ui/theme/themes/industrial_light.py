"""
ui.theme.themes.industrial_light -- Industrial light theme.

ThermoView-inspired instrumentation look: light neutral-grey surfaces,
thin grey borders, muted blue-grey headers (surface_alt), and a
restrained steel-blue accent instead of the historic purple. Functional
colors (primary/secondary greens/blues, semantic, alarm, and thermal
palette colors) are intentionally unchanged.
"""

from __future__ import annotations

from thermal_monitor.ui.theme.tokens import LIGHT_METRICS, ThemeDefinition


INDUSTRIAL_LIGHT = ThemeDefinition(
    name="industrial_light",
    display_name="Industrial Light",
    description=(
        "Light engineering-workstation theme: neutral grey surfaces, "
        "blue-grey headers, compact industrial density."
    ),
    is_dark=False,
    # surfaces & text (neutral industrial greys, not pure white)
    background="#E9ECEF",
    surface="#F4F6F8",
    surface_alt="#D9E0E7",
    border="#C6CDD4",
    border_strong="#9AA5B1",
    text="#212121",
    text_secondary="#5C686F",
    muted_text="#666666",
    title="#455A64",
    # primary (historic green, unchanged)
    primary="#2E7D32",
    primary_hover="#388E3C",
    primary_pressed="#1B5E20",
    primary_disabled_bg="#A5D6A7",
    primary_disabled_text="#E8F5E9",
    # secondary (historic blue, unchanged)
    secondary="#1976D2",
    secondary_hover="#1E88E5",
    secondary_pressed="#0D47A1",
    secondary_disabled_bg="#90CAF9",
    secondary_disabled_text="#E3F2FD",
    # accent (muted steel blue, replaces historic purple)
    accent="#546E7A",
    accent_hover="#607D8B",
    accent_pressed="#37474F",
    # semantic states (unchanged)
    success="#2E7D32",
    success_bg="#E8F5E9",
    warning="#FFA000",
    warning_bg="#FFF8E1",
    danger="#D32F2F",
    danger_bg="#FFCDD2",
    info="#1976D2",
    info_bg="#E3F2FD",
    disabled="#757575",
    alarm="#D32F2F",
    focus_ring="#1976D2",
    # domain states (unchanged, incl. thermal palette colors)
    camera_connected="#2E7D32",
    camera_disconnected="#757575",
    camera_warning="#FFA000",
    camera_error="#D32F2F",
    thermal_hot="#FF5722",
    thermal_cold="#2196F3",
    overlay_roi="#00FF7F",
    metrics=LIGHT_METRICS,
)


__all__ = ["INDUSTRIAL_LIGHT"]
