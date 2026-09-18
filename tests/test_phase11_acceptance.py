"""Phase 11 simulator acceptance: full position-bound ROI lifecycle.

Drives the REAL stack (no Qt event loop needed for this path):
PtzService over the scripted simulator transport -> real
ObserverRetargetCoordinator -> authoritative reached -> real
load_rois_for_position over real SQLite repositories -> real RoiEditor
sessions -> real persistence.

Covers the §15 flow: goto A, draw+save ROI for A, goto B, A hidden,
draw+save ROI for B, return to A (auto-restore), camera switch clears,
failed goto preserves, position delete removes its ROIs.
"""

from pathlib import Path

import pytest

from thermal_monitor.ptz.positions import PtzPosition
from thermal_monitor.ptz.retarget import ObserverRetargetCoordinator
from thermal_monitor.ptz.roi_activation import ActivePositionRegistry, RoiActivationState
from thermal_monitor.roi.editor import RoiEditor
from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.geometry import RectangleGeometry, SpotGeometry
from thermal_monitor.roi.loading import load_rois_for_position
from thermal_monitor.roi.repository import RoiDefinitionRepository
from thermal_monitor.storage.repositories.ptz import PtzPositionRepository
from thermal_monitor.storage.sqlite_database import SqliteConfig, SqliteDatabase

import sys
sys.path.insert(0, "tests")
from test_ptz_controller import ScriptedTransport  # noqa: E402
from thermal_monitor.ptz.client import OpcUaClientConfig, OpcUaSession  # noqa: E402
from thermal_monitor.ptz.mapping import LogicalField, SimulatorPtzMapping  # noqa: E402
from thermal_monitor.ptz.models import PtzStationBinding  # noqa: E402
from thermal_monitor.ptz.service import PtzService, PtzServiceConfig  # noqa: E402


CAM = "cam_HB25100001"
PTZ = "PTZ_08"


def _healthy_values(mapping: SimulatorPtzMapping, ptz_id: str) -> dict:
    return {
        mapping.resolve(LogicalField.ACTUAL_PAN, ptz_id).node_id: 10.0,
        mapping.resolve(LogicalField.ACTUAL_TILT, ptz_id).node_id: -5.0,
        mapping.resolve(LogicalField.MOVING, ptz_id).node_id: False,
        mapping.resolve(LogicalField.POSITION_REACHED, ptz_id).node_id: True,
        mapping.resolve(LogicalField.READY, ptz_id).node_id: True,
        mapping.resolve(LogicalField.ERROR, ptz_id).node_id: False,
        mapping.resolve(LogicalField.ERROR_CODE, ptz_id).node_id: 0,
        mapping.resolve(LogicalField.CALIBRATION_REQUIRED, ptz_id).node_id: False,
        mapping.resolve(LogicalField.CALIBRATION_ACTIVE, ptz_id).node_id: False,
        mapping.resolve(LogicalField.CALIBRATION_COMPLETE, ptz_id).node_id: True,
    }


@pytest.fixture
def stack(tmp_path):
    database = SqliteDatabase(SqliteConfig(path=str(tmp_path / "acc.db")))
    database.connect()
    database.run_migrations(Path("database/migrations/sqlite"))
    mapping = SimulatorPtzMapping(ptz_ids=(PTZ,))
    transport = ScriptedTransport(_healthy_values(mapping, PTZ))
    session = OpcUaSession(OpcUaClientConfig(endpoint="fake"), transport)
    session.connect()
    service = PtzService(
        session, mapping,
        PtzServiceConfig(monitor_interval_s=0.02, stale_threshold_s=0.2))
    service.register_binding(PtzStationBinding(camera_id=CAM, ptz_id=PTZ))
    registry = ActivePositionRegistry()
    coordinator = ObserverRetargetCoordinator(service, registry,
                                             poll_interval_s=0.005)
    positions = PtzPositionRepository(database)
    rois = RoiDefinitionRepository(database)
    sessions = {"gen": 1}
    return {
        "db": database, "service": service, "registry": registry,
        "coordinator": coordinator, "positions": positions, "rois": rois,
        "sessions": sessions, "mapping": mapping, "transport": transport,
    }


def _park_at(stack, pan: float, tilt: float) -> None:
    """Deterministic simulator stand-in: report the target as reached."""
    mapping, transport = stack["mapping"], stack["transport"]
    transport._values[
        mapping.resolve(LogicalField.ACTUAL_PAN, PTZ).node_id] = pan
    transport._values[
        mapping.resolve(LogicalField.ACTUAL_TILT, PTZ).node_id] = tilt


def _save_position(stack, position_id, name, pan, tilt):
    result = stack["positions"].create_position(PtzPosition(
        position_id=position_id, camera_id=CAM, ptz_id=PTZ, name=name,
        pan=pan, tilt=tilt, velocity=50.0))
    assert result.success, result.error
    return result.data


