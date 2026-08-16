# HALCON Parameters - Fluke TV46L

## Camera Information

| Property | Value |
|----------|-------|
| Vendor | Fluke Process Instruments |
| Model | TV46L-1-xxxxxxxx@9Hz | --------(Changes depending on camera)
| Serial Number | HBxxxxxxx |------------(Changes depending on camera)
| Transport Layer | GigE Vision 2 |
| Interface | Gigabit Ethernet |
| Default Stream | IR_Data |
| Pixel Depth | 16-bit |
| Resolution | 640 × 480 |
| Frame Rate | 9 Hz |

---

# Framegrabber

```python
ha.open_framegrabber(
    "GigEVision2",
    0,
    0,
    0,
    0,
    0,
    0,
    "progressive",
    -1,
    "default",
    -1,
    "false",
    "default",
    device,
    0,
    -1
)
```

---

# Stream Configuration

## Thermal Stream
Parameter
```
FLK_TI_StreamDataSourceSelector
```
Value
```
IR_Data
```
Purpose
Selects the thermal image stream.

---

## Image Bit Depth

Parameter
```
bits_per_channel
``
Value
```
16
```
Purpose
Receives raw 16-bit thermal image.
---

## Number of Buffers
Parameter
```
num_buffers
```
Recommended
```
32
```
Purpose
Internal HALCON acquisition buffers.
Note:
Some transport layers may reject this parameter.
Failure is non-critical.
---
# Continuous Acquisition
Start acquisition

```python
ha.grab_image_start(acq, -1)
```
Grab newest frame
```python
image = ha.grab_image_async(acq, 100)
```
Convert to NumPy
```python
frame = ha.himage_as_numpy_array(image)
```

---

# NUC (Non-Uniformity Correction)

## Disable Automatic Fine Offset
Parameter
```
FLK_TI_ControlFeature_REControlCmd
```
Value
```
FLK_TI_ControlFeature_REControlCmd_DisableAutomaticFineOffsets
```
Purpose
Disables automatic shutter corrections.

---

## Request Manual Fine Offset
Parameter
```
FLK_TI_ControlFeature_REControlCmd
```
Value
```
FLK_TI_ControlFeature_REControlCmd_RequestFineOffset
```
Purpose
Requests a manual NUC.

---

## Execute Manual Fine Offset
Parameter
```
FLK_TI_ControlFeature_REControlCmd
```
Value
```
FLK_TI_ControlFeature_REControlCmd_ExecuteFineOffset
```
Purpose
Executes the requested NUC.
Recommended
```python
time.sleep(0.05)
for _ in range(3):
    ha.grab_image_async(acq, 0)
