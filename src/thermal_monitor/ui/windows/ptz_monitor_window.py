"""
ui.windows.ptz_monitor_window -- PLC & PTZ live monitoring window.

Standalone, read-only observer of PLC communication and PTZ status.
Independent of the Live/Configuration/Offline modes: it owns a private
:class:`PtzService` (own OPC UA session) and never touches camera
acquisition, modes, or the Siemens mapping gate.

Polling model (never blocks the GUI thread):

* a single QTimer fires on the GUI thread and spawns at most one
  background daemon worker (overlap guard);
* the worker performs all blocking OPC UA reads, then emits one queued
  snapshot signal carrying the window generation;
* the slot drops snapshots whose generation is stale (window closed or
  replaced) before touching any widget.

Closing the window stops the timer, invalidates pending snapshots, and
shuts the owned service down in a background thread. Application
shutdown closes the window through the normal controller path.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from thermal_monitor.ui.theme import ThemeManager
from thermal_monitor.ui.theme.properties import set_role, set_variant
from thermal_monitor.ptz.state import PlcConnectionState

logger = logging.getLogger(__name__)

#: Columns of the per-PTZ status table.
PTZ_COLUMNS = (
    "PTZ",
    "Camera",
    "Conn",
    "Ready",
    "Pan act",
    "Tilt act",
    "Pan tgt",
    "Tilt tgt",
    "Move",
    "Calib",
    "Active pos",
    "Error",
    "Updated",
)

_UNKNOWN = "Unknown"
_UNAVAILABLE = "Unavailable"


@dataclass
class PtzRowSnapshot:
    """One PTZ's polled state. ``None`` values render as Unknown."""

    ptz_id: str = ""
    camera_id: str = ""
    connection: str = _UNKNOWN
    comm_ok: bool = False
    ready: str = _UNKNOWN
    actual_pan: Optional[float] = None
    actual_tilt: Optional[float] = None
    target_pan: Optional[float] = None
    target_tilt: Optional[float] = None
    movement: str = _UNKNOWN
    calibration: str = _UNKNOWN
    active_position: str = _UNKNOWN
    error: str = ""
    updated: str = ""


@dataclass
class PlcSummarySnapshot:
    """PLC/session-level rollup for the summary panel."""

    profile: str = _UNKNOWN
    endpoint: str = _UNKNOWN
    connection: str = _UNKNOWN
    session: str = _UNKNOWN
    comm_health: str = _UNKNOWN
    last_update: str = "—"
    reconnects_observed: int = 0
    error: str = ""


@dataclass
class MonitorSnapshot:
    """Full poll payload delivered to the GUI thread."""

    summary: PlcSummarySnapshot = field(default_factory=PlcSummarySnapshot)
    rows: list = field(default_factory=list)


def _fmt_float(value: Optional[float]) -> str:
    if value is None:
        return _UNKNOWN
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return _UNKNOWN


def _fmt_time(epoch_seconds: float) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(epoch_seconds))
    except (TypeError, ValueError, OverflowError):
        return _UNKNOWN


