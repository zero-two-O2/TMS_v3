"""Tests for ApplicationMode and ModeManager."""

from __future__ import annotations

import pytest

from thermal_monitor.core.modes import (
    ApplicationMode,
    ModeCapabilities,
    ModeManager,
    ModeState,
    TransitionError,
)


class TestApplicationMode:
    def test_mode_values(self):
        assert ApplicationMode.LAUNCHER.value == "launcher"
        assert ApplicationMode.LIVE.value == "live"
        assert ApplicationMode.CONFIGURATION.value == "configuration"
        assert ApplicationMode.OFFLINE.value == "offline"

    def test_all_modes_defined(self):
        modes = set(ApplicationMode)
        expected = {
            ApplicationMode.LAUNCHER,
            ApplicationMode.LIVE,
            ApplicationMode.CONFIGURATION,
            ApplicationMode.OFFLINE,
        }
        assert modes == expected


class TestModeCapabilities:
    def test_launcher_capabilities(self):
        caps = ModeCapabilities(
            live_camera_operation=False,
            camera_configuration=False,
            ptz_configuration=False,
            roi_editing=False,
            system_configuration=False,
            ptz_manual_control=False,
            alarm_observation=False,
            offline_playback=False,
            recording_playback=False,
        )
        assert caps.live_camera_operation is False
        assert caps.camera_configuration is False
        assert caps.any_camera_operation is False

    def test_live_capabilities(self):
        caps = ModeCapabilities(
            live_camera_operation=True,
            camera_configuration=False,
            ptz_configuration=False,
            roi_editing=False,
            system_configuration=False,
            ptz_manual_control=False,
            alarm_observation=True,
            offline_playback=False,
            recording_playback=False,
        )
        assert caps.live_camera_operation is True
        assert caps.camera_configuration is False
        assert caps.alarm_observation is True
        assert caps.any_camera_operation is True

    def test_configuration_capabilities(self):
        caps = ModeCapabilities(
            live_camera_operation=False,
            camera_configuration=True,
            ptz_configuration=True,
            roi_editing=True,
            system_configuration=True,
            ptz_manual_control=True,
            alarm_observation=False,
            offline_playback=False,
            recording_playback=False,
        )
        assert caps.live_camera_operation is False
        assert caps.camera_configuration is True
        assert caps.ptz_configuration is True
        assert caps.roi_editing is True
        assert caps.system_configuration is True
        assert caps.ptz_manual_control is True
        assert caps.alarm_observation is False
        assert caps.offline_playback is False
        assert caps.recording_playback is False
        assert caps.any_camera_operation is True

    def test_offline_capabilities(self):
        caps = ModeCapabilities(
            live_camera_operation=False,
            camera_configuration=False,
            ptz_configuration=False,
            roi_editing=False,
            system_configuration=False,
            ptz_manual_control=False,
            alarm_observation=False,
            offline_playback=True,
            recording_playback=True,
        )
        assert caps.live_camera_operation is False
        assert caps.camera_configuration is False
        assert caps.offline_playback is True
        assert caps.recording_playback is True
        assert caps.any_camera_operation is False


