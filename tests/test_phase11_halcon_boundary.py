"""Phase 11 HALCON boundary tests.

- ROI domain + HALCON geometry layers import without PyQt6 (GUI-thread
  separation by construction, verified in a subprocess).
- Geometry conversion failures surface as controlled RoiValidationError.
- No HALCON runtime call happens for pure interaction math.
"""

import subprocess
import sys

import pytest

from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.roi.errors import RoiValidationError
from thermal_monitor.roi.geometry import CircleGeometry, RectangleGeometry

_GUI_FREE_MODULES = [
    "thermal_monitor.roi.models",
    "thermal_monitor.roi.geometry",
    "thermal_monitor.roi.evaluator",
    "thermal_monitor.roi.measurements",
    "thermal_monitor.roi.loading",
    "thermal_monitor.roi.repository",
    "thermal_monitor.roi.io",
    "thermal_monitor.halcon.geometry",
    "thermal_monitor.halcon.adapter",
]


def test_domain_and_halcon_layers_import_without_pyqt():
    joined = ",".join(_GUI_FREE_MODULES)
    script = (
        "import sys; "
        f"mods = '{joined}'; "
        "[__import__(m) for m in mods.split(',')]; "
        "bad = [m for m in sys.modules if m == 'PyQt6' or m.startswith('PyQt6.')]; "
        "assert not bad, bad; print('gui-free ok')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        cwd=".", timeout=120)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "gui-free ok" in completed.stdout


def test_halcon_conversion_rejects_degenerate_geometry():
    from thermal_monitor.halcon.geometry import halcon_params_for
    with pytest.raises(RoiValidationError):
        halcon_params_for(
            RoiObjectType.RECTANGLE,
            RectangleGeometry(row1=5, col1=5, row2=5, col2=5))


def test_halcon_conversion_rejects_unknown_type():
    from thermal_monitor.halcon.geometry import halcon_params_for
    with pytest.raises(RoiValidationError):
        halcon_params_for("teleporter", CircleGeometry(
            center_row=1, center_col=1, radius=2))


def test_interaction_math_needs_no_halcon_runtime():
    # Pure geometry manipulation runs without touching the HALCON
    # package at all (verified structurally, order-independent).
    import ast
    from pathlib import Path
    tree = ast.parse(Path("src/thermal_monitor/roi/handles.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "halcon" not in imported
    from thermal_monitor.roi.handles import move_geometry
    moved = move_geometry(CircleGeometry(center_row=1, center_col=1, radius=2),
                          1.0, 1.0)
    assert moved.center_col == 2.0
