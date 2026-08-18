# TV46L Dual-Mode GVSP vs HALCON: Can HALCON receive the dual-source stream?

* Experiment date: 2026-08-18.
* Tool: `scripts/tv46l_dual_mode_halcon_test.py` (standalone diagnostic, NOT production code).
* Camera: `169.254.24.69` (GVCP `:3956`, GVSP `:35530`), MAC `34:08:e1:db:21:f2`, model `TV46L-1-26010002@9Hz`, serial `HB25100004`, firmware `1.0.8`. Device string `3408e1db21f2_FlukeProcessInstruments_TV46L1260100029Hz`.
* PC: `169.254.192.81`, HALCON 24.11 (Progress Steady), `mvtec_halcon` 24113.0.0, system Python 3.10.
* Evidence source: `docs/hardware/tv46l-thermoview-stream-analysis.md` (ThermoView packet capture) plus new live GVCP/GVSP tests in this session.

> **Result: HALCON 24.11 CANNOT receive the TV46L dual-source GVSP stream through a single GigEVision2 framegrabber handle.** Two independent blockers, each verified on the wire:
> 1. HALCON's provider holds the GVCP Control Channel Privilege (CCP) for the whole time its handle is open. Any external `WRITEREG 0x10a110 = 2` (the dual-mode register) is refused with GVCP status `0x8006` (BAD_ALIGNMENT) — verified while HALCON was streaming.
> 2. Even when the register is written to dual mode (`0x10a110 = 2`) BEFORE HALCON opens, HALCON's provider reset the register back to single-source (`0x10a110 -> 0`, source = `VL_Data`) during `open_framegrabber`. HALCON then delivered only ONE format (visible YUV422_8), never IR+visible alternation.
>
> V3 must not build its dual-IR+visible acquisition on a single HALCON handle. Either the FLIR vendor SDK/GVCP register path (independent of HALCON's provider) or a redesigned acquisition is required.

---

## 1. Background and question

`docs/hardware/tv46l-thermoview-stream-analysis.md` established from the ThermoView packet capture that the camera transmits a **single** GVSP stream in which IR (Mono16) and visible (YUV422_8) **alternate** block by block, and that the camera enters that dual mode via the GVCP control-channel write `WRITEREG 0x10a110 = 2` (`1` = IR only, `2` = IR + visible). ThermoView holds one GVSP flow and shows both images by buffering.

Open question for V3: **can HALCON 24.11 consume that same single dual-mode stream and deliver both IR and visible frames through one `GigEVision2` handle?** HALCON's `FLK_TI_StreamDataSourceSelector` exposes only the enum strings `IR_Data` / `VL_Data` (single-source), and HALCON has no operator for a raw GVCP register write, so the dual-mode register write must come from a minimal GVCP client external to HALCON.

## 2. Method

`scripts/tv46l_dual_mode_halcon_test.py` implements:

* A **minimal GVCP client** (DISCOVERY, READREG, WRITEREG, CCP acquire/release) targeting exactly register `0x10a110`.
* A **single HALCON handle** experiment in two orders:
  * `live` — open HALCON (IR_Data, 16-bit), start grabbing, then attempt the GVCP dual write while HALCON streams (mirrors ThermoView's "write while streaming" behavior).
  * `preopen` — write the dual-mode register via GVCP first, then open HALCON and grab.
* Per-frame classification by PFNC pixel-format ID (`0x01100007` Mono16 IR, `0x02100032` YUV422_8 visible), grab timings, and a JSON verdict in `reports/hardware/tv46l_dual_mode_halcon_*.json`.
* The camera is always restored to IR-only (`0x10a110 = 1`, selector `IR_Data`) in `finally`; the restore is verified while holding CCP.

### GVCP/register facts measured in this session

All raw GVCP behavior below was verified byte-for-byte against the camera with the GVCPClient above:

| Operation | With CCP held | Without CCP |
|---|---|---|
| `READREG 0x10a110` | returns real value (`0`, `1`, or `2`) | returns **masked `0`** |
| `WRITEREG 0x10a110 = 0/1/2` | ack status `0x0000` (success), readback equals written value | ack status `0x8006` (BAD_ALIGNMENT), write rejected |
| `WRITEREG 0x0A00 = 1` (acquire CCP) | — | grants CCP (status `0x0000`) when camera is idle |

So `0x8006` on a register write is the camera's way of saying "no CCP" (not a true alignment error), and reads without CCP are silently masked to `0`. This is why the naive first `--write-only` attempt failed and why every read must happen while holding CCP.

## 3. Experiment 1 — `live` mode (open HALCON first, then write dual)

Sequence: open one HALCON handle -> grab one IR frame (baseline) -> GVCP `WRITEREG 0x10a110 = 2` -> keep grabbing on the same handle for 30 s.

Result (from `reports/hardware/tv46l_dual_mode_halcon_cam_0004.json`):

* `ccp_status: 32774` — **CCP acquire refused while HALCON's handle is open**.
* `write_status: 32774`, `initial_read` and `readback` both `READREG ... status 0x8006` — **the dual write never reached the camera**; all GVCP register traffic from a second controller is refused.
* HALCON kept delivering IR Mono16 frames at ~7.5 FPS for the full 30 s (`224 IR`, `0 visible`), unaffected by the failed write.
* `classification: gvcp_write_failed`.

Interpretation: HALCON's GigEVision2 provider takes exclusive CCP for the camera while its handle is open and does not share it. A separate GVCP controller cannot write `0x10a110` in `live` order. This matches the long-standing V2 finding that a second GigE connection on this camera family is refused/conflicting.

## 4. Experiment 2 — `preopen` mode (write dual first, then open HALCON)

Sequence: GVCP `WRITEREG 0x10a110 = 2` (verified readback `2`) -> open HALCON -> grab 30 s.

Result (from the same report; run 2):

* The dual write itself **succeeded** on the wire (`ccp_status: 0`, `write_status: 0`, `value_after_write: 2`).
* HALCON opened, then delivered **only Mono16 IR frames** (`226 IR`, `0 visible`, ~7.5 FPS), same as IR-only behavior.
* `classification: halcon_single_format_only`.

The decisive follow-up test (raw handle, no `FLK_TI_StreamDataSourceSelector` set, no grab, close immediately):

* After writing `0x10a110 = 2`, HALCON reported `FLK_TI_StreamDataSourceSelector = VL_Data` on open, and after close the register read back **`0`** (not `2`).
* So HALCON's `open_framegrabber` **reset the dual-mode register to single-source** (`0x10a110 -> 0`). The pre-open dual write does not survive HALCON's provider initialization.
* Grabbing after that reset (same raw-handle test, 20 s) delivered **only visible YUV422_8** frames (`170 VIS`, `0 IR`, ~8.4 FPS) — HALCON follows the provider-selected single source, which after the reset is `VL_Data` (visible).

Net: regardless of ordering, HALCON's single handle ends up delivering exactly ONE format. The dual-mode alternation the ThermoView pcap proves on the wire is not reachable through HALCON's provider.

## 5. Why this is consistent with the ThermoView capture

The ThermoView capture shows the alternation because ThermoView is its own GVCP/GVSP client: it holds CCP, writes `0x10a110 = 2`, and consumes the single alternating GVSP flow itself. HALCON is a *different* controller: it grabs CCP and re-configures the camera's stream-source register to a single source on open. There is no way to keep `0x10a110 = 2` active while a HALCON handle is open, and no way to write it while it is open — so the dual-mode GVSP stream is effectively invisible to HALCON.

## 6. Consequences for V3 architecture

* Do **not** design V3 dual IR+visible acquisition around a single HALCON `GigEVision2` handle — the provider cannot expose the alternating dual-mode stream.
* The two candidate paths for V3 that remain consistent with the wire evidence:
  1. **Vendor/GVCP path**: a minimal GVCP (or FLIR SDK) receiver that itself holds CCP, writes `0x10a110 = 2`, and consumes the single alternating GVSP flow (ThermoView-style). It must NOT coexist with an open HALCON handle on the same camera, because HALCON grabs CCP and resets the register.
  2. **Keep HALCON single-source** (IR-only today) and treat visible acquisition as a separate concern — matching what V2 actually shipped (visible handle conflicts were the historical reason dual-stream was disabled).
* HALCON remains usable for IR-only acquisition; the blocker is specifically the dual-source/visible mode.
* The minimal GVCP client pattern (register `0x10a110`, CCP acquire before any read/write, masked reads without CCP, `0x8006` = no-CCP) is a proven reference for any future GVCP work on this camera.

## 7. Files

* `scripts/tv46l_dual_mode_halcon_test.py` — the experiment (modes `live`/`preopen`, `--write-only`, `--read-register`, `--list-devices`; always restores IR-only).
* `reports/hardware/tv46l_dual_mode_halcon_cam_0004.json` — machine verdict for the runs above.
* `docs/hardware/tv46l-thermoview-stream-analysis.md` — the packet-level evidence that the dual stream exists on the wire and how it is activated.
* `docs/hardware/tv46l-dual-stream-investigation.md` — prior two-handle investigation (complementary: second handle refused/conflicting).
* `reference/TMS_v2/` — V2 HALCON driver and diagnosis tools (read-only reference for single-source behavior).