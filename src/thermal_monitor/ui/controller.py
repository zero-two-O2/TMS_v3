"""
ui.controller -- Application controller managing window lifecycle and mutual exclusion.

Owns the four top-level windows and coordinates their visibility and lifecycle.
Enforces mutual exclusion between Live and Configuration modes.
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSlot
from PyQt6.QtWidgets import QMessageBox
from PyQt6.sip import isdeleted

from thermal_monitor.core.modes import ApplicationMode, ModeState
from thermal_monitor.core.models import CameraConfig
from thermal_monitor.services.mode import ModeService
from thermal_monitor.services.configuration import ConfigurationService
from thermal_monitor.services.offline import OfflineService
from thermal_monitor.services.runtime import CameraRuntimeService
from thermal_monitor.storage.database import Database
from thermal_monitor.ui.windows.launcher_window import LauncherWindow
from thermal_monitor.ui.windows.live_window import LiveWindow
from thermal_monitor.ui.windows.configuration_window import ConfigurationWindow
from thermal_monitor.ui.windows.offline_window import OfflineWindow
from thermal_monitor.services.discovery import (
    CameraDiscoveryService,
    GvcpDiscoveryService,
    build_discovery_service,
)
from thermal_monitor.services.observer import ObserverService
from thermal_monitor.config import ConfigurationManager, CamerasConfig, SystemConfig, RecordingConfig, StorageConfig, CalibrationConfig
from thermal_monitor.ui.theme import ThemeManager


logger = logging.getLogger(__name__)


class TransitionState(str, Enum):
    """Visible UI transition state (prevents reentrant navigation)."""

    IDLE = "idle"
    ENTERING_MODE = "entering_mode"
    ACTIVE = "active"
    LEAVING_MODE = "leaving_mode"


class AppController(QObject):
    """Application-level controller managing window lifecycle.

    Responsibilities:
    - Creates and owns all top-level windows
    - Enforces mutual exclusion: Live + Configuration cannot be open simultaneously
    - Coordinates window show/hide transitions
    - Connects mode requests from Launcher to window activation
    - Manages application shutdown
    """

    def __init__(
        self,
        mode_service: ModeService,
        config_service: ConfigurationService,
        offline_service: OfflineService,
        runtime_service: CameraRuntimeService,
        database: Database | None = None,
        *,
        discovery_service: "CameraDiscoveryService | GvcpDiscoveryService | None" = None,
        observer_service: ObserverService | None = None,
        config_manager: ConfigurationManager | None = None,
        theme_manager: ThemeManager | None = None,
    ) -> None:
        super().__init__()

        self._mode_service = mode_service
        self._config_service = config_service
        self._offline_service = offline_service
        self._runtime_service = runtime_service
        self._database = database
        self._discovery_service = discovery_service or CameraDiscoveryService()
        self._observer_service = observer_service
        self._config_manager = config_manager
        self._theme_manager = theme_manager

        # Window instances (created lazily)
        self._launcher_window: LauncherWindow | None = None
        self._live_window: LiveWindow | None = None
        self._config_window: ConfigurationWindow | None = None
        self._offline_window: OfflineWindow | None = None

        # Track which mode windows are currently open
        self._live_open = False
        self._config_open = False
        # True once shutdown() starts: destroyed-signal handlers become
        # no-ops so teardown can never cascade through half-dead windows
        # (e.g. touching launcher buttons whose C++ objects are already
        # gone) or resurrect UI via _show_launcher().
        self._shutting_down = False

        # --- Visible-transition orchestration (seamless Launcher <-> mode) ---
        # The Launcher is a persistent application-level window: it is
        # hidden/shown for ordinary navigation, never destroyed/recreated.
        # Mode teardown (camera/process/SHM/worker joins) always runs
        # BEHIND the visible transition — the GUI thread never waits for
        # it. Monotonic generation drops stale destroyed callbacks from
        # asynchronously closed modes.
        self._transition_state = TransitionState.IDLE
        self._transition_generation = 0
        self._transition_t0_ns = 0
        self._transition_label = ""
        self._transition_visible_ns = 0
        self._pending_mode: ApplicationMode | None = None
        # Event-loop gap watchdog: while a transition is in flight a
        # 25 ms heartbeat records the worst GUI-thread stall so any
        # >50 ms freeze is attributed to the stage window it fell in.
        self._gap_timer: QTimer | None = None
        self._gap_last_ns = 0
        self._gap_max_ms = 0.0

        # Connect mode service for mutual exclusion enforcement
        self._mode_service.add_observer(self._on_mode_changed)

    def initialize(self) -> None:
        """Create and show the launcher window maximized."""
        # Configure services with configuration
        self._configure_services()
        self._create_launcher_window()
        # Apply start_maximized from config
        config = self._config_manager.get_config()
        if config.ui.windows.start_maximized:
            self._launcher_window.showMaximized()
        else:
            self._launcher_window.show()

    def _configure_services(self) -> None:
        """Configure all services with configuration from ConfigurationManager."""
        config = self._config_manager.get_config()

        # Configure discovery backend (Stage 8E: GVCP default, HALCON fallback).
        self._discovery_service = build_discovery_service(config.cameras.discovery)

        # Configure CameraRuntimeService
        self._runtime_service = CameraRuntimeService(
            cameras_config=config.cameras,
            system_config=config.system,
            recording_config=config.recording,
            storage_config=config.storage,
            calibration_config=config.calibration,
        )

        # Configure OfflineService
        self._offline_service = OfflineService(
            playback_speed=config.offline.playback.default_speed,
        )

        # Configure ConfigurationService with defaults from config
        self._configure_configuration_service(config)

        # Configure Database if enabled
        if config.database.enabled:
            self._configure_database(config.database)

    def _configure_configuration_service(self, config) -> None:
        """Configure ConfigurationService with defaults from config."""
        # Update system config with values from config.yaml
        from thermal_monitor.core.models import SystemConfig
        system_config = SystemConfig(
            application_name=config.application.name,
            version=config.application.version,
            default_mode=config.application.default_mode,
            max_cameras=config.system.max_cameras,
            camera_discovery_enabled=config.cameras.discovery.enabled,
            camera_discovery_interval_seconds=config.cameras.discovery.interval_seconds,
            processing_enabled=config.processing.enabled,
            processing_interval_ms=config.processing.interval_ms,
            alarm_evaluation_enabled=config.alarms.evaluation_enabled,
            alarm_cooldown_seconds=config.alarms.cooldown_seconds,
            max_alarm_history=config.alarms.max_history,
            recording_enabled=config.recording.enabled,
            database_connection_string="",  # Will be set if database enabled
            recording_storage_path="",
            offline_storage_path=config.offline.storage_path,
            log_level=config.logging.level,
            log_max_size_mb=config.logging.max_size_mb,
            log_backup_count=config.logging.backup_count,
            bind_address=config.network.bind_address,
            http_port=config.network.http_port,
        )
        self._config_service.update_system_config(system_config)
        self._hydrate_camera_configs(config)

    def _hydrate_camera_configs(self, config) -> None:
        """Load persisted camera mappings into the shared runtime service."""
        for mapping in getattr(config.cameras, "mapping", []) or []:
            metadata = {}
            if mapping.target_fps is not None:
                metadata["frame_rate"] = mapping.target_fps

            identity = self._config_service.create_camera_identity(
                camera_id=mapping.camera_id,
                serial_number=mapping.serial_number,
                user_name=mapping.name,
            )
            camera_config = CameraConfig(
                identity=identity,
                name=mapping.name or mapping.camera_id,
                enabled=mapping.enabled,
                thermal_enabled=True,
                visible_enabled=False,
                metadata=metadata,
            )
            self._config_service.set_camera_config(camera_config)

        # Hydrate ConfigurationService.camera_configs from config.cameras.mapping
        # This is the single source for fixed camera positions 1-8. The mapping
        # order defines position assignment; no second loader may exist.
        try:
            mapping = getattr(config.cameras, "mapping", []) or []
            logger.info("CONFIG SERVICE HYDRATION: config_path=%s exists=%s mapping_count=%d config_service_id=%s", self._config_manager.config_path if self._config_manager else "unknown", self._config_manager.config_path.exists() if self._config_manager and hasattr(self._config_manager, "config_path") else "?", len(mapping), hex(id(self._config_service)))
            if self._config_manager and hasattr(self._config_manager, "config_path"):
                tmp_path = self._config_manager.config_path.with_suffix(self._config_manager.config_path.suffix + ".tmp")
                logger.info("CONFIG SERVICE HYDRATION: tmp_path=%s exists=%s", tmp_path, tmp_path.exists())
            for idx, entry in enumerate(mapping):
                try:
                    # entry is CameraMappingConfig: camera_id, serial_number, enabled, name, target_fps
                    identity = self._config_service.create_camera_identity(
                        camera_id=entry.camera_id,
                        serial_number=entry.serial_number,
                        model="",
                        vendor="",
                        firmware="",
                        user_name=entry.name or "",
                    )
                    cfg = self._config_service.create_camera_config(
                        identity=identity,
                        name=entry.name or entry.camera_id,
                        thermal_enabled=True,
                        visible_enabled=False,
                    )
                    metadata = dict(cfg.metadata or {})
                    if getattr(entry, "ip_address", ""):
                        metadata["ip_address"] = entry.ip_address
                    if getattr(entry, "device_identifier", ""):
                        metadata["device_identifier"] = entry.device_identifier
                    if metadata:
                        cfg = CameraConfig(
                            identity=cfg.identity,
                            name=cfg.name,
                            description=cfg.description,
                            enabled=cfg.enabled,
                            thermal_enabled=cfg.thermal_enabled,
                            visible_enabled=cfg.visible_enabled,
                            ptz_config=cfg.ptz_config,
                            tags=cfg.tags,
                            metadata=metadata,
                        )
                    # Respect enabled flag from mapping; CameraConfig.enabled defaults True
                    if not entry.enabled:
                        # Recreate with enabled=False (CameraConfig is frozen)
                        cfg = CameraConfig(
                            identity=cfg.identity,
                            name=cfg.name,
                            description=cfg.description,
                            enabled=False,
                            thermal_enabled=cfg.thermal_enabled,
                            visible_enabled=cfg.visible_enabled,
                            ptz_config=cfg.ptz_config,
                            tags=cfg.tags,
                            metadata=dict(cfg.metadata) if cfg.metadata else {},
                        )
                        # Preserve target_fps hint in metadata if present
                        if entry.target_fps is not None:
                            meta = dict(cfg.metadata) if cfg.metadata else {}
                            meta["target_fps"] = entry.target_fps
                            cfg = CameraConfig(
                                identity=cfg.identity,
                                name=cfg.name,
                                description=cfg.description,
                                enabled=cfg.enabled,
                                thermal_enabled=cfg.thermal_enabled,
                                visible_enabled=cfg.visible_enabled,
                                ptz_config=cfg.ptz_config,
                                tags=cfg.tags,
                                metadata=meta,
                            )
                    else:
                        if entry.target_fps is not None:
                            meta = dict(cfg.metadata) if cfg.metadata else {}
                            meta["target_fps"] = entry.target_fps
                            cfg = CameraConfig(
                                identity=cfg.identity,
                                name=cfg.name,
                                description=cfg.description,
                                enabled=cfg.enabled,
                                thermal_enabled=cfg.thermal_enabled,
                                visible_enabled=cfg.visible_enabled,
                                ptz_config=cfg.ptz_config,
                                tags=cfg.tags,
                                metadata=meta,
                            )
                    self._config_service.set_camera_config(cfg)
                    logger.info("  MAPPING CAM %d: id=%r serial=%r name=%r enabled=%r thermal_enabled=%r position=%d", idx + 1, entry.camera_id, entry.serial_number, entry.name, entry.enabled, cfg.thermal_enabled, idx + 1)
                except Exception as exc:
                    logger.exception("Failed to hydrate mapping entry %d (%r): %s", idx, entry, exc)
            total_after = len(self._config_service.get_all_camera_configs())
            logger.info("CONFIG SERVICE HYDRATION COMPLETE: total_camera_configs=%d", total_after)
            for i, c in enumerate(self._config_service.get_all_camera_configs()):
                logger.info("  SERVICE CAM %d: id=%r name=%r serial=%r enabled=%r thermal_enabled=%r", i + 1, c.identity.camera_id, getattr(c, "name", ""), getattr(c.identity, "serial_number", ""), getattr(c, "enabled", "?"), getattr(c, "thermal_enabled", "?"))
        except Exception as exc:
            logger.exception("CONFIG SERVICE HYDRATION FAILED: %s", exc)

    def _configure_database(self, db_config) -> None:
        """Configure Database with DatabaseConfig."""
        from thermal_monitor.storage.database import Database, DatabaseConfig

        # Get password from environment
        password = self._config_manager.get_database_password()

        database_config = DatabaseConfig(
            server=db_config.host,
            database=db_config.name,
            username=db_config.username if not db_config.trusted_connection else None,
            password=password,
            driver=db_config.driver,
            trust_server_certificate=db_config.trust_server_certificate,
            connection_timeout=db_config.connection_timeout,
            command_timeout=db_config.command_timeout,
        )
        database = Database(database_config)
        self._database = database

    def _create_launcher_window(self) -> None:
        """Create the launcher window (recreates if the C++ object died)."""
        if self._launcher_alive():
            return

        self._launcher_window = LauncherWindow(
            mode_service=self._mode_service,
            config_service=self._config_service,
            discovery_service=self._discovery_service,
            theme_manager=self._theme_manager,
            config_manager=self._config_manager,
        )
        self._launcher_window.mode_requested.connect(self._on_mode_requested)

    def _create_live_window(self) -> LiveWindow:
        """Create the live window.

        The window owns WA_DeleteOnClose: closing it destroys the C++
        object, which fires ``destroyed`` so the controller can clear its
        flags and return to the Launcher — no application restart, no
        orphan windows. (Without the attribute, close() merely hides the
        window and the Launcher never comes back.)
        """
        if self._live_window is None:
            logger.info("CONTROLLER CREATE LIVE: config_service id=%s count=%d config_path=%s", hex(id(self._config_service)), len(self._config_service.get_all_camera_configs()), self._config_manager.config_path if self._config_manager else "unknown")
            self._live_window = LiveWindow(
                mode_service=self._mode_service,
                config_service=self._config_service,
                observer_service=self._observer_service,
                runtime_service=self._runtime_service,
                theme_manager=self._theme_manager,
                config_manager=self._config_manager,
            )
            logger.info("CONTROLLER LIVE CREATED: live_window config_service id=%s same_as_controller=%s", hex(id(self._live_window._config_service)), hex(id(self._live_window._config_service)) == hex(id(self._config_service)))
            self._live_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            self._live_window.destroyed.connect(self._on_live_window_destroyed)
        else:
            logger.info("CONTROLLER REUSE LIVE: config_service id=%s count=%d", hex(id(self._config_service)), len(self._config_service.get_all_camera_configs()))
        return self._live_window

    def _create_config_window(self) -> ConfigurationWindow:
        """Create the configuration window."""
        if self._config_window is None:
            logger.info("CONTROLLER CREATE CONFIG: config_service id=%s count=%d config_path=%s", hex(id(self._config_service)), len(self._config_service.get_all_camera_configs()), self._config_manager.config_path if self._config_manager else "unknown")
            self._config_window = ConfigurationWindow(
                config_service=self._config_service,
                mode_service=self._mode_service,
                runtime_service=self._runtime_service,
                discovery_service=self._discovery_service,
                theme_manager=self._theme_manager,
                config_manager=self._config_manager,
            )
            logger.info("CONTROLLER CONFIG CREATED: config_window config_service id=%s same_as_controller=%s live_same=%s", hex(id(self._config_window._config_service)), hex(id(self._config_window._config_service)) == hex(id(self._config_service)), hex(id(self._config_window._config_service)) == hex(id(self._live_window._config_service)) if self._live_window else "no_live")
            self._config_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            self._config_window.destroyed.connect(self._on_config_window_destroyed)
        else:
            logger.info("CONTROLLER REUSE CONFIG: config_service id=%s count=%d", hex(id(self._config_service)), len(self._config_service.get_all_camera_configs()))
        return self._config_window

    def _create_offline_window(self) -> OfflineWindow:
        """Create the offline window (delete-on-close, like the rest)."""
        if self._offline_window is None:
            self._offline_window = OfflineWindow(
                offline_service=self._offline_service,
                config_service=self._config_service,
                mode_service=self._mode_service,
                database=self._database,
                theme_manager=self._theme_manager,
                config_manager=self._config_manager,
            )
            self._offline_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            self._offline_window.destroyed.connect(self._on_offline_window_destroyed)
        return self._offline_window

    @pyqtSlot(ApplicationMode)
    def _on_mode_requested(self, mode: ApplicationMode) -> None:
        """Handle mode request from launcher."""
        if mode == ApplicationMode.LIVE:
            self._request_live_mode()
        elif mode == ApplicationMode.CONFIGURATION:
            self._request_configuration_mode()
        elif mode == ApplicationMode.OFFLINE:
            self._request_offline_mode()
        # LAUNCHER is not requested from launcher

    def _request_live_mode(self) -> None:
        """Request to open Live mode with mutual exclusion check."""
        if self._mode_service.is_configuration_active() or self._config_open:
            # Exclusive hardware: leave Configuration first WITHOUT
            # freezing, then enter Live automatically once the visible
            # transition has handed control back. The Launcher shell
            # stays responsive throughout.
            self._queue_mode_after_leave(ApplicationMode.LIVE)
            return

        t0 = self._transition_begin("Launcher -> Live")
        self._ensure_launcher_hidden()
        live_window = self._create_live_window()
        live_window.on_mode_activated()
        self._transition_state = TransitionState.ACTIVE
        live_window.showMaximized()
        self._live_open = True
        self._mode_service.set_live_active(True)
        self._update_launcher_buttons()
        self._transition_visible_ns = time.perf_counter_ns()
        self._transition_mark("live_visible")
        self._transition_end()

    def _request_configuration_mode(self) -> None:
        """Request to open Configuration mode with mutual exclusion check."""
        if self._mode_service.is_live_active() or self._live_open:
            # Exclusive hardware: leave Live first WITHOUT freezing,
            # then enter Configuration automatically. See above.
            self._queue_mode_after_leave(ApplicationMode.CONFIGURATION)
            return

        t0 = self._transition_begin("Launcher -> Configuration")
        self._ensure_launcher_hidden()
        config_window = self._create_config_window()
        config_window.on_mode_activated()
        self._transition_state = TransitionState.ACTIVE
        config_window.showMaximized()
        self._config_open = True
        self._mode_service.set_configuration_active(True)
        self._update_launcher_buttons()
        self._transition_visible_ns = time.perf_counter_ns()
        self._transition_mark("configuration_visible")
        self._transition_end()

    def _request_offline_mode(self) -> None:
        """Request to open Offline mode (independent, no mutual exclusion)."""
        t0 = self._transition_begin("Launcher -> Offline")
        offline_window = self._create_offline_window()
        offline_window.on_mode_activated()
        offline_window.showMaximized()
        self._transition_visible_ns = time.perf_counter_ns()
        self._transition_mark("offline_visible")
        self._transition_end()

    def _queue_mode_after_leave(self, mode: ApplicationMode) -> None:
        """Leave the current exclusive mode, then enter ``mode`` seamlessly.

        Used for direct Configuration <-> Live switches: the current
        mode is hidden and its teardown starts in the background while
        the Launcher shell is already interactive; the destination mode
        is entered as soon as the source window is destroyed (its
        resources are released by then). The GUI thread never waits.
        """
        self._pending_mode = mode
        if self._config_open and self._config_window is not None:
            self._leave_mode_to_launcher("Configuration", self._config_window)
        elif self._live_open and self._live_window is not None:
            self._leave_mode_to_launcher("Live", self._live_window)
        else:
            # Flags disagree with reality: enter directly.
            pending, self._pending_mode = self._pending_mode, None
            if pending is not None:
                self._on_mode_requested(pending)

    def _ensure_launcher_hidden(self) -> None:
        """Hide launcher window if visible."""
        if self._launcher_alive():
            try:
                if self._launcher_window.isVisible():
                    self._launcher_window.hide()
            except RuntimeError:
                pass

    def _show_launcher(self) -> None:
        """Show the persistent Launcher immediately (never blocks).

        The window is shown, raised and activated FIRST so it paints
        without delay; camera discovery refreshes asynchronously behind
        it (see ``LauncherWindow.refresh_discovery_async``) instead of
        stalling the first paint with a multi-second GVCP broadcast.
        """
        if not self._launcher_alive():
            self._create_launcher_window()
        try:
            self._launcher_window.showMaximized()
        except RuntimeError:
            return  # torn down mid-show; shutdown owns the rest
        self._transition_visible_ns = time.perf_counter_ns()
        self._transition_mark("launcher_visible")
        try:
            self._launcher_window.notify_transition_shown()
        except RuntimeError:
            pass
        self._update_launcher_buttons()
        try:
            self._launcher_window.refresh_discovery_async()
        except RuntimeError:
            pass

    # -- Seamless-transition orchestration ----------------------------------
    #
    # Ordering (the whole point): USER CLICKS BACK/CLOSE -> detach mode
    # from the active UI -> show the Launcher IMMEDIATELY -> continue
    # camera/process/SHM/worker teardown asynchronously. The GUI thread
    # never waits for teardown, and the Launcher never gets
    # destroyed/recreated for ordinary navigation.

    def _transition_begin(self, label: str) -> int:
        """Start transition instrumentation; returns the request timestamp."""
        self._transition_generation += 1
        self._transition_label = label
        self._transition_t0_ns = time.perf_counter_ns()
        self._transition_visible_ns = 0
        self._gap_max_ms = 0.0
        self._gap_last_ns = self._transition_t0_ns
        logger.info("[MODE-TRANSITION] %s requested t_ns=%d", label, self._transition_t0_ns)
        try:
            if self._gap_timer is None:
                self._gap_timer = QTimer(self)
                self._gap_timer.setInterval(25)
                self._gap_timer.timeout.connect(self._gap_heartbeat)
            self._gap_timer.start()
        except RuntimeError:
            pass  # headless/test harness without event loop
        return self._transition_t0_ns

    def _gap_heartbeat(self) -> None:
        """Record the worst GUI event-loop stall during a transition."""
        now = time.perf_counter_ns()
        gap_ms = (now - self._gap_last_ns) / 1e6
        self._gap_last_ns = now
        if gap_ms > self._gap_max_ms:
            self._gap_max_ms = gap_ms

    def _transition_mark(self, stage: str) -> None:
        """Log one transition stage with its request-relative offset."""
        if not self._transition_t0_ns:
            return
        logger.info(
            "[MODE-TRANSITION] %s %s +%0.1fms",
            self._transition_label,
            stage,
            (time.perf_counter_ns() - self._transition_t0_ns) / 1e6,
        )

    def _transition_end(self) -> None:
        """Stop the gap watchdog and log the transition summary."""
        if not self._transition_t0_ns:
            return
        try:
            if self._gap_timer is not None:
                self._gap_timer.stop()
        except RuntimeError:
            pass
        now = time.perf_counter_ns()
        total_ms = (now - self._transition_t0_ns) / 1e6
        visible_ms = (
            (self._transition_visible_ns - self._transition_t0_ns) / 1e6
            if self._transition_visible_ns
            else -1.0
        )
        logger.info(
            "[MODE-TRANSITION] %s transition_complete total=%0.1fms "
            "request_to_visible=%0.1fms max_event_loop_gap=%0.1fms",
            self._transition_label,
            total_ms,
            visible_ms,
            self._gap_max_ms,
        )
        if self._gap_max_ms > 50.0:
            logger.warning(
                "[MODE-TRANSITION] %s GUI event-loop gap %0.1fms exceeded "
                "50 ms budget (see stage timestamps above for the cause)",
                self._transition_label,
                self._gap_max_ms,
            )
        self._transition_state = TransitionState.IDLE
        self._transition_t0_ns = 0
        self._transition_label = ""

    def _leave_mode_to_launcher(self, kind: str, window) -> None:
        """Leave ``kind`` mode for the Launcher WITHOUT blocking.

        1. The Launcher is shown FIRST (already painted before the mode
           hides, so no blank/grey intermediate frame is exposed).
        2. The mode window is hidden immediately (fast, no teardown).
        3. Ownership flags and the ModeService are updated
           synchronously so mutual exclusion stays exact even while
           background teardown still runs.
        4. The actual ``close()`` (fast closeEvent + ``WA_DeleteOnClose``
           destruction) is deferred to the event loop; the ``destroyed``
           handler only clears the reference and completes the
           transition — it never re-blocks the GUI.
        """
        self._transition_begin(f"{kind} -> Launcher")
        self._transition_state = TransitionState.LEAVING_MODE
        generation = self._transition_generation
        # 1. Launcher first: visible before the mode disappears.
        self._show_launcher()
        # 2. Detach the mode from the active UI immediately.
        try:
            window.hide()
        except RuntimeError:
            pass  # C++ object already gone
        self._transition_mark("mode_hidden")
        # 3. Synchronous ownership handoff (fast, in-memory only).
        if kind == "Live":
            self._live_open = False
            try:
                self._mode_service.set_live_active(False)
            except Exception:
                pass
        elif kind == "Configuration":
            self._config_open = False
            try:
                self._mode_service.set_configuration_active(False)
            except Exception:
                pass
        self._update_launcher_buttons()
        # 4. Deferred close: the window's own fast closeEvent detaches
        # workers/observers without waiting; heavy teardown continues in
        # background threads owned by the mode widgets.
        try:
            QTimer.singleShot(
                0, lambda _w=window, _k=kind, _g=generation: self._finish_mode_close(_w, _k, _g)
            )
        except RuntimeError:
            self._transition_end()

    def _finish_mode_close(self, window, kind: str, generation: int) -> None:
        """Deferred ``close()`` for a hidden mode window (event-loop turn).

        Stale generations (a newer transition already owns the UI) still
        close their window — resource cleanup must be deterministic —
        but never disturb the visible Launcher.
        """
        if generation != self._transition_generation:
            logger.debug(
                "[MODE-TRANSITION] %s stale close (gen %d != %d); closing quietly",
                kind,
                generation,
                self._transition_generation,
            )
        try:
            already_gone = isdeleted(window)
        except Exception:
            already_gone = False
        if already_gone:
            # The C++ object died without a destroyed delivery reaching
            # us: clear the reference and complete the transition here so
            # navigation (and any pending mode switch) can never stall.
            self._transition_mark(f"{kind.lower()}_destroyed")
            self._transition_end()
            if kind == "Live":
                self._live_window = None
            elif kind == "Configuration":
                self._config_window = None
            self._enter_pending_mode()
            return
        self._transition_mark(f"{kind.lower()}_close_started")
        try:
            window.close()
        except RuntimeError:
            pass  # already destroyed
        except Exception:
            logger.debug("Mode window close failed", exc_info=True)

    def _launcher_visible(self) -> bool:
        """True when the persistent Launcher is currently on screen."""
        if not self._launcher_alive():
            return False
        try:
            return bool(self._launcher_window.isVisible())
        except RuntimeError:
            return False

    def _launcher_alive(self) -> bool:
        """True when the launcher window exists and its C++ object is alive.

        Guards every cross-window touch: during teardown Qt may destroy
        child widgets (e.g. mode buttons) while this controller still holds
        the Python wrapper, and any call into them raises RuntimeError.
        """
        window = self._launcher_window
        if window is None:
            return False
        try:
            return not isdeleted(window)
        except Exception:
            return False

    def _update_launcher_buttons(self) -> None:
        """Update launcher mode button enabled states based on mutual exclusion.

        Safe during teardown: the launcher shell may outlive its already
        destroyed child buttons (Qt destroys C++ children in arbitrary
        order), in which case the update is skipped with a log instead of
        raising RuntimeError on a deleted QPushButton.
        """
        if not self._launcher_alive():
            logger.debug("Launcher buttons update skipped (launcher not alive)")
            return
        try:
            self._launcher_window.set_mode_buttons_enabled(
                live_enabled=not self._config_open,
                config_enabled=not self._live_open,
            )
        except RuntimeError as exc:
            logger.debug("Launcher buttons update skipped (teardown race): %s", exc)

    @pyqtSlot(ModeState)
    def _on_mode_changed(self, state: ModeState) -> None:
        """Handle mode changes from ModeService (for external transitions)."""
        # This handles programmatic mode changes
        # The actual window management is done via _request_* methods
        pass

    def _on_live_window_destroyed(self) -> None:
        """Handle Live window destruction (idempotent, never blocks).

        Normal flow (Back/leave): the Launcher is already visible and
        ownership was handed off synchronously — just clear the
        reference and complete the transition. Direct-close flow (window
        X button): perform the handoff here; the window's own fast
        closeEvent already detached workers/observers without waiting,
        so showing the Launcher stays instant.
        """
        if self._shutting_down:
            return
        self._live_window = None
        if self._live_open:
            # Direct close: no leave-flow ran yet.
            self._transition_begin("Live -> Launcher")
        self._live_open = False
        try:
            self._mode_service.set_live_active(False)
        except Exception:
            pass
        if not self._launcher_visible():
            # Launcher is not on screen yet (direct X-close, or the
            # leave-flow show was lost): bring it back now. When the
            # leave-flow already showed it this is a no-op by design.
            self._show_launcher()
        self._update_launcher_buttons()
        self._transition_mark("live_destroyed")
        self._transition_end()
        self._enter_pending_mode()

    def _on_config_window_destroyed(self) -> None:
        """Handle Configuration window destruction (see above)."""
        if self._shutting_down:
            return
        self._config_window = None
        if self._config_open:
            # Direct close: no leave-flow ran yet.
            self._transition_begin("Configuration -> Launcher")
        self._config_open = False
        try:
            self._mode_service.set_configuration_active(False)
        except Exception:
            pass
        if not self._launcher_visible():
            self._show_launcher()
        self._update_launcher_buttons()
        self._transition_mark("configuration_destroyed")
        self._transition_end()
        self._enter_pending_mode()

    def _enter_pending_mode(self) -> None:
        """Enter a mode queued by a direct Configuration <-> Live switch."""
        pending, self._pending_mode = self._pending_mode, None
        if pending is None or self._shutting_down:
            return
        if self._live_open or self._config_open:
            # Source teardown has not landed yet; re-queue behind it.
            self._pending_mode = pending
            return
        self._on_mode_requested(pending)

    def _on_offline_window_destroyed(self) -> None:
        """Handle Offline window close."""
        if self._shutting_down:
            return
        self._offline_window = None
        # Offline is independent, launcher not affected
        self._transition_mark("offline_destroyed")

    def shutdown(self) -> None:
        """Clean shutdown of all windows."""
        self._shutting_down = True
        self._pending_mode = None
        try:
            if self._gap_timer is not None:
                self._gap_timer.stop()
        except RuntimeError:
            pass
        # Close mode windows first (their fast closeEvents detach the
        # display path without waiting; determinism comes from the
        # runtime shutdown below, not from GUI-thread joins).
        for window in (self._live_window, self._config_window, self._offline_window):
            try:
                if window is not None:
                    window.close()
            except RuntimeError:
                pass  # C++ object already gone
            except Exception:
                logger.debug("Mode window close failed during shutdown", exc_info=True)
        if self._launcher_alive():
            try:
                self._launcher_window.close()
            except RuntimeError:
                pass
            except Exception:
                logger.debug("Launcher close failed during shutdown", exc_info=True)

        # Shutdown runtime service
        self._runtime_service.shutdown()

    @property
    def launcher_window(self) -> LauncherWindow | None:
        return self._launcher_window

    @property
    def live_window(self) -> LiveWindow | None:
        return self._live_window

    @property
    def configuration_window(self) -> ConfigurationWindow | None:
        return self._config_window

    @property
    def offline_window(self) -> OfflineWindow | None:
        return self._offline_window

    @property
    def is_live_open(self) -> bool:
        return self._live_open

    @property
    def is_configuration_open(self) -> bool:
        return self._config_open


__all__ = ["AppController"]