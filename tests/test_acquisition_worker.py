"""Tests for the AcquisitionWorker: lifecycle, sequences, timestamps,
reconnection, frame publication, drop detection and shutdown.
"""

from __future__ import annotations

import threading
import time

import pytest

from thermal_monitor.camera.acquisition import AcquisitionWorker, InProcessLatestPublisher
from thermal_monitor.camera.driver import CameraGrabTimeout
from thermal_monitor.camera.model import AcquisitionState
from tests.conftest import (
    FakeFrameSource,
    FakePublisher,
    default_result,
    empty_result,
    make_camera_config,
)


def test_sequence_numbers_are_monotonic():
    source = FakeFrameSource()
    publisher = FakePublisher()
    worker = AcquisitionWorker(
        "cam_1",
        source,
        publisher,
        make_camera_config(),
    )
    worker.start()
    try:
        assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=5.0)
        deadline = time.monotonic() + 2.0
        while len(publisher.frames) < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        sequences = [f.descriptor.sequence for f in publisher.frames]
        assert len(sequences) >= 5
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)
    finally:
        worker.stop()


def test_timestamps_are_available():
    source = FakeFrameSource()
    publisher = FakePublisher()
    worker = AcquisitionWorker("cam_1", source, publisher, make_camera_config())
    worker.start()
    try:
        assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=5.0)
        deadline = time.monotonic() + 2.0
        while not publisher.frames and time.monotonic() < deadline:
            time.sleep(0.01)
        assert publisher.frames, "no frames published"
        frame = publisher.frames[0]
        assert frame.descriptor.timestamp > 0
        assert frame.descriptor.monotonic_timestamp > 0
        assert frame.descriptor.thermal.present is True
        assert frame.descriptor.thermal.width == 4
        assert frame.descriptor.thermal.height == 4
        assert frame.descriptor.sync.status.value == "missing_visible"
        assert frame.payload.thermal is not None
    finally:
        worker.stop()


def test_state_transitions_to_acquiring():
    source = FakeFrameSource()
    worker = AcquisitionWorker("cam_1", source, FakePublisher(), make_camera_config())
    assert worker.state is AcquisitionState.CREATED
    worker.start()
    try:
        assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=5.0)
        assert source.connect_calls == 1
    finally:
        worker.stop()
    assert worker.state in (AcquisitionState.STOPPED, AcquisitionState.STOPPING)


def test_clean_shutdown():
    source = FakeFrameSource()
    publisher = FakePublisher()
    worker = AcquisitionWorker("cam_1", source, publisher, make_camera_config())
    worker.start()
    assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=5.0)
    worker.stop(timeout=5.0)
    assert worker.state is AcquisitionState.STOPPED
    assert source.disconnect_calls == 1
    assert worker._thread is None or not worker._thread.is_alive()


def test_grab_failures_drive_degraded_then_reconnect():
    timeout = CameraGrabTimeout("timeout")
    source = FakeFrameSource(grab_script=[timeout, timeout, timeout, timeout, default_result()])
    publisher = FakePublisher()
    config = make_camera_config(consecutive_fail_limit=2, reconnect_interval_s=0.01)
    worker = AcquisitionWorker("cam_1", source, publisher, config)
    worker.start()
    try:
        # After the failure limit is reached the worker enters recovery and
        # reopens the source, then continues acquiring.
        assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=5.0)
        deadline = time.monotonic() + 3.0
        while source.reopen_calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert source.reopen_calls >= 1
        deadline = time.monotonic() + 3.0
        while not publisher.frames and time.monotonic() < deadline:
            time.sleep(0.01)
        assert publisher.frames, "no frames after recovery"
        stats = worker.stats()
        assert stats.reconnect_count >= 1
    finally:
        worker.stop()


def test_reconnection_exhaustion_ends_in_error():
    timeout = CameraGrabTimeout("timeout")
    source = FakeFrameSource(always_raise=timeout, reopen_error=RuntimeError("cannot reopen"))
    config = make_camera_config(
        consecutive_fail_limit=1,
        reconnect_interval_s=0.01,
        max_reconnect_attempts=2,
    )
    worker = AcquisitionWorker("cam_1", source, FakePublisher(), config)
    worker.start()
    try:
        assert worker.wait_for_state(AcquisitionState.ERROR, timeout=5.0)
        stats = worker.stats()
        assert stats.state is AcquisitionState.ERROR
        assert stats.last_error is not None
    finally:
        worker.stop()
    assert not worker._thread or not worker._thread.is_alive()


def test_connection_failure_goes_to_error_or_recovery():
    source = FakeFrameSource(connect_error=RuntimeError("camera not found"))
    config = make_camera_config(reconnect_interval_s=0.01)
    worker = AcquisitionWorker("cam_1", source, FakePublisher(), config)
    worker.start()
    try:
        # Connection keeps failing; worker is in recovery/error states, never
        # acquiring.  It must not crash and must remain stoppable.
        assert not worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=1.0)
        assert worker.state in (AcquisitionState.RECONNECTING, AcquisitionState.ERROR)
    finally:
        worker.stop()


