"""
camera.acquisition -- acquisition orchestration for one camera.

This module owns the continuous acquisition loop, sequence numbers,
timestamps, FPS measurement, dropped-frame detection, recovery scheduling
and graceful shutdown.  It does not touch hardware directly; it drives a
:class:`~thermal_monitor.camera.source.FrameSource` and publishes immutable
:class:`~thermal_monitor.core.frame.Frame` objects to a
:class:`FramePublisher`.

The publisher is the future shared-memory boundary.  The acquisition loop
never assumes:

* that every published frame is consumed;
* that there is a single consumer;
* that consumers keep up with the acquisition rate;
* that frames may be mutated downstream.

A temporary in-process latest-frame publisher
(:class:`InProcessLatestPublisher`) is provided for development and tests.
It will be replaced by a shared-memory ring buffer (ADR-003) without
changing the worker.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from thermal_monitor.camera.source import (
    CameraConnectionError,
    CameraGrabError,
    CameraGrabTimeout,
    FrameSource,
)
from thermal_monitor.camera.model import (
    AcquisitionState,
    AcquisitionStats,
    CameraConfig,
    CameraValidationResult,
    PublishResult,
)
from thermal_monitor.core.frame import Frame, FrameDescriptor, FramePayload, StreamMetadata, SyncInfo, SyncStatus
from thermal_monitor.core.frame_integrity import (
    FrameIntegrityRegistry,
    IntegrityStats,
    compute_thermal_crc,
    get_default_registry,
    integrity_diag_enabled,
    integrity_sample_every as _resolve_sample_every,
)
from thermal_monitor.core.frame_latency import (
    FrameLatencyTracker,
    format_summary_brief as _format_latency_brief,
    get_default_tracker as _default_latency_tracker,
    latency_enabled as _latency_enabled,
    latency_sample_every as _resolve_latency_every,
)

logger = logging.getLogger(__name__)


class FramePublisher(Protocol):
    """Publication boundary between acquisition and its consumers.

    A future shared-memory transport must implement this protocol so the
    acquisition worker does not need to change when the transport is
    replaced.

    The protocol returns a :class:`~thermal_monitor.camera.model.PublishResult`
    so the worker gets immediate publication feedback without a separate
    stats() call.  This is critical for the shared-memory ring buffer where
    the publish operation itself determines acceptance/dropping/overwriting.
    """

    def publish(self, frame: Frame) -> PublishResult:
        """Publish a frame and return the publication result.

        Returns a PublishResult with accepted=True when the frame was
        accepted by the transport, accepted=False when dropped (e.g. buffer
        full).  The sequence and any overwritten sequence are included.
        """
        ...

    def latest(self) -> Frame | None:
        """Return the most recently accepted frame, or None.

        NOTE: This method is a development convenience for the temporary
        InProcessLatestPublisher.  A shared-memory ring buffer will not
        expose a "latest frame" as a Python object; instead, consumers will
        acquire a read-only view into the appropriate ring buffer slot.
        """
        ...

    def reset(self) -> None:
        """Clear stored frames and counters."""
        ...

    def close(self) -> None:
        """Release transport resources.  Not reusable afterwards."""
        ...


class InProcessLatestPublisher:
    """Temporary in-process latest-frame publisher (DEVELOPMENT STAND-IN).

    Keeps only the newest frame (latest-wins).  Every publish is accepted;
    frames replaced before being consumed count as overwritten.  Sequence
    gaps are detected when a published sequence is not exactly one more
    than the previous accepted sequence.

    This is a development/test stand-in for the future shared-memory ring
    buffer and must not be used as the production transport.

    IMPORTANT: This publisher exposes a `latest()` method returning a Python
    Frame object.  The real shared-memory ring buffer will NOT have this
    method; consumers will acquire read-only views into ring buffer slots.
    """

    def __init__(self) -> None:
        self._latest: Frame | None = None
        self._lock = threading.Lock()
        self._published_count = 0
        self._overwritten_count = 0
        self._sequence_gaps = 0
        self._latest_sequence: int | None = None

    def publish(self, frame: Frame) -> PublishResult:
        with self._lock:
            overwritten_sequence = None
            if self._latest is not None:
                self._overwritten_count += 1
                overwritten_sequence = self._latest.descriptor.sequence
            if self._latest_sequence is not None:
                gap = frame.descriptor.sequence - self._latest_sequence - 1
                if gap > 0:
                    self._sequence_gaps += gap
            self._latest = frame
            self._published_count += 1
            self._latest_sequence = frame.descriptor.sequence
        return PublishResult(
            accepted=True,
            sequence=frame.descriptor.sequence,
            dropped=False,
            overwritten_sequence=overwritten_sequence,
        )

    def latest(self) -> Frame | None:
        with self._lock:
            return self._latest

    def reset(self) -> None:
        with self._lock:
            self._latest = None
            self._published_count = 0
            self._overwritten_count = 0
            self._sequence_gaps = 0
            self._latest_sequence = None

    def close(self) -> None:
        with self._lock:
            self._latest = None


class AcquisitionWorker:
    """Runs continuous acquisition for exactly one camera.

    Each worker owns its own thread and its own :class:`FrameSource` and
    :class:`FramePublisher`, so one camera failing (or being reconfigured
    or stopped) never affects another camera's worker.

    Lifecycle:
        DISCOVERED -> CONNECTING -> CONTROL_READY -> STREAM_CONFIGURED
        -> FUSION_READY -> STREAMING

        STREAMING -> DISCONNECTED -> RECONNECTING -> CONTROL_READY -> ...

        Any state -> FAILED (max retries exhausted)
        Any state -> STOPPING -> STOPPED
    """

    def __init__(
        self,
        camera_id: str,
        source: FrameSource,
        publisher: FramePublisher,
        config: CameraConfig,
        integrity_diag: bool | None = None,
        integrity_sample_every: int | None = None,
        integrity_registry: FrameIntegrityRegistry | None = None,
    ) -> None:
        self._camera_id = camera_id
        self._source = source
        self._publisher = publisher
        self._config = config
        # Frame-integrity triage (diagnostic-only, off unless enabled).
        self._integrity_diag = integrity_diag_enabled(integrity_diag)
        self._integrity_every = _resolve_sample_every(integrity_sample_every)
        self._integrity = integrity_registry or get_default_registry()
        # Acquisition-to-display latency triage (diagnostic-only, off unless
        # enabled via TMS_FRAME_LATENCY_DIAG; one boolean check per frame).
        self._latency_diag = _latency_enabled()
        self._latency_every = _resolve_latency_every()
        self._latency = _default_latency_tracker()

        self._state = AcquisitionState.DISCOVERED
        self._state_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # Counters
        self._stats_lock = threading.Lock()
        self._total_acquired = 0
        self._published = 0
        self._dropped = 0
        self._sequence_gaps = 0
        self._consecutive_failures = 0
        self._reconnect_count = 0
        self._sequence = 0
        self._last_grab_duration = 0.0
        self._publish_times: deque[float] = deque(maxlen=1024)
        self._start_time = time.perf_counter()
        self._last_error: str | None = None

        # Acquisition statistics (packet-level from the GVSP receiver)
        self._frames_received = 0
        self._packets_lost = 0
        self._blocks_incomplete = 0
        self._prev_packet_stats: dict[str, int] | None = None

    # ------------------------------------------------------------------
    # Public control
    # ------------------------------------------------------------------

    def start(self) -> None:
        with self._state_lock:
            if self._state not in (
                AcquisitionState.DISCOVERED,
                AcquisitionState.STOPPED,
                AcquisitionState.FAILED,
            ):
                raise RuntimeError(f"Worker is in state {self._state.value}; cannot start")
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("Worker thread already running")
            self._state = AcquisitionState.CONNECTING
        self._stop_event.clear()
        logger.info(
            "FRAME INTEGRITY DIAG camera=%s enabled=%s every=%d",
            self._camera_id,
            self._integrity_diag,
            self._integrity_every,
        )
        logger.info(
            "FRAME LATENCY DIAG camera=%s enabled=%s every=%d",
            self._camera_id,
            self._latency_diag,
            self._latency_every,
        )
        self._thread = threading.Thread(
            target=self._run,
            name=f"Acquisition-{self._camera_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._set_state(AcquisitionState.STOPPING)
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("Camera %s: acquisition thread did not stop within %.1f s", self._camera_id, timeout)
        self._thread = None

    def wait_for_state(self, state: AcquisitionState, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state is state:
                return True
            time.sleep(0.01)
        return self.state is state

    @property
    def state(self) -> AcquisitionState:
        with self._state_lock:
            return self._state

    def stats(self) -> AcquisitionStats:
        with self._state_lock:
            state = self._state
        with self._stats_lock:
            now = time.perf_counter()
            elapsed = max(now - self._start_time, 1e-9)
            current_fps = self._compute_fps(now)
            return AcquisitionStats(
                state=state,
                total_acquired=self._total_acquired,
                published=self._published,
                dropped=self._dropped,
                sequence_gaps=self._sequence_gaps,
                consecutive_failures=self._consecutive_failures,
                reconnect_count=self._reconnect_count,
                last_grab_duration_s=self._last_grab_duration,
                current_fps=current_fps,
                average_fps=self._published / elapsed,
                last_error=self._last_error,
                frames_received=self._frames_received,
                packets_lost=self._packets_lost,
                blocks_incomplete=self._blocks_incomplete,
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _set_state(self, state: AcquisitionState) -> None:
        with self._state_lock:
            if self._state is not state:
                logger.debug("Camera %s: state %s -> %s", self._camera_id, self._state.value, state.value)
                self._state = state

    def _run(self) -> None:
        logger.info("Camera %s: acquisition worker starting", self._camera_id)
        try:
            if not self._connect_with_retries():
                return
            if not self._validate_camera():
                return
            self._set_state(AcquisitionState.STREAMING)
            self._acquisition_loop()
        except Exception:
            logger.exception("Camera %s: unexpected acquisition failure", self._camera_id)
            self._set_state(AcquisitionState.FAILED)
        finally:
            self._shutdown()
        logger.info("Camera %s: acquisition worker stopped", self._camera_id)

    def _connect_with_retries(self) -> bool:
        attempt = 0
        while not self._stop_event.is_set():
            try:
                self._source.connect()
                self._set_state(AcquisitionState.CONTROL_READY)
                return True
            except CameraConnectionError as exc:
                self._record_error(exc)
                self._set_state(AcquisitionState.RECONNECTING)
                attempt += 1
                if not self._sleep_reconnect(attempt):
                    return False
            except Exception as exc:
                self._record_error(exc)
                self._set_state(AcquisitionState.FAILED)
                return False
        return False

    def _validate_camera(self) -> bool:
        """Validate camera registers before declaring STREAMING.

        Checks SCDA == expected IP, SCP != 0, and Fusion register == 3.
        Transitions through CONTROL_READY → STREAM_CONFIGURED → FUSION_READY.
        Returns False if any check fails (camera goes to FAILED).
        """
        expected_scda = self._config.ip_address
        expected_fusion = 3

        # CONTROL_READY: verify SCDA and SCP
        validate_registers = getattr(self._source, "validate_registers", None)
        if validate_registers is None:
            logger.debug(
                "Camera %s: source does not expose register validation; "
                "skipping hardware-only checks",
                self._camera_id,
            )
            self._set_state(AcquisitionState.STREAM_CONFIGURED)
            self._set_state(AcquisitionState.FUSION_READY)
            return True

        result = validate_registers(
            expected_scda_ip=expected_scda,
            expected_fusion_value=expected_fusion,
        )
        if result is None:
            logger.debug(
                "Camera %s: source returned no register validation; "
                "skipping hardware-only checks",
                self._camera_id,
            )
            self._set_state(AcquisitionState.STREAM_CONFIGURED)
            self._set_state(AcquisitionState.FUSION_READY)
            return True

        for check in result.checks:
            if not check.passed:
                logger.warning(
                    "Camera %s: register validation failed — %s: expected=%s actual=%s",
                    self._camera_id,
                    check.name,
                    check.expected,
                    check.actual,
                )

        if result.scda_ok and result.scp_ok:
            self._set_state(AcquisitionState.STREAM_CONFIGURED)
        else:
            self._record_error(
                f"Register validation failed: SCDA={result.scda_ok} SCP={result.scp_ok}"
            )
            self._set_state(AcquisitionState.FAILED)
            return False

        if result.fusion_ok:
            self._set_state(AcquisitionState.FUSION_READY)
        else:
            self._record_error(
                f"Register validation failed: FUSION={result.fusion_ok}"
            )
            self._set_state(AcquisitionState.FAILED)
            return False

        logger.info(
            "Camera %s: register validation passed (SCDA=%s SCP=%s FUSION=%s)",
            self._camera_id,
            result.scda_ok,
            result.scp_ok,
            result.fusion_ok,
        )
        return True

    def _acquisition_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._state is AcquisitionState.RECONNECTING:
                if not self._try_reconnect():
                    return
                continue

            try:
                result = self._source.grab(self._config.grab_timeout_ms)
            except CameraGrabTimeout as exc:
                self._handle_grab_failure(exc)
                continue
            except CameraGrabError as exc:
                self._handle_grab_failure(exc)
                continue
            except CameraConnectionError as exc:
                self._record_error(exc)
                self._set_state(AcquisitionState.DISCONNECTED)
                self._set_state(AcquisitionState.RECONNECTING)
                continue
            except Exception as exc:
                self._record_error(exc)
                self._set_state(AcquisitionState.DISCONNECTED)
                self._set_state(AcquisitionState.RECONNECTING)
                continue

            frame = self._build_frame(result)
            if frame is None:
                self._record_error("grab returned no usable payload")
                self._handle_grab_failure(RuntimeError("invalid frame: no thermal and no visible payload"))
                continue

            self._publish_frame(frame)

    def _handle_grab_failure(self, exc: Exception) -> None:
        self._record_error(exc)
        with self._stats_lock:
            self._consecutive_failures += 1
            failed = self._consecutive_failures
        self._set_state(AcquisitionState.DEGRADED)
        if failed >= self._config.consecutive_fail_limit:
            logger.warning(
                "Camera %s: %d consecutive grab failures; entering recovery",
                self._camera_id,
                failed,
            )
            self._set_state(AcquisitionState.DISCONNECTED)
            self._set_state(AcquisitionState.RECONNECTING)

    def _try_reconnect(self) -> bool:
        attempt = 1
        while not self._stop_event.is_set():
            if not self._sleep_reconnect(attempt):
                return False
            try:
                logger.info("Camera %s: reopening framegrabber (attempt %d)", self._camera_id, attempt)
                self._source.reopen()
            except Exception as exc:
                self._record_error(exc)
                attempt += 1
                if attempt > self._config.max_reconnect_attempts:
                    self._set_state(AcquisitionState.FAILED)
                    return False
                continue
            with self._stats_lock:
                self._consecutive_failures = 0
                self._reconnect_count += 1
                self._prev_packet_stats = None
            self._set_state(AcquisitionState.CONTROL_READY)
            if not self._validate_camera():
                self._set_state(AcquisitionState.RECONNECTING)
                attempt += 1
                continue
            self._set_state(AcquisitionState.STREAMING)
            return True
        return False

    def _sleep_reconnect(self, attempt: int) -> bool:
        backoff = self._config.reconnect_interval_s * (self._config.reconnect_backoff_factor ** (attempt - 1))
        deadline = time.monotonic() + backoff
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return False
            time.sleep(min(0.1, max(deadline - time.monotonic(), 0.0)))
        return True

    def _build_frame(self, result) -> Frame | None:
        thermal = result.thermal
        visible = result.visible
        if thermal is None and visible is None:
            return None

        # Wall-clock timestamp for persistent recording (epoch time)
        now_wall = time.time()
        # Monotonic timestamp for relative timing within this run
        now_mono = time.perf_counter()

        # Per-stream timestamps: use grab completion as the acquisition timestamp
        # for each stream. If hardware timestamp is available, use it.
        thermal_timestamp = now_wall
        thermal_mono = now_mono
        if result.hardware_timestamp is not None:
            thermal_timestamp = result.hardware_timestamp

        visible_timestamp = None
        visible_mono = None
        if visible is not None:
            visible_timestamp = now_wall
            visible_mono = now_mono

        # Determine thermal stream sequence from hardware frame_id if available,
        # otherwise fall back to acquisition worker's internal sequence.
        thermal_sequence = None
        if result.frame_id is not None:
            thermal_sequence = result.frame_id

        # Visible correlation: a non-None visible plane arrives in
        # the SAME GrabResult as thermal, i.e. split from one combined GVSP
        # block under one hardware frame ID. It therefore carries the same
        # sequence/timestamps -- IR and VL can never mismatch within a frame.
        visible_sequence = thermal_sequence if visible is not None else None

        thermal_meta = StreamMetadata(
            present=thermal is not None,
            width=thermal.shape[1] if thermal is not None else None,
            height=thermal.shape[0] if thermal is not None else None,
            pixel_format=result.thermal_format,
            dtype=str(thermal.dtype) if thermal is not None else None,
            byte_count=thermal.nbytes if thermal is not None else None,
            sequence=thermal_sequence,
            timestamp=thermal_timestamp,
            monotonic_timestamp=thermal_mono,
            hardware_timestamp=result.hardware_timestamp,
        )
        visible_meta = StreamMetadata(
            present=visible is not None,
            width=visible.shape[1] if visible is not None else None,
            height=visible.shape[0] if visible is not None else None,
            pixel_format=result.visible_format,
            dtype=str(visible.dtype) if visible is not None else None,
            byte_count=visible.nbytes if visible is not None else None,
            sequence=visible_sequence,
            timestamp=visible_timestamp,
            monotonic_timestamp=visible_mono,
            hardware_timestamp=None,
        )

        # Synchronization status: both payloads non-None happens only for
        # same-block combined sources (custom GVCP/GVSP backend), where IR
        # and VL are split from ONE GVSP block under ONE block/frame ID --
        # hence intrinsically correlated.
        if thermal is not None and visible is not None:
            sync = SyncInfo(status=SyncStatus.SYNCHRONIZED, time_delta=0.0)
        elif thermal is not None:
            sync = SyncInfo(status=SyncStatus.MISSING_VISIBLE)
        elif visible is not None:
            sync = SyncInfo(status=SyncStatus.MISSING_THERMAL)
        else:
            sync = SyncInfo(status=SyncStatus.UNKNOWN)

        with self._stats_lock:
            sequence = self._sequence
            self._sequence += 1

        # Build immutable metadata mapping
        metadata = MappingProxyType({
            "grab_started": result.grab_started,
            "grab_completed": result.grab_completed,
            "converted_at": result.converted_at,
            "grab_duration_s": result.grab_completed - result.grab_started,
            "packet_stats": result.packet_stats,
        })

        descriptor = FrameDescriptor(
            camera_id=self._camera_id,
            sequence=sequence,
            timestamp=now_wall,
            monotonic_timestamp=now_mono,
            thermal=thermal_meta,
            visible=visible_meta,
            sync=sync,
            metadata=metadata,
        )
        return Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal, visible=visible))

    def _publish_frame(self, frame: Frame) -> None:
        result = self._publisher.publish(frame)
        with self._stats_lock:
            self._total_acquired += 1
            self._frames_received += 1
            self._last_grab_duration = frame.descriptor.metadata.get("grab_duration_s", 0.0)

            # Calculate packet counter deltas from the GVSP receiver
            packet_stats = frame.descriptor.metadata.get("packet_stats")
            if packet_stats and isinstance(packet_stats, dict):
                if self._prev_packet_stats is not None:
                    lost_delta = packet_stats.get("packets_lost", 0) - self._prev_packet_stats.get("packets_lost", 0)
                    incomplete_delta = packet_stats.get("blocks_incomplete", 0) - self._prev_packet_stats.get("blocks_incomplete", 0)
                    if lost_delta > 0:
                        self._packets_lost += lost_delta
                    if incomplete_delta > 0:
                        self._blocks_incomplete += incomplete_delta
                self._prev_packet_stats = packet_stats

            if result.accepted:
                self._published += 1
                self._publish_times.append(time.perf_counter())
                self._maybe_record_integrity(frame)
                self._maybe_mark_latency(frame)
            else:
                self._dropped += 1
            # Acquisition tracks its own sequence; consumer/transport gaps
            # are the responsibility of the transport layer, not acquisition.
            # The worker does not query publisher stats every frame.

    def integrity_stats(self) -> IntegrityStats:
        """Sampled frame-integrity counters for this camera (diagnostic)."""
        return self._integrity.stats(camera_id=self._camera_id)

    def latency_summary(self) -> dict:
        """Acquisition-to-display latency statistics (diagnostic)."""
        return self._latency.summary(self._camera_id)

    def _maybe_mark_latency(self, frame: Frame) -> None:
        """Record grab/publish stage timestamps for latency triage.

        Timestamp semantics (all ``perf_counter`` clock):
        - T0 ``grab`` = metadata ``grab_started``: immediately BEFORE the
          blocking ``grab()`` call. Wait metric only, NOT display latency.
        - T1 ``frame_complete`` = ``descriptor.monotonic_timestamp``: set in
          ``_build_frame`` immediately AFTER ``grab()`` returns a complete
          frame. This is the latency origin.
        Disabled by default; enable via TMS_FRAME_LATENCY_DIAG.
        """
        if not self._latency_diag:
            return
        grab_started = frame.descriptor.metadata.get("grab_started", 0.0)
        try:
            grab_ns = int(float(grab_started) * 1e9)
        except (TypeError, ValueError):
            grab_ns = time.perf_counter_ns()
        try:
            complete_ns = int(float(frame.descriptor.monotonic_timestamp) * 1e9)
        except (TypeError, ValueError):
            complete_ns = grab_ns
        self._latency.note_published(
            self._camera_id,
            frame.descriptor.sequence,
            frame.descriptor.thermal.sequence,
            grab_ns,
            complete_ns,
            time.perf_counter_ns(),
            self._latency_every,
        )

    def _maybe_record_integrity(self, frame: Frame) -> None:
        """Sample one acquisition-side checksum per N published frames.

        Runs post-GVSP-assembly / pre-SHM-publication on the owned GrabResult
        copy, so the checksum represents the coherent acquisition frame.
        Disabled by default; enable per worker or via TMS_FRAME_INTEGRITY_DIAG.
        """
        if not self._integrity_diag:
            return
        sequence = frame.descriptor.sequence
        if self._integrity_every > 1 and (sequence % self._integrity_every) != 0:
            return
        crc = compute_thermal_crc(frame.payload.thermal)
        if crc is None:
            return
        hw_frame_id = frame.descriptor.thermal.sequence
        packet_stats = frame.descriptor.metadata.get("packet_stats")
        self._integrity.record_acquisition(
            camera_id=self._camera_id,
            sequence=sequence,
            hw_frame_id=hw_frame_id,
            crc32=crc,
            wall_ts=frame.descriptor.timestamp,
            mono_ts=frame.descriptor.monotonic_timestamp,
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "integrity acq cam=%s seq=%d hw=%s crc=%08x packets=%s",
                self._camera_id,
                sequence,
                hw_frame_id,
                crc,
                packet_stats,
            )

    def _compute_fps(self, now: float) -> float:
        window = 1.0
        cutoff = now - window
        recent = [t for t in self._publish_times if t >= cutoff]
        if len(recent) < 2:
            return 0.0
        span = max(now - recent[0], 1e-9)
        return (len(recent) - 1) / span

    def _record_error(self, exc: Exception | str) -> None:
        message = str(exc)
        with self._stats_lock:
            self._last_error = message
        logger.warning("Camera %s: %s", self._camera_id, message)

    def _shutdown(self) -> None:
        try:
            self._source.disconnect()
        except Exception:
            logger.exception("Camera %s: error during disconnect", self._camera_id)
        try:
            self._publisher.close()
        except Exception:
            logger.exception("Camera %s: error closing publisher", self._camera_id)
        with self._stats_lock:
            total_acquired = self._total_acquired
            published = self._published
            dropped = self._dropped
        integrity = self._integrity.stats(camera_id=self._camera_id)
        logger.info(
            "FRAME INTEGRITY SUMMARY camera=%s acquired_frames=%d published=%d dropped=%d "
            "acquisition_samples=%d comparisons=%d matched=%d mismatched=%d unsampled=%d",
            self._camera_id,
            total_acquired,
            published,
            dropped,
            integrity.acquisitions_sampled,
            integrity.snapshots_checked,
            integrity.snapshots_matched,
            integrity.snapshots_mismatched,
            integrity.snapshots_unsampled,
        )
        if self._latency_diag:
            logger.info(
                "FRAME LATENCY SUMMARY %s",
                _format_latency_brief(self._latency.summary(self._camera_id)),
            )
        with self._state_lock:
            if self._state not in (AcquisitionState.FAILED, AcquisitionState.STOPPING):
                self._state = AcquisitionState.STOPPED
            elif self._state is AcquisitionState.STOPPING:
                self._state = AcquisitionState.STOPPED
            else:
                self._state = AcquisitionState.FAILED
        logger.info("Camera %s: acquisition shutdown complete (state=%s)", self._camera_id, self._state.value)