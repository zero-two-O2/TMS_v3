#!/usr/bin/env python3
"""tv46l_capture_to_recording.py -- Experimental TV46L capture to V3 recording.

EXPERIMENTAL tool (NOT production acquisition).  Its only purpose:

    TV46L -> capture raw IR/VL frames -> write V3 recording directory

This tool uses HALCON for frame acquisition and a minimal GVCP client to
enable the camera's dual-source mode (IR + visible alternating on one GVSP
stream).  It identifies each frame's stream type from the GVSP leader's
pixel-format field (exposed via HALCON's image_pixel_format parameter) and
writes physical records to the Stage 5C recording format.

Requirements:
- HALCON (mvtec_halcon) with GigEVision2 interface
- Camera in dual mode via GVCP register 0x10a110 = 2
- No SQL Server dependency

Usage:
    python scripts/tv46l_capture_to_recording.py \
        --duration 30 \
        --output recordings/test_001 \
        --camera-id HB25100004 \
        --device default \
        --ip 169.254.24.69

GVCP register:
    0x10a110 = 1  -> IR only
    0x10a110 = 2  -> IR + visible (dual mode, alternating blocks)

Pixel formats (PFNC from GVSP leader):
    0x01100007  -> Mono16 (IR)
    0x02100032  -> YUV422_8 (visible)
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from thermal_monitor.core.frame import Frame, FrameDescriptor, FramePayload, StreamMetadata, SyncInfo, SyncStatus
from thermal_monitor.storage.recording import RecordingWriteMetadata, RecordingWriter
from thermal_monitor.storage.recording.format import (
    PixelFormat,
    StreamType,
    dtype_to_code,
    pixel_format_to_code,
    sync_status_to_code,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GVCP_PORT = 3956
REGISTER_DATA_SOURCE = 0x0010A110
VALUE_IR_ONLY = 1
VALUE_DUAL = 2

REG_CCP = 0x00000A00
REG_STREAM_ENABLE = 0x00010A104

CMD_DISCOVERY_CMD = 0x0002
CMD_READREG_CMD = 0x0080
CMD_WRITEREG_CMD = 0x0082

STATUS_SUCCESS = 0x0000

# PFNC pixel formats (from GVSP leader)
PFNC_MONO16 = 0x01100007
PFNC_YUV422_8 = 0x02100032

DEFAULT_DURATION_S = 30.0
GRAB_TIMEOUT_MS = 1000
FIRST_FRAME_TIMEOUT_MS = 5000

# ---------------------------------------------------------------------------
# GVCP Client (minimal, from tv46l_dual_mode_halcon_test.py)
# ---------------------------------------------------------------------------


class GVCPError(RuntimeError):
    pass


class GVCPTimeout(GVCPError):
    pass


class GVCPClient:
    """Minimal GigE Vision control-channel client for TV46L dual-mode register."""

    def __init__(
        self, camera_ip: str, timeout: float = 1.5, retries: int = 3
    ) -> None:
        self.camera_ip = camera_ip
        self.timeout = timeout
        self.retries = retries
        self.reqid = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(timeout)
        self._sock.bind(("", 0))

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _next_reqid(self) -> int:
        self.reqid = (self.reqid + 1) & 0xFFFF
        return self.reqid

    @staticmethod
    def _build_cmd(command: int, payload: bytes, reqid: int, flags: int = 0x01) -> bytes:
        return struct.pack(">BBHHH", 0x42, flags, command, len(payload), reqid) + payload

    @staticmethod
    def _parse_ack(data: bytes):
        if len(data) < 8:
            raise GVCPError("ack too short")
        status, ackcmd, length, reqid = struct.unpack(">HHHH", data[:8])
        payload = data[8 : 8 + length]
        return status, ackcmd, reqid, payload

    def _request(self, command: int, payload: bytes, expected_ack: int) -> bytes:
        reqid = self._next_reqid()
        cmd = self._build_cmd(command, payload, reqid)
        last_exc = None
        for _ in range(self.retries):
            try:
                self._sock.sendto(cmd, (self.camera_ip, GVCP_PORT))
                data, _ = self._sock.recvfrom(1500)
                status, ackcmd, ack_reqid, ack_payload = self._parse_ack(data)
                if ack_reqid != reqid:
                    continue
                if status != STATUS_SUCCESS:
                    raise GVCPError(f"GVCP status {status:#06x} for command {command:#06x}")
                if ackcmd != expected_ack:
                    raise GVCPError(f"Unexpected ack command {ackcmd:#06x}, expected {expected_ack:#06x}")
                return ack_payload
            except socket.timeout as exc:
                last_exc = exc
                continue
        raise GVCPTimeout(f"GVCP request timed out after {self.retries} retries: {last_exc}")

    def discovery(self) -> dict:
        """Send discovery command, return camera info."""
        payload = self._request(CMD_DISCOVERY_CMD, b"", CMD_DISCOVERY_CMD + 1)
        # Parse discovery ack (simplified)
        return {"raw": payload.hex()}

    def read_register(self, address: int) -> int:
        payload = struct.pack(">II", address, 4)
        ack = self._request(CMD_READREG_CMD, payload, CMD_READREG_CMD + 1)
        if len(ack) < 4:
            raise GVCPError("readreg ack too short")
        return struct.unpack(">I", ack[:4])[0]

    def write_register(self, address: int, value: int) -> None:
        payload = struct.pack(">II", address, value)
        self._request(CMD_WRITEREG_CMD, payload, CMD_WRITEREG_CMD + 1)

    def acquire_ccp(self) -> None:
        """Acquire control channel privilege (required for WRITEREG)."""
        self.write_register(REG_CCP, 1)

    def release_ccp(self) -> None:
        """Release control channel privilege."""
        try:
            self.write_register(REG_CCP, 0)
        except GVCPError:
            pass

    def enable_dual_mode(self) -> None:
        """Enable IR + visible dual mode (register 0x10a110 = 2)."""
        self.write_register(REGISTER_DATA_SOURCE, VALUE_DUAL)

    def disable_dual_mode(self) -> None:
        """Disable dual mode, back to IR only (register 0x10a110 = 1)."""
        self.write_register(REGISTER_DATA_SOURCE, VALUE_IR_ONLY)

    def stream_enable(self, enable: bool = True) -> None:
        """Enable/disable stream."""
        self.write_register(REG_STREAM_ENABLE, 1 if enable else 0)


# ---------------------------------------------------------------------------
# HALCON Frame Grabber
# ---------------------------------------------------------------------------


class HalconGrabber:
    """Wrapper around HALCON framegrabber for TV46L dual-mode capture."""

    def __init__(self, device: str) -> None:
        self.device = device
        self.ha = None
        self.fg = None

    def _import_halcon(self):
        if self.ha is None:
            import halcon as ha
            self.ha = ha
        return self.ha

    def open(self) -> None:
        ha = self._import_halcon()
        self.fg = ha.open_framegrabber(
            "GigEVision2",
            0, 0, 0, 0, 0, 0,
            "progressive",
            -1,
            "default",
            -1,
            "false",
            "default",
            self.device,
            0,
            -1,
        )
        # Proven single-source recipe: force IR_Data at 16 bits.  HALCON's
        # provider picks the source on open (observed defaulting to VL_Data);
        # explicitly selecting IR_Data is required to keep the stream on IR.
        try:
            ha.set_framegrabber_param(self.fg, "FLK_TI_StreamDataSourceSelector", "IR_Data")
            ha.set_framegrabber_param(self.fg, "bits_per_channel", 16)
        except Exception:
            try:
                ha.close_framegrabber(self.fg)
            except Exception:
                pass
            self.fg = None
            raise
        # Proven transport params: the hardware probe measured 0 lost packets
        # with a 1 MiB receive socket and 8 buffers (the capture default lost
        # 2122 packets / 25 incomplete blocks in a 5 s run).
        for name, value in (
            ("[Stream]DeviceStreamChannelNegotiatePacketSize", 1),
            ("[Stream]GevStreamReceiveSocketSize", 1048576),
            ("num_buffers", 8),
        ):
            try:
                ha.set_framegrabber_param(self.fg, name, value)
            except Exception:
                pass
        # Start acquisition
        ha.grab_image_start(self.fg, -1)

    def close(self) -> None:
        if self.fg is not None:
            ha = self._import_halcon()
            try:
                ha.set_framegrabber_param(self.fg, "do_abort_grab", 1)
            except Exception:
                pass
            try:
                ha.close_framegrabber(self.fg)
            except Exception:
                pass
            self.fg = None

    def grab(self, timeout_ms: int) -> Optional[np.ndarray]:
        """Grab one frame, return numpy array or None on timeout."""
        ha = self._import_halcon()
        try:
            image = ha.grab_image_async(self.fg, timeout_ms)
        except Exception as exc:
            msg = str(exc) or ""
            if "5322" in msg or "timeout" in msg.lower():
                return None
            raise
        return ha.himage_as_numpy_array(image)

    def get_pixel_format(self) -> Optional[int]:
        """Get PFNC pixel format from GVSP leader (HALCON image_pixel_format)."""
        ha = self._import_halcon()
        try:
            val = ha.get_framegrabber_param(self.fg, "image_pixel_format")
            if isinstance(val, (list, tuple)) and len(val) == 1:
                val = val[0]
            return int(val) if val is not None else None
        except Exception:
            return None

    def get_frame_id(self) -> Optional[int]:
        """Get hardware frame ID (buffer_frameid)."""
        ha = self._import_halcon()
        try:
            val = ha.get_framegrabber_param(self.fg, "buffer_frameid")
            if isinstance(val, (list, tuple)) and len(val) == 1:
                val = val[0]
            return int(val) if val is not None else None
        except Exception:
            return None

    def get_hardware_timestamp_ns(self) -> Optional[int]:
        """Get hardware timestamp if available (buffer_timestamp_ns)."""
        ha = self._import_halcon()
        try:
            val = ha.get_framegrabber_param(self.fg, "buffer_timestamp_ns")
            if isinstance(val, (list, tuple)) and len(val) == 1:
                val = val[0]
            return int(val) if val is not None else None
        except Exception:
            return None

    def get_payload_size(self) -> Optional[int]:
        ha = self._import_halcon()
        try:
            val = ha.get_framegrabber_param(self.fg, "PayloadSize")
            if isinstance(val, (list, tuple)) and len(val) == 1:
                val = val[0]
            return int(val) if val is not None else None
        except Exception:
            return None

    def get_image_size(self) -> tuple[int, int]:
        ha = self._import_halcon()
        try:
            w = ha.get_framegrabber_param(self.fg, "image_width")
            h = ha.get_framegrabber_param(self.fg, "image_height")
            if isinstance(w, (list, tuple)):
                w = w[0]
            if isinstance(h, (list, tuple)):
                h = h[0]
            return int(w), int(h)
        except Exception:
            return 640, 480


# ---------------------------------------------------------------------------
# Pixel Format Classification
# ---------------------------------------------------------------------------


def classify_stream(pfnc_format: int) -> tuple[str, StreamType]:
    """Classify stream type from PFNC pixel format."""
    if pfnc_format == PFNC_MONO16:
        return "IR", StreamType.IR
    elif pfnc_format == PFNC_YUV422_8:
        return "VL", StreamType.VL
    else:
        return "UNKNOWN", StreamType.IR  # default fallback


def pixel_format_name(pfnc_format: int) -> str:
    if pfnc_format == PFNC_MONO16:
        return "Mono16"
    elif pfnc_format == PFNC_YUV422_8:
        return "YUV422_8"
    else:
        return f"0x{pfnc_format:08x}"


# ---------------------------------------------------------------------------
# Capture Session
# ---------------------------------------------------------------------------


class CaptureSession:
    """Manages a capture session writing to V3 recording."""

    def __init__(
        self,
        output_dir: Path,
        camera_id: str,
        device: str,
        camera_ip: str,
        duration_s: float,
    ) -> None:
        self.output_dir = output_dir
        self.camera_id = camera_id
        self.device = device
        self.camera_ip = camera_ip
        self.duration_s = duration_s

        self.gvcp: Optional[GVCPClient] = None
        self.grabber: Optional[HalconGrabber] = None
        self.writer: Optional[RecordingWriter] = None
        self.recording_dir: Optional[Path] = None

        # Statistics
        self.frames_captured = 0
        self.ir_frames = 0
        self.vl_frames = 0
        self.errors = 0
        self.timeouts = 0
        self.packet_stats = {"seen": 0, "lost": 0, "resent": 0, "incomplete": 0}
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None

    def setup(self) -> bool:
        """Initialize GVCP, enable dual mode, open HALCON grabber."""
        print(f"Connecting to camera at {self.camera_ip}...")
        self.gvcp = GVCPClient(self.camera_ip)

        try:
            # Discover
            self.gvcp.discovery()
            print("  Discovery OK")

            # Acquire CCP
            self.gvcp.acquire_ccp()
            print("  CCP acquired")

            # Enable dual mode
            print("Enabling dual mode (IR + visible)...")
            self.gvcp.enable_dual_mode()
            # Verify
            val = self.gvcp.read_register(REGISTER_DATA_SOURCE)
            print(f"  Register 0x10a110 = {val} (expected 2)")
            if val != VALUE_DUAL:
                print("  WARNING: Dual mode register not set to 2!")

            # Release CCP BEFORE opening HALCON.  Holding CCP while HALCON's
            # GigEVision2 provider initializes makes HALCON fail with error
            # 5312 'device cannot be initialized' (verified on hardware
            # 2026-08-19).  HALCON re-acquires CCP itself on open.
            try:
                self.gvcp.release_ccp()
                print("  CCP released (before HALCON open)")
            except Exception as exc:
                print(f"  CCP release warning: {exc}")

            # Open HALCON grabber.
            # NOTE: do NOT write stream_enable before HALCON open -- HALCON's
            # GigEVision2 provider configures the stream channel itself and a
            # pre-armed external stream (0x10a104=1) breaks device init with
            # HALCON error 5312 (verified on hardware 2026-08-19).
            print(f"Opening HALCON device '{self.device}'...")
            self.grabber = HalconGrabber(self.device)
            self.grabber.open()
            print("  HALCON grabber open")

            # Warmup grabs to flush stale frames
            print("Warming up (flushing stale frames)...")
            for _ in range(5):
                self.grabber.grab(0)
            print("  Warmup complete")

            return True

        except Exception as exc:
            print(f"Setup failed: {exc}")
            self._restore_camera()
            return False

    def _create_writer(self) -> None:
        """Create RecordingWriter with metadata."""
        streams = {self.camera_id: ["IR", "VL"]}
        meta = RecordingWriteMetadata(
            recording_id=self.output_dir.name,
            cameras=[self.camera_id],
            streams=streams,
            trigger="diagnostic",
            application_name="TV46L Capture Tool",
            application_version="1.0.0-experimental",
            start_time=None,  # Will use first frame timestamp
            camera_snapshots=[
                {
                    "camera_id": self.camera_id,
                    "identity": {"camera_id": self.camera_id},
                    "model": "TV46L",
                    "ip": self.camera_ip,
                }
            ],
            roi_snapshots=[],
            ptz_snapshots=[],
            calibration_snapshots=[],
            alarm_snapshots=[],
        )
        self.writer = RecordingWriter(self.output_dir.parent, meta, chunk_target_bytes=64 * 1024 * 1024)
        self.writer.open()
        self.recording_dir = self.writer.recording_dir
        print(f"Recording directory: {self.recording_dir}")

    def _write_frame(
        self,
        array: np.ndarray,
        stream_type: StreamType,
        pfnc_format: int,
        frame_id: Optional[int],
        hw_timestamp_ns: Optional[int],
        capture_ts: float,
    ) -> None:
        """Write one physical record to the recording."""
        if self.writer is None:
            raise RuntimeError("Writer not initialized")

        # Determine dtype and pixel format string
        if stream_type == StreamType.IR:
            dtype = np.uint16
            pixel_format_str = "IR_Data"
            bits_per_channel = 16
        else:
            dtype = np.uint8
            pixel_format_str = "YUV422_8"
            bits_per_channel = 8

        # Ensure array is correct dtype and read-only
        array = array.astype(dtype, copy=False)
        array.setflags(write=False)

        height, width = array.shape[0], array.shape[1]

        # Create minimal frame descriptor for recording
        thermal_meta = StreamMetadata(
            present=(stream_type == StreamType.IR),
            width=width if stream_type == StreamType.IR else None,
            height=height if stream_type == StreamType.IR else None,
            pixel_format=pixel_format_str if stream_type == StreamType.IR else None,
            bits_per_channel=bits_per_channel if stream_type == StreamType.IR else None,
            dtype=str(dtype) if stream_type == StreamType.IR else None,
            byte_count=array.nbytes if stream_type == StreamType.IR else None,
            sequence=self.frames_captured,
            timestamp=capture_ts,
            monotonic_timestamp=capture_ts,
            hardware_timestamp=hw_timestamp_ns / 1e9 if hw_timestamp_ns else None,
        )
        visible_meta = StreamMetadata(
            present=(stream_type == StreamType.VL),
            width=width if stream_type == StreamType.VL else None,
            height=height if stream_type == StreamType.VL else None,
            pixel_format=pixel_format_str if stream_type == StreamType.VL else None,
            bits_per_channel=bits_per_channel if stream_type == StreamType.VL else None,
            dtype=str(dtype) if stream_type == StreamType.VL else None,
            byte_count=array.nbytes if stream_type == StreamType.VL else None,
            sequence=self.frames_captured,
            timestamp=capture_ts,
            monotonic_timestamp=capture_ts,
            hardware_timestamp=hw_timestamp_ns / 1e9 if hw_timestamp_ns else None,
        )

        if stream_type == StreamType.IR:
            sync = SyncInfo(status=SyncStatus.MISSING_VISIBLE)
            payload = FramePayload(thermal=array, visible=None)
        else:
            sync = SyncInfo(status=SyncStatus.MISSING_THERMAL)
            payload = FramePayload(thermal=None, visible=array)

        descriptor = FrameDescriptor(
            camera_id=self.camera_id,
            sequence=self.frames_captured,
            timestamp=capture_ts,
            monotonic_timestamp=capture_ts,
            thermal=thermal_meta,
            visible=visible_meta,
            sync=sync,
            metadata={
                "pfnc_format": f"0x{pfnc_format:08x}",
                "frame_id": frame_id,
                "capture_tool": "tv46l_capture_to_recording",
                "capture_tool_version": "1.0.0-experimental",
            },
        )

        frame = Frame(descriptor=descriptor, payload=payload)
        self.writer.write_frame(frame)

    def run(self) -> int:
        """Run the capture session."""
        if not self.grabber or not self.gvcp:
            print("Not initialized")
            return 1

        self._create_writer()

        print(f"Starting capture for {self.duration_s:.1f} seconds...")
        self.start_time = time.time()
        end_time = self.start_time + self.duration_s

        last_frame_time = self.start_time
        ir_count = 0
        vl_count = 0

        try:
            while time.time() < end_time:
                capture_ts = time.time()
                array = self.grabber.grab(GRAB_TIMEOUT_MS)

                if array is None:
                    self.timeouts += 1
                    # Check if acquisition seems stopped
                    if self.timeouts > 10:
                        print("Warning: Multiple consecutive timeouts, acquisition may have stopped")
                    continue

                # Read pixel format from GVSP leader (via HALCON)
                pfnc_format = self.grabber.get_pixel_format()
                if pfnc_format is None:
                    print("Warning: Could not read pixel format, skipping frame")
                    self.errors += 1
                    continue

                stream_name, stream_type = classify_stream(pfnc_format)
                frame_id = self.grabber.get_frame_id()
                hw_ts_ns = self.grabber.get_hardware_timestamp_ns()

                # Update packet stats from HALCON counters
                self._update_packet_stats()

                self._write_frame(array, stream_type, pfnc_format, frame_id, hw_ts_ns, capture_ts)

                self.frames_captured += 1
                if stream_type == StreamType.IR:
                    self.ir_frames += 1
                    ir_count += 1
                else:
                    self.vl_frames += 1
                    vl_count += 1

                # Progress
                elapsed = capture_ts - self.start_time
                fps = self.frames_captured / elapsed if elapsed > 0 else 0
                print(
                    f"  Frame {self.frames_captured}: {stream_name} "
                    f"(PFNC=0x{pfnc_format:08x}, {array.shape}, {array.dtype}, "
                    f"frame_id={frame_id}, fps={fps:.1f})"
                )

                last_frame_time = capture_ts

        except KeyboardInterrupt:
            print("\nCapture interrupted by user")
        except Exception as exc:
            print(f"\nCapture error: {exc}")
            import traceback
            traceback.print_exc()
            return 1
        finally:
            self.end_time = time.time()
            self._finalize()

        return 0

    def _update_packet_stats(self) -> None:
        """Read HALCON stream packet counters."""
        if not self.grabber:
            return
        ha = self.grabber._import_halcon()
        try:
            seen = ha.get_framegrabber_param(self.grabber.fg, "[Stream]GevStreamSeenPacketCount")
            lost = ha.get_framegrabber_param(self.grabber.fg, "[Stream]GevStreamLostPacketCount")
            resent = ha.get_framegrabber_param(self.grabber.fg, "[Stream]GevStreamResendPacketCount")
            incomplete = ha.get_framegrabber_param(self.grabber.fg, "[Stream]GevStreamIncompleteBlockCount")
            for val, key in [(seen, "seen"), (lost, "lost"), (resent, "resent"), (incomplete, "incomplete")]:
                if isinstance(val, (list, tuple)) and len(val) == 1:
                    val = val[0]
                if val is not None:
                    self.packet_stats[key] = int(val)
        except Exception:
            pass

    def _finalize(self) -> None:
        """Finalize recording and restore camera."""
        if self.writer:
            self.writer.finalize()
            print(f"Recording finalized: {self.recording_dir}")

        # Close HALCON first: it releases the camera's control-channel
        # privilege, making the register restore possible.
        if self.grabber:
            self.grabber.close()
            print("HALCON grabber closed")

        self._restore_camera()

        # Summary
        duration = (self.end_time or time.time()) - (self.start_time or time.time())
        print("\n=== Capture Summary ===")
        print(f"Duration:          {duration:.2f} s")
        print(f"Total frames:      {self.frames_captured}")
        print(f"  IR (Mono16):     {self.ir_frames}")
        print(f"  VL (YUV422_8):   {self.vl_frames}")
        print(f"Timeouts:          {self.timeouts}")
        print(f"Errors:            {self.errors}")
        print(f"Avg FPS:           {self.frames_captured / duration if duration > 0 else 0:.2f}")
        print(f"Packet stats:      {self.packet_stats}")
        if self.recording_dir:
            print(f"Recording:         {self.recording_dir}")

    def _restore_camera(self) -> None:
        """Restore the camera to IR-only mode (register 0x10a110 = 1).

        Order: (re)acquire CCP, write 0x10a110 = 1, verify WHILE holding CCP,
        then release.  Reads of 0x10a110 without CCP are masked and return 0.
        """
        if self.gvcp is None:
            return
        try:
            print("Restoring camera to IR-only mode...")
            try:
                self.gvcp.acquire_ccp()
            except Exception as exc:
                print(f"  CCP acquire warning: {exc}")
            try:
                val = self.gvcp.read_register(REGISTER_DATA_SOURCE)
                print(f"  Register 0x10a110 before = {val}")
                if val != VALUE_IR_ONLY:
                    self.gvcp.write_register(REGISTER_DATA_SOURCE, VALUE_IR_ONLY)
                val = self.gvcp.read_register(REGISTER_DATA_SOURCE)
                print(f"  Register 0x10a110 after = {val} (expected 1)")
            except Exception as exc:
                print(f"  Restore warning: {exc}")
            try:
                self.gvcp.stream_enable(False)
            except Exception:
                pass
            try:
                self.gvcp.release_ccp()
            except Exception:
                pass
            self.gvcp.close()
            print("GVCP connection closed")
        except Exception as exc:
            print(f"  Restore error: {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Experimental TV46L capture to V3 recording (dual IR/VL mode)."
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION_S,
        help=f"Capture duration in seconds (default: {DEFAULT_DURATION_S})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output recording directory (will be created)",
    )
    parser.add_argument(
        "--camera-id",
        help="Camera identifier (e.g., HB25100004)",
    )
    parser.add_argument(
        "--device",
        default=os.environ.get("TV46L_PROBE_DEVICE", "default"),
        help="HALCON GigE Vision device identifier (default: %(default)s)",
    )
    parser.add_argument(
        "--ip",
        default=os.environ.get("TV46L_PROBE_IP", "169.254.24.69"),
        help="Camera IP address for GVCP (default: %(default)s)",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available HALCON GigE Vision devices and exit",
    )
    args = parser.parse_args(argv)

    # List devices if requested
    if args.list_devices:
        import halcon as ha
        try:
            raw = ha.info_framegrabber("GigEVision2", "device")
            if isinstance(raw, tuple) and len(raw) >= 2:
                for entry in (raw[1] if isinstance(raw[1], (list, tuple)) else []):
                    if isinstance(entry, str):
                        idx = entry.find("device:")
                        if idx != -1:
                            start = idx + len("device:")
                            end = entry.find(" |", start)
                            if end == -1:
                                end = len(entry)
                            name = entry[start:end].strip()
                            if name:
                                print(f"  {name}")
        except Exception as exc:
            print(f"Failed to list devices: {exc}")
        return 0

    # Validate output directory
    output_dir = args.output
    if output_dir.exists():
        print(f"Error: Output directory already exists: {output_dir}")
        return 1
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    session = CaptureSession(
        output_dir=output_dir,
        camera_id=args.camera_id,
        device=args.device,
        camera_ip=args.ip,
        duration_s=args.duration,
    )

    if not session.setup():
        return 1

    return session.run()


if __name__ == "__main__":
    sys.exit(main())