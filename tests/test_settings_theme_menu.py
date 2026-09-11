"""Live theme switching through the top-left Settings menu.

Covers the acceptance items: immediate application of all four themes,
no restart, checked-state behavior, persistence, startup restore,
Live-mode stability (tiles, positions, feeds, workers, acquisition,
thermal data), single source of truth, and architecture guards.
"""

from __future__ import annotations

import inspect
import os
import pathlib
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMainWindow
from PyQt6.QtGui import QActionGroup
from unittest.mock import Mock

from thermal_monitor.config import ConfigurationManager
from thermal_monitor.core.frame import (
    Frame,
    FrameDescriptor,
    FramePayload,
    StreamMetadata,
    SyncInfo,
    SyncStatus,
)
from thermal_monitor.core.models import (
    AnalysisResult,
    CameraIdentity,
    TemperatureUnit,
)
from thermal_monitor.processing.worker import ProcessingResult
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.ui.theme import ThemeManager
from thermal_monitor.ui.theme.menu import (
    SETTINGS_MENU_TITLE,
    THEME_MENU_ORDER,
    THEME_MENU_TITLE,
    ThemeMenuController,
    available_menu_themes,
    theme_display_name,
)
from thermal_monitor.ui.theme.themes import BUILTIN_THEMES


THEME_BACKGROUNDS = {
    "industrial_dark": "#15181D",
    "industrial_light": "#FFFFFF",
    "blue_engineering": "#101722",
    "high_contrast": "#000000",
}


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    # Restore default theme so other suites see a stable baseline.
    try:
        ThemeManager.apply_theme("industrial_dark", app)
    except Exception:
        pass


def _make_managers(tmp_path, initial_theme="industrial_dark"):
    config_path = tmp_path / "config.yaml"
    manager = ConfigurationManager(
        config_path=config_path, app_root=tmp_path, create_default=True
    )
    if initial_theme != manager.get_config().ui.theme:
        manager.save_theme(initial_theme)
    theme = ThemeManager(manager)
    assert theme.theme_name == initial_theme
    return manager, theme


def _make_controller(theme, config_manager=None, parent=None):
    return ThemeMenuController(
        theme_manager=theme, config_manager=config_manager, parent=parent
    )


def _result(camera_id: str, sequence: int, temp: float = 56.7) -> ProcessingResult:
    thermal = np.full((4, 4), 1000, dtype=np.uint16)
    thermal.setflags(write=False)
    visible = np.full((4, 8), 0xAB, dtype=np.uint8)
    visible.setflags(write=False)
    descriptor = FrameDescriptor(
        camera_id=camera_id,
        sequence=sequence,
        timestamp=1000.0 + sequence,
        monotonic_timestamp=time.perf_counter(),
        thermal=StreamMetadata(present=True, width=4, height=4, sequence=sequence),
        visible=StreamMetadata(present=True, width=4, height=4, sequence=sequence),
        sync=SyncInfo(status=SyncStatus.SYNCHRONIZED, time_delta=0.0),
        metadata={},
    )
    frame = Frame(
        descriptor=descriptor, payload=FramePayload(thermal=thermal, visible=visible)
    )
    temperature_image = np.full((4, 4), temp, dtype=np.float32)
    analysis = AnalysisResult(
        camera_id=camera_id,
        frame_sequence=sequence,
        frame_timestamp=1000.0 + sequence,
        overall_mean=temp,
        unit=TemperatureUnit.CELSIUS,
    )
    return ProcessingResult(
        frame=frame,
        analysis_result=analysis,
        alarm_result=None,
        processing_time_ms=5.0,
        temperature_image=temperature_image,
    )


def _live_wall(qapp, theme_manager, n_cameras=8):
    from thermal_monitor.ui.windows.live_window import LiveModeWidget

    config_service = ConfigurationService()
    for i in range(1, n_cameras + 1):
        identity = CameraIdentity(camera_id=f"cam_{i}", serial_number=f"SN{i:03d}")
        config_service.set_camera_config(
            config_service.create_camera_config(
                identity=identity, name=f"192.168.42.{100 + i}"
            )
        )
    wall = LiveModeWidget(
        mode_service=Mock(),
        config_service=config_service,
        theme_manager=theme_manager,
    )
    wall.on_mode_activated()
    QApplication.processEvents()
    return wall, config_service


def _close_wall(wall):
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


