# TV46L Hardware Characterization (V3)

* Run UTC: 2026-08-17T11:56:15
* Tool: `tv46l_hardware_probe v1.0.0`
* Requested device: `3408e1db21f2_FlukeProcessInstruments_TV46L1260100029Hz`

## Confidence legend

- **CONFIRMED** — directly verified by this run.
- **MEASURED** — measured value from this run.
- **INFERRED** — derived/consistent with V2 or the measurement.
- **UNKNOWN** — not observable in this run.

## A. Camera identity

| Field | Value |
|---|---|
| camera_id | `cam_HB25100004` |
| serial_number | `HB25100004` |
| model | `TV46L-1-26010002@9Hz` |
| vendor | `Fluke Process Instruments` |
| firmware | `1.0.8` |
| user_name | `` |
| ip_address | `169.254.24.69` |
| device_identifier | `3408e1db21f2_FlukeProcessInstruments_TV46L1260100029Hz` |
| acquisition_interface | `GigEVision2 (GigE Vision / Gigabit Ethernet)` |
| connection_status | `OpenReadWrite` |

## B. Thermal format

| Field | Value |
|---|---|
| tag | `thermal` |
| width | `640` |
| height | `480` |
| dtype | `uint16` |
| channels | `1` |
| itemsize | `2` |
| total_bytes | `614400` |
| numpy_shape | `[480, 640]` |
| numpy_strides | `[1280, 2]` |
| c_contiguous | `True` |
| byteorder | `native` |
| min_raw | `5035` |
| max_raw | `5495` |
| mean_raw | `5154.220787760417` |

HALCON-reported geometry/format:

```
{
  "image_width": 640,
  "image_height": 480,
  "image_pixel_format": 17825799,
  "bits_per_channel": 16,
  "PixelFormat": "Mono16",
  "PayloadSize": 1572864,
  "volatile": "disable"
}
```

## C. Visible format

```
{
  "visible_available": true,
  "visible_available_raw": 1,
  "vl_data_size": 41943520,
  "original_stream_selector": "IR_Data",
  "device_stream_channel_count": null,
  "simultaneous_ir_visible": null,
  "note_handles": "TV46L is a single-stream GigE camera (DeviceStreamChannelCount). IR and visible cannot be acquired simultaneously through one framegrabber handle; visible requires a separate handle or time-sliced selector switching.",
  "drained_stale_frames": 3,
  "switch_to_first_frame_s": 0.9482,
  "visible_format": {
    "tag": "visible",
    "width": 640,
    "height": 480,
    "dtype": "uint8",
    "channels": 3,
    "itemsize": 1,
    "total_bytes": 921600,
    "numpy_shape": [
      480,
      640,
      3
    ],
    "numpy_strides": [
      1920,
      3,
      1
    ],
    "c_contiguous": true,
    "byteorder": "native",
    "min_raw": 0,
    "max_raw": 255,
    "mean_raw": 121.325944,
    "halcon": {
      "image_width": 640,
      "image_height": 480,
      "image_pixel_format": 34603058,
      "PixelFormat": "YUV422_8",
      "PayloadSize": 1572864
    }
  },
  "visible_fps": {
    "measured": true,
    "average_fps": 8.9415,
    "frame_interval_stats_s": {
      "count": 30,
      "mean_s": 0.111838,
      "median_s": 0.111079,
      "p90_s": 0.194958,
      "p95_s": 0.196739,
      "p99_s": 0.202677,
      "max_s": 0.202677,
      "min_s": 0.022263,
      "stddev_s": 0.053418
    }
  },
  "restored_selector": "IR_Data",
  "restored_bits_per_channel": 16,
  "thermal_after_visible": {
    "tag": "thermal",
    "width": 640,
    "height": 480,
    "dtype": "uint16",
    "channels": 1,
    "itemsize": 2,
    "total_bytes": 614400,
    "numpy_shape": [
      480,
      640
    ],
    "numpy_strides": [
      1280,
      2
    ],
    "c_contiguous": true,
    "byteorder": "native",
    "min_raw": 4955,
    "max_raw": 5428,
    "mean_raw": 5113.355199
  }
}
```