class TestModeManager:
    def test_initial_mode_defaults_to_launcher(self):
        manager = ModeManager()
        assert manager.current_mode == ApplicationMode.LAUNCHER
        assert manager.capabilities.live_camera_operation is False
        assert manager.capabilities.camera_configuration is False

    def test_initial_mode_can_be_set(self):
        manager = ModeManager(ApplicationMode.LIVE)
        assert manager.current_mode == ApplicationMode.LIVE
        assert manager.capabilities.alarm_observation is True

    def test_invalid_initial_mode_raises(self):
        with pytest.raises(ValueError):
            ModeManager("invalid")  # type: ignore[arg-type]

    def test_can_transition_from_launcher_to_live(self):
        manager = ModeManager(ApplicationMode.LAUNCHER)
        assert manager.can_transition(ApplicationMode.LIVE) is True

    def test_can_transition_from_launcher_to_configuration(self):
        manager = ModeManager(ApplicationMode.LAUNCHER)
        assert manager.can_transition(ApplicationMode.CONFIGURATION) is True

    def test_can_transition_from_launcher_to_offline(self):
        manager = ModeManager(ApplicationMode.LAUNCHER)
        assert manager.can_transition(ApplicationMode.OFFLINE) is True

    def test_can_transition_from_live_to_configuration(self):
        manager = ModeManager(ApplicationMode.LIVE)
        assert manager.can_transition(ApplicationMode.CONFIGURATION) is True

    def test_can_transition_from_live_to_offline(self):
        manager = ModeManager(ApplicationMode.LIVE)
        assert manager.can_transition(ApplicationMode.OFFLINE) is True

    def test_can_transition_from_configuration_to_live(self):
        manager = ModeManager(ApplicationMode.CONFIGURATION)
        assert manager.can_transition(ApplicationMode.LIVE) is True

    def test_can_transition_from_configuration_to_offline(self):
        manager = ModeManager(ApplicationMode.CONFIGURATION)
        assert manager.can_transition(ApplicationMode.OFFLINE) is True

    def test_can_transition_from_offline_to_live(self):
        manager = ModeManager(ApplicationMode.OFFLINE)
        assert manager.can_transition(ApplicationMode.LIVE) is True

    def test_can_transition_from_offline_to_configuration(self):
        manager = ModeManager(ApplicationMode.OFFLINE)
        assert manager.can_transition(ApplicationMode.CONFIGURATION) is True

    def test_cannot_transition_from_launcher_to_launcher(self):
        manager = ModeManager(ApplicationMode.LAUNCHER)
        assert manager.can_transition(ApplicationMode.LAUNCHER) is False

    def test_cannot_transition_from_live_to_live(self):
        manager = ModeManager(ApplicationMode.LIVE)
        assert manager.can_transition(ApplicationMode.LIVE) is False

    def test_cannot_transition_from_configuration_to_configuration(self):
        manager = ModeManager(ApplicationMode.CONFIGURATION)
        assert manager.can_transition(ApplicationMode.CONFIGURATION) is False

    def test_valid_transition_changes_mode(self):
        manager = ModeManager(ApplicationMode.CONFIGURATION)
        new_state = manager.transition(ApplicationMode.LIVE, "user switched")
        assert new_state.mode == ApplicationMode.LIVE
        assert manager.current_mode == ApplicationMode.LIVE
        assert new_state.previous_mode == ApplicationMode.CONFIGURATION
        assert new_state.transition_reason == "user switched"

    def test_transition_to_same_mode_is_noop(self):
        manager = ModeManager(ApplicationMode.CONFIGURATION)
        state = manager.transition(ApplicationMode.CONFIGURATION)
        assert state.mode == ApplicationMode.CONFIGURATION
        assert state.previous_mode is None  # No previous mode on no-op

    def test_transition_callbacks_fired(self):
        manager = ModeManager(ApplicationMode.LAUNCHER)
        callback_states: list[ModeState] = []

        def callback(state: ModeState):
            callback_states.append(state)

        manager.add_callback(callback)
        manager.transition(ApplicationMode.LIVE, "test")
        manager.transition(ApplicationMode.OFFLINE, "test2")

        assert len(callback_states) == 2
        assert callback_states[0].mode == ApplicationMode.LIVE
        assert callback_states[1].mode == ApplicationMode.OFFLINE

    def test_callback_exception_does_not_crash(self):
        manager = ModeManager(ApplicationMode.LAUNCHER)

        def bad_callback(state: ModeState):
            raise RuntimeError("callback error")

        manager.add_callback(bad_callback)
        # Should not raise
        manager.transition(ApplicationMode.LIVE)

    def test_remove_callback(self):
        manager = ModeManager(ApplicationMode.LAUNCHER)
        callback_states: list[ModeState] = []

        def callback(state: ModeState):
            callback_states.append(state)

        manager.add_callback(callback)
        manager.transition(ApplicationMode.LIVE)
        manager.remove_callback(callback)
        manager.transition(ApplicationMode.OFFLINE)

        assert len(callback_states) == 1

    def test_convenience_methods(self):
        manager = ModeManager(ApplicationMode.LAUNCHER)
        assert manager.transition_to_live().mode == ApplicationMode.LIVE
        assert manager.transition_to_configuration().mode == ApplicationMode.CONFIGURATION
        assert manager.transition_to_offline().mode == ApplicationMode.OFFLINE

        # Test from LIVE
        manager2 = ModeManager(ApplicationMode.LIVE)
        assert manager2.transition_to_configuration().mode == ApplicationMode.CONFIGURATION
        assert manager2.transition_to_offline().mode == ApplicationMode.OFFLINE

        # Test from CONFIGURATION
        manager3 = ModeManager(ApplicationMode.CONFIGURATION)
        assert manager3.transition_to_live().mode == ApplicationMode.LIVE
        assert manager3.transition_to_offline().mode == ApplicationMode.OFFLINE

        # Test from OFFLINE
        manager4 = ModeManager(ApplicationMode.OFFLINE)
        assert manager4.transition_to_live().mode == ApplicationMode.LIVE
        assert manager4.transition_to_configuration().mode == ApplicationMode.CONFIGURATION

    def test_state_contains_full_info(self):
        manager = ModeManager(ApplicationMode.CONFIGURATION)
        state = manager.transition(ApplicationMode.LIVE, "user request")

        assert isinstance(state, ModeState)
        assert state.mode == ApplicationMode.LIVE
        assert state.capabilities.alarm_observation is True
        assert state.previous_mode == ApplicationMode.CONFIGURATION
        assert state.transition_reason == "user request"

    def test_previous_mode_tracking(self):
        manager = ModeManager(ApplicationMode.CONFIGURATION)
        manager.transition(ApplicationMode.LIVE)
        assert manager.previous_mode == ApplicationMode.CONFIGURATION

        manager.transition(ApplicationMode.OFFLINE)
        assert manager.previous_mode == ApplicationMode.LIVE

        manager.transition(ApplicationMode.CONFIGURATION)
        assert manager.previous_mode == ApplicationMode.OFFLINE

    def test_capabilities_match_mode(self):
        for mode in ApplicationMode:
            manager = ModeManager(mode)
            expected_caps = {
                ApplicationMode.LAUNCHER: lambda c: c.live_camera_operation is False and c.camera_configuration is False,
                ApplicationMode.LIVE: lambda c: c.live_camera_operation and c.alarm_observation,
                ApplicationMode.CONFIGURATION: lambda c: c.camera_configuration and c.roi_editing,
                ApplicationMode.OFFLINE: lambda c: c.offline_playback and c.recording_playback,
            }
            assert expected_caps[mode](manager.capabilities)


