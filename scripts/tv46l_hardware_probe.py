#!/usr/bin/env python3
"""tv46l_hardware_probe.py -- one-camera TV46L hardware characterization for V3.

Diagnostic tool (NOT production application logic).  It characterizes the real
TV46L acquisition path:

    TV46L
      -> HALCON
      -> V3 TV46LDriver
      -> raw GrabResult
      -> measurements

so that V3 frame/storage assumptions (ADR-002 / ADR-003) can be frozen on
measured facts instead of V2 guesses.

The tool deliberately does NOT implement calibration, ROI, alarms, recording,
offline mode, a GUI, GPU processing, multi-camera orchestration, or a
shared-memory ring.  It only *measures* the single-camera acquisition path.

The result must not depend on the GUI.  There is no GUI code here.

Outputs
-------
* reports/hardware/tv46l_probe_<camera_id>.json  (machine-readable values)
* docs/hardware/tv46l-characterization.md        (human-readable report)
* temporary/tv46l_probe/...                      (one raw thermal sample +
  metadata; this directory is git-ignored)

Usage
-----
    python scripts/tv46l_hardware_probe.py --device <identifier>
    python scripts/tv46l_hardware_probe.py --list-devices
    python scripts/tv46l_hardware_probe.py --device default --nuc

The device identifier is taken from the command line (or the
TV46L_PROBE_DEVICE environment variable).  It is never hardcoded here.

`--nuc` opts into the one-shot manual NUC (shutter) test.  Without it the tool
only probes NUC exposure, never triggers a correction.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import ipaddress
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from thermal_monitor.camera.driver import (  # noqa: E402
    CameraConnectionError,
    CameraGrabTimeout,
    TV46LDriver,
)
from thermal_monitor.camera.model import CameraConfig, CameraIdentity  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DEVICE = "default"
DEFAULT_FRAMES = 500
MIN_FRAMES = 200
GRAB_TIMEOUT_MS = 500
FIRST_FRAME_TIMEOUT_MS = 5000
DRIVER_PATH_FRAMES = 20
VISIBLE_FRAMES = 30
WARMUP_FRAMES = 3

# HALCON/GigE parameters the probe reads (V2-documented names).
IDENTITY_PARAMS = (
    "[Device]DeviceSerialNumber",
    "[Device]DeviceModelName",
    "[Device]DeviceVendorName",
    "[Device]DeviceVersion",
    "[Device]DeviceUserID",
    "[Device]DeviceID",
    "[Device]GevDeviceIPAddress",
    "[Device]GevDeviceMACAddress",
    "[Device]DeviceAccessStatus",
    "[Device]DeviceType",
    "[Device]DeviceStreamChannelCount",
    "[Device]DeviceLinkHeartbeatTimeout",
    "DeviceModelName",
    "DeviceVendorName",
    "FLK_TI_InfoString",
    "FLK_TI_Info_CurrentDeviceTemperatureC",
    "FLK_TI_Info_VLDataProviderAvailable",
    "FLK_TI_Info_VLDataSize",
    "FLK_TI_Info_REDataProviderAvailable",
    "FLK_TI_Info_REDataRate",
    "FLK_TI_StreamDataSourceSelector",
)

NETWORK_PARAMS = (
    "[Stream]DeviceStreamChannelPacketSize",
    "[Stream]DeviceStreamChannelPacketSizeMax",
    "[Stream]GevStreamReceiveSocketSize",
    "[Stream]GevStreamRingBufferSize",
    "[Stream]GevStreamSeenPacketCount",
    "[Stream]GevStreamLostPacketCount",
    "[Stream]GevStreamDeliveredPacketCount",
    "[Stream]GevStreamResendCommandCount",
    "[Stream]GevStreamResendPacketCount",
    "[Stream]GevStreamDiscardedBlockCount",
    "[Stream]GevStreamIncompleteBlockCount",
    "[Stream]GevStreamDuplicatePacketCount",
    "[Stream]GevStreamSkippedBlockCount",
    "[Stream]GevStreamOversizedBlockCount",
    "[Stream]GevStreamUnavailablePacketCount",
    "[Stream]PayloadSize",
    "[Interface]GevInterfaceMTU",
    "num_buffers",
    "num_buffers_await_delivery",
    "num_buffers_underrun",
    "image_width",
    "image_height",
    "image_pixel_format",
    "bits_per_channel",
    "grab_timeout",
    "volatile",
    "direct_connection",
    "revision",
)

TIMESTAMP_PARAMS = (
    "buffer_timestamp",
    "buffer_timestamp_ns",
    "device_timestamp_frequency",
    "buffer_frameid",
    "buffer_is_incomplete",
)

TIMEOUT_CANDIDATES_MS = (1, 20, 60, 100, 150, 200, 300, 500)
TIMEOUT_ATTEMPTS = 5

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _scalar(value):
    """Unwrap a single-element HTuple/list (HALCON convention)."""
    if isinstance(value, (list, tuple)) and len(value) == 1:
        return value[0]
    return value


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _int_ip(value) -> str:
    try:
        return str(ipaddress.IPv4Address(int(value)))
    except Exception:
        return str(value)


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


def _stats(values: list[float]) -> dict:
    """Summary statistics over a list of seconds."""
    if not values:
        return {"count": 0}
    s = sorted(values)
    n = len(s)
    q = lambda p: s[min(int(p * n), n - 1)]
    return {
        "count": n,
        "mean_s": statistics.fmean(s),
        "median_s": q(0.5),
        "p90_s": q(0.9),
        "p95_s": q(0.95),
        "p99_s": q(0.99),
        "max_s": s[-1],
        "min_s": s[0],
        "stddev_s": statistics.pstdev(s) if n > 1 else 0.0,
    }


def _fmt_ms(sec: float) -> str:
    return f"{sec * 1000.0:.3f} ms"


def _fmt_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.3f} MiB"
    return f"{n} B"


def _fmt_bw(bytes_per_sec: float) -> str:
    mibs = bytes_per_sec / 1024 / 1024
    mbps = bytes_per_sec * 8 / 1e6
    return f"{mibs:.2f} MiB/s ({mbps:.2f} Mbit/s)"


def _channels_of(arr: np.ndarray) -> int:
    if arr.ndim == 2:
        return 1
    if arr.ndim == 3:
        return arr.shape[2]
    return max(1, arr.ndim)


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


class TV46LProbe:
    """Drives the characterization of exactly one TV46L camera."""

    def __init__(
        self,
        device: str,
        frames: int,
        timeout_ms: int,
        run_nuc: bool,
        run_visible: bool,
        run_reconnect: bool,
        sample_dir: Path,
        out_root: Path,
    ) -> None:
        self.device = device
        self.frames = max(frames, 1)
        self.timeout_ms = timeout_ms
        self.run_nuc = run_nuc
        self.run_visible = run_visible
        self.run_reconnect = run_reconnect
        self.sample_dir = sample_dir
        self.out_root = out_root

        self.ha = None
        self.fg = None
        self.driver: TV46LDriver | None = None
        self._param_cache: set[str] = set()
        self._param_probed = False

        self.results: dict = {
            "tool": {
                "name": "tv46l_hardware_probe",
                "version": "1.0.0",
                "run_utc": datetime.utcnow().isoformat(timespec="seconds"),
                "device_requested": device,
            }
        }

    # ------------------------------------------------------------------
    # HALCON access
    # ------------------------------------------------------------------

    def _import_halcon(self):
        if self.ha is None:
            import halcon as ha

            self.ha = ha
        return self.ha

    def _probe_param_names(self) -> None:
        ha = self._import_halcon()
        if self._param_probed or self.fg is None:
            return
        try:
            raw = ha.get_framegrabber_param(self.fg, "available_param_names")
            names = set()
            for p in (raw if isinstance(raw, (list, tuple)) else [raw]):
                if isinstance(p, str):
                    names.add(p)
            self._param_cache = names
        except Exception:
            self._param_cache = set()
        self._param_probed = True

    def has_param(self, name: str) -> bool:
        self._probe_param_names()
        return name in self._param_cache

    def read_param(self, name: str, quiet: bool = True):
        """Read a framegrabber parameter.  Returns the unwrapped value or None."""
        ha = self._import_halcon()
        if self.fg is None:
            return None
        try:
            return _scalar(ha.get_framegrabber_param(self.fg, name))
        except Exception as exc:
            if not quiet:
                print(f"    [param] {name}: ERROR {exc}")
            return None

    def _read_param_or_none(self, name: str):
        """Same as read_param but never raises."""
        try:
            return self.read_param(name, quiet=True)
        except Exception:
            return None

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
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _make_config(self, serial: str | None = None) -> CameraConfig:
        camera_id = f"cam_{serial}" if serial else "cam_probe"
        identity = CameraIdentity(
            camera_id=camera_id,
            serial_number=serial or "UNKNOWN",
            model="",
            vendor="",
        )
        return CameraConfig(
            identity=identity,
            device_identifier=self.device,
            ip_address="",
            grab_timeout_ms=self.timeout_ms,
        )

    def connect(self) -> bool:
        ha = self._import_halcon()
        self.driver = TV46LDriver(self._make_config())
        try:
            self.driver.connect()
        except CameraConnectionError as exc:
            print(f"  CONNECT FAILED: {exc}")
            return False
        except Exception as exc:
            print(f"  CONNECT FAILED (unexpected): {exc!r}")
            return False
        self.fg = self.driver._framegrabber
        self._param_probed = False
        return True

    def disconnect(self) -> None:
        if self.driver is not None:
            try:
                self.driver.disconnect()
            except Exception as exc:
                print(f"  disconnect error: {exc}")
        self.fg = None

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    def read_identity(self) -> None:
        info = {}
        for name in IDENTITY_PARAMS:
            val = self._read_param_or_none(name)
            if val is not None:
                info[name] = val
        self.results["camera_identity_raw"] = _json_clean(info)

        serial = _scalar(info.get("[Device]DeviceSerialNumber"))
        model = _scalar(info.get("[Device]DeviceModelName"))
        vendor = _scalar(info.get("[Device]DeviceVendorName"))
        firmware = _scalar(info.get("[Device]DeviceVersion"))
        user_name = _scalar(info.get("[Device]DeviceUserID"))
        ip = _int_ip(_scalar(info.get("[Device]GevDeviceIPAddress")))

        camera_id = f"cam_{serial}" if serial else f"cam_{self.device}"
        self.results["camera_identity"] = {
            "camera_id": camera_id,
            "serial_number": serial or "UNKNOWN",
            "model": model or "UNKNOWN",
            "vendor": vendor or "UNKNOWN",
            "firmware": firmware or "UNKNOWN",
            "user_name": user_name or "",
            "ip_address": ip,
            "device_identifier": self.device,
            "acquisition_interface": "GigEVision2 (GigE Vision / Gigabit Ethernet)",
            "connection_status": (
                str(self._read_param_or_none("[Device]DeviceAccessStatus") or "OpenReadWrite")
            ),
        }

    # ------------------------------------------------------------------
    # Thermal format (one frame)
    # ------------------------------------------------------------------

    def characterize_thermal(self, arr: np.ndarray, tag: str = "thermal") -> dict:
        dtype = arr.dtype
        nbytes = arr.nbytes
        channels = _channels_of(arr)
        byteorder = dtype.byteorder if dtype.byteorder not in ("|", "=") else "native"
        return {
            "tag": tag,
            "width": arr.shape[1] if arr.ndim >= 2 else arr.shape[0],
            "height": arr.shape[0],
            "dtype": str(dtype),
            "channels": channels,
            "itemsize": dtype.itemsize,
            "total_bytes": int(nbytes),
            "numpy_shape": list(arr.shape),
            "numpy_strides": [int(s) for s in arr.strides],
            "c_contiguous": bool(arr.flags.c_contiguous),
            "byteorder": byteorder,
            "min_raw": int(arr.min()) if arr.size else None,
            "max_raw": int(arr.max()) if arr.size else None,
            "mean_raw": float(arr.mean()) if arr.size else None,
        }

    # ------------------------------------------------------------------
    # Ownership experiment (HALCON -> NumPy)
    # ------------------------------------------------------------------

    def ownership_experiment(self) -> None:
        """Safely determine what himage_as_numpy_array returns.

        Experiment (designed to avoid crashing HALCON):
          1. Grab image A, convert to NumPy A, snapshot bytes + hash.
          2. Grab 3 more images (HALCON may reuse buffers).
          3. Re-read A: did it change?
          4. Inspect owndata/base of A.
          5. ONLY if NumPy owns its memory (owndata=True and base is None),
             release image A and re-read A.  If the array aliases HALCON
             memory this step is skipped as unsafe and marked UNKNOWN.
        """
        ha = self._import_halcon()
        out = {"procedure": [], "ownership": "UNKNOWN"}

        imgA = ha.grab_image_async(self.fg, self.timeout_ms)
        arrA = ha.himage_as_numpy_array(imgA)
        dataA0 = bytes(arrA.tobytes())
        hashA0 = hashlib.sha256(dataA0).hexdigest()
        propsA = {
            "owndata": bool(arrA.flags.owndata),
            "writeable": bool(arrA.flags.writeable),
            "c_contiguous": bool(arrA.flags.c_contiguous),
            "base_type": type(arrA.base).__name__ if arrA.base is not None else None,
        }
        out["array_A_props"] = propsA
        out["procedure"].append("grabbed A; numpy A created")

        # Grab several more frames, then re-check A.
        hashes_b = []
        for _ in range(3):
            imgB = ha.grab_image_async(self.fg, self.timeout_ms)
            arrB = ha.himage_as_numpy_array(imgB)
            hashes_b.append(hashlib.sha256(bytes(arrB.tobytes())).hexdigest())
        out["procedure"].append("grabbed 3 more frames")

        hashA1 = hashlib.sha256(bytes(arrA.tobytes())).hexdigest()
        changed_after_grab = hashA0 != hashA1
        out["A_changed_after_subsequent_grabs"] = changed_after_grab
        out["B_hashes_distinct"] = len(set(hashes_b)) > 1

        # Release test: only safe when NumPy owns the memory.
        release = {"performed": False, "result": None}
        if propsA["owndata"] and propsA["base_type"] is None:
            del imgA
            gc.collect()
            try:
                hashA2 = hashlib.sha256(bytes(arrA.tobytes())).hexdigest()
                release = {
                    "performed": True,
                    "result": "stable" if hashA2 == hashA0 else "changed",
                }
            except Exception as exc:
                release = {"performed": True, "result": f"error: {exc}"}
        else:
            release = {
                "performed": False,
                "result": "skipped: array does not own its memory; "
                "releasing the HALCON image would risk a dangling pointer",
            }
        out["release_test"] = release

        if propsA["owndata"] and propsA["base_type"] is None:
            out["ownership"] = (
                "PROVEN: NumPy array owns its data (owndata=True, base=None); "
                "releasing the HALCON image left the array unchanged"
            )
        elif changed_after_grab:
            out["ownership"] = (
                "PROVEN: array aliases HALCON acquisition memory and changed "
                "after subsequent grabs (buffer reuse)"
            )
        elif not propsA["owndata"]:
            out["ownership"] = (
                "LIKELY aliases HALCON memory (owndata=False) but the reused "
                "buffer happened to be unchanged; releasing is unsafe (UNKNOWN)"
            )
        else:
            out["ownership"] = (
                "UNKNOWN: array survived subsequent grabs but release test was "
                "not safe to perform"
            )

        self.results["halcon_numpy_ownership"] = out

    # ------------------------------------------------------------------
    # Measurement pass (A/B/C/D timing + FPS + sequence + timestamps)
    # ------------------------------------------------------------------

    def run_measurement_pass(self) -> None:
        ha = self._import_halcon()
        fg = self.fg
        frames = self.frames
        timeout_ms = self.timeout_ms

        # Pre-pass: grab one frame to learn the payload geometry so the
        # experimental shared-memory slot can be sized.
        img0 = ha.grab_image_async(fg, FIRST_FRAME_TIMEOUT_MS)
        arr0 = ha.himage_as_numpy_array(img0)
        self.results["thermal_format"] = self.characterize_thermal(arr0, "thermal")
        self.results["thermal_format"]["halcon"] = {
            "image_width": self._read_param_or_none("image_width"),
            "image_height": self._read_param_or_none("image_height"),
            "image_pixel_format": self._read_param_or_none("image_pixel_format"),
            "bits_per_channel": self._read_param_or_none("bits_per_channel"),
            "PixelFormat": self._read_param_or_none("PixelFormat"),
            "PayloadSize": self._read_param_or_none("PayloadSize"),
            "volatile": self._read_param_or_none("volatile"),
        }

        nbytes = arr0.nbytes
        if nbytes <= 0:
            self.results["error"] = "frame payload has zero bytes; aborting pass"
            return

        # Experimental shared-memory slot (mirrors ADR-003 producer copy #2).
        import multiprocessing.shared_memory as sm

        shm = sm.SharedMemory(name=f"TMS_PROBE_{os.getpid()}", create=True, size=nbytes)
        slot = memoryview(shm.buf)
        try:
            grab_times, conv_times, copy_times, slot_times = [], [], [], []
            intervals, seqs, ts_samples = [], [], []
            minmax = []
            prev_completed = None
            first_seq = None

            for i in range(frames):
                t0 = time.perf_counter()
                image = ha.grab_image_async(fg, timeout_ms)
                t1 = time.perf_counter()
                arr = ha.himage_as_numpy_array(image)
                t2 = time.perf_counter()
                owned = arr.copy()
                t3 = time.perf_counter()
                slot[:nbytes] = memoryview(owned).cast("B")
                t4 = time.perf_counter()

                grab_times.append(t1 - t0)
                conv_times.append(t2 - t1)
                copy_times.append(t3 - t2)
                slot_times.append(t4 - t3)

                if prev_completed is not None:
                    intervals.append(t1 - prev_completed)
                prev_completed = t1

                # Per-frame HALCON metadata (best-effort reads)
                seq = self._read_param_or_none("buffer_frameid")
                if seq is not None:
                    try:
                        seqs.append(int(seq))
                    except (TypeError, ValueError):
                        pass
                ts = self._read_param_or_none("buffer_timestamp_ns")
                if ts is not None:
                    ts_samples.append(ts)
                incomplete = self._read_param_or_none("buffer_is_incomplete")
                if incomplete not in (None, 0, "0", False):
                    self.results.setdefault("incomplete_frames_seen", []).append(i)

                if arr.size:
                    minmax.append((int(arr.min()), int(arr.max()), float(arr.mean())))

            # Summaries
            self.results["copy_perf"] = {
                "A_halcon_grab": _stats(grab_times),
                "B_himage_as_numpy_array": _stats(conv_times),
                "C_numpy_copy_owned": _stats(copy_times),
                "D_numpy_to_shm_slot": _stats(slot_times),
                "A_plus_B_plus_C_end_to_end": _stats(
                    [a + b + c for a, b, c in zip(grab_times, conv_times, copy_times)]
                ),
                "frames_measured": len(grab_times),
            }

        finally:
            # Release the exported memoryview BEFORE closing so the mmap has
            # no live exports when SharedMemory.__del__ runs at shutdown.
            try:
                del slot
            except UnboundLocalError:
                pass
            try:
                shm.close()
            except Exception:
                pass
            try:
                shm.unlink()
            except Exception:
                pass

        # FPS / frame timing (intervals span N-1 gaps for N frames).
        if intervals:
            total_wall = sum(intervals)
        else:
            total_wall = 0.0
        self.results["fps"] = {
            "frame_count": frames,
            "measured": True,
            "average_fps": round(frames / max(total_wall, 1e-9), 4),
            "median_frame_interval_s": _stats(intervals)["median_s"] if intervals else None,
            "frame_interval_stats_s": _stats(intervals) if intervals else {},
        }

        # Raw min/max/mean ranges across the pass
        if minmax:
            mins = [m for m, _, _ in minmax]
            maxs = [x for _, x, _ in minmax]
            means = [m for _, _, m in minmax]
            self.results["raw_statistics"] = {
                "min_of_mins": int(min(mins)),
                "max_of_maxs": int(max(maxs)),
                "mean_of_means": float(statistics.fmean(means)),
                "observed_frame_min_range": [int(min(mins)), int(max(mins))],
                "observed_frame_max_range": [int(min(maxs)), int(max(maxs))],
            }

        # Sequence analysis
        self._analyze_sequence(seqs)

        # Timestamp analysis
        self._analyze_timestamps(ts_samples)

    def _analyze_sequence(self, seqs) -> None:
        if not seqs:
            self.results["frame_sequence"] = {
                "hardware_frame_counter": False,
                "note": (
                    "No HALCON/device frame counter is readable (buffer_frameid "
                    "unavailable). This is distinct from the V3 application "
                    "sequence assigned by AcquisitionWorker."
                ),
            }
            return
        unique = sorted(set(seqs))
        missing = [s for s in range(min(unique), max(unique) + 1) if s not in unique]
        reset_observed = any(seqs[i + 1] < seqs[i] for i in range(len(seqs) - 1))
        self.results["frame_sequence"] = {
            "hardware_frame_counter": True,
            "source": "buffer_frameid",
            "first": seqs[0],
            "last": seqs[-1],
            "count": len(seqs),
            "unique_count": len(unique),
            "duplicate_count": len(seqs) - len(unique),
            "missing_count": len(missing),
            "missing_sample": missing[:50],
            "reset_observed": reset_observed,
        }

    def _analyze_timestamps(self, ts_samples) -> None:
        freq = self._read_param_or_none("device_timestamp_frequency")
        raw_ts = self._read_param_or_none("buffer_timestamp")
        ts_ns = self._read_param_or_none("buffer_timestamp_ns")
        available = ts_samples and len(set(ts_samples)) > 1
        self.results["timestamps"] = {
            "hardware_timestamp": "UNKNOWN",
            "gige_ptp_timestamp": "UNKNOWN",
            "buffer_timestamp_readable": raw_ts is not None,
            "buffer_timestamp_ns_readable": ts_ns is not None,
            "device_timestamp_frequency": freq,
            "observed_changing_across_frames": bool(available),
            "sample_values": ts_samples[:5] if ts_samples else [],
            "note": (
                "HALCON may expose buffer_timestamp/buffer_timestamp_ns but "
                "their semantics (hardware vs host) must be interpreted from "
                "the measured values.  Application wall-clock and monotonic "
                "timestamps are measured independently."
            ),
        }
        if raw_ts is not None and ts_ns is not None and available:
            self.results["timestamps"]["hardware_timestamp"] = (
                "LIKELY: buffer_timestamp/buffer_timestamp_ns change across frames "
                "and device_timestamp_frequency is reported"
            )

    # ------------------------------------------------------------------
    # Driver-path validation (the stated test path)
    # ------------------------------------------------------------------

    def validate_driver_path(self) -> None:
        """Exercise the real V3 TV46LDriver.grab() -> GrabResult path."""
        results = []
        for _ in range(DRIVER_PATH_FRAMES):
            try:
                result = self.driver.grab(self.timeout_ms)
            except Exception as exc:
                self.results.setdefault("driver_path_errors", []).append(str(exc))
                continue
            if result.thermal is not None:
                results.append(
                    {
                        "shape": list(result.thermal.shape),
                        "dtype": str(result.thermal.dtype),
                        "owndata": bool(result.thermal.flags.owndata),
                        "writeable": bool(result.thermal.flags.writeable),
                        "thermal_format": result.thermal_format,
                        "grab_duration_s": round(
                            result.grab_completed - result.grab_started, 6
                        ),
                    }
                )
        self.results["driver_path_validation"] = {
            "frames": len(results),
            "grab_ok": len(results) == DRIVER_PATH_FRAMES,
            "sample": results[:5] if results else [],
            "note": (
                "Confirms the TV46L -> HALCON -> V3 TV46LDriver -> GrabResult "
                "path yields owned, read-only, raw thermal arrays."
            ),
        }

    # ------------------------------------------------------------------
    # Timeout behavior
    # ------------------------------------------------------------------

    def timeout_test(self) -> None:
        ha = self._import_halcon()
        out = {}

        def drain_buffered(max_frames: int = 12) -> int:
            """Consume queued frames so a subsequent short grab can time out.

            With num_buffers=8 and continuous acquisition, a 1 ms grab
            normally returns a buffered frame immediately.  Only after the
            queue is drained can a genuine timeout (HALCON 5322) be observed.
            """
            drained = 0
            for _ in range(max_frames):
                try:
                    ha.grab_image_async(self.fg, 1)
                    drained += 1
                except Exception:
                    break
            return drained

        # 1) Confirm the timeout error code surfaced by HALCON.
        out["drained_before_timeout_probe"] = drain_buffered()
        code = None
        msg = ""
        try:
            ha.grab_image_async(self.fg, 1)
            out["short_timeout_grab"] = "returned without error"
        except Exception as exc:
            msg = str(exc)
            code = getattr(exc, "error_code", None)
            try:
                code = int(code)
            except (TypeError, ValueError):
                code = str(code)
        out["error_on_1ms_grab"] = {"code": code, "message": msg[:300]}
        out["code_5322_observed"] = code == 5322

        # 2) Does the V3 driver classify it as CameraGrabTimeout?
        out["drained_before_driver_probe"] = drain_buffered()
        try:
            self.driver.grab(1)
            out["driver_classification"] = "no timeout observed"
        except CameraGrabTimeout:
            out["driver_classification"] = "CameraGrabTimeout (5322 path)"
        except Exception as exc:
            out["driver_classification"] = f"other error: {exc}"

        # 3) Is the camera still usable after a timeout?
        try:
            result = self.driver.grab(self.timeout_ms)
            out["usable_after_timeout"] = result.thermal is not None
        except Exception as exc:
            out["usable_after_timeout"] = False
            out["usable_after_timeout_error"] = str(exc)

        # 4) Practical timeout sweep: drain, then try each timeout.  A value
        #    only 'succeeds' when a frame arrives within the timeout; since the
        #    camera runs at ~9 FPS the queue refills continuously, so the
        #    practical floor reflects drain+refill timing rather than a strict
        #    synchronous grab.
        sweep = {}
        for tmo in TIMEOUT_CANDIDATES_MS:
            ok = 0
            for _ in range(TIMEOUT_ATTEMPTS):
                drain_buffered()
                try:
                    ha.grab_image_async(self.fg, tmo)
                    ok += 1
                except Exception:
                    pass
            sweep[str(tmo)] = {"timeout_ms": tmo, "success": ok, "attempts": TIMEOUT_ATTEMPTS}
        out["timeout_sweep"] = sweep

        practical = None
        for tmo, rec in sweep.items():
            if rec["success"] == TIMEOUT_ATTEMPTS:
                practical = rec["timeout_ms"]
                break
        out["practical_timeout_ms"] = practical

        self.results["timeout"] = out

    # ------------------------------------------------------------------
    # NUC behavior
    # ------------------------------------------------------------------

    def nuc_test(self) -> None:
        ha = self._import_halcon()
        out = {}

        rec = self._read_param_or_none("FLK_TI_ControlFeature_REControlCmd")
        out["nuc_exposed"] = rec is not None
        out["re_control_cmd_current"] = rec
        out["re_control_cmd_capabilities"] = self._read_param_or_none(
            "available_callback_types"
        )
        if not out["nuc_exposed"]:
            out["result"] = "UNKNOWN: NUC control feature not exposed"
            self.results["nuc"] = out
            return

        if not self.run_nuc:
            out["result"] = (
                "NOT TESTED: NUC is exposed but triggering was not requested "
                "(--nuc).  Trigger method documented from V2: "
                "RequestFineOffset -> ExecuteFineOffset -> pause -> flush."
            )
            self.results["nuc"] = out
            return

        # Baseline FPS before NUC.
        def sample_fps(seconds: float) -> dict:
            starts = []
            end = time.perf_counter() + seconds
            while time.perf_counter() < end:
                t0 = time.perf_counter()
                try:
                    ha.grab_image_async(self.fg, self.timeout_ms)
                    starts.append(time.perf_counter() - t0)
                except Exception:
                    pass
            if len(starts) >= 2:
                return {
                    "count": len(starts),
                    "avg_interval_s": statistics.fmean(starts),
                }
            return {"count": len(starts)}

        base = sample_fps(3.0)
        out["baseline"] = base

        # Trigger one NUC.
        t_nuc0 = time.perf_counter()
        try:
            self.driver.perform_nuc()
        except Exception as exc:
            out["trigger"] = f"error: {exc}"
            self.results["nuc"] = out
            return
        out["trigger"] = "ok (RequestFineOffset -> ExecuteFineOffset)"
        out["trigger_return_s"] = round(time.perf_counter() - t_nuc0, 6)

        # Observe the NUC window.
        gaps = []
        last = time.perf_counter()
        end = time.perf_counter() + 5.0
        got = 0
        while time.perf_counter() < end:
            t0 = time.perf_counter()
            try:
                ha.grab_image_async(self.fg, 2000)
                got += 1
                now = time.perf_counter()
                gaps.append(now - last)
                last = now
            except Exception:
                gaps.append(None)
        out["frames_during_nuc_window"] = got
        valid_gaps = [g for g in gaps if g is not None]
        if valid_gaps:
            out["max_frame_gap_during_nuc_s"] = round(max(valid_gaps), 4)
            out["nuc_blackout_s"] = round(max(valid_gaps), 4)
        else:
            out["nuc_blackout_s"] = None
        out["frames_stopped_during_nuc"] = (
            max(valid_gaps) > 0.5 if valid_gaps else None
        )

        post = sample_fps(3.0)
        out["post_nuc"] = post
        out["resumes_automatically"] = post.get("count", 0) >= 2
        out["invalid_frames_during_nuc"] = self.results.get(
            "incomplete_frames_seen", []
        )
        out["startup_nuc_required"] = (
            "UNKNOWN (not observable without a cold camera boot; V2 left it "
            "as an open question)"
        )
        self.results["nuc"] = out

    # ------------------------------------------------------------------
    # Visible stream
    # ------------------------------------------------------------------

    def visible_probe(self) -> None:
        ha = self._import_halcon()
        out = {}

        avail = self._read_param_or_none("FLK_TI_Info_VLDataProviderAvailable")
        vl_size = self._read_param_or_none("FLK_TI_Info_VLDataSize")
        original_selector = self._read_param_or_none("FLK_TI_StreamDataSourceSelector")
        stream_channels = self._read_param_or_none("[Device]DeviceStreamChannelCount")

        out["visible_available"] = bool(avail) if avail is not None else None
        out["visible_available_raw"] = avail
        out["vl_data_size"] = vl_size
        out["original_stream_selector"] = original_selector
        out["device_stream_channel_count"] = stream_channels
        out["simultaneous_ir_visible"] = (
            False if stream_channels == 1 else None
        )
        out["note_handles"] = (
            "TV46L is a single-stream GigE camera (DeviceStreamChannelCount). "
            "IR and visible cannot be acquired simultaneously through one "
            "framegrabber handle; visible requires a separate handle or "
            "time-sliced selector switching."
        )

        if not out["visible_available"]:
            out["result"] = "visible data provider not available on this camera"
            self.results["visible"] = out
            return

        # Switch to visible, measure, restore.
        # V2-proven recipe (halcon_camera_diagnosis.switch_stream): abort any
        # pending grab, select the stream, match bits_per_channel (-1 = HALCON
        # default for VL_Data), restart acquisition, then drain stale frames.
        t_switch0 = time.perf_counter()
        try:
            ha.set_framegrabber_param(self.fg, "do_abort_grab", 1)
        except Exception:
            pass
        try:
            self.driver.set_parameter("FLK_TI_StreamDataSourceSelector", "VL_Data")
            self.driver.set_parameter("bits_per_channel", -1)
        except Exception as exc:
            out["switch_error"] = str(exc)
            self.results["visible"] = out
            return
        try:
            ha.grab_image_start(self.fg, -1)
        except Exception as exc:
            out["switch_error"] = f"restart acquisition failed: {exc}"
            self.results["visible"] = out
            return
        # Drain stale frames queued for the previously selected stream.
        drained = 0
        while drained < 3:
            try:
                ha.grab_image_async(self.fg, 0)
                drained += 1
            except Exception:
                break
        out["drained_stale_frames"] = drained
        try:
            image = ha.grab_image_async(self.fg, FIRST_FRAME_TIMEOUT_MS)
            out["switch_to_first_frame_s"] = round(
                time.perf_counter() - t_switch0, 4
            )
        except Exception as exc:
            out["switch_error"] = f"no frame after switch: {exc}"
            # restore and bail
            try:
                self.driver.set_parameter(
                    "FLK_TI_StreamDataSourceSelector", original_selector or "IR_Data"
                )
                self.driver.set_parameter("bits_per_channel", 16)
            except Exception:
                pass
            self.results["visible"] = out
            return

        visible = ha.himage_as_numpy_array(image)
        fmt = self.characterize_thermal(visible, "visible")
        fmt["halcon"] = {
            "image_width": self._read_param_or_none("image_width"),
            "image_height": self._read_param_or_none("image_height"),
            "image_pixel_format": self._read_param_or_none("image_pixel_format"),
            "PixelFormat": self._read_param_or_none("PixelFormat"),
            "PayloadSize": self._read_param_or_none("PayloadSize"),
        }
        out["visible_format"] = fmt

        # Native FPS over a short window.
        intervals = []
        last = time.perf_counter()
        for _ in range(VISIBLE_FRAMES):
            try:
                ha.grab_image_async(self.fg, FIRST_FRAME_TIMEOUT_MS)
                now = time.perf_counter()
                intervals.append(now - last)
                last = now
            except Exception:
                pass
        if intervals:
            total = sum(intervals)
            out["visible_fps"] = {
                "measured": True,
                "average_fps": round(len(intervals) / max(total, 1e-9), 4),
                "frame_interval_stats_s": _stats(intervals),
            }
        else:
            out["visible_fps"] = {"measured": False}

        # Restore IR_Data (always) with the proven stream-switch recipe.
        try:
            ha.set_framegrabber_param(self.fg, "do_abort_grab", 1)
            self.driver.set_parameter(
                "FLK_TI_StreamDataSourceSelector", original_selector or "IR_Data"
            )
            self.driver.set_parameter("bits_per_channel", 16)
            ha.grab_image_start(self.fg, -1)
            for _ in range(3):
                try:
                    ha.grab_image_async(self.fg, 0)
                except Exception:
                    break
            out["restored_selector"] = original_selector or "IR_Data"
            out["restored_bits_per_channel"] = 16
        except Exception as exc:
            out["restore_error"] = str(exc)

        # Verify thermal still works after the switch-back.
        try:
            image = ha.grab_image_async(self.fg, FIRST_FRAME_TIMEOUT_MS)
            arr = ha.himage_as_numpy_array(image)
            out["thermal_after_visible"] = self.characterize_thermal(arr, "thermal")
        except Exception as exc:
            out["thermal_after_visible"] = f"error: {exc}"

        self.results["visible"] = out

    # ------------------------------------------------------------------
    # Reconnection behavior
    # ------------------------------------------------------------------

    def reconnect_test(self) -> None:
        out = {}
        # close
        t0 = time.perf_counter()
        self.disconnect()
        out["close_s"] = round(time.perf_counter() - t0, 4)
        out["is_connected_after_close"] = (
            self.driver.is_connected() if self.driver else False
        )
        # reopen
        t0 = time.perf_counter()
        ok = self.connect()
        out["reopen_s"] = round(time.perf_counter() - t0, 4)
        out["reopen_ok"] = ok
        # reacquire
        if ok:
            try:
                result = self.driver.grab(self.timeout_ms)
                out["reacquire_ok"] = result.thermal is not None
                out["reacquire_shape"] = list(result.thermal.shape)
            except Exception as exc:
                out["reacquire_ok"] = False
                out["reacquire_error"] = str(exc)
        else:
            out["reacquire_ok"] = False
        out["note"] = (
            "Software close/reopen/reacquire only.  Physical unplug/replug "
            "testing is deferred to a manual session."
        )
        self.results["reconnect"] = out

    # ------------------------------------------------------------------
    # Network parameters
    # ------------------------------------------------------------------

    def read_network(self) -> None:
        net = {}
        for name in NETWORK_PARAMS:
            val = self._read_param_or_none(name)
            if val is not None:
                net[name] = val
        self.results["network"] = _json_clean(net)

    # ------------------------------------------------------------------
    # Sample frame
    # ------------------------------------------------------------------

    def save_sample(self) -> None:
        ha = self._import_halcon()
        self.sample_dir.mkdir(parents=True, exist_ok=True)
        try:
            image = ha.grab_image_async(self.fg, FIRST_FRAME_TIMEOUT_MS)
            arr = ha.himage_as_numpy_array(image)
        except Exception as exc:
            self.results["raw_sample"] = {"saved": False, "error": str(exc)}
            return

        camera_id = self.results.get("camera_identity", {}).get("camera_id", "cam")
        seq = self._read_param_or_none("buffer_frameid") or 0
        ts = time.time()
        base = f"thermal_{camera_id}_seq{seq}"
        raw_path = self.sample_dir / f"{base}.raw"
        meta_path = self.sample_dir / f"{base}.json"
        arr.tofile(raw_path)
        meta = {
            "file": raw_path.name,
            "camera_id": camera_id,
            "sequence": seq,
            "timestamp_epoch_s": ts,
            "timestamp_iso": datetime.utcnow().isoformat(timespec="seconds"),
            "format": self.characterize_thermal(arr, "thermal"),
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        self.results["raw_sample"] = {
            "saved": True,
            "raw_file": str(raw_path),
            "meta_file": str(meta_path),
            "bytes": int(arr.nbytes),
        }

    # ------------------------------------------------------------------
    # Ring buffer impact (ADR-003)
    # ------------------------------------------------------------------

    def compute_ring_impact(self) -> None:
        from thermal_monitor.core.shm import PayloadSpec, RingConfig

        thermal = self.results.get("thermal_format", {})
        visible = self.results.get("visible", {}).get("visible_format")
        fps = self.results.get("fps", {}).get("average_fps", 0.0)
        vfps = self.results.get("visible", {}).get("visible_fps", {}).get("average_fps", 0.0)

        tw = int(thermal.get("width") or 0)
        th = int(thermal.get("height") or 0)
        tdtype = np.dtype(thermal.get("dtype") or "uint16")
        tbytes = int(thermal.get("total_bytes") or (tw * th * tdtype.itemsize))

        vw = vh = vbytes = 0
        vdtype = np.dtype("uint8")
        if visible and visible.get("total_bytes"):
            vw = int(visible.get("width") or 0)
            vh = int(visible.get("height") or 0)
            vdtype = np.dtype(visible.get("dtype") or "uint8")
            vbytes = int(visible.get("total_bytes") or 0)

        frame_bytes = tbytes + vbytes
        bytes_per_sec = frame_bytes * fps
        visible_bytes_per_sec = vbytes * vfps

        cam_id = self.results.get("camera_identity", {}).get("camera_id", "cam")
        thermal_spec = PayloadSpec(width=tw, height=th, dtype=tdtype, bytes_per_frame=tbytes)
        visible_spec = (
            PayloadSpec(width=vw, height=vh, dtype=vdtype, bytes_per_frame=vbytes)
            if vbytes
            else None
        )

        ring_rows = []
        for depth in (8, 16, 32):
            rc = RingConfig(
                camera_id=cam_id, thermal_spec=thermal_spec, visible_spec=visible_spec, depth=depth
            )
            slot = rc.slot_size()
            total1 = rc.total_size()
            total8 = total1 * 8
            ring_rows.append(
                {
                    "depth": depth,
                    "slot_bytes": slot,
                    "slot_mib": round(slot / 1048576, 3),
                    "one_camera_bytes": total1,
                    "one_camera_mib": round(total1 / 1048576, 3),
                    "eight_cameras_bytes": total8,
                    "eight_cameras_mib": round(total8 / 1048576, 3),
                }
            )

        self.results["ring_impact"] = {
            "bytes_per_frame": frame_bytes,
            "thermal_bytes_per_frame": tbytes,
            "visible_bytes_per_frame": vbytes,
            "fps_measured": round(fps, 4),
            "visible_fps_measured": round(vfps, 4) if vbytes else None,
            "bytes_per_sec": round(bytes_per_sec, 2),
            "visible_bytes_per_sec": round(visible_bytes_per_sec, 2) if vbytes else None,
            "thermal_only_bytes_per_sec": round(tbytes * fps, 2),
            "ring_table": ring_rows,
            "note": (
                "Slot size and ring totals computed with the actual ADR-003 "
                "RingConfig/PayloadSpec implementation (thermal + optional "
                "visible payload regions, 4096 B descriptor, 256 B slot header, "
                "64-byte alignment).  The visible scenario assumes both payload "
                "regions are always allocated in the slot; if visible is "
                "time-sliced and never simultaneous, a visible-less slot layout "
                "could be smaller."
            ),
        }

    def recommended_ring(self) -> str:
        ring = self.results.get("ring_impact", {})
        trows = ring.get("ring_table", [])
        fps = ring.get("fps_measured", 0.0)
        if not trows:
            return "No measured ring data; recommendation deferred until hardware is reachable."
        row32 = next((r for r in trows if r["depth"] == 32), trows[-1])
        row16 = next((r for r in trows if r["depth"] == 16), trows[-1])
        nuc = self.results.get("nuc", {})
        nuc_gap = nuc.get("nuc_blackout_s")
        frames_for_nuc = None
        if nuc_gap and fps:
            frames_for_nuc = int(nuc_gap * fps) + 1

        text = [
            "**Recommended provisional ring configuration: depth 32 per camera.**",
            "",
            "Rationale:",
            f"* Measured thermal {_fmt_bytes(ring['thermal_bytes_per_frame'])} "
            f"at ~{fps:.2f} FPS = {_fmt_bw(ring['thermal_only_bytes_per_sec'])} per camera.",
        ]
        if ring.get("visible_bytes_per_frame"):
            text.append(
                f"* Visible adds {_fmt_bytes(ring['visible_bytes_per_frame'])} "
                f"per frame; combined = {_fmt_bytes(ring['bytes_per_frame'])}/frame "
                f"= {_fmt_bw(ring['bytes_per_sec'])}."
            )
        text += [
            f"* depth 32 for 8 cameras = "
            f"{row32['eight_cameras_mib']} MiB (provisional, matches the ADR-003 "
            f"working estimate); depth 16 = {row16['eight_cameras_mib']} MiB.",
            "* Ring depth is a bounded rolling history, not the recording buffer; "
            "pre-alarm history lives in recorder-owned memory (ADR-003 §14).",
            "* The final depth must cover worst-case consumer delay and any NUC "
            "frame gap.",
        ]
        if frames_for_nuc:
            text.append(
                f"* Measured NUC blackout ≈ {nuc_gap}s ≈ {frames_for_nuc} frames at "
                f"the measured FPS; depth 32 covers this provisional margin."
            )
        else:
            text.append(
                "* NUC gap not measured in this run (run with --nuc); depth 32 is "
                "a provisional margin until that is known."
            )
        return "\n".join(text)

    def projection_8cam(self) -> None:
        ring = self.results.get("ring_impact", {})
        tbytes = ring.get("thermal_bytes_per_frame", 0)
        vbytes = ring.get("visible_bytes_per_frame", 0)
        fps = ring.get("fps_measured", 0.0)
        vfps = ring.get("visible_fps_measured")
        ncams = 8

        thermal_bw = tbytes * fps * ncams
        rows = {
            "thermal_8cam": {
                "bytes_per_sec": thermal_bw,
                "human": _fmt_bw(thermal_bw),
            }
        }
        if vbytes and vfps:
            visible_bw = vbytes * vfps * ncams
            combined_bw = thermal_bw + visible_bw
            rows["visible_8cam"] = {
                "bytes_per_sec": visible_bw,
                "human": _fmt_bw(visible_bw),
            }
            rows["combined_8cam"] = {
                "bytes_per_sec": combined_bw,
                "human": _fmt_bw(combined_bw),
                "note": (
                    "Combined assumes IR+visible run simultaneously.  The TV46L is "
                    "a single-stream camera (DeviceStreamChannelCount=1); if the "
                    "two must be time-sliced, the combined rate is the time-sliced "
                    "alternating rate, not the sum."
                ),
            }
        rows["assumptions"] = {
            "cameras": ncams,
            "fps": fps,
            "visible_fps": vfps,
            "linear_scaling_assumed_but_not_proven": True,
        }
        self.results["projection_8cam"] = rows

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def write_reports(self) -> None:
        json_dir = self.out_root / "reports" / "hardware"
        md_dir = self.out_root / "docs" / "hardware"
        json_dir.mkdir(parents=True, exist_ok=True)
        md_dir.mkdir(parents=True, exist_ok=True)

        camera_id = self.results.get("camera_identity", {}).get(
            "camera_id", self.device.replace("/", "_")
        )
        json_path = json_dir / f"tv46l_probe_{camera_id}.json"
        json_path.write_text(
            json.dumps(self.results, indent=2, default=str), encoding="utf-8"
        )
        self.results["outputs"] = {"json": str(json_path), "markdown": str(md_dir / "tv46l-characterization.md")}

        md_path = md_dir / "tv46l-characterization.md"
        md_path.write_text(self._build_markdown(), encoding="utf-8")
        print(f"\n  JSON report : {json_path}")
        print(f"  Markdown    : {md_path}")

    def _build_markdown(self) -> str:
        r = self.results
        lines = []
        A = lines.append
        A("# TV46L Hardware Characterization (V3)")
        A("")
        A(f"* Run UTC: {r['tool'].get('run_utc')}")
        A(f"* Tool: `{r['tool'].get('name')} v{r['tool'].get('version')}`")
        A(f"* Requested device: `{r['tool'].get('device_requested')}`")
        A("")
        A("## Confidence legend")
        A("")
        A("- **CONFIRMED** — directly verified by this run.")
        A("- **MEASURED** — measured value from this run.")
        A("- **INFERRED** — derived/consistent with V2 or the measurement.")
        A("- **UNKNOWN** — not observable in this run.")
        A("")

        ident = r.get("camera_identity", {})
        A("## A. Camera identity")
        A("")
        A("| Field | Value |")
        A("|---|---|")
        for k, v in ident.items():
            A(f"| {k} | `{v}` |")
        A("")

        thermal = r.get("thermal_format", {})
        A("## B. Thermal format")
        A("")
        A("| Field | Value |")
        A("|---|---|")
        for k, v in thermal.items():
            if k == "halcon":
                continue
            A(f"| {k} | `{v}` |")
        A("")
        A("HALCON-reported geometry/format:")
        A("")
        A("```")
        A(json.dumps(thermal.get("halcon", {}), indent=2))
        A("```")
        A("")

        visible = r.get("visible", {})
        if visible:
            A("## C. Visible format")
            A("")
            A("```")
            A(json.dumps(_json_clean(visible), indent=2))
            A("```")
            A("")
        else:
            A("## C. Visible format")
            A("")
            A("**UNKNOWN** — visible probe did not run or reported unavailable.")
            A("")

        fps = r.get("fps", {})
        A("## D. Actual FPS")
        A("")
        A(f"* average FPS: **MEASURED `{fps.get('average_fps')}`**")
        A(f"* frame count: `{fps.get('frame_count')}`")
        A("")

        A("## E. Frame timing")
        A("")
        A("```")
        A(json.dumps(_json_clean(fps.get("frame_interval_stats_s", {})), indent=2))
        A("```")
        A("")

        ts = r.get("timestamps", {})
        A("## F. Hardware timestamp availability")
        A("")
        A(f"* hardware_timestamp = **{ts.get('hardware_timestamp')}**")
        A(f"* buffer_timestamp readable: `{ts.get('buffer_timestamp_readable')}`")
        A(f"* buffer_timestamp_ns readable: `{ts.get('buffer_timestamp_ns_readable')}`")
        A(f"* device_timestamp_frequency: `{ts.get('device_timestamp_frequency')}`")
        A(f"* changing across frames: `{ts.get('observed_changing_across_frames')}`")
        A("")

        seq = r.get("frame_sequence", {})
        A("## G. Sequence availability")
        A("")
        A("```")
        A(json.dumps(_json_clean(seq), indent=2))
        A("```")
        A("")

        own = r.get("halcon_numpy_ownership", {})
        A("## H. HALCON -> NumPy ownership")
        A("")
        A(f"**{own.get('ownership')}**")
        A("")
        A("```")
        A(json.dumps(_json_clean(own), indent=2))
        A("```")
        A("")

        cp = r.get("copy_perf", {})
        A("## I. Copy timings")
        A("")
        A("| Stage | count | mean | median | p95 | max |")
        A("|---|---|---|---|---|---|")
        for name in ("A_halcon_grab", "B_himage_as_numpy_array", "C_numpy_copy_owned", "D_numpy_to_shm_slot", "A_plus_B_plus_C_end_to_end"):
            s = cp.get(name, {})
            if s.get("count"):
                A(
                    f"| {name} | {s['count']} | {_fmt_ms(s['mean_s'])} | "
                    f"{_fmt_ms(s['median_s'])} | {_fmt_ms(s['p95_s'])} | {_fmt_ms(s['max_s'])} |"
                )
        A("")

        to = r.get("timeout", {})
        A("## J. Timeout result")
        A("")
        A("```")
        A(json.dumps(_json_clean(to), indent=2))
        A("```")
        A("")

        nuc = r.get("nuc", {})
        A("## K. NUC result")
        A("")
        A("```")
        A(json.dumps(_json_clean(nuc), indent=2))
        A("```")
        A("")

        vfps = visible.get("visible_fps", {})
        A("## L. Visible acquisition result")
        A("")
        A(f"* visible available: `{visible.get('visible_available')}`")
        A(f"* measured FPS: `{vfps.get('average_fps')}`")
        A(f"* simultaneous IR/visible: `{visible.get('simultaneous_ir_visible')}`")
        A("")

        net = r.get("network", {})
        A("## M. Network / transport observations")
        A("")
        A("```")
        A(json.dumps(net, indent=2))
        A("```")
        A("")

        rec = r.get("reconnect", {})
        A("## N. Reconnection result")
        A("")
        A("```")
        A(json.dumps(_json_clean(rec), indent=2))
        A("```")
        A("")

        ring = r.get("ring_impact", {})
        A("## O. Per-camera bandwidth")
        A("")
        A(f"* bytes/frame: `{ring.get('bytes_per_frame')}` "
          f"(thermal `{ring.get('thermal_bytes_per_frame')}` + visible "
          f"`{ring.get('visible_bytes_per_frame')}`)")
        A(f"* FPS: `{ring.get('fps_measured')}`")
        A(f"* bytes/sec: `{ring.get('bytes_per_sec')}` = "
          f"{_fmt_bw(ring.get('bytes_per_sec') or 0)}")
        A("")

        proj = r.get("projection_8cam", {})
        A("## P. Projected 8-camera bandwidth")
        A("")
        A("```")
        A(json.dumps(_json_clean(proj), indent=2))
        A("```")
        A("")

        A("## Q. Ring-memory calculations (ADR-003)")
        A("")
        A("| depth | slot (B) | 1 camera (MiB) | 8 cameras (MiB) |")
        A("|---|---|---|---|")
        for row in ring.get("ring_table", []):
            A(
                f"| {row['depth']} | {row['slot_bytes']} | {row['one_camera_mib']} "
                f"| {row['eight_cameras_mib']} |"
            )
        A("")
        A("## Q1. Recommended provisional ring configuration")
        A("")
        A(self.recommended_ring())
        A("")

        A("## R. Confirmed facts")
        A("")
        if self.results.get("halcon_numpy_ownership", {}).get("ownership", "").startswith("PROVEN"):
            A("* CONFIRMED: himage_as_numpy_array ownership behavior (see H).")
        A("* CONFIRMED: V3 TV46LDriver opens the camera and returns owned, read-only raw thermal GrabResults.")
        A(f"* CONFIRMED: thermal frame is {thermal.get('width')}x{thermal.get('height')} "
          f"{thermal.get('dtype')} {thermal.get('channels')}-channel "
          f"({_fmt_bytes(thermal.get('total_bytes') or 0)}).")
        A("")

        A("## S. Unknowns")
        A("")
        A("* PTP/GigE hardware timestamp semantics (see F) unless proven.")
        A("* Physical unplug/replug reconnect behavior (manual test deferred).")
        A("* Startup NUC requirement (requires cold-boot observation).")
        A("* 8-camera scaling is a linear projection, not yet measured on hardware.")
        A("")

        A("## T. Recommended next step")
        A("")
        A(
            "Freeze the V3 frame/storage assumptions on these measured values, "
            "then run the multi-camera bandwidth validation on the identified "
            "bottleneck (see P) before finalizing ADR-003 ring depth."
        )
        A("")
        A("---")
        A("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def run(self) -> int:
        print(f"{'=' * 68}")
        print("  TV46L HARDWARE CHARACTERIZATION PROBE  (one camera, no GUI)")
        print(f"{'=' * 68}")
        print(f"  device      : {self.device}")
        print(f"  frames      : {self.frames}")
        print(f"  timeout ms  : {self.timeout_ms}")
        print(f"  run NUC     : {self.run_nuc}")
        print(f"  run visible : {self.run_visible}")
        print(f"  reconnect   : {self.run_reconnect}")

        print(f"\n[{_now_str()}] Importing HALCON ...")
        try:
            self._import_halcon()
        except ImportError as exc:
            print(f"  HALCON runtime not available: {exc}")
            print("\nHARDWARE CHARACTERIZATION INCOMPLETE")
            return 2

        print(f"\n[{_now_str()}] Connecting to {self.device} ...")
        if not self.connect():
            print("\nHARDWARE CHARACTERIZATION INCOMPLETE")
            return 3

        try:
            print("  reading identity ...")
            self.read_identity()
            ident = self.results["camera_identity"]
            print(
                f"  camera_id={ident['camera_id']} serial={ident['serial_number']} "
                f"model={ident['model']}"
            )

            print("  reading network parameters ...")
            self.read_network()

            print("  HALCON -> NumPy ownership experiment ...")
            self.ownership_experiment()

            print(f"  validating V3 driver path ({DRIVER_PATH_FRAMES} frames) ...")
            self.validate_driver_path()

            print(f"  measurement pass ({self.frames} frames) ...")
            self.run_measurement_pass()

            print("  timeout behavior ...")
            self.timeout_test()

            if self.run_visible:
                print("  visible stream probe ...")
                self.visible_probe()

            if self.run_reconnect:
                print("  reconnect (close/reopen/reacquire) ...")
                self.reconnect_test()

            print("  NUC exposure probe ...")
            self.nuc_test()

            print("  saving raw sample ...")
            self.save_sample()

            print("  computing ring impact + 8-camera projection ...")
            self.compute_ring_impact()
            self.projection_8cam()

            print("  writing reports ...")
            self.write_reports()
        finally:
            self.disconnect()

        print("\n")
        print("FINAL REPORT")
        print("------------")
        self._print_summary()

        print("\nHARDWARE CHARACTERIZATION COMPLETE")
        return 0

    def _print_summary(self) -> None:
        r = self.results
        ident = r.get("camera_identity", {})
        thermal = r.get("thermal_format", {})
        fps = r.get("fps", {})
        print(f"A. Camera identity   : {ident.get('camera_id')} / {ident.get('serial_number')} / {ident.get('model')}")
        print(
            f"B. Thermal format    : {thermal.get('width')}x{thermal.get('height')} "
            f"{thermal.get('dtype')} ch={thermal.get('channels')} "
            f"bytes={thermal.get('total_bytes')}"
        )
        v = r.get("visible", {})
        vf = v.get("visible_format")
        if vf:
            print(
                f"C. Visible format    : {vf.get('width')}x{vf.get('height')} "
                f"{vf.get('dtype')} ch={vf.get('channels')} bytes={vf.get('total_bytes')}"
            )
        else:
            print("C. Visible format    : UNKNOWN / unavailable")
        print(f"D. Actual FPS        : {fps.get('average_fps')}")
        fss = fps.get("frame_interval_stats_s", {})
        if fss:
            print(
                f"E. Frame timing      : median={_fmt_ms(fss.get('median_s'))} "
                f"p95={_fmt_ms(fss.get('p95_s'))} min={_fmt_ms(fss.get('min_s'))} "
                f"max={_fmt_ms(fss.get('max_s'))}"
            )
        print(f"F. H/W timestamp     : {r.get('timestamps', {}).get('hardware_timestamp')}")
        seq = r.get("frame_sequence", {})
        print(f"G. Sequence          : {seq.get('hardware_frame_counter')} {seq.get('note', '')[:60]}")
        print(f"H. Ownership         : {r.get('halcon_numpy_ownership', {}).get('ownership', 'UNKNOWN')[:120]}")
        cp = r.get("copy_perf", {})
        for name in ("A_halcon_grab", "B_himage_as_numpy_array", "C_numpy_copy_owned", "D_numpy_to_shm_slot"):
            s = cp.get(name, {})
            if s.get("count"):
                print(
                    f"I. {name:30s}: median={_fmt_ms(s['median_s'])} "
                    f"p95={_fmt_ms(s['p95_s'])} max={_fmt_ms(s['max_s'])}"
                )
        to = r.get("timeout", {})
        print(f"J. Timeout           : code5322={to.get('code_5322_observed')} "
              f"usable_after={to.get('usable_after_timeout')} practical={to.get('practical_timeout_ms')}ms")
        nuc = r.get("nuc", {})
        print(f"K. NUC               : exposed={nuc.get('nuc_exposed')} result={str(nuc.get('result'))[:80]}")
        vfps = v.get("visible_fps", {})
        print(f"L. Visible result    : avail={v.get('visible_available')} fps={vfps.get('average_fps')}")
        net = r.get("network", {})
        print(f"M. Network           : lost={net.get('[Stream]GevStreamLostPacketCount')} "
              f"resend={net.get('[Stream]GevStreamResendPacketCount')}")
        rec = r.get("reconnect", {})
        print(f"N. Reconnect         : reopen_ok={rec.get('reopen_ok')} reacquire={rec.get('reacquire_ok')}")
        ring = r.get("ring_impact", {})
        print(f"O. Per-cam bandwidth : {_fmt_bw(ring.get('bytes_per_sec') or 0)}")
        proj = r.get("projection_8cam", {})
        for k, vrow in proj.items():
            if k == "assumptions":
                continue
            print(f"P. 8-cam {k:12s}: {vrow.get('human')}")
        ring32 = next((row for row in ring.get("ring_table", []) if row["depth"] == 32), None)
        if ring32:
            print(f"Q. Ring depth32      : 1cam={ring32['one_camera_mib']} MiB  8cam={ring32['eight_cameras_mib']} MiB")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="One-camera TV46L hardware characterization (V3 diagnostic)."
    )
    parser.add_argument(
        "--device",
        default=os.environ.get("TV46L_PROBE_DEVICE", DEFAULT_DEVICE),
        help="HALCON GigE Vision device identifier (default: %(default)s). "
        "Not hardcoded; pass the camera's identifier or 'default'.",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=DEFAULT_FRAMES,
        help=f"Frames for the measurement pass (default: %(default)s, min {MIN_FRAMES}).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=GRAB_TIMEOUT_MS,
        help="Grab timeout in ms for normal grabs (default: %(default)s).",
    )
    parser.add_argument(
        "--nuc",
        action="store_true",
        help="Execute the one-shot manual NUC (shutter) test.",
    )
    parser.add_argument(
        "--skip-visible",
        action="store_true",
        help="Skip the visible stream characterization.",
    )
    parser.add_argument(
        "--skip-reconnect",
        action="store_true",
        help="Skip the software close/reopen/reacquire test.",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available GigE Vision devices and exit.",
    )
    parser.add_argument(
        "--sample-dir",
        type=Path,
        default=REPO_ROOT / "temporary" / "tv46l_probe",
        help="Where to save the raw sample frame (default: temporary/tv46l_probe).",
    )
    parser.add_argument(
        "--out-root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root for reports/ and docs/ output (default: repo root).",
    )
    args = parser.parse_args(argv)

    if args.frames < 1:
        print("--frames must be >= 1")
        return 2
    if args.frames < MIN_FRAMES:
        print(
            f"WARNING: --frames {args.frames} < recommended {MIN_FRAMES}; "
            "copy-timing conclusions will be based on few frames."
        )

    if args.list_devices:
        probe = TV46LProbe(
            device=args.device,
            frames=args.frames,
            timeout_ms=args.timeout,
            run_nuc=False,
            run_visible=False,
            run_reconnect=False,
            sample_dir=args.sample_dir,
            out_root=args.out_root,
        )
        print("Discovering GigE Vision devices ...")
        try:
            probe._import_halcon()
        except ImportError as exc:
            print(f"HALCON runtime not available: {exc}")
            return 2
        devices = probe._list_devices()
        if not devices:
            print("No GigE Vision devices found.")
            return 3
        for d in devices:
            print(f"  device: {d}")
        return 0

    probe = TV46LProbe(
        device=args.device,
        frames=args.frames,
        timeout_ms=args.timeout,
        run_nuc=args.nuc,
        run_visible=not args.skip_visible,
        run_reconnect=not args.skip_reconnect,
        sample_dir=args.sample_dir,
        out_root=args.out_root,
    )
    return probe.run()


if __name__ == "__main__":
    sys.exit(main())
