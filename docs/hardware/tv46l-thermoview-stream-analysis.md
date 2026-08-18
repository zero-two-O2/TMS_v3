# TV46L ThermoView GVSP Stream Analysis (packet-level)

* Capture file: `docs/Thermoview_PM.pcapng` (99,318,532 B, 64,788 packets, 291.71 s)
* Camera: `169.254.24.69` (GVSP src `:35530`, GVCP `:3956`), MAC `34:08:e1:db:21:f2` (Texas Instruments OUI), DHCP hostname **`flk-ti-am57xx`** -> FLIR camera on TI AM57xx SoC. Matches HB25100004 (TV46L-1-26010002, 9 Hz commanded, firmware 1.0.8).
* PC: `169.254.192.81` (GVSP dst `:50554`, GVCP src `:50535`), MAC `c4:65:16:99:c0:50` (HP), LLDP system name `NB643` (HP ProBook 450 G5, capture host).
* Capture tool: Dumpcap 4.6.6 on the PC itself (nanosecond precision).
* Analysis date: 2026-08-18. Analyzed with Wireshark/tshark 4.6.6 + custom reassembly scripts.

> **Result: the camera transmits a SINGLE GVSP stream that ALTERNATES Mono16 (IR) and YUV422_8 (visible) blocks.** ThermoView displays both simultaneously by buffering the latest of each type. The transport is NOT simultaneous, and it is NOT multipart.

---

## 1. Network topology

```
[TV46L camera]                       [PC - capture host]
  169.254.24.69                        169.254.192.81
  MAC 34:08:e1:db:21:f2            MAC c4:65:16:99:c0:50
  (FLIR, TI AM57xx)            (HP ProBook 450 G5, "NB643")
        |                                   |
        +------- gigabit LLA link ----------+
```

* Link-local (169.254/16) point-to-point, no router, no switches visible.
* One unrelated host (`192.168.42.50`) sent GVCP DISCOVERY broadcasts during the capture; it never communicated with the stream.

## 2. UDP / TCP flows

All traffic in the capture (protocol hierarchy `tshark -z io,phs`):

| Protocol | Frames | Bytes |
|---|---|---|
| GVSP | 64,542 | 97,093,348 |
| GVCP | 198 | 14,784 |
| LLDP | 38 | 5,496 |
| DHCP | 5 | 1,935 |
| ARP | 4 | 204 |
| data (undissected) | 1 | 46 |
| **TCP / HTTP / RTSP / WebSocket / multicast** | **0** | **0** |

**There is exactly ONE GVSP UDP flow.** No TCP of any kind exists in the file, so the visible image cannot be traveling over HTTP/WebSocket/RTSP.

| Source IP | Src port | Dest IP | Dst port | Protocol | Frames | Role |
|---|---|---|---|---|---|---|
| 169.254.24.69 | 35530 | 169.254.192.81 | 50554 | GVSP | 64,542 | image stream (IR + visible) |
| 169.254.192.81 | 50535 | 169.254.24.69 | 3956 | GVCP | ~99 | commands/acks |
| 169.254.24.69 | 3956 | 169.254.192.81 | 50535 | GVCP | ~99 | commands/acks |
| 169.254.24.69 | 35530 | 169.254.24.69 | 50554 | (one UDP) | 1 | `0x55aaaa55` vendor token (PC->camera on stream ports) |

All 236 image blocks share one GVSP source port/dest port pair with one contiguous block-ID sequence — one stream channel.

## 3. GVSP streams

* **One** GVSP stream: `35530 -> 50554`, 236 blocks, 64,542 packets.
* GVSP header layout (verified from raw bytes, big/little-endian mixed):
  * bytes 0-3: **Block ID, 32-bit big-endian** (`00 00 00 01`, `00 00 00 02`, ...) — Wireshark shows low 16 bits as "Block ID (16 bits)".
  * byte 4: **Format**: `0x01` LEADER, `0x02` TRAILER, `0x03` DATA.
  * bytes 5-7: **Packet ID, 24-bit little-endian** (LEADER=0, DATA=1..420, TRAILER=421).
  * bytes 8-9: **Payload Type**, 16-bit LE: always `0x0001` **IMAGE** (never multipart/GenDC/chunk).
