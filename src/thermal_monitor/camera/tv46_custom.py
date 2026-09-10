"""
camera.tv46_custom -- custom Python TV46L acquisition driver (Stage 8G final).

``CustomTV46LDriver`` implements the
:class:`~thermal_monitor.camera.source.FrameSource` protocol using the
pure-Python GVCP/GVSP stack (:mod:`tv46_gvcp` / :mod:`tv46_gvsp`). It is the
sole production acquisition driver for the TV46L:

    TV46L --GVSP--> CustomTV46LDriver --GrabResult--> AcquisitionWorker
        --Frame--> SharedMemoryPublisher/Ring (dual-feed IR+VL)

:meth:`grab` publishes the RAW 640x480 Mono16 IR plane AND the RAW
640x480 YUYV VL plane ((480, 1280) uint8, no conversion). Both planes come
from one GVSP block under one frame ID.

Proven direct-path sequence preserved verbatim (see Stage 8A audit):

    GVCP connect -> CCP switchover (key=2) -> heartbeat 30000 ms (+thread)
    -> GVSP bind (ephemeral) -> SCDA0/SCP0 -> packet delay 10000 + readback
    -> keep SCPS0 (1500) -> fusion 0x10A110=3 (<=3 tries)
    -> AcquisitionStart 0x10A104=1 -> stream baseline readback

Stage 8G: HALCON acquisition is removed. This driver owns the production
acquisition stream, including the NUC and focus production pathways.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from typing import Callable, Optional

import numpy as np

from thermal_monitor.camera.source import (
    FIRST_FRAME_TIMEOUT_MS,
    CameraConnectionError,
    CameraGrabError,
)
from thermal_monitor.camera.model import (
    CameraConfig,
    CameraValidationResult,
    GrabResult,
    RegisterValidation,
)
from thermal_monitor.camera.tv46_gvcp import (
    CCP_KEY,
    FRAME_RATE_FPS,
    FUSION_COMBINED_VALUE,
    HEARTBEAT_TIMEOUT_MS,
    NUC_EXECUTE_FINE_OFFSETS,
    PACKET_DELAY_TICKS,
    REG_ACQUISITION_START,
    REG_CCP,
    REG_DEVICE_TEMP_CURRENT,
    REG_FOCUS_CURRENT,
    REG_FOCUS_MAX,
    REG_FOCUS_MIN,
    REG_FOCUS_SET,
    REG_FRAME_RATE,
    REG_FUSION_SELECTOR,
    REG_HEARTBEAT_TIMEOUT,
    REG_NUC_COMMAND,
    REG_PACKET_DELAY,
    REG_SCDA0,
    REG_SCP0,
    REG_SCPS0,
    ACQUISITION_START_VALUE,
    FOCUS_SETTLE_TIMEOUT_S,
    GVCPClient,
)
from thermal_monitor.camera.tv46_gvsp import (
    GVSPReceiver,
    IR_HEIGHT,
    IR_WIDTH,
    VL_PIXEL_FORMAT,
    parse_combined_payload,
)

logger = logging.getLogger(__name__)


def _route_local_ip(camera_ip: str) -> str:
    """Resolve the local interface that routes to the camera (no traffic)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((camera_ip, 3956))
        return probe.getsockname()[0]
    finally:
        probe.close()


def _ip_to_int(ip: str) -> int:
    parts = ip.split(".")
    if len(parts) != 4:
        raise ValueError(f"Not an IPv4 address: {ip!r}")
    return (int(parts[0]) << 24) | (int(parts[1]) << 16) | (int(parts[2]) << 8) | int(
        parts[3]
    )


