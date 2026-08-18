#!/usr/bin/env python3
"""tv46l_dual_stream_probe.py -- TV46L simultaneous IR+visible experiment for V3.

Diagnostic tool (NOT production application logic).  It answers one question:

    Can the Fluke TV46L deliver IR and visible frames SIMULTANEOUSLY over GigE,
    or is the stream source selector camera-global (so two handles both get the
    same source and true simultaneity is impossible)?

The tool is a focused re-run of the V2 ``DualHandleStrategy`` plus an explicit
multipart check, driven by the investigation document:

    docs/hardware/tv46l-dual-stream-investigation.md

It never touches production code and never changes persistent camera settings:
at the end it always restores ``FLK_TI_StreamDataSourceSelector = IR_Data`` and
``bits_per_channel = 16`` on the IR handle.

Experiment
----------
1. Open handle A with the discovery device string -> ``IR_Data`` (bits 16).
   Verify a 640x480 uint16 frame (614400 B).
2. Open handle B with the camera IP address -> ``VL_Data`` (bits -1), while
   handle A stays open.  (HALCON refuses to re-open an in-use device string,
   which is why V2 used the IP for the second handle.)
3. Grab both handles concurrently (non-blocking) for N samples and record for
   each grab: byte size, shape, frameid, grab duration, wall-clock time.
4. Classify the outcome:
   - conflict   : handle A byte size changes to the visible size while B is
                  selected (camera-global selector flipping the whole camera).
   - echo       : handle B returns the IR byte size (second connection delivers
                  the same single source).
   - concurrent : both handles deliver their expected distinct sizes
                  simultaneously (proves true dual-stream).
   - multipart  : a single grab exposes multiple image/data outputs
                  (``image_contents``/``data_contents`` non-singleton).
5. Restore IR_Data, close handles, emit JSON verdict.

Usage
-----
    python scripts/tv46l_dual_stream_probe.py --device <identifier>
    python scripts/tv46l_dual_stream_probe.py --device <identifier> --ip <ip>
    python scripts/tv46l_dual_stream_probe.py --list-devices

The device identifier and camera IP come from the command line or the
TV46L_PROBE_DEVICE / TV46L_PROBE_IP environment variables; they are never
hardcoded here.

Outputs
-------
* reports/hardware/tv46l_dual_stream_<camera_id>.json  (machine verdict)
* prints a one-line summary to stdout
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DEVICE = "default"
DEFAULT_IP = ""
DEFAULT_SAMPLES = 60
MIN_SAMPLES = 10
GRAB_TIMEOUT_MS = 0
FIRST_FRAME_TIMEOUT_MS = 5000
SETTLE_GRABS = 5

IR_SOURCE = "IR_Data"
VISIBLE_SOURCE = "VL_Data"
IR_BITS = 16
VISIBLE_BITS = -1

# Expected frame sizes from the characterization run (used for classification).
EXPECTED_IR_BYTES = 640 * 480 * 2        # 614400 uint16 mono
EXPECTED_VISIBLE_BYTES = 640 * 480 * 3   # 921600 RGB8

MULTIPART_PARAMS = (
    "image_contents",
    "data_contents",
    "image_source_id",
    "image_region_id",
    "image_purpose_id",
)

PERFORMANCE_PARAMS = (
    "buffer_frameid",
    "buffer_timestamp_ns",
    "[Stream]GevStreamLostPacketCount",
    "[Stream]GevStreamSeenPacketCount",
    "[Stream]GevStreamResendCommandCount",
    "[Stream]GevStreamIncompleteBlockCount",
    "[Stream]PayloadSize",
    "image_pixel_format",
)


def _scalar(value):
    """Unwrap a single-element HTuple/list (HALCON convention)."""
    if isinstance(value, (list, tuple)) and len(value) == 1:
        return value[0]
    return value


def _tuple(value):
    """Normalize a HALCON value to a list."""
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


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


def _fmt_bytes(n: int) -> str:
    if n is None:
        return "n/a"
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.3f} MiB"
    return f"{n} B"


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


class TV46LDualStreamProbe:
    """Two-handle simultaneous IR+visible experiment for exactly one TV46L."""

    def __init__(
        self,
        device: str,
        ip: str,
        samples: int,
        out_root: Path,
    ) -> None:
        self.device = device
        self.ip = ip
        self.samples = max(samples, MIN_SAMPLES)
        self.out_root = out_root

        self.ha = None
        self.fg_ir = None      # handle A: IR_Data via device string
        self.fg_vis = None     # handle B: VL_Data via camera IP
        self.ir_bytes = None   # baseline IR byte size (expected 614400)
        self.vis_bytes = None  # baseline visible byte size (expected 921600)

        self.results: dict = {
            "tool": {
                "name": "tv46l_dual_stream_probe",
                "version": "1.0.0",
                "run_utc": datetime.utcnow().isoformat(timespec="seconds"),
                "device_requested": device,
                "ip_requested": ip,
            },
            "verdict": {
                "simultaneous_dual_stream": None,
                "classification": "unresolved",
                "stop_condition": (
                    "SIMULTANEOUS DUAL-STREAM CAPABILITY UNRESOLVED"
                ),
                "note": "",
            },
        }

    # ------------------------------------------------------------------
    # HALCON access
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
            print(f"info_framegrabber failed: {exc}")
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

    # ------------------------------------------------------------------
    # Handle helpers
    # ------------------------------------------------------------------

    def _open_handle(self, device: str, stream: str, bits: int):
        """Open one framegrabber handle and configure its stream source."""
        ha = self._import_halcon()
        fg = ha.open_framegrabber(
            "GigEVision2",
            0, 0, 0, 0, 0, 0,
            "progressive",
            -1,
            "default",
            -1,
            "false",
            "default",
            device,
            0,
            -1,
        )
        try:
            ha.set_framegrabber_param(fg, "FLK_TI_StreamDataSourceSelector", stream)
            ha.set_framegrabber_param(fg, "bits_per_channel", bits)
        except Exception:
            ha.close_framegrabber(fg)
            raise
        return fg

    def _grab(self, fg, timeout_ms: int):
        """Return (array, perf_counter_when_ready) or (None, None) on timeout."""
        ha = self._import_halcon()
        started = time.perf_counter()
        try:
            image = ha.grab_image_async(fg, timeout_ms)
        except Exception as exc:
            msg = str(exc) or ""
            if "5322" in msg or "timeout" in msg.lower():
                return None, None
            raise
        ready = time.perf_counter()
        arr = ha.himage_as_numpy_array(image)
        return arr, ready

    def _read(self, fg, name: str):
        ha = self._import_halcon()
        try:
            return _scalar(ha.get_framegrabber_param(fg, name))
        except Exception:
            return None

    def _multi_outputs(self, fg):
        """Inspect image_contents/data_contents after a grab.

        Returns a dict; a non-singleton tuple means the device sent multiple
        outputs in one acquisition (multipart/GenDC).
        """
        out = {}
        for name in MULTIPART_PARAMS:
            value = self._read(fg, name)
            if value is None:
                continue
            values = _tuple(value)
            out[name] = values
            if len(values) > 1:
                out["_multi_output_detected"] = True
        return out

    # ------------------------------------------------------------------
    # Experiment
    # ------------------------------------------------------------------

    def run(self) -> int:
        code = 0
        try:
            code = self._run()
        finally:
            self._restore_and_close()
        self._emit(verbose=True)
        return code

    def _run(self) -> int:
        ha = self._import_halcon()
        print(f"HALCON version: {_scalar(ha.get_system('version'))}")

        try:
            self.fg_ir = self._open_handle(self.device, IR_SOURCE, IR_BITS)
        except Exception as exc:
            self.results["verdict"]["note"] = f"open IR handle failed: {exc}"
            self.results["verdict"]["classification"] = "connect_error"
            return 2
        print("handle A (IR_Data via device string) open")

        # Baseline IR frame.
        try:
            arr, _ = self._grab(self.fg_ir, FIRST_FRAME_TIMEOUT_MS)
        except Exception as exc:
            self.results["verdict"]["note"] = f"IR first grab failed: {exc}"
            self.results["verdict"]["classification"] = "connect_error"
            return 2
        if arr is None:
            self.results["verdict"]["note"] = "IR first grab timed out"
            self.results["verdict"]["classification"] = "connect_error"
            return 2
        self.ir_bytes = int(arr.nbytes)
        self.results["ir_baseline"] = {
            "shape": list(arr.shape),
            "nbytes": self.ir_bytes,
            "expected_ir_bytes": EXPECTED_IR_BYTES,
            "dtype": str(arr.dtype),
        }
        print(f"  IR baseline: {list(arr.shape)} {_fmt_bytes(self.ir_bytes)}")
        if self.ir_bytes != EXPECTED_IR_BYTES:
            self.results["verdict"]["note"] += " IR baseline size unexpected; "
        self._note_multipart(self.fg_ir, "ir")

        # Second handle via V2's candidate list (fresh IP first, then the
        # discovery device string as fallback - see halcon_camera_diagnosis
        # _visible_device_candidates).
        if not self.ip:
            self.ip = self._discover_ip(self.device)
        candidates = []
        if self.ip:
            candidates.append(self.ip)
        candidates.append(self.device)
        print(
            "opening visible handle; candidates: "
            + ", ".join(repr(c) for c in candidates)
            + " ..."
        )
        open_errors = []
        for candidate in candidates:
            try:
                self.fg_vis = self._open_handle(
                    candidate, VISIBLE_SOURCE, VISIBLE_BITS
                )
                break
            except Exception as exc:
                open_errors.append(f"{candidate!r}: {exc}")
                self.fg_vis = None
        if self.fg_vis is None:
            self.results["verdict"]["note"] = (
                "visible handle open failed for all candidates: "
                + "; ".join(open_errors)
                + " (HALCON refuses a second connection to the same camera; "
                "V2 observed the same and fell back to time-slicing)"
            )
            self.results["verdict"]["classification"] = "second_handle_failed"
            return 2
        print(f"handle B (VL_Data via {candidate!r}) open")

        # Let both settle, then classify the first visible grab.
        for _ in range(SETTLE_GRABS):
            self._grab(self.fg_vis, 0)
        arr_v, _ = self._grab(self.fg_vis, FIRST_FRAME_TIMEOUT_MS)
        if arr_v is None:
            self.results["verdict"]["note"] = "no visible frame on second handle"
            self.results["verdict"]["classification"] = "no_visible_frame"
            return 2
        self.vis_bytes = int(arr_v.nbytes)
        self.results["visible_first"] = {
            "shape": list(arr_v.shape),
            "nbytes": self.vis_bytes,
            "expected_visible_bytes": EXPECTED_VISIBLE_BYTES,
            "dtype": str(arr_v.dtype),
        }
        print(f"  first visible grab: {list(arr_v.shape)} {_fmt_bytes(self.vis_bytes)}")
        self._note_multipart(self.fg_vis, "vis")

        # Now classify.
        classification = self._classify()
        self.results["verdict"]["classification"] = classification

        if classification == "conflict":
            self.results["verdict"]["note"] = (
                "IR handle byte size changed to the visible size while VL_Data "
                "was selected: the FLK_TI_StreamDataSourceSelector is "
                "camera-global and both handles deliver the same source. "
                "Simultaneous IR+visible is NOT achievable with two handles."
            )
            return 1
        if classification == "echo":
            self.results["verdict"]["note"] = (
                "The second (visible) handle returns the IR byte size: it "
                "echoes the same single stream. No distinct visible feed."
            )
            return 1
        if classification in ("concurrent", "multipart_concurrent"):
            self.results["verdict"]["simultaneous_dual_stream"] = True
            self.results["verdict"]["stop_condition"] = None
            self.results["verdict"]["note"] = (
                "Both handles delivered distinct frame sizes concurrently. "
                "Simultaneous IR+visible appears possible. V3 architecture "
                "decision is required before redesigning acquisition."
            )
            self._measure_concurrent()
        else:
            self.results["verdict"]["note"] = (
                "Outcome ambiguous; see measurements for details."
            )
        return 0

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _classify(self) -> str:
        """Decide conflict / echo / concurrent / multipart.

        Reads the current IR handle after the visible handle has been active:
        - conflict: IR byte size changed away from the IR baseline.
        - echo    : visible byte size == IR baseline and != visible baseline.
        - multipart: a multi-output payload was detected on either handle.
        - concurrent: IR == IR baseline AND visible == visible baseline.
        """
        arr_ir, _ = self._grab(self.fg_ir, GRAB_TIMEOUT_MS)
        ir_now = int(arr_ir.nbytes) if arr_ir is not None else None
        vis_now = self.vis_bytes

        self.results["classification_evidence"] = {
            "ir_handle_bytes_while_visible_selected": ir_now,
            "ir_baseline_bytes": self.ir_bytes,
            "visible_handle_bytes": vis_now,
            "expected_visible_bytes": EXPECTED_VISIBLE_BYTES,
        }

        multipart = bool(self.results.get("multipart", {}).get("_multi_output_detected"))

        if ir_now is not None and ir_now != self.ir_bytes:
            return "multipart_concurrent" if multipart else "conflict"
        if vis_now == self.ir_bytes and vis_now != EXPECTED_VISIBLE_BYTES:
            return "multipart_concurrent" if multipart else "echo"
        if (
            ir_now == self.ir_bytes
            and vis_now == EXPECTED_VISIBLE_BYTES
        ):
            return "concurrent"
        return "multipart_concurrent" if multipart else "unresolved"

    def _note_multipart(self, fg, tag: str) -> None:
        out = self._multi_outputs(fg)
        if out:
            self.results.setdefault("multipart", {})[tag] = out

    # ------------------------------------------------------------------
    # Concurrent measurement (only when distinct streams were observed)
    # ------------------------------------------------------------------

    def _measure_concurrent(self) -> None:
        ha = self._import_halcon()
        rows = {"ir": [], "vis": []}
        for i in range(self.samples):
            t0 = time.perf_counter()
            arr_ir, t_ir = self._grab(self.fg_ir, GRAB_TIMEOUT_MS)
            arr_vis, t_vis = self._grab(self.fg_vis, GRAB_TIMEOUT_MS)
            t1 = time.perf_counter()
            if arr_ir is not None:
                rows["ir"].append(
                    {
                        "sample": i,
                        "nbytes": int(arr_ir.nbytes),
                        "frameid": self._read(self.fg_ir, "buffer_frameid"),
                        "t": round(t_ir - t0, 6),
                        "wall": round(t_ir, 6),
                    }
                )
            if arr_vis is not None:
                rows["vis"].append(
                    {
                        "sample": i,
                        "nbytes": int(arr_vis.nbytes),
                        "frameid": self._read(self.fg_vis, "buffer_frameid"),
                        "t": round(t_vis - t0, 6),
                        "wall": round(t_vis, 6),
                    }
                )
            self.results["concurrent_metrics"] = {
                "stream": {
                    "ir": self._summarize_stream(rows["ir"]),
                    "vis": self._summarize_stream(rows["vis"]),
                },
                "samples": i + 1,
                "overlap_probe": f"{t1 - t0:.6f}s per double-grab loop",
            }

    @staticmethod
    def _summarize_stream(rows) -> dict:
        if not rows:
            return {"count": 0}
        counts = [r.get("frameid") for r in rows if r.get("frameid") is not None]
        frames_per_s = None
        n = len(rows)
        if n >= 2:
            dt = rows[-1]["wall"] - rows[0]["wall"]
            if dt > 0:
                frames_per_s = round((n - 1) / dt, 4)
        return {
            "count": n,
            "bytes_per_frame": rows[-1]["nbytes"] if rows else None,
            "frameids": counts,
            "frameid_gaps": len(counts) - len(set(counts)),
            "approx_fps": frames_per_s,
        }

    # ------------------------------------------------------------------
    # Discovery / restore / emit
    # ------------------------------------------------------------------

    def _discover_ip(self, device: str) -> str:
        ha = self._import_halcon()
        try:
            raw = ha.get_framegrabber_param(self.fg_ir, "[Device]GevDeviceIPAddress")
            value = _scalar(raw)
            import ipaddress

            return str(ipaddress.IPv4Address(int(value)))
        except Exception:
            return ""

    def _restore_and_close(self) -> None:
        ha = self._import_halcon()
        # Restore the IR handle to IR_Data + 16-bit (never leave VL_Data).
        if self.fg_ir is not None:
            try:
                ha.set_framegrabber_param(self.fg_ir, "do_abort_grab", 1)
            except Exception:
                pass
            try:
                ha.set_framegrabber_param(
                    self.fg_ir, "FLK_TI_StreamDataSourceSelector", IR_SOURCE
                )
                ha.set_framegrabber_param(self.fg_ir, "bits_per_channel", IR_BITS)
                self.results["restored"] = {
                    "stream_source_selector": IR_SOURCE,
                    "bits_per_channel": IR_BITS,
                }
            except Exception as exc:
                self.results["restore_error"] = str(exc)
            try:
                ha.close_framegrabber(self.fg_ir)
            except Exception:
                pass
        if self.fg_vis is not None:
            try:
                ha.close_framegrabber(self.fg_vis)
            except Exception:
                pass
        self.fg_ir = self.fg_vis = None

    def _emit(self, verbose: bool = False) -> None:
        camera_id = "cam_probe"
        json_dir = self.out_root / "reports" / "hardware"
        json_dir.mkdir(parents=True, exist_ok=True)
        path = json_dir / f"tv46l_dual_stream_{camera_id}.json"
        path.write_text(
            json.dumps(_json_clean(self.results), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        verdict = self.results["verdict"]
        if verbose:
            print(f"\n  JSON report : {path}")
            print(f"  classification : {verdict['classification']}")
            print(f"  simultaneous   : {verdict['simultaneous_dual_stream']}")
            if verdict.get("stop_condition"):
                print(f"  stop condition : {verdict['stop_condition']}")
            print(f"  note           : {verdict['note']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="TV46L simultaneous IR+visible experiment (V3 diagnostic)."
    )
    parser.add_argument(
        "--device",
        default=os.environ.get("TV46L_PROBE_DEVICE", DEFAULT_DEVICE),
        help="HALCON GigE Vision device identifier for handle A (default: %(default)s).",
    )
    parser.add_argument(
        "--ip",
        default=os.environ.get("TV46L_PROBE_IP", DEFAULT_IP),
        help="Camera IP for handle B; discovered if omitted.",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=DEFAULT_SAMPLES,
        help=f"Samples for the concurrent measurement pass (default: %(default)s, min {MIN_SAMPLES}).",
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

    probe = TV46LDualStreamProbe(args.device, args.ip, args.samples, args.out_root)
    probe._import_halcon()

    if args.list_devices:
        print("GigE Vision devices:")
        for dev in probe._list_devices():
            print(f"  {dev}")
        return 0

    return probe.run()


if __name__ == "__main__":
    sys.exit(main())