class PtzMonitorWindow(QMainWindow):
    """Top-level read-only PLC & PTZ monitor window."""

    # Snapshot from the background poll worker: (generation, MonitorSnapshot).
    _poll_ready = pyqtSignal(int, object)

    def __init__(
        self,
        config_manager=None,
        theme_manager: Optional[ThemeManager] = None,
        *,
        service=None,
        bindings: Optional[list] = None,
        poll_interval_ms: int = 500,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._config_manager = config_manager
        self._theme = theme_manager
        self._service = service
        self._owns_service = service is None
        self._preset_bindings = list(bindings) if bindings else None
        self._poll_interval_ms = max(100, int(poll_interval_ms))

        self._generation = 0
        self._closing = False
        self._poll_busy = False
        self._poll_timer: QTimer | None = None
        self._reconnects_observed = 0
        self._last_overall_ok: Optional[bool] = None
        self._service_failed_logged = False
        self._link_notified = False
        # Last reached target latched per PTZ: (pan, tilt, epoch).
        self._reached_latch: dict[str, tuple] = {}

        self.setWindowTitle("Thermal Monitoring System V3 - PLC & PTZ Monitor")
        self.setMinimumSize(1100, 480)
        self._setup_ui()
        self._poll_ready.connect(self._apply_snapshot)

    # -- UI construction -------------------------------------------------

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("PLC & PTZ Monitor (read-only)")
        set_role(title, "title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        summary_group = QGroupBox("PLC Summary")
        form = QFormLayout(summary_group)
        self._summary_labels: dict[str, QLabel] = {}
        for key, caption in (
            ("profile", "Profile:"),
            ("endpoint", "Endpoint:"),
            ("connection", "Connection:"),
            ("session", "Session:"),
            ("comm_health", "Comm health:"),
            ("last_update", "Last update:"),
            ("reconnects_observed", "Reconnects observed:"),
            ("error", "Error:"),
        ):
            label = QLabel("—")
            self._summary_labels[key] = label
            form.addRow(caption, label)
        layout.addWidget(summary_group)

        grid_group = QGroupBox("PTZ Units")
        grid_layout = QVBoxLayout(grid_group)
        self._table = QTableWidget(0, len(PTZ_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(PTZ_COLUMNS))
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        grid_layout.addWidget(self._table)
        layout.addWidget(grid_group, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self._reconnect_btn = QPushButton("Reconnect")
        self._reconnect_btn.setMinimumWidth(140)
        set_variant(self._reconnect_btn, "secondary")
        self._reconnect_btn.clicked.connect(self._on_reconnect_clicked)
        buttons.addWidget(self._reconnect_btn)
        self._close_btn = QPushButton("Close")
        self._close_btn.setMinimumWidth(140)
        self._close_btn.clicked.connect(self.close)
        buttons.addWidget(self._close_btn)
        buttons.addStretch()
        layout.addLayout(buttons)

        self._status_label = QLabel("Not connected")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_role(self._status_label, "status")
        layout.addWidget(self._status_label)

        status_bar = QStatusBar()
        self.setStatusBar(status_bar)
        self._status_bar_label = QLabel("Monitor idle")
        status_bar.addWidget(self._status_bar_label)

    # -- public lifecycle --------------------------------------------------

    def start_monitoring(self) -> None:
        """Begin background polling (idempotent, never blocks)."""
        if self._closing:
            return
        if self._poll_timer is None:
            self._poll_timer = QTimer(self)
            self._poll_timer.timeout.connect(self._on_poll_tick)
        if not self._poll_timer.isActive():
            self._poll_timer.start(self._poll_interval_ms)
        try:
            self._status_bar_label.setText("Monitor running")
        except RuntimeError:
            pass

    def stop_monitoring(self) -> None:
        """Stop polling; in-flight snapshots are dropped by generation."""
        self._generation += 1
        if self._poll_timer is not None:
            try:
                self._poll_timer.stop()
            except RuntimeError:
                pass

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        """Detach fast; service shutdown continues in the background."""
        self._closing = True
        self.stop_monitoring()
        service, owns = self._service, self._owns_service
        self._service = None

        def _shutdown() -> None:
            if owns and service is not None:
                try:
                    service.shutdown(timeout_s=5.0)
                except Exception:
                    logger.debug("Monitor service shutdown failed", exc_info=True)

        threading.Thread(target=_shutdown, name="PtzMonitorShutdown", daemon=True).start()
        try:
            super().closeEvent(event)
        except RuntimeError:
            pass

    # -- polling -----------------------------------------------------------

    def _on_poll_tick(self) -> None:
        if self._closing or self._poll_busy:
            return
        self._poll_busy = True
        generation = self._generation

        def _worker() -> None:
            try:
                snapshot = self._poll_once()
            except Exception as exc:
                logger.debug("Monitor poll failed: %s", exc)
                snapshot = None
            finally:
                self._poll_busy = False
            if snapshot is None:
                return
            try:
                self._poll_ready.emit(generation, snapshot)
            except RuntimeError:
                pass  # window torn down mid-poll

        threading.Thread(target=_worker, name="PtzMonitorPoll", daemon=True).start()

    def _poll_once(self) -> Optional[MonitorSnapshot]:
        """Blocking poll body: runs on the worker thread only."""
        from thermal_monitor.ptz.siemens import validate_profile

        profile, endpoint = self._profile_and_endpoint()
        status = validate_profile(
            profile,
            endpoint,
            self._security_mode(),
            self._username(),
        )
        if not status.ready or status.mapping != "simulator":
            return MonitorSnapshot(
                summary=PlcSummarySnapshot(
                    profile=profile or _UNKNOWN,
                    endpoint=endpoint or _UNKNOWN,
                    connection=_UNAVAILABLE,
                    session=_UNAVAILABLE,
                    comm_health=_UNAVAILABLE,
                    error=status.reason,
                ),
                rows=[],
            )
        service = self._ensure_service(status.endpoint)
        if service is None:
            if not self._service_failed_logged:
                logger.warning(
                    "[PTZ-MONITOR] service unavailable; showing Unavailable "
                    "until reachable (further failures logged at debug)"
                )
                self._service_failed_logged = True
            return MonitorSnapshot(
                summary=PlcSummarySnapshot(
                    profile=status.profile,
                    endpoint=status.endpoint,
                    connection=_UNAVAILABLE,
                    session=_UNAVAILABLE,
                    comm_health=_UNAVAILABLE,
                    error="PTZ service unavailable (see logs)",
                ),
                rows=[],
            )
        ptz_ids = self._ptz_ids(status)
        rows: list[PtzRowSnapshot] = []
        worst_error = ""
        states: set[str] = set()
        connected_count = 0
        for ptz_id in ptz_ids:
            row = self._poll_one_ptz(service, ptz_id)
            rows.append(row)
            states.add(row.connection)
            # Health is authoritative per read, not per session: the
            # session object keeps its last known state until loss is
            # reported, while communication_ok reflects this poll.
            if row.comm_ok and row.connection == PlcConnectionState.CONNECTED.value:
                connected_count += 1
            if row.error and not worst_error:
                worst_error = row.error
        overall_ok = bool(rows) and connected_count == len(rows)
        if overall_ok:
            self._service_failed_logged = False
            self._link_notified = False
        elif rows and not self._link_notified:
            # Reads alone never flip session state (existing contract);
            # report the observed dead link once so the session owns
            # bounded reconnect until health returns.
            try:
                notify = getattr(service, "notify_connection_lost", None)
                if notify is not None:
                    notify("monitor poll observed dead link")
            except Exception:
                pass
            self._link_notified = True
        if self._last_overall_ok is False and overall_ok is True:
            self._reconnects_observed += 1
        self._last_overall_ok = overall_ok
        if len(states) == 1:
            session = next(iter(states))
        else:
            session = "Mixed"
        summary = PlcSummarySnapshot(
            profile=status.profile,
            endpoint=status.endpoint,
            connection="Connected" if overall_ok else "Disconnected",
            session=session,
            comm_health="OK" if overall_ok else "LOST",
            last_update=_fmt_time(time.time()),
            reconnects_observed=self._reconnects_observed,
            error=worst_error,
        )
        return MonitorSnapshot(summary=summary, rows=rows)

    def _poll_one_ptz(self, service, ptz_id: str) -> PtzRowSnapshot:
        """Read one PTZ's status + last commanded target (read-only)."""
        from thermal_monitor.ptz.mapping import LogicalField

        camera_id = self._camera_for_ptz(ptz_id)
        try:
            status = service.status_for_ptz(ptz_id)
        except Exception as exc:
            logger.debug("Monitor status read failed for %s: %s", ptz_id, exc)
            return PtzRowSnapshot(ptz_id=ptz_id, camera_id=camera_id)
        target_pan = target_tilt = None
        try:
            target_pan = service.read_field(ptz_id, LogicalField.TARGET_PAN)
            target_tilt = service.read_field(ptz_id, LogicalField.TARGET_TILT)
            target_pan = float(target_pan)
            target_tilt = float(target_tilt)
        except Exception:
            target_pan = target_tilt = None
        if status.position_reached and target_pan is not None:
            self._reached_latch[ptz_id] = (target_pan, target_tilt, time.time())
        latched = self._reached_latch.get(ptz_id)
        active = (
            f"{latched[0]:.2f},{latched[1]:.2f}" if latched is not None else _UNKNOWN
        )
        error = ""
        if status.error is not None:
            code = getattr(status.error, "code", "")
            message = getattr(status.error, "message", "")
            error = f"{code}: {message}" if code else str(message)
        connection = (
            status.plc_state.value
            if hasattr(status.plc_state, "value")
            else str(status.plc_state)
        )
        if not status.communication_ok:
            # Session state goes stale on loss (it only changes on
            # reported events); mark it so the grid never implies a
            # live link.
            connection = f"{connection} (stale)"
        return PtzRowSnapshot(
            ptz_id=ptz_id,
            camera_id=camera_id,
            connection=connection,
            comm_ok=bool(status.communication_ok),
            ready="Yes" if status.ready else "No",
            actual_pan=status.actual_pan,
            actual_tilt=status.actual_tilt,
            target_pan=target_pan,
            target_tilt=target_tilt,
            movement=status.movement.value
            if hasattr(status.movement, "value")
            else str(status.movement),
            calibration=status.calibration.value
            if hasattr(status.calibration, "value")
            else str(status.calibration),
            active_position=active,
            error=error,
            updated=_fmt_time(time.time()),
        )

    # -- service + configuration -------------------------------------------

    def _profile_and_endpoint(self) -> tuple[str, str]:
        if self._config_manager is None:
            return "simulator", ""
        try:
            ptz_cfg = self._config_manager.get_config().ptz
        except Exception:
            return "simulator", ""
        return (
            getattr(ptz_cfg, "profile", "simulator") or "simulator",
            getattr(ptz_cfg, "endpoint", "") or "",
        )

    def _security_mode(self) -> str:
        if self._config_manager is None:
            return "none"
        try:
            return getattr(self._config_manager.get_config().ptz, "security_mode", "none")
        except Exception:
            return "none"

    def _username(self) -> str:
        if self._config_manager is None:
            return ""
        try:
            return getattr(self._config_manager.get_config().ptz, "username", "")
        except Exception:
            return ""

    def _ptz_ids(self, profile_status) -> list[str]:
        """PTZ units to display: bound units, else the simulator's 8."""
        ids: list[str] = []
        for camera_id, ptz_id in self._bindings():
            if ptz_id not in ids:
                ids.append(ptz_id)
        if not ids and profile_status.profile == "simulator":
            # Simulator convention: 8 units PTZ_01..PTZ_08. Documented
            # development default, never a Siemens claim.
            from thermal_monitor.ptz.mapping import default_ptz_ids

            ids.extend(default_ptz_ids())
        return ids

    def _bindings(self) -> list[tuple[str, str]]:
        """(camera_id, ptz_id) pairs from config, or preset (tests)."""
        if self._preset_bindings is not None:
            return list(self._preset_bindings)
        if self._config_manager is None:
            return []
        try:
            mappings = self._config_manager.get_config().cameras.mapping
        except Exception:
            return []
        pairs = []
        for mapping in mappings or []:
            ptz_id = (getattr(mapping, "ptz_id", "") or "").strip()
            camera_id = (getattr(mapping, "camera_id", "") or "").strip()
            if ptz_id and camera_id:
                pairs.append((camera_id, ptz_id))
        return pairs

    def _camera_for_ptz(self, ptz_id: str) -> str:
        for camera_id, bound_ptz in self._bindings():
            if bound_ptz == ptz_id:
                return camera_id
        return "—"

    def _ensure_service(self, endpoint: str):
        """Lazily build + connect the owned service (worker thread only)."""
        if self._service is not None:
            return self._service
        if not self._owns_service:
            return None
        try:
            from thermal_monitor.ptz.mapping import SimulatorPtzMapping
            from thermal_monitor.ptz.models import PtzLimits, PtzStationBinding
            from thermal_monitor.ptz.station import (
                build_service,
                build_service_config,
            )

            ptz_cfg = (
                self._config_manager.get_config().ptz
                if self._config_manager is not None
                else None
            )
            ptz_ids = tuple(
                sorted(
                    {
                        ptz_id
                        for _, ptz_id in self._bindings()
                    }
                )
            )
            if not ptz_ids:
                from thermal_monitor.ptz.mapping import default_ptz_ids

                ptz_ids = default_ptz_ids()
            limits = getattr(ptz_cfg, "limits", None) if ptz_cfg is not None else None
            if limits is not None:
                service_config = build_service_config(
                    limits=PtzLimits(
                        min_pan=limits.min_pan,
                        max_pan=limits.max_pan,
                        min_tilt=limits.min_tilt,
                        max_tilt=limits.max_tilt,
                        min_velocity=0.1,
                        max_velocity=360.0,
                    ),
                    tolerance_pan=getattr(ptz_cfg, "tolerance_pan", 0.5),
                    tolerance_tilt=getattr(ptz_cfg, "tolerance_tilt", 0.5),
                    move_timeout_s=getattr(ptz_cfg, "move_timeout_s", 30.0),
                    calibration_timeout_s=getattr(
                        ptz_cfg, "calibration_timeout_s", 120.0
                    ),
                    monitor_interval_s=getattr(ptz_cfg, "monitor_interval_s", 0.5),
                )
            else:
                service_config = build_service_config()
            service = build_service(
                endpoint,
                SimulatorPtzMapping(ptz_ids=ptz_ids),
                service_config,
            )
            for camera_id, bound_ptz in self._bindings():
                try:
                    service.register_binding(
                        PtzStationBinding(camera_id=camera_id, ptz_id=bound_ptz)
                    )
                except Exception:
                    pass
            service.connect()
        except Exception as exc:
            # Debug only: _poll_once logs one warning per outage.
            logger.debug("[PTZ-MONITOR] service unavailable: %s", exc)
            return None
        self._service = service
        return service

    # -- GUI slots -----------------------------------------------------------

    @pyqtSlot(int, object)
    def _apply_snapshot(self, generation: int, snapshot: MonitorSnapshot) -> None:
        """Apply a poll snapshot (GUI thread only; stale-safe)."""
        if generation != self._generation or self._closing:
            return
        try:
            self._apply_summary(snapshot.summary)
            self._apply_rows(snapshot.rows)
            self._status_label.setText(f"Updated {snapshot.summary.last_update}")
        except RuntimeError:
            pass  # torn down mid-update

    def _apply_summary(self, summary: PlcSummarySnapshot) -> None:
        self._summary_labels["profile"].setText(summary.profile)
        self._summary_labels["endpoint"].setText(summary.endpoint)
        self._summary_labels["connection"].setText(summary.connection)
        self._summary_labels["session"].setText(summary.session)
        self._summary_labels["comm_health"].setText(summary.comm_health)
        self._summary_labels["last_update"].setText(summary.last_update)
        self._summary_labels["reconnects_observed"].setText(
            str(summary.reconnects_observed)
        )
        self._summary_labels["error"].setText(summary.error or "—")

    def _apply_rows(self, rows: list[PtzRowSnapshot]) -> None:
        self._table.setRowCount(0)
        for row in rows:
            index = self._table.rowCount()
            self._table.insertRow(index)
            values = (
                row.ptz_id or _UNKNOWN,
                row.camera_id or "—",
                row.connection,
                row.ready,
                _fmt_float(row.actual_pan),
                _fmt_float(row.actual_tilt),
                _fmt_float(row.target_pan),
                _fmt_float(row.target_tilt),
                row.movement,
                row.calibration,
                row.active_position,
                row.error or "—",
                row.updated or "—",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(index, column, item)

    def _on_reconnect_clicked(self) -> None:
        """Best-effort reconnect in a worker thread (never blocks)."""
        if self._closing:
            return
        try:
            self._status_bar_label.setText("Reconnecting...")
        except RuntimeError:
            return

        def _worker() -> None:
            service = self._service
            if service is None:
                return
            try:
                service.disconnect()
            except Exception:
                pass
            try:
                service.connect()
            except Exception as exc:
                logger.debug("Monitor reconnect failed: %s", exc)

        threading.Thread(target=_worker, name="PtzMonitorReconnect", daemon=True).start()


__all__ = ["PtzMonitorWindow", "MonitorSnapshot", "PlcSummarySnapshot", "PtzRowSnapshot", "PTZ_COLUMNS"]
