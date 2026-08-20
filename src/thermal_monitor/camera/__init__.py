"""Camera domain: TV46L hardware interaction and acquisition orchestration."""

from thermal_monitor.camera.acquisition import (
    AcquisitionWorker,
    FramePublisher,
    InProcessLatestPublisher,
)
from thermal_monitor.camera.driver import (
    CameraConnectionError,
    CameraGrabError,
    CameraGrabTimeout,
    FrameSource,
    TV46LDriver,
)
from thermal_monitor.camera.model import (
    AcquisitionState,
    AcquisitionStats,
    CameraConfig,
    CameraIdentity,
    GrabResult,
)
from thermal_monitor.camera.shm import (
    create_frame_publisher_for_camera,
    create_ring_buffer_and_publisher,
    create_thermal_ring_config,
)

__all__ = [
    "AcquisitionState",
    "AcquisitionStats",
    "AcquisitionWorker",
    "CameraConfig",
    "CameraConnectionError",
    "CameraGrabError",
    "CameraGrabTimeout",
    "CameraIdentity",
    "FramePublisher",
    "FrameSource",
    "GrabResult",
    "InProcessLatestPublisher",
    "TV46LDriver",
    "create_frame_publisher_for_camera",
    "create_ring_buffer_and_publisher",
    "create_thermal_ring_config",
]