"""
core.frame_latency -- acquisition-to-display latency diagnostics.

Temporary triage instrumentation behind ``TMS_FRAME_LATENCY_DIAG=1``
(cadence ``TMS_FRAME_LATENCY_EVERY``, default every 10th worker sequence).
Production overhead when disabled is one boolean check per hook.

For every sampled frame a timestamp is recorded at each pipeline stage::

    grab -> published -> consumed -> process_start -> process_end
        -> emitted -> ui_received -> render_start -> render_done -> displayed

plus, for every published frame (cheap, no storage growth), the newest
worker sequence / hardware id / timestamp, so any display site can compute

    display_age   = now - acquisition timestamp of the displayed frame
    behind_frames = latest published sequence - displayed sequence

All clocks are :func:`time.perf_counter_ns` on this machine (all stages run
in-process). This deliberately matches the existing acquisition clocks:
``GrabResult.grab_started`` and ``FrameDescriptor.monotonic_timestamp`` are
both ``time.perf_counter`` seconds, so differences are directly comparable.
(``time.monotonic_ns`` must NOT be mixed in: on Windows it uses a different
epoch than ``perf_counter_ns``.) Thermal uses the camera id as stream key;
VL uses ``"<camera_id>#vl"``.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict, deque

logger = logging.getLogger(__name__)

DIAG_ENV_VAR = "TMS_FRAME_LATENCY_DIAG"
EVERY_ENV_VAR = "TMS_FRAME_LATENCY_EVERY"
DEFAULT_SAMPLE_EVERY = 10
MAX_SAMPLED_FRAMES = 2048
MAX_AGE_SAMPLES = 2048

_TRUE_VALUES = {"1", "true", "yes", "on"}

#: Pipeline stages in order. ``grab`` is the blocking-call start (T0) and
#: is NOT part of display latency; ``frame_complete`` (T1) is the latency
#: origin: earliest point the complete frame is available to the app.
STAGES = (
    "grab",
    "frame_complete",
    "published",
    "consumed",
    "process_start",
    "process_end",
    "emitted",
    "ui_received",
    "render_start",
    "render_done",
    "displayed",
)

#: (segment label, start stage, end stage) for the Part B breakdown.
#: H is blocking wait (NOT display latency); A..G start at frame_complete.
SEGMENTS = (
    ("H_grab_wait", "grab", "frame_complete"),
    ("A_acq_to_shm", "frame_complete", "published"),
    ("B_shm_to_consumer", "published", "consumed"),
    ("C_processing", "process_start", "process_end"),
    ("D_process_to_ui", "emitted", "ui_received"),
    ("E_ui_to_render", "ui_received", "render_start"),
    ("F_render", "render_start", "render_done"),
    ("G_acq_to_display", "frame_complete", "displayed"),
)


def latency_enabled(override: bool | None = None) -> bool:
    """Whether latency sampling is enabled (explicit flag wins over env)."""
    if override is not None:
        return bool(override)
    return os.environ.get(DIAG_ENV_VAR, "").strip().lower() in _TRUE_VALUES


def latency_sample_every(override: int | None = None) -> int:
    """Sample one frame per N worker sequences (minimum 1)."""
    if override is not None:
        try:
            return max(1, int(override))
        except (TypeError, ValueError):
            return DEFAULT_SAMPLE_EVERY
    try:
        return max(1, int(os.environ.get(EVERY_ENV_VAR, str(DEFAULT_SAMPLE_EVERY))))
    except (TypeError, ValueError):
        return DEFAULT_SAMPLE_EVERY


def _percentile(sorted_ms: list[float], pct: float) -> float | None:
    if not sorted_ms:
        return None
    if len(sorted_ms) == 1:
        return sorted_ms[0]
    rank = (pct / 100.0) * (len(sorted_ms) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_ms) - 1)
    frac = rank - low
    return sorted_ms[low] + (sorted_ms[high] - sorted_ms[low]) * frac


def _describe_ms(samples_ms: list[float]) -> dict:
    ordered = sorted(samples_ms)
    n = len(ordered)
    return {
        "n": n,
        "min_ms": ordered[0] if n else None,
        "median_ms": _percentile(ordered, 50),
        "p95_ms": _percentile(ordered, 95),
        "p99_ms": _percentile(ordered, 99),
        "max_ms": ordered[-1] if n else None,
        "mean_ms": (sum(ordered) / n) if n else None,
        "latest_ms": samples_ms[-1] if samples_ms else None,
    }


class FrameLatencyTracker:
    """Thread-safe store for per-stage monotonic timestamps and age stats."""

    def __init__(
        self,
        max_sampled_frames: int = MAX_SAMPLED_FRAMES,
        max_age_samples: int = MAX_AGE_SAMPLES,
    ) -> None:
        self._max_frames = max(1, max_sampled_frames)
        self._max_age = max(1, max_age_samples)
        self._lock = threading.Lock()
        # stream -> OrderedDict[sequence, {"hw": int|None, "stages": {stage: ns}}]
        self._frames: dict[str, OrderedDict[int, dict]] = {}
        # stream -> {"seq": int, "hw": int|None, "ns": int, "count": int}
        self._latest: dict[str, dict] = {}
        # stream -> deque of display-age ms
        self._ages_ms: dict[str, deque[float]] = {}
        # stream -> deque of behind-frames
        self._behind: dict[str, deque[int]] = {}
        # stream -> deque of hw frame-id deltas (latest_acquired - displayed)
        self._hw_delta: dict[str, deque[int]] = {}
        # stream -> last displayed sequence (regression detection)
        self._last_displayed: dict[str, int] = {}
        # stream -> last displayed hw frame id
        self._last_displayed_hw: dict[str, int | None] = {}
        # stream -> regression count
        self._regressions: dict[str, int] = {}

    # -- producers ------------------------------------------------------

    def note_published(
        self,
        stream: str,
        sequence: int,
        hw_frame_id: int | None,
        grab_start_ns: int,
        frame_complete_ns: int,
        published_mono_ns: int,
        sample_every: int = 1,
    ) -> None:
        """Record stages grab / frame_complete / published.

        ``grab_start_ns`` is T0 (blocking-call start, wait metric only).
        ``frame_complete_ns`` is T1 (latency origin). Always tracks newest;
        stores per-frame detail per cadence.
        """
        with self._lock:
            latest = self._latest.get(stream)
            if latest is None or sequence >= latest["seq"]:
                self._latest[stream] = {
                    "seq": sequence,
                    "hw": hw_frame_id,
                    "ns": published_mono_ns,
                    "count": (latest["count"] + 1) if latest else 1,
                }
            if sample_every > 1 and (sequence % sample_every) != 0:
                return
            frames = self._frames.setdefault(stream, OrderedDict())
            frames[sequence] = {
                "hw": hw_frame_id,
                "stages": {
                    "grab": grab_start_ns,
                    "frame_complete": frame_complete_ns,
                    "published": published_mono_ns,
                },
            }
            while len(frames) > self._max_frames:
                frames.popitem(last=False)

    def note_stage(
        self, stream: str, sequence: int, stage: str, mono_ns: int
    ) -> None:
        """Record one downstream stage for a sampled frame (no-op if unsampled)."""
        with self._lock:
            frames = self._frames.get(stream)
            if not frames:
                return
            entry = frames.get(sequence)
            if entry is None:
                return
            entry["stages"][stage] = mono_ns

    def note_displayed(
        self,
        stream: str,
        sequence: int,
        hw_frame_id: int | None,
        acq_mono_ns: int | None,
        displayed_mono_ns: int,
    ) -> None:
        """Record final display; updates age/behind/hw-delta, flags regressions.

        ``acq_mono_ns`` must be T_frame_complete (NOT grab-call start).
        Captures latest-acquired hw id alongside the displayed hw id so the
        newest-frame correlation is directly measurable.
        """
        with self._lock:
            frames = self._frames.get(stream)
            if frames is not None:
                entry = frames.get(sequence)
                if entry is not None:
                    entry["stages"]["displayed"] = displayed_mono_ns
            last = self._last_displayed.get(stream)
            if last is not None and sequence < last:
                self._regressions[stream] = self._regressions.get(stream, 0) + 1
                logger.error(
                    "FRAME LATENCY REGRESSION stream=%s displayed_seq=%d hw=%s "
                    "after_seq=%d (older frame shown after newer)",
                    stream,
                    sequence,
                    hw_frame_id,
                    last,
                )
            if last is None or sequence > last:
                self._last_displayed[stream] = sequence
                self._last_displayed_hw[stream] = hw_frame_id
            age_ms = None
            if acq_mono_ns is not None:
                age_ms = (displayed_mono_ns - acq_mono_ns) / 1e6
                ages = self._ages_ms.setdefault(stream, deque(maxlen=self._max_age))
                ages.append(age_ms)
            latest = self._latest.get(stream)
            behind = None
            hw_delta = None
            if latest is not None:
                behind = max(0, latest["seq"] - sequence)
                self._behind.setdefault(stream, deque(maxlen=self._max_age)).append(behind)
                if hw_frame_id is not None and latest["hw"] is not None:
                    try:
                        hw_delta = int(latest["hw"]) - int(hw_frame_id)
                    except (TypeError, ValueError):
                        hw_delta = None
                if hw_delta is not None:
                    self._hw_delta.setdefault(stream, deque(maxlen=self._max_age)).append(
                        max(0, hw_delta)
                    )
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "FRAME LATENCY DISPLAY stream=%s seq=%d hw=%s latest_hw=%s "
                    "age_ms=%s behind=%s hw_delta=%s",
                    stream,
                    sequence,
                    hw_frame_id,
                    latest["hw"] if latest else None,
                    ("%.1f" % age_ms) if age_ms is not None else "?",
                    behind if behind is not None else "?",
                    hw_delta if hw_delta is not None else "?",
                )

    # -- reporting --------------------------------------------------------

    def latest_published(self, stream: str) -> dict | None:
        """Newest published (seq, hw, ns, count) snapshot, or None."""
        with self._lock:
            known = self._latest.get(stream)
            return dict(known) if known else None

    def summary(self, stream: str) -> dict:
        """Segment/age/behind statistics for one stream (plain data)."""
        with self._lock:
            frames = self._frames.get(stream, OrderedDict())
            segments: dict[str, dict] = {}
            for label, start, end in SEGMENTS:
                samples_ms = [
                    (entry["stages"][end] - entry["stages"][start]) / 1e6
                    for entry in frames.values()
                    if start in entry["stages"] and end in entry["stages"]
                ]
                segments[label] = _describe_ms(samples_ms)
            ages = self._ages_ms.get(stream, deque())
            behind_list = list(self._behind.get(stream, deque()))
            hw_delta_list = list(self._hw_delta.get(stream, deque()))
            latest = self._latest.get(stream)
            return {
                "stream": stream,
                "sampled_frames": len(frames),
                "published_total": latest["count"] if latest else 0,
                "latest_seq": latest["seq"] if latest else None,
                "latest_hw": latest["hw"] if latest else None,
                "last_displayed_seq": self._last_displayed.get(stream),
                "last_displayed_hw": self._last_displayed_hw.get(stream),
                "regressions": self._regressions.get(stream, 0),
                "segments_ms": segments,
                "display_age_ms": _describe_ms(list(ages)),
                "behind_frames": _describe_ms([float(v) for v in behind_list])
                if behind_list
                else _describe_ms([]),
                "hw_frame_delta": _describe_ms([float(v) for v in hw_delta_list])
                if hw_delta_list
                else _describe_ms([]),
            }

    def reset(self, stream: str | None = None) -> None:
        """Clear samples and counters (one stream, or all)."""
        with self._lock:
            if stream is None:
                self._frames.clear()
                self._latest.clear()
                self._ages_ms.clear()
                self._behind.clear()
                self._hw_delta.clear()
                self._last_displayed.clear()
                self._last_displayed_hw.clear()
                self._regressions.clear()
                return
            self._frames.pop(stream, None)
            self._latest.pop(stream, None)
            self._ages_ms.pop(stream, None)
            self._behind.pop(stream, None)
            self._hw_delta.pop(stream, None)
            self._last_displayed.pop(stream, None)
            self._last_displayed_hw.pop(stream, None)
            self._regressions.pop(stream, None)


_DEFAULT_TRACKER = FrameLatencyTracker()


def get_default_tracker() -> FrameLatencyTracker:
    """Process-global tracker used when a component is not given one."""
    return _DEFAULT_TRACKER


def format_summary_brief(summary: dict) -> str:
    """One-line human-readable rendering of :meth:`FrameLatencyTracker.summary`."""
    parts = [
        "stream={}".format(summary.get("stream")),
        "sampled={}".format(summary.get("sampled_frames")),
        "published_total={}".format(summary.get("published_total")),
        "regressions={}".format(summary.get("regressions")),
    ]
    for label, stats in (summary.get("segments_ms") or {}).items():
        if stats.get("n"):
            parts.append(
                "{}_ms(n={} med={:.1f} p95={:.1f} max={:.1f})".format(
                    label,
                    stats["n"],
                    stats["median_ms"] or 0.0,
                    stats["p95_ms"] or 0.0,
                    stats["max_ms"] or 0.0,
                )
            )
    age = summary.get("display_age_ms") or {}
    if age.get("n"):
        parts.append(
            "display_age_ms(n={} med={:.1f} p95={:.1f} max={:.1f})".format(
                age["n"], age["median_ms"] or 0.0, age["p95_ms"] or 0.0, age["max_ms"] or 0.0
            )
        )
    behind = summary.get("behind_frames") or {}
    if behind.get("n"):
        parts.append(
            "behind_frames(n={} med={:.1f} p95={:.1f} max={:.1f})".format(
                behind["n"],
                behind["median_ms"] or 0.0,
                behind["p95_ms"] or 0.0,
                behind["max_ms"] or 0.0,
            )
        )
    hw_delta = summary.get("hw_frame_delta") or {}
    if hw_delta.get("n"):
        parts.append(
            "hw_frame_delta(n={} med={:.1f} p95={:.1f} max={:.1f})".format(
                hw_delta["n"],
                hw_delta["median_ms"] or 0.0,
                hw_delta["p95_ms"] or 0.0,
                hw_delta["max_ms"] or 0.0,
            )
        )
    return " ".join(parts)


__all__ = [
    "DEFAULT_SAMPLE_EVERY",
    "DIAG_ENV_VAR",
    "EVERY_ENV_VAR",
    "STAGES",
    "SEGMENTS",
    "FrameLatencyTracker",
    "format_summary_brief",
    "get_default_tracker",
    "latency_enabled",
    "latency_sample_every",
]