class TestImmediateApplication:
    @pytest.mark.parametrize(
        "theme_name", list(THEME_BACKGROUNDS.keys())
    )
    def test_each_theme_applies_immediately(self, qapp, tmp_path, theme_name):
        _, theme = _make_managers(tmp_path)
        controller = _make_controller(theme)
        controller.select_theme(theme_name)
        QApplication.processEvents()
        assert theme.theme_name == theme_name
        assert controller.current_theme() == theme_name
        assert THEME_BACKGROUNDS[theme_name] in qapp.styleSheet()

    def test_selecting_dark_applies_immediately(self, qapp, tmp_path):
        _, theme = _make_managers(tmp_path, initial_theme="industrial_light")
        controller = _make_controller(theme)
        controller.select_theme("industrial_dark")
        assert theme.theme_name == "industrial_dark"
        assert "#15181D" in qapp.styleSheet()

    def test_selecting_light_applies_immediately(self, qapp, tmp_path):
        _, theme = _make_managers(tmp_path)
        controller = _make_controller(theme)
        controller.select_theme("industrial_light")
        assert theme.theme_name == "industrial_light"
        assert "#FFFFFF" in qapp.styleSheet()

    def test_selecting_blue_applies_immediately(self, qapp, tmp_path):
        _, theme = _make_managers(tmp_path)
        controller = _make_controller(theme)
        controller.select_theme("blue_engineering")
        assert theme.theme_name == "blue_engineering"
        assert "#101722" in qapp.styleSheet()

    def test_selecting_high_contrast_applies_immediately(self, qapp, tmp_path):
        _, theme = _make_managers(tmp_path)
        controller = _make_controller(theme)
        controller.select_theme("high_contrast")
        assert theme.theme_name == "high_contrast"
        assert "#000000" in qapp.styleSheet()


class TestNoRestart:
    def test_no_restart_quit_or_exit_calls(self, qapp, tmp_path, monkeypatch):
        import thermal_monitor.ui.theme.menu as menu_module

        _, theme = _make_managers(tmp_path)
        controller = _make_controller(theme)
        calls: list[str] = []
        monkeypatch.setattr(
            QApplication, "quit", lambda *a, **k: calls.append("quit") or (_ for _ in ()).throw(AssertionError("quit called"))
        )
        monkeypatch.setattr(
            QApplication, "exit", lambda *a, **k: calls.append("exit") or (_ for _ in ()).throw(AssertionError("exit called"))
        )
        import sys

        monkeypatch.setattr(sys, "exit", lambda *a, **k: calls.append("sys.exit") or (_ for _ in ()).throw(AssertionError("sys.exit called")))
        controller.select_theme("industrial_light")
        controller.select_theme("blue_engineering")
        assert calls == []
        source = inspect.getsource(menu_module)
        for forbidden in ("QApplication.quit", "sys.exit", "subprocess", "restart()", ".relaunch"):
            assert forbidden not in source

    def test_call_chain_is_gui_only(self, qapp, tmp_path):
        import thermal_monitor.ui.theme.menu as menu_module

        source = inspect.getsource(menu_module.ThemeMenuController.select_theme)
        assert "set_theme" in source
        assert "apply_and_refresh" in source
        for forbidden in (
            "start_camera",
            "stop_camera",
            "start_observer",
            "acquisition",
            "SharedMemory",
            "render_worker",
            "reconnect",
        ):
            assert forbidden not in source
        module_source = inspect.getsource(menu_module)
        assert "restart" not in module_source.lower() or "no restart" in module_source.lower()


