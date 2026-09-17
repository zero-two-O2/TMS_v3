"""
ui.widgets.ptz_control_panel -- PTZ Control shelf panel (Phase 6).

Service-agnostic display/control surface: emits movement requests, renders
PtzStatus snapshots and PtzOperation updates. Never touches OPC UA, the
service, or the database; the parent widget owns all I/O on background
threads. All button enablement derives from authoritative status.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from thermal_monitor.ptz.controller import PtzOperation, PtzOperationState
from thermal_monitor.ptz.state import PtzStatus
from thermal_monitor.ui.theme import ThemeManager
from thermal_monitor.ui.theme.properties import set_variant


class PtzControlPanel(QWidget):
    """PTZ Control panel: status, absolute/relative movement, calibration."""

    move_requested = pyqtSignal(float, float, float, str)  # pan, tilt, velocity, mode
    relative_requested = pyqtSignal(float, float, float)  # dpan, dtilt, velocity
    stop_requested = pyqtSignal()
    clear_error_requested = pyqtSignal()
    calibration_requested = pyqtSignal()

    def __init__(self, theme_manager: Optional[ThemeManager] = None) -> None:
        super().__init__()
        self._theme = theme_manager
        self._camera_id: str | None = None
        self._ptz_id: str | None = None
        self._setup_ui()
        self.clear()

    def _setup_ui(self) -> None:
        from thermal_monitor.ui.theme.tokens import metrics_for

        m = metrics_for(self._theme)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(m.panel_spacing)

        # Connection/status group
        status_group = QGroupBox("Connection")
        status_layout = QFormLayout(status_group)
        self._plc_label = QLabel("—")
        self._ptz_label = QLabel("—")
        self._state_label = QLabel("—")
        status_layout.addRow("PLC:", self._plc_label)
        status_layout.addRow("PTZ:", self._ptz_label)
        status_layout.addRow("State:", self._state_label)
        layout.addWidget(status_group)

        # Position group
        pos_group = QGroupBox("Position")
        pos_layout = QFormLayout(pos_group)
        self._actual_label = QLabel("—")
        self._target_label = QLabel("—")
        self._reached_label = QLabel("—")
        pos_layout.addRow("Actual:", self._actual_label)
        pos_layout.addRow("Target:", self._target_label)
        pos_layout.addRow("Reached:", self._reached_label)
        layout.addWidget(pos_group)

        # Absolute movement group
        abs_group = QGroupBox("Absolute Move")
        abs_layout = QFormLayout(abs_group)
        self._pan_spin = QDoubleSpinBox()
        self._pan_spin.setRange(-360.0, 360.0)
        self._pan_spin.setDecimals(1)
        self._pan_spin.setSuffix("°")
        self._tilt_spin = QDoubleSpinBox()
        self._tilt_spin.setRange(-360.0, 360.0)
        self._tilt_spin.setDecimals(1)
        self._tilt_spin.setSuffix("°")
        self._vel_spin = QDoubleSpinBox()
        self._vel_spin.setRange(0.1, 360.0)
        self._vel_spin.setDecimals(1)
        self._vel_spin.setValue(10.0)
        self._vel_spin.setSuffix("°/s")
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["single", "per_axis"])
        self._go_btn = QPushButton("Go")
        self._go_btn.clicked.connect(self._on_go)
        abs_layout.addRow("Pan:", self._pan_spin)
        abs_layout.addRow("Tilt:", self._tilt_spin)
        abs_layout.addRow("Velocity:", self._vel_spin)
        abs_layout.addRow("Mode:", self._mode_combo)
        abs_layout.addRow(self._go_btn)
        layout.addWidget(abs_group)

        # Incremental movement group
        inc_group = QGroupBox("Incremental Move")
        inc_layout = QVBoxLayout(inc_group)
        step_row = QHBoxLayout()
        self._step_combo = QComboBox()
        self._step_combo.addItems(["1°", "5°", "10°"])
        self._step_combo.setCurrentIndex(1)
        step_row.addWidget(QLabel("Step:"))
        step_row.addWidget(self._step_combo, 1)
        inc_layout.addLayout(step_row)
        pad = QGridLayout()
        self._up_btn = QPushButton("↑")
        self._down_btn = QPushButton("↓")
        self._left_btn = QPushButton("←")
        self._right_btn = QPushButton("→")
        self._up_btn.clicked.connect(lambda: self._on_step(0.0, 1.0))
        self._down_btn.clicked.connect(lambda: self._on_step(0.0, -1.0))
        self._left_btn.clicked.connect(lambda: self._on_step(-1.0, 0.0))
        self._right_btn.clicked.connect(lambda: self._on_step(1.0, 0.0))
        pad.addWidget(self._up_btn, 0, 1)
        pad.addWidget(self._left_btn, 1, 0)
        pad.addWidget(self._right_btn, 1, 2)
        pad.addWidget(self._down_btn, 2, 1)
        inc_layout.addLayout(pad)
        layout.addWidget(inc_group)

        # Actions group
        actions_group = QGroupBox("Actions")
        actions_layout = QVBoxLayout(actions_group)
        self._stop_btn = QPushButton("STOP")
        self._stop_btn.clicked.connect(self.stop_requested.emit)
        self._clear_btn = QPushButton("Clear Error")
        self._clear_btn.clicked.connect(self.clear_error_requested.emit)
        self._cal_btn = QPushButton("Calibrate")
        self._cal_btn.clicked.connect(self.calibration_requested.emit)
        actions_layout.addWidget(self._stop_btn)
        actions_layout.addWidget(self._clear_btn)
        actions_layout.addWidget(self._cal_btn)
        layout.addWidget(actions_group)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        layout.addWidget(self._error_label)
        layout.addStretch(1)

        for btn, style in (
            (self._go_btn, "accent"),
            (self._stop_btn, "danger"),
            (self._clear_btn, "outline"),
            (self._cal_btn, "secondary"),
            (self._up_btn, "outline"),
            (self._down_btn, "outline"),
            (self._left_btn, "outline"),
            (self._right_btn, "outline"),
        ):
            set_variant(btn, style)

    # -- events ------------------------------------------------------------

    def _on_go(self) -> None:
        self.move_requested.emit(
            float(self._pan_spin.value()),
            float(self._tilt_spin.value()),
            float(self._vel_spin.value()),
            self._mode_combo.currentText(),
        )

    def _on_step(self, pan_sign: float, tilt_sign: float) -> None:
        step = float(self._step_combo.currentText().rstrip("°"))
        self.relative_requested.emit(
            pan_sign * step, tilt_sign * step, float(self._vel_spin.value())
        )

    # -- public API ----------------------------------------------------------

    def set_binding(
        self, camera_id: str | None, ptz_id: str | None, endpoint_configured: bool
    ) -> None:
        """Show which station this panel follows (no stale data)."""
        self._camera_id = camera_id
        self._ptz_id = ptz_id
        if camera_id is None:
            self._ptz_label.setText("No camera selected")
        elif ptz_id is None:
            self._ptz_label.setText("No PTZ configured")
        else:
            suffix = "" if endpoint_configured else " (no endpoint)"
            self._ptz_label.setText(f"{ptz_id}{suffix}")
        self._refresh_buttons(None)

    def set_status(self, status: PtzStatus | None) -> None:
        """Render an authoritative status snapshot."""
        if status is None:
            self._plc_label.setText("—")
            self._state_label.setText("—")
            self._actual_label.setText("—")
            self._reached_label.setText("—")
            self._refresh_buttons(None)
            return
        self._plc_label.setText(status.plc_state.value)
        self._actual_label.setText(
            f"{status.actual_pan:.1f}°, {status.actual_tilt:.1f}°"
        )
        if status.error is not None:
            state = f"ERROR: {status.error.message}"
        elif status.calibration.value == "active":
            state = "Calibrating…"
        elif status.moving:
            state = "Moving…"
        elif status.position_reached:
            state = "Position reached"
        elif not status.communication_ok:
            state = "Communication lost"
        elif not status.ready:
            state = "Not ready"
        else:
            state = "Ready"
        self._state_label.setText(state)
        self._reached_label.setText("Yes" if status.position_reached else "No")
        self._error_label.setText(
            status.error.message if status.error is not None else ""
        )
        self._refresh_buttons(status)

    def set_operation(self, operation: PtzOperation | None) -> None:
        """Render the latest operation snapshot (target + phase)."""
        if operation is None:
            self._target_label.setText("—")
            return
        phase = operation.state.value
        self._target_label.setText(
            f"{operation.target_pan:.1f}°, {operation.target_tilt:.1f}° ({phase})"
        )
        if operation.state == PtzOperationState.FAILED and operation.error is not None:
            self._error_label.setText(operation.error.message)

    def show_message(self, message: str) -> None:
        self._error_label.setText(message)

    def clear(self) -> None:
        """Drop all station state (camera switch/disconnect safety)."""
        self._camera_id = None
        self._ptz_id = None
        self._ptz_label.setText("—")
        self._plc_label.setText("—")
        self._state_label.setText("—")
        self._actual_label.setText("—")
        self._target_label.setText("—")
        self._reached_label.setText("—")
        self._error_label.setText("")
        self._refresh_buttons(None)

    def _refresh_buttons(self, status: PtzStatus | None) -> None:
        can_command = bool(
            status is not None
            and status.accepts_commands
            and self._ptz_id is not None
        )
        moving = bool(status is not None and status.moving)
        has_error = bool(status is not None and status.error is not None)
        self._go_btn.setEnabled(can_command)
        self._up_btn.setEnabled(can_command)
        self._down_btn.setEnabled(can_command)
        self._left_btn.setEnabled(can_command)
        self._right_btn.setEnabled(can_command)
        self._pan_spin.setEnabled(can_command)
        self._tilt_spin.setEnabled(can_command)
        self._vel_spin.setEnabled(can_command)
        self._mode_combo.setEnabled(can_command)
        self._stop_btn.setEnabled(moving)
        self._clear_btn.setEnabled(has_error)
        self._cal_btn.setEnabled(
            bool(
                status is not None
                and status.plc_connected
                and status.ptz_available
                and status.communication_ok
                and status.error is None
                and status.calibration.value != "active"
                and self._ptz_id is not None
            )
        )
