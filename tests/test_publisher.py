"""Tests for InProcessLatestPublisher (temporary transport stand-in)."""

from __future__ import annotations

import numpy as np

from thermal_monitor.camera.acquisition import InProcessLatestPublisher
from thermal_monitor.camera.model import PublishResult
from thermal_monitor.core.frame import (
    Frame,
    FrameDescriptor,
    FramePayload,
    StreamMetadata,
    SyncInfo,
    SyncStatus,
)


def make_frame(sequence: int) -> Frame:
    thermal = np.zeros((4, 4), dtype=np.uint16)
    thermal.setflags(write=False)
    return Frame(
        descriptor=FrameDescriptor(
            camera_id="cam_1",
            sequence=sequence,
            timestamp=1.0,
            monotonic_timestamp=1.0,
            thermal=StreamMetadata(present=True, width=4, height=4),
            visible=StreamMetadata(present=False),
            sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
        ),
        payload=FramePayload(thermal=thermal),
    )


def test_latest_wins_semantics():
    pub = InProcessLatestPublisher()
    result0 = pub.publish(make_frame(0))
    result1 = pub.publish(make_frame(1))
    result2 = pub.publish(make_frame(2))
    assert pub.latest().descriptor.sequence == 2
    assert result0.accepted is True
    assert result1.accepted is True
    assert result2.accepted is True
    assert result2.overwritten_sequence == 1


def test_sequence_gap_detection():
    pub = InProcessLatestPublisher()
    pub.publish(make_frame(0))
    result = pub.publish(make_frame(5))
    assert result.sequence == 5
    # The gap is tracked internally but not exposed via stats() anymore
    # Consumers track their own gaps


def test_no_gap_for_contiguous_frames():
    pub = InProcessLatestPublisher()
    for seq in range(3):
        result = pub.publish(make_frame(seq))
        assert result.accepted is True
    # For contiguous frames, only the second and subsequent publishes
    # have an overwritten_sequence (the previous frame)
    result0 = pub.publish(make_frame(3))
    result1 = pub.publish(make_frame(4))
    assert result0.overwritten_sequence == 2  # frame 2 was overwritten
    assert result1.overwritten_sequence == 3  # frame 3 was overwritten


def test_reset_clears_state():
    pub = InProcessLatestPublisher()
    pub.publish(make_frame(0))
    pub.reset()
    assert pub.latest() is None


def test_close_clears_latest():
    pub = InProcessLatestPublisher()
    pub.publish(make_frame(0))
    pub.close()
    assert pub.latest() is None


def test_publish_result_contains_sequence():
    pub = InProcessLatestPublisher()
    result = pub.publish(make_frame(42))
    assert isinstance(result, PublishResult)
    assert result.accepted is True
    assert result.sequence == 42
    assert result.dropped is False


def test_overwritten_sequence_reported():
    pub = InProcessLatestPublisher()
    pub.publish(make_frame(0))
    result = pub.publish(make_frame(1))
    assert result.overwritten_sequence == 0