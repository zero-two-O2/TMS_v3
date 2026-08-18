#!/usr/bin/env python3
r"""tv46l_dual_mode_halcon_test.py -- prove HALCON can receive the TV46L dual-source GVSP stream.

Standalone diagnostic (NOT production application logic).  It answers one question:

    Does HALCON 24.11 receive the TV46L native dual-source GVSP stream (IR + visible
    alternating on ONE GVSP channel) through ONE framegrabber handle?

Wire-level evidence (docs/hardware/tv46l-thermoview-stream-analysis.md, packet capture
of ThermoView) shows the camera:

    * is a single GVSP flow  (169.254.24.69:35530 -> PC:50554),
    * alternates Mono16 (IR, 0x01100007) and YUV422_8 (visible, 0x02100032) blocks,
    * is put into dual mode by a GVCP control-channel write  ``WRITEREG 0x10a110 = 2``
      (1 = IR only, 2 = IR + visible),
    * then delivers ~9 IR FPS + ~9 visible FPS (~18 blocks/s).

The question is whether HALCON can consume that same stream.  HALCON GigEVision2 exposes
the FLK_TI_StreamDataSourceSelector feature only as the enum strings ``IR_Data`` /
``VL_Data`` (single-source selection) and exposes NO raw GVCP register-write operator
(verified against the HALCON 24.11 docs and the ``mvtec_halcon`` operator set).  So this
tool contains a MINIMAL GVCP client (DISCOVERY + READREG + WRITEREG for exactly the one
register ``0x10a110``) plus the HALCON experiment.

Experiment modes
----------------
live   (default): open ONE HALCON handle, configure IR_Data/16-bit, start streaming,
       then issue the raw GVCP ``WRITEREG 0x10a110 = 2`` while the SAME acquisition keeps
       running (exactly what ThermoView does: the write happens while the stream is up),
       then keep grabbing that same handle for ``--duration`` seconds.
preopen          : issue the raw GVCP ``WRITEREG 0x10a110 = 2`` FIRST (with CCP), then open
       the HALCON handle and grab for ``--duration`` seconds.  Tests whether HALCON's
       provider initialization resets the dual-mode register.

Both modes keep exactly ONE HALCON framegrabber handle.  Every grab is recorded with its
pixel-format ID (``image_pixel_format``, the PFNC source ID), array shape/dtype/bytes,
frameid and timing; grab timeouts/errors are recorded, not hidden.

Safety: the camera is ALWAYS restored to IR-only at the end (register ``0x10a110 = 1``,
``FLK_TI_StreamDataSourceSelector = IR_Data``), never left in dual mode.

Usage
-----
    python scripts/tv46l_dual_mode_halcon_test.py                 # live mode, 30 s
    python scripts/tv46l_dual_mode_halcon_test.py --mode preopen  # dual first
    python scripts/tv46l_dual_mode_halcon_test.py --duration 60
    python scripts/tv46l_dual_mode_halcon_test.py --write-only    # GVCP write + readback only
    python scripts/tv46l_dual_mode_halcon_test.py --read-register # read 0x10a110 only
    python scripts/tv46l_dual_mode_halcon_test.py --list-devices  # list GigE devices

NOTE: run with the Python 3.10 interpreter that has the real ``halcon`` interface
(``mvtec_halcon``) installed.  The V3 .venv contains an unrelated PyPI package also named
``halcon`` which shadows the real interface; use e.g.
``C:\Users\admin\AppData\Local\Programs\Python\Python310\python.exe``.

Outputs
-------
* reports/hardware/tv46l_dual_mode_halcon_<camera_id>.json  (machine verdict)
* prints per-frame lines plus a one-line summary to stdout
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CAMERA_IP = "169.254.24.69"
DEFAULT_DEVICE = "default"
GVCP_PORT = 3956

# The one vendor register this tool ever touches (stream data source).
# 1 = IR only, 2 = IR + visible (dual).  Source: ThermoView pcap analysis.
REGISTER_DATA_SOURCE = 0x0010A110
VALUE_IR_ONLY = 1
VALUE_DUAL = 2

# GVCP command codes (GigE Vision Control Protocol).
CMD_DISCOVERY_CMD = 0x0002
CMD_DISCOVERY_ACK = 0x0003
CMD_READREG_CMD = 0x0080
CMD_READREG_ACK = 0x0081
CMD_WRITEREG_CMD = 0x0082
CMD_WRITEREG_ACK = 0x0083

# GVCP ack status codes.
STATUS_SUCCESS = 0x0000
STATUS_ACCESS_DENIED = 0x0004

# GVCP CCP (control channel privilege) bootstrap register.
REG_CCP = 0x00000A00

# PFNC pixel-format IDs (identical to the GVSP leader pixel-format field).
PFNC_MONO16 = 0x01100007
PFNC_YUV422_8 = 0x02100032

GRAB_TIMEOUT_MS = 1000
FIRST_FRAME_TIMEOUT_MS = 5000
MAX_CONSECUTIVE_STOPS = 8  # consecutive timeouts/errors => acquisition considered stopped
GVCP_TIMEOUT_S = 1.5
GVCP_RETRIES = 3

DEFAULT_DURATION_S = 30.0


def _scalar(value):
    """Unwrap a single-element HALCON tuple/list."""
    if isinstance(value, (list, tuple)) and len(value) == 1:
        return value[0]
    return value


def _json_clean(obj):
    """Recursively convert numpy/HALCON values to JSON-safe Python types."""
    if isinstance(obj, dict):
        return {str(k): _json_clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_clean(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, (np.ndarray, bytes, bytearray)):
        return str(obj)
    if isinstance(obj, float):
        return round(obj, 6)
    if isinstance(obj, (int, str, bool)) or obj is None:
        return obj
    return str(obj)


# ---------------------------------------------------------------------------
# Minimal GVCP client (single register only)
# ---------------------------------------------------------------------------


class GVCPError(RuntimeError):
    pass


class GVCPTimeout(GVCPError):
    pass


class GVCPClient:
    """MINIMAL GigE Vision control-channel client.

    Implements only DISCOVERY, READREG and WRITEREG (single register) against one
    camera.  The one operation this experiment needs is ``WRITEREG 0x10a110 = 2``.
    """

    def __init__(self, camera_ip: str, timeout: float = GVCP_TIMEOUT_S, retries: int = GVCP_RETRIES) -> None:
        self.camera_ip = camera_ip
        self.timeout = timeout
        self.retries = retries
        self.local_ip = None
        self.reqid = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(timeout)
        self._sock.bind(("", 0))
        self._port = self._sock.getsockname()[1]
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.settimeout(0.1)
            probe.connect((camera_ip, 9))
            self.local_ip = probe.getsockname()[0]
            probe.close()
        except OSError:
            self.local_ip = None

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _next_reqid(self) -> int:
        self.reqid = (self.reqid + 1) & 0xFFFF
        return self.reqid

    def _build_cmd(self, command: int, payload: bytes, reqid: int, flags: int = 0x01) -> bytes:
        return struct.pack(">BBHHH", 0x42, flags, command, len(payload), reqid) + payload

    @staticmethod
    def _parse_ack(data: bytes):
        if len(data) < 8:
            raise GVCPError("ack too short")
        status, ackcmd, length, reqid = struct.unpack(">HHHH", data[:8])
        payload = data[8:8 + length]
        return status, ackcmd, reqid, payload

    def _request(self, command: int, payload: bytes, expected_ack: int):
        """Send a unicast GVCP command and wait for the matching ack.

        Returns (status, ack_payload).  Retries on timeout.
        """
        for _ in range(self.retries):
            reqid = self._next_reqid()
            packet = self._build_cmd(command, payload, reqid)
            self._sock.sendto(packet, (self.camera_ip, GVCP_PORT))
            try:
                while True:
                    data, addr = self._sock.recvfrom(4096)
                    if addr[0] != self.camera_ip:
                        continue
                    status, ackcmd, ack_reqid, ack_payload = self._parse_ack(data)
                    if ack_reqid != reqid:
                        continue
                    if ackcmd != expected_ack:
                        raise GVCPError(
                            "unexpected ack 0x{:04x} (expected 0x{:04x})".format(ackcmd, expected_ack)
                        )
                    return status, ack_payload
            except socket.timeout:
                continue
        raise GVCPTimeout("no GVCP ack from {} after {} tries".format(self.camera_ip, self.retries))

    # -- high level ops -----------------------------------------------------

    def read_register(self, address: int) -> int:
        """READREG; returns the register value.  Raises GVCPError on non-success."""
        status, payload = self._request(CMD_READREG_CMD, struct.pack(">I", address), CMD_READREG_ACK)
        if status != STATUS_SUCCESS:
            raise GVCPError("READREG 0x{:08x} status 0x{:04x}".format(address, status))
        if len(payload) < 4:
            raise GVCPError("READREG 0x{:08x} ack payload too short".format(address))
        return struct.unpack(">I", payload[:4])[0]

    def write_register(self, address: int, value: int) -> int:
        """WRITEREG; returns the ack status (0 = success, 0x0004 = access denied)."""
        status, _ = self._request(
            CMD_WRITEREG_CMD,
            struct.pack(">II", address, value),
            CMD_WRITEREG_ACK,
        )
        return status

    def acquire_ccp(self) -> int:
        """Write 1 to the control channel privilege register."""
        return self.write_register(REG_CCP, 1)

    def release_ccp(self) -> int:
        """Write 0 to the control channel privilege register."""
        return self.write_register(REG_CCP, 0)

    def discover(self, timeout: float = 0.6) -> dict | None:
        """Broadcast DISCOVERY and parse the ack from our camera (best-effort).

        Returns a dict of camera identity fields, or None on failure.  Parsing
        follows the GVCP DISCOVERY_ACK payload layout (verified against the pcap).
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("", 0))
            reqid = self._next_reqid()
            packet = self._build_cmd(CMD_DISCOVERY_CMD, b"", reqid, flags=0x11)
            sock.sendto(packet, ("255.255.255.255", GVCP_PORT))
            info = None
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    break
                if addr[0] != self.camera_ip:
                    continue
                try:
                    status, ackcmd, ack_reqid, payload = self._parse_ack(data)
                except GVCPError:
                    continue
                if ackcmd != CMD_DISCOVERY_ACK or ack_reqid != reqid:
                    continue
                info = self._parse_discovery(payload)
                break
            sock.close()
            return info
        except OSError:
            return None

    @staticmethod
    def _parse_discovery(payload: bytes) -> dict:
        def _str(off: int, size: int) -> str:
            if off + size > len(payload):
                return ""
            return payload[off:off + size].split(b"\x00")[0].decode("utf-8", "replace").strip()

        info: dict = {}
        if len(payload) >= 2:
            info["spec_version"] = struct.unpack(">H", payload[:2])[0]
        if len(payload) >= 22:
            ip = struct.unpack(">I", payload[18:22])[0]
            info["ip"] = socket.inet_ntoa(struct.pack(">I", ip))
        if len(payload) >= 50:
            info["mac"] = payload[42:50][:6].hex(":")
        if len(payload) >= 238:
            info["vendor"] = _str(78, 32)
            info["model"] = _str(110, 32)
            info["firmware"] = _str(142, 32)
            info["manufacturer_specific"] = _str(174, 48)
            info["serial"] = _str(222, 16)
        return info


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------


