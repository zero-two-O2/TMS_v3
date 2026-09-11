"""
ui.widgets.frame_info_panel -- Frame metadata and image information panel.

Shows camera info, frame details, calibration data, and processing statistics.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QGroupBox,
    QFormLayout,
    QLabel,
    QFrame,
)

from thermal_monitor.ui.theme import ThemeManager
from thermal_monitor.ui.theme.properties import set_role


class FrameInfoPanel(QWidget):
    """Frame information panel - shows metadata about the current frame."""

    def __init__(self, theme_manager: Optional[ThemeManager] = None) -> None:
        super().__init__()
        self._theme = theme_manager
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        frame_group = QGroupBox("Frame Information")
        frame_form = QFormLayout(frame_group)
        frame_form.setSpacing(4)
        frame_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # Camera/Frame info
        self._info_camera_id = QLabel("—")
        self._info_frame_size = QLabel("—")
        self._info_acq_fps = QLabel("—")
        self._info_disp_fps = QLabel("—")
        self._info_sequence = QLabel("—")
        self._info_timestamp = QLabel("—")

        # Calibration info
        self._info_cal_range = QLabel("—")
        self._info_emissivity = QLabel("—")
        self._info_ambient = QLabel("—")

        # Processing info
        self._info_proc_time = QLabel("—")

        labels = [
            self._info_camera_id,
            self._info_frame_size,
            self._info_acq_fps,
            self._info_disp_fps,
            self._info_sequence,
            self._info_timestamp,
            self._info_cal_range,
            self._info_emissivity,
            self._info_ambient,
            self._info_proc_time,
        ]

        for label in labels:
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            set_role(label, "mono")

        frame_form.addRow("Camera ID:", self._info_camera_id)
        frame_form.addRow("Frame Size:", self._info_frame_size)
        frame_form.addRow("Acq FPS:", self._info_acq_fps)
        frame_form.addRow("Disp FPS:", self._info_disp_fps)
        frame_form.addRow("Sequence:", self._info_sequence)
        frame_form.addRow("Timestamp:", self._info_timestamp)
        frame_form.addRow("Cal Range:", self._info_cal_range)
        frame_form.addRow("Emissivity:", self._info_emissivity)
        frame_form.addRow("Ambient:", self._info_ambient)
        frame_form.addRow("Proc Time:", self._info_proc_time)

        layout.addWidget(frame_group)
        layout.addStretch()

    # Public API

    def update_from_frame(self, frame, result=None) -> None:
        """Update panel from frame descriptor and processing result."""
        if frame is not None:
            desc = frame.descriptor
            self._info_camera_id.setText(desc.camera_id)
            self._info_frame_size.setText(f"{desc.thermal.width}×{desc.thermal.height}")
            self._info_sequence.setText(str(desc.sequence))
            self._info_timestamp.setText(f"{desc.timestamp:.3f}")

        if result is not None:
            self._info_proc_time.setText(f"{result.processing_time_ms:.1f} ms")

    def set_acquisition_fps(self, fps: float | None) -> None:
        if fps is not None:
            self._info_acq_fps.setText(f"{fps:.1f}")
        else:
            self._info_acq_fps.setText("—")

    def set_display_fps(self, fps: float | None) -> None:
        if fps is not None:
            self._info_disp_fps.setText(f"{fps:.1f}")
        else:
            self._info_disp_fps.setText("—")

    def set_calibration_info(self, cal_range: str | None, emissivity: float | None, ambient: float | None) -> None:
        if cal_range is not None:
            self._info_cal_range.setText(cal_range)
        if emissivity is not None:
            self._info_emissivity.setText(f"{emissivity:.2f}")
        if ambient is not None:
            self._info_ambient.setText(f"{ambient:.1f} °C")

    def clear(self) -> None:
        for label in [
            self._info_camera_id,
            self._info_frame_size,
            self._info_acq_fps,
            self._info_disp_fps,
            self._info_sequence,
            self._info_timestamp,
            self._info_cal_range,
            self._info_emissivity,
            self._info_ambient,
            self._info_proc_time,
        ]:
            label.setText("—")


__all__ = ["FrameInfoPanel"]