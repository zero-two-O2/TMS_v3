"""Phase 7: profiles, Siemens gate, import/export, diagnostics."""

from __future__ import annotations

import json

import pytest
from PyQt6.QtWidgets import QApplication

import thermal_monitor.camera.source  # noqa: F401 (camera package first)

from thermal_monitor.config.models import PTZConfig
from thermal_monitor.ptz.mapping import SimulatorPtzMapping
from thermal_monitor.ptz.positions import PtzPosition
from thermal_monitor.ptz.positions_io import (
    ConflictPolicy,
    FORMAT_TYPE,
    FORMAT_VERSION,
    commit_import,
    export_positions,
    preview_import,
)
from thermal_monitor.ptz.siemens import (
    REQUIRED_SIEMENS_FIELDS,
    SIEMENS_MAPPING_DOCUMENT_VERSION,
    SiemensMappingDocument,
    mapping_for_profile,
    validate_profile,
    verify_siemens_document,
)


def make_position(pid="pos_1", name="A", camera="cam_A", ptz="PTZ_01") -> PtzPosition:
    return PtzPosition(
        position_id=pid, camera_id=camera, ptz_id=ptz, name=name,
        pan=10.0, tilt=-5.0, velocity=10.0, roi_set_ref="set_a",
    )


def existing_state(positions):
    return (
        {p.position_id for p in positions},
        {(p.camera_id, p.name): p.position_id for p in positions},
    )


class TestProfiles:
    def test_simulator_ready(self):
        status = validate_profile("simulator", "opc.tcp://127.0.0.1:4840", "none", "")
        assert status.ready is True
        assert status.mapping == "simulator"

    def test_simulator_rejects_remote_endpoint(self):
        status = validate_profile("simulator", "opc.tcp://192.168.1.10:4840", "none", "")
        assert status.ready is False

    def test_generic_needs_endpoint(self):
        assert validate_profile("generic", "", "none", "").ready is False
        status = validate_profile("generic", "opc.tcp://10.0.0.5:4840", "none", "")
        assert status.ready is False  # no mapping yet
        assert "mapping" in status.reason

    def test_siemens_always_blocked(self):
        status = validate_profile("siemens", "opc.tcp://10.0.0.5:4840", "none", "")
        assert status.ready is False
        assert "blocked" in status.reason

    def test_no_silent_fallback(self):
        # A configured siemens profile must never resolve a mapping.
        with pytest.raises(Exception):
            mapping_for_profile("siemens", ("PTZ_01",))
        with pytest.raises(Exception):
            mapping_for_profile("generic", ("PTZ_01",))
        mapping = mapping_for_profile("simulator", ("PTZ_01",))
        assert isinstance(mapping, SimulatorPtzMapping)

    def test_config_model_profiles(self):
        assert PTZConfig().profile == "simulator"
        assert PTZConfig(profile="generic", endpoint="opc.tcp://10.0.0.5:4840")
        with pytest.raises(ValueError):
            PTZConfig(profile="trailer-park")
        with pytest.raises(ValueError):
            PTZConfig(profile="simulator", endpoint="opc.tcp://192.168.1.1:4840")
        with pytest.raises(ValueError):
            PTZConfig(profile="siemens", endpoint="")
        with pytest.raises(ValueError):
            PTZConfig(security_mode="username", username="")


class TestConfigModelProfiles:
    @pytest.fixture
    def qapp(self):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance()
        if app is None:
            app = QApplication([])
        yield app

    def test_yaml_profiles_parse(self, tmp_path):
        import yaml

        from thermal_monitor.config import ConfigurationManager

        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "ptz": {
                        "profile": "generic",
                        "endpoint": "opc.tcp://10.0.0.5:4840",
                        "security_mode": "username",
                        "username": "op",
                        "password_env": "TMS_PTZ_PASSWORD",
                        "namespace_uri": "urn:example:plc",
                    }
                }
            ),
            encoding="utf-8",
        )
        config = ConfigurationManager(config_path=path).get_config()
        assert config.ptz.profile == "generic"
        assert config.ptz.security_mode == "username"
        assert config.ptz.namespace_uri == "urn:example:plc"
        # Secrets never come from YAML values.
        assert "TMS_PTZ_PASSWORD" == config.ptz.password_env

    def test_yaml_rejects_bad_profile(self, tmp_path):
        import yaml

        from thermal_monitor.config import ConfigurationManager

        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump({"ptz": {"profile": "nope"}}), encoding="utf-8"
        )
        with pytest.raises(Exception):
            ConfigurationManager(config_path=path).get_config()

    def test_editor_clone_preserves_new_fields(self, qapp):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from thermal_monitor.config import create_config_manager
        from thermal_monitor.ui.configuration_editor import ConfigurationEditor

        manager = create_config_manager()
        editor = ConfigurationEditor(manager)
        clone = editor._clone_ptz_config(manager.get_config().ptz)
        assert clone.profile == manager.get_config().ptz.profile
        assert clone.security_mode == manager.get_config().ptz.security_mode
        assert clone.namespace_uri == manager.get_config().ptz.namespace_uri
        editor.close()