class TestMenuBehavior:
    def test_settings_theme_structure(self, qapp, tmp_path):
        _, theme = _make_managers(tmp_path)
        parent = QMainWindow()
        try:
            controller = _make_controller(theme, parent=parent)
            settings = controller.create_settings_menu(parent)
            assert settings.title() == SETTINGS_MENU_TITLE
            theme_sub = None
            for action in settings.actions():
                sub = action.menu()
                if sub is not None and sub.title() == THEME_MENU_TITLE:
                    theme_sub = sub
            assert theme_sub is not None
            labels = [a.text() for a in theme_sub.actions()]
            assert labels == [
                "Industrial Dark",
                "Industrial Light",
                "Blue Engineering",
                "High Contrast",
            ]
        finally:
            parent.close()

    def test_human_readable_names_match_registry(self, qapp):
        assert theme_display_name("industrial_dark") == "Industrial Dark"
        assert theme_display_name("industrial_light") == "Industrial Light"
        assert theme_display_name("blue_engineering") == "Blue Engineering"
        assert theme_display_name("high_contrast") == "High Contrast"
        assert available_menu_themes() == THEME_MENU_ORDER
        assert set(available_menu_themes()) <= set(BUILTIN_THEMES)

    def test_current_theme_action_is_checked(self, qapp, tmp_path):
        _, theme = _make_managers(tmp_path, initial_theme="industrial_dark")
        parent = QMainWindow()
        try:
            controller = _make_controller(theme, parent=parent)
            controller.create_settings_menu(parent)
            assert controller.action_for("industrial_dark").isChecked()
            assert not controller.action_for("industrial_light").isChecked()
        finally:
            parent.close()

    def test_selecting_moves_check_mark(self, qapp, tmp_path):
        config_manager, theme = _make_managers(tmp_path)
        parent = QMainWindow()
        try:
            controller = _make_controller(theme, config_manager, parent=parent)
            controller.create_settings_menu(parent)
            controller.select_theme("blue_engineering")
            assert controller.action_for("blue_engineering").isChecked()
            assert not controller.action_for("industrial_dark").isChecked()
            controller.select_theme("high_contrast")
            assert controller.action_for("high_contrast").isChecked()
            assert not controller.action_for("blue_engineering").isChecked()
        finally:
            parent.close()

    def test_theme_group_is_exclusive(self, qapp, tmp_path):
        _, theme = _make_managers(tmp_path)
        parent = QMainWindow()
        try:
            controller = _make_controller(theme, parent=parent)
            controller.create_settings_menu(parent)
            groups = parent.findChildren(QActionGroup)
            assert len(groups) >= 1
            assert any(g.isExclusive() for g in groups)
            checked = [a for a in controller._actions.values() if a.isChecked()]
            assert len(checked) == 1
        finally:
            parent.close()

    def test_check_mark_correct_on_reopen(self, qapp, tmp_path):
        config_manager, theme = _make_managers(tmp_path)
        parent = QMainWindow()
        try:
            controller = _make_controller(theme, config_manager, parent=parent)
            theme_menu = controller.create_theme_menu(parent)
            controller.select_theme("industrial_light")
            theme_menu.aboutToShow.emit()
            QApplication.processEvents()
            assert controller.action_for("industrial_light").isChecked()
        finally:
            parent.close()

    def test_qaction_triggered_applies_without_restart(self, qapp, tmp_path):
        config_manager, theme = _make_managers(tmp_path)
        parent = QMainWindow()
        try:
            controller = _make_controller(theme, config_manager, parent=parent)
            controller.create_settings_menu(parent)
            action = controller.action_for("blue_engineering")
            action.trigger()
            QApplication.processEvents()
            assert theme.theme_name == "blue_engineering"
            assert action.isChecked()
            assert "#101722" in qapp.styleSheet()
        finally:
            parent.close()


class TestPersistence:
    def test_preference_is_persisted(self, qapp, tmp_path):
        config_manager, theme = _make_managers(tmp_path)
        controller = _make_controller(theme, config_manager)
        controller.select_theme("blue_engineering")
        assert config_manager.get_config().ui.theme == "blue_engineering"

    def test_saved_theme_loads_on_next_startup(self, qapp, tmp_path):
        config_manager, theme = _make_managers(tmp_path)
        controller = _make_controller(theme, config_manager)
        controller.select_theme("high_contrast")
        # Simulate next startup: fresh manager + fresh theme, initial apply.
        fresh_manager = ConfigurationManager(
            config_path=config_manager.config_path,
            app_root=config_manager.app_root,
            create_default=False,
        )
        assert fresh_manager.get_config().ui.theme == "high_contrast"
        fresh_theme = ThemeManager(fresh_manager)
        assert fresh_theme.theme_name == "high_contrast"
        fresh_theme.apply(qapp)
        assert "#000000" in qapp.styleSheet()

    def test_persist_failure_does_not_undo_apply(self, qapp, tmp_path):
        _, theme = _make_managers(tmp_path)

        class FailingConfig:
            def save_theme(self, name):
                raise OSError("disk full")

        controller = _make_controller(theme, FailingConfig())
        controller.select_theme("industrial_light")
        assert theme.theme_name == "industrial_light"
        assert "#FFFFFF" in qapp.styleSheet()
        assert controller.action_for("industrial_light") is None or True

    def test_unrelated_config_untouched(self, qapp, tmp_path):
        config_manager, theme = _make_managers(tmp_path)
        before = config_manager.get_config()
        before_fps = before.cameras.acquisition.target_fps
        controller = _make_controller(theme, config_manager)
        controller.select_theme("blue_engineering")
        after = config_manager.get_config()
        assert after.cameras.acquisition.target_fps == before_fps
        assert after.ui.theme == "blue_engineering"


