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

import time
from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QRect, QPointF, pyqtSlot
from PyQt6.QtGui import QImage, QPainter, QColor, QFont, QPen, QBrush, QMouseEvent, QWheelEvent
from PyQt6.QtWidgets import QWidget

import numpy as np

from thermal_monitor.core.frame_latency import (
    get_default_tracker as _latency_tracker,
    latency_enabled as _latency_enabled,
)
from thermal_monitor.ui.modes.thermal_render_worker import RenderRequest, ThermalRenderWorker


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
    # Signal emitted when the zoom mode changes (e.g. via mouse wheel)
    zoom_changed = pyqtSignal(str)  # zoom mode text
    # Signal emitted on any zoom/pan/fit/resize view change (finder sync).
    view_changed = pyqtSignal()
    rendered_frame = pyqtSignal(object, object)  # QImage, thumbnail QImage
    render_error = pyqtSignal(str)

    # View-only zoom bounds (never touch source data or calibration).
    _ZOOM_MAX = 8.0  # relative to 1:1 native pixels
    _WHEEL_STEP = 1.25

    def __init__(self) -> None:
        super().__init__()
        self._temperature_image: np.ndarray | None = None
        self._raw_thermal: np.ndarray | None = None
        self._display_image: QImage | None = None
        self._display_array: np.ndarray | None = None
        self._render_worker = ThermalRenderWorker(parent=self)
        self._render_worker.latest_ready.connect(self._on_latest_ready)
        self._render_worker.render_error.connect(self._on_render_error)
        self.destroyed.connect(self._render_worker.stop)
        self._render_worker.start()
        self._last_submitted_sequence = -1
        self._last_hw_sequence: int | None = None
        self._last_camera_id: str | None = None
        self._last_acq_mono_ns: int | None = None
        # Camera-session gate: when set, set_frame() drops frames whose
        # descriptor camera_id differs, so a previous camera's queued
        # results can never reach the renderer after a switch. Widgets
        # that never set a session (Live wall, Offline) keep the
        # previous accept-everything behavior.
        self._session_camera_id: str | None = None
        # seq -> (camera_id, hw_sequence, acq_mono_ns) for display-age
        # correlation at render completion; bounded by pruning on render.
        self._pending_meta: dict[int, tuple[str | None, int | None, int | None]] = {}

        # Display settings
        self._palette = "temperature"
        self._auto_range = True
        self._manual_min = 20.0
        self._manual_max = 80.0
        # View-only zoom: None = fit-to-window (the minimum); otherwise an
        # absolute scale relative to 1:1 native pixels. The transform is
        # applied at paint time — the source thermal frame, conversion and
        # calibration are never altered, and wheel events copy nothing.
        self._zoom: float | None = None
        self._zoom_mode = "Fit to Window"  # compat text: Fit or "NN%"

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

    def set_session(self, camera_id: str | None) -> None:
        """Begin a new camera session on this widget.

        Clears the displayed image, resets the accepted-sequence baseline
        (the new camera numbers its frames from 0) and tells the render
        worker to drop any late request from the old camera. State owned
        by the panels (ROI, alarms, scale) is untouched.
        """
        self._session_camera_id = camera_id
        self._last_submitted_sequence = -1
        self._last_hw_sequence = None
        self._last_camera_id = camera_id
        self._last_acq_mono_ns = None
        self._pending_meta.clear()
        self._render_worker.set_session(camera_id)
        self.clear()

    def set_frame(self, temperature_image: np.ndarray | None, frame, minimum: float | None = None, maximum: float | None = None) -> None:
        """Submit an immutable frame snapshot; rendering is never synchronous."""
        if self._session_camera_id is not None and frame is not None:
            descriptor = getattr(frame, "descriptor", None)
            frame_camera = getattr(descriptor, "camera_id", None)
            if frame_camera is not None and frame_camera != self._session_camera_id:
                return  # stale frame from a previous camera session
        self._temperature_image = np.asarray(temperature_image) if temperature_image is not None else None
        self._raw_thermal = frame.payload.thermal if frame is not None else None
        source = self._temperature_image
        if source is None and self._raw_thermal is not None:
            source = np.asarray(self._raw_thermal)
        if source is None or np.asarray(source).ndim != 2:
            self.clear()
            return
        sequence_value = getattr(getattr(frame, "descriptor", None), "sequence", None)
        try:
            sequence = int(sequence_value)
        except (TypeError, ValueError):
            sequence = self._last_submitted_sequence + 1
        if sequence <= self._last_submitted_sequence:
            return
        self._last_submitted_sequence = sequence
        descriptor = getattr(frame, "descriptor", None)
        thermal_meta = getattr(descriptor, "thermal", None)
        hw_sequence = getattr(thermal_meta, "sequence", None)
        try:
            hw_sequence = int(hw_sequence) if hw_sequence is not None else None
        except (TypeError, ValueError):
            hw_sequence = None
        self._last_hw_sequence = hw_sequence
        camera_id = getattr(descriptor, "camera_id", None)
        acq_mono = getattr(descriptor, "monotonic_timestamp", None)
        try:
            acq_mono_ns = int(float(acq_mono) * 1e9) if acq_mono is not None else None
        except (TypeError, ValueError):
            acq_mono_ns = None
        self._last_camera_id = camera_id
        self._last_acq_mono_ns = acq_mono_ns
        if _latency_enabled():
            self._pending_meta[sequence] = (camera_id, hw_sequence, acq_mono_ns)
        self._render_worker.submit(RenderRequest(np.asarray(source), sequence, minimum, maximum, hw_sequence, camera_id, acq_mono_ns))

    def clear(self) -> None:
        self._temperature_image = None
        self._raw_thermal = None
        self._display_image = None
        self._display_array = None
        self._roi_overlays = []
        self._selected_roi_id = None
        # Reset the accepted-sequence baseline so a reconnect (which
        # renumbers frames from 0) is accepted. Stale cross-camera data
        # is still rejected by the session camera_id gate in set_frame()
        # and by the generation check at the result slot.
        self._last_submitted_sequence = -1
        self._pending_meta.clear()
        self.update()

    def set_palette(self, palette: str) -> None:
        """Set color palette for thermal display."""
        self._palette = palette
        self._render_worker.set_palette(palette)
        self.update()

    def set_auto_range(self, enabled: bool) -> None:
        """Enable/disable automatic temperature range."""
        self._auto_range = enabled
        self.update()

    def set_temperature_range(self, min_temp: float, max_temp: float) -> None:
        """Set manual temperature range."""
        self._manual_min = min_temp
        self._manual_max = max_temp
        if not self._auto_range:
            self.update()

    def set_zoom(self, zoom_text: str) -> None:
        """Set zoom from a mode string (compat: "Fit to Window" or "NN%")."""
        text = (zoom_text or "").strip()
        if text.lower().startswith("fit"):
            self.zoom_fit()
            return
        try:
            percent = float(text.rstrip("%"))
        except (TypeError, ValueError):
            return
        self.set_zoom_factor(percent / 100.0)

    def zoom_fit(self) -> None:
        """Reset to fit-to-window (the minimum zoom)."""
        self._zoom = None
        self._pan_offset = QPointF(0, 0)
        self._sync_zoom_mode()
        self.view_changed.emit()
        self.update()

    def zoom_one_to_one(self) -> None:
        """Show native pixels (1 image px = 1 screen px), centered."""
        self.set_zoom_factor(1.0)

    def zoom_in(self, center: QPointF | None = None) -> None:
        """Zoom in one step around ``center`` (default: widget center)."""
        if center is not None and not hasattr(center, "x"):
            center = None  # defensive: never trust a signal payload here
        self.zoom_at(center if center is not None else QPointF(self.width() / 2, self.height() / 2), self._WHEEL_STEP)

    def zoom_out(self, center: QPointF | None = None) -> None:
        """Zoom out one step around ``center`` (floors at fit-to-window)."""
        if center is not None and not hasattr(center, "x"):
            center = None  # defensive: never trust a signal payload here
        self.zoom_at(center if center is not None else QPointF(self.width() / 2, self.height() / 2), 1.0 / self._WHEEL_STEP)

    def zoom_at(self, pos, factor: float) -> None:
        """Smooth bounded zoom around a widget point (wheel/buttons).

        Zooming out past the fit scale snaps back to fit-to-window.
        The image point under ``pos`` stays under it (cursor-anchored).
        """
        current = self._view_transform()
        if current is None:
            return
        s_old, dx_old, dy_old, _, _ = current
        fit = self._fit_scale()
        if fit is None:
            return
        s_new = s_old * factor
        if s_new <= fit * 1.001:
            self.zoom_fit()
            return
        s_new = min(s_new, self._ZOOM_MAX)
        image = self._display_image
        pixel_x = (float(pos.x()) - dx_old) / s_old
        pixel_y = (float(pos.y()) - dy_old) / s_old
        self._zoom = s_new
        scaled_w = max(1, int(round(image.width() * s_new)))
        scaled_h = max(1, int(round(image.height() * s_new)))
        dx_new = float(pos.x()) - pixel_x * s_new
        dy_new = float(pos.y()) - pixel_y * s_new
        self._pan_offset = QPointF(
            dx_new - (self.width() - scaled_w) / 2.0,
            dy_new - (self.height() - scaled_h) / 2.0,
        )
        self._clamp_pan(scaled_w, scaled_h)
        self._sync_zoom_mode()
        self.view_changed.emit()
        self.update()

    def set_zoom_factor(self, zoom: float | None) -> None:
        """Set an absolute zoom scale (None = fit); clamped to [>0, max].

        Explicit requests (1:1, "NN%", persisted values) are honored
        exactly — even below the fit scale on large windows. The
        fit-to-window floor applies only to zoom_out()/wheel-out, so an
        explicit native-pixels request is never forced up to the max.
        """
        if zoom is None:
            self.zoom_fit()
            return
        try:
            value = float(zoom)
        except (TypeError, ValueError):
            return
        self._zoom = min(max(value, 1e-6), self._ZOOM_MAX)
        self._clamp_pan()
        self._sync_zoom_mode()
        self.view_changed.emit()
        self.update()

    def set_pan_offset(self, x: float, y: float) -> None:
        """Restore a persisted pan offset (clamped to the current view)."""
        self._pan_offset = QPointF(float(x), float(y))
        self._clamp_pan()
        self.view_changed.emit()
        self.update()

    def zoom_percent(self) -> str:
        """Human-readable zoom state for toolbars ("Fit" or "NN%")."""
        if self._zoom is None:
            return "Fit"
        return f"{int(round(self._zoom * 100))}%"

    def is_fit(self) -> bool:
        """True while the view shows the whole image fitted."""
        return self._zoom is None

    def _sync_zoom_mode(self) -> None:
        """Keep the compat mode text aligned with the absolute zoom."""
        if self._zoom is None:
            mode = "Fit to Window"
        else:
            mode = f"{int(round(self._zoom * 100))}%"
        if mode != self._zoom_mode:
            self._zoom_mode = mode
            self.zoom_changed.emit(mode)

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

    def _resubmit_latest(self) -> None:
        source = self._temperature_image if self._temperature_image is not None else self._raw_thermal
        if source is not None:
            minimum = None if self._auto_range else self._manual_min
            maximum = None if self._auto_range else self._manual_max
            sequence = self._last_submitted_sequence + 1
            if _latency_enabled():
                self._pending_meta[sequence] = (
                    self._last_camera_id,
                    self._last_hw_sequence,
                    self._last_acq_mono_ns,
                )
            self._render_worker.submit(RenderRequest(np.asarray(source), sequence, minimum, maximum, self._last_hw_sequence, self._last_camera_id, self._last_acq_mono_ns))

    @pyqtSlot(object, object, float, float, int, object, object)
    def _on_rendered(self, image: QImage, temperature: np.ndarray, minimum: float, maximum: float, sequence: int, thumbnail: QImage, rgb: np.ndarray) -> None:
        self._display_image = image
        self._display_array = rgb
        if _latency_enabled():
            meta = self._pending_meta.pop(sequence, None)
            # Prune superseded entries: delivery is ordered, so anything at
            # or below the rendered sequence can never complete later.
            for old in [s for s in self._pending_meta if s <= sequence]:
                del self._pending_meta[old]
            if meta is not None and meta[0] is not None:
                _latency_tracker().note_displayed(
                    meta[0], sequence, meta[1], meta[2], time.perf_counter_ns()
                )
        self.range_changed.emit(minimum, maximum)
        self.rendered_frame.emit(image, thumbnail)
        self._temperature_image = temperature
        self.update()

    @pyqtSlot()
    def _on_latest_ready(self) -> None:
        output = self._render_worker.take_latest_output()
        if output is not None:
            self._on_rendered(*output)

    @pyqtSlot(str)
    def _on_render_error(self, message: str) -> None:
        self._display_image = None
        self._display_array = None
        self.render_error.emit(message)

    def closeEvent(self, event) -> None:
        self._render_worker.stop()
        super().closeEvent(event)

    def close(self) -> bool:
        """Stop the persistent renderer even for widgets never shown."""
        if self._render_worker.isRunning():
            self._render_worker.stop()
        return super().close()

    def __del__(self) -> None:
        try:
            worker = getattr(self, "_render_worker", None)
            if worker is not None and worker.isRunning():
                worker.stop()
        except RuntimeError:
            pass

    # Kept as a non-GUI utility for compatibility and unit-level palette tests.
    def _apply_palette(self, display: np.ndarray) -> np.ndarray:
        """Vectorized palette conversion; called only by the render worker path."""
        from thermal_monitor.ui.modes.thermal_render_worker import PALETTE_LUTS
        return PALETTE_LUTS.get(self._palette, PALETTE_LUTS["gray"])[np.asarray(display, dtype=np.uint8)]

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Dark background
        painter.fillRect(self.rect(), QColor(20, 20, 20))

        transform = self._view_transform()
        if transform is None:
            painter.setPen(QColor(150, 150, 150))
            painter.setFont(QFont("Segoe UI", 14))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No live data\n\nConnect a camera and press Start")
            if hasattr(self, '_image_rect'):
                del self._image_rect
            return

        scale, draw_x, draw_y, scaled_w, scaled_h = transform
        # Uniform scale only (same factor both axes): 4:3 geometry preserved.
        scaled = self._display_image.scaled(
            scaled_w, scaled_h,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        int_x, int_y = int(draw_x), int(draw_y)
        painter.drawImage(int_x, int_y, scaled)

        # Draw ROI overlays
        self._draw_roi_overlays(painter, int_x, int_y, scaled.width(), scaled.height())

    def _fit_scale(self) -> float | None:
        """Scale that fits the whole image (None without image/geometry)."""
        image = self._display_image
        width, height = self.width(), self.height()
        if image is None or image.width() <= 0 or image.height() <= 0:
            return None
        if width <= 0 or height <= 0:
            return None
        return min(width / image.width(), height / image.height())

    def _clamp_pan(self, scaled_w: int | None = None, scaled_h: int | None = None) -> None:
        """Constrain the pan offset so the view stays usable.

        When the scaled image is smaller than the viewport the offset is
        zeroed (paint centers it); when larger, panning stops at the
        image edges so no oversized empty areas appear.
        """
        image = self._display_image
        width, height = self.width(), self.height()
        if image is None or width <= 0 or height <= 0:
            return
        scale = self._ZOOM_MAX if self._zoom is None else self._zoom
        if self._zoom is None:
            fit = self._fit_scale()
            scale = fit if fit is not None else 1.0
        if scaled_w is None:
            scaled_w = max(1, int(round(image.width() * scale)))
        if scaled_h is None:
            scaled_h = max(1, int(round(image.height() * scale)))
        pan_x, pan_y = self._pan_offset.x(), self._pan_offset.y()
        if scaled_w <= width:
            pan_x = 0.0
        else:
            lower, upper = (width - scaled_w) / 2.0, (scaled_w - width) / 2.0
            pan_x = min(upper, max(lower, pan_x))
        if scaled_h <= height:
            pan_y = 0.0
        else:
            lower, upper = (height - scaled_h) / 2.0, (scaled_h - height) / 2.0
            pan_y = min(upper, max(lower, pan_y))
        self._pan_offset = QPointF(pan_x, pan_y)

    def _view_transform(self):
        """Single source for the image->widget mapping.

        Returns ``(scale, draw_x, draw_y, scaled_w, scaled_h)`` with a
        uniform scale (never distorted; 4:3 geometry preserved) and keeps
        ``self._image_rect`` aligned for hit-testing. None without image.
        """
        image = self._display_image
        width, height = self.width(), self.height()
        if image is None or image.width() <= 0 or image.height() <= 0:
            return None
        if width <= 0 or height <= 0:
            return None
        fit = min(width / image.width(), height / image.height())
        scale = fit if self._zoom is None else min(self._zoom, self._ZOOM_MAX)
        scaled_w = max(1, int(round(image.width() * scale)))
        scaled_h = max(1, int(round(image.height() * scale)))
        self._clamp_pan(scaled_w, scaled_h)
        draw_x = (width - scaled_w) / 2.0 + self._pan_offset.x()
        draw_y = (height - scaled_h) / 2.0 + self._pan_offset.y()
        if scaled_w <= width:
            draw_x = (width - scaled_w) / 2.0
        if scaled_h <= height:
            draw_y = (height - scaled_h) / 2.0
        self._image_rect = QRect(int(draw_x), int(draw_y), scaled_w, scaled_h)
        return (scale, draw_x, draw_y, scaled_w, scaled_h)

    def viewport_rect_normalized(self):
        """Visible image region as normalized (x0, y0, x1, y1), 0..1.

        Drives the View Finder rectangle. None when the whole image is
        fitted (or no image): the finder then shows the full frame.
        """
        image = self._display_image
        if image is None or self._zoom is None:
            return None
        transform = self._view_transform()
        if transform is None:
            return None
        scale, draw_x, draw_y, _, _ = transform
        width, height = self.width(), self.height()
        image_w, image_h = image.width(), image.height()
        x0 = max(0.0, min(1.0, (-draw_x / scale) / image_w))
        y0 = max(0.0, min(1.0, (-draw_y / scale) / image_h))
        x1 = max(0.0, min(1.0, ((width - draw_x) / scale) / image_w))
        y1 = max(0.0, min(1.0, ((height - draw_y) / scale) / image_h))
        if x1 <= x0 or y1 <= y0:
            return None
        return (x0, y0, x1, y1)

    def pan_to_normalized(self, center_x: float, center_y: float) -> None:
        """Center the view on a normalized image point (finder drag).

        When fitted (whole image visible) the view first zooms to 2x so
        the drag target is meaningful, then pans to it.
        """
        image = self._display_image
        if image is None:
            return
        if self._zoom is None:
            self._zoom = min(2.0, self._ZOOM_MAX)
        scale = min(self._zoom, self._ZOOM_MAX)
        width, height = self.width(), self.height()
        scaled_w = max(1, int(round(image.width() * scale)))
        scaled_h = max(1, int(round(image.height() * scale)))
        center_x = max(0.0, min(1.0, center_x))
        center_y = max(0.0, min(1.0, center_y))
        self._pan_offset = QPointF(
            width / 2.0 - center_x * image.width() * scale - (width - scaled_w) / 2.0,
            height / 2.0 - center_y * image.height() * scale - (height - scaled_h) / 2.0,
        )
        self._clamp_pan(scaled_w, scaled_h)
        self._sync_zoom_mode()
        self.view_changed.emit()
        self.update()

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
        """Track mouse position for cursor temperature readout (and panning)."""
        if self._is_panning:
            delta = event.pos() - self._pan_start_pos
            self._pan_start_pos = event.pos()
            self._pan_offset = QPointF(
                self._pan_offset.x() + delta.x(),
                self._pan_offset.y() + delta.y(),
            )
            self._clamp_pan()
            self.view_changed.emit()
            self.update()
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
        """Mouse wheel over the IR image: up zooms in, down zooms out.

        Smooth, bounded, cursor-anchored; operates on the displayed view
        only (no source copies, no calibration impact).
        """
        if self._display_image is not None:
            if not hasattr(self, '_image_rect') or self._image_rect.contains(event.position().toPoint()):
                delta = event.angleDelta().y()
                if delta > 0:
                    self.zoom_at(event.position(), self._WHEEL_STEP)
                    event.accept()
                    return
                elif delta < 0:
                    self.zoom_at(event.position(), 1.0 / self._WHEEL_STEP)
                    event.accept()
                    return
        super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:
        """Keyboard zoom: + / - steps, 0 = Fit, 1 = 1:1."""
        if self._display_image is not None:
            text = event.text()
            key = event.key()
            if text in ("+", "=") or key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                self.zoom_in()
                event.accept()
                return
            if text in ("-", "_") or key == Qt.Key.Key_Minus:
                self.zoom_out()
                event.accept()
                return
            if text == "0":
                self.zoom_fit()
                event.accept()
                return
            if text == "1":
                self.zoom_one_to_one()
                event.accept()
                return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:
        """Viewport geometry changed: re-clamp pan, notify the finder."""
        super().resizeEvent(event)
        if self._zoom is not None:
            self._clamp_pan()
            self.view_changed.emit()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Handle mouse press for panning.

        Middle button (or Alt+left) always pans; plain left-drag pans
        while zoomed. ROI editing here is panel-driven, so left-drag is
        free for navigation.
        """
        begin_pan = event.button() == Qt.MouseButton.MiddleButton or (
            event.button() == Qt.MouseButton.LeftButton
            and (
                bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
                or self._zoom is not None
            )
        )
        if begin_pan:
            if hasattr(self, '_image_rect') and self._image_rect.contains(event.pos()):
                self._is_panning = True
                self._pan_start_pos = event.pos()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Handle mouse release for panning."""
        if self._is_panning and event.button() in (
            Qt.MouseButton.MiddleButton,
            Qt.MouseButton.LeftButton,
        ):
            self._is_panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseReleaseEvent(event)

    def _widget_to_image_coords(self, widget_x: float, widget_y: float) -> tuple[int, int]:
        """Convert widget coordinates to image array indices."""
        if self._display_image is None:
            return (0, 0)
        transform = self._view_transform()
        if transform is None:
            return (0, 0)
        scale, draw_x, draw_y, _, _ = transform
        img_x = int((widget_x - draw_x) / scale)
        img_y = int((widget_y - draw_y) / scale)

        # Clamp to image bounds
        if self._temperature_image is not None:
            img_x = max(0, min(img_x, self._temperature_image.shape[1] - 1))
            img_y = max(0, min(img_y, self._temperature_image.shape[0] - 1))

        return (img_x, img_y)


__all__ = ["LiveThermalWidget", "ROIOverlay"]