class TestSiemensGate:
    def test_checklist_covers_required_fields(self):
        keys = [key for key, _ in REQUIRED_SIEMENS_FIELDS]
        for expected in (
            "endpoint", "namespace_uri", "target_pan", "command",
            "position_reached", "tolerance", "calibration_request",
            "error_codes", "ptz_addressing",
        ):
            assert expected in keys

    def test_empty_document_fails(self):
        problems = verify_siemens_document(SiemensMappingDocument())
        assert any("version" in p for p in problems)
        assert any("endpoint" in p for p in problems)

    def test_full_document_passes_structure(self):
        document = SiemensMappingDocument(
            version=SIEMENS_MAPPING_DOCUMENT_VERSION,
            values={key: "verified" for key, _ in REQUIRED_SIEMENS_FIELDS},
        )
        assert verify_siemens_document(document) == []
        # ...yet mapping still raises: no implementation exists in Phase 7.
        with pytest.raises(Exception):
            mapping_for_profile("siemens", ("PTZ_01",), document)


class TestImportExport:
    def test_round_trip(self):
        positions = [make_position(), make_position("pos_2", "B")]
        text = export_positions(positions, camera_id="cam_A", ptz_id="PTZ_01")
        document = json.loads(text)
        assert document["format"] == FORMAT_TYPE
        assert document["version"] == FORMAT_VERSION
        assert len(document["positions"]) == 2
        preview, parsed = preview_import(text, set(), {})
        assert preview.valid is True
        assert preview.to_create == 2
        assert len(parsed) == 2
        assert parsed[0].roi_set_ref == "set_a"  # reference, no blobs

    def test_invalid_schema(self):
        preview, _ = preview_import("not json", set(), {})
        assert preview.valid is False

    def test_unsupported_version(self):
        text = json.dumps({"format": FORMAT_TYPE, "version": "v99", "positions": []})
        preview, _ = preview_import(text, set(), {})
        assert preview.valid is False

    def test_invalid_coordinates(self):
        text = json.dumps({
            "format": FORMAT_TYPE, "version": FORMAT_VERSION,
            "positions": [{
                "position_id": "p", "camera_id": "c", "ptz_id": "z",
                "name": "n", "pan": float("nan"), "tilt": 0.0,
            }],
        })
        preview, _ = preview_import(text, set(), {})
        assert preview.valid is False

    def test_missing_references(self):
        text = json.dumps({
            "format": FORMAT_TYPE, "version": FORMAT_VERSION,
            "positions": [{"position_id": "p", "name": "n", "pan": 0.0, "tilt": 0.0}],
        })
        preview, _ = preview_import(text, set(), {})
        assert preview.valid is False

    def test_duplicate_ids_in_file(self):
        text = export_positions([make_position(), make_position()])
        preview, _ = preview_import(text, set(), {})
        assert preview.valid is False

    def test_binding_mismatch(self):
        text = export_positions([make_position()])
        preview, _ = preview_import(
            text, set(), {}, expected_camera_id="cam_A", expected_ptz_id="PTZ_99"
        )
        assert preview.valid is False

    def test_reject_policy(self):
        text = export_positions([make_position()])
        ids, names = existing_state([make_position()])
        preview, _ = preview_import(text, ids, names, policy=ConflictPolicy.REJECT)
        assert preview.valid is False
        # Conflict details name the affected records for confirmation UI.
        assert preview.conflicts == ("A (pos_1)",)
        assert all("conflicts with existing record" in e for e in preview.errors)

    def test_skip_policy(self):
        text = export_positions([make_position(), make_position("pos_9", "New")])
        ids, names = existing_state([make_position()])
        preview, parsed = preview_import(text, ids, names, policy=ConflictPolicy.SKIP)
        assert preview.valid is True
        assert preview.to_skip == 1
        assert preview.to_create == 1

    def test_replace_policy(self):
        text = export_positions([make_position()])
        ids, names = existing_state([make_position()])
        preview, parsed = preview_import(
            text, ids, names, policy=ConflictPolicy.REPLACE
        )
        assert preview.valid is True
        assert preview.to_replace == 1

    def test_empty_file_rejected(self):
        preview, _ = preview_import("", set(), {})
        assert preview.valid is False

    def test_empty_list_valid(self):
        text = export_positions([])
        preview, parsed = preview_import(text, set(), {})
        assert preview.valid is True
        assert parsed == []

    def test_large_bounded_import(self):
        positions = [
            make_position(f"pos_{i:04d}", f"N{i}") for i in range(200)
        ]
        text = export_positions(positions)
        preview, parsed = preview_import(text, set(), {})
        assert preview.valid is True
        assert len(parsed) == 200

    def test_too_large_rejected(self):
        document = {
            "format": FORMAT_TYPE, "version": FORMAT_VERSION,
            "positions": [{"position_id": f"p{i}"} for i in range(1001)],
        }
        preview, _ = preview_import(json.dumps(document), set(), {})
        assert preview.valid is False

    def test_unknown_roi_ref_warns(self):
        text = export_positions([make_position()])
        preview, _ = preview_import(text, set(), {}, known_roi_refs={"other"})
        assert preview.valid is True
        assert len(preview.warnings) == 1

    def test_commit_all_or_nothing(self):
        from test_ptz_positions import MockCursor, MockDatabase

        from thermal_monitor.storage.repositories.ptz import PtzPositionRepository

        positions = [make_position(), make_position("pos_2", "B")]
        cursor = MockCursor(rowcount=1)
        database = MockDatabase(cursor)
        repo = PtzPositionRepository(database)

        # Seed one existing record so get_position finds it for REPLACE.
        seed = MockCursor(
            rows=[(
                1, "pos_1", "cam_A", "PTZ_01", "A", 10.0, -5.0,
                10.0, None, None, "set_a", 1, None, None,
            )],
            rowcount=1,
        )

        class SeedDatabase(MockDatabase):
            def fetch_all(self, sql, params=()):
                if "pos_2" in str(params):
                    return []
                return seed.fetchall()

            def fetch_one(self, sql, params=()):
                return None

        preview, parsed = preview_import(
            export_positions(positions), {"pos_1"}, {("cam_A", "A"): "pos_1"},
            policy=ConflictPolicy.REPLACE,
        )
        assert preview.valid is True
        result = commit_import(repo, parsed, policy=ConflictPolicy.REPLACE)
        assert result.committed is True
        assert result.replaced + result.created == 2