class CustomTV46LDriver:
    """GVCP/GVSP driver for one TV46L camera (no HALCON).

    One instance owns one GVCP control socket (+ heartbeat thread) and one
    GVSP stream socket (+ receiver thread).  Constructing the driver never
    touches the network; :meth:`connect` performs the full bring-up.
    """

    def __init__(
        self,
        config: CameraConfig,
        camera_ip: str = "",
        local_ip: Optional[str] = None,
        stream_port: int = 0,
        rcvbuf: int = 16 * 1024 * 1024,
        packet_delay: int = PACKET_DELAY_TICKS,
        gvcp_factory: Optional[Callable[[], GVCPClient]] = None,
        receiver_factory: Optional[Callable[[], GVSPReceiver]] = None,
    ) -> None:
        self._config = config
        self._camera_ip = camera_ip or config.ip_address
        if not self._camera_ip:
            raise ValueError("CustomTV46LDriver requires camera_ip (or config.ip_address)")
        self._local_ip = local_ip  # resolved lazily at connect (test seam)
        self._stream_port = stream_port
        self._rcvbuf = rcvbuf
        self._packet_delay = packet_delay
        self._gvcp_factory = gvcp_factory
        self._receiver_factory = receiver_factory
        self._gvcp: Optional[GVCPClient] = None
        self._gvsp: Optional[GVSPReceiver] = None
        self._streaming = False
        self._heartbeat_running = False
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._last_block_id: Optional[int] = None
        self._block_epoch = 0
        # Destination IP programmed into SCDA0 at connect (host interface).
        self._programmed_dest_ip: Optional[str] = None

    # ------------------------------------------------------------------
    # FrameSource protocol (AcquisitionWorker contract)
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Full bring-up: GVCP control + GVSP stream + AcquisitionStart."""
        if self._streaming:
            return
        local_ip = self._local_ip or _route_local_ip(self._camera_ip)
        gvcp = (
            self._gvcp_factory()
            if self._gvcp_factory is not None
            else GVCPClient(self._camera_ip, local_ip=local_ip)
        )
        try:
            gvcp.connect()
        except Exception as exc:
            raise CameraConnectionError(f"GVCP connect failed: {exc}") from exc
        if not gvcp.control_switchover(key=CCP_KEY):
            gvcp.close()
            raise CameraConnectionError(f"{self._camera_ip}: failed to acquire GVCP control")
        self._gvcp = gvcp
        try:
            self._bring_up_stream(local_ip)
        except Exception:
            self._teardown()
            raise

    def _bring_up_stream(self, local_ip: str) -> None:
        assert self._gvcp is not None
        gvcp = self._gvcp
        gvcp.write_register(REG_HEARTBEAT_TIMEOUT, HEARTBEAT_TIMEOUT_MS)
        try:
            # Best-effort: some firmware revisions reject the rate register.
            gvcp.write_register(REG_FRAME_RATE, FRAME_RATE_FPS)
        except Exception:
            pass
        self._start_heartbeat()

        gvsp = (
            self._receiver_factory()
            if self._receiver_factory is not None
            else GVSPReceiver(local_ip=local_ip, port=self._stream_port, buffer_size=self._rcvbuf)
        )
        self._stream_port = gvsp.start()
        self._gvsp = gvsp

        if not self._set_stream_destination(local_ip, self._stream_port):
            raise CameraConnectionError(
                f"{self._camera_ip}: failed to set GVSP destination"
            )
        if self._packet_delay:
            if not gvcp.write_register(REG_PACKET_DELAY, self._packet_delay):
                raise CameraConnectionError(
                    f"{self._camera_ip}: failed to set packet delay {self._packet_delay}"
                )
            if gvcp.read_register(REG_PACKET_DELAY) != self._packet_delay:
                raise CameraConnectionError(
                    f"{self._camera_ip}: packet delay readback mismatch"
                )
        # Proven: keep the camera's SCPS0 (1500). Forcing jumbo degrades the stream.
        mode_selected = False
        for _ in range(3):
            if gvcp.write_register(REG_FUSION_SELECTOR, FUSION_COMBINED_VALUE):
                try:
                    if gvcp.read_register(REG_FUSION_SELECTOR) == FUSION_COMBINED_VALUE:
                        mode_selected = True
                        break
                except Exception:
                    mode_selected = True
                    break
            time.sleep(0.2)
        if not mode_selected:
            raise CameraConnectionError(
                f"{self._camera_ip}: failed to select Combined stream"
            )
        if not gvcp.write_register(REG_ACQUISITION_START, ACQUISITION_START_VALUE):
            raise CameraConnectionError(
                f"{self._camera_ip}: failed to start acquisition"
            )
        self._streaming = True
        logger.info(
            "CustomTV46LDriver %s streaming (local=%s:%d)",
            self._camera_ip,
            local_ip,
            self._stream_port,
        )

    def _set_stream_destination(self, host_ip: str, port: int) -> bool:
        assert self._gvcp is not None
        ip_int = _ip_to_int(host_ip)
        ok_ip = self._gvcp.write_register(REG_SCDA0, ip_int)
        ok_port = self._gvcp.write_register(REG_SCP0, port)
        if ok_ip:
            # Record the destination WE programmed: validation compares the
            # readback against this (see validate_registers).
            self._programmed_dest_ip: Optional[str] = host_ip
            try:
                readback = self._gvcp.read_register(REG_SCDA0)
                if readback != ip_int:
                    logger.warning(
                        "%s: SCDA readback mismatch (%#x != %#x)",
                        self._camera_ip,
                        readback,
                        ip_int,
                    )
            except Exception:
                pass
        return ok_ip

    def disconnect(self) -> None:
        """Idempotent teardown: stop stream, release CCP, close sockets."""
        self._teardown()

    def _teardown(self) -> None:
        self._streaming = False
        self._stop_heartbeat()
        if self._gvcp is not None:
            try:
                # Best-effort stream stop (WO command register; failures ignored).
                self._gvcp.write_register(REG_ACQUISITION_START, 0)
            except Exception:
                pass
            try:
                self._gvcp.write_register(REG_CCP, 0)
            except Exception:
                pass
        if self._gvsp is not None:
            try:
                self._gvsp.stop()
            except Exception:
                logger.exception("Error stopping GVSP receiver for %s", self._camera_ip)
            self._gvsp = None
        if self._gvcp is not None:
            try:
                self._gvcp.close()
            except Exception:
                logger.exception("Error closing GVCP for %s", self._camera_ip)
            self._gvcp = None

    def is_connected(self) -> bool:
        """True while streaming after a successful :meth:`connect`."""
        return self._streaming and self._gvcp is not None and self._gvsp is not None

    def reopen(self) -> None:
        """Close and reopen the stream (worker recovery action). A valid
        first frame is required before the handle is usable again."""
        self._teardown()
        self.connect()
        self.grab(FIRST_FRAME_TIMEOUT_MS)

    def validate_registers(
        self,
        expected_scda_ip: str = "",
        expected_fusion_value: int = FUSION_COMBINED_VALUE,
    ) -> CameraValidationResult:
        """Validate SCDA / SCP / fusion registers (worker CONTROL_READY gate).

        SCDA semantics (documented gate, unchanged worker compatible): SCDA
        is the HOST destination the camera streams to, while the worker
        passes ``CameraConfig.ip_address`` -- which in V3 deployments is the
        CAMERA IP.  The check therefore passes when the SCDA readback equals
        any of: the caller expectation, the destination this driver
        programmed at connect, or the routed local interface for the camera
        IP.  A camera pointed at a *different* host still fails.  Use an
        explicit host IP as ``expected_scda_ip`` for the strictest check.
        """
        checks: list[RegisterValidation] = []
        scda_raw: object = None
        scp_raw: object = None
        fusion_raw: object = None
        if self._gvcp is not None:
            try:
                scda_raw = self._gvcp.read_register(REG_SCDA0)
            except Exception:
                scda_raw = None
            try:
                scp_raw = self._gvcp.read_register(REG_SCP0)
            except Exception:
                scp_raw = None
            try:
                fusion_raw = self._gvcp.read_register(REG_FUSION_SELECTOR)
            except Exception:
                fusion_raw = None
        scda_ok = False
        if isinstance(scda_raw, int):
            acceptable = {_safe_ip_to_int(expected_scda_ip)} if expected_scda_ip else set()
            if self._programmed_dest_ip:
                acceptable.add(_safe_ip_to_int(self._programmed_dest_ip))
            try:
                acceptable.add(_safe_ip_to_int(_route_local_ip(self._camera_ip)))
            except Exception:
                pass
            acceptable.discard(-1)
            scda_ok = scda_raw in acceptable
        scp_ok = isinstance(scp_raw, int) and scp_raw != 0
        fusion_ok = fusion_raw == expected_fusion_value
        checks.append(
            RegisterValidation(
                name="SCDA", expected=expected_scda_ip, actual=scda_raw, passed=scda_ok
            )
        )
        checks.append(
            RegisterValidation(
                name="SCP", expected="non-zero", actual=scp_raw, passed=scp_ok
            )
        )
        checks.append(
            RegisterValidation(
                name="FUSION",
                expected=expected_fusion_value,
                actual=fusion_raw,
                passed=fusion_ok,
            )
        )
        return CameraValidationResult(
            scda_ok=scda_ok, scp_ok=scp_ok, fusion_ok=fusion_ok, checks=tuple(checks)
        )

    def grab(self, timeout_ms: int) -> GrabResult:
        """Acquire one RAW combined frame (IR + VL). Never synthesises --
        raises :class:`CameraGrabError` on timeout so the worker's reconnect
        policy applies.

        Both planes come from the SAME GVSP block, so IR and VL are
        intrinsically correlated (same ``frame_id``). No RGB or display
        conversion is performed here -- VL stays raw YUYV."""
        from thermal_monitor.camera.source import CameraGrabTimeout

        if not self._streaming or self._gvsp is None:
            raise CameraConnectionError("Stream is not running (call connect first)")
        started = time.perf_counter()
        completed = self._gvsp.get_block_with_id(timeout=max(0.0, timeout_ms / 1000.0))
        if completed is None:
            raise CameraGrabTimeout(f"GVSP grab timed out after {timeout_ms} ms")
        block_id, payload = completed
        if self._last_block_id is not None and block_id < self._last_block_id:
            self._block_epoch += 1
        self._last_block_id = block_id
        try:
            frame = parse_combined_payload(payload, block_id)
        except ValueError as exc:
            raise CameraGrabError(f"Combined payload parse failed: {exc}") from exc
        frame_id = (self._block_epoch << 16) | (block_id & 0xFFFF)
        converted = time.perf_counter()
        # Owned read-only copies: worker/ring must not mutate acquisition data.
        ir = frame.ir.copy()
        ir.setflags(write=False)
        vl = frame.vl.copy()
        vl.setflags(write=False)
        stats = self._gvsp.get_stats() if self._gvsp is not None else {}
        packet_stats = {
            "packets_seen": int(stats.get("packets_received", 0)),
            "packets_lost": int(stats.get("packets_dropped", 0))
            + int(stats.get("duplicate_packets", 0)),
            "blocks_incomplete": int(stats.get("blocks_incomplete", 0)),
            "blocks_discarded": int(stats.get("block_timeouts", 0)),
        }
        return GrabResult(
            thermal=ir,
            thermal_format=self._config.stream_source_thermal,
            visible=vl,
            visible_format=VL_PIXEL_FORMAT,
            hardware_timestamp=None,  # TV46L exposes no HW timestamp (same as HALCON path)
            grab_started=started,
            grab_completed=converted,
            converted_at=time.perf_counter(),
            frame_id=frame_id,
            packet_stats=packet_stats,
        )

    # ------------------------------------------------------------------
    # NUC (custom one-step production command -- Stage 8G final)
    # ------------------------------------------------------------------

    def perform_nuc(self) -> None:
        """Execute the manual fine-offset/NUC command (0x20A134 = 11).

        Production behavior (Stage 8G, custom-path only):

        * Command only -- a single GVCP register write while the custom
          GVSP stream keeps running. No backend switch, no reconnect, no
          synthetic or duplicated frames, no arbitrary sleeps.
        * The SHM ring naturally freezes on the last pre-NUC frame during
          the short hardware silence (latest-frame consumers keep showing
          it); malformed transitional blocks are rejected by
          :func:`parse_combined_payload` (raised as ``CameraGrabError`` and
          never published), so the first accepted post-NUC frame is a fully
          valid IR+VL pair with synchronized frame IDs.
        * Stream configuration (packet delay / fusion) is verified by the
          caller (:meth:`verify_stream_config` / runtime ``perform_nuc``);
          this method only issues the command and reports rejection.
        """
        if self._gvcp is None or not self._streaming:
            raise CameraConnectionError("Cannot trigger NUC before streaming starts")
        if not self._gvcp.write_register(REG_NUC_COMMAND, NUC_EXECUTE_FINE_OFFSETS):
            raise CameraGrabError("Camera rejected manual NUC command")

    # ------------------------------------------------------------------
    # Focus (no V3 incumbent -- new API, §8B.8)
    # ------------------------------------------------------------------

    def get_focus_limits(self) -> tuple[int, int]:
        """Hardware-reported focus range in mm (min, max)."""
        if self._gvcp is None:
            raise CameraConnectionError("Cannot read focus without GVCP control")
        return (self._gvcp.read_register(REG_FOCUS_MIN), self._gvcp.read_register(REG_FOCUS_MAX))

    def get_focus_mm(self) -> int:
        """Current focus distance readback in mm."""
        if self._gvcp is None:
            raise CameraConnectionError("Cannot read focus without GVCP control")
        return self._gvcp.read_register(REG_FOCUS_CURRENT)

    def set_focus_mm(
        self, value_mm: int, settle_timeout: float = FOCUS_SETTLE_TIMEOUT_S
    ) -> int:
        """Write the focus distance (mm) and verify via readback.

        Never clamps: non-integer / non-positive / out-of-range values raise.
        A settle mismatch after the timeout is logged (motor positioning,
        e.g. 4800 -> 4765) but not raised -- the hardware accepted the value.
        Returns the readback in mm. Safe to call while streaming.
        """
        if self._gvcp is None:
            raise CameraConnectionError("Cannot set focus without GVCP control")
        if isinstance(value_mm, bool) or not isinstance(value_mm, int):
            raise ValueError(f"Focus value must be an integer number of mm, got {value_mm!r}")
        if value_mm <= 0:
            raise ValueError(f"Focus value must be > 0 mm, got {value_mm}")
        focus_min, focus_max = self.get_focus_limits()
        if not focus_min <= value_mm <= focus_max:
            raise ValueError(
                f"Focus value {value_mm} mm outside camera range "
                f"[{focus_min}, {focus_max}] mm (never clamped)"
            )
        if not self._gvcp.write_register(REG_FOCUS_SET, value_mm):
            raise CameraGrabError(f"Camera rejected focus value {value_mm} mm")
        deadline = time.monotonic() + max(0.0, settle_timeout)
        readback = self.get_focus_mm()
        while readback != value_mm and time.monotonic() < deadline:
            time.sleep(0.2)
            readback = self.get_focus_mm()
        if readback != value_mm:
            logger.warning(
                "%s: focus settle mismatch requested=%d readback=%d (continuing)",
                self._camera_ip,
                value_mm,
                readback,
            )
        return readback

    # ------------------------------------------------------------------
    # Telemetry / diagnostics
    # ------------------------------------------------------------------

    def get_device_temperature_c(self) -> Optional[float]:
        """Internal device temperature in °C. Never raises, never disturbs
        the stream -- any failure returns None."""
        try:
            if self._gvcp is None:
                return None
            raw = self._gvcp.read_memory(REG_DEVICE_TEMP_CURRENT, 4)
            if len(raw) != 4:
                return None
            value = struct.unpack(">f", bytes(raw))[0]
        except Exception:
            return None
        if value != value or value in (float("inf"), float("-inf")):
            return None
        if not -50.0 <= value <= 150.0:
            return None
        return float(value)

    def read_stream_config(self) -> dict:
        """Read back stream registers (diagnostic, never writes)."""
        if self._gvcp is None:
            raise CameraConnectionError("Cannot read stream config without GVCP control")
        return {
            "packet_delay": self._gvcp.read_register(REG_PACKET_DELAY),
            "scp0": self._gvcp.read_register(REG_SCP0),
            "scps0": self._gvcp.read_register(REG_SCPS0),
            "scda0": self._gvcp.read_register(REG_SCDA0),
            "fusion_selector": self._gvcp.read_register(REG_FUSION_SELECTOR),
        }

    def verify_stream_config(self, expected_packet_delay: int) -> dict:
        """Verify NUC did not disturb stream config. Restores ONLY the
        proven packet-delay value on mismatch; everything else is reported.

        Returns the post-verification readback (after any restore), so
        callers see the effective stream configuration.
        """
        actual = self.read_stream_config()
        if actual["packet_delay"] != expected_packet_delay and self._gvcp is not None:
            logger.warning(
                "%s: packet delay changed (%s != %s); restoring",
                self._camera_ip,
                actual["packet_delay"],
                expected_packet_delay,
            )
            if not self._gvcp.write_register(REG_PACKET_DELAY, expected_packet_delay):
                raise CameraGrabError(
                    f"{self._camera_ip}: failed to restore packet delay"
                )
            actual["packet_delay"] = self._gvcp.read_register(REG_PACKET_DELAY)
            if actual["packet_delay"] != expected_packet_delay:
                raise CameraGrabError(
                    f"{self._camera_ip}: packet delay readback "
                    f"{actual['packet_delay']} != {expected_packet_delay} after restore"
                )
        if actual["fusion_selector"] != FUSION_COMBINED_VALUE:
            logger.warning(
                "%s: fusion selector %s != %s (Combined mode changed?)",
                self._camera_ip,
                actual["fusion_selector"],
                FUSION_COMBINED_VALUE,
            )
        return actual

    def get_status(self) -> dict:
        """Receiver counters + stream state for FPS/loss measurement."""
        stats = self._gvsp.get_stats() if self._gvsp is not None else {}
        stats.update(
            {
                "camera_ip": self._camera_ip,
                "streaming": self._streaming,
                "stream_port": self._stream_port,
                "last_block_id": self._last_block_id,
                "block_epoch": self._block_epoch,
            }
        )
        return stats

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        self._heartbeat_running = True

        def loop() -> None:
            while self._heartbeat_running:
                time.sleep(0.5)
                try:
                    if self._gvcp is not None:
                        self._gvcp.heartbeat_ping()
                except Exception:
                    pass

        self._heartbeat_thread = threading.Thread(
            target=loop, name=f"gvcp-heartbeat-{self._camera_ip}", daemon=True
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_running = False
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=1.5)
            self._heartbeat_thread = None


def _safe_ip_to_int(ip: str) -> int:
    try:
        return _ip_to_int(ip)
    except (ValueError, AttributeError):
        return -1


__all__ = ["CustomTV46LDriver"]
