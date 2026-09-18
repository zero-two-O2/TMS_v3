"""roi.editor -- session-scoped editing model for one active position.

Owns the ROI list of the currently active context plus the undo/redo
stack. All mutations validate against the active (camera, PTZ,
position) binding; the stack is discarded on every position change so
commands can never leak across contexts. Drag operations commit a
single logical undo command (begin_drag/end_drag).

Persistence stays outside: the host reads ``rois`` and writes through
``RoiDefinitionRepository`` (off the GUI thread).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from thermal_monitor.roi.context import RoiActiveContext
from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.errors import RoiBindingError, RoiValidationError
from thermal_monitor.roi.handles import clamp_to_image, move_geometry, resize_with_handle
from thermal_monitor.roi.models import RoiDefinition, generate_roi_id
from thermal_monitor.roi.validation import check_roi_binding

MAX_NAME_LEN = 120
MAX_POLYGON_VERTICES = 256
MAX_ROIS_PER_POSITION = 500
MAX_GEOMETRY_JSON_BYTES = 65536


@dataclass(slots=True)
class EditorCommand:
    kind: str  # create | delete | geometry | properties
    roi_id: str
    before: RoiDefinition | None = None
    after: RoiDefinition | None = None


class RoiEditor:
    """Editable ROI session bound to one immutable active context."""

    def __init__(self, context: RoiActiveContext,
                 rois: list[RoiDefinition] | None = None) -> None:
        if context is None or context.state != "active":
            raise RoiBindingError("editing requires an active position context")
        self._context = context
        self._rois: dict[str, RoiDefinition] = {}
        for roi in rois or []:
            self._check_owner(roi)
            self._rois[roi.roi_id] = roi
        self._undo: list[EditorCommand] = []
        self._redo: list[EditorCommand] = []
        self._drag: tuple[str, RoiDefinition, int] | None = None

    # -- accessors ------------------------------------------------------
    @property
    def context(self) -> RoiActiveContext:
        return self._context

    @property
    def rois(self) -> list[RoiDefinition]:
        return [self._rois[k] for k in sorted(self._rois)]

    def get(self, roi_id: str) -> RoiDefinition | None:
        return self._rois.get(roi_id)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    # -- mutations --------------------------------------------------------
    def create(self, object_type: RoiObjectType, geometry,
               name: str = "", *,
               analysis_config: dict | None = None,
               display: dict | None = None) -> RoiDefinition:
        if len(self._rois) >= MAX_ROIS_PER_POSITION:
            raise RoiValidationError(
                f"position holds the maximum of {MAX_ROIS_PER_POSITION} objects")
        self._check_geometry_bounds(object_type, geometry)
        from types import MappingProxyType
        now = time.time()
        roi = RoiDefinition(
            roi_id=generate_roi_id(), camera_id=self._context.camera_id,
            ptz_id=self._context.ptz_id, position_id=self._context.position_id,
            object_type=object_type, geometry=geometry,
            name=self._check_name(name), created_at=now, updated_at=now,
            analysis_config=MappingProxyType(dict(analysis_config or {})),
            display=MappingProxyType(dict(display or {})))
        self._check_payload_size(roi)
        self._rois[roi.roi_id] = roi
        self._push(EditorCommand(kind="create", roi_id=roi.roi_id, after=roi))
        return roi

    def delete(self, roi_id: str) -> RoiDefinition:
        roi = self._require(roi_id)
        del self._rois[roi_id]
        self._push(EditorCommand(kind="delete", roi_id=roi_id, before=roi))
        return roi

    def set_geometry(self, roi_id: str, geometry) -> RoiDefinition:
        roi = self._require(roi_id)
        self._check_geometry_bounds(roi.object_type, geometry)
        updated = roi.with_updated(
            geometry=clamp_to_image(geometry, self._context.image_width,
                                    self._context.image_height))
        self._check_payload_size(updated)
        self._rois[roi_id] = updated
        self._push(EditorCommand(kind="geometry", roi_id=roi_id,
                                 before=roi, after=updated))
        return updated

    def move(self, roi_id: str, dcol: float, drow: float) -> RoiDefinition:
        roi = self._require(roi_id)
        return self.set_geometry(
            roi_id, move_geometry(roi.geometry, dcol, drow))

    def resize(self, roi_id: str, handle_id: str, col: float,
               row: float) -> RoiDefinition:
        roi = self._require(roi_id)
        return self.set_geometry(
            roi_id, resize_with_handle(roi.geometry, handle_id, col, row))

    def rename(self, roi_id: str, name: str) -> RoiDefinition:
        roi = self._require(roi_id)
        updated = roi.with_updated(name=self._check_name(name))
        self._rois[roi_id] = updated
        self._push(EditorCommand(kind="properties", roi_id=roi_id,
                                 before=roi, after=updated))
        return updated

    def set_visibility(self, roi_id: str, visible: bool) -> RoiDefinition:
        roi = self._require(roi_id)
        updated = roi.with_updated(visible=bool(visible))
        self._rois[roi_id] = updated
        self._push(EditorCommand(kind="properties", roi_id=roi_id,
                                 before=roi, after=updated))
        return updated

    def set_enabled(self, roi_id: str, enabled: bool) -> RoiDefinition:
        roi = self._require(roi_id)
        updated = roi.with_updated(enabled=bool(enabled))
        self._rois[roi_id] = updated
        self._push(EditorCommand(kind="properties", roi_id=roi_id,
                                 before=roi, after=updated))
        return updated

    def duplicate(self, roi_id: str) -> RoiDefinition:
        """Explicit duplicate inside the same position (new identity)."""
        roi = self._require(roi_id)
        if len(self._rois) >= MAX_ROIS_PER_POSITION:
            raise RoiValidationError(
                f"position holds the maximum of {MAX_ROIS_PER_POSITION} objects")
        now = time.time()
        copy = RoiDefinition(
            roi_id=generate_roi_id(), camera_id=roi.camera_id,
            ptz_id=roi.ptz_id, position_id=roi.position_id,
            object_type=roi.object_type,
            geometry=move_geometry(roi.geometry, 10.0, 10.0),
            name=(roi.name + " copy")[:MAX_NAME_LEN], enabled=roi.enabled,
            visible=roi.visible, created_at=now, updated_at=now,
            analysis_config=roi.analysis_config,
            alarm_rule_ref=roi.alarm_rule_ref, display=roi.display)
        self._rois[copy.roi_id] = copy
        self._push(EditorCommand(kind="create", roi_id=copy.roi_id, after=copy))
        return copy

    # -- single-undo drag sessions -----------------------------------------
    def begin_drag(self, roi_id: str) -> None:
        roi = self._require(roi_id)
        if self._drag is not None:
            raise RoiValidationError("a drag is already in progress")
        self._drag = (roi_id, roi, len(self._undo))

    def end_drag(self) -> RoiDefinition | None:
        if self._drag is None:
            return None
        roi_id, before, depth = self._drag
        self._drag = None
        after = self._rois.get(roi_id)
        if after is None or after == before:
            del self._undo[depth:]  # cancelled/no movement: drop drag entries
            return after
        # Collapse the per-move entries into one logical command.
        del self._undo[depth:]
        self._push(EditorCommand(kind="geometry", roi_id=roi_id,
                                 before=before, after=after))
        return after

    def cancel_drag(self) -> RoiDefinition | None:
        if self._drag is None:
            return None
        roi_id, before, depth = self._drag
        self._drag = None
        self._rois[roi_id] = before
        del self._undo[depth:]
        return before

    # -- undo/redo -----------------------------------------------------------
    def undo(self) -> EditorCommand | None:
        if not self._undo:
            return None
        command = self._undo.pop()
        self._apply_inverse(command)
        self._redo.append(command)
        return command

    def redo(self) -> EditorCommand | None:
        if not self._redo:
            return None
        command = self._redo.pop()
        self._apply_forward(command)
        self._undo.append(command)
        return command

    def _apply_inverse(self, command: EditorCommand) -> None:
        if command.kind == "create":
            self._rois.pop(command.roi_id, None)
        elif command.kind == "delete":
            assert command.before is not None
            self._rois[command.roi_id] = command.before
        else:
            assert command.before is not None
            self._rois[command.roi_id] = command.before

    def _apply_forward(self, command: EditorCommand) -> None:
        if command.kind == "create":
            assert command.after is not None
            self._rois[command.roi_id] = command.after
        elif command.kind == "delete":
            self._rois.pop(command.roi_id, None)
        else:
            assert command.after is not None
            self._rois[command.roi_id] = command.after

    def _push(self, command: EditorCommand) -> None:
        self._undo.append(command)
        self._redo.clear()

    # -- guards -----------------------------------------------------------------
    def _require(self, roi_id: str) -> RoiDefinition:
        try:
            return self._rois[roi_id]
        except KeyError as exc:
            raise RoiValidationError(f"unknown ROI {roi_id!r}") from exc

    def _check_owner(self, roi: RoiDefinition) -> None:
        check_roi_binding(roi, camera_id=self._context.camera_id,
                          ptz_id=self._context.ptz_id,
                          position_id=self._context.position_id)

    def _check_name(self, name: str) -> str:
        if not isinstance(name, str):
            raise RoiValidationError("name must be a string")
        if len(name) > MAX_NAME_LEN:
            raise RoiValidationError(
                f"name exceeds {MAX_NAME_LEN} characters")
        return name

    def _check_geometry_bounds(self, object_type: RoiObjectType,
                               geometry) -> None:
        from thermal_monitor.roi.models import geometry_expected_type
        expected = geometry_expected_type(object_type)
        if not isinstance(geometry, expected):
            raise RoiValidationError(
                f"{object_type.value} requires {expected.__name__}")
        pts = getattr(geometry, "points", None)
        if pts is not None and len(pts) > MAX_POLYGON_VERTICES:
            raise RoiValidationError(
                f"geometry exceeds {MAX_POLYGON_VERTICES} vertices")

    def _check_payload_size(self, roi: RoiDefinition) -> None:
        size = len(json.dumps(roi.geometry.to_dict()))
        if size > MAX_GEOMETRY_JSON_BYTES:
            raise RoiValidationError(
                f"geometry payload {size}B exceeds {MAX_GEOMETRY_JSON_BYTES}B")


__all__ = ["RoiEditor", "EditorCommand", "MAX_NAME_LEN",
           "MAX_POLYGON_VERTICES", "MAX_ROIS_PER_POSITION",
           "MAX_GEOMETRY_JSON_BYTES"]
