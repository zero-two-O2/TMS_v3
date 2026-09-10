"""Stage 8D Phase 4 tests: focus Configuration UI (panel + async worker).

No hardware required. The panel is a dumb view (signals only); FocusWorker
drives a fake runtime off the GUI thread. Window wiring (signal connections,
stale-result tokens, cleanup) mirrors the tested panel/worker contracts.
"""

from __future__ import annotations

import time
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication

from thermal_monitor.ui.widgets.image_acquisition_panel import ImageAcquisitionPanel
from thermal_monitor.ui.windows.configuration_window import FocusWorker


@pytest.fixture
def qapp():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


class TestFocusPanel:
    def test_initially_disabled(self, qapp):
        panel = ImageAcquisitionPanel()
        assert not panel._focus_group.isEnabled()

    def test_enable_and_state(self, qapp):
        panel = ImageAcquisitionPanel()
        panel.set_focus_enabled(True)
        panel.set_focus_state(362, 150, 1000000)
        assert panel._focus_current_label.text() == "362 mm"
        assert "150" in panel._focus_range_label.text()
        assert "1000000" in panel._focus_range_label.text()
        assert panel._focus_spin.minimum() == 150
        assert panel._focus_spin.maximum() == 1000000

    def test_apply_emits_requested_value(self, qapp):
        panel = ImageAcquisitionPanel()
        panel.set_focus_enabled(True)
        panel.set_focus_state(362, 150, 1000000)
        seen: list[int] = []
        panel.focus_set_requested.connect(seen.append)
        panel._focus_spin.setValue(2500)
        panel._focus_apply_btn.click()
        assert seen == [2500]

    def test_refresh_button_emits(self, qapp):
        panel = ImageAcquisitionPanel()
        panel.set_focus_enabled(True)
        seen: list = []
        panel.focus_refresh_requested.connect(lambda: seen.append(True))
        panel._focus_refresh_btn.click()
        assert seen == [True]

    def test_busy_disables_result_reenables(self, qapp):
        panel = ImageAcquisitionPanel()
        panel.set_focus_enabled(True)
        panel.set_focus_busy("Writing…")
        assert not panel._focus_apply_btn.isEnabled()
        panel.set_focus_result(2500, 2500)
        assert panel._focus_apply_btn.isEnabled()
        assert "2500" in panel._focus_status_label.text()

    def test_motor_offset_reported_not_hidden(self, qapp):
        panel = ImageAcquisitionPanel()
        panel.set_focus_enabled(True)
        panel.set_focus_result(4800, 4765)
        assert "4765" in panel._focus_status_label.text()
        assert "4800" in panel._focus_status_label.text()

    def test_error_reported_and_reenabled(self, qapp):
        panel = ImageAcquisitionPanel()
        panel.set_focus_enabled(True)
        panel.set_focus_busy()
        panel.set_focus_error("boom")
        assert panel._focus_apply_btn.isEnabled()
        assert "boom" in panel._focus_status_label.text()

    def test_disable_with_reason(self, qapp):
        panel = ImageAcquisitionPanel()
        panel.set_focus_enabled(False, "Camera not running")
        assert "not running" in panel._focus_status_label.text()


class FakeRuntime:
    def __init__(self, vmin=150, vmax=1000000, current=362, fail=None):
        self._limits = (vmin, vmax)
        self._current = current
        self._fail = fail
        self.calls: list = []

    def get_focus_limits(self, camera_id):
        self.calls.append("limits")
        if self._fail:
            raise self._fail
        return self._limits

    def get_focus_mm(self, camera_id):
        self.calls.append("read")
        if self._fail:
            raise self._fail
        return self._current

    def set_focus_mm(self, camera_id, value):
        self.calls.append(("write", value))
        if self._fail:
            raise self._fail
        vmin, vmax = self._limits
        if not vmin <= value <= vmax:
            raise ValueError("out of range")
        self._current = value
        return value


class TestFocusWorker:
    def test_read_path(self, qapp):
        runtime = FakeRuntime()
        worker = FocusWorker(runtime, "cam1", None)
        got: list = []
        worker.read_finished.connect(lambda *a: got.append(a))
        worker.run()
        assert got == [("cam1", 150, 1000000, 362)]

    def test_write_path(self, qapp):
        runtime = FakeRuntime()
        worker = FocusWorker(runtime, "cam1", 2500)
        got: list = []
        worker.write_finished.connect(lambda *a: got.append(a))
        worker.run()
        assert got == [("cam1", 2500, 2500)]

    def test_failure_path(self, qapp):
        runtime = FakeRuntime(fail=RuntimeError("GVCP timeout"))
        worker = FocusWorker(runtime, "cam1", None)
        got: list = []
        worker.failed.connect(lambda *a: got.append(a))
        worker.run()
        assert len(got) == 1 and got[0][0] == "cam1" and "timeout" in got[0][1]

    def test_runs_off_gui_thread(self, qapp):
        """The blocking GVCP call must not execute on the GUI thread."""
        from PyQt6.QtCore import QThread

        runtime = FakeRuntime()
        gui_thread = qapp.thread()
        seen: dict = {}
        worker = FocusWorker(runtime, "cam1", None)
        orig = runtime.get_focus_mm

        def slow(camera_id):
            time.sleep(0.2)
            seen["thread"] = QThread.currentThread()
            return orig(camera_id)

        runtime.get_focus_mm = slow
        thread = QThread()
        worker.moveToThread(thread)
        done: list = []
        worker.read_finished.connect(lambda *a: done.append(a))
        worker.read_finished.connect(thread.quit)
        thread.started.connect(worker.run)
        thread.start()
        deadline = time.time() + 5.0
        while not done and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        thread.wait(2000)
        assert done, "worker never delivered"
        assert seen["thread"] is not gui_thread
