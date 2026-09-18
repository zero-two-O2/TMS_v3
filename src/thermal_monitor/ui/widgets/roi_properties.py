"""ui.widgets.roi_properties -- properties editor for one ROI object.

Camera, PTZ, and Position are read-only: rebinding requires the
explicit copy/move workflow in the repository layer, never a silent
edit. Geometry edits produce a new validated geometry object.
"""

from __future__ import annotations

try:
    from PyQt6.QtCore import pyqtSignal
    from PyQt6.QtWidgets import (
        QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
        QLineEdit, QPushButton, QVBoxLayout, QWidget,
    )
    _HAS_PYQT6 = True
except ImportError:
    _HAS_PYQT6 = False
    QWidget = object  # type: ignore

from thermal_monitor.roi.models import RoiDefinition


class RoiPropertiesPanel(QWidget if _HAS_PYQT6 else object):
    if _HAS_PYQT6:
        properties_changed = pyqtSignal(object)  # RoiDefinition
        copy_requested = pyqtSignal(object)  # RoiDefinition

    def __init__(self, parent=None) -> None:
        if not _HAS_PYQT6:
            self._roi = None
            return
        super().__init__(parent)
        self._roi: RoiDefinition | None = None
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        binding = QGroupBox("Ownership (read-only)")
        form = QFormLayout(binding)
        self._camera_label = QLabel("—")
        self._ptz_label = QLabel("—")
        self._position_label = QLabel("—")
        self._context_label = QLabel("—")
        form.addRow("Camera:", self._camera_label)
        form.addRow("PTZ:", self._ptz_label)
        form.addRow("Position:", self._position_label)
        form.addRow("Context:", self._context_label)
        layout.addWidget(binding)

        props = QGroupBox("Properties")
        pform = QFormLayout(props)
        self._name_edit = QLineEdit()
        self._name_edit.setToolTip("Object name")
        self._type_label = QLabel("—")
        self._enabled_box = QCheckBox("Enabled")
        self._enabled_box.setToolTip("Disabled objects are stored but not evaluated")
        self._visible_box = QCheckBox("Visible")
        self._visible_box.setToolTip("Hidden objects stay bound but are not drawn")
        self._alarm_edit = QLineEdit()
        self._alarm_edit.setToolTip("Alarm rule reference (analysis objects only)")
        self._geometry_label = QLabel("—")
        self._geometry_label.setWordWrap(True)
        pform.addRow("Name:", self._name_edit)
        pform.addRow("Type:", self._type_label)
        pform.addRow("", self._enabled_box)
        pform.addRow("", self._visible_box)
        pform.addRow("Alarm ref:", self._alarm_edit)
        pform.addRow("Geometry:", self._geometry_label)
        layout.addWidget(props)

        buttons = QHBoxLayout()
        self._apply_btn = QPushButton("Apply")
        self._apply_btn.setToolTip("Validate and apply property changes")
        self._apply_btn.clicked.connect(self._on_apply)
        self._copy_btn = QPushButton("Copy to position…")
        self._copy_btn.setToolTip("Explicit ownership transfer to another position")
        self._copy_btn.clicked.connect(lambda: self.copy_requested.emit(self._roi))
        buttons.addWidget(self._apply_btn)
        buttons.addWidget(self._copy_btn)
        layout.addLayout(buttons)
        layout.addStretch(1)
        self.set_roi(None, context_text="No selection")

    def set_roi(self, roi: RoiDefinition | None, context_text: str = "") -> None:
        self._roi = roi
        if not _HAS_PYQT6:
            return
        if roi is None:
            self._camera_label.setText("—")
            self._ptz_label.setText("—")
            self._position_label.setText("—")
            self._context_label.setText(context_text or "No selection")
            self._name_edit.setText("")
            self._type_label.setText("—")
            self._geometry_label.setText("—")
            self._alarm_edit.setText("")
            self.setEnabled(False)
            return
        self.setEnabled(True)
        self._camera_label.setText(roi.camera_id)
        self._ptz_label.setText(roi.ptz_id)
        self._position_label.setText(roi.position_id)
        self._context_label.setText(context_text or "Active")
        self._name_edit.setText(roi.name)
        self._type_label.setText(f"{roi.object_type.value} ({roi.category.value})")
        self._enabled_box.setChecked(roi.enabled)
        self._visible_box.setChecked(roi.visible)
        self._alarm_edit.setText(roi.alarm_rule_ref)
        self._alarm_edit.setEnabled(roi.is_analysis)
        self._geometry_label.setText(str(roi.geometry.to_dict()))

    def _on_apply(self) -> None:
        if self._roi is None:
            return
        try:
            updated = self._roi.with_updated(
                name=self._name_edit.text(),
                enabled=self._enabled_box.isChecked(),
                visible=self._visible_box.isChecked(),
                alarm_rule_ref=self._alarm_edit.text().strip())
        except Exception:
            return
        self._roi = updated
        self.properties_changed.emit(updated)


__all__ = ["RoiPropertiesPanel"]
