"""Live view-finder navigation thumbnail (Configuration workstation).

A second *presentation* of the latest thermal frame already available to
Configuration Mode — never a second acquisition stream, SHM reader, or
processing pipeline. Shows the complete image with the main view's
visible-region rectangle; dragging the rectangle pans the main view
(latest-wins: only the newest image/rectangle is kept).
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal, QPointF
from PyQt6.QtGui import QImage, QPainter, QColor, QPen
from PyQt6.QtWidgets import QWidget


class ViewFinderWidget(QWidget):
    """Clickable navigation thumbnail for the IR workspace."""

    # IR display sampling modes (mirrors LiveThermalWidget; display only).
    IR_SCALING_MODES = ("fast", "smooth")

    # Normalized image-center (0..1) requested by a finder drag.
    viewport_dragged = pyqtSignal(float, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        # Connect-time IR display sampling (follows the Acquisition Setup
        # dialog choice; display only, thermal data untouched).
        self._ir_scaling = "fast"
        # Normalized visible rect (x0, y0, x1, y1) or None (whole image).
        self._viewport: tuple[float, float, float, float] | None = None
        self._dragging = False
        self.setMinimumSize(160, 120)
        self.setMouseTracking(True)

    def set_image(self, image: QImage | None) -> None:
        """Show the latest full frame (implicitly shared — no copy)."""
        self._image = image
        if image is None:
            self._viewport = None
        self.update()

    def set_viewport(self, rect: tuple[float, float, float, float] | None) -> None:
        """Set the main view's visible region (normalized, 0..1)."""
        if rect is not None:
            x0, y0, x1, y1 = (max(0.0, min(1.0, float(v))) for v in rect)
            if x1 <= x0 or y1 <= y0:
                rect = None
            else:
                rect = (x0, y0, x1, y1)
        if rect != self._viewport:
            self._viewport = rect
            self.update()

    @property
    def ir_scaling(self) -> str:
        """Current IR display sampling mode ('fast' or 'smooth')."""
        return self._ir_scaling

    def set_ir_scaling(self, mode: str) -> None:
        """Set the IR display sampling mode (connect-time only, display only)."""
        normalized = str(mode or "fast").lower()
        self._ir_scaling = normalized if normalized in self.IR_SCALING_MODES else "fast"
        self.update()

    def _ir_transformation(self):
        """Qt scaling mode matching the connect-time IR display setting."""
        if self._ir_scaling == "smooth":
            return Qt.TransformationMode.SmoothTransformation
        return Qt.TransformationMode.FastTransformation

    @property
    def viewport(self):
        """Current normalized viewport rect (test hook)."""
        return self._viewport

    def _image_geometry(self):
        """Widget rect of the fitted image: (x, y, w, h) or None."""
        if self._image is None or self._image.isNull():
            return None
        width, height = self.width(), self.height()
        if width <= 0 or height <= 0:
            return None
        image_w, image_h = self._image.width(), self._image.height()
        if image_w <= 0 or image_h <= 0:
            return None
        scale = min(width / image_w, height / image_h)
        draw_w, draw_h = image_w * scale, image_h * scale
        return (
            (width - draw_w) / 2.0,
            (height - draw_h) / 2.0,
            draw_w,
            draw_h,
        )

    def _pos_to_normalized(self, pos: QPointF) -> tuple[float, float] | None:
        """Widget point -> normalized image point (clamped 0..1)."""
        geometry = self._image_geometry()
        if geometry is None:
            return None
        draw_x, draw_y, draw_w, draw_h = geometry
        normalized_x = (pos.x() - draw_x) / draw_w if draw_w > 0 else 0.0
        normalized_y = (pos.y() - draw_y) / draw_h if draw_h > 0 else 0.0
        return (
            max(0.0, min(1.0, normalized_x)),
            max(0.0, min(1.0, normalized_y)),
        )

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(20, 20, 20))
        if self._image is None or self._image.isNull():
            painter.setPen(QColor(150, 150, 150))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "View Finder\n(thumbnail)"
            )
            return
        geometry = self._image_geometry()
        if geometry is None:
            return
        draw_x, draw_y, draw_w, draw_h = geometry
        # Pixel-preserving thumbnail by default (nearest-neighbor so
        # thermal pixels stay identifiable like ThermoView); the
        # connect-time "smooth" option selects bilinear instead.
        thumb = self._image.scaled(
            max(1, int(draw_w)),
            max(1, int(draw_h)),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            self._ir_transformation(),
        )
        painter.drawImage(int(draw_x), int(draw_y), thumb)
        if self._viewport is None:
            return
        # Dim the frame outside the main view's visible region, restore
        # the visible crop at full brightness, then outline it.
        x0, y0, x1, y1 = self._viewport
        rect_x, rect_y = draw_x + x0 * draw_w, draw_y + y0 * draw_h
        rect_w, rect_h = max(1, (x1 - x0) * draw_w), max(1, (y1 - y0) * draw_h)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        crop = thumb.copy(
            max(0, int(x0 * draw_w)),
            max(0, int(y0 * draw_h)),
            min(thumb.width(), int(rect_w)),
            min(thumb.height(), int(rect_h)),
        )
        painter.drawImage(int(rect_x), int(rect_y), crop)
        painter.setPen(QPen(QColor(0, 0, 0), 3))
        painter.drawRect(int(rect_x) - 1, int(rect_y) - 1, int(rect_w) + 2, int(rect_h) + 2)
        painter.setPen(QPen(QColor(255, 255, 255), 1))
        painter.drawRect(int(rect_x), int(rect_y), int(rect_w), int(rect_h))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._image is not None:
            point = self._pos_to_normalized(event.position())
            if point is not None:
                self._dragging = True
                self.viewport_dragged.emit(*point)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging and self._image is not None:
            point = self._pos_to_normalized(event.position())
            if point is not None:
                self.viewport_dragged.emit(*point)
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
        super().mouseReleaseEvent(event)


__all__ = ["ViewFinderWidget"]