## D. Actual FPS

* average FPS: **MEASURED `7.2522`**
* frame count: `500`

## E. Frame timing

```
{
  "count": 499,
  "mean_s": 0.138165,
  "median_s": 0.117283,
  "p90_s": 0.224828,
  "p95_s": 0.324639,
  "p99_s": 0.44248,
  "max_s": 0.547271,
  "min_s": 0.005938,
  "stddev_s": 0.089505
}
```

## F. Hardware timestamp availability

* hardware_timestamp = **UNKNOWN**
* buffer_timestamp readable: `False`
* buffer_timestamp_ns readable: `False`
* device_timestamp_frequency: `None`
* changing across frames: `False`

## G. Sequence availability

```
{
  "hardware_frame_counter": true,
  "source": "buffer_frameid",
  "first": 62,
  "last": 680,
  "count": 500,
  "unique_count": 500,
  "duplicate_count": 0,
  "missing_count": 119,
  "missing_sample": [
    74,
    79,
    80,
    81,
    83,
    84,
    85,
    90,
    95,
    102,
    113,
    114,
    116,
    128,
    133,
    140,
    142,
    150,
    151,
    158,
    165,
    174,
    175,
    176,
    181,
    184,
    192,
    195,
    198,
    199,
    212,
    219,
    239,
    245,
    255,
    258,
    259,
    261,
    266,
    272,
    273,
    275,
    291,
    297,
    304,
    309,
    317,
    318,
    319,
    325
  ],
  "reset_observed": false
}
```

## H. HALCON -> NumPy ownership

**PROVEN: NumPy array owns its data (owndata=True, base=None); releasing the HALCON image left the array unchanged**

```
{
  "procedure": [
    "grabbed A; numpy A created",
    "grabbed 3 more frames"
  ],
  "ownership": "PROVEN: NumPy array owns its data (owndata=True, base=None); releasing the HALCON image left the array unchanged",
  "array_A_props": {
    "owndata": true,
    "writeable": true,
    "c_contiguous": true,
    "base_type": null
  },
  "A_changed_after_subsequent_grabs": false,
  "B_hashes_distinct": true,
  "release_test": {
    "performed": true,
    "result": "stable"
  }
}
```

## I. Copy timings

| Stage                      | count | mean       | median     | p95        | max        |
|----------------------------|-------|------------|------------|------------|------------|
| A_halcon_grab              | 500   | 137.316 ms | 115.980 ms | 323.636 ms | 545.856 ms |
| B_himage_as_numpy_array    | 500   | 0.300 ms   | 0.286 ms   | 0.459 ms   | 0.789 ms   |
| C_numpy_copy_owned         | 500   | 0.068 ms   | 0.062 ms   | 0.104 ms   | 0.228 ms   |
| D_numpy_to_shm_slot        | 500   | 0.067 ms   | 0.061 ms   | 0.113 ms   | 0.592 ms   |
| A_plus_B_plus_C_end_to_end | 500   | 137.684 ms | 116.311 ms | 323.879 ms | 546.276 ms |

## J. Timeout result

```
{
  "drained_before_timeout_probe": 12,
  "short_timeout_grab": "returned without error",
  "error_on_1ms_grab": {
    "code": null,
    "message": ""
  },
  "code_5322_observed": false,
  "drained_before_driver_probe": 12,
  "driver_classification": "no timeout observed",
  "usable_after_timeout": true,
  "timeout_sweep": {
    "1": {
      "timeout_ms": 1,
      "success": 5,
      "attempts": 5
    },
    "20": {
      "timeout_ms": 20,
      "success": 5,
      "attempts": 5
    },
    "60": {
      "timeout_ms": 60,
      "success": 5,
      "attempts": 5
    },
    "100": {
      "timeout_ms": 100,
      "success": 5,
      "attempts": 5
    },
    "150": {
      "timeout_ms": 150,
      "success": 5,
      "attempts": 5
    },
    "200": {
      "timeout_ms": 200,
      "success": 5,
      "attempts": 5
    },
    "300": {
      "timeout_ms": 300,
      "success": 5,
      "attempts": 5
    },
    "500": {
      "timeout_ms": 500,
      "success": 5,
      "attempts": 5
    }
  },
  "practical_timeout_ms": 1
}
```