class TV46LDualModeHalconTest:
    """Single-handle HALCON dual-mode experiment for exactly one TV46L."""

    def __init__(
        self,
        camera_ip: str,
        device: str,
        mode: str,
        duration_s: float,
        out_root: Path,
        force_ccp: bool = False,
    ) -> None:
        self.camera_ip = camera_ip
        self.device = device
        self.mode = mode
        self.duration_s = duration_s
        self.out_root = out_root
        self.force_ccp = force_ccp

        self.ha = None
        self.fg = None
        self.gvcp = GVCPClient(camera_ip)

        self.rows: list[dict] = []
        self.write_attempt: dict = {}
        self.initial_register_value = None
        self.register_value_after_write = None
        self.register_value_after_halcon_open = None
        self.camera_info: dict = {}
        self.halcon_version = None
        self.stop_reason = "completed"

        self.results: dict = {
            "tool": {
                "name": "tv46l_dual_mode_halcon_test",
                "version": "1.0.0",
                "run_utc": datetime.utcnow().isoformat(timespec="seconds"),
                "camera_ip": camera_ip,
                "device": device,
                "mode": mode,
                "duration_s": duration_s,
            },
            "verdict": {
                "dual_mode_on_wire": None,
                "halcon_received_both": None,
                "classification": "unresolved",
                "note": "",
            },
        }

    # ------------------------------------------------------------------
    # HALCON helpers
    # ------------------------------------------------------------------

    def _import_halcon(self):
        if self.ha is None:
            import halcon as ha

            self.ha = ha
        return self.ha

    def _list_devices(self):
        ha = self._import_halcon()
        devices = []
        try:
            raw = ha.info_framegrabber("GigEVision2", "device")
        except Exception as exc:
            print("info_framegrabber failed: {}".format(exc))
            return devices
        if not isinstance(raw, tuple) or len(raw) < 2:
            return devices
        for entry in (raw[1] if isinstance(raw[1], (list, tuple)) else []):
            if not isinstance(entry, str):
                continue
            idx = entry.find("device:")
            if idx == -1:
                continue
            start = idx + len("device:")
            end = entry.find(" |", start)
            if end == -1:
                end = len(entry)
            name = entry[start:end].strip()
            if name and name not in devices:
                devices.append(name)
        return devices

    def _read(self, fg, name: str):
        ha = self._import_halcon()
        try:
            return _scalar(ha.get_framegrabber_param(fg, name))
        except Exception:
            return None

    def _open_handle(self) -> None:
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
        for name, value in (
            ("[Stream]DeviceStreamChannelNegotiatePacketSize", 1),
            ("[Stream]GevStreamReceiveSocketSize", 1048576),
            ("num_buffers", 8),
        ):
            try:
                ha.set_framegrabber_param(self.fg, name, value)
            except Exception:
                pass
        ha.grab_image_start(self.fg, -1)

    def _close_handle(self) -> None:
        if self.fg is None:
            return
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

    def _grab_row(self, index: int, timeout_ms: int):
        """Grab one frame and record everything.  Never raises."""
        ha = self._import_halcon()
        t0 = time.perf_counter()
        try:
            image = ha.grab_image_async(self.fg, timeout_ms)
            t1 = time.perf_counter()
            array = ha.himage_as_numpy_array(image)
        except Exception as exc:
            t1 = time.perf_counter()
            msg = str(exc) or ""
            code = getattr(exc, "error_code", None)
            if code is None and "5322" in msg:
                code = 5322
            is_timeout = code == 5322 or "timeout" in msg.lower()
            return {
                "index": index,
                "wall": time.time(),
                "mono": t1,
                "kind": "timeout" if is_timeout else "error",
                "error_code": code,
                "message": msg[:500],
                "image_type": None,
                "pixel_format_id": None,
                "pixel_format_name": None,
                "shape": None,
                "width": None,
                "height": None,
                "channels": None,
                "dtype": None,
                "byte_count": None,
                "frameid": None,
                "grab_s": round(t1 - t0, 6),
            }

        pfnc = self._read(self.fg, "image_pixel_format")
        frameid = self._read(self.fg, "buffer_frameid")

        if array is not None:
            shape = list(array.shape)
            width = int(array.shape[1]) if len(array.shape) >= 2 else None
            height = int(array.shape[0]) if array.ndim >= 1 else None
            channels = int(array.shape[2]) if array.ndim >= 3 else 1
            dtype = str(array.dtype)
            byte_count = int(array.nbytes)
        else:
            shape = width = height = channels = dtype = byte_count = None

        image_type, fmt_name = self._classify(pfnc, array)

        return {
            "index": index,
            "wall": time.time(),
            "mono": t1,
            "kind": "frame",
            "image_type": image_type,
            "pixel_format_id": pfnc,
            "pixel_format_name": fmt_name,
            "shape": shape,
            "width": width,
            "height": height,
            "channels": channels,
            "dtype": dtype,
            "byte_count": byte_count,
            "frameid": frameid,
            "grab_s": round(t1 - t0, 6),
        }

    @staticmethod
    def _classify(pfnc, array):
        if pfnc == PFNC_MONO16:
            return "IR", "Mono16"
        if pfnc == PFNC_YUV422_8:
            return "VISIBLE", "YUV422_8"
        if pfnc is not None and isinstance(pfnc, int) and pfnc != 0:
            return "UNKNOWN", "pfid:{:#010x}".format(pfnc)
        if array is not None and array.ndim >= 2:
            if array.dtype == np.uint16 and array.nbytes == 640 * 480 * 2:
                return "IR", "Mono16(guess)"
            if array.dtype == np.uint8 and array.nbytes == 640 * 480 * 3:
                return "VISIBLE", "RGB8(fromYUV)"
            if array.dtype == np.uint8 and array.nbytes == 640 * 480 * 2:
                return "VISIBLE", "YUV422_8(raw)"
        return "UNKNOWN", "?"

    # ------------------------------------------------------------------
    # GVCP helpers
    # ------------------------------------------------------------------

    def _gvcp_write_dual(self) -> None:
        """WRITEREG 0x10a110 = 2, with readback, always holding CCP.

        Measured behavior of this camera (raw GVCP, verified 2026-08-18):
        * reads of 0x10a110 without CCP held are MASKED (return 0);
        * WRITEREG 0x10a110 without CCP held returns 0x8006 (BAD_ALIGNMENT);
        * with CCP held, write 0/1/2 all succeed and read back exactly;
        * after release_ccp, reads are masked again.
        """
        attempt = {
            "register": "0x{:08x}".format(REGISTER_DATA_SOURCE),
            "write_value": VALUE_DUAL,
            "mode": self.mode,
            "force_ccp": self.force_ccp,
        }
        ccp_status = None
        try:
            ccp_status = self.gvcp.acquire_ccp()
            attempt["ccp_status"] = ccp_status
        except Exception as exc:
            attempt["ccp_error"] = str(exc)

        try:
            attempt["initial_value"] = self.gvcp.read_register(REGISTER_DATA_SOURCE)
        except Exception as exc:
            attempt["initial_read_error"] = str(exc)

        try:
            status = self.gvcp.write_register(REGISTER_DATA_SOURCE, VALUE_DUAL)
            attempt["write_status"] = status
            attempt["write_success"] = status == STATUS_SUCCESS
        except Exception as exc:
            attempt["write_error"] = str(exc)
            attempt["write_success"] = False

        try:
            attempt["value_after_write"] = self.gvcp.read_register(REGISTER_DATA_SOURCE)
        except Exception as exc:
            attempt["readback_error"] = str(exc)

        if ccp_status == STATUS_SUCCESS:
            try:
                self.gvcp.release_ccp()
            except Exception:
                pass

        self.write_attempt = attempt
        self.initial_register_value = attempt.get("initial_value")
        self.register_value_after_write = attempt.get("value_after_write")

    # ------------------------------------------------------------------
    # Grab loop
    # ------------------------------------------------------------------

    def _grab_loop(self, duration_s: float) -> str:
        """Grab continuously for duration_s; returns stop reason."""
        deadline = time.monotonic() + duration_s
        index = 0
        consecutive_stops = 0
        stop_reason = "completed"
        while time.monotonic() < deadline:
            row = self._grab_row(index + 1, GRAB_TIMEOUT_MS)
            self.rows.append(row)
            self._print_row(row)
            if row["kind"] == "frame":
                consecutive_stops = 0
            else:
                consecutive_stops += 1
                if consecutive_stops >= MAX_CONSECUTIVE_STOPS:
                    stop_reason = (
                        "acquisition stopped after {} consecutive {}s".format(
                            consecutive_stops, row["kind"]
                        )
                    )
                    break
            index += 1
        return stop_reason

    def _print_row(self, row: dict) -> None:
        if row["kind"] == "frame":
            w = row["width"] or 0
            h = row["height"] or 0
            bc = row["byte_count"] or 0
            print(
                "{:04d} {:<8s} {:<12s} {}x{} {} ({}, ch={}, fid={})".format(
                    row["index"],
                    str(row["image_type"]),
                    str(row["pixel_format_name"]),
                    w, h, bc,
                    row["dtype"], row["channels"], row["frameid"],
                )
            )
        else:
            print(
                "{:04d} {:<8s} code={} {}".format(
                    row["index"], row["kind"].upper(), row["error_code"], row["message"][:120]
                )
            )

    # ------------------------------------------------------------------
    # Experiment drivers
    # ------------------------------------------------------------------

    def _run_live(self) -> None:
        """Open HALCON, start IR stream, write dual register, keep grabbing."""
        ha = self._import_halcon()
        self.halcon_version = _scalar(ha.get_system("version"))
        print("HALCON version: {}".format(self.halcon_version))
        print("GVCP camera: {} (local GVCP source {})".format(self.camera_ip, self.gvcp.local_ip))

        self._open_handle()
        print("ONE framegrabber handle open (IR_Data, 16-bit)")

        baseline = self._grab_row(0, FIRST_FRAME_TIMEOUT_MS)
        self.baseline = baseline
        self._print_row(baseline)

        print("writing GVCP register 0x{:08x} = {} (dual mode)...".format(REGISTER_DATA_SOURCE, VALUE_DUAL))
        self._gvcp_write_dual()
        self._print_write()

        self.stop_reason = self._grab_loop(self.duration_s)

    def _run_preopen(self) -> None:
        """Write dual register FIRST, then open HALCON and grab."""
        ha = self._import_halcon()
        self.halcon_version = _scalar(ha.get_system("version"))
        print("HALCON version: {}".format(self.halcon_version))
        print("GVCP camera: {} (local GVCP source {})".format(self.camera_ip, self.gvcp.local_ip))

        print("writing GVCP register 0x{:08x} = {} (dual mode) BEFORE opening HALCON...".format(
            REGISTER_DATA_SOURCE, VALUE_DUAL
        ))
        self._gvcp_write_dual()
        self._print_write()

        self._open_handle()
        print("ONE framegrabber handle open (IR_Data, 16-bit)")

        try:
            self.register_value_after_halcon_open = self.gvcp.read_register(REGISTER_DATA_SOURCE)
        except Exception as exc:
            self.results.setdefault("after_halcon_open", {})["read_error"] = str(exc)
        print(
            "register 0x{:08x} after HALCON open: {}".format(
                REGISTER_DATA_SOURCE, self.register_value_after_halcon_open
            )
        )

        baseline = self._grab_row(0, FIRST_FRAME_TIMEOUT_MS)
        self.baseline = baseline
        self._print_row(baseline)

        self.stop_reason = self._grab_loop(self.duration_s)

    def _print_write(self) -> None:
        print("  GVCP write attempt: {}".format(json.dumps(_json_clean(self.write_attempt), indent=2)))

    # ------------------------------------------------------------------
    # Analysis / report
    # ------------------------------------------------------------------

    def _analyze(self) -> None:
        frames = [r for r in self.rows if r["kind"] == "frame"]
        ir = [r for r in frames if r["image_type"] == "IR"]
        vis = [r for r in frames if r["image_type"] == "VISIBLE"]
        unk = [r for r in frames if r["image_type"] not in ("IR", "VISIBLE")]
        timeouts = [r for r in self.rows if r["kind"] == "timeout"]
        errors = [r for r in self.rows if r["kind"] == "error"]

        def _fps(sub):
            if len(sub) < 2:
                return None
            dt = sub[-1]["mono"] - sub[0]["mono"]
            if dt <= 0:
                return None
            return round((len(sub) - 1) / dt, 3)

        ir_fps = _fps(ir)
        vis_fps = _fps(vis)
        total = len(frames)
        span = 0.0
        if len(frames) >= 2:
            span = frames[-1]["mono"] - frames[0]["mono"]
        blocks_per_s = round(total / span, 3) if span > 0 else None

        types = [r["image_type"] for r in frames]
        alternating = False
        transitions = 0
        if len(types) >= 2:
            for a, b in zip(types, types[1:]):
                if a != b:
                    transitions += 1
            alternating = transitions / (len(types) - 1) > 0.6

        visible_arr_fmt = None
        for r in vis:
            if r["dtype"] == "uint8" and r["channels"] == 3:
                visible_arr_fmt = "RGB8(921600B)"
            elif r["dtype"] == "uint8" and r["byte_count"] == 614400:
                visible_arr_fmt = "YUV422_8-raw(614400B)"
            if visible_arr_fmt:
                break

        dual_on_wire = bool(
            self.write_attempt.get("write_success")
            and self.register_value_after_write == VALUE_DUAL
        )
        halcon_received_both = bool(ir and vis and ir_fps and vis_fps)

        classification = "unresolved"
        note = ""
        if not dual_on_wire:
            classification = "gvcp_write_failed"
            note = (
                "The raw GVCP WRITEREG 0x10a110=2 did not reach the camera (ack status "
                "0x{:04x}); the stream was never switched to dual mode.".format(
                    self.write_attempt.get("write_status", -1)
                )
            )
        elif not vis:
            if errors or timeouts:
                classification = "halcon_error_after_dual_write"
                note = (
                    "Register was written to dual mode, but HALCON did not deliver visible "
                    "frames; {} errors / {} timeouts recorded. See frame rows.".format(
                        len(errors), len(timeouts)
                    )
                )
            else:
                classification = "halcon_single_format_only"
                note = (
                    "Register was written to dual mode, but the single HALCON handle delivered "
                    "only Mono16 frames ({} IR frames). HALCON may ignore/reject the YUV422_8 "
                    "blocks while keeping the IR stream.".format(len(ir))
                )
        elif ir_fps and vis_fps and 5 <= ir_fps <= 12 and 5 <= vis_fps <= 12:
            classification = "success_dual_mode"
            note = (
                "One HALCON handle received BOTH Mono16 (IR) and YUV422_8 (visible) frames "
                "from the single dual-mode GVSP stream at ~{} IR FPS + ~{} visible FPS "
                "(~{} blocks/s).".format(ir_fps, vis_fps, blocks_per_s)
            )
        else:
            classification = "dual_mode_partial"
            note = (
                "Both frame types were seen but rates are not in the expected ~9+9 range "
                "(IR {} FPS, visible {} FPS).".format(ir_fps, vis_fps)
            )

        self.results["verdict"] = {
            "dual_mode_on_wire": dual_on_wire,
            "halcon_received_both": halcon_received_both,
            "classification": classification,
            "note": note,
        }
        self.results["summary"] = {
            "total_frames": total,
            "ir_frames": len(ir),
            "visible_frames": len(vis),
            "unknown_frames": len(unk),
            "timeouts": len(timeouts),
            "errors": len(errors),
            "ir_fps": ir_fps,
            "visible_fps": vis_fps,
            "blocks_per_sec": blocks_per_s,
            "alternating": alternating,
            "visible_array_format": visible_arr_fmt,
            "first_types": types[:24],
            "stop_reason": self.stop_reason,
            "elapsed_s": round(self.duration_s, 1),
        }

    # ------------------------------------------------------------------
    # Restore / emit
    # ------------------------------------------------------------------

    def _restore_ir_only(self) -> None:
        """Always leave the camera in IR-only mode.

        Order: close the HALCON handle first (releases its CCP), then re-acquire
        CCP, write register 0x10a110 = 1, verify WHILE holding CCP, then release.
        (Reads of 0x10a110 without CCP are masked and return 0, so verification
        must happen with CCP held.)
        """
        self._close_handle()
        restored = {}
        try:
            ccp_status = self.gvcp.acquire_ccp()
            restored["restore_ccp_status"] = ccp_status
        except Exception as exc:
            restored["restore_ccp_error"] = str(exc)
        try:
            val = self.gvcp.read_register(REGISTER_DATA_SOURCE)
            restored["value_before_restore"] = val
            if val != VALUE_IR_ONLY:
                try:
                    status = self.gvcp.write_register(REGISTER_DATA_SOURCE, VALUE_IR_ONLY)
                    restored["restore_write_status"] = status
                except Exception as exc:
                    restored["restore_write_error"] = str(exc)
            restored["value_after_restore"] = self.gvcp.read_register(REGISTER_DATA_SOURCE)
        except Exception as exc:
            restored["restore_error"] = str(exc)
        try:
            self.gvcp.release_ccp()
        except Exception:
            pass
        self.results["restored"] = restored

    def _emit(self) -> None:
        camera_id = "cam_{}".format(
            (self.camera_info.get("serial") or self.device or "probe").replace(":", "").replace(" ", "")
        )
        json_dir = self.out_root / "reports" / "hardware"
        json_dir.mkdir(parents=True, exist_ok=True)
        path = json_dir / "tv46l_dual_mode_halcon_{}.json".format(camera_id)
        payload = dict(self.results)
        payload["frames"] = [
            {k: v for k, v in row.items() if k != "mono"} for row in self.rows
        ]
        payload["baseline"] = self.baseline
        path.write_text(
            json.dumps(_json_clean(payload), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        self.results["report_path"] = str(path)
        v = self.results["verdict"]
        print()
        print("JSON report : {}".format(path))
        print("classification : {}".format(v["classification"]))
        print("dual_mode_on_wire : {}".format(v["dual_mode_on_wire"]))
        print("halcon_received_both : {}".format(v["halcon_received_both"]))
        print("note : {}".format(v["note"]))

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> int:
        code = 0
        ha = self._import_halcon()
        self.halcon_version = _scalar(ha.get_system("version"))
        self.camera_info = self.gvcp.discover() or {}
        if self.camera_info:
            self.results["camera"] = self.camera_info
        self.results["network"] = {
            "camera_ip": self.camera_ip,
            "local_ip": self.gvcp.local_ip,
            "gvcp_port": GVCP_PORT,
        }
        if not self.camera_info:
            print("DISCOVERY: no ack from {}; camera may be unreachable on this link.".format(self.camera_ip))
            code = 2
        else:
            print("DISCOVERY: {} model={} serial={} ip={}".format(
                self.camera_info.get("vendor", "?"),
                self.camera_info.get("model", "?"),
                self.camera_info.get("serial", "?"),
                self.camera_info.get("ip", "?"),
            ))
        try:
            if self.mode == "live":
                self._run_live()
            else:
                self._run_preopen()
        except Exception as exc:
            print("EXPERIMENT FAILED: {}".format(exc))
            self.results["verdict"]["note"] += " experiment error: {}".format(exc)
            code = 3
        finally:
            try:
                self._analyze()
            except Exception as exc:
                print("ANALYSIS FAILED: {}".format(exc))
            self._restore_ir_only()
            self._emit()
            self.gvcp.close()
        return code


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="TV46L dual-mode HALCON experiment (V3 diagnostic)."
    )
    parser.add_argument(
        "--ip",
        default=DEFAULT_CAMERA_IP,
        help="Camera IP for GVCP writes (default: %(default)s).",
    )
    parser.add_argument(
        "--device",
        default=os.environ.get("TV46L_PROBE_DEVICE", DEFAULT_DEVICE),
        help="HALCON GigE Vision device identifier (default: %(default)s).",
    )
    parser.add_argument(
        "--mode",
        choices=("live", "preopen"),
        default="live",
        help=(
            "live = open HALCON first, then write dual mode while streaming "
            "(mirrors ThermoView); preopen = write dual mode first, then open "
            "HALCON (tests provider init). Default: %(default)s."
        ),
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION_S,
        help="Grab duration in seconds (default: %(default)s).",
    )
    parser.add_argument(
        "--force-ccp",
        action="store_true",
        help="Acquire GVCP control channel privilege before the write, even in live mode.",
    )
    parser.add_argument(
        "--write-only",
        action="store_true",
        help="Only perform the GVCP WRITEREG 0x10a110=2 (with readback); no HALCON.",
    )
    parser.add_argument(
        "--read-register",
        action="store_true",
        help="Only read register 0x10a110 and print its value; no writes, no HALCON.",
    )
    parser.add_argument(
        "--no-restore",
        action="store_true",
        help="Do NOT restore the camera to IR-only at the end (default restores).",
    )
    parser.add_argument(
        "--out-root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root for reports/ output (default: repo root).",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available GigE Vision devices and exit.",
    )
    args = parser.parse_args(argv)

    client = GVCPClient(args.ip)

    if args.list_devices:
        probe = TV46LDualModeHalconTest(args.ip, args.device, args.mode, args.duration, args.out_root)
        probe._import_halcon()
        print("GigE Vision devices:")
        for dev in probe._list_devices():
            print("  {}".format(dev))
        return 0

    if args.read_register:
        try:
            value = client.read_register(REGISTER_DATA_SOURCE)
            print("register 0x{:08x} = {} ({})".format(REGISTER_DATA_SOURCE, value, "IR only" if value == 1 else "dual" if value == 2 else "unknown"))
        except Exception as exc:
            print("READREG failed: {}".format(exc))
            return 2
        finally:
            client.close()
        return 0

    if args.write_only:
        test = TV46LDualModeHalconTest(args.ip, args.device, args.mode, 0.0, args.out_root, args.force_ccp)
        test.camera_info = test.gvcp.discover() or {}
        test._gvcp_write_dual()
        test._print_write()
        print(json.dumps(_json_clean(test.write_attempt), indent=2))
        # restore IR-only unless disabled
        if not args.no_restore:
            test._restore_ir_only()
            print("restored: {}".format(json.dumps(_json_clean(test.results.get("restored", {})))))
        test.gvcp.close()
        ok = test.write_attempt.get("write_success") and test.register_value_after_write == VALUE_DUAL
        print("WRITE OK" if ok else "WRITE FAILED")
        return 0 if ok else 2

    test = TV46LDualModeHalconTest(
        args.ip, args.device, args.mode, args.duration, args.out_root, args.force_ccp
    )
    return test.run()


if __name__ == "__main__":
    sys.exit(main())
