"""Tests for the Phase 3 PTZ mapping layer (no PLC, no GUI, no asyncua)."""

from __future__ import annotations

import pytest

from thermal_monitor.ptz.errors import PtzValidationError
from thermal_monitor.ptz.mapping import (
    LogicalField,
    PtzMappingError,
    SiemensPtzMapping,
    SimulatorPtzMapping,
)

PTZ_IDS = ("PTZ_01", "PTZ_02")


@pytest.fixture
def sim_mapping() -> SimulatorPtzMapping:
    return SimulatorPtzMapping(ptz_ids=PTZ_IDS)


class TestSimulatorMapping:
    def test_ptz_01_actual_pan_resolution(self, sim_mapping):
        node = sim_mapping.resolve(LogicalField.ACTUAL_PAN, "PTZ_01")
        assert node.node_id == "ns=2;s=PTZ_01.ActualPan"
        assert node.datatype == "float"
        assert node.access == "read"

    def test_ptz_02_gets_own_tag_set(self, sim_mapping):
        node = sim_mapping.resolve(LogicalField.TARGET_PAN, "PTZ_02")
        assert node.node_id == "ns=2;s=PTZ_02.TargetPan"
        assert "PTZ_01" not in node.node_id

    def test_no_raw_node_ids_outside_mapping(self, sim_mapping):
        """Callers only handle LogicalField + ptz_id, never ns= strings."""
        for field in sim_mapping.available_fields("PTZ_01"):
            node = sim_mapping.resolve(field, "PTZ_01")
            assert node.logical_name == f"PTZ_01.{field.value}"

    def test_unknown_ptz_id_rejected(self, sim_mapping):
        with pytest.raises(PtzMappingError):
            sim_mapping.resolve(LogicalField.ACTUAL_PAN, "PTZ_09")

    def test_unknown_field_rejected(self, sim_mapping):
        with pytest.raises(PtzMappingError):
            sim_mapping.resolve("not_a_field", "PTZ_01")  # type: ignore[arg-type]

    def test_simulator_namespace_is_explicit(self):
        mapping = SimulatorPtzMapping(ptz_ids=PTZ_IDS)
        assert mapping.namespace_uri == "urn:tms:ptz:sim"

    def test_blank_ptz_id_rejected(self):
        with pytest.raises(PtzValidationError):
            SimulatorPtzMapping(ptz_ids=("PTZ_01", " "))

    def test_eight_instances_without_per_instance_clients(self):
        ids = tuple(f"PTZ_{i:02d}" for i in range(1, 9))
        mapping = SimulatorPtzMapping(ptz_ids=ids)
        assert mapping.known_ptz_ids == ids
        for ptz_id in ids:
            assert mapping.resolve(LogicalField.MOVING, ptz_id).node_id.startswith(
                f"ns=2;s={ptz_id}."
            )


class TestVelocityMapping:
    def test_single_velocity_field(self, sim_mapping):
        node = sim_mapping.resolve(LogicalField.VELOCITY, "PTZ_01")
        assert node.access == "write"

    def test_per_axis_velocity_fields(self, sim_mapping):
        pan = sim_mapping.resolve(LogicalField.PAN_VELOCITY, "PTZ_01")
        tilt = sim_mapping.resolve(LogicalField.TILT_VELOCITY, "PTZ_01")
        assert pan.node_id != tilt.node_id


class TestSiemensPlaceholder:
    def test_resolve_always_fails_with_clear_message(self):
        mapping = SiemensPtzMapping(ptz_ids=("PTZ_01",))
        with pytest.raises(PtzMappingError, match="not configured"):
            mapping.resolve(LogicalField.ACTUAL_PAN, "PTZ_01")

    def test_unknown_ptz_id_rejected(self):
        mapping = SiemensPtzMapping(ptz_ids=("PTZ_01",))
        with pytest.raises(PtzMappingError):
            mapping.resolve(LogicalField.ACTUAL_PAN, "PTZ_99")
