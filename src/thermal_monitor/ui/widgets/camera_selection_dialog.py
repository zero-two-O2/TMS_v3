"""
ui.widgets.camera_selection_dialog -- Camera selection dialog for Connect workflow.

Similar to ThermoView's "Select Camera" dialog:
- List of discovered cameras with serial, model, IP
- Refresh button
- Connect/Cancel buttons
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QFrame,
    QGroupBox,
    QMessageBox,
)

from thermal_monitor.services.discovery import (
    CameraDiscoveryService,
    DiscoveredCamera,
    GvcpDiscoveryService,
)
from thermal_monitor.ui.theme import ThemeManager


def _interface_label(service: object) -> str:
    """Human interface label for either discovery backend."""
    halcon = getattr(service, "_halcon_interface", None)
    if halcon:
        return str(halcon)
    return str(getattr(service, "interface_label", "GVCP"))


class _DiscoveryWorker(QThread):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, service: "CameraDiscoveryService | GvcpDiscoveryService") -> None:
        super().__init__()
        self._service = service

    def run(self) -> None:
        try:
            self.completed.emit(self._service.discover_cameras())
        except Exception as exc:
            self.failed.emit(str(exc))


class CameraSelectionDialog(QDialog):
    """Dialog for selecting a discovered camera to connect."""

    camera_selected = pyqtSignal(DiscoveredCamera)
    discovery_finished = pyqtSignal()
    discovery_failed = pyqtSignal(str)

    def __init__(
        self,
        discovery_service: "CameraDiscoveryService | GvcpDiscoveryService",
        theme_manager: Optional[ThemeManager] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._discovery_service = discovery_service
        self._theme = theme_manager
        self._selected_camera: DiscoveredCamera | None = None
        self._discovery_worker: _DiscoveryWorker | None = None

        self.setWindowTitle("Select Camera")
        self.setModal(True)
        self.resize(600, 450)

        self._setup_ui()
        self._apply_theme()
        self._refresh_cameras()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Title
        title = QLabel("Select Camera to Connect")
        title.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(title)

        # Camera list
        list_group = QGroupBox("Available Cameras")
        list_layout = QVBoxLayout(list_group)
        list_layout.setContentsMargins(8, 8, 8, 8)
        list_layout.setSpacing(8)

        self._camera_tree = QTreeWidget()
        self._camera_tree.setHeaderLabels(["Interface", "Camera", "Serial", "IP Address", "Model"])
        self._camera_tree.setColumnWidth(0, 120)
        self._camera_tree.setColumnWidth(1, 180)
        self._camera_tree.setColumnWidth(2, 120)
        self._camera_tree.setColumnWidth(3, 120)
        self._camera_tree.setColumnWidth(4, 120)
        self._camera_tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self._camera_tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._camera_tree.itemDoubleClicked.connect(self._on_double_click)
        self._apply_tree_style()
        list_layout.addWidget(self._camera_tree)

        # Refresh button
        refresh_layout = QHBoxLayout()
        refresh_layout.addStretch()
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self._refresh_cameras)
        self._apply_button_style(self._refresh_btn, "secondary")
        refresh_layout.addWidget(self._refresh_btn)
        list_layout.addLayout(refresh_layout)

        layout.addWidget(list_group, 1)

        # Selection info
        self._selection_info = QLabel("No camera selected")
        self._selection_info.setStyleSheet("color: #888; font-style: italic; padding: 4px;")
        layout.addWidget(self._selection_info)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.reject)
        self._apply_button_style(self._cancel_btn, "secondary")
        button_layout.addWidget(self._cancel_btn)

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.clicked.connect(self._on_connect)
        self._connect_btn.setEnabled(False)
        self._apply_button_style(self._connect_btn, "primary")
        button_layout.addWidget(self._connect_btn)

        layout.addLayout(button_layout)

    def _apply_theme(self) -> None:
        if not self._theme:
            return
        colors = self._theme.colors()
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {colors.background};
                color: {colors.text_primary};
            }}
            QGroupBox {{
                border: 1px solid {colors.border};
                border-radius: 4px;
                margin-top: 8px;
                padding-top: 8px;
                font-weight: bold;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
            }}
            QTreeWidget {{
                background-color: {colors.background};
                border: 1px solid {colors.border};
                alternate-background-color: {colors.panel};
            }}
            QHeaderView::section {{
                background-color: {colors.panel};
                color: {colors.text_primary};
                border: 1px solid {colors.border};
                padding: 6px;
                font-weight: bold;
            }}
        """)

    def _apply_tree_style(self) -> None:
        if self._theme:
            colors = self._theme.colors()
            self._camera_tree.setStyleSheet(f"""
                QTreeWidget {{
                    background-color: {colors.background};
                    border: 1px solid {colors.border};
                    alternate-background-color: {colors.panel};
                }}
                QTreeWidget::item {{
                    padding: 4px;
                }}
                QTreeWidget::item:selected {{
                    background-color: {colors.accent};
                    color: white;
                }}
                QHeaderView::section {{
                    background-color: {colors.panel};
                    color: {colors.text_primary};
                    border: 1px solid {colors.border};
                    padding: 6px;
                    font-weight: bold;
                }}
            """)
        self._camera_tree.setAlternatingRowColors(True)

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
                    padding: 8px 24px;
                    font-weight: bold;
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
                    padding: 8px 24px;
                }}
                QPushButton:hover {{ background-color: {colors.secondary_hover}; }}
                QPushButton:disabled {{ background-color: {colors.background}; color: {colors.secondary_disabled_text}; }}
            """)

    def _refresh_cameras(self) -> None:
        """Discover and populate cameras."""
        if self._discovery_worker is not None and self._discovery_worker.isRunning():
            return
        self._camera_tree.clear()
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText("Discovering...")

        self._selection_info.setText("Discovering cameras...")
        self._discovery_worker = _DiscoveryWorker(self._discovery_service)
        self._discovery_worker.completed.connect(self._on_discovery_completed)
        self._discovery_worker.failed.connect(self._on_discovery_failed)
        self._discovery_worker.finished.connect(self._on_discovery_finished)
        self._discovery_worker.start()

    def _on_discovery_completed(self, cameras) -> None:
        for cam in cameras:
            interface = _interface_label(self._discovery_service)
            item = QTreeWidgetItem([interface, cam.camera_id, cam.serial_number or "—", cam.ip_address or "—", cam.model or "—"])
            item.setData(0, Qt.ItemDataRole.UserRole, cam)
            self._camera_tree.addTopLevelItem(item)
        self._selection_info.setText(f"{len(cameras)} camera(s) discovered ({_interface_label(self._discovery_service)})")
        self.discovery_finished.emit()

    def _on_discovery_failed(self, message: str) -> None:
        self._selection_info.setText(f"Discovery failed: {message}")
        self.discovery_failed.emit(message)
        QMessageBox.warning(self, "Discovery Failed", f"Failed to discover cameras:\n{message}")

    def _on_discovery_finished(self) -> None:
        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText("Refresh")

    def _on_selection_changed(self) -> None:
        items = self._camera_tree.selectedItems()
        if items:
            cam = items[0].data(0, Qt.ItemDataRole.UserRole)
            self._selected_camera = cam
            self._connect_btn.setEnabled(True)
            self._selection_info.setText(
                f"Selected: {cam.camera_id}  |  Serial: {cam.serial_number or '—'}  |  IP: {cam.ip_address or '—'}  |  Model: {cam.model or '—'}"
            )
        else:
            self._selected_camera = None
            self._connect_btn.setEnabled(False)
            self._selection_info.setText("No camera selected")

    def _on_double_click(self, item: QTreeWidgetItem, column: int) -> None:
        self._on_connect()

    def _on_connect(self) -> None:
        if self._selected_camera:
            self.camera_selected.emit(self._selected_camera)
            self.accept()

    def closeEvent(self, event) -> None:
        if self._discovery_worker is not None and self._discovery_worker.isRunning():
            self._discovery_worker.quit()
            self._discovery_worker.wait()
        super().closeEvent(event)

    def get_selected_camera(self) -> DiscoveredCamera | None:
        return self._selected_camera


__all__ = ["CameraSelectionDialog"]
