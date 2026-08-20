"""
services.recording_consumer -- Recording consumer for shared-memory ring to RecordingWriter.

Consumes frames from a SharedMemoryRingBuffer and writes them to the Stage 5C
RecordingWriter format. Runs independently of the acquisition producer in its
own thread. Tracks all drop categories separately.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from thermal_monitor.core.frame import Frame, FrameDescriptor, FramePayload, StreamMetadata, SyncInfo, SyncStatus
from thermal_monitor.core.shm import Consumer, SharedMemoryRingBuffer, RingConfig, PayloadSpec
from thermal_monitor.storage.recording import RecordingWriteMetadata, RecordingWriter

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RecordingConsumerStats:
    """Statistics for the recording consumer."""
    frames_consumed: int = 0
    frames_written: int = 0
    ring_overwritten: int = 0
    ring_gaps: int = 0
    ring_stale: int = 0
    ring_invalid: int = 0
    writer_dropped: int = 0
    last_sequence: int = -1
    first_hardware_frame_id: int | None = None
    last_hardware_frame_id: int | None = None
    first_timestamp: float | None = None
    last_timestamp: float | None = None

    def copy(self) -> "RecordingConsumerStats":
        """Return a copy of the current stats."""
        return RecordingConsumerStats(
            frames_consumed=self.frames_consumed,
            frames_written=self.frames_written,
            ring_overwritten=self.ring_overwritten,
            ring_gaps=self.ring_gaps,
            ring_stale=self.ring_stale,
            ring_invalid=self.ring_invalid,
            writer_dropped=self.writer_dropped,
            last_sequence=self.last_sequence,
            first_hardware_frame_id=self.first_hardware_frame_id,
            last_hardware_frame_id=self.last_hardware_frame_id,
            first_timestamp=self.first_timestamp,
            last_timestamp=self.last_timestamp,
        )


@dataclass(slots=True)
class RecordingConsumer:
    """Consumes frames from a shared-memory ring and writes to RecordingWriter.

    Runs in its own thread. Does not block the acquisition producer.
    """

    camera_id: str
    ring_buffer: SharedMemoryRingBuffer
    consumer_name: str
    output_dir: Path
    recording_metadata: RecordingWriteMetadata
    ring_depth: int = 32
    chunk_target_bytes: int = 64 * 1024 * 1024  # 64 MiB

    _consumer: Consumer = field(init=False, repr=False)
    _writer: RecordingWriter = field(init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _stats: RecordingConsumerStats = field(default_factory=RecordingConsumerStats, init=False, repr=False)
    _stats_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _recording_started: bool = field(default=False, init=False, repr=False)
    _base_time: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._consumer = self.ring_buffer.consumer(self.consumer_name)
        self._writer = RecordingWriter(
            self.output_dir,
            self.recording_metadata,
            chunk_target_bytes=self.chunk_target_bytes,
        )

    def start(self) -> None:
        """Start the recording consumer thread."""
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("RecordingConsumer already running")

        self._writer.open()
        self._recording_started = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"RecordingConsumer-{self.camera_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info("RecordingConsumer started for camera %s", self.camera_id)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the recording consumer and finalize the recording."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("Camera %s: recording consumer thread did not stop within %.1f s", self.camera_id, timeout)
        self._thread = None
        self._finalize_recording()
        logger.info("RecordingConsumer stopped for camera %s", self.camera_id)

    def abort(self) -> None:
        """Abort the recording without finalizing (crash recovery test)."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        if self._recording_started:
            self._writer.abort()
        self._recording_started = False
        logger.info("RecordingConsumer aborted for camera %s", self.camera_id)

    def stats(self) -> RecordingConsumerStats:
        """Get current consumer statistics."""
        with self._stats_lock:
            return self._stats.copy()

    def _run(self) -> None:
        """Main consumer loop."""
        logger.debug("RecordingConsumer %s: entering run loop", self.camera_id)

        # Use next_pinned for sequential consumption with automatic pin management
        expected_sequence = 0
        first_frame = True

        while not self._stop_event.is_set():
            try:
                # Try to get the next expected frame
                pinned_view = self._consumer.next_pinned(expected_sequence)

                if pinned_view is not None:
                    # Successfully got a frame
                    frame_view = pinned_view.view
                    frame = self._frame_view_to_frame(frame_view)

                    # Write to recording
                    try:
                        self._writer.write_frame(frame)
                        with self._stats_lock:
                            self._stats.frames_written += 1
                            self._stats.last_sequence = frame.descriptor.sequence
                            if self._stats.first_timestamp is None:
                                self._stats.first_timestamp = frame.descriptor.timestamp
                            self._stats.last_timestamp = frame.descriptor.timestamp

                            # Track hardware frame_id from thermal stream metadata
                            hw_id = frame.descriptor.thermal.sequence
                            if hw_id is not None:
                                if self._stats.first_hardware_frame_id is None:
                                    self._stats.first_hardware_frame_id = hw_id
                                self._stats.last_hardware_frame_id = hw_id

                    except Exception as exc:
                        logger.error("Camera %s: failed to write frame: %s", self.camera_id, exc)
                        with self._stats_lock:
                            self._stats.writer_dropped += 1

                    with self._stats_lock:
                        self._stats.frames_consumed += 1

                    expected_sequence = frame.descriptor.sequence + 1
                    first_frame = False

                    # Release the pinned view
                    try:
                        self._consumer.release(pinned_view)
                    except Exception as exc:
                        logger.warning("Camera %s: failed to release pinned view: %s", self.camera_id, exc)

                else:
                    # Frame not available - check consumer stats for drops
                    consumer_stats = self._consumer.stats()
                    with self._stats_lock:
                        if consumer_stats.overwritten > self._stats.ring_overwritten:
                            self._stats.ring_overwritten = consumer_stats.overwritten
                        if consumer_stats.gaps > self._stats.ring_gaps:
                            self._stats.ring_gaps = consumer_stats.gaps
                        if consumer_stats.stale > self._stats.ring_stale:
                            self._stats.ring_stale = consumer_stats.stale
                        if consumer_stats.invalid > self._stats.ring_invalid:
                            self._stats.ring_invalid = consumer_stats.invalid

                    # If we haven't seen any frames yet, try latest_pinned() to catch up
                    if first_frame:
                        latest_pinned = self._consumer.latest_pinned()
                        if latest_pinned is not None:
                            frame_view = latest_pinned.view
                            frame = self._frame_view_to_frame(frame_view)

                            try:
                                self._writer.write_frame(frame)
                                with self._stats_lock:
                                    self._stats.frames_written += 1
                                    self._stats.last_sequence = frame.descriptor.sequence
                                    if self._stats.first_timestamp is None:
                                        self._stats.first_timestamp = frame.descriptor.timestamp
                                    self._stats.last_timestamp = frame.descriptor.timestamp

                                    hw_id = frame.descriptor.thermal.sequence
                                    if hw_id is not None:
                                        if self._stats.first_hardware_frame_id is None:
                                            self._stats.first_hardware_frame_id = hw_id
                                        self._stats.last_hardware_frame_id = hw_id
                            except Exception as exc:
                                logger.error("Camera %s: failed to write frame: %s", self.camera_id, exc)
                                with self._stats_lock:
                                    self._stats.writer_dropped += 1

                            with self._stats_lock:
                                self._stats.frames_consumed += 1

                            expected_sequence = frame.descriptor.sequence + 1
                            first_frame = False

                            try:
                                self._consumer.release(latest_pinned)
                            except Exception as exc:
                                logger.warning("Camera %s: failed to release pinned view: %s", self.camera_id, exc)

                    # Small sleep to avoid busy-waiting when frames not yet available
                    time.sleep(0.001)

            except Exception as exc:
                logger.exception("Camera %s: consumer loop error: %s", self.camera_id, exc)
                time.sleep(0.1)  # Back off on error

        logger.debug("RecordingConsumer %s: run loop exited", self.camera_id)

    def wait_for_frames(self, count: int, timeout: float = 5.0) -> bool:
        """Wait until at least `count` frames have been consumed."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._stats_lock:
                if self._stats.frames_consumed >= count:
                    return True
            time.sleep(0.01)
        return False

    def _finalize_recording(self) -> None:
        """Finalize the recording."""
        if self._recording_started:
            try:
                self._writer.finalize()
                logger.info("Camera %s: recording finalized at %s", self.camera_id, self._writer.recording_dir)
            except Exception as exc:
                logger.error("Camera %s: failed to finalize recording: %s", self.camera_id, exc)
            finally:
                self._recording_started = False

    def _frame_view_to_frame(self, view) -> Frame:
        """Convert a FrameView to a Frame for RecordingWriter.

        The FrameView already has the correct descriptor and payload structure.
        We need to ensure it matches the Frame contract exactly.
        """
        # The FrameView.descriptor is already a FrameDescriptor
        # The FrameView.payload is already a FramePayload
        # But we need to reconstruct a Frame with the same data

        descriptor = view.descriptor
        payload = view.payload

        # Ensure thermal metadata has hardware_timestamp
        thermal_meta = descriptor.thermal
        if thermal_meta.present and thermal_meta.hardware_timestamp is None:
            # Try to get hardware_timestamp from metadata if available
            hw_ts = descriptor.metadata.get("hardware_timestamp")
            if hw_ts is not None:
                thermal_meta = StreamMetadata(
                    present=thermal_meta.present,
                    width=thermal_meta.width,
                    height=thermal_meta.height,
                    pixel_format=thermal_meta.pixel_format,
                    bits_per_channel=thermal_meta.bits_per_channel,
                    dtype=thermal_meta.dtype,
                    byte_count=thermal_meta.byte_count,
                    sequence=thermal_meta.sequence,
                    timestamp=thermal_meta.timestamp,
                    monotonic_timestamp=thermal_meta.monotonic_timestamp,
                    hardware_timestamp=hw_ts,
                )

        # Reconstruct frame with possibly updated thermal metadata
        new_descriptor = FrameDescriptor(
            camera_id=descriptor.camera_id,
            sequence=descriptor.sequence,
            timestamp=descriptor.timestamp,
            monotonic_timestamp=descriptor.monotonic_timestamp,
            thermal=thermal_meta,
            visible=descriptor.visible,
            sync=descriptor.sync,
            metadata=descriptor.metadata,
        )

        return Frame(descriptor=new_descriptor, payload=payload)

    def close(self) -> None:
        """Clean up resources."""
        if self._thread is not None and self._thread.is_alive():
            self.stop(timeout=2.0)
        try:
            self._consumer.close()
        except Exception:
            pass
        try:
            # Don't close ring_buffer - it's owned by the producer
            pass
        except Exception:
            pass


def create_recording_consumer(
    camera_id: str,
    output_dir: Path,
    recording_metadata: RecordingWriteMetadata,
    ring_depth: int = 32,
    chunk_target_bytes: int = 64 * 1024 * 1024,
    thermal_width: int = 640,
    thermal_height: int = 480,
    thermal_dtype: np.dtype = np.dtype(np.uint16),
) -> tuple[SharedMemoryRingBuffer, RecordingConsumer]:
    """Factory to attach to existing ring buffer and create RecordingConsumer.

    This is the consumer-side factory. The ring buffer must already exist
    (created by the producer via create_ring_buffer_and_publisher).

    Args:
        camera_id: Camera identifier
        output_dir: Directory for recording output
        recording_metadata: Metadata for the recording
        ring_depth: Ring buffer depth (must match producer)
        chunk_target_bytes: Target chunk size for RecordingWriter
        thermal_width: Thermal frame width
        thermal_height: Thermal frame height
        thermal_dtype: Thermal frame dtype

    Returns:
        Tuple of (ring_buffer, recording_consumer). The ring_buffer must be
        closed by the caller when all consumers are done.
    """
    config = RingConfig(
        camera_id=camera_id,
        thermal_spec=PayloadSpec(
            width=thermal_width,
            height=thermal_height,
            dtype=thermal_dtype,
            bytes_per_frame=thermal_width * thermal_height * thermal_dtype.itemsize,
        ),
        visible_spec=None,  # IR-only for now
        depth=ring_depth,
    )

    ring = SharedMemoryRingBuffer.attach(config)
    consumer = RecordingConsumer(
        camera_id=camera_id,
        ring_buffer=ring,
        consumer_name=f"recorder_{camera_id}",
        output_dir=output_dir,
        recording_metadata=recording_metadata,
        ring_depth=ring_depth,
        chunk_target_bytes=chunk_target_bytes,
    )

    return ring, consumer


__all__ = [
    "RecordingConsumer",
    "RecordingConsumerStats",
    "create_recording_consumer",
]