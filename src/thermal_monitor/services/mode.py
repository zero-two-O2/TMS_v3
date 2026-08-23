"""
services.mode -- Mode service for managing application modes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from thermal_monitor.core.modes import ApplicationMode, ModeCapabilities, ModeManager, ModeState, TransitionError


class MutualExclusionError(RuntimeError):
    """Raised when a mode transition violates mutual exclusion rules."""

    def __init__(self, current_mode: ApplicationMode, requested_mode: ApplicationMode) -> None:
        self.current_mode = current_mode
        self.requested_mode = requested_mode
        if current_mode == ApplicationMode.LIVE and requested_mode == ApplicationMode.CONFIGURATION:
            message = "Configuration Mode is currently active. Close Live Mode before opening Configuration Mode."
        elif current_mode == ApplicationMode.CONFIGURATION and requested_mode == ApplicationMode.LIVE:
            message = "Live Mode is currently active. Close Configuration Mode before opening Live Mode."
        else:
            message = f"Mutual exclusion violation: {current_mode.value} -> {requested_mode.value}"
        super().__init__(message)


@dataclass
class ModeService:
    """Application-level service for mode management.

    Coordinates mode transitions and provides capability checking
    for the application layer.

    Enforces mutual exclusion: Live and Configuration cannot be active simultaneously.
    """

    _manager: ModeManager
    _live_active: bool = False
    _configuration_active: bool = False

    def __init__(self, initial_mode: ApplicationMode = ApplicationMode.LAUNCHER) -> None:
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
        """Check if a transition is valid (including mutual exclusion)."""
        # Base transition validity
        if not self._manager.can_transition(target_mode):
            return False

        # Mutual exclusion: Live and Configuration cannot both be active
        if self._live_active and target_mode == ApplicationMode.CONFIGURATION:
            return False
        if self._configuration_active and target_mode == ApplicationMode.LIVE:
            return False

        return True

    def transition_to(self, target_mode: ApplicationMode, reason: str = "") -> ModeState:
        """Transition to a new mode with mutual exclusion enforcement."""
        if target_mode == self._manager.current_mode:
            # No-op transition
            return self._manager.state

        # Check mutual exclusion before transition
        if self._live_active and target_mode == ApplicationMode.CONFIGURATION:
            raise MutualExclusionError(ApplicationMode.LIVE, ApplicationMode.CONFIGURATION)
        if self._configuration_active and target_mode == ApplicationMode.LIVE:
            raise MutualExclusionError(ApplicationMode.CONFIGURATION, ApplicationMode.LIVE)

        if not self._manager.can_transition(target_mode):
            raise TransitionError(self._manager.current_mode, target_mode)

        return self._manager.transition(target_mode, reason)

    def transition_to_configuration(self, reason: str = "") -> ModeState:
        return self.transition_to(ApplicationMode.CONFIGURATION, reason)

    def transition_to_live(self, reason: str = "") -> ModeState:
        return self.transition_to(ApplicationMode.LIVE, reason)

    def transition_to_launcher(self, reason: str = "") -> ModeState:
        return self.transition_to(ApplicationMode.LAUNCHER, reason)

    def transition_to_offline(self, reason: str = "") -> ModeState:
        return self.transition_to(ApplicationMode.OFFLINE, reason)

    def set_live_active(self, active: bool) -> None:
        """Mark Live mode as active/inactive (called by AppController)."""
        self._live_active = active

    def set_configuration_active(self, active: bool) -> None:
        """Mark Configuration mode as active/inactive (called by AppController)."""
        self._configuration_active = active

    def is_live_active(self) -> bool:
        return self._live_active

    def is_configuration_active(self) -> bool:
        return self._configuration_active

    def is_configuration_mode(self) -> bool:
        return self.current_mode == ApplicationMode.CONFIGURATION

    def is_live_mode(self) -> bool:
        return self.current_mode == ApplicationMode.LIVE

    def is_launcher_mode(self) -> bool:
        return self.current_mode == ApplicationMode.LAUNCHER

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


__all__ = ["ModeService", "MutualExclusionError"]