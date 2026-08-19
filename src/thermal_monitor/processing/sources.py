"""
processing.sources -- FrameSource implementations for live and synthetic sources.

The OfflineFrameSource has been moved to thermal_monitor.offline.source for
proper architectural separation:
- storage owns recording persistence
- offline owns reading/replay of recordings
- processing owns processing algorithms
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Optional

import numpy as np

from thermal_monitor.core.frame import Frame, FrameDescriptor, FramePayload, StreamMetadata, SyncInfo, SyncStatus
from thermal_monitor.processing.pipeline import FrameSource


class LiveFrameSource:
    """FrameSource that wraps the live acquisition publisher.

    This connects the acquisition worker's publisher to the processing pipeline.
    """

    def __init__(self, camera_id: str, publisher) -> None:
        self._camera_id = camera_id
        self._publisher = publisher

    def get_next_frame(self) -> Frame | None:
        # For live, we typically use latest frame
        return self._publisher.latest()

    def get_latest_frame(self) -> Frame | None:
        return self._publisher.latest()

    def seek(self, sequence: int) -> bool:
        # Live sources don't support seeking
        return False

    @property
    def camera_id(self) -> str:
        return self._camera_id

    @property
    def is_live(self) -> bool:
        return True


class SyntheticFrameSource:
    """Synthetic frame source for testing.

    Generates deterministic synthetic frames with configurable thermal patterns.
    """

    def __init__(
        self,
        camera_id: str = "synthetic_cam",
        thermal_shape: tuple[int, int] = (480, 640),
        thermal_dtype: np.dtype = np.uint16,
        include_visible: bool = False,
        frame_rate: float = 9.0,
    ) -> None:
        self._camera_id = camera_id
        self._thermal_shape = thermal_shape
        self._thermal_dtype = thermal_dtype
        self._include_visible = include_visible
        self._frame_rate = frame_rate
        self._sequence = 0
        self._timestamp = 0.0
        self._frame_interval = 1.0 / frame_rate

    def get_next_frame(self) -> Frame:
        frame = self._generate_frame()
        self._sequence += 1
        self._timestamp += self._frame_interval
        return frame

    def get_latest_frame(self) -> Frame:
        return self._generate_frame()

    def seek(self, sequence: int) -> bool:
        # For synthetic, we can just set sequence
        self._sequence = sequence
        self._timestamp = sequence * self._frame_interval
        return True

    def _generate_frame(self) -> Frame:
        # Generate synthetic thermal data with a hot spot
        thermal = np.zeros(self._thermal_shape, dtype=self._thermal_dtype)
        # Add a gradient
        for y in range(self._thermal_shape[0]):
            thermal[y, :] = y * 100
        # Add a hot spot in the center
        cy, cx = self._thermal_shape[0] // 2, self._thermal_shape[1] // 2
        for dy in range(-20, 21):
            for dx in range(-20, 21):
                y, x = cy + dy, cx + dx
                if 0 <= y < self._thermal_shape[0] and 0 <= x < self._thermal_shape[1]:
                    dist = (dy ** 2 + dx ** 2) ** 0.5
                    if dist < 20:
                        thermal[y, x] = min(65535, int(30000 + (1 - dist/20) * 30000))

        thermal.setflags(write=False)

        visible = None
        if self._include_visible:
            visible = np.zeros((*self._thermal_shape, 3), dtype=np.uint8)
            visible.setflags(write=False)

        thermal_meta = StreamMetadata(
            present=True,
            width=self._thermal_shape[1],
            height=self._thermal_shape[0],
            pixel_format="IR_Data",
            bits_per_channel=16,
            dtype=str(self._thermal_dtype),
            byte_count=thermal.nbytes,
            sequence=self._sequence,
            timestamp=self._timestamp,
            monotonic_timestamp=self._timestamp,
        )
        visible_meta = StreamMetadata(
            present=visible is not None,
            width=self._thermal_shape[1] if visible is not None else None,
            height=self._thermal_shape[0] if visible is not None else None,
        )
        sync = SyncInfo(status=SyncStatus.SYNCHRONIZED if visible is not None else SyncStatus.MISSING_VISIBLE)

        descriptor = FrameDescriptor(
            camera_id=self._camera_id,
            sequence=self._sequence,
            timestamp=self._timestamp,
            monotonic_timestamp=self._timestamp,
            thermal=thermal_meta,
            visible=visible_meta,
            sync=sync,
        )

        return Frame(
            descriptor=descriptor,
            payload=FramePayload(thermal=thermal, visible=visible),
        )

    @property
    def camera_id(self) -> str:
        return self._camera_id

    @property
    def is_live(self) -> bool:
        return False  # Synthetic is not live hardware