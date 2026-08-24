"""
ui.modes.observer_image -- Reusable live thermal image widget.

ThermoView-style thermal image display with:
- 640×480 aspect ratio preservation
- Smooth scaling
- ROI overlay support
- Cursor temperature readout
- Zoom and pan support
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QRect, QPointF
from PyQt6.QtGui import QImage, QPainter, QColor, QFont, QPen, QBrush, QMouseEvent, QWheelEvent
from PyQt6.QtWidgets import QWidget

import numpy as np


@dataclass
class ROIOverlay:
    """ROI overlay data for drawing on thermal image."""
    roi_id: str
    shape: str  # "rectangle1", "rectangle2", "circle", "ellipse", "polygon"
    geometry: dict  # shape-specific parameters in row/col (image coordinates)
    color: str = "#FFFF00"  # Default yellow
    selected: bool = False
    alarm_active: bool = False


class LiveThermalWidget(QWidget):
    """Displays the live thermal image with temperature display controls and ROI overlays."""

    # Signal emitted when mouse moves over image with temperature value
    cursor_temperature_changed = pyqtSignal(float)
    # Signal emitted when temperature range changes (auto or manual)
    range_changed = pyqtSignal(float, float)  # min, max

    def __init__(self) -> None:
        super().__init__()
        self._temperature_image: np.ndarray | None = None
        self._raw_thermal: np.ndarray | None = None
        self._display_image: QImage | None = None
        self._display_array: np.ndarray | None = None

        # Display settings
        self._palette = "temperature"
        self._auto_range = True
        self._manual_min = 20.0
        self._manual_max = 80.0
        self._zoom_mode = "Fit to Window"  # "Fit to Window", "50%", "100%", "200%", "400%"

        # ROI overlays
        self._roi_overlays: list[ROIOverlay] = []
        self._selected_roi_id: str | None = None

        # For cursor temperature
        self._last_mouse_pos: QPoint | None = None
        self.setMouseTracking(True)

        # Pan support
        self._pan_offset = QPointF(0, 0)
        self._is_panning = False
        self._pan_start_pos = QPoint()

        self.setMinimumSize(480, 360)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    @property
    def display_array(self) -> np.ndarray | None:
        """The copied uint8 display buffer (test/inspection hook)."""
        return self._display_array

    @property
    def temperature_image(self) -> np.ndarray | None:
        return self._temperature_image

    def set_frame(self, temperature_image: np.ndarray | None, frame) -> None:
        """Update the displayed image from a processed frame."""
        self._temperature_image = temperature_image
        self._raw_thermal = frame.payload.thermal if frame is not None else None
        self._rebuild_display()
        self.update()

    def clear(self) -> None:
        self._temperature_image = None
        self._raw_thermal = None
        self._display_image = None
        self._display_array = None
        self._roi_overlays = []
        self._selected_roi_id = None
        self.update()

    def set_palette(self, palette: str) -> None:
        """Set color palette for thermal display."""
        self._palette = palette
        self._rebuild_display()
        self.update()

    def set_auto_range(self, enabled: bool) -> None:
        """Enable/disable automatic temperature range."""
        self._auto_range = enabled
        self._rebuild_display()
        self.update()

    def set_temperature_range(self, min_temp: float, max_temp: float) -> None:
        """Set manual temperature range."""
        self._manual_min = min_temp
        self._manual_max = max_temp
        if not self._auto_range:
            self._rebuild_display()
            self.update()

    def set_zoom(self, zoom_text: str) -> None:
        """Set zoom mode."""
        self._zoom_mode = zoom_text
        if zoom_text != "Fit to Window":
            self._pan_offset = QPointF(0, 0)  # Reset pan on fixed zoom
        self.update()

    def set_roi_overlays(self, overlays: list[ROIOverlay]) -> None:
        """Set ROI overlays for drawing."""
        self._roi_overlays = overlays
        self.update()

    def highlight_roi(self, roi_id: str | None) -> None:
        """Highlight a specific ROI."""
        self._selected_roi_id = roi_id
        for overlay in self._roi_overlays:
            overlay.selected = (overlay.roi_id == roi_id)
        self.update()

    def _rebuild_display(self) -> None:
        src = self._temperature_image
        if src is None:
            src = self._raw_thermal
        if src is None:
            self._display_image = None
            self._display_array = None
            return

        src = np.asarray(src)
        if src.ndim != 2:
            self._display_image = None
            self._display_array = None
            return

        finite = np.isfinite(src)
        if not np.any(finite):
            display = np.zeros(src.shape, dtype=np.uint8)
            lo = 0.0
            hi = 1.0
        else:
            if self._auto_range:
                lo = float(src[finite].min())
                hi = float(src[finite].max())
            else:
                lo = self._manual_min
                hi = self._manual_max
            if hi <= lo:
                hi = lo + 1.0
            normalized = np.clip((src - lo) / (hi - lo), 0.0, 1.0)
            normalized[~finite] = 0.0
            display = (normalized * 255.0).astype(np.uint8)

        # Emit range changed signal
        self.range_changed.emit(lo, hi)

        # Apply palette
        display_rgb = self._apply_palette(display)
        self._display_array = display_rgb  # fresh array, no view of the source
        h, w = display_rgb.shape[:2]
        self._display_image = QImage(
            display_rgb.data, w, h, display_rgb.strides[0], QImage.Format.Format_RGB888
        ).copy()

    def _apply_palette(self, display: np.ndarray) -> np.ndarray:
        """Apply color palette to grayscale display."""
        h, w = display.shape
        rgb = np.zeros((h, w, 3), dtype=np.uint8)

        if self._palette == "temperature":
            # Temperature palette: blue -> cyan -> green -> yellow -> orange -> red
            for i in range(h):
                for j in range(w):
                    v = display[i, j] / 255.0
                    if v < 0.125:
                        # Dark blue to blue
                        t = v / 0.125
                        rgb[i, j] = [0, int(255 * t), int(128 + 127 * t)]
                    elif v < 0.25:
                        # Blue to cyan
                        t = (v - 0.125) / 0.125
                        rgb[i, j] = [0, 255, int(255 * t)]
                    elif v < 0.375:
                        # Cyan to green
                        t = (v - 0.25) / 0.125
                        rgb[i, j] = [0, int(255 * (1 - t) + 255 * t), int(255 * (1 - t))]
                    elif v < 0.5:
                        # Green to yellow
                        t = (v - 0.375) / 0.125
                        rgb[i, j] = [int(255 * t), 255, 0]
                    elif v < 0.625:
                        # Yellow to orange
                        t = (v - 0.5) / 0.125
                        rgb[i, j] = [255, int(255 * (1 - t) + 128 * t), 0]
                    elif v < 0.75:
                        # Orange to red
                        t = (v - 0.625) / 0.125
                        rgb[i, j] = [255, int(128 * (1 - t)), 0]
                    else:
                        # Red to dark red
                        t = (v - 0.75) / 0.25
                        rgb[i, j] = [int(255 * (1 - t) + 128 * t), 0, 0]
        elif self._palette == "iron":
            # Iron palette: black -> red -> orange -> yellow -> white
            for i in range(h):
                for j in range(w):
                    v = display[i, j] / 255.0
                    if v < 0.25:
                        t = v / 0.25
                        rgb[i, j] = [int(64 + 191 * t), 0, 0]
                    elif v < 0.5:
                        t = (v - 0.25) / 0.25
                        rgb[i, j] = [255, int(64 * t), 0]
                    elif v < 0.75:
                        t = (v - 0.5) / 0.25
                        rgb[i, j] = [255, int(64 + 191 * t), 0]
                    else:
                        t = (v - 0.75) / 0.25
                        rgb[i, j] = [255, 255, int(128 * t)]
        elif self._palette == "rainbow":
            # Rainbow palette
            for i in range(h):
                for j in range(w):
                    v = display[i, j] / 255.0
                    if v < 1/6:
                        t = v * 6
                        rgb[i, j] = [int(128 * (1 - t)), 0, int(128 + 127 * t)]
                    elif v < 2/6:
                        t = (v - 1/6) * 6
                        rgb[i, j] = [0, int(255 * t), 255]
                    elif v < 3/6:
                        t = (v - 2/6) * 6
                        rgb[i, j] = [0, 255, int(255 * (1 - t))]
                    elif v < 4/6:
                        t = (v - 3/6) * 6
                        rgb[i, j] = [int(255 * t), 255, 0]
                    elif v < 5/6:
                        t = (v - 4/6) * 6
                        rgb[i, j] = [255, int(255 * (1 - t)), 0]
                    else:
                        t = (v - 5/6) * 6
                        rgb[i, j] = [255, 0, 0]
        elif self._palette == "gray":
            rgb[:, :, 0] = display
            rgb[:, :, 1] = display
            rgb[:, :, 2] = display
        elif self._palette == "hot":
            # Hot palette: black -> red -> yellow -> white
            for i in range(h):
                for j in range(w):
                    v = display[i, j] / 255.0
                    if v < 1/3:
                        t = v * 3
                        rgb[i, j] = [int(255 * t), 0, 0]
                    elif v < 2/3:
                        t = (v - 1/3) * 3
                        rgb[i, j] = [255, int(255 * t), 0]
                    else:
                        t = (v - 2/3) * 3
                        rgb[i, j] = [255, 255, int(255 * t)]
        else:
            # Default: grayscale
            rgb[:, :, 0] = display
            rgb[:, :, 1] = display
            rgb[:, :, 2] = display

        return rgb

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Dark background
        painter.fillRect(self.rect(), QColor(20, 20, 20))

        if self._display_image is not None:
            # Calculate zoom
            zoom_factor = self._get_zoom_factor()

            if self._zoom_mode == "Fit to Window":
                scaled = self._display_image.scaled(
                    self.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                x = (self.width() - scaled.width()) // 2
                y = (self.height() - scaled.height()) // 2
            else:
                w = int(self._display_image.width() * zoom_factor)
                h = int(self._display_image.height() * zoom_factor)
                scaled = self._display_image.scaled(
                    w, h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                x = (self.width() - scaled.width()) // 2 + self._pan_offset.x()
                y = (self.height() - scaled.height()) // 2 + self._pan_offset.y()

            # Store image rect for coordinate mapping
            self._image_rect = QRect(x, y, scaled.width(), scaled.height())

            painter.drawImage(x, y, scaled)

            # Draw ROI overlays
            self._draw_roi_overlays(painter, x, y, scaled.width(), scaled.height())

        else:
            painter.setPen(QColor(150, 150, 150))
            painter.setFont(QFont("Segoe UI", 14))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No live data\n\nConnect a camera and press Start")

    def _get_zoom_factor(self) -> float:
        if self._zoom_mode == "Fit to Window":
            return 1.0
        zoom_map = {"50%": 0.5, "100%": 1.0, "200%": 2.0, "400%": 4.0}
        return zoom_map.get(self._zoom_mode, 1.0)

    def _draw_roi_overlays(self, painter: QPainter, img_x: int, img_y: int, img_w: int, img_h: int) -> None:
        """Draw ROI overlays on the thermal image."""
        if not self._roi_overlays or self._temperature_image is None:
            return

        # Image coordinates (640x480)
        img_height, img_width = self._temperature_image.shape[:2]

        # Scale factors
        scale_x = img_w / img_width
        scale_y = img_h / img_height

        for overlay in self._roi_overlays:
            # Determine color
            if overlay.alarm_active:
                color = QColor("#FF0000")  # Red for alarm
            elif overlay.selected:
                color = QColor("#00FF00")  # Green for selected
            else:
                color = QColor(overlay.color)

            pen_width = 3 if overlay.selected or overlay.alarm_active else 2
            pen = QPen(color, pen_width)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)

            # Draw based on shape
            geom = overlay.geometry
            shape = overlay.shape.lower()

            if shape == "rectangle1":
                # y1, x1, y2, x2 (row/col)
                y1 = geom.get("y1", 0) * scale_y + img_y
                x1 = geom.get("x1", 0) * scale_x + img_x
                y2 = geom.get("y2", img_height) * scale_y + img_y
                x2 = geom.get("x2", img_width) * scale_x + img_x
                painter.drawRect(int(x1), int(y1), int(x2 - x1), int(y2 - y1))

            elif shape == "rectangle2":
                # center_y, center_x, phi, length1, length2
                cy = geom.get("center_y", img_height/2) * scale_y + img_y
                cx = geom.get("center_x", img_width/2) * scale_x + img_x
                phi = geom.get("phi", 0)
                l1 = geom.get("length1", 50) * scale_y
                l2 = geom.get("length2", 50) * scale_x

                painter.save()
                painter.translate(cx, cy)
                painter.rotate(-phi * 180 / np.pi)
                painter.drawRect(int(-l2/2), int(-l1/2), int(l2), int(l1))
                painter.restore()

            elif shape == "circle":
                cy = geom.get("center_y", img_height/2) * scale_y + img_y
                cx = geom.get("center_x", img_width/2) * scale_x + img_x
                radius = geom.get("radius", 50) * min(scale_x, scale_y)
                painter.drawEllipse(QPointF(cx, cy), radius, radius)

            elif shape == "ellipse":
                cy = geom.get("center_y", img_height/2) * scale_y + img_y
                cx = geom.get("center_x", img_width/2) * scale_x + img_x
                phi = geom.get("phi", 0)
                r1 = geom.get("radius1", 50) * scale_y
                r2 = geom.get("radius2", 30) * scale_x

                painter.save()
                painter.translate(cx, cy)
                painter.rotate(-phi * 180 / np.pi)
                painter.drawEllipse(QPointF(0, 0), r2, r1)
                painter.restore()

            elif shape == "polygon":
                points = geom.get("points", [])
                if len(points) >= 3:
                    from PyQt6.QtGui import QPolygon
                    from PyQt6.QtCore import QPoint
                    qpoints = []
                    for py, px in points:
                        qx = px * scale_x + img_x
                        qy = py * scale_y + img_y
                        qpoints.append(QPoint(int(qx), int(qy)))
                    painter.drawPolygon(QPolygon(qpoints))

            # Draw ROI ID label
            if overlay.selected or overlay.alarm_active:
                painter.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
                painter.setPen(QPen(QColor("#FFFFFF"), 1))
                painter.setBrush(QBrush(QColor("#000000")))
                # Find a good position for label (top-left of ROI)
                if shape == "rectangle1":
                    label_x = geom.get("x1", 0) * scale_x + img_x + 4
                    label_y = geom.get("y1", 0) * scale_y + img_y + 16
                elif shape == "circle":
                    label_x = geom.get("center_x", img_width/2) * scale_x + img_x - 20
                    label_y = geom.get("center_y", img_height/2) * scale_y + img_y - geom.get("radius", 50) * min(scale_x, scale_y) - 4
                else:
                    label_x = img_x + 4
                    label_y = img_y + 16
                painter.drawRect(int(label_x - 2), int(label_y - 14),
                                 painter.fontMetrics().horizontalAdvance(overlay.roi_id) + 8, 18)
                painter.drawText(int(label_x + 2), int(label_y), overlay.roi_id)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Track mouse position for cursor temperature readout."""
        if self._temperature_image is not None and self._display_image is not None:
            if hasattr(self, '_image_rect') and self._image_rect.contains(event.pos()):
                img_x, img_y = self._widget_to_image_coords(event.position().x(), event.position().y())
                if 0 <= img_y < self._temperature_image.shape[0] and 0 <= img_x < self._temperature_image.shape[1]:
                    temp = float(self._temperature_image[img_y, img_x])
                    if np.isfinite(temp):
                        self.cursor_temperature_changed.emit(temp)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        """Reset cursor temperature when mouse leaves."""
        self.cursor_temperature_changed.emit(float('nan'))
        super().leaveEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Handle mouse wheel for zoom."""
        if self._zoom_mode != "Fit to Window" and self._display_image is not None:
            delta = event.angleDelta().y()
            zoom_levels = ["50%", "100%", "200%", "400%"]
            current_idx = zoom_levels.index(self._zoom_mode) if self._zoom_mode in zoom_levels else 1
            if delta > 0 and current_idx < len(zoom_levels) - 1:
                self.set_zoom(zoom_levels[current_idx + 1])
                self.zoom_changed.emit(zoom_levels[current_idx + 1])
            elif delta < 0 and current_idx > 0:
                self.set_zoom(zoom_levels[current_idx - 1])
                self.zoom_changed.emit(zoom_levels[current_idx - 1])
        super().wheelEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Handle mouse press for panning."""
        if event.button() == Qt.MouseButton.MiddleButton or (event.button() == Qt.MouseButton.LeftButton and event.modifiers() & Qt.KeyboardModifier.AltModifier):
            if hasattr(self, '_image_rect') and self._image_rect.contains(event.pos()):
                self._is_panning = True
                self._pan_start_pos = event.pos()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Handle mouse release for panning."""
        if event.button() == Qt.MouseButton.MiddleButton or (event.button() == Qt.MouseButton.LeftButton and event.modifiers() & Qt.KeyboardModifier.AltModifier):
            self._is_panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseReleaseEvent(event)

    def _widget_to_image_coords(self, widget_x: float, widget_y: float) -> tuple[int, int]:
        """Convert widget coordinates to image array indices."""
        if self._display_image is None or not hasattr(self, '_image_rect'):
            return (0, 0)

        img_rect = self._image_rect
        zoom_factor = self._get_zoom_factor()

        if self._zoom_mode == "Fit to Window":
            img_w = self._display_image.width()
            img_h = self._display_image.height()
            scale_w = img_rect.width() / img_w
            scale_h = img_rect.height() / img_h
            scale = min(scale_w, scale_h)
            display_w = img_w * scale
            display_h = img_h * scale
            offset_x = img_rect.x() + (img_rect.width() - display_w) / 2
            offset_y = img_rect.y() + (img_rect.height() - display_h) / 2
            img_x = int((widget_x - offset_x) / scale)
            img_y = int((widget_y - offset_y) / scale)
        else:
            img_w = self._display_image.width()
            img_h = self._display_image.height()
            display_w = img_w * zoom_factor
            display_h = img_h * zoom_factor
            offset_x = img_rect.x()
            offset_y = img_rect.y()
            img_x = int((widget_x - offset_x) / zoom_factor)
            img_y = int((widget_y - offset_y) / zoom_factor)

        # Clamp to image bounds
        if self._temperature_image is not None:
            img_x = max(0, min(img_x, self._temperature_image.shape[1] - 1))
            img_y = max(0, min(img_y, self._temperature_image.shape[0] - 1))

        return (img_x, img_y)


__all__ = ["LiveThermalWidget", "ROIOverlay"]