## K. NUC result

```
{
  "nuc_exposed": true,
  "re_control_cmd_current": "FLK_TI_ControlFeature_REControlCmd_SelectCommandToExecute",
  "re_control_cmd_capabilities": [
    "DeviceVendorName",
    "DeviceModelName",
    "DeviceManufacturerInfo",
    "FLK_TI_Info_PlatformId",
    "FLK_TI_Info_VLDataProviderAvailable",
    "FLK_TI_Info_VLDataSize",
    "FLK_TI_Info_REDataProviderAvailable",
    "FLK_TI_Info_REDataSize",
    "FLK_TI_Info_REDataRate",
    "FLK_TI_Info_RE_TPM_Validity_Code",
    "FLK_TI_Info_RE_Platform_Id",
    "FLK_TI_Info_RE_Capabilities0",
    "FLK_TI_Info_CurrentDeviceTemperatureC",
    "FLK_TI_Info_CriticalDeviceTemperatureC",
    "FLK_TI_Info_SecuredDataStreamingOnly",
    "FLK_TI_InfoSelector",
    "FLK_TI_InfoString",
    "FLK_TI_Info_CalibrationRangeInfoQuery",
    "FLK_TI_Info_CalibrationRangeInfo_RangeSpan_LowerBoundary",
    "FLK_TI_Info_CalibrationRangeInfo_RangeSpan_UpperBoundary",
    "FLK_TI_CalibrationInfo",
    "FLK_TI_ControlFeature_SetFocusDistanceMm",
    "FLK_TI_ControlFeature_CurrentFocusDistanceMm",
    "FLK_TI_ControlFeature_FocusDistanceMm_Min",
    "FLK_TI_ControlFeature_FocusDistanceMm_Max",
    "FLK_TI_ControlFeature_REControlCmd",
    "FLK_TI_ControlFeature_SetFrameRate",
    "DeviceSFNCVersionMajor",
    "DeviceSFNCVersionMinor",
    "DeviceSFNCVersionSubMinor",
    "DeviceType",
    "DeviceStreamChannelCount",
    "FLK_TI_StreamDataSourceSelector",
    "TransmitAs",
    "ImageCompressionMode",
    "SensorWidth",
    "SensorHeight",
    "WidthMin",
    "WidthMax",
    "Width",
    "HeightMin",
    "HeightMax",
    "Height",
    "PixelFormat",
    "AcquisitionMode",
    "PayloadSize",
    "GevCurrentPhysicalLinkConfiguration",
    "GevPrimaryApplicationIPAddress",
    "GevPrimaryApplicationSocket",
    "GevSupportedOptionSelector",
    "GevSupportedOption",
    "[System]TLID",
    "[System]TLVendorName",
    "[System]TLModelName",
    "[System]TLVersion",
    "[System]TLFileName",
    "[System]TLPath",
    "[System]TLType",
    "[System]GenTLVersionMajor",
    "[System]GenTLVersionMinor",
    "[System]GenTLSFNCVersionMajor",
    "[System]GenTLSFNCVersionMinor",
    "[System]GevTLSubsystemInfo",
    "[System]InterfaceUpdateList",
    "[System]InterfaceUpdateTimeout",
    "[System]InterfaceSelector",
    "[System]InterfaceID",
    "[System]GevInterfaceMACAddress",
    "[System]GevInterfaceDefaultIPAddress",
    "[System]GevInterfaceDefaultSubnetMask",
    "[Interface]InterfaceID",
    "[Interface]InterfaceType",
    "[Interface]InterfaceTLVersionMajor",
    "[Interface]InterfaceTLVersionMinor",
    "[Interface]GevInterfaceMACAddress",
    "[Interface]GevInterfaceSubnetSelector",
    "[Interface]GevInterfaceSubnetIPAddress",
    "[Interface]GevInterfaceSubnetMask",
    "[Interface]GevInterfaceMTU",
    "[Interface]GevInterfaceGatewaySelector",
    "[Interface]GevInterfaceGateway",
    "[Interface]DeviceUpdateList",
    "[Interface]DeviceUpdateTimeout",
    "[Interface]DeviceSelector",
    "[Interface]DeviceID",
    "[Interface]DeviceVendorName",
    "[Interface]DeviceModelName",
    "[Interface]DeviceAccessStatus",
    "[Interface]DeviceSerialNumber",
    "[Interface]DeviceUserID",
    "[Interface]DeviceTLVersionMajor",
    "[Interface]DeviceTLVersionMinor",
    "[Interface]GevDeviceIPAddress",
    "[Interface]GevDeviceSubnetMask",
    "[Interface]GevDeviceGateway",
    "[Interface]GevDeviceMACAddress",
    "[Interface]GevDeviceForceIPAddress",
    "[Interface]GevDeviceForceSubnetMask",
    "[Interface]GevDeviceForceGateway",
    "[Interface]GevDeviceForceIPTimeout",
    "[Interface]GevDeviceProposeIP",
    "[Interface]GevDeviceForceIP",
    "[Interface]GevDeviceLastForceIPSuccess",
    "[Interface]ActionCommand",
    "[Interface]ActionDeviceKey",
    "[Interface]ActionGroupKey",
    "[Interface]ActionGroupMask",
    "[Interface]ActionScheduledTimeEnable",
    "[Interface]ActionScheduledTime",
    "[Interface]GevActionDestinationIPAddress",
    "[Device]DeviceID",
    "[Device]DeviceSerialNumber",
    "[Device]DeviceUserID",
    "[Device]DeviceVendorName",
    "[Device]DeviceModelName",
    "[Device]DeviceVersion",
    "[Device]DeviceManufacturerInfo",
    "[Device]DeviceType",
    "[Device]DeviceAccessStatus",
    "[Device]GevDeviceIPAddress",
    "[Device]GevDeviceSubnetMask",
    "[Device]GevDeviceMACAddress",
    "[Device]GevDeviceGateway",
    "[Device]StreamSelector",
    "[Device]StreamID",
    "[Device]DeviceEndianessMechanism",
    "[Device]LinkCommandTimeout",
    "[Device]LinkCommandRetryCount",
    "[Device]DeviceLinkHeartbeatMode",
    "[Device]DeviceLinkHeartbeatTimeout",
    "[Device]ForceSocketDriver",
    "[Device]EventSelector",
    "[Device]EventNotification",
    "[Device]EventDeviceLost",
    "[Device]DeviceMessageChannelKeepAliveTimeout",
    "[Stream]StreamID",
    "[Stream]StreamType",
    "[Stream]StreamAnnouncedBufferCount",
    "[Stream]StreamBufferHandlingMode",
    "[Stream]StreamAnnounceBufferMinimum",
    "[Stream]PayloadSize",
    "[Stream]StreamThreadPriority",
    "[Stream]StreamThreadApplyPriority",
    "[Stream]StreamAuxiliaryBufferCount",
    "[Stream]GevStreamMaxPacketGaps",
    "[Stream]GevStreamMaxBlockDuration",
    "[Stream]GevStreamDeliverIncompleteBlocks",
    "[Stream]GevStreamFullBlockTerminatesPrev",
    "[Stream]GevStreamPacketOrderDelay",
    "[Stream]GevStreamAbortCheckPeriod",
    "[Stream]GevStreamActiveEngine",
    "[Stream]GevStreamRingBufferSize",
    "[Stream]GevStreamReceiveSocketSize",
    "[Stream]GevStreamAssumeImageInChunkPayload",
    "[Stream]DeviceStreamChannelPacketSize",
    "[Stream]DeviceStreamChannelPacketSizeMin",
    "[Stream]DeviceStreamChannelPacketSizeMax",
    "[Stream]DeviceStreamChannelPacketSizeInc",
    "[Stream]DeviceStreamChannelNegotiatePacketSize",
    "[Stream]DeviceStreamChannelKeepAliveTimeout",
    "[Stream]GevStreamSeenPacketCount",
    "[Stream]GevStreamLostPacketCount",
    "[Stream]GevStreamDeliveredPacketCount",
    "[Stream]GevStreamUnavailablePacketCount",
    "[Stream]GevStreamDuplicatePacketCount",
    "[Stream]GevStreamResendCommandCount",
    "[Stream]GevStreamResendPacketCount",
    "[Stream]GevStreamSkippedBlockCount",
    "[Stream]GevStreamEngineUnderrunCount",
    "[Stream]GevStreamDiscardedBlockCount",
    "[Stream]GevStreamIncompleteBlockCount",
    "[Stream]GevStreamOversizedBlockCount",
    "[Stream]EventSelector",
    "[Stream]EventNotification",
    "[Stream]EventTransferEnd",
    "[Stream]EventTransferEndFrameID",
    "[Stream]EventTransferEndBufferUndeliverable",
    "[Stream]EventTransferEndUndeliverabilityReason",
    "available_callback_types",
    "available_event_names",
    "available_param_names",
    "do_abort_grab",
    "do_write_configuration",
    "grab_timeout",
    "image_available",
    "revision",
    "start_async_after_grab_async",
    "volatile",
    "image_width",
    "image_height",
    "bits_per_channel",
    "color_space",
    "num_buffers",
    "tl_id",
    "tl_model",
    "tl_filename",
    "tl_pathname",
    "tl_displayname",
    "delay_after_stop",
    "buffer_timestamp",
    "buffer_timestamp_ns",
    "device_timestamp_frequency",
    "buffer_is_incomplete",
    "num_buffers_underrun",
    "num_buffers_await_delivery",
    "clear_buffer",
    "split_param_values_into_dwords",
    "direct_connection",
    "streaming_mode",
    "device_event_handling",
    "device_access",
    "workarounds",
    "force_ip",
    "force_sockdrv",
    "buffer_reallocation_mode",
    "settings_selector",
    "do_write_settings",
    "do_load_settings",
    "image_contents",
    "image_source_id",
    "image_region_id",
    "image_purpose_id",
    "image_raw_buffer_type",
    "image_raw_buffer_padding_bytes",
    "data_contents",
    "data_source_id",
    "data_region_id",
    "data_purpose_id",
    "create_objectmodel3d",
    "coordinate_transform_mode",
    "confidence_mode",
    "confidence_threshold",
    "add_objectmodel3d_overlay_attrib",
    "buffer_frameid",
    "image_pixel_format",
    "event_notification_helper",
    "fileaccess_remote_name",
    "fileaccess_file_path",
    "do_fileaccess_download",
    "do_fileaccess_upload",
    "do_fileaccess_delete",
    "event_data",
    "event_message_queue",
    "event_selector",
    "available_easyparam_names",
    "event_new_buffer"
  ],
  "result": "NOT TESTED: NUC is exposed but triggering was not requested (--nuc).  Trigger method documented from V2: RequestFineOffset -> ExecuteFineOffset -> pause -> flush."
}
```

