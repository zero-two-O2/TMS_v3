"""
ui.widgets.ptz_position_table -- Position Table shelf panel (Phase 6).

Service-agnostic list surface for saved PTZ positions: renders
PtzPosition records, emits CRUD/GoTo intents. Never touches the service,
OPC UA, or the database; the parent widget owns all I/O on background
threads. ROI data is displayed by reference only (roi_set_ref).
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from thermal_monitor.ptz.positions import PtzPosition
from thermal_monitor.ui.theme import ThemeManager
from thermal_monitor.ui.theme.properties import set_variant


class PtzPositionTablePanel(QWidget):
    """Position Table: saved positions for the active camera/PTZ."""

    goto_requested = pyqtSignal(str)  # position_id
    save_current_requested = pyqtSignal(str)  # name
    delete_requested = pyqtSignal(str)  # position_id
    rename_requested = pyqtSignal(str, str)  # position_id, new name
    roi_associate_requested = pyqtSignal(str, str)  # position_id, roi_set_ref
    refresh_requested = pyqtSignal()
    export_requested = pyqtSignal()
    import_requested = pyqtSignal()

    _COLUMNS = ("Name", "Pan", "Tilt", "Velocity", "ROI set", "Enabled")

    def __init__(self, theme_manager: Optional[ThemeManager] = None) -> None:
        super().__init__()
        self._theme = theme_manager
        self._camera_id: str | None = None
        self._ptz_id: str | None = None
        self._positions: list[PtzPosition] = []
        self._setup_ui()

    def _setup_ui(self) -> None:
        from thermal_monitor.ui.theme.tokens import metrics_for

        m = metrics_for(self._theme)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(m.panel_spacing)

        self._station_label = QLabel("No camera selected")
        layout.addWidget(self._station_label)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(len(self._COLUMNS))
        self._tree.setHeaderLabels(list(self._COLUMNS))
        self._tree.setRootIsDecorated(False)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self._tree, 1)

        save_row = QHBoxLayout()
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Position name…")
        self._save_btn = QPushButton("Save Current")
        self._save_btn.clicked.connect(self._on_save_current)
        save_row.addWidget(self._name_edit, 1)
        save_row.addWidget(self._save_btn)
        layout.addLayout(save_row)

        row = QHBoxLayout()
        self._goto_btn = QPushButton("Go To")
        self._goto_btn.clicked.connect(self._on_goto)
        self._rename_btn = QPushButton("Rename")
        self._rename_btn.clicked.connect(self._on_rename)
        self._roi_btn = QPushButton("Set ROI Set…")
        self._roi_btn.clicked.connect(self._on_roi_associate)
        self._delete_btn = QPushButton("Delete")
        self._delete_btn.clicked.connect(self._on_delete)
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh_requested.emit)
        for btn in (
            self._goto_btn,
            self._rename_btn,
            self._roi_btn,
            self._delete_btn,
            self._refresh_btn,
        ):
            row.addWidget(btn)
        layout.addLayout(row)

        io_row = QHBoxLayout()
        self._export_btn = QPushButton("Export…")
        self._export_btn.clicked.connect(self.export_requested.emit)
        self._import_btn = QPushButton("Import…")
        self._import_btn.clicked.connect(self.import_requested.emit)
        io_row.addWidget(self._export_btn)
        io_row.addWidget(self._import_btn)
        layout.addLayout(io_row)

        for btn, style in (
            (self._save_btn, "accent"),
            (self._goto_btn, "primary"),
            (self._rename_btn, "outline"),
            (self._roi_btn, "outline"),
            (self._delete_btn, "danger"),
            (self._refresh_btn, "outline"),
            (self._export_btn, "outline"),
            (self._import_btn, "outline"),
        ):
            set_variant(btn, style)
        self._refresh_buttons()

    # -- events --------------------------------------------------------------

    def selected_position_id(self) -> str | None:
        items = self._tree.selectedItems()
        if not items:
            return None
        return items[0].data(0, Qt.ItemDataRole.UserRole)

    def _on_selection_changed(self) -> None:
        self._refresh_buttons()

    def _on_goto(self) -> None:
        position_id = self.selected_position_id()
        if position_id:
            self.goto_requested.emit(position_id)

    def _on_save_current(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.information(self, "Save Position", "Enter a position name first.")
            return
        self.save_current_requested.emit(name)
        self._name_edit.clear()

    def _on_delete(self) -> None:
        position_id = self.selected_position_id()
        if not position_id:
            return
        answer = QMessageBox.question(
            self,
            "Delete Position",
            "Delete the selected position? The ROI set itself is kept.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.delete_requested.emit(position_id)

    def _on_rename(self) -> None:
        position_id = self.selected_position_id()
        if not position_id:
            return
        current = next(
            (p.name for p in self._positions if p.position_id == position_id), ""
        )
        name, accepted = QInputDialog.getText(
            self, "Rename Position", "Name:", text=current
        )
        if accepted and name.strip():
            self.rename_requested.emit(position_id, name.strip())

    def _on_roi_associate(self) -> None:
        position_id = self.selected_position_id()
        if not position_id:
            return
        current = next(
            (p.roi_set_ref for p in self._positions if p.position_id == position_id),
            "",
        )
        ref, accepted = QInputDialog.getText(
            self,
            "Associate ROI Set",
            "ROI-set reference (position ID key, empty to clear):",
            text=current,
        )
        if accepted:
            self.roi_associate_requested.emit(position_id, ref.strip())

    # -- public API ------------------------------------------------------------

    def set_station(self, camera_id: str | None, ptz_id: str | None) -> None:
        """Show which station positions belong to (no stale rows)."""
        self._camera_id = camera_id
        self._ptz_id = ptz_id
        self._positions = []
        self._tree.clear()
        if camera_id is None:
            self._station_label.setText("No camera selected")
        elif ptz_id is None:
            self._station_label.setText(f"{camera_id}: no PTZ configured")
        else:
            self._station_label.setText(f"{camera_id} / {ptz_id}")
        self._refresh_buttons()

    def set_positions(self, positions: list[PtzPosition]) -> None:
        """Replace the full row set (parent owns filtering/ordering)."""
        selected = self.selected_position_id()
        self._positions = list(positions)
        self._tree.clear()
        for position in positions:
            velocity = (
                f"{position.velocity:.1f}"
                if position.velocity is not None
                else (
                    f"{position.pan_velocity:.1f}/{position.tilt_velocity:.1f}"
                    if position.pan_velocity is not None
                    else "—"
                )
            )
            mismatch = (
                position.ptz_id != self._ptz_id if self._ptz_id is not None else False
            )
            item = QTreeWidgetItem(
                [
                    position.name + (" ⚠" if mismatch else ""),
                    f"{position.pan:.1f}°",
                    f"{position.tilt:.1f}°",
                    velocity,
                    position.roi_set_ref or "—",
                    "Yes" if position.enabled else "No",
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, position.position_id)
            if position.position_id == selected:
                item.setSelected(True)
            self._tree.addTopLevelItem(item)
        self._refresh_buttons()

    def show_message(self, message: str) -> None:
        QMessageBox.information(self, "Position Table", message)

    def clear(self) -> None:
        self.set_station(None, None)

    def _refresh_buttons(self) -> None:
        has_selection = self.selected_position_id() is not None
        station_ready = self._camera_id is not None and self._ptz_id is not None
        self._save_btn.setEnabled(station_ready)
        self._name_edit.setEnabled(station_ready)
        self._goto_btn.setEnabled(has_selection and station_ready)
        self._rename_btn.setEnabled(has_selection)
        self._roi_btn.setEnabled(has_selection)
        self._delete_btn.setEnabled(has_selection)
        self._export_btn.setEnabled(station_ready)
        self._import_btn.setEnabled(station_ready)
