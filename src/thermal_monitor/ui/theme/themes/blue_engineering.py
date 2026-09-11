"""
ui.theme.themes.blue_engineering -- Alternate dark-blue visual identity.

Proves that a completely different visual identity only requires a new
token definition: no widget code changes.
"""

from __future__ import annotations

from thermal_monitor.ui.theme.tokens import DEFAULT_METRICS, ThemeDefinition


BLUE_ENGINEERING = ThemeDefinition(
    name="blue_engineering",
    display_name="Blue Engineering",
    description="Dark navy control-room identity with blue emphasis.",
    is_dark=True,
    background="#101722",
    surface="#16202E",
    surface_alt="#1C2940",
    border="#2A3A52",
    border_strong="#3E5478",
    text="#DCE7F5",
    text_secondary="#93A7C4",
    muted_text="#67788F",
    title="#FFFFFF",
    primary="#2F81F7",
    primary_hover="#4C9AFF",
    primary_pressed="#1F6FEB",
    primary_disabled_bg="#1B2A44",
    primary_disabled_text="#54678A",
    secondary="#0E9BD8",
    secondary_hover="#29B6F6",
    secondary_pressed="#0B7CB0",
    secondary_disabled_bg="#16293A",
    secondary_disabled_text="#4E6A84",
    accent="#E8A33D",
    accent_hover="#F2B45C",
    accent_pressed="#B57E22",
    success="#3FB950",
    success_bg="#14301C",
    warning="#D29922",
    warning_bg="#38300F",
    danger="#F85149",
    danger_bg="#3D1D1D",
    info="#58A6FF",
    info_bg="#16283D",
    disabled="#55606C",
    alarm="#F85149",
    focus_ring="#58A6FF",
    camera_connected="#3FB950",
    camera_disconnected="#6B7A90",
    camera_warning="#D29922",
    camera_error="#F85149",
    thermal_hot="#FF7B54",
    thermal_cold="#58A6FF",
    overlay_roi="#00FF7F",
    metrics=DEFAULT_METRICS,
)


__all__ = ["BLUE_ENGINEERING"]
