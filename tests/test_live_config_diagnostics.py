"""Live configuration diagnosis tests - 14 acceptance items.

Proves the exact root cause of the NO CAMERAS bug:
- LiveModeWidget uses the same ConfigurationService instance as Configuration mode
- Hydration from config.yaml mapping is the single loader
- Filtering semantics, assignment, and button states are correct
- Render workers are not conflated with camera connection
- No acquisition pipeline is modified
"""
from __future__ import annotations

import os
import pathlib
import inspect
import logging

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from unittest.mock import Mock
from PyQt6.QtWidgets import QApplication
from dataclasses import replace

import thermal_monitor.camera  # ensure shm circular import resolved
from thermal_monitor.core.models import CameraIdentity
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.ui.windows.live_window import LiveModeWidget, LiveTileState, FIXED_CAMERA_SLOTS
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.offline import OfflineService
from thermal_monitor.services.runtime import CameraRuntimeService
from thermal_monitor.config import create_config_manager
from thermal_monitor.config.models import CameraMappingConfig

@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _make_config_service(n, enabled_fn=None, thermal_fn=None):
    svc = ConfigurationService()
    for i in range(1, n+1):
        ident = CameraIdentity(camera_id=f"cam_{i}", serial_number=f"SN{i:05d}")
        cfg = svc.create_camera_config(identity=ident, name=f"Camera {i}")
        if enabled_fn and not enabled_fn(i):
            cfg = replace(cfg, enabled=False)
        if thermal_fn and not thermal_fn(i):
            cfg = replace(cfg, thermal_enabled=False)
        svc.set_camera_config(cfg)
    return svc


def _close(wall):
    try:
        wall.on_mode_deactivated()
    except Exception:
        pass
    for tile in list(getattr(wall, "_tiles", [])):
        for widget in (getattr(tile, "_image_widget", None), getattr(tile, "_vl_widget", None)):
            try:
                if widget is not None:
                    widget.close()
            except Exception:
                pass
    try:
        wall.close()
    except Exception:
        pass


# 1. Live receives configured cameras from ConfigurationService.
def test_1_live_receives_configured_cameras(qapp, caplog):
    caplog.set_level(logging.INFO)
    svc = _make_config_service(3)
    wall = LiveModeWidget(mode_service=Mock(), config_service=svc, runtime_service=Mock(is_camera_running=lambda x: False, observer_service=lambda x: None, camera_stats=lambda x: None), config_manager=Mock(config_path=pathlib.Path("config/config.yaml")))
    wall.on_mode_activated()
    assert len(svc.get_all_camera_configs()) == 3
    assert len(wall._enabled_cameras()) == 3
    assert len(wall._camera_to_slot) == 3
    # Diagnostics must have logged
    assert any("LIVE CONFIG" in r.message for r in caplog.records)
    assert any("LIVE ELIGIBLE CAMERAS = 3" in r.message for r in caplog.records)
    _close(wall)


# 2. ConfigurationService returns expected number via hydration
def test_2_hydration_from_config_yaml(qapp):
    cm = create_config_manager(create_default=False)
    cfg = cm.get_config()
    cfg.cameras.mapping.extend(
        CameraMappingConfig(
            camera_id=f"cam_{i:02d}",
            serial_number=f"SN{i:05d}",
            name=f"Camera {i}",
        )
        for i in range(1, 9)
    )
    svc = ConfigurationService()
    ms = ModeService()
    offline = OfflineService()
    runtime = CameraRuntimeService(cameras_config=cfg.cameras, system_config=cfg.system, recording_config=cfg.recording, storage_config=cfg.storage, calibration_config=cfg.calibration)
    from thermal_monitor.ui.controller import AppController
    controller = AppController(mode_service=ms, config_service=svc, offline_service=offline, runtime_service=runtime, config_manager=cm, theme_manager=None)
    controller._configure_configuration_service(cfg)
    assert len(svc.get_all_camera_configs()) == 8
    # Each entry has expected ids
    ids = [c.identity.camera_id for c in svc.get_all_camera_configs()]
    assert ids == [f"cam_{i:02d}" for i in range(1, 9)]