## L. Visible acquisition result

* visible available: `True`
* measured FPS: `8.9415`
* simultaneous IR/visible: `None`

## M. Network / transport observations

```
{
  "[Stream]DeviceStreamChannelPacketSize": 1500,
  "[Stream]DeviceStreamChannelPacketSizeMax": 9000,
  "[Stream]GevStreamReceiveSocketSize": 1048576,
  "[Stream]GevStreamRingBufferSize": 262144,
  "[Stream]GevStreamSeenPacketCount": 422,
  "[Stream]GevStreamLostPacketCount": 0,
  "[Stream]GevStreamDeliveredPacketCount": 422,
  "[Stream]GevStreamResendCommandCount": 0,
  "[Stream]GevStreamResendPacketCount": 0,
  "[Stream]GevStreamDiscardedBlockCount": 0,
  "[Stream]GevStreamIncompleteBlockCount": 0,
  "[Stream]GevStreamDuplicatePacketCount": 0,
  "[Stream]GevStreamSkippedBlockCount": 0,
  "[Stream]GevStreamOversizedBlockCount": 0,
  "[Stream]GevStreamUnavailablePacketCount": 0,
  "[Interface]GevInterfaceMTU": 9000,
  "num_buffers": 4,
  "num_buffers_await_delivery": 1,
  "num_buffers_underrun": 0,
  "image_width": 640,
  "image_height": 480,
  "bits_per_channel": 16,
  "grab_timeout": 5000,
  "volatile": "disable",
  "direct_connection": "disable",
  "revision": "24.11.23"
}
```