* LEADER payload (36 B for IMAGE): Timestamp (8 B, always 0) + Pixel Format (4 B) + Size X (4 B) + Size Y (4 B) + 16 zero padding bytes.
* TRAILER payload: IMAGE trailer carrying Size Y (4 B) + padding; always Packet ID 421, UDP len 24.
* Per-block structure (complete block): LEADER (44 B UDP) + 420 DATA packets + TRAILER (24 B UDP). DATA payload = 1464 image bytes for packets 1..419, 984 for the final packet 420.
* **Image payload per block = 419×1464 + 984 = 614,400 bytes exactly = 640×480×2.**
* No GVSP resend packets (`gvsp.flag.packetresend` = 0 for all), no "previous block dropped" flag, no GVCP RESEND_PACKET requests.

### Capture loss (important caveat)

Only 148 of 236 blocks carried a TRAILER and only 46 blocks were captured completely (420 data packets: 28 Mono16 + 18 YUV422_8). Missing packets are interior gaps (e.g. block 3 captured 266/420 packets). No resends were requested. Because the capture ran on the receiving PC while it also processed the stream (~90 Mbps), the gaps are almost certainly **capture-side drops, not network loss**; the stream itself showed zero loss counters in the HALCON runs of 2026-08-18. Treat the pcap as lossy but the on-wire stream as intact.

## 4. Block timing

Computed from LEADER timestamps across all 236 blocks (`tshark` + script):

| Phase | Blocks | N | Mean interval | Mean rate |
|---|---|---|---|---|
| **Phase 1** (IR only) | 1-31 | 31 | 109.24 ms | **9.15 blocks/s** |
| **Phase 2** (IR + visible) | 32-236 | 205 | 55.55 ms | **18.00 blocks/s** |
| Phase 2, Mono16 (IR) only | — | 100 | 113.36 ms | **8.82 FPS** |
| Phase 2, YUV422_8 (visible) only | — | 105 | 108.97 ms | **9.18 FPS** |

* The reported `115 -> 116 ≈ 50 ms`, `116 -> 117 ≈ 56 ms` reproduces exactly (measured 55.1 ms / 56.3 ms). The exported list was showing **block IDs**, and those blocks alternate IR/visible.
* **The "~18 FPS" is NOT one stream at 18 FPS. It is IR ~9 FPS + visible ~9 FPS alternating on one stream.**

## 5. Payload size

| Block type | Pixel format (leader) | Image bytes | Byte layout |
|---|---|---|---|
| IR (thermal) | `0x01100007` Mono16 | **614,400** (= 640×480×2) | 16-bit LE |
| Visible | `0x02100032` YUV422_8 | **614,400** (= 640×480×2) | Y0 U0 Y1 V0 per 4 B |

Both types are 614,400 bytes — size alone cannot distinguish IR from visible (exactly the ambiguity called out in the brief). The distinction comes from the GVSP LEADER's pixel-format field, not the size.

## 6. Pixel format

Two and only two pixel formats across all 236 leaders:

* `0x01100007` = **Mono16** (GenICam PFNC standard) — 131 blocks.
* `0x02100032` = **YUV422_8** (GenICam PFNC standard) — 105 blocks. (Matches the HALCON fact: visible delivered as `YUV422_8` / RGB8.)

## 7. IR identification (Mono16 = thermal)

Evidence from a fully reassembled complete Mono16 block (block 25; also 128, 177, 189):

