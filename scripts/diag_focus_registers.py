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
    GVCPCommand,
    GVCPError,
    REG_FOCUS_CURRENT,
    REG_FOCUS_MAX,
    REG_FOCUS_MIN,
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
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
