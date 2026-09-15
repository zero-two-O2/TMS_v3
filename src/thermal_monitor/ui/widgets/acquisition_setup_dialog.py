"""
ui.widgets.acquisition_setup_dialog -- Acquisition Setup dialog for Configuration mode.

ThermoView-style startup workflow:

    Camera Control [Connect]
         ↓
    Acquisition Setup (this dialog: camera + FPS/averaging/history)
         ↓
    [Connect...] → Camera Selection dialog (GVCP discovery)
         ↓
    camera connects (existing lifecycle pipeline)
         ↓
    [Start] → acquisition starts (existing Start pipeline)

The dialog owns NO acquisition logic: it only collects the startup
parameters supported by the existing configuration/runtime path
(frame rate, averaging, history length) and forwards Connect/Start
requests to the ConfigurationModeWidget, which reuses the unchanged
lifecycle pipeline. Display-only feed selection is NOT here (it lives
in Camera Control and never restarts acquisition).
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from thermal_monitor.ui.theme import ThemeManager
from thermal_monitor.ui.theme.properties import set_role, set_variant

#: Averaging options supported by the existing configuration path.
AVERAGING_OPTIONS = ["Off", "2", "4", "8", "16"]

#: Startup-parameter defaults (match the previous Camera Control defaults).
DEFAULT_FPS = 9
DEFAULT_AVERAGING = "Off"
DEFAULT_HISTORY_FRAMES = 100


class AcquisitionSetupDialog(QDialog):
    """Startup acquisition parameters + camera connect workflow."""

    # User pressed [Connect...]: widget must open the Camera Selection dialog.
    connect_requested = pyqtSignal()
    # User pressed [Start]: widget must apply values() then start acquisition.
    start_requested = pyqtSignal()

    def __init__(
        self,
        theme_manager: Optional[ThemeManager] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme_manager
        self.setWindowTitle("Acquisition Setup")
        self.setModal(True)
        self.setMinimumWidth(380)

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # --- Camera section ---
        camera_group = QGroupBox("Camera")
        camera_layout = QVBoxLayout(camera_group)
        camera_layout.setContentsMargins(8, 12, 8, 8)
        camera_layout.setSpacing(8)

        self._camera_label = QLabel("Camera: Not connected / selected")
        self._camera_label.setWordWrap(True)
        set_role(self._camera_label, "strong")
        camera_layout.addWidget(self._camera_label)

        connect_row = QHBoxLayout()
        connect_row.setSpacing(6)
        self._connect_btn = QPushButton("Connect...")
        self._connect_btn.setToolTip("Discover and select a camera")
        self._connect_btn.clicked.connect(self.connect_requested.emit)
        set_variant(self._connect_btn, "outline")
        connect_row.addWidget(self._connect_btn)
        connect_row.addStretch()
        camera_layout.addLayout(connect_row)

        self._status_label = QLabel("Status: —")
        set_role(self._status_label, "mono")
        camera_layout.addWidget(self._status_label)

        layout.addWidget(camera_group)

        # --- Acquisition parameters (existing supported parameters only) ---
        params_group = QGroupBox("Acquisition parameters")
        params_layout = QFormLayout(params_group)
        params_layout.setContentsMargins(8, 12, 8, 8)
        params_layout.setSpacing(8)
        params_layout.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        self._fps_spin = QSpinBox()
        self._fps_spin.setRange(1, 60)
        self._fps_spin.setValue(DEFAULT_FPS)
        self._fps_spin.setSuffix(" Hz")
        params_layout.addRow("Frame rate:", self._fps_spin)

        params_layout.addRow("Averaging:", self._averaging_combo())
        params_layout.addRow("History:", self._history_spin())

        layout.addWidget(params_group)

        # --- Buttons ---
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.reject)
        set_variant(self._cancel_btn, "outline")
        button_layout.addWidget(self._cancel_btn)

        self._start_btn = QPushButton("Start")
        self._start_btn.setToolTip("Apply parameters and start acquisition")
        self._start_btn.clicked.connect(self.start_requested.emit)
        self._start_btn.setEnabled(False)
        set_variant(self._start_btn, "accent")
        button_layout.addWidget(self._start_btn)

        layout.addLayout(button_layout)

    def _averaging_combo(self) -> QComboBox:
        self._averaging_combo_box = QComboBox()
        self._averaging_combo_box.addItems(AVERAGING_OPTIONS)
        return self._averaging_combo_box

    def _history_spin(self) -> QSpinBox:
        self._history_spin_box = QSpinBox()
        self._history_spin_box.setRange(1, 1000)
        self._history_spin_box.setValue(DEFAULT_HISTORY_FRAMES)
        self._history_spin_box.setSuffix(" frames")
        return self._history_spin_box

    # -- Public API (dumb view; the widget drives everything) --------------

    def set_camera_summary(self, text: str) -> None:
        """Show the currently selected/connected camera identity."""
        self._camera_label.setText(text or "Camera: Not connected / selected")

    def set_connection_status(self, text: str) -> None:
        """Show the lifecycle-derived connection status line."""
        self._status_label.setText(text or "Status: —")

    def set_start_enabled(self, enabled: bool) -> None:
        """Enable Start only when acquisition can actually start."""
        self._start_btn.setEnabled(bool(enabled))

    def set_params(self, fps: int, averaging: str, history_frames: int) -> None:
        """Populate the dialog from the selected camera's metadata."""
        try:
            self._fps_spin.setValue(int(fps))
        except (TypeError, ValueError):
            self._fps_spin.setValue(DEFAULT_FPS)
        idx = self._averaging_combo_box.findText(str(averaging))
        if idx >= 0:
            self._averaging_combo_box.setCurrentIndex(idx)
        try:
            self._history_spin_box.setValue(int(history_frames))
        except (TypeError, ValueError):
            self._history_spin_box.setValue(DEFAULT_HISTORY_FRAMES)

    def values(self) -> dict:
        """Current dialog parameters (fps / averaging / history_frames)."""
        return {
            "fps": int(self._fps_spin.value()),
            "averaging": str(self._averaging_combo_box.currentText()),
            "history_frames": int(self._history_spin_box.value()),
        }


__all__ = [
    "AcquisitionSetupDialog",
    "AVERAGING_OPTIONS",
    "DEFAULT_FPS",
    "DEFAULT_AVERAGING",
    "DEFAULT_HISTORY_FRAMES",
]
