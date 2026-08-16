"""
core.frame -- V3 shared frame contract (ADR-002).

The frame is the immutable representation of acquired camera data that is
passed between acquisition, processing, storage, offline and UI.  It must
not contain processing results or GUI state.

The contract deliberately separates:

    Frame
    +-- FrameDescriptor   (metadata, identity, payload references)
    +-- FramePayload      (bulk pixel data)

so that a future shared-memory transport can keep the descriptor in-process
and store only the payload bytes in a ring buffer, without changing the
frame model consumers see.

In this temporary in-process representation the payload is backed by NumPy
arrays that the acquisition layer marks read-only.  The shared-memory
transport (ADR-003) will replace the payload storage, not the contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping

import numpy as np


class SyncStatus(str, Enum):
    """Relationship between the thermal and visible payloads of one frame."""

    SYNCHRONIZED = "synchronized"
    ACCEPTABLE = "acceptable"
    DEGRADED = "degraded"
    MISSING_THERMAL = "missing_thermal"
    MISSING_VISIBLE = "missing_visible"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class StreamMetadata:
    """Metadata describing one payload stream (thermal or visible).

    ``present`` tells consumers whether this payload was acquired.  The
    remaining fields are None when the information is not available.
    """

    present: bool = False
    width: int | None = None
    height: int | None = None
    pixel_format: str | None = None
    bits_per_channel: int | None = None
    dtype: str | None = None
    byte_count: int | None = None
    sequence: int | None = None
    timestamp: float | None = None
    monotonic_timestamp: float | None = None
    hardware_timestamp: float | None = None


@dataclass(frozen=True, slots=True)
class SyncInfo:
    """IR/visible synchronization information for one frame."""

    status: SyncStatus
    time_delta: float | None = None


@dataclass(frozen=True, slots=True)
class FrameDescriptor:
    """Frame identity and metadata.  No bulk pixel data lives here.

    ``payload`` references the matching :class:`FramePayload`.  In the
    shared-memory design this reference will point into the ring buffer
    instead of an in-process object.

    ``metadata`` is an immutable mapping to ensure the descriptor is truly
    immutable for shared-memory transport.
    """

    camera_id: str
    sequence: int
    timestamp: float
    monotonic_timestamp: float
    thermal: StreamMetadata
    visible: StreamMetadata
    sync: SyncInfo
    metadata: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class FramePayload:
    """Bulk pixel data.  Acquired arrays are immutable (read-only).

    The constructor validates that any provided arrays have writeable=False.
    This enforces the frame contract at the contract boundary, not relying
    solely on the driver.
    """

    thermal: np.ndarray | None = None
    visible: np.ndarray | None = None

    def __post_init__(self) -> None:
        for name, arr in (("thermal", self.thermal), ("visible", self.visible)):
            if arr is not None and arr.flags.writeable:
                raise ValueError(f"FramePayload.{name} must be read-only (writeable=False)")


@dataclass(frozen=True, slots=True)
class Frame:
    """A complete acquired frame: metadata plus payload."""

    descriptor: FrameDescriptor
    payload: FramePayload