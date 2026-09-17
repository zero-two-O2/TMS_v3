"""Phase 8: GUI guards, re-apply, REPLACE dialog, panel label (offscreen)."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

import thermal_monitor.camera.source  # noqa: F401 (camera package first)
import thermal_monitor.ui.theme.fonts as fonts
import thermal_monitor.ui.windows.configuration_window as mod
from thermal_monitor.core.models import CameraConfig, CameraIdentity
from thermal_monitor.ptz.roi_activation import ActivePositionContext
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.mode import ModeService
from thermal_monitor.ui.windows.configuration_window import ConfigurationModeWidget


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setattr(mod, "_DOCK_SETTINGS_ORG", "TMS-Test-Org")
    monkeypatch.setattr(mod, "_DOCK_SETTINGS_APP", "TMS-Test-RetargetGui")
    monkeypatch.setattr(fonts, "FONT_SETTINGS_ORG", "TMS-Test-Org")
    monkeypatch.setattr(fonts, "FONT_SETTINGS_APP", "TMS-Test-RetargetGuiFonts")
    from PyQt6.QtCore import QSettings

    QSettings("TMS-Test-Org", "TMS-Test-RetargetGui").clear()
    yield
    QSettings("TMS-Test-Org", "TMS-Test-RetargetGui").clear()


@pytest.fixture
def widget(qapp):
    service = ConfigurationService()
    service.set_camera_config(
        CameraConfig(identity=CameraIdentity(camera_id="camA", serial_number="SN-A"))
    )
    w = ConfigurationModeWidget(
        config_service=service, mode_service=ModeService(), runtime_service=None
    )
    w.show()
    qapp.processEvents()
    yield w
    w.close()


def _make_result(camera_id, sequence, context_generation=None):
    frame = SimpleNamespace(
        descriptor=SimpleNamespace(
            camera_id=camera_id,
            sequence=sequence,
            timestamp=1234.5,
            monotonic_timestamp=time.perf_counter(),
            thermal=SimpleNamespace(sequence=sequence, width=640, height=480),
            visible=SimpleNamespace(sequence=sequence),
        ),
        payload=SimpleNamespace(
            thermal=np.zeros((480, 640), dtype=np.uint16),
            visible=None,
        ),
    )
    analysis = None
    if context_generation is not None:
        from thermal_monitor.core.models import AnalysisResult, TemperatureUnit

        analysis = AnalysisResult(
            camera_id=camera_id,
            frame_sequence=sequence,
            frame_timestamp=1234.5,
            roi_results={},
            overall_min=20.0,
            overall_max=30.0,
            overall_mean=25.0,
            unit=TemperatureUnit.CELSIUS,
            processing_time_ms=1.0,
            metadata={
                "position_id": "set_a",
                "context_generation": context_generation,
            },
        )
    return SimpleNamespace(
        frame=frame,
        analysis_result=analysis,
        alarm_result=None,
        processing_time_ms=1.0,
        temperature_image=np.full((480, 640), 25.0, dtype=np.float64),
    )


def _activate(widget, camera_id="camA", generation=3):
    widget._ptz_registry.set(
        ActivePositionContext(
            camera_id=camera_id,
            ptz_id="PTZ_01",
            position_id="pos_1",
            position_name="F",
            roi_set_ref="set_a",
            roi_ids=("roi_1",),
            context_generation=generation,
            session_generation=widget._session.generation,
        )
    )


class TestResultGuard:
    def test_no_context_passes_through(self, widget, qapp):
        widget._selected_camera_id = "camA"
        result = _make_result("camA", 10, context_generation=9)
        widget._on_processing_result(result)
        assert widget._latest_result is result

    def test_matching_generation_passes(self, widget, qapp):
        widget._selected_camera_id = "camA"
        _activate(widget, generation=3)
        result = _make_result("camA", 11, context_generation=3)
        widget._on_processing_result(result)
        assert widget._latest_result is result

    def test_mismatched_generation_dropped(self, widget, qapp):
        widget._selected_camera_id = "camA"
        _activate(widget, generation=3)
        before = widget._stale_results_dropped
        widget._on_processing_result(_make_result("camA", 12, context_generation=1))
        assert widget._stale_results_dropped == before + 1

    def test_legacy_result_passes_with_context(self, widget, qapp):
        widget._selected_camera_id = "camA"
        _activate(widget, generation=3)
        result = _make_result("camA", 13, context_generation=None)
        widget._on_processing_result(result)
        assert widget._latest_result is result


class TestSessionClearing:
    def test_begin_session_clears_registry(self, widget, qapp):
        _activate(widget, generation=3)
        assert widget._ptz_registry.get("camA") is not None
        widget._begin_session("camA")
        qapp.processEvents()
        assert widget._ptz_registry.get("camA") is None


class FakeObserver:
    def __init__(self, camera_id="camA") -> None:
        self.camera_id = camera_id
        self.published: list = []

    def set_active_position(self, position_id, generation=None):
        self.published.append((position_id, generation))
        return generation if generation is not None else 1

    @property
    def active_position(self):
        return self.published[-1] if self.published else ("default", 0)


class TestReapply:
    def test_reapply_matching(self, widget):
        widget._selected_camera_id = "camA"
        _activate(widget, generation=4)
        observer = FakeObserver()
        widget._observer = observer
        try:
            assert widget._ptz_reapply_context("camA") is True
            assert observer.published == [("set_a", 4)]
        finally:
            widget._observer = None

    def test_reapply_no_observer(self, widget):
        widget._selected_camera_id = "camA"
        _activate(widget, generation=4)
        widget._observer = None
        assert widget._ptz_reapply_context("camA") is False

    def test_reapply_wrong_camera(self, widget):
        widget._selected_camera_id = "camA"
        _activate(widget, generation=4)
        observer = FakeObserver(camera_id="camB")
        widget._observer = observer
        try:
            assert widget._ptz_reapply_context("camA") is False
            assert observer.published == []
        finally:
            widget._observer = None

    def test_reapply_stale_session(self, widget):
        widget._selected_camera_id = "camA"
        _activate(widget, generation=4)
        widget._session.renew("camA")  # epoch bump; registry now stale
        observer = FakeObserver()
        widget._observer = observer
        try:
            assert widget._ptz_reapply_context("camA") is False
            assert observer.published == []
        finally:
            widget._observer = None


class TestReplaceDialog:
    def test_confirm_yes(self, widget, qapp, monkeypatch):
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
        )
        done = threading.Event()
        answer: dict = {}
        widget._selected_camera_id = "camA"
        gen = widget._session.generation
        widget._on_ptz_import_confirm(
            "camA", gen, {"entries": ["Furnace (pos_1)"], "done": done, "answer": answer}
        )
        assert done.is_set()
        assert answer.get("confirmed") is True

    def test_confirm_no(self, widget, qapp, monkeypatch):
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *args, **kwargs: QMessageBox.StandardButton.No,
        )
        done = threading.Event()
        answer: dict = {}
        widget._selected_camera_id = "camA"
        gen = widget._session.generation
        widget._on_ptz_import_confirm(
            "camA", gen, {"entries": ["Furnace (pos_1)"], "done": done, "answer": answer}
        )
        assert answer.get("confirmed") is False

    def test_confirm_stale_auto_cancels(self, widget, qapp, monkeypatch):
        called: list = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *args, **kwargs: called.append(True) or QMessageBox.StandardButton.Yes,
        )
        done = threading.Event()
        answer: dict = {}
        widget._selected_camera_id = "camA"
        widget._on_ptz_import_confirm(
            "camA", widget._session.generation + 99,
            {"entries": ["x"], "done": done, "answer": answer},
        )
        assert called == []
        assert done.is_set()
        assert answer.get("confirmed") is False


class TestActiveLabel:
    def test_label_updates_and_clears(self, widget, qapp):
        widget._selected_camera_id = "camA"
        gen = widget._session.generation
        widget._on_ptz_active("camA", gen, "Furnace")
        assert widget._ptz_panel._active_label.text() == "Furnace"
        widget._on_ptz_active("camA", gen + 99, "Stale")
        assert widget._ptz_panel._active_label.text() == "Furnace"
        widget._ptz_panel.clear()
        assert widget._ptz_panel._active_label.text() == "—"
