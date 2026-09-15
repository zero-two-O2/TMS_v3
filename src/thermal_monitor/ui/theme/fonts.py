"""
ui.theme.fonts -- Global application font-size setting.

Settings -> Font Size submenu offers Small / 100% / 110% / 120% /
130% / 140%. The choice persists through QSettings and applies
immediately without restarting the application.

Implementation notes (§16: no massive QSS patch):

- The central stylesheet is token-driven, so scaling happens at the
  token level: ThemeManager carries ``font_scale_pct`` and regenerates
  the SAME stylesheet with scaled font/metric tokens, then repolishes
  (the standard theme-apply path — no new stylesheet system).
- Widgets with explicit fonts (side-shelf tabs, panel headers) expose a
  duck-typed ``refresh_font_metrics()`` hook that recomputes their sizes
  from the same scale; this module calls it on every top-level config
  widget after applying.
- Layouts/size policies do the rest: no fixed widget heights were added
  for scaling, scroll areas absorb growth, and the 4:3 image painters
  letterbox independently of fonts.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QSettings

logger = logging.getLogger(__name__)

#: QSettings scope for the font-size preference (deterministic in prod
#: and in tests; test files monkeypatch these to stay hermetic).
FONT_SETTINGS_ORG = "ThermalMonitor"
FONT_SETTINGS_APP = "TMS"
FONT_SETTINGS_KEY = "ui/font_scale_pct"

#: Default appearance (unchanged from the historic look).
FONT_SCALE_DEFAULT_PCT = 100

#: Offered scales. "Small" is 90%.
FONT_SCALE_OPTIONS: tuple[int, ...] = (90, 100, 110, 120, 130, 140)

#: Menu labels per scale.
FONT_SCALE_LABELS: dict[int, str] = {
    90: "Small",
    100: "100%",
    110: "110%",
    120: "120%",
    130: "130%",
    140: "140%",
}

#: Hard clamp for hand-entered / out-of-range persisted values.
FONT_SCALE_MIN_PCT = 80
FONT_SCALE_MAX_PCT = 150

#: Explicit widget type sizes (pt at 100% scale). The central stylesheet
#: cannot read ConfigurationModeWidget's PANEL_* constants (import cycle),
#: so these live here: the config window aliases them (PANEL_TAB_FONT_SIZE_PT
#: / PANEL_HEADER_FONT_SIZE_PT stay the single documented knobs) and the
#: central QSS rules below scale them with the font setting.
#: (Why QSS rules at all: repolish/show() resets explicitly-set widget
#: fonts back to the stylesheet value whenever a theme is active, so
#: setPointSize() alone silently never applies. Central rules + dynamic
#: properties are the same mechanism QLabel roles already use.)
#: Shelf tabs render at 10 pt so full vertical titles stay readable at
#: the supported window sizes (industrial tool-strip minimum).
SHELF_TAB_FONT_PT = 10
PANEL_TITLE_FONT_PT = 10


def _settings() -> QSettings:
    return QSettings(FONT_SETTINGS_ORG, FONT_SETTINGS_APP)


def clamp_font_scale(pct: object) -> int:
    """Coerce any value to a sane scale percentage."""
    try:
        value = int(pct)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return FONT_SCALE_DEFAULT_PCT
    return max(FONT_SCALE_MIN_PCT, min(FONT_SCALE_MAX_PCT, value))


def current_font_scale() -> int:
    """Persisted global font scale (default 100 when unset/invalid)."""
    try:
        raw = _settings().value(FONT_SETTINGS_KEY, FONT_SCALE_DEFAULT_PCT)
    except Exception:
        return FONT_SCALE_DEFAULT_PCT
    return clamp_font_scale(raw)


def save_font_scale(pct: int) -> int:
    """Persist the global font scale; returns the clamped value stored."""
    clamped = clamp_font_scale(pct)
    try:
        _settings().setValue(FONT_SETTINGS_KEY, clamped)
    except Exception:
        logger.debug("Font scale persist failed", exc_info=True)
    return clamped


def scaled_font_px(base_px: int, pct: int, floor: int = 8) -> int:
    """Scale a QSS pixel font/metric value.

    Readability floor (default 8px) applies to type; pass a lower floor
    for structural padding that must stay tight.
    """
    try:
        return max(int(floor), round(int(base_px) * int(pct) / 100))
    except (TypeError, ValueError):
        return int(base_px)


def scaled_point_size(base_pt: int, pct: int) -> int:
    """Scale an explicit point-size font (floored at 7pt)."""
    try:
        return max(7, round(int(base_pt) * int(pct) / 100))
    except (TypeError, ValueError):
        return int(base_pt)


def apply_font_scale(pct: int, theme_manager=None, app=None) -> int:
    """Persist + apply the global font scale immediately (no restart).

    Regenerates the central stylesheet through the shared ThemeManager
    (same path as theme switching) and asks every top-level widget with
    a ``refresh_font_metrics()`` hook (side shelves) to recompute its
    explicit fonts. Pure GUI operation: no acquisition, lifecycle, or
    rendering call happens here. Returns the clamped scale applied.
    """
    clamped = save_font_scale(pct)
    try:
        if theme_manager is not None:
            try:
                theme_manager.set_font_scale(clamped)
            except Exception:
                logger.debug("Theme manager font-scale set failed", exc_info=True)
            try:
                theme_manager.apply_and_refresh(app)
            except Exception:
                logger.debug("Theme re-apply after font change failed", exc_info=True)
        refresh_font_metrics_hooks(app)
    except Exception:
        logger.debug("Font scale apply failed", exc_info=True)
    return clamped


def refresh_font_metrics_hooks(app=None) -> int:
    """Call ``refresh_font_metrics()`` on every widget that provides it.

    Duck-typed (no import of window modules, so no import cycles):
    side-shelf hosts recompute tab/header fonts from the current scale.
    Returns the number of widgets refreshed.
    """
    try:
        if app is None:
            from PyQt6.QtWidgets import QApplication

            app = QApplication.instance()
        if app is None:
            return 0
        top_levels = list(app.topLevelWidgets())
    except Exception:
        return 0
    refreshed = 0
    seen: set[int] = set()
    for top in top_levels:
        try:
            from PyQt6.QtWidgets import QWidget as _QWidget

            candidates = [top] + list(top.findChildren(_QWidget))
        except Exception:
            continue
        for widget in candidates:
            if id(widget) in seen:
                continue
            seen.add(id(widget))
            hook = getattr(widget, "refresh_font_metrics", None)
            if callable(hook):
                try:
                    hook()
                    refreshed += 1
                except Exception:
                    logger.debug("refresh_font_metrics failed", exc_info=True)
    return refreshed


__all__ = [
    "FONT_SETTINGS_ORG",
    "FONT_SETTINGS_APP",
    "FONT_SETTINGS_KEY",
    "FONT_SCALE_DEFAULT_PCT",
    "FONT_SCALE_OPTIONS",
    "FONT_SCALE_LABELS",
    "FONT_SCALE_MIN_PCT",
    "FONT_SCALE_MAX_PCT",
    "SHELF_TAB_FONT_PT",
    "PANEL_TITLE_FONT_PT",
    "apply_font_scale",
    "clamp_font_scale",
    "current_font_scale",
    "refresh_font_metrics_hooks",
    "save_font_scale",
    "scaled_font_px",
    "scaled_point_size",
]