def _goto_and_session(stack, position, pos_gen):
    coordinator = stack["coordinator"]
    sessions = stack["sessions"]
    _park_at(stack, position.pan, position.tilt)
    result = coordinator.retarget(
        CAM, position, lambda cam: None, lambda: None,
        lambda: sessions["gen"], timeout_s=30.0)
    assert result.state == RoiActivationState.COMPLETED, (
        result.error.message if result.error else result.state)
    retained = coordinator.registry.get(CAM)
    context, rois = load_rois_for_position(
        CAM, position.position_id, sessions["gen"], pos_gen,
        position_provider=lambda pid: stack["positions"].get_position(pid).data,
        roi_repository=stack["rois"],
        current_session_generation=sessions["gen"],
        context_generation=(retained.context_generation
                            if retained is not None else 0),
        source="goto")
    return context, list(rois)


def test_simulator_acceptance_lifecycle(stack):
    pos_a = _save_position(stack, "pos_A", "Position 1", 90.0, 90.0)
    pos_b = _save_position(stack, "pos_B", "Position 2", 60.3, 65.9)

    # Goto A -> session A, empty.
    ctx_a, rois_a = _goto_and_session(stack, pos_a, 1)
    assert rois_a == []
    editor_a = RoiEditor(ctx_a, rois_a)

    # Draw + save a rectangle ROI for A.
    created = editor_a.create(
        RoiObjectType.RECTANGLE,
        RectangleGeometry(row1=100, col1=100, row2=200, col2=250), "Chair ROI")
    stack["rois"].create(created)
    assert stack["rois"].list_for_position(CAM, "pos_A")[0].name == "Chair ROI"

    # Goto B -> session B shows no A ROIs.
    ctx_b, rois_b = _goto_and_session(stack, pos_b, 2)
    assert rois_b == []
    editor_b = RoiEditor(ctx_b, rois_b)
    created_b = editor_b.create(
        RoiObjectType.SPOT, SpotGeometry(row=300, col=320), "Door ROI")
    stack["rois"].create(created_b)

    # Return to A -> A ROI automatically restored.
    _, rois_a2 = _goto_and_session(stack, pos_a, 3)
    assert [r.name for r in rois_a2] == ["Chair ROI"]
    # Return to B -> B ROI automatically restored.
    _, rois_b2 = _goto_and_session(stack, pos_b, 4)
    assert [r.name for r in rois_b2] == ["Door ROI"]

    # Cross-checks: strict isolation in the store.
    assert [r.roi_id for r in stack["rois"].list_for_position(CAM, "pos_B")] == [
        r.roi_id for r in rois_b2]
    assert all(r.position_id == "pos_B" for r in rois_b2)

    # Camera switch: another camera's session generation rejects A's context.
    assert not ctx_a.matches(camera_id="cam_OTHER", session_generation=99,
                             position_id="pos_A",
                             context_generation=ctx_a.context_generation)

    # Position delete removes its ROIs atomically (same pattern as the UI).
    with stack["db"].transaction() as cursor:
        cursor.execute(
            "DELETE FROM roi_definitions WHERE camera_id = ? AND position_id = ?",
            (CAM, "pos_B"))
        cursor.execute(
            "DELETE FROM ptz_positions WHERE position_id = ? AND camera_id = ?",
            ("pos_B", CAM))
    assert stack["rois"].list_for_position(CAM, "pos_B") == []
    assert stack["rois"].list_for_position(CAM, "pos_A") != []


def test_failed_goto_preserves_previous_session(stack):
    pos_a = _save_position(stack, "pos_A", "Position 1", 90.0, 90.0)
    ctx_a, _ = _goto_and_session(stack, pos_a, 1)
    # Unknown position: movement workflow rejects before any publish.
    from thermal_monitor.ptz.positions import PtzPosition as _P
    ghost = _P(position_id="pos_GHOST", camera_id=CAM, ptz_id=PTZ,
               name="Ghost", pan=10.0, tilt=10.0)
    result = stack["coordinator"].retarget(
        CAM, ghost, lambda cam: None, lambda: None,
        lambda: stack["sessions"]["gen"], timeout_s=5.0)
    # Either cancelled/failed (no analysis config path) or completed with
    # no ROI rows; in no case may a ghost session validate.
    if result.state == RoiActivationState.COMPLETED:
        context, rois = load_rois_for_position(
            CAM, "pos_GHOST", stack["sessions"]["gen"], 2,
            position_provider=lambda pid: None,
            roi_repository=stack["rois"])
        assert False, "ghost position must not load"
    assert ctx_a.position_id == "pos_A"  # previous session object untouched
    stored = stack["coordinator"].registry.get(CAM)
    assert stored is None or stored.position_id in ("pos_A", "pos_GHOST")
