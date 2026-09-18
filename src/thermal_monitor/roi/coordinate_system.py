"""roi.coordinate_system -- widget <-> image coordinate conversions.

ROI geometry is stored in image pixel coordinates (row=y, col=x) tied
to the source image size. This module converts to/from widget pixels,
accounting for letterbox offset, uniform scale (zoom/fit), and pan.

Convention: pixel centers at integer coordinates; bounds-checked;
never modifies the stored ROI on resize.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ViewportMapping:
    """Uniform-scale mapping for one painted frame."""

    image_width: int
    image_height: int
    widget_width: int
    widget_height: int
    zoom: float | None = None  # None = fit-to-window
    pan_x: float = 0.0
    pan_y: float = 0.0
    max_zoom: float = 8.0

    def _fit_scale(self) -> float:
        return min(self.widget_width / self.image_width,
                   self.widget_height / self.image_height)

    @property
    def scale(self) -> float:
        fit = self._fit_scale()
        if self.zoom is None:
            return fit
        return min(max(float(self.zoom), 1e-6), self.max_zoom)

    @property
    def draw_origin(self) -> tuple[float, float]:
        s = self.scale
        scaled_w = self.image_width * s
        scaled_h = self.image_height * s
        dx = (self.widget_width - scaled_w) / 2.0 + self.pan_x
        dy = (self.widget_height - scaled_h) / 2.0 + self.pan_y
        if scaled_w <= self.widget_width:
            dx = (self.widget_width - scaled_w) / 2.0
        if scaled_h <= self.widget_height:
            dy = (self.widget_height - scaled_h) / 2.0
        return dx, dy

    def widget_to_image(self, wx: float, wy: float) -> tuple[float, float]:
        """Widget pixels -> image pixels (col=x, row=y), clamped to bounds."""
        s = self.scale
        dx, dy = self.draw_origin
        col = (wx - dx) / s
        row = (wy - dy) / s
        col = min(max(col, 0.0), float(self.image_width - 1))
        row = min(max(row, 0.0), float(self.image_height - 1))
        return col, row

    def image_to_widget(self, col: float, row: float) -> tuple[float, float]:
        """Image pixels -> widget pixels."""
        s = self.scale
        dx, dy = self.draw_origin
        return dx + col * s, dy + row * s

    def is_inside_image(self, wx: float, wy: float) -> bool:
        s = self.scale
        dx, dy = self.draw_origin
        return (dx <= wx < dx + self.image_width * s
                and dy <= wy < dy + self.image_height * s)


def mapping_for_widget(image_widget) -> ViewportMapping | None:
    """Build the widget<->image mapping from a live image widget's state.

    Single shared construction (display size, temperature-image size
    override, widget size, zoom, pan). Used by every ROI mouse path so
    press/move/release and hit-testing agree with what is painted.
    Returns None when no image is displayed yet.
    """
    image = getattr(image_widget, "_display_image", None)
    if image is None:
        return None
    try:
        iw, ih = image.width(), image.height()
    except Exception:
        return None
    temp = getattr(image_widget, "_temperature_image", None)
    if temp is not None:
        try:
            ih, iw = temp.shape[:2]
        except Exception:
            pass
    zoom = getattr(image_widget, "_zoom", None)
    pan = getattr(image_widget, "_pan_offset", None)
    try:
        px = float(pan.x()) if pan is not None else 0.0
        py = float(pan.y()) if pan is not None else 0.0
    except Exception:
        px, py = 0.0, 0.0
    try:
        widget_width = max(1, image_widget.width())
        widget_height = max(1, image_widget.height())
    except Exception:
        return None
    return ViewportMapping(
        image_width=int(iw), image_height=int(ih),
        widget_width=widget_width, widget_height=widget_height,
        zoom=zoom, pan_x=px, pan_y=py)


__all__ = ["ViewportMapping", "mapping_for_widget"]
