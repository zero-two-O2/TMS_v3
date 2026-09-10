"""Camera discovery: native GVCP broadcast (primary) + HALCON fallback.

Discovery is deliberately separate from acquisition. Each backend opens a
temporary handle only long enough to read device metadata and always
closes it. GVCP discovery is the normal application path; the HALCON
GigE Vision backend exists only as an explicit fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiscoveredCamera:
    """Metadata for one physical camera (either discovery backend)."""

    device_identifier: str
    serial_number: str = ""
    ip_address: str = ""
    model: str = ""
    vendor: str = ""
    firmware: str = ""
    user_name: str = ""
    # Stage 8D: local interface that reaches the camera (GVCP backend fills
    # this in; the custom driver needs it and V3 must not hardcode it).
    local_ip: str = ""
    # Provenance: "halcon" | "gvcp" | "static".
    source: str = "halcon"

    @property
    def stable_identity(self) -> str:
        """Prefer the hardware serial, otherwise the device identifier."""
        return self.serial_number or self.device_identifier

    @property
    def camera_id(self) -> str:
        return f"cam_{self.stable_identity}"


@dataclass(frozen=True, slots=True)
class GvcpCameraState:
    """Per-IP classification from a GVCP scan (Stage 8D).

    States: ``discovered`` (answered broadcast), ``static-verified``
    (answered unicast DISCOVERY), ``gvcp-nonresponsive`` (ignores DISCOVERY
    but answers register reads -- present but discovery-shy, e.g. the known
    .16 case), ``unreachable`` (answers nothing).
    """

    ip_address: str
    state: str
    camera: "DiscoveredCamera | None" = None
    local_ip: str = ""


@dataclass
class GvcpScanReport:
    """Merged result of broadcast discovery + static verification."""

    found: list[DiscoveredCamera]
    states: list[GvcpCameraState]
    duplicates: list[str]

    @property
    def unreachable(self) -> list[str]:
        return [s.ip_address for s in self.states if s.state == "unreachable"]

    @property
    def nonresponsive(self) -> list[str]:
        return [s.ip_address for s in self.states if s.state == "gvcp-nonresponsive"]


class CameraDiscoveryError(RuntimeError):
    """Discovery was unavailable or failed (either backend)."""


def _scalar(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and len(value) == 1:
        return value[0]
    return value


def _text(value: Any) -> str:
    value = _scalar(value)
    return "" if value is None else str(value).strip()


def _ip_text(value: Any) -> str:
    value = _scalar(value)
    if isinstance(value, int) and 0 <= value <= 0xFFFFFFFF:
        return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))
    return _text(value)


def _parse_devices(result: Any) -> list[str]:
    """Parse a HALCON ``info_framegrabber(..., 'device')`` response."""
    if not isinstance(result, tuple) or len(result) < 2:
        return []
    entries = result[1]
    if not isinstance(entries, (list, tuple)):
        return []

    devices: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        marker = "device:"
        start = entry.find(marker)
        if start < 0:
            continue
        start += len(marker)
        end = entry.find(" |", start)
        device = entry[start:] if end < 0 else entry[start:end]
        device = device.strip()
        if device and device not in devices:
            devices.append(device)
    return devices


class CameraDiscoveryService:
    """Short-lived HALCON discovery service (explicit fallback backend)."""

    def __init__(
        self,
        *,
        halcon: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        halcon_interface: str = "GigEVision2",
        attempts: int = 3,
        retry_delay_s: float = 3.0,
    ) -> None:
        self._halcon = halcon
        self._sleep = sleep
        self._halcon_interface = halcon_interface
        self._attempts = max(1, attempts)
        self._retry_delay_s = retry_delay_s
        self._cameras: list[DiscoveredCamera] = []
        self.last_error: str | None = None

    @property
    def cameras(self) -> list[DiscoveredCamera]:
        return list(self._cameras)

    def discover_cameras(self) -> list[DiscoveredCamera]:
        """Discover all available cameras without starting acquisition."""
        self._cameras = []
        self.last_error = None
        try:
            ha = self._halcon or self._import_halcon()
            devices: list[str] = []
            for attempt in range(self._attempts):
                devices = _parse_devices(ha.info_framegrabber(self._halcon_interface, "device"))
                if devices or attempt == self._attempts - 1:
                    break
                self._sleep(self._retry_delay_s)

            seen: set[str] = set()
            for device in devices:
                try:
                    camera = self._read_camera(ha, device)
                except Exception as exc:
                    logger.warning("Unable to read discovered camera %s: %s", device, exc)
                    continue
                if camera.stable_identity in seen:
                    logger.warning("Ignoring duplicate discovered camera %s", camera.stable_identity)
                    continue
                seen.add(camera.stable_identity)
                self._cameras.append(camera)
        except Exception as exc:
            self.last_error = str(exc)
            raise CameraDiscoveryError(self.last_error) from exc
        return self.cameras

    def refresh(self) -> list[DiscoveredCamera]:
        return self.discover_cameras()

    def _read_camera(self, ha: Any, device: str) -> DiscoveredCamera:
        handle = None
        try:
            handle = ha.open_framegrabber(
                self._halcon_interface, 0, 0, 0, 0, 0, 0,
                "progressive", -1, "default", -1, "false", "default",
                device, 0, -1,
            )
            values = {
                "serial_number": "[Device]DeviceSerialNumber",
                "model": "[Device]DeviceModelName",
                "vendor": "[Device]DeviceVendorName",
                "ip_address": "[Device]GevDeviceIPAddress",
                "firmware": "[Device]DeviceVersion",
                "user_name": "[Device]DeviceUserID",
            }
            metadata = {
                field: (_ip_text(ha.get_framegrabber_param(handle, parameter))
                        if field == "ip_address"
                        else _text(ha.get_framegrabber_param(handle, parameter)))
                for field, parameter in values.items()
            }
            return DiscoveredCamera(device_identifier=device, **metadata)
        finally:
            if handle is not None:
                try:
                    ha.close_framegrabber(handle)
                except Exception:
                    logger.debug("Failed to close discovery handle for %s", device, exc_info=True)

    @staticmethod
    def _import_halcon() -> Any:
        import halcon as ha
        return ha


def _default_gvcp_client(camera_ip: str, local_ip: str) -> Any:
    from thermal_monitor.camera.tv46_gvcp import GVCPClient

    return GVCPClient(camera_ip, local_ip=local_ip)


def _tv46_info_to_camera(info: Any, local_ip: str, source: str) -> DiscoveredCamera:
    """Map a TV46DeviceInfo onto the shared DiscoveredCamera contract."""
    serial = info.serial_number or ""
    return DiscoveredCamera(
        device_identifier=f"gvcp:{serial or info.ip_address}",
        serial_number=serial,
        ip_address=info.ip_address,
        model=info.model_name,
        vendor=info.manufacturer_name,
        firmware=info.device_version,
        user_name=info.user_defined_name,
        local_ip=local_ip,
        source=source,
    )


class GvcpDiscoveryService:
    """Native GVCP discovery (Stage 8D), additive alongside HALCON discovery.

    Broadcast DISCOVERY per local interface finds responsive cameras;
    unicast verification of known/static IPs classifies the rest, so a
    camera that ignores DISCOVERY (known .16 behavior) is reported as
    ``gvcp-nonresponsive`` rather than silently dropped. Identity is the
    hardware serial wherever the camera provides one.
    """

    interface_label = "GVCP"

    def __init__(
        self,
        *,
        local_ips: "list[str] | None" = None,
        broadcast: str = "255.255.255.255",
        timeout: float = 1.5,
        attempts: int = 2,
        client_factory: Callable[[str, str], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._local_ips = list(local_ips) if local_ips is not None else None
        self._broadcast = broadcast
        self._timeout = timeout
        self._attempts = max(1, attempts)
        self._client_factory = client_factory or _default_gvcp_client
        self._sleep = sleep
        self._cameras: list[DiscoveredCamera] = []
        self.last_error: str | None = None

    @property
    def cameras(self) -> list[DiscoveredCamera]:
        return list(self._cameras)

    def _interfaces(self) -> list[str]:
        if self._local_ips is not None:
            return list(self._local_ips)
        from thermal_monitor.camera.tv46_gvcp import local_interface_ips

        return local_interface_ips() or ["0.0.0.0"]

    def discover_broadcast(self) -> list[DiscoveredCamera]:
        """Broadcast DISCOVERY on every interface; dedupe by serial."""
        from thermal_monitor.camera.tv46_gvcp import broadcast_discover

        found: list[DiscoveredCamera] = []
        seen: set[str] = set()
        duplicates: list[str] = []
        for local_ip in self._interfaces():
            for attempt in range(self._attempts):
                try:
                    answers = broadcast_discover(
                        local_ip=local_ip,
                        broadcast=self._broadcast,
                        timeout=self._timeout,
                    )
                except Exception as exc:
                    logger.debug("GVCP broadcast failed on %s: %s", local_ip, exc)
                    break
                for info, _local in answers:
                    camera = _tv46_info_to_camera(info, local_ip, "gvcp")
                    identity = camera.stable_identity
                    if identity in seen:
                        if identity not in duplicates:
                            duplicates.append(identity)
                        continue
                    seen.add(identity)
                    found.append(camera)
                if found:
                    break
        self._cameras = found
        self._duplicates = duplicates
        return self.cameras

    def discover_cameras(self) -> list[DiscoveredCamera]:
        """Broadcast discovery (same duck-type as CameraDiscoveryService).

        The selection dialog drives either backend through this method.
        Use :meth:`scan` with static IPs when classification of missing
        cameras is required.
        """
        return self.discover_broadcast()

    def refresh(self) -> list[DiscoveredCamera]:
        return self.discover_cameras()

    def verify_static(self, ips: "list[str]") -> list[GvcpCameraState]:
        """Classify known IPs: static-verified / gvcp-nonresponsive / unreachable."""
        from thermal_monitor.camera.tv46_gvcp import REG_PACKET_DELAY, routed_local_ip

        states: list[GvcpCameraState] = []
        for ip in ips:
            try:
                local_ip = routed_local_ip(ip)
            except Exception:
                local_ip = "0.0.0.0"
            client = self._client_factory(ip, local_ip)
            try:
                try:
                    client.connect()
                except Exception:
                    states.append(GvcpCameraState(ip, "unreachable", None, local_ip))
                    continue
                try:
                    info = client.discover_once()
                    camera = _tv46_info_to_camera(info, local_ip, "static")
                    states.append(GvcpCameraState(ip, "static-verified", camera, local_ip))
                    continue
                except Exception:
                    pass
                try:
                    client.read_register(REG_PACKET_DELAY)
                    states.append(GvcpCameraState(ip, "gvcp-nonresponsive", None, local_ip))
                except Exception:
                    states.append(GvcpCameraState(ip, "unreachable", None, local_ip))
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        return states

    def scan(self, static_ips: "list[str] | None" = None) -> GvcpScanReport:
        """Broadcast sweep merged with static verification (deduped by serial)."""
        found = self.discover_broadcast()
        by_identity = {c.stable_identity: c for c in found}
        duplicates = list(getattr(self, "_duplicates", []))
        states: list[GvcpCameraState] = [
            GvcpCameraState(c.ip_address, "discovered", c, c.local_ip) for c in found
        ]
        for state in self.verify_static(list(static_ips or [])):
            if state.camera is not None:
                identity = state.camera.stable_identity
                if identity in by_identity:
                    continue  # already discovered via broadcast
                by_identity[identity] = state.camera
                found.append(state.camera)
            states.append(state)
            if state.ip_address and state.state == "unreachable":
                logger.debug("GVCP static IP unreachable: %s", state.ip_address)
        return GvcpScanReport(found=found, states=states, duplicates=duplicates)


__all__ = [
    "CameraDiscoveryError",
    "CameraDiscoveryService",
    "DiscoveredCamera",
    "GvcpCameraState",
    "GvcpDiscoveryService",
    "GvcpScanReport",
    "build_discovery_service",
]


def build_discovery_service(
    discovery_config: Any,
) -> "CameraDiscoveryService | GvcpDiscoveryService":
    """Build the configured discovery backend (Stage 8E cutover).

    ``discovery_config.backend`` selects ``"gvcp"`` (default, native
    broadcast) or ``"halcon"`` (explicit fallback). Exactly one backend is
    constructed -- never silent switching. Callers that need the HALCON
    fallback after a GVCP failure rebuild with a halcon-configured config.
    """
    backend = getattr(discovery_config, "backend", "gvcp")
    if backend == "halcon":
        return CameraDiscoveryService(
            halcon_interface=getattr(discovery_config, "halcon_interface", "GigEVision2"),
            attempts=getattr(discovery_config, "attempts", 3),
            retry_delay_s=getattr(discovery_config, "retry_delay_s", 3.0),
        )
    if backend != "gvcp":
        raise CameraDiscoveryError(f"Unknown discovery backend: {backend!r}")
    return GvcpDiscoveryService(
        timeout=getattr(discovery_config, "retry_delay_s", 3.0),
        attempts=getattr(discovery_config, "attempts", 3),
    )