## N. Reconnection result

```
{
  "close_s": 0.2679,
  "is_connected_after_close": false,
  "reopen_s": 3.5697,
  "reopen_ok": true,
  "reacquire_ok": true,
  "reacquire_shape": [
    480,
    640
  ],
  "note": "Software close/reopen/reacquire only.  Physical unplug/replug testing is deferred to a manual session."
}
```

## O. Per-camera bandwidth

* bytes/frame: `1536000` (thermal `614400` + visible `921600`)
* FPS: `7.2522`
* bytes/sec: `11139379.2` = 10.62 MiB/s (89.12 Mbit/s)

## P. Projected 8-camera bandwidth

```
{
  "thermal_8cam": {
    "bytes_per_sec": 35646013.44,
    "human": "33.99 MiB/s (285.17 Mbit/s)"
  },
  "visible_8cam": {
    "bytes_per_sec": 65923891.2,
    "human": "62.87 MiB/s (527.39 Mbit/s)"
  },
  "combined_8cam": {
    "bytes_per_sec": 101569904.64,
    "human": "96.86 MiB/s (812.56 Mbit/s)",
    "note": "Combined assumes IR+visible run simultaneously.  The TV46L is a single-stream camera (DeviceStreamChannelCount=1); if the two must be time-sliced, the combined rate is the time-sliced alternating rate, not the sum."
  },
  "assumptions": {
    "cameras": 8,
    "fps": 7.2522,
    "visible_fps": 8.9415,
    "linear_scaling_assumed_but_not_proven": true
  }
}
```

