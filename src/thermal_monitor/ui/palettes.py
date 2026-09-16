"""Central authoritative thermal palette registry.

Single source of truth for every thermal palette in TMS_v3.

Both the actual thermal rendering (``PALETTE_LUTS`` consumed by
``thermal_render_worker`` / ``observer_image``) and every visual preview
(temperature-scale legend gradient, palette-combo dropdown strip, combo
icons) are derived from :data:`PALETTE_STOPS` below.

To add or remove a palette in the future, edit ONLY:

    * :data:`PALETTE_STOPS`   -- authoritative RGB gradient stops
    * :data:`PALETTE_DISPLAY` -- display name shown in the UI
    * :data:`PALETTE_ORDER`   -- ordering used by the dropdown/menus

Everything else (256-entry NumPy LUTs, QColors, preview pixmaps) is
derived automatically, so the preview can never drift from rendering.
"""

from __future__ import annotations

import numpy as np
from PyQt6.QtGui import QColor, QImage, QPixmap

_LUT_SIZE = 256


def _lut(points: list[tuple[int, int, int]]) -> np.ndarray:
    """Interpolate RGB control points to a 256-entry uint8 LUT."""
    values = np.asarray(points, dtype=np.float32)
    x = np.linspace(0.0, 1.0, len(values), dtype=np.float32)
    indices = np.linspace(0.0, 1.0, _LUT_SIZE, dtype=np.float32)
    return np.rint(
        np.column_stack(
            [np.interp(indices, x, values[:, channel]) for channel in range(3)]
        )
    ).astype(np.uint8)


#: Authoritative gradient stops per palette key. Existing five palettes
#: keep their exact historic control points so rendering is unchanged.
PALETTE_STOPS: dict[str, list[tuple[int, int, int]]] = {
    # --- Existing palettes (unchanged behavior) -------------------------
    "temperature": [
        (0, 0, 128),
        (0, 255, 255),
        (0, 255, 0),
        (255, 255, 0),
        (255, 128, 0),
        (255, 0, 0),
        (128, 0, 0),
    ],
    "iron": [
        (0, 0, 0),
        (255, 0, 0),
        (255, 128, 0),
        (255, 255, 0),
        (255, 255, 128),
    ],
    "rainbow": [
        (128, 0, 128),
        (0, 0, 255),
        (0, 255, 255),
        (0, 255, 0),
        (255, 255, 0),
        (255, 0, 0),
    ],
    "gray": [
        (0, 0, 0),
        (255, 255, 255),
    ],
    "hot": [
        (0, 0, 0),
        (255, 0, 0),
        (255, 255, 0),
        (255, 255, 255),
    ],
    # --- New ThermoView palettes ---------------------------------------
    # RContrast: high-contrast rainbow (black start, white-hot end).
    "rcontrast": [
        (0, 0, 0),
        (0, 0, 255),
        (0, 255, 255),
        (255, 255, 0),
        (255, 0, 0),
        (255, 255, 255),
    ],
    # Rain900: darker, saturated variant of Rain.
    "rain900": [
        (16, 0, 64),
        (64, 0, 192),
        (0, 128, 255),
        (0, 255, 255),
        (255, 255, 0),
        (255, 64, 0),
        (192, 0, 0),
    ],
    # Rain: classic blue -> cyan -> green -> yellow -> red.
    "rain": [
        (0, 0, 255),
        (0, 255, 255),
        (0, 255, 0),
        (255, 255, 0),
        (255, 0, 0),
    ],
    # Fire: black -> maroon -> red -> orange -> yellow -> white.
    "fire": [
        (0, 0, 0),
        (128, 0, 0),
        (255, 0, 0),
        (255, 128, 0),
        (255, 255, 0),
        (255, 255, 255),
    ],
    # Yellow: yellow monochrome (black -> brown -> orange -> yellow -> white).
    "yellow": [
        (0, 0, 0),
        (64, 48, 0),
        (128, 96, 0),
        (255, 192, 0),
        (255, 255, 0),
        (255, 255, 192),
        (255, 255, 255),
    ],
    # Grayred: grayscale ramp with a red hot-end.
    "grayred": [
        (0, 0, 0),
        (51, 51, 51),
        (102, 102, 102),
        (153, 153, 153),
        (204, 204, 204),
        (255, 0, 0),
    ],
    # Midgray: compressed mid-tone grays (no pure black/white).
    "midgray": [
        (48, 48, 48),
        (96, 96, 96),
        (144, 144, 144),
        (192, 192, 192),
        (224, 224, 224),
    ],
    # Y-Glow: black -> purple -> magenta -> orange -> yellow -> white.
    "yglow": [
        (0, 0, 0),
        (64, 0, 64),
        (192, 0, 128),
        (255, 64, 0),
        (255, 192, 0),
        (255, 255, 128),
        (255, 255, 255),
    ],
}