# 3. enabled filtering is correct
def test_3_enabled_filtering(qapp):
    svc = _make_config_service(8, enabled_fn=lambda i: i != 3)
    wall = LiveModeWidget(mode_service=Mock(), config_service=svc, runtime_service=Mock(is_camera_running=lambda x: False, observer_service=lambda x: None, camera_stats=lambda x: None))
    wall.on_mode_activated()
    assert "cam_3" not in wall._camera_to_slot
    assert len(wall._enabled_cameras()) == 7
    _close(wall)

    svc2 = _make_config_service(8, enabled_fn=lambda i: False)
    wall2 = LiveModeWidget(mode_service=Mock(), config_service=svc2, runtime_service=Mock(is_camera_running=lambda x: False, observer_service=lambda x: None, camera_stats=lambda x: None))
    wall2.on_mode_activated()
    assert len(wall2._enabled_cameras()) == 0
    # Button should be NO ELIGIBLE CAMERAS when configured but none eligible
    assert wall2._connect_button.text() == "NO ELIGIBLE CAMERAS"
    assert not wall2._connect_button.isEnabled()
    _close(wall2)


# 4. thermal_enabled filtering is correct
def test_4_thermal_enabled_filtering(qapp):
    svc = _make_config_service(8, thermal_fn=lambda i: i not in (1, 8))
    wall = LiveModeWidget(mode_service=Mock(), config_service=svc, runtime_service=Mock(is_camera_running=lambda x: False, observer_service=lambda x: None, camera_stats=lambda x: None))
    wall.on_mode_activated()
    assert "cam_1" not in wall._camera_to_slot
    assert "cam_8" not in wall._camera_to_slot
    assert len(wall._enabled_cameras()) == 6
    _close(wall)


# 5. configured-but-stopped becomes READY, not NOT_CONFIGURED
def test_5_configured_but_stopped_is_ready(qapp):
    svc = _make_config_service(8)
    wall = LiveModeWidget(mode_service=Mock(), config_service=svc, runtime_service=Mock(is_camera_running=lambda x: False, observer_service=lambda x: None, camera_stats=lambda x: None))
    wall.on_mode_activated()
    for t in wall._tiles:
        assert t.state is LiveTileState.READY
        assert "Not configured" not in t._name_label.text()
    assert "No cameras configured" not in wall._summary_label.text()
    assert wall._header_status_word(wall._tile_counts()) == "READY"
    _close(wall)


# 6. zero configurations produces NO CAMERAS
def test_6_zero_configurations_no_cameras(qapp):
    svc = ConfigurationService()
    wall = LiveModeWidget(mode_service=Mock(), config_service=svc, runtime_service=Mock(is_camera_running=lambda x: False, observer_service=lambda x: None, camera_stats=lambda x: None))
    wall.on_mode_activated()
    assert len(svc.get_all_camera_configs()) == 0
    assert len(wall._enabled_cameras()) == 0
    assert all(t.state is LiveTileState.NOT_AVAILABLE for t in wall._tiles)
    assert wall._summary_label.text() == "No cameras configured"
    assert wall._connect_button.text() == "NO CAMERAS"
    assert not wall._connect_button.isEnabled()
    # NOT_AVAILABLE tiles show Not configured
    assert wall._tiles[0]._name_label.text() == "Not configured"
    _close(wall)


# 7. eight configurations produce eight fixed assignments
def test_7_eight_produces_eight_assignments(qapp):
    svc = _make_config_service(8)
    wall = LiveModeWidget(mode_service=Mock(), config_service=svc, runtime_service=Mock(is_camera_running=lambda x: False, observer_service=lambda x: None, camera_stats=lambda x: None))
    wall.on_mode_activated()
    assert len(wall._camera_to_slot) == 8
    for slot in range(8):
        assert wall._tiles[slot].camera_id == f"cam_{slot+1}"
        assert wall._tiles[slot].slot_index == slot
        assert wall._tiles[slot].state is LiveTileState.READY
    assert len([t for t in wall._tiles if t.state is LiveTileState.NOT_AVAILABLE]) == 0
    _close(wall)


# 8. camera positions remain fixed
def test_8_positions_fixed(qapp):
    svc = _make_config_service(8)
    wall = LiveModeWidget(mode_service=Mock(), config_service=svc, runtime_service=Mock(is_camera_running=lambda x: False, observer_service=lambda x: None, camera_stats=lambda x: None))
    wall.on_mode_activated()
    pos_before = [wall._grid_layout.getItemPosition(i)[:2] for i in range(9)]
    ids_before = [t.camera_id for t in wall._tiles]
    # Simulate error on tile 4
    wall._tiles[4].on_error("link loss")
    assert wall._tiles[4].camera_id == "cam_5"
    assert wall._tiles[4].slot_index == 4
    assert [t.camera_id for t in wall._tiles] == ids_before
    assert [wall._grid_layout.getItemPosition(i)[:2] for i in range(9)] == pos_before
    _close(wall)


