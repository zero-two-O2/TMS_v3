"""ui.theme.menu -- Central Settings and Theme menu for live switching.

Builds the top-level Settings menu with an exclusive Theme submenu.
Selecting a theme is a pure GUI operation: the shared ThemeManager
applies it to the running application immediately, then the choice is
persisted through the configuration manager.  No restart, no camera
or acquisition lifecycle call happens on this path.

Only this module builds theme menus, so every window shares one
source of truth for styling.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMainWindow, QMenu, QMenuBar

from thermal_monitor.ui.theme.themes import BUILTIN_THEMES


logger = logging.getLogger(__name__)


SETTINGS_MENU_TITLE = "Settings"
THEME_MENU_TITLE = "Theme"

THEME_MENU_ORDER: tuple[str, ...] = (
    "industrial_dark",
    "industrial_light",
    "blue_engineering",
    "high_contrast",
)


def theme_display_name(name: str) -> str:
    """Return the human readable label for a menu theme name."""
    return BUILTIN_THEMES[name].display_name


def available_menu_themes() -> tuple[str, ...]:
    """Return the theme names exposed in the Settings menu, in order."""
    return THEME_MENU_ORDER


class ThemeMenuController(QObject):
    """Owns one window's Settings and Theme menus on a shared manager.

    All windows must share the same ThemeManager instance so styling
    stays centralized.  The optional configuration manager is only
    used to persist the preference after a successful live apply; a
    persistence failure never undoes the applied theme.
    """

    theme_applied = pyqtSignal(str)

    def __init__(
        self,
        theme_manager=None,
        config_manager=None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        if theme_manager is None:
            from thermal_monitor.ui.theme.manager import ThemeManager

            theme_manager = ThemeManager(None)
        self._theme_manager = theme_manager
        self._config_manager = config_manager
        self._actions: dict[str, QAction] = {}
        self._group = None
        self._theme_menu: QMenu | None = None
        self._settings_menu: QMenu | None = None

    @property
    def theme_manager(self):
        return self._theme_manager

    @property
    def config_manager(self):
        return self._config_manager

    def current_theme(self) -> str:
        """Return the active theme name from the shared manager."""
        return self._theme_manager.theme_name

    def action_for(self, name: str) -> QAction | None:
        """Return the menu action for a theme name, if built."""
        return self._actions.get(name)

    def create_theme_menu(self, parent_widget) -> QMenu:
        """Build the exclusive Theme submenu with checked state."""
        from PyQt6.QtGui import QActionGroup

        if self._theme_menu is not None:
            return self._theme_menu
        theme_menu = QMenu(THEME_MENU_TITLE, parent_widget)
        group = QActionGroup(theme_menu)
        group.setExclusive(True)
        current = self.current_theme()
        for name in THEME_MENU_ORDER:
            action = QAction(theme_display_name(name), theme_menu)
            action.setCheckable(True)
            action.setChecked(name == current)
            action.setData(name)
            action.triggered.connect(
                lambda checked=False, theme_name=name: self.select_theme(theme_name)
            )
            group.addAction(action)
            theme_menu.addAction(action)
            self._actions[name] = action
        self._group = group
        self._theme_menu = theme_menu
        try:
            theme_menu.aboutToShow.connect(self.refresh_checked)
        except Exception:
            logger.debug("Theme menu refresh on show not connected")
        return theme_menu

    def create_settings_menu(self, parent_widget) -> QMenu:
        """Build the top-level Settings menu containing the Theme submenu."""
        if self._settings_menu is not None:
            return self._settings_menu
        settings = QMenu(SETTINGS_MENU_TITLE, parent_widget)
        theme_menu = self.create_theme_menu(settings)
        settings.addMenu(theme_menu)
        self._settings_menu = settings
        try:
            settings.aboutToShow.connect(self.refresh_checked)
        except Exception:
            logger.debug("Settings menu refresh on show not connected")
        return settings

    def attach_to_menu_bar(self, menu_bar: QMenuBar) -> QMenu:
        """Add the Settings menu to an existing menu bar, reusing it."""
        for existing in menu_bar.findChildren(QMenu):
            if existing.title().replace("&", "") == SETTINGS_MENU_TITLE:
                parent_menu = existing
                if self._theme_menu is None:
                    self._sync_existing_settings_menu(parent_menu)
                return parent_menu
        settings = self.create_settings_menu(menu_bar)
        menu_bar.addMenu(settings)
        return settings

    def attach_to_window(self, main_window: QMainWindow) -> QMenu:
        """Add the Settings menu to a main window's menu bar."""
        return self.attach_to_menu_bar(main_window.menuBar())

    def _sync_existing_settings_menu(self, settings: QMenu) -> None:
        """Adopt a pre-existing Settings menu built elsewhere, if any."""
        from PyQt6.QtGui import QActionGroup

        theme_sub: QMenu | None = None
        for action in settings.actions():
            sub = action.menu()
            if sub is not None and sub.title().replace("&", "") == THEME_MENU_TITLE:
                theme_sub = sub
                break
        if theme_sub is None:
            theme_sub = self.create_theme_menu(settings)
            settings.addMenu(theme_sub)
            self._settings_menu = settings
            return
        self._theme_menu = theme_sub
        self._settings_menu = settings
        group = QActionGroup(theme_sub)
        group.setExclusive(True)
        current = self.current_theme()
        for action in theme_sub.actions():
            name = action.data()
            if name in THEME_MENU_ORDER:
                action.setCheckable(True)
                action.setChecked(name == current)
                group.addAction(action)
                self._actions[name] = action
        try:
            theme_sub.aboutToShow.connect(self.refresh_checked)
        except Exception:
            logger.debug("Existing theme menu refresh not connected")
        try:
            settings.aboutToShow.connect(self.refresh_checked)
        except Exception:
            logger.debug("Existing settings menu refresh not connected")
        self._group = group

    def refresh_checked(self) -> None:
        """Update check marks from the shared manager's active theme."""
        try:
            current = self.current_theme()
        except Exception:
            return
        for name, action in self._actions.items():
            try:
                action.setChecked(name == current)
            except RuntimeError:
                continue

    def select_theme(self, name: str) -> str:
        """Apply a theme live, persist the preference, update checks.

        Steps: set the shared manager theme, apply to the running
        application with a widget repolish, persist through the
        configuration manager, refresh checks, emit theme_applied.
        Persistence failures are logged and never undo the live apply.
        Returns the newly active theme name.
        """
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        self._theme_manager.set_theme(name)
        self._theme_manager.apply_and_refresh(app)
        self._persist_preference(name)
        self.refresh_checked()
        try:
            self.theme_applied.emit(name)
        except Exception:
            logger.debug("Theme applied signal emission skipped")
        return name

    def _persist_preference(self, name: str) -> bool:
        """Save the theme preference; return True on success or skip."""
        if self._config_manager is None:
            return True
        try:
            self._config_manager.save_theme(name)
        except Exception as exc:
            logger.warning("Theme preference persist failed: %s", exc)
            return False
        try:
            self._theme_manager.refresh()
        except Exception as exc:
            logger.debug("Theme manager refresh after persist skipped: %s", exc)
        self.refresh_checked()
        return True


__all__ = [
    "SETTINGS_MENU_TITLE",
    "THEME_MENU_TITLE",
    "THEME_MENU_ORDER",
    "ThemeMenuController",
    "available_menu_themes",
    "theme_display_name",
]