class TestModeCapabilitiesMatrix:
    """Test that the internal capability matrix matches requirements."""

    def test_launcher_disables_all_operations(self):
        manager = ModeManager(ApplicationMode.LAUNCHER)
        caps = manager.capabilities
        assert caps.live_camera_operation is False
        assert caps.camera_configuration is False
        assert caps.ptz_configuration is False
        assert caps.roi_editing is False
        assert caps.system_configuration is False
        assert caps.ptz_manual_control is False
        assert caps.alarm_observation is False
        assert caps.offline_playback is False
        assert caps.recording_playback is False

    def test_live_enables_camera_operation_alarms(self):
        manager = ModeManager(ApplicationMode.LIVE)
        caps = manager.capabilities
        assert caps.live_camera_operation is True
        assert caps.camera_configuration is False
        assert caps.ptz_configuration is False
        assert caps.roi_editing is False
        assert caps.system_configuration is False
        assert caps.ptz_manual_control is False
        assert caps.alarm_observation is True
        assert caps.offline_playback is False
        assert caps.recording_playback is False

    def test_configuration_allows_camera_config_roi_ptz(self):
        manager = ModeManager(ApplicationMode.CONFIGURATION)
        caps = manager.capabilities
        assert caps.camera_configuration is True
        assert caps.roi_editing is True
        assert caps.ptz_configuration is True
        assert caps.ptz_manual_control is True
        assert caps.system_configuration is True
        assert caps.live_camera_operation is False
        assert caps.alarm_observation is False

    def test_offline_disables_all_live_operations(self):
        manager = ModeManager(ApplicationMode.OFFLINE)
        caps = manager.capabilities
        assert caps.live_camera_operation is False
        assert caps.camera_configuration is False
        assert caps.ptz_configuration is False
        assert caps.roi_editing is False
        assert caps.system_configuration is False
        assert caps.ptz_manual_control is False
        assert caps.alarm_observation is False
        assert caps.offline_playback is True
        assert caps.recording_playback is True