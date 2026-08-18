"""
services.mode -- Mode service for managing application modes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from thermal_monitor.core.modes import ApplicationMode, ModeCapabilities, ModeManager, ModeState, TransitionError


@dataclass
class ModeService:
    """Application-level service for mode management.

    Coordinates mode transitions and provides capability checking
    for the application layer.
    """

    _manager: ModeManager

    def __init__(self, initial_mode: ApplicationMode = ApplicationMode.CONFIGURATION) -> None:
        self._manager = ModeManager(initial_mode)

    @property
    def current_mode(self) -> ApplicationMode:
        return self._manager.current_mode

    @property
    def capabilities(self) -> ModeCapabilities:
        return self._manager.capabilities

    @property
    def state(self) -> ModeState:
        return self._manager.state

    def add_observer(self, callback: Callable[[ModeState], None]) -> None:
        """Register a callback for mode changes."""
        self._manager.add_callback(callback)

    def remove_observer(self, callback: Callable[[ModeState], None]) -> None:
        """Unregister a callback."""
        self._manager.remove_callback(callback)

    def can_transition(self, target_mode: ApplicationMode) -> bool:
        """Check if a transition is valid."""
        return self._manager.can_transition(target_mode)

    def transition_to(self, target_mode: ApplicationMode, reason: str = "") -> ModeState:
        """Transition to a new mode."""
        return self._manager.transition(target_mode, reason)

    def transition_to_configuration(self, reason: str = "") -> ModeState:
        return self._manager.transition_to_configuration(reason)

    def transition_to_observer(self, reason: str = "") -> ModeState:
        return self._manager.transition_to_observer(reason)

    def transition_to_offline(self, reason: str = "") -> ModeState:
        return self._manager.transition_to_offline(reason)

    def is_configuration_mode(self) -> bool:
        return self.current_mode == ApplicationMode.CONFIGURATION

    def is_observer_mode(self) -> bool:
        return self.current_mode == ApplicationMode.OBSERVER

    def is_offline_mode(self) -> bool:
        return self.current_mode == ApplicationMode.OFFLINE

    def check_capability(self, capability: str) -> bool:
        """Check if a specific capability is enabled in current mode."""
        caps = self.capabilities
        return getattr(caps, capability, False)

    def require_capability(self, capability: str) -> None:
        """Raise exception if capability is not available."""
        if not self.check_capability(capability):
            raise RuntimeError(
                f"Operation requires '{capability}' which is not available in "
                f"{self.current_mode.value} mode"
            )