#: Display name per palette key (shown in the dropdown/menus).
PALETTE_DISPLAY: dict[str, str] = {
    "temperature": "Temperature",
    "rainbow": "Rainbow",
    "iron": "Iron",
    "gray": "Gray",
    "rcontrast": "RContrast",
    "rain900": "Rain900",
    "rain": "Rain",
    "fire": "Fire",
    "yellow": "Yellow",
    "grayred": "Grayred",
    "midgray": "Midgray",
    "yglow": "Y-Glow",
    # Legacy palette preserved for backward compatibility (was part of
    # the original 5-palette set); kept last so the required ThermoView
    # order stays intact.
    "hot": "Hot",
}

#: Dropdown/menu order. Matches the required ThermoView order with the
#: legacy "hot" palette appended (not removed, to avoid regressions).
PALETTE_ORDER: list[str] = [
    "temperature",
    "rainbow",
    "iron",
    "gray",
    "rcontrast",
    "rain900",
    "rain",
    "fire",
    "yellow",
    "grayred",
    "midgray",
    "yglow",
    "hot",
]

#: Required ThermoView set from the task (without legacy "hot").
REQUIRED_PALETTES: list[str] = [
    "temperature",
    "rainbow",
    "iron",
    "gray",
    "rcontrast",
    "rain900",
    "rain",
    "fire",
    "yellow",
    "grayred",
    "midgray",
    "yglow",
]


def _build_luts() -> dict[str, np.ndarray]:
    luts: dict[str, np.ndarray] = {}
    for key in PALETTE_ORDER:
        stops = PALETTE_STOPS[key]
        if key == "gray":
            # Identical to the historic repeat-ramp; built via stops keeps
            # one code path while producing the same linear ramp.
            luts[key] = np.repeat(
                np.arange(_LUT_SIZE, dtype=np.uint8)[:, None], 3, axis=1
            )
        else:
            luts[key] = _lut(stops)
    return luts


#: Authoritative 256x3 uint8 rendering LUTs, derived from PALETTE_STOPS.
PALETTE_LUTS: dict[str, np.ndarray] = _build_luts()


def palette_keys() -> list[str]:
    """Ordered palette keys (dropdown order)."""
    return list(PALETTE_ORDER)


def palette_display_names() -> list[str]:
    """Ordered display names (dropdown order)."""
    return [PALETTE_DISPLAY[key] for key in PALETTE_ORDER]


def key_to_display(key: str) -> str:
    """Map a palette key to its display name (unknown -> Temperature)."""
    return PALETTE_DISPLAY.get(key, PALETTE_DISPLAY["temperature"])


def display_to_key(display: str) -> str:
    """Map a display name to its palette key (unknown -> temperature)."""
    for key, name in PALETTE_DISPLAY.items():
        if name == display:
            return key
    return "temperature"


def get_lut(key: str) -> np.ndarray:
    """Return the authoritative 256x3 rendering LUT for ``key``."""
    return PALETTE_LUTS.get(key, PALETTE_LUTS["temperature"])


def get_qcolors(key: str) -> list[QColor]:
    """Return QColors for the palette's authoritative stops.

    Used by the temperature-scale legend gradient; same definition as
    the rendering LUT, so legend and image can never disagree.
    """
    stops = PALETTE_STOPS.get(key, PALETTE_STOPS["temperature"])
    return [QColor(r, g, b) for r, g, b in stops]


def build_preview_image(key: str, width: int = 256, height: int = 12) -> QImage:
    """Build a horizontal gradient preview from the rendering LUT.

    The preview samples the SAME 256-entry LUT used for thermal
    rendering, guaranteeing preview == rendering.
    """
    lut = get_lut(key)
    width = max(1, int(width))
    height = max(1, int(height))
    # Sample the LUT uniformly across the requested width.
    xs = np.linspace(0, 255, width).astype(np.int64)
    row = lut[xs]  # (width, 3)
    img = np.repeat(row[None, :, :], height, axis=0)  # (height, width, 3)
    buf = np.ascontiguousarray(img, dtype=np.uint8)
    h, w = buf.shape[0], buf.shape[1]
    image = QImage(buf.data, w, h, buf.strides[0], QImage.Format.Format_RGB888).copy()
    # Keep the buffer alive semantics: .copy() detaches, safe to return.
    return image


def build_preview_pixmap(key: str, width: int = 64, height: int = 12) -> QPixmap:
    """Build a compact preview pixmap for combo icons/delegate fallback."""
    return QPixmap.fromImage(build_preview_image(key, width, height))


__all__ = [
    "PALETTE_STOPS",
    "PALETTE_DISPLAY",
    "PALETTE_ORDER",
    "PALETTE_LUTS",
    "REQUIRED_PALETTES",
    "palette_keys",
    "palette_display_names",
    "key_to_display",
    "display_to_key",
    "get_lut",
    "get_qcolors",
    "build_preview_image",
    "build_preview_pixmap",
]