## Q. Ring-memory calculations (ADR-003)

| depth | slot (B) | 1 camera (MiB) | 8 cameras (MiB) |
|---|---|---|---|
| 8 | 1540352 | 11.753 | 94.023 |
| 16 | 1540352 | 23.505 | 188.039 |
| 32 | 1540352 | 47.009 | 376.07 |

## Q1. Recommended provisional ring configuration

**Recommended provisional ring configuration: depth 32 per camera.**

Rationale:
* Measured thermal 614400 B at ~7.25 FPS = 4.25 MiB/s (35.65 Mbit/s) per camera.
* Visible adds 921600 B per frame; combined = 1.465 MiB/frame = 10.62 MiB/s (89.12 Mbit/s).
* depth 32 for 8 cameras = 376.07 MiB (provisional, matches the ADR-003 working estimate); depth 16 = 188.039 MiB.
* Ring depth is a bounded rolling history, not the recording buffer; pre-alarm history lives in recorder-owned memory (ADR-003 §14).
* The final depth must cover worst-case consumer delay and any NUC frame gap.
* NUC gap not measured in this run (run with --nuc); depth 32 is a provisional margin until that is known.

## R. Confirmed facts

* CONFIRMED: himage_as_numpy_array ownership behavior (see H).
* CONFIRMED: V3 TV46LDriver opens the camera and returns owned, read-only raw thermal GrabResults.
* CONFIRMED: thermal frame is 640x480 uint16 1-channel (614400 B).

## S. Unknowns

* PTP/GigE hardware timestamp semantics (see F) unless proven.
* Physical unplug/replug reconnect behavior (manual test deferred).
* Startup NUC requirement (requires cold-boot observation).
* 8-camera scaling is a linear projection, not yet measured on hardware.

## T. Recommended next step

Freeze the V3 frame/storage assumptions on these measured values, then run the multi-camera bandwidth validation on the identified bottleneck (see P) before finalizing ADR-003 ring depth.

---