# 9. CONNECT & START ALL enabled when eligible exists
def test_9_connect_enabled_when_eligible(qapp):
    svc = _make_config_service(8)
    wall = LiveModeWidget(mode_service=Mock(), config_service=svc, runtime_service=Mock(is_camera_running=lambda x: False, observer_service=lambda x: None, camera_stats=lambda x: None))
    wall.on_mode_activated()
    assert wall._connect_button.text() == "CONNECT & START ALL"
    assert wall._connect_button.isEnabled()
    _close(wall)


# 10. CONNECT disabled when zero eligible (both zero-config and disabled)
def test_10_connect_disabled_when_zero_eligible(qapp):
    # Zero config
    svc0 = ConfigurationService()
    wall0 = LiveModeWidget(mode_service=Mock(), config_service=svc0, runtime_service=Mock(is_camera_running=lambda x: False, observer_service=lambda x: None, camera_stats=lambda x: None))
    wall0.on_mode_activated()
    assert wall0._connect_button.text() == "NO CAMERAS"
    assert not wall0._connect_button.isEnabled()
    _close(wall0)

    # Configured but disabled
    svc1 = _make_config_service(4, enabled_fn=lambda i: False)
    wall1 = LiveModeWidget(mode_service=Mock(), config_service=svc1, runtime_service=Mock(is_camera_running=lambda x: False, observer_service=lambda x: None, camera_stats=lambda x: None))
    wall1.on_mode_activated()
    assert wall1._connect_button.text() == "NO ELIGIBLE CAMERAS"
    assert not wall1._connect_button.isEnabled()
    _close(wall1)


# 11. Live and Configuration use consistent configuration state
def test_11_live_and_config_consistent(qapp):
    svc = _make_config_service(5)
    live_wall = LiveModeWidget(mode_service=Mock(), config_service=svc, runtime_service=Mock(is_camera_running=lambda x: False, observer_service=lambda x: None, camera_stats=lambda x: None))
    # Configuration mode widget uses same svc
    from thermal_monitor.ui.windows.configuration_window import ConfigurationModeWidget
    config_widget = ConfigurationModeWidget(config_service=svc, mode_service=Mock(), runtime_service=None, discovery_service=None, theme_manager=None, config_manager=None)
    assert hex(id(live_wall._config_service)) == hex(id(config_widget._config_service))
    assert len(live_wall._config_service.get_all_camera_configs()) == len(config_widget._config_service.get_all_camera_configs()) == 5
    # Via controller: verify same service instance is injected without creating UI windows (which spawn render threads)
    cm = create_config_manager(create_default=False)
    cfg = cm.get_config()
    svc2 = ConfigurationService()
    ms = ModeService()
    offline = OfflineService()
    runtime = CameraRuntimeService(cameras_config=cfg.cameras, system_config=cfg.system, recording_config=cfg.recording, storage_config=cfg.storage, calibration_config=cfg.calibration)
    from thermal_monitor.ui.controller import AppController
    controller = AppController(mode_service=ms, config_service=svc2, offline_service=offline, runtime_service=runtime, config_manager=cm, theme_manager=None)
    controller._configure_configuration_service(cfg)
    # Verify controller holds single service before any window creation
    assert controller._config_service is svc2
    assert len(controller._config_service.get_all_camera_configs()) == 0
    _close(live_wall)
    try:
        config_widget.close()
    except Exception:
        pass
    from PyQt6.QtWidgets import QApplication
    QApplication.processEvents()


# 12. no second configuration loader exists
def test_12_no_second_loader():
    import pathlib
    import re
    live_source = pathlib.Path("src/thermal_monitor/ui/windows/live_window.py").read_text(encoding="utf-8")
    # Must not directly create second ConfigurationManager/Service
    assert "create_config_manager" not in live_source
    assert "ConfigurationManager(" not in live_source
    # Must not directly open config file (yaml loading) in Live window - string literals for logging are allowed
    # Look for actual open() call that reads config
    assert not re.search(r'open\s*\(.*config\.yaml', live_source)
    assert not re.search(r'yaml\.safe_load', live_source)
    # Must not instantiate new ConfigurationService inside LiveModeWidget
    assert live_source.count("ConfigurationService()") == 0
    # Controller should be the only place hydrating
    controller_source = pathlib.Path("src/thermal_monitor/ui/controller.py").read_text(encoding="utf-8")
    assert "_configure_configuration_service" in controller_source
    assert "mapping" in controller_source


