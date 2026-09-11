"""
tests.test_theme_system -- Tests for the centralized theme architecture.

Covers: theme registry, token definitions, central QSS generation,
runtime switching, semantic property helpers, and the architectural
rule that application chrome lives centrally (no per-widget stylesheets
with literal colors outside documented exceptions).
"""

from __future__ import annotations

import pathlib

import pytest
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

from thermal_monitor.config import create_config_manager
from thermal_monitor.ui.theme import (
    BUILTIN_THEMES,
    DEFAULT_THEME_NAME,
    ThemeManager,
    apply_theme,
    build_stylesheet,
    get_builtin_theme,
)
from thermal_monitor.ui.theme.properties import (
    refresh_all_widgets,
    set_dirty,
    set_role,
    set_status,
    set_tile_state,
    set_variant,
)
from thermal_monitor.ui.theme.tokens import (
    BUTTON_VARIANTS,
    LABEL_ROLES,
    STATUS_VALUES,
    TILE_STATES,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class TestThemeRegistry:
    def test_default_theme_name(self) -> None:
        assert DEFAULT_THEME_NAME == "industrial_dark"

    def test_builtin_themes_present(self) -> None:
        assert set(BUILTIN_THEMES) == {
            "industrial_dark",
            "industrial_light",
            "blue_engineering",
            "high_contrast",
        }

    def test_available_themes_include_legacy(self) -> None:
        available = ThemeManager.available_themes()
        for name in ("industrial_dark", "industrial_light", "blue_engineering",
                     "high_contrast", "light", "dark", "system"):
            assert name in available

    def test_unknown_theme_lookup_raises(self) -> None:
        with pytest.raises(KeyError):
            get_builtin_theme("does_not_exist")

    def test_set_unknown_theme_raises(self) -> None:
        theme = ThemeManager(create_config_manager())
        with pytest.raises(ValueError):
            theme.set_theme("does_not_exist")

    def test_every_definition_has_required_tokens(self) -> None:
        required = (
            "background", "surface", "surface_alt", "border", "text",
            "text_secondary", "muted_text", "primary", "secondary",
            "accent", "success", "warning", "danger", "info",
            "camera_connected", "camera_disconnected", "camera_warning",
            "camera_error", "thermal_hot", "thermal_cold", "overlay_roi",
        )
        for name, definition in BUILTIN_THEMES.items():
            for token in required:
                value = getattr(definition, token)
                assert isinstance(value, str) and value.startswith("#"), (name, token)

    def test_controlled_vocabularies(self) -> None:
        assert set(BUTTON_VARIANTS) == {
            "primary", "secondary", "accent", "danger", "outline", "ghost"}
        for role in ("title", "subtitle", "status", "muted", "mono", "readout"):
            assert role in LABEL_ROLES
        for status in ("connected", "disconnected", "error", "starting",
                       "running", "not_available"):
            assert status in STATUS_VALUES
        assert set(TILE_STATES) == {"starting", "running", "error", "not_available"}


class TestStylesheetGeneration:
    def test_all_themes_build_distinct_stylesheets(self) -> None:
        sheets = {name: build_stylesheet(defn) for name, defn in BUILTIN_THEMES.items()}
        assert len(set(sheets.values())) == len(sheets)

    def test_stylesheet_covers_required_components(self) -> None:
        sheet = build_stylesheet(BUILTIN_THEMES["industrial_dark"])
        for selector in (
            "QPushButton", "QToolButton", "QLabel", "QLineEdit", "QComboBox",
            "QSpinBox", "QDoubleSpinBox", "QCheckBox", "QRadioButton",
            "QSlider", "QTabWidget", "QTableWidget", "QGroupBox",
            "QScrollArea", "QProgressBar", "QListWidget", "QTreeWidget",
            "QDialog", "QMenu", "QToolTip", "QStatusBar", "QSplitter",
            "QHeaderView", "QScrollBar",
        ):
            assert selector in sheet, selector

    def test_stylesheet_covers_semantic_selectors(self) -> None:
        sheet = build_stylesheet(BUILTIN_THEMES["industrial_dark"])
        for variant in BUTTON_VARIANTS:
            assert f'[variant="{variant}"]' in sheet, variant
        for role in LABEL_ROLES:
            assert f'[role="{role}"]' in sheet, role
        for status in ("connected", "disconnected", "connecting", "error",
                       "warning", "starting", "running", "not_available"):
            assert f'[status="{status}"]' in sheet, status
        for state in TILE_STATES:
            assert f'[tileState="{state}"]' in sheet, state
        assert '[dirty="true"]' in sheet

    def test_stylesheet_has_no_gradients_or_shadows(self) -> None:
        for name, definition in BUILTIN_THEMES.items():
            sheet = build_stylesheet(definition).lower()
            assert "qlineargradient" not in sheet, name
            assert "gradient" not in sheet, name
            assert "box-shadow" not in sheet, name


class TestThemeSwitching:
    def test_set_theme_changes_active_colors(self) -> None:
        theme = ThemeManager(create_config_manager())
        assert theme.theme_name == "industrial_dark"
        previous = theme.set_theme("industrial_light")
        assert previous == "industrial_dark"
        assert theme.theme_name == "industrial_light"
        assert theme.background() == "#FFFFFF"

    def test_set_theme_updates_live_tile_colors(self) -> None:
        theme = ThemeManager(create_config_manager())
        theme.set_theme("blue_engineering")
        assert theme.live_tile_running_bg() == theme.success()

    def test_refresh_preserves_explicit_theme(self) -> None:
        theme = ThemeManager(create_config_manager())
        theme.set_theme("high_contrast")
        theme.refresh()
        assert theme.theme_name == "high_contrast"

    def test_apply_theme_classmethod(self, app) -> None:
        manager = ThemeManager.apply_theme("industrial_dark", app)
        assert manager.theme_name == "industrial_dark"
        assert "Industrial Dark" in app.styleSheet() or "#15181D" in app.styleSheet()

    def test_module_level_apply_theme(self, app) -> None:
        manager = apply_theme("blue_engineering", app)
        assert "#101722" in app.styleSheet()
        # Restore default for other tests
        apply_theme("industrial_dark", app)

    def test_apply_and_refresh_counts_widgets(self, app) -> None:
        theme = ThemeManager(create_config_manager())
        count = theme.apply_and_refresh(app)
        assert count >= 0
        assert "#15181D" in app.styleSheet()

    def test_switching_does_not_require_config(self) -> None:
        theme = ThemeManager(None)
        assert theme.theme_name == "industrial_dark"
        theme.set_theme("high_contrast")
        assert theme.text() == "#FFFFFF"


class TestPropertyHelpers:
    def test_variant_helper(self, app) -> None:
        btn = QPushButton("x")
        set_variant(btn, "danger")
        assert btn.property("variant") == "danger"

    def test_status_helper(self, app) -> None:
        label = QLabel("x")
        set_status(label, "connected")
        assert label.property("status") == "connected"

    def test_role_helper(self, app) -> None:
        label = QLabel("x")
        set_role(label, "readout")
        assert label.property("role") == "readout"

    def test_tile_state_helper(self, app) -> None:
        label = QLabel("x")
        set_tile_state(label, "error")
        assert label.property("tileState") == "error"

    def test_dirty_helper(self, app) -> None:
        btn = QPushButton("x")
        set_dirty(btn, True)
        assert btn.property("dirty") is True
        set_dirty(btn, False)
        assert btn.property("dirty") is False

    def test_refresh_all_widgets_runs(self, app) -> None:
        assert refresh_all_widgets(app) >= 0


class TestChromeIsCentralized:
    """Static guard: chrome modules must not embed stylesheets/colors.

    Documented exceptions (thermal/image data or paint-code chrome with
    central fallbacks) are allow-listed with reasons below.
    """

    UI_ROOT = pathlib.Path(__file__).resolve().parent.parent / "src" / "thermal_monitor" / "ui"

    # module -> literal-kind roots permitted there, with reasons
    ALLOW = {
        # Thermal palettes + viewport paint code: image data, not chrome.
        "modes/observer_image.py": ("QColor", "#"),
        # VL viewport paint code: image data, not chrome.
        "modes/vl_image.py": ("QColor", "#"),
        # Temperature legend palettes: thermal data, not chrome.
        # (Legend border/tick chrome reads the theme with central fallbacks.)
        "widgets/thermal_scale_panel.py": ("QColor", "#"),
        # Offline viewport paint + ROI overlay default: image data.
        "windows/offline_window.py": ("QColor",),
        # ROI overlay default color: image overlay data (unchanged behavior).
        "windows/configuration_window.py": ("QColor", "#"),
        # The theme system itself owns all literals by design.
        "theme/tokens.py": ("#",),
        "theme/themes/industrial_dark.py": ("#",),
        "theme/themes/industrial_light.py": ("#",),
        "theme/themes/blue_engineering.py": ("#",),
        "theme/themes/high_contrast.py": ("#",),
        # The single central QApplication.setStyleSheet call + legacy
        # config-derived color math live here by design.
        "theme/manager.py": ("#", "setStyleSheet"),
    }

    # marker -> allow-list root.  Hex colors are matched as real CSS/color
    # literals (not "#" in comments or table headers); rgb()/rgba() only
    # when not part of a longer identifier (e.g. yuyv_to_rgb); QColor()
    # only when constructed from a literal rather than a theme accessor.
    LITERAL_MARKERS = {
        "setStyleSheet(": "setStyleSheet",
        "QColor(": "QColor",
        "rgb(": "rgb",
        "rgba(": "rgba",
    }

    def _rel(self, path: pathlib.Path) -> str:
        return path.relative_to(self.UI_ROOT).as_posix()

    @staticmethod
    def _strip_comment(line: str) -> str:
        """Remove a trailing # comment, keeping # inside string literals."""
        quote: str | None = None
        i = 0
        while i < len(line):
            ch = line[i]
            if quote is not None:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
            elif ch == "#":
                return line[:i]
            i += 1
        return line

    def _line_hits(self, code: str) -> list[str]:
        import re

        hits: list[str] = []
        for marker, root in self.LITERAL_MARKERS.items():
            if marker == "QColor(":
                # QColor from a literal: QColor("..."), QColor('#...'),
                # or QColor(int, ...) paint code.
                if re.search(r"QColor\(\s*(['\"]|\d)", code):
                    hits.append(root)
            elif marker in ("rgb(", "rgba("):
                if re.search(r"(?<![\w])" + re.escape(marker), code):
                    hits.append(root)
            elif marker in code:
                hits.append(root)
        # Real hex color literals (3-8 hex digits), anywhere in code.
        if re.search(r"#[0-9A-Fa-f]{3,8}\b", code):
            hits.append("#")
        return hits

    def test_no_scattered_stylesheets_or_colors(self) -> None:
        violations: list[str] = []
        for path in sorted(self.UI_ROOT.rglob("*.py")):
            rel = self._rel(path)
            allowed = self.ALLOW.get(rel, ())
            text = path.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith(('"""', "'''", "*")):
                    continue
                code = self._strip_comment(line)
                if not code.strip():
                    continue
                for root in self._line_hits(code):
                    if root not in allowed:
                        violations.append(f"{rel}:{i}: [{root}] {stripped[:110]}")
        assert violations == [], "\n".join(violations)
