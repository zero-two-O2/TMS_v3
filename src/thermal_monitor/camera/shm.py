"""
camera.shm -- Shared-memory ring buffer factory for camera acquisition.

Creates and manages the shared-memory ring buffer transport between
AcquisitionWorker and downstream consumers.
"""

from __future__ import annotations

import numpy as np

from thermal_monitor.camera.model import CameraConfig
from thermal_monitor.core.frame import Frame
from thermal_monitor.core.shm import (
    SharedMemoryRingBuffer,
    RingConfig,
    PayloadSpec,
    SharedMemoryPublisher,
    PublishResult,
)
from thermal_monitor.camera.acquisition import FramePublisher


def create_thermal_ring_config(
    camera_id: str,
    width: int = 640,
    height: int = 480,
    dtype: np.dtype = np.dtype(np.uint16),
    depth: int = 32,
    visible_width: int | None = None,
    visible_height: int | None = None,
    visible_dtype: np.dtype | None = None,
) -> RingConfig:
    """Create a RingConfig for thermal (IR) acquisition.

    Args:
        camera_id: Camera identifier
        width: Thermal frame width
        height: Thermal frame height
        dtype: Thermal frame dtype (uint16 for Mono16)
        depth: Ring buffer depth (number of slots)
        visible_width: Visible frame width (None = no visible payload allocated)
        visible_height: Visible frame height
        visible_dtype: Visible frame dtype

    Returns:
        RingConfig configured for IR-only acquisition with space reserved for VL.
    """
    thermal_bytes = width * height * dtype.itemsize
    thermal_spec = PayloadSpec(
        width=width,
        height=height,
        dtype=dtype,
        bytes_per_frame=thermal_bytes,
    )

    visible_spec = None
    if visible_width is not None and visible_height is not None and visible_dtype is not None:
        visible_bytes = visible_width * visible_height * visible_dtype.itemsize
        visible_spec = PayloadSpec(
            width=visible_width,
            height=visible_height,
            dtype=visible_dtype,
            bytes_per_frame=visible_bytes,
        )

    return RingConfig(
        camera_id=camera_id,
        thermal_spec=thermal_spec,
        visible_spec=visible_spec,
        depth=depth,
    )


def create_ring_buffer_and_publisher(
    camera_id: str,
    width: int = 640,
    height: int = 480,
    dtype: np.dtype = np.dtype(np.uint16),
    depth: int = 32,
) -> tuple[SharedMemoryRingBuffer, SharedMemoryPublisher]:
    """Create a new shared-memory ring buffer and publisher for one camera.

    This is the producer-side factory. Call once per camera at startup.

    Args:
        camera_id: Camera identifier (used for shared memory segment name)
        width: Thermal frame width
        height: Thermal frame height
        dtype: Thermal frame dtype
        depth: Ring buffer depth

    Returns:
        Tuple of (ring_buffer, publisher). The ring_buffer owns the shared memory
        and must be closed on shutdown. The publisher implements FramePublisher
        protocol for AcquisitionWorker.
    """
    config = create_thermal_ring_config(
        camera_id=camera_id,
        width=width,
        height=height,
        dtype=dtype,
        depth=depth,
    )
    ring = SharedMemoryRingBuffer.create(config)
    publisher = SharedMemoryPublisher(ring)
    return ring, publisher


def attach_ring_buffer_and_consumer(
    camera_id: str,
    consumer_name: str,
    width: int = 640,
    height: int = 480,
    dtype: np.dtype = np.dtype(np.uint16),
    depth: int = 32,
) -> tuple[SharedMemoryRingBuffer, "Consumer"]:
    """Attach to an existing ring buffer and create a consumer.

    This is the consumer-side factory. Multiple consumers can attach to the same ring.

    Args:
        camera_id: Camera identifier
        consumer_name: Unique name for this consumer
        width: Thermal frame width
        height: Thermal frame height
        dtype: Thermal frame dtype
        depth: Ring buffer depth (must match producer)

    Returns:
        Tuple of (ring_buffer, consumer). The ring_buffer must be closed when done.
    """
    from thermal_monitor.core.shm import Consumer
    config = create_thermal_ring_config(
        camera_id=camera_id,
        width=width,
        height=height,
        dtype=dtype,
        depth=depth,
    )
    ring = SharedMemoryRingBuffer.attach(config)
    consumer = ring.consumer(consumer_name)
    return ring, consumer


def create_frame_publisher_for_camera(
    camera_config: CameraConfig,
    ring_depth: int = 32,
) -> tuple[SharedMemoryRingBuffer, FramePublisher]:
    """Create ring buffer and publisher from CameraConfig.

    Extracts thermal frame parameters from the validated camera configuration.

    Args:
        camera_config: Validated CameraConfig with identity and stream settings
        ring_depth: Ring buffer depth (default 32 for ~3.5s at 9 FPS)

    Returns:
        Tuple of (ring_buffer, publisher)
    """
    # Thermal parameters from config
    width = 640  # TV46L fixed
    height = 480
    dtype = np.dtype(np.uint16)  # 16-bit Mono16

    return create_ring_buffer_and_publisher(
        camera_id=camera_config.identity.camera_id,
        width=width,
        height=height,
        dtype=dtype,
        depth=ring_depth,
    )