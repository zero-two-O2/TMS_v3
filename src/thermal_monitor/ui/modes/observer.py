"""
ui.modes.observer -- Observer mode widget (placeholder).

Observer mode depends on the final production acquisition path.
This is a placeholder showing the mode state only.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QGroupBox,
    QFormLayout,
)

from thermal_monitor.services.mode import ModeService


class ObserverModeWidget(QWidget):
    """Placeholder widget for Observer mode.

    Observer mode is not yet implemented - it requires the final
    production acquisition path which is still under investigation.
    """

    def __init__(self, mode_service: ModeService) -> None:
        super().__init__()
        self._mode_service = mode_service
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Icon/title area
        title_label = QLabel("OBSERVER MODE")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet("font-size: 32px; font-weight: bold; color: #2196F3;")
        layout.addWidget(title_label)

        subtitle = QLabel("Live Camera Monitoring")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet("font-size: 18px; color: #666;")
        layout.addWidget(subtitle)

        layout.addSpacing(30)

        # Status info
        status_group = QGroupBox("Status")
        status_layout = QFormLayout(status_group)

        self._acquisition_status = QLabel("Not Available")
        self._acquisition_status.setStyleSheet("color: #F44336; font-weight: bold;")
        status_layout.addRow("Acquisition:", self._acquisition_status)

        self._camera_count = QLabel("0")
        status_layout.addRow("Connected Cameras:", self._camera_count)

        self._fps_label = QLabel("—")
        status_layout.addRow("Aggregate FPS:", self._fps_label)

        self._active_alarms = QLabel("0")
        status_layout.addRow("Active Alarms:", self._active_alarms)

        layout.addWidget(status_group)

        layout.addSpacing(20)

        # Information box
        info_group = QGroupBox("Information")
        info_layout = QVBoxLayout(info_group)

        info_text = QLabel(
            "Observer mode provides live monitoring of connected thermal cameras.\n\n"
            "This mode is currently a PLACEHOLDER because it depends on the final\n"
            "production acquisition path, which is still under investigation.\n\n"
            "Known blockers:\n"
            "• IR/VL alternating acquisition via HALCON is not working\n"
            "• Sustained HALCON capture loses packets and drops effective FPS\n"
            "• HALCON engineer is investigating the acquisition path separately\n\n"
            "Once the production acquisition is resolved, Observer mode will provide:\n"
            "• Live 8-camera display grid\n"
            "• Real-time ROI statistics overlay\n"
            "• Live alarm state updates\n"
            "• PTZ control and position switching\n"
            "• SharedMemoryRingBuffer consumer UI\n"
            "• Recording trigger controls"
        )
        info_text.setWordWrap(True)
        info_text.setAlignment(Qt.AlignmentFlag.AlignLeft)
        info_text.setStyleSheet("font-size: 13px; color: #333;")
        info_layout.addWidget(info_text)

        layout.addWidget(info_group)

        layout.addStretch()

    def on_mode_activated(self) -> None:
        """Called when observer mode becomes active."""
        pass