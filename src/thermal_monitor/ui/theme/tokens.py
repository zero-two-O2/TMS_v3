"""
ui.theme.tokens -- Centralized design tokens for the TMS V3 GUI.

Every color, spacing, radius, and typography value used by application
*chrome* (windows, panels, buttons, tables, indicators) lives here, in a
named :class:`ThemeDefinition`.  Widgets must never hardcode literal color
or metric values; they only set semantic dynamic properties
(``variant`` / ``status`` / ``role`` / ``tileState`` / ``dirty`` -- see
:mod:`thermal_monitor.ui.theme.properties`) which the centrally generated
stylesheet resolves against the active theme.

Thermal *image data* (palettes, ROI overlay paint colors, the letterbox
fill inside image viewports) is deliberately NOT part of these tokens:
the rendering pipeline must stay behavior-identical across themes.
The single exception is :attr:`ThemeDefinition.overlay_roi`, whose default
matches the historic ROI overlay color so behavior is unchanged while the
value is still centrally visible.
"""

from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Controlled vocabularies for dynamic properties.
#
# Widgets may only use these literal values with setProperty(); the QSS
# builder styles exactly this vocabulary centrally.
# ---------------------------------------------------------------------------

#: QPushButton emphasis: filled primary/secondary/accent/danger,
#: neutral bordered ``outline``, transparent bordered ``ghost``.
BUTTON_VARIANTS = (
    "primary",
    "secondary",
    "accent",
    "danger",
    "outline",
    "ghost",
)

#: QLabel text roles: title/subtitle headers, status lines, strong
#: emphasis, muted notes, monospace values, temperature readouts,
#: and the dark image-well placeholder.
LABEL_ROLES = (
    "title",
    "subtitle",
    "status",
    "strong",
    "muted",
    "mono",
    "readout",
    "viewfinder",
)

#: Connection / lifecycle / alarm states for indicators and status labels.
#: Camera tiles reuse the same vocabulary (ready/starting/reconnecting/
#: running/error/not_available) so one selector set covers tiles, headers,
#: and labels.
STATUS_VALUES = (
    "connected",
    "connecting",
    "disconnected",
    "acquiring",
    "running",
    "live",
    "ready",
    "starting",
    "reconnecting",
    "error",
    "degraded",
    "warning",
    "not_available",
    "unavailable",
    "active",
    "inactive",
    "alarm",
    "ok",
)

#: Live-camera tile frame states (QWidget[role="tile"][tileState="..."]).
TILE_STATES = (
    "ready",
    "starting",
    "reconnecting",
    "running",
    "error",
    "not_available",
)


@dataclass(frozen=True, slots=True)
class ThemeMetrics:
    """Centralized layout/typography metrics (no hardcoded sizes in widgets).

    Field defaults preserve the historic spacious look; themes that want
    the compact industrial density (Industrial Light) override them via a
    dedicated instance — never by editing widget code.
    """

    font_family: str = '"Segoe UI", "Arial", sans-serif'
    font_mono: str = '"Consolas", "Courier New", monospace'
    font_size_xs: int = 10
    font_size_sm: int = 11
    font_size_base: int = 13
    font_size_subtitle: int = 15
    font_size_title: int = 24
    control_height: int = 28
    # Vertical padding inside buttons / inputs: button height ≈ text +
    # 2 * padding + borders (compact rectangular industrial controls).
    button_padding_v: int = 6
    input_padding_v: int = 4
    radius_sm: int = 2
    radius_md: int = 3
    radius_lg: int = 4
    border_width: int = 1
    spacing_xs: int = 4
    spacing_sm: int = 8
    spacing_md: int = 12
    spacing_lg: int = 16
    # Side-panel density: top-level layout spacing, group content spacing,
    # form-row spacing, and group content margins (h, top).
    panel_spacing: int = 8
    panel_group_spacing: int = 8
    panel_form_spacing: int = 6
    panel_group_margin: int = 8
    panel_group_margin_top: int = 12


DEFAULT_METRICS = ThemeMetrics()


def metrics_for(theme_manager) -> ThemeMetrics:
    """Panel density metrics for a (possibly absent) theme manager.

    Returns the active theme's metrics, falling back to DEFAULT_METRICS
    for legacy themes, mocks, or a missing manager. Panels read layout
    spacing/margins from here instead of hardcoding them.
    """
    try:
        metrics = theme_manager.theme_metrics()
        if isinstance(getattr(metrics, "panel_spacing", None), int):
            return metrics
    except Exception:
        pass
    return DEFAULT_METRICS

#: Compact industrial density for the Industrial Light theme
#: (ThermoView-like): tighter buttons, inputs, and panel packing.
#: Other themes keep DEFAULT_METRICS untouched.
LIGHT_METRICS = ThemeMetrics(
    control_height=25,
    button_padding_v=3,
    input_padding_v=3,
    spacing_sm=6,
    spacing_md=10,
    panel_spacing=6,
    panel_group_spacing=6,
    panel_form_spacing=4,
    panel_group_margin=6,
    panel_group_margin_top=10,
)


@dataclass(frozen=True, slots=True)
class ThemeDefinition:
    """Complete visual identity of one named theme.

    ``name`` is the key used with ``ThemeManager.set_theme()`` /
    ``ThemeManager.apply_theme()`` and in ``config.yaml`` (``ui.theme``).
    """

    name: str
    display_name: str
    description: str
    is_dark: bool

    # -- surfaces & text -------------------------------------------------
    background: str
    surface: str
    surface_alt: str
    border: str
    border_strong: str
    text: str
    text_secondary: str
    muted_text: str
    title: str

    # -- button/emphasis colors ------------------------------------------
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

    # -- semantic states ---------------------------------------------------
    success: str
    success_bg: str
    warning: str
    warning_bg: str
    danger: str
    danger_bg: str
    info: str
    info_bg: str
    disabled: str
    alarm: str
    focus_ring: str

    # -- domain states ------------------------------------------------------
    camera_connected: str
    camera_disconnected: str
    camera_warning: str
    camera_error: str
    thermal_hot: str
    thermal_cold: str
    #: Default ROI overlay color; matches the historic overlay default so
    #: image rendering stays behavior-identical.
    overlay_roi: str = "#00FF7F"
    #: Text on filled emphasis buttons (primary/secondary/accent/danger).
    text_on_filled: str = "#FFFFFF"
    #: Text on the warning-tinted dirty state.
    text_on_warning: str = "#1A1A1A"

    metrics: ThemeMetrics = DEFAULT_METRICS

    def token(self, name: str) -> str:
        """Return a color/metric token by name (raises KeyError if unknown)."""
        if name.startswith("font_size_") or name in (
            "control_height",
            "button_padding_v",
            "input_padding_v",
            "radius_sm",
            "radius_md",
            "radius_lg",
            "border_width",
            "spacing_xs",
            "spacing_sm",
            "spacing_md",
            "spacing_lg",
            "panel_spacing",
            "panel_group_spacing",
            "panel_form_spacing",
            "panel_group_margin",
            "panel_group_margin_top",
        ):
            return str(getattr(self.metrics, name))
        value = getattr(self, name)
        if isinstance(value, ThemeMetrics):
            raise KeyError(f"Not a scalar token: {name!r}")
        return str(value)


__all__ = [
    "BUTTON_VARIANTS",
    "LABEL_ROLES",
    "STATUS_VALUES",
    "TILE_STATES",
    "ThemeDefinition",
    "ThemeMetrics",
    "DEFAULT_METRICS",
]
