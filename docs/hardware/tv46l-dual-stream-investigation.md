# TV46L Simultaneous Dual-Stream Investigation (V3)

* Investigation date: 2026-08-18
* Camera: `cam_HB25100004` (TV46L-1-26010002@9Hz, firmware 1.0.8)
* HALCON: `24.11` (GigEVision2 interface, `hAcqGigEVision2` reference)
* Dual-stream probe run: **2026-08-18, result `second_handle_failed`**
  (`reports/hardware/tv46l_dual_stream_cam_probe.json`)
* Related documents: `docs/hardware/tv46l-characterization.md`,
  `docs/Halcon/Halcon_Parameters.md`, `docs/decisions/ADR-002-frame-model.md`,
  `docs/decisions/ADR-003-shared-memory-ring-buffer.md`,
  `docs/architecture/v3-acquisition.md`.

## Purpose

The V3 probe (`scripts/tv46l_hardware_probe.py`) currently concludes that the
TV46L is a "single-stream GigE camera" because it reads
`[Device]DeviceStreamChannelCount = 1`. This investigation tests whether that
conclusion is correct, and whether **simultaneous IR + visible acquisition
without time-slicing** is possible at all, using:

1. V2 reference evidence (dual-handle / rapid-switch / time-sliced / dual-component
   strategies already implemented and run against the TV46L in V2).
2. The camera's GenICam feature map as exposed through HALCON.
3. HALCON 24.11 GigEVision2 documentation (single-stream/channel-0 limitation,
   GenDC/chunk support, `grab_data_async` multi-output capability).
4. Measured facts from `tv46l-characterization.md` (formats, FPS, switch latency).

## Verdict (stop condition)

> **SIMULTANEOUS DUAL-STREAM CAPABILITY UNRESOLVED.**

Evidence gathered in this document shows the TV46L exposes exactly one GigE
stream channel (`DeviceStreamChannelCount = 1`, MEASURED) and that HALCON's
GigEVision2 interface only ever opens the first stream channel of a device
(`hAcqGigEVision2.html`, CONFIRMED).

**Measured on 2026-08-18 (`scripts/tv46l_dual_stream_probe.py`):** the two-handle
experiment failed at the very first step - HALCON 24.11 **refuses to open a
second connection to the camera entirely**, for both the fresh IP
(`169.254.24.69`) and the discovery device string, with HALCON error #5312
("No device matching the specified device string could be found and/or opened").
The IR handle itself verified 640x480 uint16 (614400 B) and `image_contents`
reported a single `"image"` output (no multipart). The camera was restored to
`IR_Data`/16-bit afterwards.

This is a stronger negative result than V2's: V2 could sometimes open a second
IP connection but then observed the `FLK_TI_StreamDataSourceSelector` is
camera-global (the visible handle flipped the whole camera to `VL_Data`, so the
IR handle suddenly delivered RGB). On HB25100004 with HALCON 24.11 the second
connection does not open at all.

**Until a packet-level capture (or a vendor specification) proves the camera can
stream RE + VL providers simultaneously over its single GVSP channel, V3 must
NOT be redesigned to assume simultaneous IR+visible.**

## Confidence legend

- **CONFIRMED** - directly verified by HALCON documentation or a measured run.
- **MEASURED** - value measured on this camera (characterization run 2026-08-17).
- **INFERRED** - derived/consistent with V2 or HALCON behavior, not re-verified
  on HB25100004.
- **UNKNOWN** - not observable in the available evidence.

## A. V2 evidence on IR+visible acquisition (INFERRED until re-run)

V2 implemented four strategies in `reference/TMS_v2/halcon_camera_diagnosis.py`
and ran them against the TV46L:

