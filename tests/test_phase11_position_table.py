"""Phase 11 table tests: corrected Position Table behavior.

- No ROI Set column; operator columns + read-only ROI count.
- Selection never moves the PTZ (no signal on select).
- Go To emits the exact selected position id.
- Active (reached) marker independent of selection.
- Rename/delete/save/refresh/import/export entry points intact.
"""

import pytest

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtWidgets import QApplication  # noqa: E402

from thermal_monitor.ptz.positions import PtzPosition  # noqa: E402
from thermal_monitor.ui.widgets.ptz_position_table import (  # noqa: E402
    PtzPositionTablePanel,
)


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _positions():
    return [
        PtzPosition(position_id="pos_1", camera_id="cam_A", ptz_id="PTZ_01",
                    name="Furnace", pan=90.0, tilt=90.0, velocity=10.0),
        PtzPosition(position_id="pos_2", camera_id="cam_A", ptz_id="PTZ_01",
                    name="Door", pan=60.3, tilt=65.9, velocity=10.0),
    ]


def _panel(qapp):
    panel = PtzPositionTablePanel()
    panel.set_station("cam_A", "PTZ_01")
    panel.set_positions(_positions())
    return panel


def test_no_roi_set_column(qapp):
    panel = _panel(qapp)
    headers = [panel._tree.headerItem().text(i)
               for i in range(panel._tree.columnCount())]
    assert headers == ["Name", "Pan", "Tilt", "Velocity", "ROIs", "Enabled"]
    assert "ROI set" not in headers
    assert "roi_set_ref" not in " ".join(headers).lower()


def test_roi_count_column_read_only(qapp):
    panel = _panel(qapp)
    assert panel._tree.topLevelItem(0).text(4) == "—"  # unknown, not zero
    panel.set_roi_counts({"pos_1": 3, "pos_2": 0})
    assert panel._tree.topLevelItem(0).text(4) == "3"
    assert panel._tree.topLevelItem(1).text(4) == "0"


def test_selection_does_not_move_ptz(qapp):
    panel = _panel(qapp)
    moved = []
    panel.goto_requested.connect(moved.append)
    panel._tree.topLevelItem(0).setSelected(True)
    assert panel.selected_position_id() == "pos_1"
    assert moved == []  # row click alone never commands motion


def test_goto_uses_exact_selected_position(qapp):
    panel = _panel(qapp)
    moved = []
    panel.goto_requested.connect(moved.append)
    panel._tree.topLevelItem(1).setSelected(True)
    panel._goto_btn.click()
    assert moved == ["pos_2"]


def test_active_marker_independent_of_selection(qapp):
    panel = _panel(qapp)
    panel.set_active_position("pos_2")
    assert panel._tree.topLevelItem(1).text(0).startswith("● ")
    assert not panel._tree.topLevelItem(0).text(0).startswith("● ")
    assert panel.selected_position_id() is None
    panel._tree.topLevelItem(0).setSelected(True)
    assert panel._tree.topLevelItem(1).text(0).startswith("● ")


def test_station_change_clears_active_and_counts(qapp):
    panel = _panel(qapp)
    panel.set_active_position("pos_1")
    panel.set_roi_count("pos_1", 5)
    panel.set_station("cam_B", "PTZ_02")
    assert panel.active_position_id is None
    assert panel._tree.topLevelItemCount() == 0


def test_edit_rois_signal_replaces_associate(qapp):
    panel = _panel(qapp)
    assert not hasattr(panel, "roi_associate_requested")
    requested = []
    panel.edit_rois_requested.connect(requested.append)
    panel._tree.topLevelItem(0).setSelected(True)
    panel._roi_btn.click()
    assert requested == ["pos_1"]


def test_rename_save_delete_refresh_wiring(qapp):
    panel = _panel(qapp)
    panel._tree.topLevelItem(0).setSelected(True)
    assert panel._delete_btn.isEnabled()
    assert panel._rename_btn.isEnabled()
    assert panel._goto_btn.isEnabled()
    assert panel._save_btn.isEnabled()
    assert panel._export_btn.isEnabled() and panel._import_btn.isEnabled()