```
This allows the shutter operation to complete and flushes unstable frames.


The camera driver is responsible only for:

- Camera connection
- Camera configuration
- Frame acquisition
- Manual NUC
- Stream statistics
- Device health

The driver is **not responsible** for:

- Temperature calibration
- ROI processing
- Alarm generation
- Image recording
- GUI rendering



================================================================================
HB25100004
AVAILABLE PARAMETERS
================================================================================
Total parameters: 244
AcquisitionMode
DeviceManufacturerInfo
DeviceModelName
DeviceSFNCVersionMajor
DeviceSFNCVersionMinor
DeviceSFNCVersionSubMinor
DeviceStreamChannelCount
DeviceType
DeviceVendorName
FLK_TI_CalibrationInfo
FLK_TI_ControlFeature_CurrentFocusDistanceMm
FLK_TI_ControlFeature_FocusDistanceMm_Max
FLK_TI_ControlFeature_FocusDistanceMm_Min
FLK_TI_ControlFeature_REControlCmd
FLK_TI_ControlFeature_SetFocusDistanceMm
FLK_TI_ControlFeature_SetFrameRate
FLK_TI_InfoSelector
FLK_TI_InfoString
FLK_TI_Info_CalibrationRangeInfoQuery
FLK_TI_Info_CalibrationRangeInfo_RangeSpan_LowerBoundary
FLK_TI_Info_CalibrationRangeInfo_RangeSpan_UpperBoundary
FLK_TI_Info_CriticalDeviceTemperatureC
FLK_TI_Info_CurrentDeviceTemperatureC
FLK_TI_Info_PlatformId
FLK_TI_Info_REDataProviderAvailable
FLK_TI_Info_REDataRate
FLK_TI_Info_REDataSize
FLK_TI_Info_RE_Capabilities0
FLK_TI_Info_RE_Platform_Id
FLK_TI_Info_RE_TPM_Validity_Code
FLK_TI_Info_SecuredDataStreamingOnly
FLK_TI_Info_VLDataProviderAvailable
FLK_TI_Info_VLDataSize
FLK_TI_StreamDataSourceSelector
GevCurrentPhysicalLinkConfiguration
GevPrimaryApplicationIPAddress
GevPrimaryApplicationSocket
GevSupportedOption
GevSupportedOptionSelector
Height
HeightMax
HeightMin
ImageCompressionMode
PayloadSize
PixelFormat
SensorHeight
SensorWidth
TransmitAs
Width
WidthMax
WidthMin
[Device]DeviceAccessStatus
[Device]DeviceEndianessMechanism
[Device]DeviceID
[Device]DeviceLinkHeartbeatMode
[Device]DeviceLinkHeartbeatTimeout
[Device]DeviceManufacturerInfo
[Device]DeviceMessageChannelKeepAliveTimeout
[Device]DeviceModelName
[Device]DeviceSerialNumber
[Device]DeviceType
[Device]DeviceUserID
[Device]DeviceVendorName
[Device]DeviceVersion
[Device]EventDeviceLost
[Device]EventNotification
[Device]EventSelector
[Device]ForceSocketDriver
[Device]GevDeviceGateway
[Device]GevDeviceIPAddress
[Device]GevDeviceMACAddress
[Device]GevDeviceSubnetMask
[Device]LinkCommandRetryCount
[Device]LinkCommandTimeout
[Device]StreamID
[Device]StreamSelector
[Interface]ActionCommand
[Interface]ActionDeviceKey
[Interface]ActionGroupKey
[Interface]ActionGroupMask
[Interface]ActionScheduledTime
[Interface]ActionScheduledTimeEnable
[Interface]DeviceAccessStatus
[Interface]DeviceID
[Interface]DeviceModelName
[Interface]DeviceSelector
[Interface]DeviceSerialNumber
[Interface]DeviceTLVersionMajor
[Interface]DeviceTLVersionMinor
[Interface]DeviceUpdateList
[Interface]DeviceUpdateTimeout
[Interface]DeviceUserID
[Interface]DeviceVendorName
[Interface]GevActionDestinationIPAddress
[Interface]GevDeviceForceGateway
[Interface]GevDeviceForceIP
[Interface]GevDeviceForceIPAddress
[Interface]GevDeviceForceIPTimeout
[Interface]GevDeviceForceSubnetMask
[Interface]GevDeviceGateway
[Interface]GevDeviceIPAddress
[Interface]GevDeviceLastForceIPSuccess
[Interface]GevDeviceMACAddress
[Interface]GevDeviceProposeIP
[Interface]GevDeviceSubnetMask
[Interface]GevInterfaceGateway
[Interface]GevInterfaceGatewaySelector
[Interface]GevInterfaceMACAddress
[Interface]GevInterfaceMTU
[Interface]GevInterfaceSubnetIPAddress
[Interface]GevInterfaceSubnetMask
[Interface]GevInterfaceSubnetSelector
[Interface]InterfaceID
[Interface]InterfaceTLVersionMajor
[Interface]InterfaceTLVersionMinor
[Interface]InterfaceType
[Stream]DeviceStreamChannelKeepAliveTimeout
[Stream]DeviceStreamChannelNegotiatePacketSize
[Stream]DeviceStreamChannelPacketSize
[Stream]DeviceStreamChannelPacketSizeInc
[Stream]DeviceStreamChannelPacketSizeMax
[Stream]DeviceStreamChannelPacketSizeMin
[Stream]EventNotification
[Stream]EventSelector
[Stream]EventTransferEnd
[Stream]EventTransferEndBufferUndeliverable
[Stream]EventTransferEndFrameID
[Stream]EventTransferEndUndeliverabilityReason
[Stream]GevStreamAbortCheckPeriod
[Stream]GevStreamActiveEngine
[Stream]GevStreamDeliverIncompleteBlocks
[Stream]GevStreamDeliveredPacketCount
[Stream]GevStreamDiscardedBlockCount
[Stream]GevStreamDuplicatePacketCount
[Stream]GevStreamEngineUnderrunCount
[Stream]GevStreamFullBlockTerminatesPrev
[Stream]GevStreamIncompleteBlockCount
[Stream]GevStreamLostPacketCount
[Stream]GevStreamMaxBlockDuration
[Stream]GevStreamMaxPacketGaps
[Stream]GevStreamOversizedBlockCount
[Stream]GevStreamPacketOrderDelay
[Stream]GevStreamReceiveSocketSize
[Stream]GevStreamResendCommandCount
[Stream]GevStreamResendPacketCount
[Stream]GevStreamRingBufferSize
[Stream]GevStreamSeenPacketCount
[Stream]GevStreamSkippedBlockCount
[Stream]GevStreamUnavailablePacketCount
[Stream]PayloadSize
[Stream]StreamAnnounceBufferMinimum
[Stream]StreamAnnouncedBufferCount
[Stream]StreamAuxiliaryBufferCount
[Stream]StreamBufferHandlingMode
[Stream]StreamID
[Stream]StreamThreadApplyPriority
[Stream]StreamThreadPriority
[Stream]StreamType
[System]GenTLSFNCVersionMajor
[System]GenTLSFNCVersionMinor
[System]GenTLVersionMajor
[System]GenTLVersionMinor
[System]GevInterfaceDefaultIPAddress
[System]GevInterfaceDefaultSubnetMask
[System]GevInterfaceMACAddress
[System]GevTLSubsystemInfo
[System]InterfaceID
[System]InterfaceSelector
[System]InterfaceUpdateList
[System]InterfaceUpdateTimeout
[System]TLFileName
[System]TLID
[System]TLModelName
[System]TLPath
[System]TLType
[System]TLVendorName
[System]TLVersion
add_objectmodel3d_overlay_attrib
available_callback_types
available_easyparam_names
available_event_names
available_param_names
bits_per_channel
buffer_frameid
buffer_is_incomplete
buffer_reallocation_mode
buffer_timestamp
buffer_timestamp_ns
clear_buffer
color_space
confidence_mode
confidence_threshold
coordinate_transform_mode
create_objectmodel3d
data_contents
data_purpose_id
data_region_id
data_source_id
delay_after_stop
device_access
device_event_handling
device_timestamp_frequency
direct_connection
do_abort_grab
do_fileaccess_delete
do_fileaccess_download
do_fileaccess_upload
do_load_settings
do_write_configuration
do_write_settings
event_data
event_message_queue
event_notification_helper
event_selector
fileaccess_file_path
fileaccess_remote_name
force_ip
force_sockdrv
grab_timeout
image_available
image_contents
image_height
image_pixel_format
image_purpose_id
image_raw_buffer_padding_bytes
image_raw_buffer_type
image_region_id
image_source_id
image_width
num_buffers
num_buffers_await_delivery
num_buffers_underrun
revision
settings_selector
split_param_values_into_dwords
start_async_after_grab_async
streaming_mode
tl_displayname
tl_filename
tl_id
tl_model
tl_pathname
volatile
workarounds

================================================================================
PARAMETER VALUES
================================================================================
AcquisitionMode = ['Continuous']
DeviceManufacturerInfo = ['']
DeviceModelName = ['TV46L-1-26010002@9Hz']
DeviceSFNCVersionMajor = [2]
DeviceSFNCVersionMinor = [2]
DeviceSFNCVersionSubMinor = [0]
DeviceStreamChannelCount = [1]
DeviceType = ['Transmitter']
DeviceVendorName = ['Fluke Process Instruments']
FLK_TI_CalibrationInfo = ['0x016d69520200000003000000b0d000000000a0c10000a0420000b4c10000a542000020400000a040070000005f7089305f7089305f708930cd8c88c3000034c340704344cc0e134180bfdc3c000034c30000f0c2113f2e456dce34427c38403e0000f0c20000f0c100803b458a1261425e57a43e0000f0c10000000000803b4570105f427baac03e000000000000a042e2af2d45f4126f4216bbb83e0000a042000048430a3ca3c4f4c2c642189c873e000048430000af4300000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000a0c1000096440000b4c100509644000020400000a0400b0000005f7089305f7089305f708930cd8c88c3000034c3d54a8242ba13443f552a133b000034c30000f0c2175468433c137140a825803c0000f0c20000f0c100007a435c0c9640281fdb3c0000f0c10000000000007a43a0b59440a871003d000000000000a0422e956743f8619f40c84ef63c0000a0420000484362a5d9c2f881044120d0b43c000048430000af43ec0b94c45fe965415d805c3c0000af43008009440f743dc578d9a8410bb4ef3b0080094400004844b73db1c59ae9dc411daa583b0000484400409c44f8840fc60bc704422fed8e3a00409c44004003450000a0c100803b450000b4c100a83b45000020400000a0400b0000005f7089305f7089305f708930cd8c88c3000034c35a538942048b4e3f54f11a3b000034c30000f0c2ec066a43deb3704036ee7d3c0000f0c20000f0c100007a43b6df9240d8e6cf3c0000f0c10000000000007a4374199240cddaeb3c000000000000a0426ed161435a99a1407835d93c0000a042000048439acec0c2600d0341898d9a3c000048430000af43c35282c418df58417f92383c0000af4300800944e92c23c58a3a9a41e3fcc53b0080094400004844f63096c55e88c541b557313b0000484400409c44a846f0c5f72bea41f289683a00409c4400400345672dfe10']
FLK_TI_ControlFeature_CurrentFocusDistanceMm = [392]
FLK_TI_ControlFeature_FocusDistanceMm_Max = [1000000]
FLK_TI_ControlFeature_FocusDistanceMm_Min = [150]
FLK_TI_ControlFeature_REControlCmd = ['FLK_TI_ControlFeature_REControlCmd_SelectCommandToExecute']
FLK_TI_ControlFeature_SetFrameRate = [9]
FLK_TI_InfoSelector = ['FLK_TI_FirmwareVersion']
FLK_TI_InfoString = ['1.0.8']
FLK_TI_Info_CalibrationRangeInfoQuery = ['FLK_TI_Info_CalibrationRangeInfoQuery_CalRange1']
FLK_TI_Info_CalibrationRangeInfo_RangeSpan_LowerBoundary = [-20.0]
FLK_TI_Info_CalibrationRangeInfo_RangeSpan_UpperBoundary = [80.0]
FLK_TI_Info_CriticalDeviceTemperatureC = [82.0]
FLK_TI_Info_CurrentDeviceTemperatureC = [28.799999237060547]
FLK_TI_Info_PlatformId = [256]
FLK_TI_Info_REDataProviderAvailable = [1]
FLK_TI_Info_REDataRate = [9]
FLK_TI_Info_REDataSize = [41943520]
FLK_TI_Info_RE_Capabilities0 = [15]
FLK_TI_Info_RE_Platform_Id = [263]
FLK_TI_Info_RE_TPM_Validity_Code = [0]
FLK_TI_Info_SecuredDataStreamingOnly = [0]
FLK_TI_Info_VLDataProviderAvailable = [1]
FLK_TI_Info_VLDataSize = [41943520]
FLK_TI_StreamDataSourceSelector = ['VL_Data']
GevCurrentPhysicalLinkConfiguration = ['SingleLink']
GevPrimaryApplicationIPAddress = [2852044881]
GevPrimaryApplicationSocket = [56552]
GevSupportedOption = [1]
GevSupportedOptionSelector = ['SingleLink']
Height = [480]
HeightMax = [480]
HeightMin = [480]
PayloadSize = [1572864]
PixelFormat = ['YUV422_8']
SensorHeight = [480]
SensorWidth = [640]
TransmitAs = ['GigE_Image_Payload']
Width = [640]
WidthMax = [640]
WidthMin = [640]
[Device]DeviceAccessStatus = ['OpenReadWrite']
[Device]DeviceEndianessMechanism = ['Standard']
[Device]DeviceID = ['3408e1db21f2_FlukeProcessInstruments_TV46L1260100029Hz']
[Device]DeviceLinkHeartbeatTimeout = [3000000.0]
[Device]DeviceManufacturerInfo = ['']
[Device]DeviceModelName = ['TV46L-1-26010002@9Hz']
[Device]DeviceSerialNumber = ['HB25100004']
[Device]DeviceType = ['GigEVision']
[Device]DeviceUserID = ['']
[Device]DeviceVendorName = ['Fluke Process Instruments']
[Device]DeviceVersion = ['1.0.8']
[Device]EventNotification = ['Off']
[Device]EventSelector = ['DeviceLost']
[Device]ForceSocketDriver = [0]
[Device]GevDeviceGateway = [0]
[Device]GevDeviceIPAddress = [2852001861]
[Device]GevDeviceMACAddress = [57212753617394]
[Device]GevDeviceSubnetMask = [4294901760]
[Device]LinkCommandRetryCount = [3]
[Device]LinkCommandTimeout = [2000000.0]
[Device]StreamID = ['Esen_DS0_3408e1db21f2']
[Device]StreamSelector = [0]
[Interface]ActionDeviceKey = [0]
[Interface]ActionGroupKey = [0]
[Interface]ActionGroupMask = [0]
[Interface]ActionScheduledTimeEnable = [0]
[Interface]DeviceAccessStatus = ['OpenReadWrite']
[Interface]DeviceID = ['3408e1db21f2_FlukeProcessInstruments_TV46L1260100029Hz']
[Interface]DeviceModelName = ['TV46L-1-26010002@9Hz']
[Interface]DeviceSelector = [0]
[Interface]DeviceSerialNumber = ['HB25100004']
[Interface]DeviceTLVersionMajor = [2]
[Interface]DeviceTLVersionMinor = [0]
[Interface]DeviceUpdateTimeout = [0]
[Interface]DeviceUserID = ['']
[Interface]DeviceVendorName = ['Fluke Process Instruments']
[Interface]GevActionDestinationIPAddress = [0]
[Interface]GevDeviceForceGateway = [0]
[Interface]GevDeviceForceIP = [1]
[Interface]GevDeviceForceIPAddress = [0]
[Interface]GevDeviceForceIPTimeout = [7000000]
[Interface]GevDeviceForceSubnetMask = [0]
[Interface]GevDeviceGateway = [0]
[Interface]GevDeviceIPAddress = [2852001861]
[Interface]GevDeviceLastForceIPSuccess = [1]
[Interface]GevDeviceMACAddress = [57212753617394]
[Interface]GevDeviceSubnetMask = [4294901760]
[Interface]GevInterfaceGateway = [0]
[Interface]GevInterfaceGatewaySelector = [0]
[Interface]GevInterfaceMACAddress = [215938449915984]
[Interface]GevInterfaceMTU = [9000]
[Interface]GevInterfaceSubnetIPAddress = [2852044881]
[Interface]GevInterfaceSubnetMask = [4294901760]
[Interface]GevInterfaceSubnetSelector = [0]
[Interface]InterfaceID = ['Esen_ITF_c4651699c050a9fec051ffff0000']
[Interface]InterfaceTLVersionMajor = [2]
[Interface]InterfaceTLVersionMinor = [1]
[Interface]InterfaceType = ['GigEVision']
[Stream]DeviceStreamChannelKeepAliveTimeout = [30000.0]
[Stream]DeviceStreamChannelPacketSize = [1500]
[Stream]DeviceStreamChannelPacketSizeInc = [4]
[Stream]DeviceStreamChannelPacketSizeMax = [9000]
[Stream]DeviceStreamChannelPacketSizeMin = [576]
[Stream]EventNotification = ['Off']
[Stream]EventSelector = ['TransferEnd']
[Stream]GevStreamAbortCheckPeriod = [300000.0]
[Stream]GevStreamActiveEngine = ['FilterDriver']
[Stream]GevStreamDeliverIncompleteBlocks = [1]
[Stream]GevStreamDeliveredPacketCount = [0]
[Stream]GevStreamDiscardedBlockCount = [0]
[Stream]GevStreamDuplicatePacketCount = [0]
[Stream]GevStreamEngineUnderrunCount = [0]
[Stream]GevStreamFullBlockTerminatesPrev = [0]
[Stream]GevStreamIncompleteBlockCount = [0]
[Stream]GevStreamLostPacketCount = [0]
[Stream]GevStreamMaxBlockDuration = [0.0]
[Stream]GevStreamMaxPacketGaps = [30]
[Stream]GevStreamOversizedBlockCount = [0]
[Stream]GevStreamPacketOrderDelay = [10.0]
[Stream]GevStreamReceiveSocketSize = [131072]
[Stream]GevStreamResendCommandCount = [0]
[Stream]GevStreamResendPacketCount = [0]
[Stream]GevStreamRingBufferSize = [262144]
[Stream]GevStreamSeenPacketCount = [0]
[Stream]GevStreamSkippedBlockCount = [0]
[Stream]GevStreamUnavailablePacketCount = [0]
[Stream]StreamAnnounceBufferMinimum = [1]
[Stream]StreamAnnouncedBufferCount = [0]
[Stream]StreamAuxiliaryBufferCount = [0]
[Stream]StreamBufferHandlingMode = ['OldestFirst']
[Stream]StreamID = ['Esen_DS0_3408e1db21f2']
[Stream]StreamThreadPriority = [2]
[Stream]StreamType = ['GigEVision']
[System]GenTLSFNCVersionMajor = [1]
[System]GenTLSFNCVersionMinor = [1]
[System]GenTLVersionMajor = [1]
[System]GenTLVersionMinor = [6]
[System]GevInterfaceDefaultIPAddress = [2852044881]
[System]GevInterfaceDefaultSubnetMask = [4294901760]
[System]GevInterfaceMACAddress = [215938449915984]
[System]GevTLSubsystemInfo = ['filter driver: 2020804/2020804']
[System]InterfaceID = ['Esen_ITF_c4651699c050a9fec051ffff0000']
[System]InterfaceSelector = [0]
[System]InterfaceUpdateTimeout = [0]
[System]TLFileName = ['hAcqGigEVision2.dll']
[System]TLID = ['C:\\Program Files\\MVTec\\HALCON-24.11-Progress-Steady\\bin\\x64-win64\\hAcqGigEVision2.dll']
[System]TLModelName = ['GigEVision']
[System]TLPath = ['C:\\Program Files\\MVTec\\HALCON-24.11-Progress-Steady\\bin\\x64-win64\\hAcqGigEVision2.dll']
[System]TLType = ['GigEVision']
[System]TLVendorName = ['MVTec Software GmbH']
[System]TLVersion = ['1.0.0']
add_objectmodel3d_overlay_attrib = ['disable']
available_callback_types = ['DeviceVendorName', 'DeviceModelName', 'DeviceManufacturerInfo', 'FLK_TI_Info_PlatformId', 'FLK_TI_Info_VLDataProviderAvailable', 'FLK_TI_Info_VLDataSize', 'FLK_TI_Info_REDataProviderAvailable', 'FLK_TI_Info_REDataSize', 'FLK_TI_Info_REDataRate', 'FLK_TI_Info_RE_TPM_Validity_Code', 'FLK_TI_Info_RE_Platform_Id', 'FLK_TI_Info_RE_Capabilities0', 'FLK_TI_Info_CurrentDeviceTemperatureC', 'FLK_TI_Info_CriticalDeviceTemperatureC', 'FLK_TI_Info_SecuredDataStreamingOnly', 'FLK_TI_InfoSelector', 'FLK_TI_InfoString', 'FLK_TI_Info_CalibrationRangeInfoQuery', 'FLK_TI_Info_CalibrationRangeInfo_RangeSpan_LowerBoundary', 'FLK_TI_Info_CalibrationRangeInfo_RangeSpan_UpperBoundary', 'FLK_TI_CalibrationInfo', 'FLK_TI_ControlFeature_SetFocusDistanceMm', 'FLK_TI_ControlFeature_CurrentFocusDistanceMm', 'FLK_TI_ControlFeature_FocusDistanceMm_Min', 'FLK_TI_ControlFeature_FocusDistanceMm_Max', 'FLK_TI_ControlFeature_REControlCmd', 'FLK_TI_ControlFeature_SetFrameRate', 'DeviceSFNCVersionMajor', 'DeviceSFNCVersionMinor', 'DeviceSFNCVersionSubMinor', 'DeviceType', 'DeviceStreamChannelCount', 'FLK_TI_StreamDataSourceSelector', 'TransmitAs', 'ImageCompressionMode', 'SensorWidth', 'SensorHeight', 'WidthMin', 'WidthMax', 'Width', 'HeightMin', 'HeightMax', 'Height', 'PixelFormat', 'AcquisitionMode', 'PayloadSize', 'GevCurrentPhysicalLinkConfiguration', 'GevPrimaryApplicationIPAddress', 'GevPrimaryApplicationSocket', 'GevSupportedOptionSelector', 'GevSupportedOption', '[System]TLID', '[System]TLVendorName', '[System]TLModelName', '[System]TLVersion', '[System]TLFileName', '[System]TLPath', '[System]TLType', '[System]GenTLVersionMajor', '[System]GenTLVersionMinor', '[System]GenTLSFNCVersionMajor', '[System]GenTLSFNCVersionMinor', '[System]GevTLSubsystemInfo', '[System]InterfaceUpdateList', '[System]InterfaceUpdateTimeout', '[System]InterfaceSelector', '[System]InterfaceID', '[System]GevInterfaceMACAddress', '[System]GevInterfaceDefaultIPAddress', '[System]GevInterfaceDefaultSubnetMask', '[Interface]InterfaceID', '[Interface]InterfaceType', '[Interface]InterfaceTLVersionMajor', '[Interface]InterfaceTLVersionMinor', '[Interface]GevInterfaceMACAddress', '[Interface]GevInterfaceSubnetSelector', '[Interface]GevInterfaceSubnetIPAddress', '[Interface]GevInterfaceSubnetMask', '[Interface]GevInterfaceMTU', '[Interface]GevInterfaceGatewaySelector', '[Interface]GevInterfaceGateway', '[Interface]DeviceUpdateList', '[Interface]DeviceUpdateTimeout', '[Interface]DeviceSelector', '[Interface]DeviceID', '[Interface]DeviceVendorName', '[Interface]DeviceModelName', '[Interface]DeviceAccessStatus', '[Interface]DeviceSerialNumber', '[Interface]DeviceUserID', '[Interface]DeviceTLVersionMajor', '[Interface]DeviceTLVersionMinor', '[Interface]GevDeviceIPAddress', '[Interface]GevDeviceSubnetMask', '[Interface]GevDeviceGateway', '[Interface]GevDeviceMACAddress', '[Interface]GevDeviceForceIPAddress', '[Interface]GevDeviceForceSubnetMask', '[Interface]GevDeviceForceGateway', '[Interface]GevDeviceForceIPTimeout', '[Interface]GevDeviceProposeIP', '[Interface]GevDeviceForceIP', '[Interface]GevDeviceLastForceIPSuccess', '[Interface]ActionCommand', '[Interface]ActionDeviceKey', '[Interface]ActionGroupKey', '[Interface]ActionGroupMask', '[Interface]ActionScheduledTimeEnable', '[Interface]ActionScheduledTime', '[Interface]GevActionDestinationIPAddress', '[Device]DeviceID', '[Device]DeviceSerialNumber', '[Device]DeviceUserID', '[Device]DeviceVendorName', '[Device]DeviceModelName', '[Device]DeviceVersion', '[Device]DeviceManufacturerInfo', '[Device]DeviceType', '[Device]DeviceAccessStatus', '[Device]GevDeviceIPAddress', '[Device]GevDeviceSubnetMask', '[Device]GevDeviceMACAddress', '[Device]GevDeviceGateway', '[Device]StreamSelector', '[Device]StreamID', '[Device]DeviceEndianessMechanism', '[Device]LinkCommandTimeout', '[Device]LinkCommandRetryCount', '[Device]DeviceLinkHeartbeatMode', '[Device]DeviceLinkHeartbeatTimeout', '[Device]ForceSocketDriver', '[Device]EventSelector', '[Device]EventNotification', '[Device]EventDeviceLost', '[Device]DeviceMessageChannelKeepAliveTimeout', '[Stream]StreamID', '[Stream]StreamType', '[Stream]StreamAnnouncedBufferCount', '[Stream]StreamBufferHandlingMode', '[Stream]StreamAnnounceBufferMinimum', '[Stream]PayloadSize', '[Stream]StreamThreadPriority', '[Stream]StreamThreadApplyPriority', '[Stream]StreamAuxiliaryBufferCount', '[Stream]GevStreamMaxPacketGaps', '[Stream]GevStreamMaxBlockDuration', '[Stream]GevStreamDeliverIncompleteBlocks', '[Stream]GevStreamFullBlockTerminatesPrev', '[Stream]GevStreamPacketOrderDelay', '[Stream]GevStreamAbortCheckPeriod', '[Stream]GevStreamActiveEngine', '[Stream]GevStreamRingBufferSize', '[Stream]GevStreamReceiveSocketSize', '[Stream]DeviceStreamChannelPacketSize', '[Stream]DeviceStreamChannelPacketSizeMin', '[Stream]DeviceStreamChannelPacketSizeMax', '[Stream]DeviceStreamChannelPacketSizeInc', '[Stream]DeviceStreamChannelNegotiatePacketSize', '[Stream]DeviceStreamChannelKeepAliveTimeout', '[Stream]GevStreamSeenPacketCount', '[Stream]GevStreamLostPacketCount', '[Stream]GevStreamDeliveredPacketCount', '[Stream]GevStreamUnavailablePacketCount', '[Stream]GevStreamDuplicatePacketCount', '[Stream]GevStreamResendCommandCount', '[Stream]GevStreamResendPacketCount', '[Stream]GevStreamSkippedBlockCount', '[Stream]GevStreamEngineUnderrunCount', '[Stream]GevStreamDiscardedBlockCount', '[Stream]GevStreamIncompleteBlockCount', '[Stream]GevStreamOversizedBlockCount', '[Stream]EventSelector', '[Stream]EventNotification', '[Stream]EventTransferEnd', '[Stream]EventTransferEndFrameID', '[Stream]EventTransferEndBufferUndeliverable', '[Stream]EventTransferEndUndeliverabilityReason', 'available_callback_types', 'available_event_names', 'available_param_names', 'do_abort_grab', 'do_write_configuration', 'grab_timeout', 'image_available', 'revision', 'start_async_after_grab_async', 'volatile', 'image_width', 'image_height', 'bits_per_channel', 'color_space', 'num_buffers', 'tl_id', 'tl_model', 'tl_filename', 'tl_pathname', 'tl_displayname', 'delay_after_stop', 'buffer_timestamp', 'buffer_timestamp_ns', 'device_timestamp_frequency', 'buffer_is_incomplete', 'num_buffers_underrun', 'num_buffers_await_delivery', 'clear_buffer', 'split_param_values_into_dwords', 'direct_connection', 'streaming_mode', 'device_event_handling', 'device_access', 'workarounds', 'force_ip', 'force_sockdrv', 'buffer_reallocation_mode', 'settings_selector', 'do_write_settings', 'do_load_settings', 'image_contents', 'image_source_id', 'image_region_id', 'image_purpose_id', 'image_raw_buffer_type', 'image_raw_buffer_padding_bytes', 'data_contents', 'data_source_id', 'data_region_id', 'data_purpose_id', 'create_objectmodel3d', 'coordinate_transform_mode', 'confidence_mode', 'confidence_threshold', 'add_objectmodel3d_overlay_attrib', 'buffer_frameid', 'image_pixel_format', 'event_notification_helper', 'fileaccess_remote_name', 'fileaccess_file_path', 'do_fileaccess_download', 'do_fileaccess_upload', 'do_fileaccess_delete', 'event_data', 'event_message_queue', 'event_selector', 'available_easyparam_names', 'event_new_buffer']
available_easyparam_names = ['[Consumer]exposure_auto', '[Consumer]exposure', '[Consumer]gain_auto', '[Consumer]gain', '[Consumer]info_general', '[Consumer]trigger', '[Consumer]trigger_activation', '[Consumer]trigger_delay', '[Consumer]trigger_software']
available_event_names = ['DeviceVendorName', 'DeviceModelName', 'DeviceManufacturerInfo', 'FLK_TI_Info_PlatformId', 'FLK_TI_Info_VLDataProviderAvailable', 'FLK_TI_Info_VLDataSize', 'FLK_TI_Info_REDataProviderAvailable', 'FLK_TI_Info_REDataSize', 'FLK_TI_Info_REDataRate', 'FLK_TI_Info_RE_TPM_Validity_Code', 'FLK_TI_Info_RE_Platform_Id', 'FLK_TI_Info_RE_Capabilities0', 'FLK_TI_Info_CurrentDeviceTemperatureC', 'FLK_TI_Info_CriticalDeviceTemperatureC', 'FLK_TI_Info_SecuredDataStreamingOnly', 'FLK_TI_InfoSelector', 'FLK_TI_InfoString', 'FLK_TI_Info_CalibrationRangeInfoQuery', 'FLK_TI_Info_CalibrationRangeInfo_RangeSpan_LowerBoundary', 'FLK_TI_Info_CalibrationRangeInfo_RangeSpan_UpperBoundary', 'FLK_TI_CalibrationInfo', 'FLK_TI_ControlFeature_SetFocusDistanceMm', 'FLK_TI_ControlFeature_CurrentFocusDistanceMm', 'FLK_TI_ControlFeature_FocusDistanceMm_Min', 'FLK_TI_ControlFeature_FocusDistanceMm_Max', 'FLK_TI_ControlFeature_REControlCmd', 'FLK_TI_ControlFeature_SetFrameRate', 'DeviceSFNCVersionMajor', 'DeviceSFNCVersionMinor', 'DeviceSFNCVersionSubMinor', 'DeviceType', 'DeviceStreamChannelCount', 'FLK_TI_StreamDataSourceSelector', 'TransmitAs', 'ImageCompressionMode', 'SensorWidth', 'SensorHeight', 'WidthMin', 'WidthMax', 'Width', 'HeightMin', 'HeightMax', 'Height', 'PixelFormat', 'AcquisitionMode', 'PayloadSize', 'GevCurrentPhysicalLinkConfiguration', 'GevPrimaryApplicationIPAddress', 'GevPrimaryApplicationSocket', 'GevSupportedOptionSelector', 'GevSupportedOption', '[System]TLID', '[System]TLVendorName', '[System]TLModelName', '[System]TLVersion', '[System]TLFileName', '[System]TLPath', '[System]TLType', '[System]GenTLVersionMajor', '[System]GenTLVersionMinor', '[System]GenTLSFNCVersionMajor', '[System]GenTLSFNCVersionMinor', '[System]GevTLSubsystemInfo', '[System]InterfaceUpdateList', '[System]InterfaceUpdateTimeout', '[System]InterfaceSelector', '[System]InterfaceID', '[System]GevInterfaceMACAddress', '[System]GevInterfaceDefaultIPAddress', '[System]GevInterfaceDefaultSubnetMask', '[Interface]InterfaceID', '[Interface]InterfaceType', '[Interface]InterfaceTLVersionMajor', '[Interface]InterfaceTLVersionMinor', '[Interface]GevInterfaceMACAddress', '[Interface]GevInterfaceSubnetSelector', '[Interface]GevInterfaceSubnetIPAddress', '[Interface]GevInterfaceSubnetMask', '[Interface]GevInterfaceMTU', '[Interface]GevInterfaceGatewaySelector', '[Interface]GevInterfaceGateway', '[Interface]DeviceUpdateList', '[Interface]DeviceUpdateTimeout', '[Interface]DeviceSelector', '[Interface]DeviceID', '[Interface]DeviceVendorName', '[Interface]DeviceModelName', '[Interface]DeviceAccessStatus', '[Interface]DeviceSerialNumber', '[Interface]DeviceUserID', '[Interface]DeviceTLVersionMajor', '[Interface]DeviceTLVersionMinor', '[Interface]GevDeviceIPAddress', '[Interface]GevDeviceSubnetMask', '[Interface]GevDeviceGateway', '[Interface]GevDeviceMACAddress', '[Interface]GevDeviceForceIPAddress', '[Interface]GevDeviceForceSubnetMask', '[Interface]GevDeviceForceGateway', '[Interface]GevDeviceForceIPTimeout', '[Interface]GevDeviceProposeIP', '[Interface]GevDeviceForceIP', '[Interface]GevDeviceLastForceIPSuccess', '[Interface]ActionCommand', '[Interface]ActionDeviceKey', '[Interface]ActionGroupKey', '[Interface]ActionGroupMask', '[Interface]ActionScheduledTimeEnable', '[Interface]ActionScheduledTime', '[Interface]GevActionDestinationIPAddress', '[Device]DeviceID', '[Device]DeviceSerialNumber', '[Device]DeviceUserID', '[Device]DeviceVendorName', '[Device]DeviceModelName', '[Device]DeviceVersion', '[Device]DeviceManufacturerInfo', '[Device]DeviceType', '[Device]DeviceAccessStatus', '[Device]GevDeviceIPAddress', '[Device]GevDeviceSubnetMask', '[Device]GevDeviceMACAddress', '[Device]GevDeviceGateway', '[Device]StreamSelector', '[Device]StreamID', '[Device]DeviceEndianessMechanism', '[Device]LinkCommandTimeout', '[Device]LinkCommandRetryCount', '[Device]DeviceLinkHeartbeatMode', '[Device]DeviceLinkHeartbeatTimeout', '[Device]ForceSocketDriver', '[Device]EventSelector', '[Device]EventNotification', '[Device]EventDeviceLost', '[Device]DeviceMessageChannelKeepAliveTimeout', '[Stream]StreamID', '[Stream]StreamType', '[Stream]StreamAnnouncedBufferCount', '[Stream]StreamBufferHandlingMode', '[Stream]StreamAnnounceBufferMinimum', '[Stream]PayloadSize', '[Stream]StreamThreadPriority', '[Stream]StreamThreadApplyPriority', '[Stream]StreamAuxiliaryBufferCount', '[Stream]GevStreamMaxPacketGaps', '[Stream]GevStreamMaxBlockDuration', '[Stream]GevStreamDeliverIncompleteBlocks', '[Stream]GevStreamFullBlockTerminatesPrev', '[Stream]GevStreamPacketOrderDelay', '[Stream]GevStreamAbortCheckPeriod', '[Stream]GevStreamActiveEngine', '[Stream]GevStreamRingBufferSize', '[Stream]GevStreamReceiveSocketSize', '[Stream]DeviceStreamChannelPacketSize', '[Stream]DeviceStreamChannelPacketSizeMin', '[Stream]DeviceStreamChannelPacketSizeMax', '[Stream]DeviceStreamChannelPacketSizeInc', '[Stream]DeviceStreamChannelNegotiatePacketSize', '[Stream]DeviceStreamChannelKeepAliveTimeout', '[Stream]GevStreamSeenPacketCount', '[Stream]GevStreamLostPacketCount', '[Stream]GevStreamDeliveredPacketCount', '[Stream]GevStreamUnavailablePacketCount', '[Stream]GevStreamDuplicatePacketCount', '[Stream]GevStreamResendCommandCount', '[Stream]GevStreamResendPacketCount', '[Stream]GevStreamSkippedBlockCount', '[Stream]GevStreamEngineUnderrunCount', '[Stream]GevStreamDiscardedBlockCount', '[Stream]GevStreamIncompleteBlockCount', '[Stream]GevStreamOversizedBlockCount', '[Stream]EventSelector', '[Stream]EventNotification', '[Stream]EventTransferEnd', '[Stream]EventTransferEndFrameID', '[Stream]EventTransferEndBufferUndeliverable', '[Stream]EventTransferEndUndeliverabilityReason', 'available_callback_types', 'available_event_names', 'available_param_names', 'do_abort_grab', 'do_write_configuration', 'grab_timeout', 'image_available', 'revision', 'start_async_after_grab_async', 'volatile', 'image_width', 'image_height', 'bits_per_channel', 'color_space', 'num_buffers', 'tl_id', 'tl_model', 'tl_filename', 'tl_pathname', 'tl_displayname', 'delay_after_stop', 'buffer_timestamp', 'buffer_timestamp_ns', 'device_timestamp_frequency', 'buffer_is_incomplete', 'num_buffers_underrun', 'num_buffers_await_delivery', 'clear_buffer', 'split_param_values_into_dwords', 'direct_connection', 'streaming_mode', 'device_event_handling', 'device_access', 'workarounds', 'force_ip', 'force_sockdrv', 'buffer_reallocation_mode', 'settings_selector', 'do_write_settings', 'do_load_settings', 'image_contents', 'image_source_id', 'image_region_id', 'image_purpose_id', 'image_raw_buffer_type', 'image_raw_buffer_padding_bytes', 'data_contents', 'data_source_id', 'data_region_id', 'data_purpose_id', 'create_objectmodel3d', 'coordinate_transform_mode', 'confidence_mode', 'confidence_threshold', 'add_objectmodel3d_overlay_attrib', 'buffer_frameid', 'image_pixel_format', 'event_notification_helper', 'fileaccess_remote_name', 'fileaccess_file_path', 'do_fileaccess_download', 'do_fileaccess_upload', 'do_fileaccess_delete', 'event_data', 'event_message_queue', 'event_selector', 'available_easyparam_names', 'event_new_buffer']
available_param_names = ['DeviceVendorName', 'DeviceModelName', 'DeviceManufacturerInfo', 'FLK_TI_Info_PlatformId', 'FLK_TI_Info_VLDataProviderAvailable', 'FLK_TI_Info_VLDataSize', 'FLK_TI_Info_REDataProviderAvailable', 'FLK_TI_Info_REDataSize', 'FLK_TI_Info_REDataRate', 'FLK_TI_Info_RE_TPM_Validity_Code', 'FLK_TI_Info_RE_Platform_Id', 'FLK_TI_Info_RE_Capabilities0', 'FLK_TI_Info_CurrentDeviceTemperatureC', 'FLK_TI_Info_CriticalDeviceTemperatureC', 'FLK_TI_Info_SecuredDataStreamingOnly', 'FLK_TI_InfoSelector', 'FLK_TI_InfoString', 'FLK_TI_Info_CalibrationRangeInfoQuery', 'FLK_TI_Info_CalibrationRangeInfo_RangeSpan_LowerBoundary', 'FLK_TI_Info_CalibrationRangeInfo_RangeSpan_UpperBoundary', 'FLK_TI_CalibrationInfo', 'FLK_TI_ControlFeature_SetFocusDistanceMm', 'FLK_TI_ControlFeature_CurrentFocusDistanceMm', 'FLK_TI_ControlFeature_FocusDistanceMm_Min', 'FLK_TI_ControlFeature_FocusDistanceMm_Max', 'FLK_TI_ControlFeature_REControlCmd', 'FLK_TI_ControlFeature_SetFrameRate', 'DeviceSFNCVersionMajor', 'DeviceSFNCVersionMinor', 'DeviceSFNCVersionSubMinor', 'DeviceType', 'DeviceStreamChannelCount', 'FLK_TI_StreamDataSourceSelector', 'TransmitAs', 'ImageCompressionMode', 'SensorWidth', 'SensorHeight', 'WidthMin', 'WidthMax', 'Width', 'HeightMin', 'HeightMax', 'Height', 'PixelFormat', 'AcquisitionMode', 'PayloadSize', 'GevCurrentPhysicalLinkConfiguration', 'GevPrimaryApplicationIPAddress', 'GevPrimaryApplicationSocket', 'GevSupportedOptionSelector', 'GevSupportedOption', '[System]TLID', '[System]TLVendorName', '[System]TLModelName', '[System]TLVersion', '[System]TLFileName', '[System]TLPath', '[System]TLType', '[System]GenTLVersionMajor', '[System]GenTLVersionMinor', '[System]GenTLSFNCVersionMajor', '[System]GenTLSFNCVersionMinor', '[System]GevTLSubsystemInfo', '[System]InterfaceUpdateList', '[System]InterfaceUpdateTimeout', '[System]InterfaceSelector', '[System]InterfaceID', '[System]GevInterfaceMACAddress', '[System]GevInterfaceDefaultIPAddress', '[System]GevInterfaceDefaultSubnetMask', '[Interface]InterfaceID', '[Interface]InterfaceType', '[Interface]InterfaceTLVersionMajor', '[Interface]InterfaceTLVersionMinor', '[Interface]GevInterfaceMACAddress', '[Interface]GevInterfaceSubnetSelector', '[Interface]GevInterfaceSubnetIPAddress', '[Interface]GevInterfaceSubnetMask', '[Interface]GevInterfaceMTU', '[Interface]GevInterfaceGatewaySelector', '[Interface]GevInterfaceGateway', '[Interface]DeviceUpdateList', '[Interface]DeviceUpdateTimeout', '[Interface]DeviceSelector', '[Interface]DeviceID', '[Interface]DeviceVendorName', '[Interface]DeviceModelName', '[Interface]DeviceAccessStatus', '[Interface]DeviceSerialNumber', '[Interface]DeviceUserID', '[Interface]DeviceTLVersionMajor', '[Interface]DeviceTLVersionMinor', '[Interface]GevDeviceIPAddress', '[Interface]GevDeviceSubnetMask', '[Interface]GevDeviceGateway', '[Interface]GevDeviceMACAddress', '[Interface]GevDeviceForceIPAddress', '[Interface]GevDeviceForceSubnetMask', '[Interface]GevDeviceForceGateway', '[Interface]GevDeviceForceIPTimeout', '[Interface]GevDeviceProposeIP', '[Interface]GevDeviceForceIP', '[Interface]GevDeviceLastForceIPSuccess', '[Interface]ActionCommand', '[Interface]ActionDeviceKey', '[Interface]ActionGroupKey', '[Interface]ActionGroupMask', '[Interface]ActionScheduledTimeEnable', '[Interface]ActionScheduledTime', '[Interface]GevActionDestinationIPAddress', '[Device]DeviceID', '[Device]DeviceSerialNumber', '[Device]DeviceUserID', '[Device]DeviceVendorName', '[Device]DeviceModelName', '[Device]DeviceVersion', '[Device]DeviceManufacturerInfo', '[Device]DeviceType', '[Device]DeviceAccessStatus', '[Device]GevDeviceIPAddress', '[Device]GevDeviceSubnetMask', '[Device]GevDeviceMACAddress', '[Device]GevDeviceGateway', '[Device]StreamSelector', '[Device]StreamID', '[Device]DeviceEndianessMechanism', '[Device]LinkCommandTimeout', '[Device]LinkCommandRetryCount', '[Device]DeviceLinkHeartbeatMode', '[Device]DeviceLinkHeartbeatTimeout', '[Device]ForceSocketDriver', '[Device]EventSelector', '[Device]EventNotification', '[Device]EventDeviceLost', '[Device]DeviceMessageChannelKeepAliveTimeout', '[Stream]StreamID', '[Stream]StreamType', '[Stream]StreamAnnouncedBufferCount', '[Stream]StreamBufferHandlingMode', '[Stream]StreamAnnounceBufferMinimum', '[Stream]PayloadSize', '[Stream]StreamThreadPriority', '[Stream]StreamThreadApplyPriority', '[Stream]StreamAuxiliaryBufferCount', '[Stream]GevStreamMaxPacketGaps', '[Stream]GevStreamMaxBlockDuration', '[Stream]GevStreamDeliverIncompleteBlocks', '[Stream]GevStreamFullBlockTerminatesPrev', '[Stream]GevStreamPacketOrderDelay', '[Stream]GevStreamAbortCheckPeriod', '[Stream]GevStreamActiveEngine', '[Stream]GevStreamRingBufferSize', '[Stream]GevStreamReceiveSocketSize', '[Stream]DeviceStreamChannelPacketSize', '[Stream]DeviceStreamChannelPacketSizeMin', '[Stream]DeviceStreamChannelPacketSizeMax', '[Stream]DeviceStreamChannelPacketSizeInc', '[Stream]DeviceStreamChannelNegotiatePacketSize', '[Stream]DeviceStreamChannelKeepAliveTimeout', '[Stream]GevStreamSeenPacketCount', '[Stream]GevStreamLostPacketCount', '[Stream]GevStreamDeliveredPacketCount', '[Stream]GevStreamUnavailablePacketCount', '[Stream]GevStreamDuplicatePacketCount', '[Stream]GevStreamResendCommandCount', '[Stream]GevStreamResendPacketCount', '[Stream]GevStreamSkippedBlockCount', '[Stream]GevStreamEngineUnderrunCount', '[Stream]GevStreamDiscardedBlockCount', '[Stream]GevStreamIncompleteBlockCount', '[Stream]GevStreamOversizedBlockCount', '[Stream]EventSelector', '[Stream]EventNotification', '[Stream]EventTransferEnd', '[Stream]EventTransferEndFrameID', '[Stream]EventTransferEndBufferUndeliverable', '[Stream]EventTransferEndUndeliverabilityReason', 'available_callback_types', 'available_event_names', 'available_param_names', 'do_abort_grab', 'do_write_configuration', 'grab_timeout', 'image_available', 'revision', 'start_async_after_grab_async', 'volatile', 'image_width', 'image_height', 'bits_per_channel', 'color_space', 'num_buffers', 'tl_id', 'tl_model', 'tl_filename', 'tl_pathname', 'tl_displayname', 'delay_after_stop', 'buffer_timestamp', 'buffer_timestamp_ns', 'device_timestamp_frequency', 'buffer_is_incomplete', 'num_buffers_underrun', 'num_buffers_await_delivery', 'clear_buffer', 'split_param_values_into_dwords', 'direct_connection', 'streaming_mode', 'device_event_handling', 'device_access', 'workarounds', 'force_ip', 'force_sockdrv', 'buffer_reallocation_mode', 'settings_selector', 'do_write_settings', 'do_load_settings', 'image_contents', 'image_source_id', 'image_region_id', 'image_purpose_id', 'image_raw_buffer_type', 'image_raw_buffer_padding_bytes', 'data_contents', 'data_source_id', 'data_region_id', 'data_purpose_id', 'create_objectmodel3d', 'coordinate_transform_mode', 'confidence_mode', 'confidence_threshold', 'add_objectmodel3d_overlay_attrib', 'buffer_frameid', 'image_pixel_format', 'event_notification_helper', 'fileaccess_remote_name', 'fileaccess_file_path', 'do_fileaccess_download', 'do_fileaccess_upload', 'do_fileaccess_delete', 'event_data', 'event_message_queue', 'event_selector', 'available_easyparam_names']
bits_per_channel = [-1]
buffer_reallocation_mode = ['only_increase_size']
clear_buffer = ['disable']
color_space = ['default']
confidence_mode = ['off']
confidence_threshold = [0.5]
coordinate_transform_mode = ['reference']
create_objectmodel3d = ['disable']
delay_after_stop = [0]
device_access = ['exclusive']
device_event_handling = [1]
direct_connection = ['disable']
event_data = []
event_message_queue = [<halcon.ffi.HNull object at 0x0000024A90634D60>]
event_notification_helper = ['disable']
event_selector = ['grab_timeout']
fileaccess_file_path = ['']
fileaccess_remote_name = ['']
force_ip = ['']
force_sockdrv = [0]
grab_timeout = [5000]
image_available = [0]
image_height = [480]
image_width = [640]
num_buffers = [4]
num_buffers_await_delivery = [0]
num_buffers_underrun = [0]
revision = ['24.11.21']
settings_selector = ['RemoteDevice']
split_param_values_into_dwords = ['disable']
start_async_after_grab_async = ['enable']
streaming_mode = [1]
tl_displayname = ['MVTec GigE Vision GenTL Producer']
tl_filename = ['hAcqGigEVision2.dll']
tl_id = ['C:\\Program Files\\MVTec\\HALCON-24.11-Progress-Steady\\bin\\x64-win64\\hAcqGigEVision2.dll']
tl_model = ['GigEVision']
tl_pathname = ['C:\\Program Files\\MVTec\\HALCON-24.11-Progress-Steady\\bin\\x64-win64\\hAcqGigEVision2.dll']
volatile = ['disable']
workarounds = ['']

================================================================================
EASY PARAMETERS
================================================================================
[Consumer]exposure
[Consumer]exposure_auto
[Consumer]gain
[Consumer]gain_auto
[Consumer]info_general
[Consumer]trigger
[Consumer]trigger_activation
[Consumer]trigger_delay
[Consumer]trigger_software