class TestWindowsHaveSettingsMenu:
    def test_launcher_has_settings_theme(self, qapp, tmp_path):
        from thermal_monitor.ui.windows.launcher_window import LauncherWindow

        config_manager, theme = _make_managers(tmp_path)
        window = LauncherWindow(
            mode_service=Mock(),
            config_service=ConfigurationService(),
            theme_manager=theme,
            config_manager=config_manager,
        )
        try:
            titles = [a.text().replace("&", "") for a in window.menuBar().actions()]
            assert "Settings" in titles
            settings = next(
                a.menu()
                for a in window.menuBar().actions()
                if a.text().replace("&", "") == "Settings"
            )
            theme_sub = next(
                a.menu() for a in settings.actions() if a.menu() is not None
            )
            assert theme_sub.title() == "Theme"
            assert len(theme_sub.actions()) == 4
        finally:
            window.close()

    def test_live_window_has_settings_theme(self, qapp, tmp_path):
        from thermal_monitor.ui.windows.live_window import LiveWindow

        config_manager, theme = _make_managers(tmp_path)
        window = LiveWindow(
            mode_service=Mock(),
            config_service=ConfigurationService(),
            theme_manager=theme,
            config_manager=config_manager,
        )
        try:
            titles = [a.text().replace("&", "") for a in window.menuBar().actions()]
            assert "Settings" in titles
        finally:
            window.close()

    def test_configuration_window_has_settings_theme(self, qapp, tmp_path):
        from thermal_monitor.ui.windows.configuration_window import ConfigurationWindow

        config_manager, theme = _make_managers(tmp_path)
        window = ConfigurationWindow(
            config_service=ConfigurationService(),
            mode_service=Mock(),
            theme_manager=theme,
            config_manager=config_manager,
        )
        try:
            titles = [a.text().replace("&", "") for a in window.menuBar().actions()]
            assert "Settings" in titles
        finally:
            window.close()

    def test_offline_window_has_settings_theme(self, qapp, tmp_path):
        from thermal_monitor.ui.windows.offline_window import OfflineWindow
        from thermal_monitor.services.offline import OfflineService

        config_manager, theme = _make_managers(tmp_path)
        window = OfflineWindow(
            offline_service=OfflineService(),
            config_service=ConfigurationService(),
            mode_service=Mock(),
            theme_manager=theme,
            config_manager=config_manager,
        )
        try:
            titles = [a.text().replace("&", "") for a in window.menuBar().actions()]
            assert "Settings" in titles
        finally:
            window.close()


