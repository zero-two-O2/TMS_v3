"""Phase 12.5.3: dropdown separation, theme-aware icons, popup menus.

- Dropdown buttons: measured icon/indicator geometry (no overlap).
- Popup menus: available, mapped, themed icons, no clipping.
- Light + dark theme icon visibility (WCAG contrast vs real surfaces).
- Selected (On-state) and disabled icon variants.
- Theme switch refreshes existing toolbar icons (manager funnel).
- Toolbar height/compactness unchanged.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from thermal_monitor.roi.enums import RoiObjectType
from thermal_monitor.ui.widgets.roi_toolbar import (
    MENU_BUTTON_WIDTH,
    TOOL_BUTTON_SIZE,
    TOOL_GROUPS,
    TOOL_ICON_SIZE,
    RoiToolbar,
    ink_for_surface,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_ink_for_surface_rule():
    assert ink_for_surface("#1E232A") == "light"  # industrial_dark
    assert ink_for_surface("#0A0A0A") == "light"  # high_contrast
    assert ink_for_surface("#16202E") == "light"  # blue_engineering
    assert ink_for_surface("#F4F6F8") == "dark"  # industrial_light
    assert ink_for_surface("garbage") == "light"  # safe default


def _grab_pixels(widget):
    import numpy as np
    pixmap = widget.grab()
    image = pixmap.toImage().convertToFormat(
        pixmap.toImage().Format.Format_ARGB32)
    ptr = image.constBits()
    width, height = image.width(), image.height()
    ptr.setsize(width * height * 4)
    return np.frombuffer(ptr, dtype=np.uint8).reshape(height, width, 4)


def test_dropdown_icon_and_indicator_separate(qapp):
    """Icon ink never enters the style's menu-indicator zone.

    Compares the real menu button against the same button with a null
    icon (separator + arrow identical in both), isolating artwork
    pixels. Uses dark ink for contrast against the offscreen palette.
    """
    from PyQt6.QtWidgets import QStyle, QStyleOptionToolButton, QToolButton
    toolbar = RoiToolbar(ink="dark")
    toolbar.set_context_active(True, "test")
    toolbar.show()
    qapp.processEvents()
    button = toolbar._group_buttons["Regions"]
    assert button.width() == MENU_BUTTON_WIDTH
    assert button.height() == TOOL_BUTTON_SIZE
    option = QStyleOptionToolButton()
    option.initFrom(button)
    option.features = (
        QStyleOptionToolButton.ToolButtonFeature.HasMenu
        | QStyleOptionToolButton.ToolButtonFeature.MenuButtonPopup)
    menu_rect = button.style().subControlRect(
        QStyle.ComplexControl.CC_ToolButton, option,
        QStyle.SubControl.SC_ToolButtonMenu, button)
    assert menu_rect.width() >= 10  # a real dedicated zone exists
    import numpy as np
    art = _grab_pixels(button).astype(int)
    # Icon ink #1F2329 -> BGRA bytes (0x23, 0x29, 0x1F, 0xFF). The
    # style-drawn arrow uses the palette text color, so artwork and
    # indicator separate cleanly by color.
    ink_match = (np.abs(art[:, :, 0] - 0x23)
                 + np.abs(art[:, :, 1] - 0x29)
                 + np.abs(art[:, :, 2] - 0x1F) < 60) & (art[:, :, 3] > 8)
    ys, xs = ink_match.nonzero()
    assert len(xs) > 100  # the icon is actually painted
    assert int(xs.max()) < menu_rect.x() + 2, (
        f"icon ink reaches x={xs.max()} into menu zone x>={menu_rect.x()}")
    # The style arrow still owns the dedicated zone (not hidden):
    # non-background pixels exist right of the artwork boundary.
    bg = art[2, 2, :3]
    chrome = (np.abs(art[:, :, :3] - bg).sum(axis=2) > 60)
    zys, zxs = chrome.nonzero()
    assert (zxs >= menu_rect.x() - 2).any(), "no indicator in arrow zone"


def test_all_groups_have_popup_and_mapping(qapp):
    toolbar = RoiToolbar()
    seen = []
    toolbar.tool_changed.connect(seen.append)
    for group in ("Spots", "Lines", "Regions", "Measure", "Annotate"):
        button = toolbar._group_buttons[group]
        menu = button.menu()
        assert menu is not None
        actions = menu.actions()
        assert [a.text() for a in actions] == [
            label for label, _t, _i, _p in TOOL_GROUPS[group]]
        for action in actions:
            assert action.icon() is not None
            assert not action.icon().isNull()
        # Triggering the first entry arms the tool and updates the key.
        actions[0].trigger()
        assert toolbar.active_tool == TOOL_GROUPS[group][0][1]
        assert seen and seen[-1] == TOOL_GROUPS[group][0][1]
    assert toolbar._group_buttons["Select"].menu() is None


def test_popup_menu_icons_themed(qapp):
    from thermal_monitor.ui.widgets.roi_icons import icon_source
    toolbar = RoiToolbar(ink="dark")
    for group, entries in toolbar._menu_actions.items():
        for action, icon_key in entries:
            assert not action.icon().isNull()
    assert icon_source("rectangle", "dark") == "svg"
    assert icon_source("rectangle", "light") == "svg"


def _contrast(fg, bg):
    def luminance(rgb):
        channels = [v / 255.0 for v in rgb]
        linearized = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
                      for v in channels]
        return (0.2126 * linearized[0] + 0.7152 * linearized[1]
                + 0.0722 * linearized[2])

    top = luminance(fg)
    bottom = luminance(bg)
    return (max(top, bottom) + 0.05) / (min(top, bottom) + 0.05)


def _mean_core_color(key, ink, size=40):
    import numpy as np
    from thermal_monitor.ui.widgets.roi_icons import icon_for
    pixmap = icon_for(key, ink=ink).pixmap(size, size)
    image = pixmap.toImage().convertToFormat(
        pixmap.toImage().Format.Format_ARGB32)
    ptr = image.constBits()
    ptr.setsize(size * size * 4)
    pixels = np.frombuffer(ptr, dtype=np.uint8).reshape(size, size, 4)
    core = pixels[:, :, 3] > 200
    assert core.any(), key
    return pixels[:, :, [2, 1, 0]][core].astype(float).mean(axis=0)


def test_theme_visibility_both_surfaces(qapp):
    """Off ink on dark surfaces, dark ink on light surfaces (WCAG>=2.5);
    all 28 keys in both inks."""
    from thermal_monitor.ui.widgets.roi_icons import ICON_KEYS
    assert len(ICON_KEYS) == 28
    dark_surfaces = [(0x1E, 0x23, 0x2A), (0x0A, 0x0A, 0x0A),
                     (0x16, 0x20, 0x2E)]
    light_surfaces = [(0xF4, 0xF6, 0xF8)]
    for key in ICON_KEYS:
        for surface in dark_surfaces:
            ratio = _contrast(_mean_core_color(key, "light"), surface)
            assert ratio >= 2.5, ("dark", key, round(float(ratio), 2))
        for surface in light_surfaces:
            ratio = _contrast(_mean_core_color(key, "dark"), surface)
            assert ratio >= 2.5, ("light", key, round(float(ratio), 2))


def test_selected_state_uses_light_ink(qapp):
    """Checked buttons sit on blue secondary in every theme: the On
    pixmap must be bright in both inks."""
    from PyQt6.QtGui import QIcon
    from thermal_monitor.ui.widgets.roi_icons import ICON_KEYS, icon_for
    import numpy as np
    blue = {"industrial_dark": (0x1E, 0x88, 0xE5),
            "industrial_light": (0x19, 0x76, 0xD2)}
    for key in ("rectangle", "circle", "undo"):
        for ink in ("light", "dark"):
            pixmap = icon_for(key, ink=ink).pixmap(
                40, 40, mode=QIcon.Mode.Normal, state=QIcon.State.On)
            assert not pixmap.isNull()
            image = pixmap.toImage().convertToFormat(
                pixmap.toImage().Format.Format_ARGB32)
            ptr = image.constBits()
            ptr.setsize(40 * 40 * 4)
            pixels = np.frombuffer(ptr, dtype=np.uint8).reshape(40, 40, 4)
            core = pixels[:, :, 3] > 200
            assert core.any()
            mean = pixels[:, :, [2, 1, 0]][core].astype(float).mean(axis=0)
            for _theme, surface in blue.items():
                assert _contrast(mean, surface) >= 2.5, (key, ink)


def test_disabled_state_generated(qapp):
    from PyQt6.QtGui import QIcon
    from thermal_monitor.ui.widgets.roi_icons import icon_for
    pixmap = icon_for("rectangle", ink="light").pixmap(
        40, 40, mode=QIcon.Mode.Disabled)
    assert not pixmap.isNull()


def test_theme_switch_refreshes_toolbar(qapp):
    from thermal_monitor.ui.theme.manager import ThemeManager
    toolbar = RoiToolbar()
    toolbar.set_context_active(True, "test")
    manager = ThemeManager()
    manager.set_theme("industrial_dark")
    manager.apply_and_refresh(qapp)
    assert toolbar.ink == "light"
    before = toolbar._group_buttons["Regions"].icon().pixmap(40, 40).toImage()
    manager.set_theme("industrial_light")
    manager.apply_and_refresh(qapp)
    assert toolbar.ink == "dark"
    after = toolbar._group_buttons["Regions"].icon().pixmap(40, 40).toImage()
    import numpy as np

    def mean_brightness(image):
        ptr = image.constBits()
        ptr.setsize(40 * 40 * 4)
        pixels = np.frombuffer(ptr, dtype=np.uint8).reshape(40, 40, 4)
        mask = pixels[:, :, 3] > 8
        return pixels[:, :, [2, 1, 0]][mask].astype(float).mean()

    assert mean_brightness(after) < mean_brightness(before)
    # Switch back while a popup menu exists: no crash, icons refresh.
    menu = toolbar._group_buttons["Regions"].menu()
    menu.show()
    qapp.processEvents()
    manager.set_theme("industrial_dark")
    manager.apply_and_refresh(qapp)
    assert toolbar.ink == "light"
    menu.hide()


def test_toolbar_height_unchanged(qapp):
    toolbar = RoiToolbar()
    toolbar.set_context_active(True, "test")
    toolbar.show()
    qapp.processEvents()
    assert toolbar.height() <= TOOL_BUTTON_SIZE + 4
    for button in toolbar._group_buttons.values():
        assert button.height() == TOOL_BUTTON_SIZE
