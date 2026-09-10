"""Camera domain: TV46L hardware interaction and acquisition orchestration."""

from thermal_monitor.camera.acquisition import (
    AcquisitionWorker,
    FramePublisher,
    InProcessLatestPublisher,
)
from thermal_monitor.camera.source import (
    CameraConnectionError,
    CameraGrabError,
    CameraGrabTimeout,
    FrameSource,
)
from thermal_monitor.camera.tv46_custom import CustomTV46LDriver
from thermal_monitor.camera.tv46_gvcp import (
    GVCPClient,
    GVCPError,
    TV46DeviceInfo,
    broadcast_discover,
    discover_devices,
    local_interface_ips,
    routed_local_ip,
)
from thermal_monitor.camera.tv46_gvsp import (
    GVSPBlock,
    GVSPReceiver,
    IR_HEIGHT,
    IR_WIDTH,
    VL_HEIGHT,
    VL_PACKING,
    VL_PIXEL_FORMAT,
    VL_WIDTH,
    parse_combined_payload,
    parse_gvsp_header,
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
    "CustomTV46LDriver",
    "FramePublisher",
    "FrameSource",
    "GrabResult",
    "GVCPBlock",
    "GVCPClient",
    "GVCPError",
    "GVSPReceiver",
    "IR_HEIGHT",
    "IR_WIDTH",
    "InProcessLatestPublisher",
    "TV46DeviceInfo",
    "VL_HEIGHT",
    "VL_PACKING",
    "VL_PIXEL_FORMAT",
    "VL_WIDTH",
    "broadcast_discover",
    "discover_devices",
    "local_interface_ips",
    "parse_combined_payload",
    "parse_gvsp_header",
    "routed_local_ip",
    "create_frame_publisher_for_camera",
    "create_ring_buffer_and_publisher",
    "create_thermal_ring_config",
]