| Strategy | Mechanism | V2 outcome |
|---|---|---|
| `Baseline` | single handle, `IR_Data` only | reference; IR-only |
| `DualHandle` | two GigE connections, one per stream | **conflict**: camera-global selector shared; visible handle flipping to `VL_Data` made the IR handle deliver RGB; V2 disabled dual-stream |
| `RapidSwitch` | one handle alternating `IR_Data`/`VL_Data` at 250 ms hold | works but is time-slicing; switch latency measurable |
| `TimeSliced` (default) | IR priority with periodic visible bursts (2.0 s IR / 0.6 s visible) | stable IR feed + periodic visible; explicitly time-sliced |
| `DualComponent` | single handle probing for a multi-channel/combined payload | V2 recorded evidence: a multi-channel frame or a "components" parameter would indicate both feeds arrive in one acquisition; none found (V2 fell back to single-channel IR) |

Key V2 code facts:

* `HalconAcquisition` docstring (lines ~270-286): "HALCON's GigEVision2
  interface opens only one stream channel per camera, and the TV46L selects
  IR_Data or VL_Data as that channel's source via the camera-global
  FLK_TI_StreamDataSourceSelector."
* `DualHandleStrategy` opened the IR handle via the discovery device string and
  the visible handle via the camera **IP address**, because "HALCON ... refuses
  to re-open the in-use device string."
* `_verify_visible_stream` compared the visible frame byte size to the IR byte
  size and treated an identical size as "visible handle echoes the IR source",
  dropping the second handle.
* `_ingest` detected "IR frame size changed; stream source conflict" when the
  dual-handle mode caused the IR handle to deliver RGB, and disabled the
  conflicting dual-stream.

