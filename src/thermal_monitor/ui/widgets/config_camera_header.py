"""
ui.widgets.config_camera_header -- Instrument toolbar for Configuration mode.

Top toolbar with:
- Camera selector (compact)
- Connection status indicator
- Global actions (Save, Snapshot, etc.)
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QFrame,
    QMenuBar,
    QMenu,
)

from thermal_monitor.core.models import CameraConnectionState, CameraIdentity
from thermal_monitor.ui.theme import ThemeManager
from thermal_monitor.ui.theme.properties import set_role, set_status, set_variant

#: Local button-style names mapped onto global semantic variants.
#: (Historic "primary" here was accent-filled; "secondary" neutral.)
_BUTTON_VARIANT_MAP = {"primary": "accent", "secondary": "outline", "accent": "ghost"}

_CONNECTION_STATUS_MAP = {
    CameraConnectionState.DISCONNECTED: "disconnected",
    CameraConnectionState.CONNECTING: "connecting",
    CameraConnectionState.CONNECTED: "connected",
    CameraConnectionState.ACQUIRING: "acquiring",
    CameraConnectionState.DEGRADED: "degraded",
    CameraConnectionState.RECONNECTING: "connecting",
    CameraConnectionState.ERROR: "error",
}


class ConfigCameraHeader(QWidget):
    """Top instrument toolbar for Configuration mode."""

    # Signals
    camera_selected = pyqtSignal(str)  # camera_id
    prev_camera_requested = pyqtSignal()
    next_camera_requested = pyqtSignal()
    snapshot_requested = pyqtSignal()
    save_requested = pyqtSignal()

    def __init__(self, theme_manager: Optional[ThemeManager] = None) -> None:
        super().__init__()
        self._theme = theme_manager
        self._cameras: list[tuple[str, str, CameraIdentity | None, bool]] = []
        self._selected_camera_id: str | None = None
        self._connection_state = CameraConnectionState.DISCONNECTED

        self._setup_ui()
        self._apply_theme()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Toolbar row
        toolbar = QWidget()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(8, 4, 8, 4)
        toolbar_layout.setSpacing(8)

        # Left: Camera selector
        self._prev_btn = QPushButton("◀")
        self._prev_btn.setFixedWidth(32)
        self._prev_btn.clicked.connect(self.prev_camera_requested.emit)
        self._prev_btn.setEnabled(False)
        self._apply_button_style(self._prev_btn, "secondary")

        self._camera_combo = QComboBox()
        self._camera_combo.setMinimumWidth(280)
        self._camera_combo.setMaximumWidth(350)
        self._camera_combo.currentIndexChanged.connect(self._on_combo_changed)
        self._apply_input_style(self._camera_combo)

        self._next_btn = QPushButton("▶")
        self._next_btn.setFixedWidth(32)
        self._next_btn.clicked.connect(self.next_camera_requested.emit)
        self._next_btn.setEnabled(False)
        self._apply_button_style(self._next_btn, "secondary")

        toolbar_layout.addWidget(self._prev_btn)
        toolbar_layout.addWidget(self._camera_combo)
        toolbar_layout.addWidget(self._next_btn)

        toolbar_layout.addSpacing(16)

        # Separator
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.VLine)
        sep1.setFrameShadow(QFrame.Shadow.Sunken)
        self._apply_border_style(sep1)
        toolbar_layout.addWidget(sep1)

        toolbar_layout.addSpacing(8)

        # Connection state indicator
        self._conn_indicator = QLabel("●")
        self._conn_indicator.setFixedWidth(16)
        set_status(self._conn_indicator, "disconnected")
        self._conn_label = QLabel("Disconnected")
        set_role(self._conn_label, "strong")

        toolbar_layout.addWidget(self._conn_indicator)
        toolbar_layout.addWidget(self._conn_label)

        toolbar_layout.addStretch()

        # Right side actions
        self._snapshot_btn = QPushButton("Snapshot")
        self._snapshot_btn.clicked.connect(self.snapshot_requested.emit)
        self._apply_button_style(self._snapshot_btn, "secondary")
        toolbar_layout.addWidget(self._snapshot_btn)

        self._save_btn = QPushButton("Save Config")
        self._save_btn.clicked.connect(self.save_requested.emit)
        self._apply_button_style(self._save_btn, "accent")
        toolbar_layout.addWidget(self._save_btn)

        layout.addWidget(toolbar)

        # Separator line at bottom
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        self._apply_border_style(separator)
        layout.addWidget(separator)

    def _apply_theme(self) -> None:
        # Chrome comes from the central stylesheet; only declare the role.
        set_role(self, "toolbar")

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

    def _on_combo_changed(self, index: int) -> None:
        camera_id = self._camera_combo.itemData(index)
        if camera_id:
            self.camera_selected.emit(camera_id)

    def _update_navigation(self) -> None:
        current = self._camera_combo.currentIndex()
        count = self._camera_combo.count()
        self._prev_btn.setEnabled(current > 0)
        self._next_btn.setEnabled(current < count - 1 and count > 0)

    # Public API

    def set_cameras(self, cameras: list[tuple[str, str, CameraIdentity | None, bool]]) -> None:
        """Update camera list. Each entry: (camera_id, display_name, identity, enabled)."""
        current_id = self._camera_combo.currentData()
        self._camera_combo.clear()
        self._cameras = cameras

        for camera_id, display_name, identity, enabled in cameras:
            display = display_name
            if not enabled:
                display = f"[Disabled] {display}"
            self._camera_combo.addItem(display, camera_id)

        # Restore selection
        if current_id:
            idx = self._camera_combo.findData(current_id)
            if idx >= 0:
                self._camera_combo.setCurrentIndex(idx)
        elif cameras:
            self._camera_combo.setCurrentIndex(0)

        self._update_navigation()

    def set_connection_state(self, state: CameraConnectionState) -> None:
        """Update connection state indicator."""
        self._connection_state = state

        status = _CONNECTION_STATUS_MAP.get(state, "disconnected")
        status_text = state.value.replace("_", " ").title()

        set_status(self._conn_indicator, status)
        self._conn_label.setText(status_text)
        # Status color + strong emphasis: status selectors already bold.
        set_status(self._conn_label, status)

    def select_camera_by_id(self, camera_id: str) -> bool:
        """Select camera by ID. Returns True if found."""
        idx = self._camera_combo.findData(camera_id)
        if idx >= 0:
            self._camera_combo.setCurrentIndex(idx)
            return True
        return False

    @property
    def selected_camera_id(self) -> str | None:
        return self._selected_camera_id

    @property
    def connection_state(self) -> CameraConnectionState:
        return self._connection_state


__all__ = ["ConfigCameraHeader"]