class TestInstalledAppIsolation:
    """Production code must never import the dev-tools simulator package.

    Regression test for the live Phase 9A defect where the installed app
    (repo root NOT on sys.path) failed with ``No module named 'tools'``.
    """

    @staticmethod
    def _production_sources():
        import pathlib

        src = pathlib.Path(__file__).resolve().parent.parent / "src" / "thermal_monitor"
        files = list(src.rglob("*.py"))
        assert files, "production sources not found"
        return files

    def test_no_tools_import_in_production(self):
        offenders = []
        for path in self._production_sources():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "from tools" in stripped or "import tools" in stripped:
                    offenders.append(f"{path.name}:{lineno}: {stripped}")
        assert offenders == [], f"production imports dev tools package: {offenders}"

    def test_default_ptz_ids_live_in_production(self):
        from thermal_monitor.ptz.mapping import default_ptz_ids

        assert default_ptz_ids() == tuple(f"PTZ_{i:02d}" for i in range(1, 9))
        assert default_ptz_ids(2) == ("PTZ_01", "PTZ_02")

    def test_simulator_config_reexports_default_ptz_ids(self):
        from thermal_monitor.ptz.mapping import (
            default_ptz_ids as prod_default,
        )
        from tools.ptz_plc_simulator.simulator_config import (
            default_ptz_ids as sim_default,
        )

        assert sim_default() == prod_default()
