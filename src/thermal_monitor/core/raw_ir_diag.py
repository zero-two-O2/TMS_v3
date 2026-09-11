"""
core.raw_ir_diag -- raw Mono16 diagnostic capture (TMS_RAW_IR_DIAG).

Temporary distortion-localization aid: dumps the raw IR payload *before*
calibration/temperature/palette/rendering together with the converted
temperature sidecar, so a distorted display can be classified as::

    raw IR bad, temperature bad   -> acquisition / GVSP side
    raw IR good, temperature bad  -> calibration / processing side
    both good, display bad        -> render / UI side

Filenames carry camera id, worker sequence and hardware frame id so dumps
correlate 1:1 with integrity CRC samples and latency tracks. Never raises:
capture must not disturb acquisition or processing.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

DIAG_ENV_VAR = "TMS_RAW_IR_DIAG"
DIR_ENV_VAR = "TMS_RAW_IR_DIR"
EVERY_ENV_VAR = "TMS_RAW_IR_EVERY"
DEFAULT_DIR = "recordings/raw_diag"
DEFAULT_EVERY = 30

_TRUE_VALUES = {"1", "true", "yes", "on"}

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def raw_diag_enabled(override: bool | None = None) -> bool:
    """Whether raw capture is enabled (explicit flag wins over env)."""
    if override is not None:
        return bool(override)
    return os.environ.get(DIAG_ENV_VAR, "").strip().lower() in _TRUE_VALUES


def raw_diag_dir(override: str | Path | None = None) -> Path:
    """Capture directory (created on first dump)."""
    if override is not None:
        return Path(override)
    return Path(os.environ.get(DIR_ENV_VAR, DEFAULT_DIR))


def raw_diag_every(override: int | None = None) -> int:
    """Dump one frame per N worker sequences (minimum 1)."""
    if override is not None:
        try:
            return max(1, int(override))
        except (TypeError, ValueError):
            return DEFAULT_EVERY
    try:
        return max(1, int(os.environ.get(EVERY_ENV_VAR, str(DEFAULT_EVERY))))
    except (TypeError, ValueError):
        return DEFAULT_EVERY


def safe_camera_name(camera_id: str) -> str:
    """Filesystem-safe camera token (display identity is unchanged)."""
    return _SAFE_NAME.sub("_", str(camera_id))[:64] or "cam"


def maybe_dump_raw(
    camera_id: str,
    sequence: int,
    hw_frame_id: int | None,
    thermal: np.ndarray | None,
    temperature: np.ndarray | None = None,
    *,
    enabled: bool | None = None,
    every: int | None = None,
    directory: str | Path | None = None,
) -> Path | None:
    """Dump one raw IR frame (+ optional temperature sidecar).

    Returns the IR dump path, or None when disabled, off-cadence, or when
    no thermal payload is available. Never raises.
    """
    try:
        if not raw_diag_enabled(enabled):
            return None
        cadence = raw_diag_every(every)
        if cadence > 1 and (sequence % cadence) != 0:
            return None
        if thermal is None:
            return None
        out_dir = raw_diag_dir(directory)
        out_dir.mkdir(parents=True, exist_ok=True)
        hw = hw_frame_id if hw_frame_id is not None else -1
        stem = "{}_seq{:06d}_hw{}".format(safe_camera_name(camera_id), sequence, hw)
        ir_path = out_dir / (stem + "_ir.npy")
        np.save(str(ir_path), np.ascontiguousarray(thermal))
        if temperature is not None:
            np.save(str(out_dir / (stem + "_temp.npy")), np.ascontiguousarray(temperature))
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("raw IR dump cam=%s seq=%d hw=%s path=%s", camera_id, sequence, hw, ir_path)
        return ir_path
    except Exception:
        logger.warning("raw IR dump failed cam=%s seq=%s", camera_id, sequence, exc_info=True)
        return None


__all__ = [
    "DEFAULT_DIR",
    "DEFAULT_EVERY",
    "DIR_ENV_VAR",
    "DIAG_ENV_VAR",
    "EVERY_ENV_VAR",
    "maybe_dump_raw",
    "raw_diag_dir",
    "raw_diag_enabled",
    "raw_diag_every",
    "safe_camera_name",
]