**Assessment for V3:** V2's dual-handle attempt on the TV46L family did NOT
yield two simultaneous distinct streams. Re-run on `HB25100004` (2026-08-18,
`scripts/tv46l_dual_stream_probe.py`): the second connection is now refused
outright by HALCON 24.11 (error #5312), a stronger negative than V2 observed.
The camera-global-selector behavior is therefore not even reachable on this
setup; both sources point to "no simultaneous dual-stream via HALCON".

## B. TV46L feature map as exposed through HALCON (MEASURED)

From the characterization run and `docs/Halcon/Halcon_Parameters.md`:

| Feature | Value | Meaning |
|---|---|---|
| `DeviceStreamChannelCount` | `1` | **MEASURED**: one GigE stream channel |
| `FLK_TI_StreamDataSourceSelector` | `IR_Data` / `VL_Data` | camera-global source selection for the one channel |
| `FLK_TI_Info_VLDataProviderAvailable` | `1` | visible-light data provider present |
| `FLK_TI_Info_VLDataSize` | `41943520` | visible data provider buffer size |
| `FLK_TI_Info_REDataProviderAvailable` | `1` | radiometric/IR provider present |
| `FLK_TI_Info_REDataSize` / `FLK_TI_Info_REDataRate` | read in probe | IR provider size/rate |
| `TransmitAs`, `ImageCompressionMode` | present | streaming payload controls |
| `[Stream]StreamID`, `[Stream]StreamType` | present | single stream object |
| `GevCurrentPhysicalLinkConfiguration` | `SingleLink` | single link |
| `image_contents` / `data_contents` | HALCON interface params | HALCON inspects these for multi-output grabs |
| `image_source_id` / `image_region_id` / `image_purpose_id` | present | per-output identifiers (needed for multipart identification) |
| `buffer_frameid` | readable | hardware frame counter (used for sequence in V3) |
| `buffer_timestamp` / `buffer_timestamp_ns` | NOT readable | no hardware timestamp available |
| `device_timestamp_frequency` | `None` | no device timestamp |
| Chunk features (`ChunkModeActive`, `ChunkSelector`, `ChunkEnable`) | NOT in feature map | no chunk-data feature exposure seen |
| `ComponentSelector` / `ComponentEnable` / `DeviceStreamChannelSelector` | NOT in feature map | **no multipart component selection** exposed |

**Assessment:** the feature map shows ONE stream channel with ONE selectable
data source at a time, and no multipart-component features. This is consistent
with a camera that multiplexes two internal providers (RE + VL) onto a single
GigE stream.

## C. HALCON GigEVision2 capability (CONFIRMED)

From `hAcqGigEVision2.html` (HALCON 24.11):

* "Only stream channel 0 supported."
* "Note that the current version of the interface supports only single-stream
  devices and the first available stream is always open. If support for devices
  with multiple image streams will be added in future, the format to open the
  device might be extended with the stream entry."
* The interface supports GigE Vision 1.0-3.0, GenDC streaming, RDMA/RoCEv2,
  chunk data (`ChunkModeActive`, `ChunkSelector`/`ChunkEnable`,
  `[Stream]GevStreamAssumeImageInChunkPayload`), and 3D devices.
* `grab_data` / `grab_data_async` "allow to output arbitrary number of HALCON
  images and also arbitrary number of control data ... suitable for advanced
  use cases when more than just a single HALCON image should be output ...
  when the device outputs multiple images for a single acquisition."
* For single-image use cases `grab_data_async` behaves exactly like
  `grab_image_async`.

**Assessment:** HALCON can consume multi-output (multipart / GenDC) payloads if
the device produces them, but it cannot open more than one stream channel per
device and always opens channel 0. Therefore two distinct *simultaneous* streams
would have to come as a single multipart payload on channel 0 (see D) - HALCON
itself is not the blocker for multipart, the camera is.

## D. Multipart payload possibility (MEASURED as absent)

A single GVSP payload could in principle carry both IR and visible components
(GenDC / extended chunk / multi-part), which is how HALCON's `grab_data_async`
multi-output path would expose them. On HB25100004:

* The acquired IR frame is a single 2D image: `640x480 uint16` 614400 B (MEASURED).
* The acquired visible frame is a single 2D image: `640x480` RGB8 921600 B
  (MEASURED), delivered via `YUV422_8` HALCON pixel format.
* `image_contents` for the current grab returns a single-image logical type
  (V2 DualComponent probe found no multi-channel frame; characterization shows
  single images per acquisition).
* No `ComponentSelector`/`ComponentEnable`/multipart feature in the camera's
  exposed feature map (see B).

**Assessment:** the TV46L does not currently expose a multipart payload carrying
IR+visible together through HALCON. **INFERRED** (based on V2 probe + feature
map); the dual-stream probe should explicitly verify `image_contents` and
`data_contents` after each grab.

## E. Simultaneous IR+visible feasibility (MEASURED: second handle refused)

Paths that could yield true simultaneity, ranked by evidence:

1. **Two framegrabber handles** (V2 `DualHandle`): **MEASURED on 2026-08-18 as
   unavailable** - HALCON 24.11 refuses a second connection to HB25100004
   entirely (error #5312 for both the fresh IP and the device string). V2 could
   sometimes open a second IP connection but then observed the source selector
   is camera-global (both handles deliver the same source). Either way, two
   handles do NOT yield two simultaneous distinct streams.
2. **Multipart payload on channel 0**: no multipart features exposed; single
   2D image per grab (`image_contents == ["image"]`, MEASURED). UNKNOWN at the
   wire level (could not be confirmed without a packet-level capture).
3. **Vendor-proprietary dual-provider mode**: `FLK_TI_Info_*` provider features
   show the camera has both RE and VL providers internally. Whether they can be
   streamed simultaneously over the one GVSP channel is **UNKNOWN** and needs
   either a packet capture or Fluke/vendor confirmation (see K).
4. **Time-slicing** (RapidSwitch / TimeSliced): proven to work but violates the
   NO-time-slicing requirement. CONFIRMED as possible, REJECTED by requirement.

**Conclusion:** no proven path for simultaneous IR+visible without time-slicing.

## F. Measured formats and rates (MEASURED)

From `tv46l-characterization.md`:

| Stream | Format | Bytes/frame | Measured FPS |
|---|---|---|---|
| IR (thermal) | 640x480 uint16, Mono16 | 614400 | ~7.25 (probe path, 500 frames) |
| Visible | 640x480 RGB8 (HALCON YUV422_8) | 921600 | ~8.94 (30 frames) |

* `switch_to_first_frame_s` = **0.9482 s** - switching the camera between
  `IR_Data` and `VL_Data` costs ~0.95 s before a valid frame arrives. This makes
  frequent time-slicing very disruptive and is a key reason the NO-time-slicing
  requirement matters for V3.
* Packet loss 0; `[Stream]GevStreamLostPacketCount` 0 (MEASURED).
* Reconnect (software close/reopen): 3.57 s (MEASURED).
* Note: thermal FPS (7.25) vs visible FPS (8.94) differ because the thermal
  pass includes per-frame parameter reads; the camera's commanded rate is 9 Hz.

## G. Synchronization feasibility (MEASURED / UNKNOWN)

* `buffer_frameid` is readable and provides a hardware frame counter
  (sequence in V3). CONFIRMED.
* No `buffer_timestamp` / `device_timestamp_frequency` on this camera
  (MEASURED). Hardware timestamps are NOT available; V3 frame timestamps must
  come from the acquisition host clock (`time.perf_counter`), which matches
  ADR-002.
* If two streams ever existed, their alignment would rely on frameid/sequence
  correlation, not a shared timestamp base. This is UNKNOWN and unverifiable
  until a simultaneous stream is demonstrated.

## H. HALCON Python interface availability (CONFIRMED)

* Real HALCON Python interface: `mvtec_halcon-24113.0.0` installed in the
  Python 3.10 environment (`C:\Users\admin\AppData\Local\Programs\Python\Python310`).
* `import halcon as ha` exposes `open_framegrabber`, `grab_image_async`,
  `grab_data_async`, `himage_as_numpy_array`, `get_framegrabber_param`, etc.
* `get_system('version')` returns `24.11`.
* **Caveat**: the V3 `.venv` contains a *different* PyPI package also named
  `halcon` (v1.0.0, a documentation search stub) which shadows the real
  interface when the venv Python is used. The probe must be run with the Python
  3.10 interpreter that has `mvtec_halcon` installed (or with the venv fixed).

## I. What the dual-stream probe must test (design)

`scripts/tv46l_dual_stream_probe.py` (built and run 2026-08-18) tests, without
touching production code or persistent camera settings:

1. Open an IR handle (`FLK_TI_StreamDataSourceSelector = IR_Data`, bits 16) and
   verify a 640x480 uint16 frame (614400 B). DONE - verified.
2. Open a visible handle via the camera IP (`VL_Data`, bits -1) while the IR
   handle remains open, and grab concurrently. DONE - **refused (HALCON #5312)**
   for both the fresh IP and the device string.
3. Detect the V2 conflict signature: IR handle byte size changing to visible
   size (camera-global selector flipping). NOT REACHABLE - the second handle
   never opens.
4. Detect the V2 echo signature: visible handle returning the IR byte size
   (second handle delivering the same source). NOT REACHABLE.
5. If distinct streams are observed, measure per-stream FPS, frameid sequence,
   frame intervals, and packet loss over N frames, and confirm they are
   concurrent (overlapping in time), not time-sliced. NOT REACHABLE.
6. Inspect `image_contents` / `data_contents` after each grab to detect any
   multipart/multi-output payload. DONE - `image_contents == ["image"]`, no
   multipart on the IR handle.
7. Restore the camera to IR_Data at the end (never leave `VL_Data` selected).
   DONE - `restored.stream_source_selector == "IR_Data"` in the JSON report.
8. Emit a machine-readable JSON verdict (`reports/hardware/tv46l_dual_stream_<id>.json`).
   DONE.

## J. Recommended V3 architecture (pending this investigation)

* **Do NOT modify** ADR-003 or production acquisition code until the probe
  proves or disproves simultaneity. This document and the probe are the
  gate.
* If the probe reproduces the V2 conflict/echo (expected), V3 keeps:
  - single handle, IR-only acquisition (`stream_source_thermal`), which is the
    current production state;
  - `GrabResult.visible = None` by default;
  - the frame contract with visible as optional (ADR-002 already allows this);
  - visible acquisition only via an explicit, future decision (e.g. time-sliced
    recording), clearly separated from IR acquisition.
* If the probe somehow demonstrates simultaneous distinct streams, V3 would
  need a second framegrabber handle per camera and a new `Frame.visible` path -
  an architecture change to be captured in a follow-up ADR, not assumed now.

## K. Unknowns and vendor/hardware information required

| # | Unknown | Why it matters | How to resolve |
|---|---|---|---|
| 1 | Is `FLK_TI_StreamDataSourceSelector` camera-global or per-connection? | Determines whether two handles can select different sources. V2 says camera-global. | **Partially resolved**: the second handle is refused on HB25100004 (HALCON #5312), so the question is moot via HALCON; still open for ThermoView/proprietary paths |
| 2 | Can the RE and VL providers stream simultaneously over the one GVSP channel? | The core simultaneity question | Packet capture (Wireshark GVSP) while ThermoView runs; vendor (Fluke) spec |
| 3 | How does ThermoView/Fluke software achieve IR+visible simultaneously? | Reference implementation of the intended behavior | Vendor documentation, protocol capture |
| 4 | Does the camera support a multipart / GenDC payload not exposed as GenICam features? | Could unlock `grab_data_async` multi-output | Packet capture + `image_contents` inspection in probe (IR handle reports single `"image"` output, MEASURED) |
| 5 | Is there a vendor SDK/API that opens both providers? | Possible production path if HALCON cannot | Fluke Process Instruments SDK documentation |
| 6 | `FLK_TI_Info_SecuredDataStreamingOnly` semantics | May indicate a proprietary streaming mode with both feeds | Vendor documentation |
| 7 | Actual thermal vs visible sensor timing offset | Needed if simultaneity is ever proven | Not addressable until a simultaneous stream exists |

## References

* `reference/TMS_v2/halcon_camera_diagnosis.py` - V2 dual-handle / rapid-switch /
  time-sliced / dual-component strategies and conflict detection.
* `scripts/tv46l_dual_stream_probe.py` - V3 simultaneous-dual-stream experiment
  (run 2026-08-18, result `second_handle_failed`).
* `reports/hardware/tv46l_dual_stream_cam_probe.json` - machine-readable verdict.
* `reference/TMS_v2/camera/tv46l_camera.py` - V2 production driver (single-handle
  IR, `FLK_TI_StreamDataSourceSelector = IR_Data`).
* `reference/TMS_v2/camera/services/halcon_driver.py` - V2 HALCON service.
* `reference/TMS_v2/halcon_roi_validation.py` - V2 validation path.
* `scripts/tv46l_hardware_probe.py` - V3 characterization tool.
* `docs/hardware/tv46l-characterization.md` - measured formats/FPS/switch latency.
* `docs/Halcon/Halcon_Parameters.md` - TV46L feature map.
* `C:\Program Files\MVTec\HALCON-24.11-Progress-Steady\doc\html\reference\acquisition\hAcqGigEVision2.html` -
  single-stream/channel-0 limitation, GenDC/chunk/multi-output support.
* `C:\Program Files\MVTec\HALCON-24.11-Progress-Steady\examples\hdevelop\Image\Acquisition\gigevision2_2cameras.hdev` -
  multi-handle pattern (separate devices only).
* `src/thermal_monitor/core/frame.py`, `src/thermal_monitor/camera/*.py` - V3
  frame contract and acquisition (unchanged by this investigation).
