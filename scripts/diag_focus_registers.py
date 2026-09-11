#!/usr/bin/env python3
"""diag_focus_registers.py -- raw GVCP focus-register diagnostic (read-only).

Uses the production :class:`GVCPClient` wire encoding directly against a
real TV46L while GVSP acquisition keeps streaming. READ-ONLY by design:

* binds a UDP socket, performs NO CCP switchover (CCP stays with the
  streaming CustomTV46LDriver; reads do not need control privilege),
* performs NO writes (focus SET is tested through the UI Apply path once
  reads are understood, so control ownership is never disturbed).

For each focus register it prints the outcome class from the task:

  A) valid value   -- bug is above the driver (worker/Qt/UI)
  B) camera error  -- exact GVCP status code is printed
  C) timeout       -- elapsed time is printed
  D) parse nonsense -- raw response hex is printed

Usage (while the V3 app streams the camera)::

    python scripts/diag_focus_registers.py --camera 192.168.42.11
    python scripts/diag_focus_registers.py --camera 192.168.42.18 --repeats 3

Registers probed (proven map, mm, uint32 BE, no scaling):

    CURRENT = 0x20A13C, MIN = 0x20A140, MAX = 0x20A144

Optional write test (opt-in only)::

    python scripts/diag_focus_registers.py --camera 192.168.42.11 --set 500 --take-control

    1. reads CURRENT (original focus, e.g. 917),
    2. attempts CCP switchover (key=2). If the streaming V3 app holds
       control, the camera is expected to DENY this -- that denial is
       itself evidence (it proves the app owns CCP) and the script exits
       without touching anything,
    3. on grant: writes SET=0x20A138, polls CURRENT every 0.2 s up to
       5.0 s (proven settle behavior), prints every poll + raw packets,
    4. writes back the ORIGINAL value, verifies the restore readback,
    5. releases CCP (writes 0).

    GVSP streaming continues throughout (the stream channel does not need
    CCP). After a granted write test, re-check focus Read in the V3 app:
    if its control ops complain, reconnect that camera (control-only).
"""

from __future__ import annotations

import argparse
import logging
import struct
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from thermal_monitor.camera.tv46_gvcp import (  # noqa: E402
    CCP_KEY,
    GVCPCommand,
    GVCPError,
    REG_CCP,
    REG_FOCUS_CURRENT,
    REG_FOCUS_MAX,
    REG_FOCUS_MIN,
    REG_FOCUS_SET,
    GVCPClient,
    build_request,
    parse_ack_header,
    routed_local_ip,
)

REGISTERS = (
    ("CURRENT", REG_FOCUS_CURRENT),
    ("MIN", REG_FOCUS_MIN),
    ("MAX", REG_FOCUS_MAX),
)


