"""
ui.widgets.image_acquisition_panel -- Camera Control operational panel.

Clean operational controls only (ThermoView-style):
- Configured-camera selector
- Camera identity + connection status
- Connect / Disconnect (Connect opens the Acquisition Setup dialog)
- Feed display selection (IR / IR+VL / VL, display only)
- Start / Stop acquisition
- Focus, NUC

Startup acquisition parameters (FPS, averaging, history) live in the
Acquisition Setup dialog, and image metadata lives in the dedicated
Image Information side panel — neither is duplicated here.
"""

from __future__ import annotations

import logging
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
    QComboBox,
    QFrame,
    QSizePolicy,
)

from thermal_monitor.core.models import CameraConnectionState, CameraIdentity
from thermal_monitor.ui.theme import ThemeManager
from thermal_monitor.ui.theme.properties import set_role, set_status, set_variant


logger = logging.getLogger(__name__)


#: Historic local button-style names mapped onto global semantic variants.
_BUTTON_VARIANT_MAP = {"primary": "accent", "secondary": "outline", "accent": "ghost"}

_CONNECTION_STATUS_MAP = {
    CameraConnectionState.DISCONNECTED: "disconnected",
    CameraConnectionState.CONNECTING: "connecting",
    CameraConnectionState.CONNECTED: "connected",
    CameraConnectionState.STARTING: "connecting",
    CameraConnectionState.ACQUIRING: "acquiring",
    CameraConnectionState.STOPPING: "connecting",
    CameraConnectionState.DISCONNECTING: "connecting",
    CameraConnectionState.DEGRADED: "degraded",
    CameraConnectionState.RECONNECTING: "connecting",
    CameraConnectionState.ERROR: "error",
}


