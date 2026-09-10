"""YUYV-to-RGB conversion for TV46L visible-light display (Stage 8E).

Pure NumPy BT.601 conversion of the raw (H, W*2) uint8 YUYV plane produced
by the custom GVSP path. No OpenCV dependency (V3 does not vendor it).
Math matches the proven standalone integer fallback.
"""

from __future__ import annotations

import numpy as np


def yuyv_to_rgb(yuyv: np.ndarray) -> np.ndarray:
    """Convert a raw YUYV plane to a contiguous (H, W, 3) uint8 RGB image.

    Args:
        yuyv: 2-D uint8 array ``(H, W*2)`` with byte order
            ``Y0 U Y1 V`` per pixel pair (TV46L packing).

    Raises:
        ValueError: if the input is not 2-D uint8 with an even width.
    """
    arr = np.asarray(yuyv)
    if arr.ndim != 2 or arr.dtype != np.uint8 or arr.shape[1] % 2 != 0:
        raise ValueError(f"YUYV plane must be 2-D uint8 with even width, got {arr.shape} {arr.dtype}")
    height, double_width = arr.shape
    width = double_width // 2
    # One pixel pair = 4 bytes (Y0 U Y1 V); expand to per-pixel planes.
    quads = arr.reshape(height, width // 2, 4)
    y = np.empty((height, width), dtype=np.int32)
    y[:, 0::2] = quads[..., 0]
    y[:, 1::2] = quads[..., 2]
    u = np.repeat(quads[..., 1].astype(np.int32) - 128, 2, axis=1)
    v = np.repeat(quads[..., 3].astype(np.int32) - 128, 2, axis=1)
    c = y - 16
    r = (298 * c + 409 * v + 128) >> 8
    g = (298 * c - 100 * u - 208 * v + 128) >> 8
    b = (298 * c + 516 * u + 128) >> 8
    rgb = np.empty((height, width, 3), dtype=np.uint8)
    rgb[:, :, 0] = np.clip(r, 0, 255)
    rgb[:, :, 1] = np.clip(g, 0, 255)
    rgb[:, :, 2] = np.clip(b, 0, 255)
    return np.ascontiguousarray(rgb)


__all__ = ["yuyv_to_rgb"]