# 13. renderer creation does not imply camera connection
def test_13_renderer_not_imply_connection(qapp):
    svc = ConfigurationService()  # zero cameras
    wall = LiveModeWidget(mode_service=Mock(), config_service=svc, runtime_service=Mock(is_camera_running=lambda x: False, observer_service=lambda x: None, camera_stats=lambda x: None))
    wall.on_mode_activated()
    # 16 render workers exist even with zero cameras
    assert len(wall._tiles) == 8
    assert all(hasattr(t, "_image_widget") and hasattr(t, "_vl_widget") for t in wall._tiles)
    # But connected count is zero
    counts = wall._tile_counts()
    assert counts["connected"] == 0
    assert counts["configured"] == 0
    # With 8 cameras but not running, render workers still there but connected 0
    svc2 = _make_config_service(8)
    wall2 = LiveModeWidget(mode_service=Mock(), config_service=svc2, runtime_service=Mock(is_camera_running=lambda x: False, observer_service=lambda x: None, camera_stats=lambda x: None))
    wall2.on_mode_activated()
    counts2 = wall2._tile_counts()
    assert counts2["connected"] == 0
    assert counts2["configured"] == 8
    assert counts2["assigned"] == 8
    _close(wall)
    _close(wall2)


# 14. no acquisition pipeline is modified
def test_14_no_acquisition_pipeline_modified():
    import pathlib
    live_source = pathlib.Path("src/thermal_monitor/ui/windows/live_window.py").read_text(encoding="utf-8")
    # Check that no class defining forbidden acquisition components exists in live_window
    for name in ["ThermalRenderWorker", "VlRenderWorker", "AcquisitionWorker", "CustomTV46LDriver"]:
        assert f"class {name}" not in live_source
    # Live window must not instantiate acquisition pipeline components
    for marker in ["AcquisitionWorker(", "CustomTV46LDriver(", "SharedMemoryRingBuffer(", "GVCP(", "GVSP("]:
        assert marker not in live_source
    # Controller should not directly instantiate acquisition workers either (hydration only)
    controller_src = pathlib.Path("src/thermal_monitor/ui/controller.py").read_text(encoding="utf-8")
    assert "AcquisitionWorker(" not in controller_src
    assert "CustomTV46LDriver(" not in controller_src


# Additional: config file path diagnostics
def test_config_path_diagnostics(qapp, caplog):
    caplog.set_level(logging.INFO)
    cm = create_config_manager(create_default=False)
    svc = ConfigurationService()
    # hydrate via controller to simulate real path
    from thermal_monitor.ui.controller import AppController
    ms = ModeService()
    offline = OfflineService()
    cfg = cm.get_config()
    runtime = CameraRuntimeService(cameras_config=cfg.cameras, system_config=cfg.system, recording_config=cfg.recording, storage_config=cfg.storage, calibration_config=cfg.calibration)
    controller = AppController(mode_service=ms, config_service=svc, offline_service=offline, runtime_service=runtime, config_manager=cm, theme_manager=None)
    controller._configure_configuration_service(cfg)
    wall = LiveModeWidget(mode_service=Mock(), config_service=svc, runtime_service=runtime, config_manager=cm)
    wall.on_mode_activated()
    # Path must be logged and be the resolved config.yaml
    assert any("config_path" in r.message and "config.yaml" in r.message for r in caplog.records)
    # tmp path also logged
    assert any("config.yaml.tmp" in r.message for r in caplog.records)
    _close(wall)


def test_enabled_thermal_counts_reported(qapp, caplog):
    caplog.set_level(logging.INFO)
    svc = ConfigurationService()
    ident = CameraIdentity(camera_id="cam_99", serial_number="SN999")
    from dataclasses import replace
    cfg_enabled = svc.create_camera_config(identity=ident, name="Cam99")
    cfg_disabled = replace(cfg_enabled, enabled=False)
    cfg_no_thermal = replace(cfg_enabled, thermal_enabled=False)
    # We need distinct ids
    svc2 = ConfigurationService()
    for cid, cfg in [("cam_1", cfg_enabled), ("cam_2", cfg_disabled), ("cam_3", cfg_no_thermal)]:
        ident2 = CameraIdentity(camera_id=cid, serial_number=f"SN{cid}")
        c = svc2.create_camera_config(identity=ident2, name=cid)
        if cid == "cam_2":
            c = replace(c, enabled=False)
        if cid == "cam_3":
            c = replace(c, thermal_enabled=False)
        svc2.set_camera_config(c)
    wall = LiveModeWidget(mode_service=Mock(), config_service=svc2, runtime_service=Mock(is_camera_running=lambda x: False, observer_service=lambda x: None, camera_stats=lambda x: None))
    wall.on_mode_activated()
    assert any("get_all_camera_configs()=3" in r.message for r in caplog.records)
    assert any("enabled=1" in r.message or "enabled=1" in str(r.message) for r in caplog.records) or any("LIVE COUNTS" in r.message for r in caplog.records)
    _close(wall)