def test_frame_publication_delivers_frames():
    source = FakeFrameSource()
    publisher = FakePublisher()
    worker = AcquisitionWorker("cam_1", source, publisher, make_camera_config())
    worker.start()
    try:
        assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=5.0)
        deadline = time.monotonic() + 2.0
        while len(publisher.frames) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(publisher.frames) >= 3
        stats = worker.stats()
        assert stats.total_acquired >= 3
        assert stats.published >= 3
    finally:
        worker.stop()


def test_invalid_frame_handling():
    empty = empty_result()
    source = FakeFrameSource(default=empty)
    publisher = FakePublisher()
    config = make_camera_config(consecutive_fail_limit=100)
    worker = AcquisitionWorker("cam_1", source, publisher, config)
    worker.start()
    try:
        assert worker.wait_for_state(AcquisitionState.DEGRADED, timeout=5.0)
        # An empty frame must not be published.
        assert not publisher.frames
        assert worker.stats().last_error is not None
    finally:
        worker.stop()


def test_worker_accepts_driver_publisher_and_inprocess_publisher():
    """The worker must work with any FramePublisher implementation."""
    source = FakeFrameSource()
    inprocess = InProcessLatestPublisher()
    worker = AcquisitionWorker("cam_1", source, inprocess, make_camera_config())
    worker.start()
    try:
        assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=5.0)
        deadline = time.monotonic() + 2.0
        latest = None
        while latest is None and time.monotonic() < deadline:
            latest = inprocess.latest()
            time.sleep(0.01)
        assert latest is not None
        # InProcessLatestPublisher no longer has stats(); verify via worker stats
        stats = worker.stats()
        assert stats.published >= 1
    finally:
        worker.stop()


def test_two_workers_are_independent():
    source_a, source_b = FakeFrameSource(), FakeFrameSource()
    pub_a, pub_b = FakePublisher(), FakePublisher()
    worker_a = AcquisitionWorker("cam_a", source_a, pub_a, make_camera_config())
    worker_b = AcquisitionWorker("cam_b", source_b, pub_b, make_camera_config())
    worker_a.start()
    worker_b.start()
    try:
        assert worker_a.wait_for_state(AcquisitionState.ACQUIRING, timeout=5.0)
        assert worker_b.wait_for_state(AcquisitionState.ACQUIRING, timeout=5.0)
        worker_a.stop()
        # Camera A stopping must not affect camera B.
        deadline = time.monotonic() + 2.0
        while len(pub_b.frames) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert worker_b.state is AcquisitionState.ACQUIRING
        assert len(pub_b.frames) >= 3
    finally:
        worker_b.stop()


def test_fps_is_measured():
    source = FakeFrameSource()
    publisher = FakePublisher()
    worker = AcquisitionWorker("cam_1", source, publisher, make_camera_config())
    worker.start()
    try:
        assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=5.0)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            time.sleep(0.05)
        stats = worker.stats()
        assert stats.current_fps >= 0
        assert stats.average_fps >= 0
    finally:
        worker.stop()


def test_stop_returns_promptly_even_while_running():
    source = FakeFrameSource()
    worker = AcquisitionWorker("cam_1", source, FakePublisher(), make_camera_config())
    worker.start()
    assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=5.0)
    started = time.monotonic()
    worker.stop(timeout=2.0)
    assert time.monotonic() - started < 2.0
    assert worker.state is AcquisitionState.STOPPED


def test_worker_lifecycle_stop_disconnect_reconnect_acquire():
    """Regression test for HALCON access violation fix.

    Verifies the strict lifecycle:
    1. acquisition running
    2. stop requested
    3. worker exits (thread joined)
    4. driver disconnect called
    5. reconnect (new worker start)
    6. acquisition resumes

    This ensures no concurrent grab/close on the same framegrabber handle.
    """
    source = FakeFrameSource()
    publisher = FakePublisher()
    config = make_camera_config()

    # Phase 1: acquisition running
    worker = AcquisitionWorker("cam_1", source, publisher, config)
    worker.start()
    assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=5.0)

    deadline = time.monotonic() + 2.0
    while len(publisher.frames) < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    frames_before = len(publisher.frames)
    assert frames_before >= 5

    # Phase 2-4: stop requested -> worker exits -> driver disconnect
    worker.stop(timeout=5.0)
    assert worker.state is AcquisitionState.STOPPED
    assert worker._thread is None or not worker._thread.is_alive()
    assert source.disconnect_calls == 1

    # Phase 5-6: reconnect -> acquisition resumes
    worker.start()
    assert worker.wait_for_state(AcquisitionState.ACQUIRING, timeout=5.0)

    deadline = time.monotonic() + 2.0
    while len(publisher.frames) < frames_before + 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    frames_after = len(publisher.frames)
    assert frames_after > frames_before, "No frames acquired after restart"

    worker.stop(timeout=5.0)
    assert worker.state is AcquisitionState.STOPPED
    assert source.disconnect_calls == 2
    assert source.connect_calls == 2