class ImageAcquisitionPanel(QWidget):
    """Left sidebar: Image Acquisition instrument panel."""

    # Signals
    connect_requested = pyqtSignal()
    disconnect_requested = pyqtSignal()
    start_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    focus_set_requested = pyqtSignal(int)
    focus_refresh_requested = pyqtSignal()
    nuc_requested = pyqtSignal()
    camera_selection_changed = pyqtSignal(str)  # camera_id
    feed_mode_changed = pyqtSignal(str)  # "ir" | "both" | "vl" (display only)

    def __init__(self, theme_manager: Optional[ThemeManager] = None) -> None:
        super().__init__()
        self._theme = theme_manager
        self._connection_state = CameraConnectionState.DISCONNECTED
        self._acquisition_running = False
        self._selected_camera_identity: CameraIdentity | None = None

        self._setup_ui()
        self._apply_theme()
        self._update_button_states()
        logger.debug("Panel constructed id=%r theme=%r", id(self), theme_manager)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # --- CAMERA SELECTION GROUP (sole camera selector in Config mode) ---
        select_group = QGroupBox("CAMERA")
        select_layout = QVBoxLayout(select_group)
        select_layout.setContentsMargins(8, 12, 8, 8)
        select_layout.setSpacing(6)

        self._camera_combo = QComboBox()
        self._camera_combo.setMinimumHeight(24)
        self._camera_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._camera_combo.currentIndexChanged.connect(self._on_camera_combo_changed)
        self._apply_input_style(self._camera_combo)
        select_layout.addWidget(self._camera_combo)
        layout.addWidget(select_group)

        # --- IMAGE ACQUISITION GROUP ---
        group = QGroupBox("IMAGE ACQUISITION")
        group_layout = QVBoxLayout(group)
        group_layout.setContentsMargins(8, 12, 8, 8)
        group_layout.setSpacing(8)

        # Camera identity (compact)
        self._camera_label = QLabel("No camera selected")
        self._camera_label.setWordWrap(True)
        set_role(self._camera_label, "strong")
        group_layout.addWidget(self._camera_label)

        # Connection status
        status_layout = QHBoxLayout()
        status_layout.setSpacing(8)

        self._status_indicator = QLabel("●")
        self._status_indicator.setFixedWidth(16)
        self._status_text = QLabel("Disconnected")
        set_status(self._status_text, "disconnected")

        status_layout.addWidget(self._status_indicator)
        status_layout.addWidget(self._status_text)
        status_layout.addStretch()
        group_layout.addLayout(status_layout)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        self._apply_border_style(sep)
        group_layout.addWidget(sep)

        # Connection controls (Connect/Disconnect)
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
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        self._apply_border_style(sep2)
        group_layout.addWidget(sep2)

        # Separator
        sep3 = QFrame()
        sep3.setFrameShape(QFrame.Shape.HLine)
        sep3.setFrameShadow(QFrame.Shadow.Sunken)
        self._apply_border_style(sep3)
        group_layout.addWidget(sep3)

        # Start/Stop acquisition (enabled only when connected)
        self._run_controls = QWidget()
        run_layout = QHBoxLayout(self._run_controls)
        run_layout.setContentsMargins(0, 0, 0, 0)
        run_layout.setSpacing(6)

        self._start_btn = QPushButton("Start")
        self._start_btn.clicked.connect(self.start_requested.emit)
        self._apply_button_style(self._start_btn, "primary")

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.clicked.connect(self.stop_requested.emit)
        self._stop_btn.setEnabled(False)
        self._apply_button_style(self._stop_btn, "secondary")

        run_layout.addWidget(self._start_btn)
        run_layout.addWidget(self._stop_btn)
        self._run_controls.setEnabled(False)
        group_layout.addWidget(self._run_controls)

        layout.addWidget(group)

        # --- FEED GROUP (display only: IR / IR+VL / VL) ---
        feed_group = QGroupBox("FEED")
        feed_layout = QHBoxLayout(feed_group)
        feed_layout.setContentsMargins(8, 12, 8, 8)
        feed_layout.setSpacing(4)
        self._feed_buttons: dict[str, QPushButton] = {}
        for mode, text, tip in (
            ("ir", "IR", "Infrared feed only (display)"),
            ("both", "IR + VL", "Infrared and visible side by side (display)"),
            ("vl", "VL", "Visible-light feed only (display)"),
        ):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setToolTip(tip)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setMinimumHeight(24)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._apply_button_style(button, "secondary")
            button.clicked.connect(
                lambda _checked=False, feed_mode=mode: self._on_feed_button(feed_mode)
            )
            feed_layout.addWidget(button)
            self._feed_buttons[mode] = button
        self._feed_mode = "both"
        self._sync_feed_buttons()
        layout.addWidget(feed_group)

        # --- FOCUS GROUP (Stage 8D: custom-backend focus, async via window) ---
        focus_group = QGroupBox("FOCUS")
        focus_layout = QFormLayout(focus_group)
        focus_layout.setSpacing(6)
        focus_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        focus_layout.setContentsMargins(8, 12, 8, 8)

        self._focus_current_label = QLabel("— mm")
        set_role(self._focus_current_label, "mono")
        focus_layout.addRow("Current:", self._focus_current_label)

        self._focus_range_label = QLabel("—")
        set_role(self._focus_range_label, "mono")
        focus_layout.addRow("Range:", self._focus_range_label)

        focus_row = QHBoxLayout()
        focus_row.setSpacing(6)
        self._focus_spin = QSpinBox()
        self._focus_spin.setRange(1, 1000000)
        self._focus_spin.setSuffix(" mm")
        self._apply_input_style(self._focus_spin)
        self._focus_apply_btn = QPushButton("Apply")
        self._focus_apply_btn.setObjectName("focusApplyButton")
        self._focus_apply_btn.clicked.connect(
            lambda: self.focus_set_requested.emit(self._focus_spin.value())
        )
        logger.info(
            "APPLY BUTTON CREATED name=%s id=%r",
            self._focus_apply_btn.objectName(),
            id(self._focus_apply_btn),
        )
        self._apply_button_style(self._focus_apply_btn, "primary")
        focus_row.addWidget(self._focus_spin, 1)
        focus_row.addWidget(self._focus_apply_btn)
        focus_layout.addRow("Set:", focus_row)

        focus_btn_row = QHBoxLayout()
        focus_btn_row.setSpacing(6)
        self._focus_refresh_btn = QPushButton("Read")
        self._focus_refresh_btn.setObjectName("focusReadButton")
        self._focus_refresh_btn.clicked.connect(self.focus_refresh_requested.emit)
        self._apply_button_style(self._focus_refresh_btn, "secondary")
        focus_btn_row.addWidget(self._focus_refresh_btn)
        focus_btn_row.addStretch()
        focus_layout.addRow("", focus_btn_row)

        self._focus_status_label = QLabel("Focus unavailable")
        set_role(self._focus_status_label, "mono")
        self._focus_status_label.setWordWrap(True)
        focus_layout.addRow("Status:", self._focus_status_label)

        self._focus_group = focus_group
        self._focus_group.setEnabled(False)
        layout.addWidget(focus_group)

        # --- NUC GROUP (Stage 8G: custom-path production NUC, async via window) ---
        nuc_group = QGroupBox("NUC")
        nuc_layout = QVBoxLayout(nuc_group)
        nuc_layout.setSpacing(6)
        nuc_layout.setContentsMargins(8, 12, 8, 8)

        self._nuc_button = QPushButton("Execute NUC")
        self._nuc_button.clicked.connect(self.nuc_requested.emit)
        self._apply_button_style(self._nuc_button, "primary")
        nuc_layout.addWidget(self._nuc_button)

        self._nuc_status_label = QLabel("NUC unavailable")
        set_role(self._nuc_status_label, "mono")
        self._nuc_status_label.setWordWrap(True)
        nuc_layout.addWidget(self._nuc_status_label)

        self._nuc_group = nuc_group
        self._nuc_group.setEnabled(False)
        layout.addWidget(nuc_group)

        # NOTE: image metadata lives ONLY in the dedicated Image
        # Information side panel (FrameInfoPanel) — never duplicated here.
        layout.addStretch()

        # Scroll-friendly sizing: readable minimum, never forces horizontal
        # scroll inside the side-panel scroll area.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(220)

    def _apply_theme(self) -> None:
        # Group boxes, labels, and inputs are styled centrally; nothing
        # per-widget to do.
        return

    def _apply_button_style(self, btn: QPushButton, style: str) -> None:
        # Kept for call-site compatibility: maps historic local style
        # names onto the global semantic button variants.
        set_variant(btn, _BUTTON_VARIANT_MAP.get(style, "outline"))

    def _apply_input_style(self, widget) -> None:
        # Inputs are styled centrally; nothing per-widget to do.
        return

    def _apply_border_style(self, widget) -> None:
        # Separators inherit the central QFrame border color.
        return

    def _update_button_states(self) -> None:
        connected = self._connection_state in (CameraConnectionState.CONNECTED, CameraConnectionState.ACQUIRING)
        acquiring = self._connection_state == CameraConnectionState.ACQUIRING
        connecting = self._connection_state == CameraConnectionState.CONNECTING

        # DISCONNECTED: Connect=ENABLED, Disconnect=DISABLED, Start=DISABLED, Stop=DISABLED
        # CONNECTING: all DISABLED (bounded background connect owns the camera)
        # CONNECTED/IDLE: Connect=DISABLED, Disconnect=ENABLED, Start=ENABLED, Stop=DISABLED
        # STARTING: Connect/Start/Stop=DISABLED, Disconnect=ENABLED (cancels into safe shutdown)
        # ACQUIRING: Connect=DISABLED, Disconnect=ENABLED (auto-stops), Start=DISABLED, Stop=ENABLED
        # STOPPING: Connect/Start/Stop=DISABLED, Disconnect=ENABLED (queues after stop)
        # DISCONNECTING: all DISABLED (bounded background teardown owns the camera)
        # ERROR/DEGRADED/RECONNECTING: Disconnect=ENABLED (always a path back), Start per state

        if self._connection_state == CameraConnectionState.DISCONNECTED:
            self._connect_btn.setEnabled(True)
            self._disconnect_btn.setEnabled(False)
            self._start_btn.setEnabled(False)
            self._stop_btn.setEnabled(False)
        elif self._connection_state in (
            CameraConnectionState.CONNECTING,
            CameraConnectionState.DISCONNECTING,
        ):
            self._connect_btn.setEnabled(False)
            self._disconnect_btn.setEnabled(False)
            self._start_btn.setEnabled(False)
            self._stop_btn.setEnabled(False)
        elif self._connection_state == CameraConnectionState.CONNECTED:
            self._connect_btn.setEnabled(False)
            self._disconnect_btn.setEnabled(True)
            self._start_btn.setEnabled(True)
            self._stop_btn.setEnabled(False)
        elif self._connection_state == CameraConnectionState.STARTING:
            self._connect_btn.setEnabled(False)
            self._disconnect_btn.setEnabled(True)
            self._start_btn.setEnabled(False)
            self._stop_btn.setEnabled(False)
        elif self._connection_state == CameraConnectionState.ACQUIRING:
            self._connect_btn.setEnabled(False)
            self._disconnect_btn.setEnabled(True)
            self._start_btn.setEnabled(False)
            self._stop_btn.setEnabled(True)
        elif self._connection_state == CameraConnectionState.STOPPING:
            self._connect_btn.setEnabled(False)
            self._disconnect_btn.setEnabled(True)
            self._start_btn.setEnabled(False)
            self._stop_btn.setEnabled(False)
        else:  # ERROR, DEGRADED, RECONNECTING
            self._connect_btn.setEnabled(False)
            self._disconnect_btn.setEnabled(True)
            self._start_btn.setEnabled(False)
            self._stop_btn.setEnabled(False)

        self._run_controls.setEnabled(connected)

    # Public API

    def _on_camera_combo_changed(self, index: int) -> None:
        camera_id = self._camera_combo.itemData(index)
        if camera_id:
            self.camera_selection_changed.emit(camera_id)

    def set_camera_list(
        self, cameras: list[tuple[str, str, CameraIdentity | None, bool]]
    ) -> None:
        """Replace the camera list silently (no phantom selection emit).

        Exactly one deliberate emit happens, and only when the effective
        selection genuinely changed — same contract the old top toolbar
        provided, now owned by Camera Control.
        """
        combo = self._camera_combo
        current_id = combo.currentData()
        combo.blockSignals(True)
        try:
            combo.clear()
            for camera_id, display_name, _identity, _enabled in cameras:
                combo.addItem(display_name, camera_id)
            restored_idx = -1
            if current_id:
                restored_idx = combo.findData(current_id)
            if restored_idx < 0 and cameras:
                restored_idx = 0
            if restored_idx >= 0:
                combo.setCurrentIndex(restored_idx)
        finally:
            combo.blockSignals(False)
        new_id = combo.currentData()
        if new_id != current_id and new_id:
            self._on_camera_combo_changed(combo.currentIndex())

    def select_camera_by_id(self, camera_id: str) -> bool:
        """User-equivalent selection (emits when the selection changes)."""
        idx = self._camera_combo.findData(camera_id)
        if idx >= 0:
            self._camera_combo.setCurrentIndex(idx)
            return True
        return False

    def sync_camera_selection(self, camera_id: str) -> None:
        """Silent authority sync: move the combo without emitting."""
        self._camera_combo.blockSignals(True)
        try:
            self.select_camera_by_id_no_emit(camera_id)
        finally:
            self._camera_combo.blockSignals(False)

    def select_camera_by_id_no_emit(self, camera_id: str) -> bool:
        idx = self._camera_combo.findData(camera_id)
        if idx >= 0:
            self._camera_combo.blockSignals(True)
            try:
                self._camera_combo.setCurrentIndex(idx)
            finally:
                self._camera_combo.blockSignals(False)
            return True
        return False

    @property
    def selected_combo_camera_id(self) -> str | None:
        try:
            return self._camera_combo.currentData()
        except Exception:
            return None

    def _on_feed_button(self, mode: str) -> None:
        if mode == self._feed_mode:
            self._sync_feed_buttons()
            return
        self.feed_mode_changed.emit(mode)

    def set_feed_mode(self, mode: str) -> None:
        """Reflect the workspace feed mode (display only, no acquire touch)."""
        if mode not in ("ir", "both", "vl"):
            return
        self._feed_mode = mode
        self._sync_feed_buttons()

    def _sync_feed_buttons(self) -> None:
        for mode, button in getattr(self, "_feed_buttons", {}).items():
            active = mode == getattr(self, "_feed_mode", "both")
            button.setChecked(active)
            self._apply_button_style(button, "primary" if active else "secondary")

    @property
    def feed_mode(self) -> str:
        return getattr(self, "_feed_mode", "both")

    def set_camera_identity(self, identity: CameraIdentity | None) -> None:
        """Set the camera identity display."""
        self._selected_camera_identity = identity
        if identity:
            self._camera_label.setText(f"{identity.model or 'TV46L'}-{identity.serial_number}")
        else:
            self._camera_label.setText("No camera selected")

    def set_connection_state(self, state: CameraConnectionState) -> None:
        """Update connection state and UI.

        Button enablement is applied FIRST and never depends on styling:
        a missing theme manager must freeze colors, never controls.
        """
        self._connection_state = state
        self._update_button_states()
        logger.debug(
            "Panel %r connection=%s buttons(connect=%s disconnect=%s start=%s stop=%s)",
            id(self),
            state.value,
            self._connect_btn.isEnabled(),
            self._disconnect_btn.isEnabled(),
            self._start_btn.isEnabled(),
            self._stop_btn.isEnabled(),
        )

        status = _CONNECTION_STATUS_MAP.get(state, "disconnected")
        status_text = state.value.replace("_", " ").title()

        set_status(self._status_indicator, status)
        self._status_text.setText(status_text)
        set_status(self._status_text, status)

    def set_acquisition_running(self, running: bool) -> None:
        """Update acquisition running state."""
        self._acquisition_running = running
        # Button states are now managed by set_connection_state
        # This method is kept for compatibility but delegates to connection state
        if running:
            self.set_connection_state(CameraConnectionState.ACQUIRING)
        elif self._connection_state == CameraConnectionState.ACQUIRING:
            self.set_connection_state(CameraConnectionState.CONNECTED)

    # -- Focus (Stage 8D; dumb view, window drives runtime asynchronously) --

    @property
    def focus_apply_button(self) -> "QPushButton":
        """The visible Focus Apply button (identity/wiring diagnostics)."""
        return self._focus_apply_btn

    @property
    def focus_spin_value(self) -> int:
        """Current Set-field value as plain int (Apply-path type audit)."""
        return int(self._focus_spin.value())

    def set_focus_enabled(self, enabled: bool, reason: str = "") -> None:
        """Enable/disable the focus group AND its Apply/Read buttons.

        Single authority for focus interactivity: set_focus_busy() blocks
        re-entry during an operation, and this method restores it after.
        (Previously the buttons were only disabled here-by-omission and
        never re-enabled on the read path, leaving them dead while the
        panel showed Ready.)
        """
        old_apply = self._focus_apply_btn.isEnabled()
        old_read = self._focus_refresh_btn.isEnabled()
        self._focus_group.setEnabled(enabled)
        self._focus_apply_btn.setEnabled(enabled)
        self._focus_refresh_btn.setEnabled(enabled)
        if not enabled:
            self._focus_status_label.setText(reason or "Focus unavailable")
        logger.debug(
            "Panel focus enabled=%s apply: %s->%s read: %s->%s reason=%r",
            enabled,
            old_apply,
            self._focus_apply_btn.isEnabled(),
            old_read,
            self._focus_refresh_btn.isEnabled(),
            reason,
        )

    def set_focus_state(self, current_mm: int, min_mm: int, max_mm: int) -> None:
        """Show hardware-reported focus state; clamp spinbox to [min, max].

        Values are clamped to the 32-bit QSpinBox domain so an insane
        camera readback can never raise inside this slot (which would leave
        the panel stuck at "Reading..." with no error state).
        """
        logger.debug(
            "Panel set_focus_state entry current=%r min=%r max=%r",
            current_mm,
            min_mm,
            max_mm,
        )
        _INT_MAX = 2**31 - 1
        current_mm = max(0, min(int(current_mm), _INT_MAX))
        min_mm = max(0, min(int(min_mm), _INT_MAX))
        max_mm = max(0, min(int(max_mm), _INT_MAX))
        self._focus_current_label.setText(f"{current_mm} mm")
        logger.debug("Panel focus current label updated")
        self._focus_range_label.setText(f"{min_mm} … {max_mm} mm")
        logger.debug("Panel focus range label updated")
        self._focus_spin.setRange(max(1, min_mm), max(max_mm, min_mm + 1))
        if not self._focus_spin.hasFocus():
            self._focus_spin.setValue(min(max(current_mm, min_mm), max_mm))
        self._focus_status_label.setText("Ready")
        logger.debug("Panel set_focus_state done status=Ready")

    def set_focus_busy(self, text: str = "Writing…") -> None:
        """Indicate an in-flight focus operation; block re-entry."""
        logger.debug(
            "Panel focus busy %r apply: %s->False read: %s->False",
            text,
            self._focus_apply_btn.isEnabled(),
            self._focus_refresh_btn.isEnabled(),
        )
        self._focus_apply_btn.setEnabled(False)
        self._focus_refresh_btn.setEnabled(False)
        self._focus_status_label.setText(text)

    def set_focus_result(self, requested_mm: int, readback_mm: int) -> None:
        """Report completion with the hardware readback (may differ slightly)."""
        logger.debug(
            "Panel focus result requested=%r readback=%r",
            requested_mm,
            readback_mm,
        )
        self._focus_apply_btn.setEnabled(True)
        self._focus_refresh_btn.setEnabled(True)
        self._focus_current_label.setText(f"{readback_mm} mm")
        if readback_mm == requested_mm:
            self._focus_status_label.setText(f"OK: {requested_mm} mm")
        else:
            self._focus_status_label.setText(
                f"OK (motor offset): requested {requested_mm} mm, at {readback_mm} mm"
            )

    def set_focus_error(self, message: str) -> None:
        """Report failure clearly and re-enable the controls."""
        logger.debug("Panel focus error %r", message)
        self._focus_apply_btn.setEnabled(True)
        self._focus_refresh_btn.setEnabled(True)
        self._focus_status_label.setText(f"Error: {message}")

    # -- NUC (Stage 8G; dumb view, window drives runtime asynchronously) --

    def set_nuc_enabled(self, enabled: bool, reason: str = "") -> None:
        """Enable/disable the NUC group (e.g. camera not running)."""
        self._nuc_group.setEnabled(enabled)
        self._nuc_button.setEnabled(enabled)
        if not enabled:
            self._nuc_status_label.setText(reason or "NUC unavailable")
        elif self._nuc_status_label.text() in ("NUC unavailable", "Camera not running", ""):
            self._nuc_status_label.setText("Ready")

    def set_nuc_busy(self, text: str = "NUC running…") -> None:
        """Indicate an in-flight NUC operation; block re-entry."""
        self._nuc_button.setEnabled(False)
        self._nuc_status_label.setText(text)

    def set_nuc_result(self, duration_s: float) -> None:
        """Report NUC completion with the measured command duration."""
        self._nuc_button.setEnabled(True)
        self._nuc_status_label.setText(f"OK: NUC complete in {duration_s:.2f}s")

    def set_nuc_error(self, message: str) -> None:
        """Report NUC failure clearly and re-enable the control."""
        self._nuc_button.setEnabled(True)
        self._nuc_status_label.setText(f"Error: {message}")


__all__ = ["ImageAcquisitionPanel"]