* Little-endian 16-bit counts, range **4933-5285**, mean ~5023, std ~61 — raw ADC counts, not temperature-scaled, consistent with an uncalibrated LWIR bolometer.
* Smooth spatial gradient: coarse 8×8 map rises monotonically across the FOV (~5000 near one edge to ~5030 at the other) — a coherent thermal scene, not noise.
* **Temporal stability: frame-to-frame correlation 0.991 over 2.7 s** — a slowly changing thermal scene (heat drifts, nothing moves).
* Frame-to-frame mean abs difference ~6 counts of ~5000 — consistent with bolometer temporal noise on a static scene.
* Format Mono16 in the leader + low-contrast counts + slow drift => this is the radiometric IR (RE) provider.

## 8. Visible identification (YUV422_8 = separate visible sensor)

Evidence from complete YUV422_8 blocks (32, 129, 178, 190):

* Byte-order test: positions 0 and 2 of each 4-byte group are **Y** (std ~76), positions 1 and 3 are **U/V** (std ~5-7) — a genuine YUV 4:2:2 stream (YUYV order), not random data.
* **Colormap-render test:** if the color image were the Mono16 data pushed through a thermal colormap, then V would be a function of Y. Measured conditional std of V|Y = 1.03× the global std of V => **U and V are independent of Y. This rules out a thermal colormap / false-color render.**
* Chroma is low-variance and centered near neutral (U ~126, V ~123) — a muted real scene.
* **Temporal stability: correlation 0.9987 over 2.7 s** — static visible view.
* Cross-modal: Mono16 and Y-channel are uncorrelated (raw and edge correlation ≈ -0.06 to -0.08 at both full and coarse 8×8 scale, and after shift search) => the visible image content is **not** the thermal content rendered; it is a separate visible-light view (different scene/FOV relationship not determinable without visual inspection; this model cannot view rendered frames).

## 9. Multipart / component analysis

**None.**

* Every leader reports Payload Type IMAGE (`0x0001`); Wireshark's `gvsp.numparts` is never set; no GenDC container signature, no `ComponentID`/`PartID`/`DataType`/`ComponentSelector` fields, no chunk data.
* Reassembled payloads are a single contiguous 614,400-byte raster per block with no internal part headers.
* The `miR` block read via READMEM (addresses `0xa104`, `0xa31c`) is vendor **calibration data** (IEEE-754 coefficient tables), not a stream component.

## 10. Alternation analysis

* Phase 1 (blocks 1-31): IR only, Mono16.
* Phase 2 (blocks 32-236): strict **IR, VL, IR, VL, ...** alternation. Run-length analysis of the 205-block phase: 100 single-IR runs, 99 single-VL runs, plus 2 glitches (one 4-VL run at blocks 74-77, one 2-VL run at 200-201). During a glitch the visible source is delivered twice and the IR source is skipped — the two providers share one ~18 Hz transport scheduler.
* Consequence: an IR frame and the adjacent visible frame are ~55 ms apart in time. They are **not** simultaneous acquisitions.

## 11. Control traffic (GVCP)

Complete GVCP sequence (PC `:50535` <-> camera `:3956`):

