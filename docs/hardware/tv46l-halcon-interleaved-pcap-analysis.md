# TV46L HALCON Interleaved IR/VL PCAP Analysis

* Capture: `docs/Halcon_Interleaved_IR_VL_Data.pcapng` (2026-08-18 12:36:44..12:37:57, 72.923 s, 1769 packets, 359,976 B, SHA256 `e2ffb2928c36db2c01b6221aae3bac48054cabe94e15ef0ef63b1881c1bd7e45`).
* Method: Wireshark 4.6.6 (Dumpcap, nanosecond precision, no capture filter), manual capture while HALCON was configured for interleaved IR/VL data.
* Camera: `169.254.24.69` (GVCP `:3956`), MAC `34:08:e1:db:21:f2`. PC: `169.254.192.81`, MAC `c4:65:16:99:c0:50`.

> **Verdict: C — THE HALCON PCAP DOES NOT CONTAIN TRUE IR/VL INTERLEAVED GVSP.**
>
> The capture contains **zero GVSP image blocks** (no LEADER/DATA/TRAILER, no Mono16 `0x01100007`, no YUV422_8 `0x02100032`). HALCON's own GVCP session (PC:64795) wrote the dual-mode register **`0x10a110 = 2`** at `t=21.6128` (frame 1109) and the camera **ACKed success** (frame 1110), then HALCON re-configured and enabled the stream channel ~14 times, but **the camera never delivered a single image packet** on any attempt. HALCON is not receiving any GVSP stream in this capture.

---

## 1. TL;DR

| Question | Answer |
|---|---|
| Native interleaved IR/VL GVSP received by HALCON? | **No** — zero GVSP image blocks on the wire |
| IR (Mono16) data present? | No |
| Visible (YUV422_8) data present? | No |
| Alternation / FPS? | Not applicable (0 frames) |
| Dual-mode register write | `0x10a110 = 2` **by HALCON itself**, ACK success |
| Does this pcap prove HALCON can receive interleaved IR/VL? | **No** — the interleaved data is not on the wire |

The pcap *does* prove something new and important: **HALCON can write the dual-mode register `0x10a110=2` on its own GVCP channel and the camera accepts it.** This contradicts the earlier `tv46l-halcon-dual-mode.md` finding that HALCON's provider resets the register to `0`. The conclusion "HALCON cannot receive the dual stream" still stands, but for a different reason: the camera simply does not stream to HALCON even after the register is set.

---

## 2. Evidence that there is no image data

### 2.1 Protocol hierarchy accounts for every packet

| Protocol | Packets |
|---|---|
| frame / eth | 1769 |
| lldp | 10 |
| ip | 1749 |
| udp | 1749 |
| gvcp | 1726 |
| gvsp | **22** (all proprietary keep-alive / negotiation) |
| dhcp | 1 |
| arp | 10 |

All 1749 UDP packets are accounted for. There is **no image traffic of any kind** (not even a non-GVSP image flow).

### 2.2 All 22 "GVSP" frames are proprietary, not image data

| Type | Count | Direction | Payload |
|---|---|---|---|
| `stream_keep_alive` | 18 | PC:64797 → camera:&lt;SCSP0&gt; | ASCII `stream_keep_alive\0`, 26 B |
| packet-size negotiation probe | 4 | camera:36696 → PC:64797 | proprietary; sizes 1068/1332/1464/1472 B |

* None has a valid GVSP format byte (0x01/0x02/0x03). Wireshark reports `Unknown Format (0x0)` / `(0x61)`.
* Zero LEADER, zero DATA, zero TRAILER packets. No Mono16 or YUV422_8 pixel format anywhere.
* The 4 probes exactly match the SCPS0 packet-size negotiation sizes negotiated at `t=16.41..17.30` (1068+28=1096, 1332+28=1360, 1464+28=1492, 1472+28=1500 ≈ final SCPS0 0x05DC) — they are the camera echoing the negotiated packet size, not frames.
* Keep-alive destination ports equal the camera's SCSP0 readbacks (36696, 0xA372=41842, 0xB5E1=46561, 0x95AD=38317, …), proving the keep-alives target the camera's *live* stream source port.

### 2.3 Frame-length distribution

