"""Tests for the V3 shared frame contract (core.frame, ADR-002)."""

from __future__ import annotations

import numpy as np
from types import MappingProxyType

from thermal_monitor.core.frame import (
    Frame,
    FrameDescriptor,
    FramePayload,
    StreamMetadata,
    SyncInfo,
    SyncStatus,
)


def test_frame_separates_descriptor_from_payload():
    thermal = np.zeros((4, 4), dtype=np.uint16)
    thermal.setflags(write=False)
    meta = StreamMetadata(
        present=True,
        width=4,
        height=4,
        pixel_format="IR_Data",
        dtype="uint16",
        byte_count=thermal.nbytes,
    )
    descriptor = FrameDescriptor(
        camera_id="cam_1",
        sequence=7,
        timestamp=1234.5,
        monotonic_timestamp=999.0,
        thermal=meta,
        visible=StreamMetadata(present=False),
        sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
    )
    payload = FramePayload(thermal=thermal, visible=None)
    frame = Frame(descriptor=descriptor, payload=payload)

    assert frame.descriptor.camera_id == "cam_1"
    assert frame.descriptor.sequence == 7
    assert frame.payload.thermal is thermal
    assert frame.payload.visible is None
    assert frame.descriptor.thermal.present is True
    assert frame.descriptor.visible.present is False


def test_frame_metadata_does_not_hold_bulk_data():
    """The descriptor's thermal/visible fields are metadata, not pixel arrays."""
    thermal = np.zeros((64, 64), dtype=np.uint16)
    thermal.setflags(write=False)
    descriptor = FrameDescriptor(
        camera_id="cam_1",
        sequence=1,
        timestamp=1.0,
        monotonic_timestamp=1.0,
        thermal=StreamMetadata(present=True, byte_count=thermal.nbytes),
        visible=StreamMetadata(present=False),
        sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
    )
    payload = FramePayload(thermal=thermal)
    # Descriptor carries only metadata objects, never the pixel array.
    assert isinstance(descriptor.thermal, StreamMetadata)
    assert not isinstance(descriptor.thermal, np.ndarray)
    assert descriptor.thermal.byte_count == thermal.nbytes
    # The payload alone owns the pixel data.
    assert payload.thermal is thermal
    assert descriptor.metadata == MappingProxyType({})


def test_payload_arrays_are_read_only():
    thermal = np.arange(16, dtype=np.uint16).reshape(4, 4)
    thermal.setflags(write=False)
    payload = FramePayload(thermal=thermal)
    assert not payload.thermal.flags.writeable


def test_payload_rejects_writeable_arrays():
    thermal = np.arange(16, dtype=np.uint16).reshape(4, 4)
    # Not setting writeable=False should raise
    try:
        FramePayload(thermal=thermal)
        assert False, "Expected ValueError for writeable array"
    except ValueError as e:
        assert "must be read-only" in str(e)


def test_sync_status_values_match_adr002():
    expected = {
        SyncStatus.SYNCHRONIZED,
        SyncStatus.ACCEPTABLE,
        SyncStatus.DEGRADED,
        SyncStatus.MISSING_THERMAL,
        SyncStatus.MISSING_VISIBLE,
        SyncStatus.UNKNOWN,
    }
    assert set(SyncStatus) == expected


def test_frame_is_frozen():
    thermal = np.zeros((2, 2), dtype=np.uint16)
    thermal.setflags(write=False)
    descriptor = FrameDescriptor(
        camera_id="cam_1",
        sequence=1,
        timestamp=1.0,
        monotonic_timestamp=1.0,
        thermal=StreamMetadata(present=True),
        visible=StreamMetadata(present=False),
        sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
    )
    frame = Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal))
    assert frame.descriptor.sequence == 1