"""
ui.theme.themes.high_contrast -- High-contrast accessibility identity.

Black surfaces, bright text, strong borders, yellow focus ring.  Proves
the token model can express accessibility variants without widget edits.
"""

from __future__ import annotations

from thermal_monitor.ui.theme.tokens import ThemeDefinition, ThemeMetrics


_HIGH_CONTRAST_METRICS = ThemeMetrics(
    border_width=2,
    radius_sm=0,
    radius_md=0,
    radius_lg=0,
)


HIGH_CONTRAST = ThemeDefinition(
    name="high_contrast",
    display_name="High Contrast",
    description="Black high-contrast accessibility theme.",
    is_dark=True,
    background="#000000",
    surface="#0A0A0A",
    surface_alt="#1A1A1A",
    border="#8A8A8A",
    border_strong="#FFFFFF",
    text="#FFFFFF",
    text_secondary="#E8E8E8",
    muted_text="#C0C0C0",
    title="#FFFFFF",
    primary="#00E676",
    primary_hover="#33EE8C",
    primary_pressed="#00B356",
    primary_disabled_bg="#1A1A1A",
    primary_disabled_text="#6A6A6A",
    secondary="#40C4FF",
    secondary_hover="#6ED3FF",
    secondary_pressed="#0098D6",
    secondary_disabled_bg="#1A1A1A",
    secondary_disabled_text="#6A6A6A",
    accent="#FFD600",
    accent_hover="#FFE04D",
    accent_pressed="#C7A800",
    success="#00E676",
    success_bg="#062A17",
    warning="#FFD600",
    warning_bg="#333000",
    danger="#FF5252",
    danger_bg="#3D0A0A",
    info="#40C4FF",
    info_bg="#0A2A3D",
    disabled="#6A6A6A",
    alarm="#FF5252",
    focus_ring="#FFD600",
    camera_connected="#00E676",
    camera_disconnected="#9A9A9A",
    camera_warning="#FFD600",
    camera_error="#FF5252",
    thermal_hot="#FF6E40",
    thermal_cold="#40C4FF",
    overlay_roi="#00FF7F",
    text_on_filled="#000000",
    text_on_warning="#000000",
    metrics=_HIGH_CONTRAST_METRICS,
)


__all__ = ["HIGH_CONTRAST"]
