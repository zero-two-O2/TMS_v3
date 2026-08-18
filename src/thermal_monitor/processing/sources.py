"""
processing.sources -- FrameSource implementations for live, offline, and synthetic sources.
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


@dataclass
class OfflineFrameSource:
    """FrameSource for offline playback of recorded frames.

    Loads frames from a recording and provides sequential or random access.
    """

    camera_id: str
    frames: list[Frame]
    _index: int = 0

    @classmethod
    def from_recording_file(cls, file_path: str) -> OfflineFrameSource:
        """Load frames from a recording file.

        Args:
            file_path: Path to the recording file.

        Returns:
            OfflineFrameSource with loaded frames.
        """
        import pickle
        import json

        frames = []
        with open(file_path, "rb") as f:
            # Read header
            header_len = int.from_bytes(f.read(4), "little")
            header_json = f.read(header_len).decode("utf-8")
            header = json.loads(header_json)

            # Verify magic
            if header.get("magic") != "TMS3REC":
                raise ValueError("Invalid recording file format")

            # Read frames
            while True:
                frame_len_bytes = f.read(4)
                if not frame_len_bytes:
                    break
                frame_len = int.from_bytes(frame_len_bytes, "little")
                frame_data = f.read(frame_len)
                if not frame_data:
                    break

                frame_dict = pickle.loads(frame_data)
                frame = cls._deserialize_frame(frame_dict)
                frames.append(frame)

        if frames:
            camera_id = frames[0].descriptor.camera_id
        else:
            camera_id = header.get("metadata", {}).get("camera_id", "unknown")

        return cls(camera_id=camera_id, frames=frames)

    @staticmethod
    def _deserialize_frame(frame_dict: dict) -> Frame:
        """Deserialize a frame from the recording format."""
        import numpy as np

        desc_data = frame_dict["descriptor"]
        payload_data = frame_dict["payload"]

        # Reconstruct StreamMetadata
        thermal_meta = StreamMetadata(
            present=desc_data["thermal"]["present"],
            width=desc_data["thermal"]["width"],
            height=desc_data["thermal"]["height"],
            pixel_format=desc_data["thermal"]["pixel_format"],
            bits_per_channel=desc_data["thermal"]["bits_per_channel"],
            dtype=desc_data["thermal"]["dtype"],
            byte_count=desc_data["thermal"]["byte_count"],
            sequence=desc_data["thermal"]["sequence"],
            timestamp=desc_data["thermal"]["timestamp"],
            monotonic_timestamp=desc_data["thermal"]["monotonic_timestamp"],
            hardware_timestamp=desc_data["thermal"]["hardware_timestamp"],
        )
        visible_meta = StreamMetadata(
            present=desc_data["visible"]["present"],
            width=desc_data["visible"]["width"],
            height=desc_data["visible"]["height"],
            pixel_format=desc_data["visible"]["pixel_format"],
            bits_per_channel=desc_data["visible"]["bits_per_channel"],
            dtype=desc_data["visible"]["dtype"],
            byte_count=desc_data["visible"]["byte_count"],
            sequence=desc_data["visible"]["sequence"],
            timestamp=desc_data["visible"]["timestamp"],
            monotonic_timestamp=desc_data["visible"]["monotonic_timestamp"],
            hardware_timestamp=desc_data["visible"]["hardware_timestamp"],
        )
        sync = SyncInfo(
            status=SyncStatus(desc_data["sync"]["status"]),
            time_delta=desc_data["sync"]["time_delta"],
        )

        descriptor = FrameDescriptor(
            camera_id=desc_data["camera_id"],
            sequence=desc_data["sequence"],
            timestamp=desc_data["timestamp"],
            monotonic_timestamp=desc_data["monotonic_timestamp"],
            thermal=thermal_meta,
            visible=visible_meta,
            sync=sync,
            metadata=desc_data.get("metadata", {}),
        )

        # Reconstruct payload
        thermal = None
        if frame_dict["thermal_bytes"]:
            thermal = np.frombuffer(
                frame_dict["thermal_bytes"],
                dtype=np.dtype(payload_data["thermal_dtype"])
            ).reshape(payload_data["thermal_shape"])
            thermal.setflags(write=False)

        visible = None
        if frame_dict["visible_bytes"]:
            visible = np.frombuffer(
                frame_dict["visible_bytes"],
                dtype=np.dtype(payload_data["visible_dtype"])
            ).reshape(payload_data["visible_shape"])
            visible.setflags(write=False)

        return Frame(
            descriptor=descriptor,
            payload=FramePayload(thermal=thermal, visible=visible),
        )

    def get_next_frame(self) -> Frame | None:
        if self._index < len(self.frames):
            frame = self.frames[self._index]
            self._index += 1
            return frame
        return None

    def get_latest_frame(self) -> Frame | None:
        if self.frames:
            return self.frames[-1]
        return None

    def seek(self, sequence: int) -> bool:
        # Find frame with matching sequence
        for i, frame in enumerate(self.frames):
            if frame.descriptor.sequence == sequence:
                self._index = i
                return True
        return False

    def seek_to_index(self, index: int) -> bool:
        if 0 <= index < len(self.frames):
            self._index = index
            return True
        return False

    @property
    def is_live(self) -> bool:
        return False

    def __len__(self) -> int:
        return len(self.frames)

    def __iter__(self) -> Iterator[Frame]:
        return iter(self.frames)

    def reset(self) -> None:
        self._index = 0


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