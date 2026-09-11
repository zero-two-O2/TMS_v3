"""
ui.theme.themes.industrial_light -- Industrial light theme.

Carries the historic TMS light palette (same values as the legacy
``light`` theme) so existing light-mode users see no change, expressed
as centralized tokens plus status-background tints.
"""

from __future__ import annotations

from thermal_monitor.ui.theme.tokens import DEFAULT_METRICS, ThemeDefinition


INDUSTRIAL_LIGHT = ThemeDefinition(
    name="industrial_light",
    display_name="Industrial Light",
    description=(
        "Light engineering-workstation theme using the historic TMS "
        "light palette."
    ),
    is_dark=False,
    # surfaces & text (historic light values)
    background="#FFFFFF",
    surface="#F5F5F5",
    surface_alt="#ECEFF1",
    border="#E0E0E0",
    border_strong="#BDBDBD",
    text="#212121",
    text_secondary="#888888",
    muted_text="#666666",
    title="#2196F3",
    # primary (historic green)
    primary="#2E7D32",
    primary_hover="#388E3C",
    primary_pressed="#1B5E20",
    primary_disabled_bg="#A5D6A7",
    primary_disabled_text="#E8F5E9",
    # secondary (historic blue)
    secondary="#1976D2",
    secondary_hover="#1E88E5",
    secondary_pressed="#0D47A1",
    secondary_disabled_bg="#90CAF9",
    secondary_disabled_text="#E3F2FD",
    # accent (historic purple)
    accent="#7B1FA2",
    accent_hover="#8E24AA",
    accent_pressed="#4A148C",
    # semantic states
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
    # domain states
    camera_connected="#2E7D32",
    camera_disconnected="#757575",
    camera_warning="#FFA000",
    camera_error="#D32F2F",
    thermal_hot="#FF5722",
    thermal_cold="#2196F3",
    overlay_roi="#00FF7F",
    metrics=DEFAULT_METRICS,
)


__all__ = ["INDUSTRIAL_LIGHT"]