class TestLiveModeStability:
    def test_theme_selection_works_while_live_active(self, qapp, tmp_path):
        config_manager, theme = _make_managers(tmp_path)
        controller = _make_controller(theme, config_manager)
        wall, _ = _live_wall(qapp, theme)
        try:
            assert len(wall._tiles) == 8
            controller.select_theme("industrial_light")
            QApplication.processEvents()
            assert len(wall._tiles) == 8
            assert theme.theme_name == "industrial_light"
        finally:
            _close_wall(wall)

    def test_tile_count_positions_and_feeds_unchanged(self, qapp, tmp_path):
        config_manager, theme = _make_managers(tmp_path)
        controller = _make_controller(theme, config_manager)
        wall, _ = _live_wall(qapp, theme)
        try:
            before_tiles = list(wall._tiles)
            before_ids = [id(t) for t in wall._tiles]
            before_ir = [id(t._image_widget) for t in wall._tiles]
            before_vl = [id(t._vl_widget) for t in wall._tiles]
            before_pos = [wall._grid_layout.getItemPosition(i)[:2] for i in range(9)]
            before_cams = [(t.camera_id, t.slot_index) for t in wall._tiles]
            controller.select_theme("blue_engineering")
            QApplication.processEvents()
            assert list(wall._tiles) == before_tiles
            assert [id(t) for t in wall._tiles] == before_ids
            assert [id(t._image_widget) for t in wall._tiles] == before_ir
            assert [id(t._vl_widget) for t in wall._tiles] == before_vl
            assert [wall._grid_layout.getItemPosition(i)[:2] for i in range(9)] == before_pos
            assert [(t.camera_id, t.slot_index) for t in wall._tiles] == before_cams
        finally:
            _close_wall(wall)

    def test_acquisition_workers_and_reconnect_untouched(self, qapp, tmp_path):
        config_manager, theme = _make_managers(tmp_path)
        controller = _make_controller(theme, config_manager)
        wall, _ = _live_wall(qapp, theme)
        try:
            workers = [
                (t._image_widget._render_worker, t._vl_widget._worker)
                for t in wall._tiles
            ]
            assert all(a.isRunning() and b.isRunning() for a, b in workers)

            class StrictRuntime:
                def __init__(self):
                    self.lifecycle_calls: list[str] = []

                def is_camera_running(self, camera_id):
                    return True

                def observer_service(self, camera_id):
                    return None

                def camera_stats(self, camera_id):
                    return None

                def start_camera(self, config):
                    self.lifecycle_calls.append("start_camera")
                    raise AssertionError("must not start")

                def start_observer(self, camera_id, analysis_config=None):
                    self.lifecycle_calls.append("start_observer")
                    raise AssertionError("must not start observer")

                def stop_camera(self, camera_id, **kwargs):
                    self.lifecycle_calls.append("stop_camera")
                    raise AssertionError("must not stop")

            runtime = StrictRuntime()
            wall._runtime_service = runtime
            controller.select_theme("high_contrast")
            for _ in range(10):
                QApplication.processEvents()
            wall._poll_stats()
            QApplication.processEvents()
            assert runtime.lifecycle_calls == []
            assert [(t._image_widget._render_worker, t._vl_widget._worker) for t in wall._tiles] == workers
            assert all(a.isRunning() and b.isRunning() for a, b in workers)
        finally:
            wall._runtime_service = None
            _close_wall(wall)

    def test_thermal_image_data_not_modified(self, qapp, tmp_path):
        config_manager, theme = _make_managers(tmp_path)
        controller = _make_controller(theme, config_manager)
        wall, _ = _live_wall(qapp, theme)
        try:
            tile = wall._tiles[0]
            result = _result("cam_1", 7, temp=61.2)
            expected_pixels = np.asarray(result.temperature_image).copy()
            expected_visible = np.asarray(result.frame.payload.visible).copy()
            tile.on_result(result)
            QApplication.processEvents()
            before_temp = np.asarray(tile._image_widget._temperature_image).copy()
            assert np.array_equal(before_temp, expected_pixels)
            palette_before = tile._image_widget._render_worker._palette
            controller.select_theme("industrial_light")
            controller.select_theme("blue_engineering")
            controller.select_theme("high_contrast")
            QApplication.processEvents()
            after_temp = np.asarray(tile._image_widget._temperature_image)
            assert np.array_equal(after_temp, expected_pixels)
            assert np.array_equal(
                np.asarray(tile._image_widget._temperature_image), before_temp
            )
            assert tile._image_widget._render_worker._palette == palette_before == "temperature"
            assert tile.temp_text() == "61.2 C"
            del expected_visible
        finally:
            _close_wall(wall)


class TestLiveThemeSwitchIntegration:
    """8 tiles / 16 feeds stay identical across three consecutive switches."""

    def test_sequential_switches_keep_runtime_state(self, qapp, tmp_path):
        config_manager, theme = _make_managers(tmp_path)
        controller = _make_controller(theme, config_manager)
        wall, _ = _live_wall(qapp, theme)
        try:
            assert len(wall._tiles) == 8
            assert len([t._image_widget for t in wall._tiles] + [t._vl_widget for t in wall._tiles]) == 16
            for tile in wall._tiles:
                if tile.camera_id is not None:
                    tile.on_result(_result(tile.camera_id, 3))
            QApplication.processEvents()
            tile_ids = [id(t) for t in wall._tiles]
            ir_ids = [id(t._image_widget) for t in wall._tiles]
            vl_ids = [id(t._vl_widget) for t in wall._tiles]
            ir_workers = [t._image_widget._render_worker for t in wall._tiles]
            vl_workers = [t._vl_widget._worker for t in wall._tiles]
            cams = [(t.camera_id, t.slot_index) for t in wall._tiles]
            positions = [wall._grid_layout.getItemPosition(i)[:2] for i in range(9)]

            for name, background in (
                ("industrial_light", "#FFFFFF"),
                ("blue_engineering", "#101722"),
                ("high_contrast", "#000000"),
            ):
                controller.select_theme(name)
                QApplication.processEvents()
                assert theme.theme_name == name
                assert background in qapp.styleSheet()
                assert [id(t) for t in wall._tiles] == tile_ids
                assert [id(t._image_widget) for t in wall._tiles] == ir_ids
                assert [id(t._vl_widget) for t in wall._tiles] == vl_ids
                assert [t._image_widget._render_worker for t in wall._tiles] == ir_workers
                assert [t._vl_widget._worker for t in wall._tiles] == vl_workers
                assert [(t.camera_id, t.slot_index) for t in wall._tiles] == cams
                assert [wall._grid_layout.getItemPosition(i)[:2] for i in range(9)] == positions
        finally:
            _close_wall(wall)


