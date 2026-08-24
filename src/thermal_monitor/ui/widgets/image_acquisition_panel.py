"""
ui.widgets.image_acquisition_panel -- Left sidebar panel for Image Acquisition controls.

ThermoView-style instrument panel:
- Camera identity (compact)
- Connection status with indicator
- Connect/Disconnect buttons
- Start/Stop acquisition
- Requested/Acquisition/Display FPS
- Averaging, History
- Change button
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QDoubleSpinBox,
    QComboBox,
    QFrame,
    QSizePolicy,
)

from thermal_monitor.core.models import CameraConnectionState, CameraIdentity
from thermal_monitor.ui.theme import ThemeManager


class ImageAcquisitionPanel(QWidget):
    """Left sidebar: Image Acquisition instrument panel."""

    # Signals
    connect_requested = pyqtSignal()
    disconnect_requested = pyqtSignal()
    start_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    change_requested = pyqtSignal()
    fps_changed = pyqtSignal(int)
    averaging_changed = pyqtSignal(str)
    history_changed = pyqtSignal(int)

    def __init__(self, theme_manager: Optional[ThemeManager] = None) -> None:
        super().__init__()
        self._theme = theme_manager
        self._connection_state = CameraConnectionState.DISCONNECTED
        self._acquisition_running = False
        self._selected_camera_identity: CameraIdentity | None = None

        self._setup_ui()
        self._apply_theme()
        self._update_button_states()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # --- IMAGE ACQUISITION GROUP ---
        group = QGroupBox("IMAGE ACQUISITION")
        group_layout = QVBoxLayout(group)
        group_layout.setContentsMargins(8, 12, 8, 8)
        group_layout.setSpacing(8)

        # Camera identity (compact)
        self._camera_label = QLabel("No camera selected")
        self._camera_label.setWordWrap(True)
        self._camera_label.setStyleSheet("font-weight: bold; font-size: 12px;")
        group_layout.addWidget(self._camera_label)

        # Connection status
        status_layout = QHBoxLayout()
        status_layout.setSpacing(8)

        self._status_indicator = QLabel("●")
        self._status_indicator.setFixedWidth(16)
        self._status_text = QLabel("Disconnected")
        self._status_text.setStyleSheet("font-weight: bold; font-size: 11px;")

        status_layout.addWidget(self._status_indicator)
        status_layout.addWidget(self._status_text)
        status_layout.addStretch()
        group_layout.addLayout(status_layout)

        # Connect/Disconnect buttons
        conn_btn_layout = QHBoxLayout()
        conn_btn_layout.setSpacing(6)

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.clicked.connect(self.connect_requested.emit)
        self._apply_button_style(self._connect_btn, "primary")

        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.clicked.connect(self.disconnect_requested.emit)
        self._disconnect_btn.setEnabled(False)
        self._apply_button_style(self._disconnect_btn, "secondary")

        conn_btn_layout.addWidget(self._connect_btn)
        conn_btn_layout.addWidget(self._disconnect_btn)
        group_layout.addLayout(conn_btn_layout)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        self._apply_border_style(sep)
        group_layout.addWidget(sep)

        # Acquisition controls (enabled only when connected)
        self._acq_controls = QWidget()
        acq_layout = QFormLayout(self._acq_controls)
        acq_layout.setSpacing(6)
        acq_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # Start/Stop buttons
        acq_btn_layout = QHBoxLayout()
        acq_btn_layout.setSpacing(6)

        self._start_btn = QPushButton("Start")
        self._start_btn.clicked.connect(self.start_requested.emit)
        self._apply_button_style(self._start_btn, "primary")

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.clicked.connect(self.stop_requested.emit)
        self._stop_btn.setEnabled(False)
        self._apply_button_style(self._stop_btn, "secondary")

        acq_btn_layout.addWidget(self._start_btn)
        acq_btn_layout.addWidget(self._stop_btn)
        acq_layout.addRow("", acq_btn_layout)

        # FPS controls
        self._requested_fps = QSpinBox()
        self._requested_fps.setRange(1, 60)
        self._requested_fps.setValue(9)
        self._requested_fps.setSuffix(" Hz")
        self._requested_fps.valueChanged.connect(self.fps_changed.emit)
        self._apply_input_style(self._requested_fps)
        acq_layout.addRow("Requested FPS:", self._requested_fps)

        self._acq_fps_label = QLabel("— Hz")
        self._acq_fps_label.setStyleSheet("font-family: monospace;")
        acq_layout.addRow("Acquisition FPS:", self._acq_fps_label)

        self._disp_fps_label = QLabel("— Hz")
        self._disp_fps_label.setStyleSheet("font-family: monospace;")
        acq_layout.addRow("Display FPS:", self._disp_fps_label)

        # Averaging
        self._averaging_combo = QComboBox()
        self._averaging_combo.addItems(["Off", "2", "4", "8", "16"])
        self._averaging_combo.currentTextChanged.connect(self.averaging_changed.emit)
        self._apply_input_style(self._averaging_combo)
        acq_layout.addRow("Averaging:", self._averaging_combo)

        # History
        self._history_spin = QSpinBox()
        self._history_spin.setRange(1, 1000)
        self._history_spin.setValue(100)
        self._history_spin.setSuffix(" frames")
        self._history_spin.valueChanged.connect(self.history_changed.emit)
        self._apply_input_style(self._history_spin)
        acq_layout.addRow("History:", self._history_spin)

        self._acq_controls.setEnabled(False)
        group_layout.addWidget(self._acq_controls)

        # Change button
        self._change_btn = QPushButton("Change...")
        self._change_btn.clicked.connect(self.change_requested.emit)
        self._change_btn.setEnabled(False)
        self._apply_button_style(self._change_btn, "accent")
        group_layout.addWidget(self._change_btn)

        layout.addWidget(group)

        # --- IMAGE INFO GROUP ---
        info_group = QGroupBox("IMAGE INFO")
        info_layout = QFormLayout(info_group)
        info_layout.setSpacing(4)
        info_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        info_layout.setContentsMargins(8, 12, 8, 8)

        self._info_camera = QLabel("—")
        self._info_serial = QLabel("—")
        self._info_size = QLabel("—")
        self._info_frame = QLabel("—")
        self._info_timestamp = QLabel("—")
        self._info_calibration = QLabel("—")
        self._info_emissivity = QLabel("—")
        self._info_ambient = QLabel("—")
        self._info_processing = QLabel("—")

        for label in [
            self._info_camera, self._info_serial, self._info_size,
            self._info_frame, self._info_timestamp, self._info_calibration,
            self._info_emissivity, self._info_ambient, self._info_processing
        ]:
            label.setStyleSheet("font-family: monospace; font-size: 10px;")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        info_layout.addRow("Camera:", self._info_camera)
        info_layout.addRow("Serial:", self._info_serial)
        info_layout.addRow("Image Size:", self._info_size)
        info_layout.addRow("Frame:", self._info_frame)
        info_layout.addRow("Timestamp:", self._info_timestamp)
        info_layout.addRow("Calibration:", self._info_calibration)
        info_layout.addRow("Emissivity:", self._info_emissivity)
        info_layout.addRow("Ambient:", self._info_ambient)
        info_layout.addRow("Processing:", self._info_processing)

        layout.addWidget(info_group)

        layout.addStretch()

        # Set fixed width for left panel
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setMinimumWidth(260)
        self.setMaximumWidth(300)

    def _apply_theme(self) -> None:
        if not self._theme:
            return
        colors = self._theme.colors()
        self.setStyleSheet(f"""
            QGroupBox {{
                border: 1px solid {colors.border};
                border-radius: 4px;
                margin-top: 8px;
                padding-top: 8px;
                font-weight: bold;
                font-size: 10px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
                color: {colors.text_secondary};
            }}
            QLabel {{
                color: {colors.text_primary};
            }}
        """)

    def _apply_button_style(self, btn: QPushButton, style: str) -> None:
        if not self._theme:
            return
        colors = self._theme.colors()
        if style == "primary":
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {colors.accent};
                    color: white;
                    border: none;
                    border-radius: 4px;
                    padding: 6px 16px;
                    font-weight: bold;
                    font-size: 11px;
                }}
                QPushButton:hover {{ background-color: {colors.accent_hover}; }}
                QPushButton:disabled {{ background-color: {colors.disabled}; color: {colors.primary_disabled_text}; }}
            """)
        elif style == "secondary":
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {colors.panel};
                    color: {colors.text_primary};
                    border: 1px solid {colors.border};
                    border-radius: 4px;
                    padding: 6px 16px;
                    font-size: 11px;
                }}
                QPushButton:hover {{ background-color: {colors.secondary_hover}; }}
                QPushButton:disabled {{ background-color: {colors.background}; color: {colors.secondary_disabled_text}; }}
            """)
        elif style == "accent":
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: transparent;
                    color: {colors.accent};
                    border: 1px solid {colors.accent};
                    border-radius: 4px;
                    padding: 6px 16px;
                    font-size: 11px;
                }}
                QPushButton:hover {{ background-color: {colors.accent}; color: white; }}
                QPushButton:disabled {{ background-color: transparent; color: {colors.disabled}; border-color: {colors.disabled}; }}
            """)

    def _apply_input_style(self, widget) -> None:
        if self._theme:
            colors = self._theme.colors()
            widget.setStyleSheet(f"""
                QSpinBox, QDoubleSpinBox, QComboBox {{
                    background-color: {colors.background};
                    color: {colors.text_primary};
                    border: 1px solid {colors.border};
                    border-radius: 3px;
                    padding: 4px 8px;
                    font-size: 11px;
                }}
                QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
                    border-color: {colors.accent};
                }}
                QComboBox::drop-down {{
                    border: none;
                    width: 20px;
                }}
                QComboBox::down-arrow {{
                    image: none;
                    border-left: 4px solid transparent;
                    border-right: 4px solid transparent;
                    border-top: 5px solid {colors.text_primary};
                    margin-right: 8px;
                }}
            """)

    def _apply_border_style(self, widget) -> None:
        if self._theme:
            widget.setStyleSheet(f"border-color: {self._theme.colors().border};")

    def _update_button_states(self) -> None:
        connected = self._connection_state in (CameraConnectionState.CONNECTED, CameraConnectionState.ACQUIRING)
        acquiring = self._connection_state == CameraConnectionState.ACQUIRING

        self._connect_btn.setEnabled(not connected and self._connection_state != CameraConnectionState.CONNECTING)
        self._disconnect_btn.setEnabled(connected)
        self._start_btn.setEnabled(connected and not acquiring)
        self._stop_btn.setEnabled(acquiring)
        self._change_btn.setEnabled(connected)
        self._acq_controls.setEnabled(connected)

    # Public API

    def set_camera_identity(self, identity: CameraIdentity | None) -> None:
        """Set the camera identity display."""
        self._selected_camera_identity = identity
        if identity:
            self._camera_label.setText(f"{identity.model or 'TV46L'}-{identity.serial_number}")
            self._info_camera.setText(identity.model or "TV46L")
            self._info_serial.setText(identity.serial_number)
        else:
            self._camera_label.setText("No camera selected")
            self._info_camera.setText("—")
            self._info_serial.setText("—")

    def set_connection_state(self, state: CameraConnectionState) -> None:
        """Update connection state and UI."""
        self._connection_state = state

        if not self._theme:
            return

        colors = self._theme.colors()
        state_colors = {
            CameraConnectionState.DISCONNECTED: colors.disabled,
            CameraConnectionState.CONNECTING: colors.info,
            CameraConnectionState.CONNECTED: colors.success,
            CameraConnectionState.ACQUIRING: colors.success,
            CameraConnectionState.DEGRADED: colors.warning,
            CameraConnectionState.RECONNECTING: colors.warning,
            CameraConnectionState.ERROR: colors.danger,
        }

        color = state_colors.get(state, colors.disabled)
        status_text = state.value.replace("_", " ").title()

        self._status_indicator.setStyleSheet(f"color: {color}; font-size: 14px;")
        self._status_text.setText(status_text)
        self._status_text.setStyleSheet(f"font-weight: bold; font-size: 11px; color: {color};")

        self._update_button_states()

    def set_acquisition_running(self, running: bool) -> None:
        """Update acquisition running state."""
        self._acquisition_running = running
        self._start_btn.setEnabled(not running and self._connection_state == CameraConnectionState.CONNECTED)
        self._stop_btn.setEnabled(running)

    def set_acquisition_fps(self, fps: float | None) -> None:
        """Update acquisition FPS display."""
        if fps is not None:
            self._acq_fps_label.setText(f"{fps:.1f} Hz")
        else:
            self._acq_fps_label.setText("— Hz")

    def set_display_fps(self, fps: float | None) -> None:
        """Update display FPS display."""
        if fps is not None:
            self._disp_fps_label.setText(f"{fps:.1f} Hz")
        else:
            self._disp_fps_label.setText("— Hz")

    def set_requested_fps(self, fps: int) -> None:
        """Set requested FPS spinbox."""
        self._requested_fps.setValue(fps)

    def get_requested_fps(self) -> int:
        return self._requested_fps.value()

    def set_averaging(self, value: str) -> None:
        idx = self._averaging_combo.findText(value)
        if idx >= 0:
            self._averaging_combo.setCurrentIndex(idx)

    def set_history(self, frames: int) -> None:
        self._history_spin.setValue(frames)

    def update_image_info(
        self,
        image_size: str | None = None,
        frame: str | None = None,
        timestamp: str | None = None,
        calibration: str | None = None,
        emissivity: str | None = None,
        ambient: str | None = None,
        processing: str | None = None,
    ) -> None:
        """Update image info fields."""
        if image_size is not None:
            self._info_size.setText(image_size)
        if frame is not None:
            self._info_frame.setText(frame)
        if timestamp is not None:
            self._info_timestamp.setText(timestamp)
        if calibration is not None:
            self._info_calibration.setText(calibration)
        if emissivity is not None:
            self._info_emissivity.setText(emissivity)
        if ambient is not None:
            self._info_ambient.setText(ambient)
        if processing is not None:
            self._info_processing.setText(processing)

    def clear_image_info(self) -> None:
        """Clear all image info fields."""
        for label in [
            self._info_size, self._info_frame, self._info_timestamp,
            self._info_calibration, self._info_emissivity, self._info_ambient,
            self._info_processing
        ]:
            label.setText("—")


__all__ = ["ImageAcquisitionPanel"]