| t (s) | Cmd | Register | Value | Meaning |
|---|---|---|---|---|
| 0.00-2.25 | DISCOVERY | — | — | discovery + version |
| 1.03 | READREG | spec/control | — | probe |
| 3.25 | WRITEREG | `0x0a00` (CCP) | 1 | acquire control-channel privilege |
| 3.25 | WRITEREG | `0x0938` | 3000 | control timeout/packet-size config |
| 3.26 | WRITEREG | `0x0d08` | 0 | stream channel config |
| 3.26 | WRITEREG | `0x0d18` | `0xa9fec051` = 169.254.192.81 | **GVSP destination IP** |
| 3.26 | WRITEREG | `0x0d00` | `0xc57a` = 50554 | **GVSP destination port** |
| 3.26-3.46 | READMEM | `0xa104` / `0xa31c` | — | read `miR` calibration tables |
| 3.41 | WRITEREG | `0x10a110` | **1** | **data source = IR only** |
| 3.41-3.46 | WRITEREG | `0x20a134` | 17 → 1 → 12 | VL/provider select config |
| 3.46 | WRITEREG | `0x20a138` | 1068 | provider size/rate config |
| 5.68 | WRITEREG | `0x10a104` | **1** | **stream enable** (stream starts) |
| 5.68 | (undissected) | — | `0x55aaaa55` | vendor "start" token on stream channel |
| 7.03/7.07 | WRITEREG | `0x20a148` | 1 → 2 | provider config (no stream change) |
| **8.86** | **WRITEREG** | **`0x10a110`** | **2** | **data source = IR + visible (dual)** |
| 9.02 | — | — | — | **block 32: first visible frame; alternation begins** |
| 9.31-20.31 | READREG | heartbeats | — | ~1 Hz keep-alive |
| 20.36 | WRITEREG | `0x10a108` | 1 | shutdown config |
| 20.78-20.78 | WRITEREG | `0x0d18`=0, `0x0d00`=0, `0x0a00`=0 | — | clear dest IP/port, release CCP |
| 24.9-280.9 | DHCP | — | — | camera idle, DHCP DISCOVER retries |

Key correlation: **writing `0x10a110 = 2` at t=8.857 is immediately followed (block 32, t=9.020) by the start of IR/visible alternation.** Value 1 = IR-only source, value 2 = dual (IR + visible) source. This is the wire-level equivalent of the `FLK_TI_StreamDataSourceSelector` behavior known from V2 (`IR_Data` / `VL_Data`), but the write targets a numeric dual mode rather than the two string values HALCON exposes.

## 12. ThermoView / web traffic

No ThermoView-specific protocol, no HTTP, no RTSP, no WebSocket — nothing beyond GVCP/GVSP + one `0x55aaaa55` vendor token. ThermoView achieves "simultaneous" display purely by consuming the alternating stream and buffering the latest IR + latest visible frame.

## 13. Final architecture classification

**C. ALTERNATING GVSP FRAMES** (single GVSP stream, IR and visible blocks alternate on the wire).

* Not A — there is only one GVSP flow, one channel, one block-ID sequence.
* Not B — no multipart/GenDC/component payload; each block is one complete single image.
* Not D — the visible image is on the same GVSP stream (no other transport exists).
* Not E — both images originate from the camera's own RE + VL providers.
* The camera time-slices its own dual acquisition onto one GVSP channel (~18 blocks/s = ~9 IR + ~9 visible). **This is transport-level alternation, not sensor-level simultaneity.** ThermoView reconstructs the illusion of simultaneity by buffering.
* This fully explains the earlier HALCON measurement: HALCON sees IR frames arriving at ~9 FPS because that is the IR block rate.

## 14. Evidence

* tshark commands used:
  - `tshark -r docs/Thermoview_PM.pcapng -q -z io,phs` (protocol inventory)
  - `tshark -r docs/Thermoview_PM.pcapng -Y "gvsp.format==0x01" -T fields -e frame.time_relative -e gvsp.blockid16 -e gvsp.pixel -e gvsp.sizex -e gvsp.sizey` (leader table)
  - `tshark -r docs/Thermoview_PM.pcapng -Y "gvsp" -T fields -E separator=, -e ... gvsp.blockid16 gvsp.packetid24 udp.length ...` (full packet export -> 64,542-row CSV)
  - `tshark -r docs/Thermoview_PM.pcapng -Y "gvcp" -T fields -E separator=, -e ... gvcp.cmd.command gvcp.cmd.writereg.bootstrapregister gvcp.cmd.writereg.data ...` (control export)
  - `tshark -r docs/Thermoview_PM.pcapng -Y "frame.number==136" -x` (leader hexdump)
  - `tshark -r docs/Thermoview_PM.pcapng -Y "frame.number==557" -x` (trailer hexdump)