Dominant sizes are GVCP control frames (54/58/60/566 B). The only frames ≥ 1.1 KB are the four negotiation probes (max 1514 B). A 640×480 image would need ~420 GVSP data packets per frame; there are zero.

---

## 3. The GVCP sequence (what HALCON actually did)

All 137 WRITEREG commands originate from **PC:64795 = HALCON's own control channel** (same port as all node-map READMEM and heartbeat reads). No external process wrote anything.

| t (s) | Register | Name | Value |
|---|---|---|---|
| 16.394562 | `0x0a00` | CCP acquire | 1 |
| 16.403465 | `0x0954` | GVCP Configuration | 0 |
| 16.404663 | `0x0960` | GVSP Configuration (64-bit block ID) | `0x40000000` |
| 16.405764 | `0x0d24` | SCCONF0 | 0 |
| 16.410446 | `0x0d18` | SCDA0 (dest addr) | `0xa9fec051` (PC) |
| 16.410787 | `0x0d00` | SCP0 (dest port) | `0xfd1d` (64797) |
| 16.41–17.30 | `0x0d04` | SCPS0 packet-size negotiation | `0x4000xxxx`/`0xC000xxxx` alternating → final `0x400005DC`/`0x400005E0` (~1500) |
| 17.297247 | `0x0d04` | SCPS0 final | `0x400005DC` |
| 17.303889 | `0x0d00` | SCP0 teardown | 0 |
| 17.305500 | `0x0d18` | SCDA0 teardown | 0 |
| **21.612786** | **`0x10a110`** | **DATA SOURCE = IR + VISIBLE (DUAL)** | **2** ✅ ack 0x0000 |
| 21.630915 | `0x0d18` | SCDA0 re-arm | `0xa9fec051` |
| 21.631194 | `0x0d00` | SCP0 re-arm | `0xfd1d` |
| 21.632310 | `0x10a104` | stream enable | 1 |
| 21.660469 | `0x10a108` | stream stop / shutdown config | 1 |
| 21.660832 | `0x0d00` | SCP0 | 0 |
| 21.661204 | `0x0d18` | SCDA0 | 0 |
| 27.97–37.19 | — | ~14× repeat: SCDA0/SCP0 set, SCSP0 read, keep-alive, `0x10a104=1`, `0x10a108=1`, teardown | — |
| 62.954649 | `0x10a108` | final stream stop | 1 |
| 62.954941 | `0x0d00` | SCP0 | 0 |
| 62.955185 | `0x0d18` | SCDA0 | 0 |
| 72.921763 | `0x0a00` | CCP release | 0 |

Key register reads around the dual write (all from HALCON, port 64795):

| t (s) | Register | Value |
|---|---|---|
| 17.816847 | `0x10a110` | **0** (before dual write) |
| 21.613300 | `0x20a154` | 9 |
| 21.613706 | `0x10c1c4` | 1 |
| 21.614075 | `0x10c1a4` | 1 |
| 21.614379 | `0x10c1a8` | 2 |
| 21.615395 | `0x10c1a0` | 1 |
| 21.615735 | `0x10c120` | 1 |
| 21.617768 | Heartbeat timeout | `0x0BB8` |
| 21.618378 | SCPS0 | ~`0x05DC` |
| 21.620113 | `0x20a13c` | `0x3B5` |
| 21.620526 | `0x20a140` | `0x96` |
| 21.620868 | `0x20a144` | `0xF4240` |

### Interpretation

HALCON:
1. Opens the camera: reads the GenICam node-map XML via `READMEM` (`0xff00xxxx..0xff02xxxx`), holds CCP.
2. Configures the stream channel (SCDA0/SCP0/SCPS0 negotiation, GVSP config with **64-bit block ID**, SCCONF0=0) and tears it down again.
3. **Writes `0x10a110 = 2` (dual mode) — accepted.**
4. Re-arms the channel and enters a ~14-cycle start/stop loop (`0x10a104=1` then `0x10a108=1` ~28 ms later, then SCP0/SCDA0=0). The camera sends **no image data** in any cycle.

The ~28 ms enable→stop gap and the repetition is consistent with HALCON's grab returning no data each time (grab timeout/error retry loop), not with a working stream.

---

## 4. Comparison with the ThermoView capture

Both captures use the same GVCP write code (`0x0082`), the same camera, and both write `0x10a110=2`.