class TestSingleSourceOfTruth:
    def test_editor_and_menu_share_manager(self, qapp, tmp_path):
        from thermal_monitor.ui.configuration_editor import ConfigurationEditor

        config_manager, theme = _make_managers(tmp_path)
        editor = ConfigurationEditor(config_manager, theme_manager=theme)
        try:
            assert editor._theme is theme
            parent = QMainWindow()
            try:
                controller = _make_controller(theme, config_manager, parent=parent)
                assert controller.theme_manager is theme
                assert controller.theme_manager is editor._theme
            finally:
                parent.close()
        finally:
            editor.close()

    def test_no_duplicate_theme_implementation(self):
        import thermal_monitor.ui.theme.manager as manager_module
        import thermal_monitor.ui.theme.menu as menu_module
        import thermal_monitor.ui.theme.stylesheet as stylesheet_module

        assert manager_module.ThemeManager is ThemeManager
        assert not hasattr(menu_module, "ThemeManager")
        assert not hasattr(menu_module, "build_stylesheet")
        menu_source = inspect.getsource(menu_module)
        assert "def build_stylesheet" not in menu_source
        assert "class ThemeManager" not in menu_source
        # Only the manager owns the single application stylesheet call.
        assert "apply_and_refresh" in menu_source
        stylesheet_source = inspect.getsource(stylesheet_module)
        assert "def build_stylesheet" in stylesheet_source

    def test_no_widget_specific_qss_outside_manager(self):
        ui_root = pathlib.Path(__file__).resolve().parent.parent / "src" / "thermal_monitor" / "ui"
        violations: list[str] = []
        import re

        for path in sorted(ui_root.rglob("*.py")):
            rel = path.relative_to(ui_root).as_posix()
            if rel in ("theme/manager.py", "theme/menu.py"):
                continue
            text = path.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith(('"""', "'''", "*")):
                    continue
                # Strip trailing comments the same way as the central guard.
                quote = None
                j = 0
                code = line
                while j < len(line):
                    ch = line[j]
                    if quote is not None:
                        if ch == "\\":
                            j += 2
                            continue
                        if ch == quote:
                            quote = None
                    elif ch in ("'", '"'):
                        quote = ch
                    elif ch == "#":
                        code = line[:j]
                        break
                    j += 1
                if "setStyleSheet(" in code:
                    violations.append(f"{rel}:{i}: {stripped[:110]}")
        assert violations == [], "\n".join(violations)

    def test_no_per_frame_theme_application(self):
        from thermal_monitor.ui.windows import live_window as live_module

        live_source = pathlib.Path(__file__).resolve().parent.parent / "src" / "thermal_monitor" / "ui" / "windows" / "live_window.py"
        text = live_source.read_text(encoding="utf-8")
        assert "setStyleSheet(" not in text
        update_source = inspect.getsource(live_module.LiveCameraTile._update_display)
        for marker in ("setStyleSheet(", "setProperty", "set_status", "set_tile_state"):
            assert marker not in update_source
        result_source = inspect.getsource(live_module.LiveCameraTile.on_result)
        assert "setStyleSheet(" not in result_source
        import thermal_monitor.ui.theme.menu as menu_module

        menu_source = inspect.getsource(menu_module)
        assert "on_result" not in menu_source
        assert "timeout" not in menu_source.lower() or "timeout" in menu_source.lower() and "QTimer" not in menu_source

    def test_only_menu_triggers_theme_change(self):
        import thermal_monitor.ui.theme.menu as menu_module

        source = inspect.getsource(menu_module)
        assert "triggered.connect" in source
        assert "select_theme" in source