* Raw leader bytes, block 1 (frame 136): `00000001 01 000000 0001 | 0000000000000000 | 01100007 | 00000280 | 000001e0 | 00000000000000000000` => block 1, LEADER, IMAGE, Mono16, 640x480.
* Raw leader bytes, block 2 (frame 561): `00000002 01 000000 0001 | ...` — block ID increments, all else identical.
* Raw trailer bytes, block 1 (frame 557): `00000001 02 01a500 0001 | 000001e0 0000` => TRAILER, packet 421, IMAGE, Size Y 480.
* Block 115 = YUV422_8 at t=13.630961, block 116 = Mono16 at t=13.686026 (+55.1 ms), block 117 = YUV422_8 at t=13.742309 (+56.3 ms) — reproduces the reported interval and shows alternation.
* Reassembled complete blocks: block 25 = 614,400 B Mono16 (420 pkts), block 32 = 614,400 B YUV422_8 (420 pkts); pixel statistics and colormap test as described in sections 7-8.
* GVCP write `0x10a110=1` at t=3.413 (IR-only), `0x10a110=2` at t=8.857 -> first YUV block at t=9.020.

## 15. What V3 must implement

Do not implement any of this yet — record as architecture input (ADR-003 and acquisition code remain untouched):

1. **Treat the TV46L as one GVSP channel carrying two multiplexed providers.** Acquisition must consume the alternating stream and demultiplex by reading each block's GVSP leader pixel format: `0x01100007` -> Mono16 IR frame, `0x02100032` -> YUV422_8 visible frame. Both are 640×480, 614,400 B.
2. **Dual-source activation is a control-channel write, not a HALCON string feature.** The camera enters dual mode via a numeric register write (`0x10a110 = 2`; `1` = IR only). HALCON exposes `FLK_TI_StreamDataSourceSelector` only as the strings `IR_Data`/`VL_Data` (camera-global, and a second handle is refused on this camera). V3 must verify whether HALCON can reach the dual mode; if not, V3 needs a raw GVCP WRITEREG path or a vendor SDK for that one register.
3. **Frame contract impact:** a GVSP block is a *single* image (IR or visible), never both. The frame model (ADR-002) must be able to tag which kind each raw frame is; visible remains optional and arrives at its own ~9 Hz cadence.
4. **No second framegrabber handle, no multipart.** Both prior V2 strategies (dual handle, multipart probing) are ruled out by the wire evidence. Any redesign that assumes two simultaneous transport streams or a combined payload is wrong.
5. **Timing:** with dual mode active, expect IR ~9 FPS + visible ~9 FPS alternating (~18 blocks/s, ~55 ms apart); IR-only mode is ~9 FPS. IR and visible frames from the same scene are ~55-110 ms apart — there is no shared timestamp (leader timestamps are always 0), so V3 must use the acquisition host clock and sequence numbers (consistent with ADR-002).
6. **Bandwidth:** ~46 Mbps (IR only) / ~92 Mbps (dual). Fine for gigabit.
7. **Loss:** expect a clean on-wire stream; implement or rely on GVCP resend only if V3 needs absolute completeness. The pcap's gaps are capture-side artifacts on the receiving PC.

## References

* `docs/hardware/tv46l-dual-stream-investigation.md` — prior HALCON/GenICam-level investigation ("SIMULTANEOUS DUAL-STREAM CAPABILITY UNRESOLVED").
* `docs/hardware/tv46l-characterization.md` — measured formats/FPS/switch latency.
* `reference/TMS_v2/halcon_camera_diagnosis.py`, `reference/TMS_v2/camera/tv46l_camera.py` — V2 IR/VL strategies.
* `docs/decisions/ADR-002-frame-model.md`, `docs/decisions/ADR-003-shared-memory-ring-buffer.md` — frame contract (unchanged).
* `docs/Thermoview_PM.pcapng` — this capture.