"""
camera.driver -- hardware interaction with the Fluke TV46L via HALCON.

This module owns the actual HALCON calls (open/configure/grab/close).  It
must not orchestrate threading, sequence numbers or reconnection policy;
that belongs to :mod:`thermal_monitor.camera.acquisition`.

Design notes inherited from the V2 recovery report (read-only evidence):

* ``open_framegrabber`` parameter layout is the V2-validated call:
  ``("GigEVision2", 0,0,0,0,0,0, "progressive", -1, "default", -1,
  "false", "default", <device>, 0, -1)``.
* Grab-timeout detection uses HALCON error code 5322.
* The proven validation path sets ``num_buffers=8`` and a 1 MB receive
  socket; V3 defaults to those values.
* ``himage_as_numpy_array`` may return an array backed by HALCON's
  internal buffer, so V3 copies the frame into an owned, read-only array
  before publishing (the raw data must be immutable for consumers).

HALCON is imported lazily inside the methods so that importing this module
never fails on machines without the HALCON runtime (unit tests exercise the
acquisition orchestration through :class:`FrameSource`).
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

import numpy as np

from thermal_monitor.camera.model import CameraConfig, GrabResult

logger = logging.getLogger(__name__)

GRAB_TIMEOUT_ERROR_CODE = 5322

FIRST_FRAME_TIMEOUT_MS = 5000


class CameraConnectionError(RuntimeError):
    """Failed to open/configure the camera framegrabber."""


class CameraGrabError(RuntimeError):
    """A grab failed for a reason other than a timeout."""


class CameraGrabTimeout(CameraGrabError):
    """A grab did not return within the configured timeout."""


class FrameSource(Protocol):
    """Interface between acquisition orchestration and a frame producer.

    The worker depends only on this protocol so the acquisition loop can be
    tested with a fake source and later reused for offline playback
    (offline is allowed to reuse the live processing path).
    """

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def grab(self, timeout_ms: int) -> GrabResult: ...

    def is_connected(self) -> bool: ...

    def reopen(self) -> None: ...


class TV46LDriver:
    """Low-level HALCON GigE Vision driver for one TV46L camera.

    One driver owns one framegrabber handle.  The TV46L is a single-stream
    GigE camera: ``FLK_TI_StreamDataSourceSelector`` selects the source for
    the one channel, so a single handle acquires one stream at a time.

    VISIBLE STREAM NOTE: The TV46L requires switching the stream source
    selector (``FLK_TI_StreamDataSourceSelector``) between ``"IR_Data"``
    (thermal) and ``"VL_Data"`` (visible) to acquire each stream.  They cannot
    be acquired simultaneously through a single framegrabber handle.  V3
    currently implements only thermal acquisition (the default).  Visible
    acquisition would require either:
      - A second framegrabber handle (second GigE connection to the same IP)
      - Time-sliced acquisition by switching the selector between grabs
    Neither is implemented yet.  The architecture is extensible for future
    visible support via ``CameraConfig.stream_source_visible`` and
    ``GrabResult.visible``.
    """

    HALCON_INTERFACE = "GigEVision2"

    def __init__(self, config: CameraConfig) -> None:
        self._config = config
        self._framegrabber = None
        self._connected = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        if self._connected:
            return

        ha = self._import_halcon()
        logger.info("Opening camera %s (%s)", self._config.identity.camera_id, self._config.device_identifier)

        try:
            self._framegrabber = ha.open_framegrabber(
                self.HALCON_INTERFACE,
                0, 0, 0, 0, 0, 0,
                "progressive",
                -1,
                "default",
                -1,
                "false",
                "default",
                self._config.device_identifier,
                0,
                -1,
            )
            self._connected = True
            self._configure_camera()
            ha.grab_image_start(self._framegrabber, -1)
            logger.info("Camera %s framegrabber opened and streaming", self._config.identity.camera_id)
        except Exception as exc:
            self._connected = False
            self._framegrabber = None
            logger.exception("Unable to open camera %s", self._config.identity.camera_id)
            raise CameraConnectionError(str(exc)) from exc

    def disconnect(self) -> None:
        if not self._connected:
            return
        ha = self._import_halcon()
        try:
            ha.close_framegrabber(self._framegrabber)
        except Exception:
            logger.exception("Error closing framegrabber for camera %s", self._config.identity.camera_id)
        finally:
            self._framegrabber = None
            self._connected = False
            logger.info("Camera %s disconnected", self._config.identity.camera_id)

    def reopen(self) -> None:
        """Close and reopen only the framegrabber (recovery action).

        Mirror of the V2 validation path: state such as configuration is
        re-applied, nothing downstream is touched.  A valid first frame is
        required before the handle is considered usable again.
        """
        self.disconnect()
        self.connect()
        self.grab(FIRST_FRAME_TIMEOUT_MS)

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure_camera(self) -> None:
        ha = self._import_halcon()
        cfg = self._config

        source = cfg.stream_source_thermal
        bits = cfg.thermal_bits_per_channel

        ha.set_framegrabber_param(self._framegrabber, "FLK_TI_StreamDataSourceSelector", source)
        ha.set_framegrabber_param(self._framegrabber, "bits_per_channel", bits)

        for name, value in (
            ("[Stream]DeviceStreamChannelNegotiatePacketSize", 1),
            ("[Stream]GevStreamReceiveSocketSize", cfg.socket_buffer_size),
            ("num_buffers", cfg.num_buffers),
            ("FLK_TI_ControlFeature_SetFrameRate", cfg.frame_rate),
            (
                "FLK_TI_ControlFeature_REControlCmd",
                "FLK_TI_ControlFeature_REControlCmd_DisableAutomaticFineOffsets",
            ),
        ):
            try:
                ha.set_framegrabber_param(self._framegrabber, name, value)
            except Exception:
                logger.warning(
                    "Camera %s: unable to set %s=%r",
                    cfg.identity.camera_id,
                    name,
                    value,
                )

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------

    def set_parameter(self, name: str, value: object) -> None:
        ha = self._import_halcon()
        ha.set_framegrabber_param(self._framegrabber, name, value)

    def get_parameter(self, name: str) -> object:
        ha = self._import_halcon()
        value = ha.get_framegrabber_param(self._framegrabber, name)
        if isinstance(value, (list, tuple)):
            return value[0]
        return value

    # ------------------------------------------------------------------
    # Grabbing
    # ------------------------------------------------------------------

    def grab(self, timeout_ms: int) -> GrabResult:
        ha = self._import_halcon()
        if not self._connected:
            raise CameraConnectionError("Framegrabber is not open")

        started = time.perf_counter()
        try:
            image = ha.grab_image_async(self._framegrabber, timeout_ms)
        except Exception as exc:
            completed = time.perf_counter()
            if self._is_grab_timeout(exc):
                raise CameraGrabTimeout(
                    f"grab timed out after {timeout_ms} ms"
                ) from exc
            logger.exception("Grab failed for camera %s", self._config.identity.camera_id)
            raise CameraGrabError(str(exc)) from exc
        completed = time.perf_counter()

        try:
            array = ha.himage_as_numpy_array(image)
        except Exception as exc:
            raise CameraGrabError(f"himage_as_numpy_array failed: {exc}") from exc

        # Copy into an owned, read-only buffer so consumers cannot mutate
        # acquisition data and the next grab cannot overwrite it.
        # COPY CHAIN DOCUMENTATION:
        # 1. HALCON internal buffer (himage_as_numpy_array may return a view)
        # 2. array.copy() -> owned NumPy array (Copy #1)
        # 3. array.setflags(write=False) -> read-only view of owned array
        # 4. FramePayload constructor validates writeable=False
        # 5. Publisher receives Frame (no additional copy in InProcessLatestPublisher)
        #    Future SharedMemoryRingBufferPublisher will copy into ring buffer slot (Copy #2)
        if array is not None:
            array = array.copy()
            array.setflags(write=False)

        # Read hardware metadata from HALCON
        hardware_timestamp = None
        frame_id = None
        packet_stats = None
        try:
            ts_ns = ha.get_framegrabber_param(self._framegrabber, "buffer_timestamp_ns")
            if isinstance(ts_ns, (list, tuple)) and len(ts_ns) == 1:
                ts_ns = ts_ns[0]
            if ts_ns is not None:
                hardware_timestamp = float(ts_ns) / 1e9
        except Exception:
            pass

        try:
            fid = ha.get_framegrabber_param(self._framegrabber, "buffer_frameid")
            if isinstance(fid, (list, tuple)) and len(fid) == 1:
                fid = fid[0]
            if fid is not None:
                frame_id = int(fid)
        except Exception:
            pass

        try:
            seen = ha.get_framegrabber_param(self._framegrabber, "[Stream]GevStreamSeenPacketCount")
            lost = ha.get_framegrabber_param(self._framegrabber, "[Stream]GevStreamLostPacketCount")
            incomplete = ha.get_framegrabber_param(self._framegrabber, "[Stream]GevStreamIncompleteBlockCount")
            discarded = ha.get_framegrabber_param(self._framegrabber, "[Stream]GevStreamDiscardedBlockCount")

            def unwrap(val):
                if isinstance(val, (list, tuple)) and len(val) == 1:
                    return val[0]
                return val

            packet_stats = {
                "packets_seen": int(unwrap(seen)) if unwrap(seen) is not None else 0,
                "packets_lost": int(unwrap(lost)) if unwrap(lost) is not None else 0,
                "blocks_incomplete": int(unwrap(incomplete)) if unwrap(incomplete) is not None else 0,
                "blocks_discarded": int(unwrap(discarded)) if unwrap(discarded) is not None else 0,
            }
        except Exception:
            packet_stats = {
                "packets_seen": 0,
                "packets_lost": 0,
                "blocks_incomplete": 0,
                "blocks_discarded": 0,
            }

        converted = time.perf_counter()

        return GrabResult(
            thermal=array,
            thermal_format=self._config.stream_source_thermal,
            hardware_timestamp=hardware_timestamp,
            grab_started=started,
            grab_completed=completed,
            converted_at=converted,
            frame_id=frame_id,
            packet_stats=packet_stats,
        )

    # ------------------------------------------------------------------
    # NUC
    # ------------------------------------------------------------------

    def perform_nuc(self) -> None:
        """Execute a manual Non-Uniformity Correction.

        V2-validated sequence: RequestFineOffset -> ExecuteFineOffset ->
        short pause -> flush stale frames.
        """
        ha = self._import_halcon()
        self.set_parameter(
            "FLK_TI_ControlFeature_REControlCmd",
            "FLK_TI_ControlFeature_REControlCmd_RequestFineOffset",
        )
        self.set_parameter(
            "FLK_TI_ControlFeature_REControlCmd",
            "FLK_TI_ControlFeature_REControlCmd_ExecuteFineOffset",
        )
        time.sleep(0.05)
        for _ in range(3):
            try:
                ha.grab_image_async(self._framegrabber, 0)
            except Exception:
                logger.warning("Camera %s: NUC flush grab failed", self._config.identity.camera_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _import_halcon():
        import halcon as ha

        return ha

    @staticmethod
    def _is_grab_timeout(exc: Exception) -> bool:
        code = getattr(exc, "error_code", None)
        if code is not None:
            try:
                return int(code) == GRAB_TIMEOUT_ERROR_CODE
            except (TypeError, ValueError):
                pass
        message = str(exc) or ""
        lowered = message.lower()
        return "5322" in message and "timeout" in lowered or (
            "grab" in lowered and "timeout" in lowered
        )