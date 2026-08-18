"""
core.modes -- Application modes and mode management.

Defines the three application modes (CONFIGURATION, OBSERVER, OFFLINE) and a
ModeManager that enforces valid transitions between them.  This module is
pure domain logic with no PyQt, HALCON, or hardware dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence

from types import MappingProxyType


class ApplicationMode(str, Enum):
    """Top-level application operating mode."""

    CONFIGURATION = "configuration"
    OBSERVER = "observer"
    OFFLINE = "offline"


class TransitionError(Exception):
    """Raised when a mode transition is not allowed."""

    def __init__(self, from_mode: ApplicationMode, to_mode: ApplicationMode) -> None:
        self.from_mode = from_mode
        self.to_mode = to_mode
        super().__init__(
            f"Invalid mode transition: {from_mode.value} -> {to_mode.value}"
        )


@dataclass(frozen=True, slots=True)
class ModeCapabilities:
    """Capability flags for a given application mode.

    These flags express what operations are permitted in each mode.
    They are used by the application layer to enable/disable UI features
    and to validate operations at runtime.
    """

    live_camera_operation: bool = False
    camera_configuration: bool = False
    ptz_configuration: bool = False
    roi_editing: bool = False
    system_configuration: bool = False
    ptz_manual_control: bool = False
    alarm_observation: bool = False
    offline_playback: bool = False
    recording_playback: bool = False

    # Convenience properties
    @property
    def any_camera_operation(self) -> bool:
        return self.live_camera_operation or self.camera_configuration


# Capability matrix per mode (defined once, frozen)
_MODE_CAPABILITIES: Mapping[ApplicationMode, ModeCapabilities] = MappingProxyType({
    ApplicationMode.CONFIGURATION: ModeCapabilities(
        live_camera_operation=True,
        camera_configuration=True,
        ptz_configuration=True,
        roi_editing=True,
        system_configuration=True,
        ptz_manual_control=True,
        alarm_observation=False,
        offline_playback=False,
        recording_playback=False,
    ),
    ApplicationMode.OBSERVER: ModeCapabilities(
        live_camera_operation=True,
        camera_configuration=False,
        ptz_configuration=False,
        roi_editing=False,
        system_configuration=False,
        ptz_manual_control=False,
        alarm_observation=True,
        offline_playback=False,
        recording_playback=False,
    ),
    ApplicationMode.OFFLINE: ModeCapabilities(
        live_camera_operation=False,
        camera_configuration=False,
        ptz_configuration=False,
        roi_editing=False,
        system_configuration=False,
        ptz_manual_control=False,
        alarm_observation=False,
        offline_playback=True,
        recording_playback=True,
    ),
})


@dataclass(frozen=True, slots=True)
class ModeState:
    """Current mode state with capabilities and metadata."""

    mode: ApplicationMode
    capabilities: ModeCapabilities
    previous_mode: ApplicationMode | None = None
    transition_reason: str = ""


class ModeManager:
    """Manages application mode transitions.

    The mode manager enforces the valid transition graph:

        CONFIGURATION <-> OBSERVER
        CONFIGURATION -> OFFLINE
        OBSERVER -> OFFLINE
        OFFLINE -> CONFIGURATION
        OFFLINE -> OBSERVER

    Direct transitions between CONFIGURATION and OBSERVER are allowed
    in both directions.  OFFLINE can be entered from either mode, and
    exiting OFFLINE returns to either CONFIGURATION or OBSERVER.

    The manager emits ModeChanged events via a simple callback mechanism.
    """

    # Valid transitions: from_mode -> allowed target modes
    _VALID_TRANSITIONS: Mapping[ApplicationMode, Sequence[ApplicationMode]] = MappingProxyType({
        ApplicationMode.CONFIGURATION: (
            ApplicationMode.OBSERVER,
            ApplicationMode.OFFLINE,
        ),
        ApplicationMode.OBSERVER: (
            ApplicationMode.CONFIGURATION,
            ApplicationMode.OFFLINE,
        ),
        ApplicationMode.OFFLINE: (
            ApplicationMode.CONFIGURATION,
            ApplicationMode.OBSERVER,
        ),
    })

    def __init__(self, initial_mode: ApplicationMode = ApplicationMode.CONFIGURATION) -> None:
        if not isinstance(initial_mode, ApplicationMode):
            raise ValueError(f"Invalid initial mode: {initial_mode!r}")

        self._current_state = ModeState(
            mode=initial_mode,
            capabilities=_MODE_CAPABILITIES[initial_mode],
            previous_mode=None,
            transition_reason="initial",
        )
        self._callbacks: list[callable] = []

    @property
    def current_mode(self) -> ApplicationMode:
        return self._current_state.mode

    @property
    def capabilities(self) -> ModeCapabilities:
        return self._current_state.capabilities

    @property
    def previous_mode(self) -> ApplicationMode | None:
        return self._current_state.previous_mode

    @property
    def state(self) -> ModeState:
        return self._current_state

    def add_callback(self, callback: callable) -> None:
        """Register a callback for mode changes.

        Callback signature: callback(new_state: ModeState) -> None
        """
        self._callbacks.append(callback)

    def remove_callback(self, callback: callable) -> None:
        """Unregister a callback."""
        try:
            self._callbacks.remove(callback)
        except ValueError:
            pass

    def can_transition(self, target_mode: ApplicationMode) -> bool:
        """Check if a transition to the target mode is valid."""
        return target_mode in self._VALID_TRANSITIONS.get(self._current_state.mode, ())

    def transition(self, target_mode: ApplicationMode, reason: str = "") -> ModeState:
        """Transition to a new mode.

        Args:
            target_mode: The mode to transition to.
            reason: Optional human-readable reason for the transition.

        Returns:
            The new ModeState after transition.

        Raises:
            TransitionError: If the transition is not allowed.
        """
        if target_mode == self._current_state.mode:
            # No-op transition
            return self._current_state

        if not self.can_transition(target_mode):
            raise TransitionError(self._current_state.mode, target_mode)

        new_state = ModeState(
            mode=target_mode,
            capabilities=_MODE_CAPABILITIES[target_mode],
            previous_mode=self._current_state.mode,
            transition_reason=reason,
        )
        self._current_state = new_state
        self._notify_callbacks(new_state)
        return new_state

    def transition_to_configuration(self, reason: str = "") -> ModeState:
        """Convenience method to transition to CONFIGURATION mode."""
        return self.transition(ApplicationMode.CONFIGURATION, reason)

    def transition_to_observer(self, reason: str = "") -> ModeState:
        """Convenience method to transition to OBSERVER mode."""
        return self.transition(ApplicationMode.OBSERVER, reason)

    def transition_to_offline(self, reason: str = "") -> ModeState:
        """Convenience method to transition to OFFLINE mode."""
        return self.transition(ApplicationMode.OFFLINE, reason)

    def _notify_callbacks(self, new_state: ModeState) -> None:
        for callback in self._callbacks:
            try:
                callback(new_state)
            except Exception:
                # Callbacks must not crash the mode manager
                pass