def probe(client: GVCPClient, name: str, address: int) -> dict:
    """One raw register read with full evidence. Never raises."""
    req_id = client._next_req_id()
    request = build_request(GVCPCommand.READ_REG, struct.pack(">I", address), req_id)
    t_send = time.perf_counter()
    try:
        resp = client._send_recv(request, GVCPCommand.READ_REG_ACK)
    except GVCPError as exc:
        elapsed = time.perf_counter() - t_send
        return {
            "register": name,
            "address": f"{address:#010x}",
            "outcome": "C/TIMEOUT-or-B/ERROR",
            "request_hex": request.hex(),
            "error": str(exc),
            "elapsed_s": round(elapsed, 3),
        }
    elapsed = time.perf_counter() - t_send
    try:
        status, ack_cmd, ack_id = parse_ack_header(resp)
    except ValueError as exc:
        return {
            "register": name,
            "address": f"{address:#010x}",
            "outcome": "D/PARSE",
            "request_hex": request.hex(),
            "response_hex": bytes(resp).hex(),
            "error": f"ack header unparsable: {exc}",
            "elapsed_s": round(elapsed, 3),
        }
    if status != 0:
        return {
            "register": name,
            "address": f"{address:#010x}",
            "outcome": f"B/CAMERA-ERROR status={status:#06x}",
            "request_hex": request.hex(),
            "response_hex": bytes(resp).hex(),
            "req_id": req_id,
            "ack_req_id": ack_id,
            "elapsed_s": round(elapsed, 3),
        }
    if len(resp) < 12:
        return {
            "register": name,
            "address": f"{address:#010x}",
            "outcome": "D/SHORT",
            "request_hex": request.hex(),
            "response_hex": bytes(resp).hex(),
            "elapsed_s": round(elapsed, 3),
        }
    value = struct.unpack(">I", resp[8:12])[0]
    return {
        "register": name,
        "address": f"{address:#010x}",
        "outcome": "A/VALID",
        "request_hex": request.hex(),
        "response_hex": bytes(resp).hex(),
        "req_id": req_id,
        "ack_req_id": ack_id,
        "raw_uint32": value,
        "raw_hex": f"{value:#010x}",
        "elapsed_s": round(elapsed, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", required=True, help="Camera IP (e.g. 192.168.42.11)")
    parser.add_argument("--timeout", type=float, default=2.0, help="GVCP timeout (s)")
    parser.add_argument(
        "--repeats", type=int, default=1, help="Read each register N times"
    )
    parser.add_argument(
        "--set",
        type=int,
        default=None,
        metavar="MM",
        help="Write-test target in mm (requires --take-control)",
    )
    parser.add_argument(
        "--take-control",
        action="store_true",
        help="Allow CCP switchover for the write test (see docstring)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    )

    local_ip = routed_local_ip(args.camera)
    client = GVCPClient(args.camera, local_ip=local_ip, timeout=args.timeout)
    client.connect()
    try:
        local_port = client._socket.getsockname()[1]
    except Exception:
        local_port = -1
    print(f"camera={args.camera} local={local_ip}:{local_port} (bind-only, no CCP)")
    print("streaming must stay up on the V3 app while this runs")
    print("-" * 78)
    try:
        for round_no in range(args.repeats):
            for name, address in REGISTERS:
                result = probe(client, name, address)
                print(f"[round {round_no + 1}] {name} {result['address']}:")
                for key, val in result.items():
                    if key in ("register", "address"):
                        continue
                    print(f"    {key}: {val}")
        if args.set is not None:
            return write_test(client, args.camera, args.set, args.take_control)
    finally:
        client.close()
    return 0


def write_test(client: GVCPClient, camera_ip: str, target_mm: int, take_control: bool) -> int:
    """Direct SET-write test with restore. Returns process exit code."""
    print("-" * 78)
    print(f"WRITE TEST: target={target_mm}mm SET=0x20A138")
    if not take_control:
        print("refused: pass --take-control to allow CCP switchover (see docstring)")
        return 2
    if target_mm <= 0:
        print(f"refused: target must be > 0 mm, got {target_mm}")
        return 2
    original = probe(client, "CURRENT", REG_FOCUS_CURRENT)
    if original.get("outcome") != "A/VALID":
        print(f"refused: CURRENT unreadable: {original}")
        return 2
    orig_value = int(original["raw_uint32"])
    print(f"original CURRENT={orig_value}mm (will be restored)")
    if not 150 <= target_mm <= 1000000:
        print("note: target outside the usual [150,1000000] range; proceeding anyway")

    print(f"CCP switchover key={CCP_KEY} ...")
    if not client.control_switchover(key=CCP_KEY):
        print(
            "CCP DENIED by camera (expected while the streaming V3 app holds "
            "control) -- this itself proves app ownership. Nothing was written."
        )
        return 3
    print("CCP granted")
    try:
        expected_payload = struct.pack(">II", REG_FOCUS_SET, target_mm & 0xFFFFFFFF)
        print(f"WRITE request payload hex: {expected_payload.hex()} (expect 0020a138 + value)")
        ok = client.write_register(REG_FOCUS_SET, target_mm)
        print(f"WRITE ack ok={ok}")
        if not ok:
            print("camera rejected the SET write (transport reason in log above)")
            return 4
        deadline = time.monotonic() + 5.0
        readback = None
        while time.monotonic() < deadline:
            poll = probe(client, "CURRENT", REG_FOCUS_CURRENT)
            readback = poll.get("raw_uint32")
            print(f"  poll CURRENT={readback} outcome={poll.get('outcome')}")
            if readback == target_mm:
                break
            time.sleep(0.2)
        print(f"SET requested={target_mm}mm readback={readback}mm")
    finally:
        print(f"RESTORE original={orig_value}mm ...")
        restored_ok = client.write_register(REG_FOCUS_SET, orig_value)
        print(f"restore WRITE ack ok={restored_ok}")
        final = probe(client, "CURRENT", REG_FOCUS_CURRENT)
        print(f"restore CURRENT={final.get('raw_uint32')} outcome={final.get('outcome')}")
        print("CCP release ...")
        client.release_control()
        print("CCP released; re-check focus Read in the V3 app afterwards")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