| Aspect | ThermoView (`Thermoview_PM.pcapng`) | HALCON (this pcap) |
|---|---|---|
| Image data on wire | **64,542 GVSP frames / 236 blocks** (Mono16 + YUV422_8 alternating) | **0 frames** |
| `0x10a110=2` write | yes | yes (HALCON itself) |
| Heartbeat `0x0938` | writes `0x0BB8` | reads only |
| `0x0d08` SCPD0 packet delay | writes 0 | not written |
| `0x20a134` data-source selector | writes `0x11→0x01→0x0c` | **reads only** |
| `0x20a138` | writes `0x042c` | reads only |
| `0x20a148` | writes `1→2` | reads only |
| `0x0960` GVSP config (64-bit block ID) | not written (32-bit default) | writes `0x40000000` (64-bit) |
| `0x0954` GVCP config | not written | writes 0 |
| `0x0d24` SCCONF0 | not written | writes 0 |
| SCPS0 negotiation | not present | `0x4000xxxx`/`0xC000xxxx` loop → 1500 |
| Stream keep-alive | `0x55aaaa55` vendor token (PC→camera) | `stream_keep_alive\0` ASCII (PC→camera) |
| SCP0 | `0xc57a` (50554) | `0xfd1d` (64797) |

**Decisive difference:** ThermoView actually receives image data; HALCON receives none. The most probable causes are that HALCON never writes the provider configuration (`0x20a134`/`0x20a138`/`0x20a148`) that ThermoView writes, and/or the 64-bit block-ID setting (`0x0960=0x40000000`) is incompatible with the camera's dual-source output engine even though the write is ACKed.

---

## 5. What HALCON actually exposes (STEP 11 correlation)

From the app-side tool `scripts/tv46l_dual_mode_halcon_test.py` and its 11:25 run report `reports/hardware/tv46l_dual_mode_halcon_cam_0004.json` (preopen mode):

* HALCON delivered **only Mono16 IR frames** at ~7.5 FPS (`226 IR`, `0 visible`) even with `0x10a110=2` written before open.
* `FLK_TI_StreamDataSourceSelector` exposes only `IR_Data`/`VL_Data` (single-source strings). No dual-source value.
* In *this* 12:38 capture HALCON wrote `0x10a110=2` itself, but the wire shows **zero frames of any format** — even IR.

So the app-side picture and the wire picture agree: **HALCON never surfaces IR+visible alternation.** The difference between the two runs (IR frames in the 11:25 run vs. nothing in the 12:38 run) is likely a configuration difference (e.g. `IR_Data` selected in the 11:25 run vs. the interleaved attempt in the 12:38 run), which itself is a useful data point: in interleaved configuration HALCON gets nothing at all.

---

## 6. Recommended next experiment

Capture ThermoView and HALCON sessions back-to-back on the same camera/link with Wireshark and diff the exact WRITEREG/READREG streams:

1. Record ThermoView's `0x20a134`/`0x20a138`/`0x20a148` values and timing relative to `0x10a110=2`.
2. After writing `0x10a110=2`, re-read `0x10a110` (with CCP held) to confirm it stays `2`.
3. Test writing `0x0960 = 0` (32-bit block ID) on HALCON's channel before stream enable.
4. Test writing the `0x20a1xx` provider registers that ThermoView writes, either from a minimal GVCP client on the same session or before opening HALCON.
5. Optionally verify whether HALCON requires 64-bit block IDs for its own receive path (it sets `0x0960=0x40000000` unconditionally here).

---

## 7. Files

* `reports/hardware/tv46l_halcon_interleaved_pcap_analysis.json` — machine-readable verdict (this analysis).
* `docs/hardware/tv46l-thermoview-stream-analysis.md` — the working dual-mode GVSP evidence (ThermoView).
* `docs/hardware/tv46l-halcon-dual-mode.md` — prior HALCON dual-mode experiment (register-reset claim now contradicted by this capture's own `0x10a110=2` write).
* `reports/hardware/tv46l_dual_mode_halcon_cam_0004.json` / `scripts/tv46l_dual_mode_halcon_test.py` — HALCON app-side grab results.
* `docs/Halcon_Interleaved_IR_VL_Data.pcapng` — the capture analyzed here.
