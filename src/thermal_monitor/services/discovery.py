"""HALCON GigE Vision camera discovery.

Discovery is deliberately separate from acquisition.  It opens a temporary
framegrabber only long enough to read device metadata and always closes it.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiscoveredCamera:
    """Metadata returned by HALCON for one physical camera."""

    device_identifier: str
    serial_number: str = ""
    ip_address: str = ""
    model: str = ""
    vendor: str = ""
    firmware: str = ""
    user_name: str = ""

    @property
    def stable_identity(self) -> str:
        """Prefer the hardware serial, otherwise HALCON's device identifier."""
        return self.serial_number or self.device_identifier

    @property
    def camera_id(self) -> str:
        return f"cam_{self.stable_identity}"


class CameraDiscoveryError(RuntimeError):
    """HALCON was unavailable or discovery failed."""


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
    """Parse V2's ``info_framegrabber(..., 'device')`` response."""
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
    """Short-lived HALCON discovery service with injectable HALCON module."""

    HALCON_INTERFACE = "GigEVision2"

    def __init__(
        self,
        *,
        halcon: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = 3,
        retry_delay_s: float = 3.0,
    ) -> None:
        self._halcon = halcon
        self._sleep = sleep
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
                devices = _parse_devices(ha.info_framegrabber(self.HALCON_INTERFACE, "device"))
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
                self.HALCON_INTERFACE, 0, 0, 0, 0, 0, 0,
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


__all__ = ["CameraDiscoveryError", "CameraDiscoveryService", "DiscoveredCamera"]
