"""Run the TMS_v3 PTZ/PLC Simulator (development only).

Example::

    python -m tools.ptz_plc_simulator --endpoint opc.tcp://127.0.0.1:4840
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from tools.ptz_plc_simulator.plc_server import PtzPlcSimulatorServer
from tools.ptz_plc_simulator.simulator_config import (
    SIMULATOR_DEFAULT_ENDPOINT,
    SimulatorConfig,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint",
        default=SIMULATOR_DEFAULT_ENDPOINT,
        help="OPC UA endpoint (default: localhost, never a real PLC IP)",
    )
    parser.add_argument("--ptz-count", type=int, default=8)
    return parser


async def _amain(args: argparse.Namespace) -> int:
    from tools.ptz_plc_simulator.simulator_config import default_ptz_ids

    config = SimulatorConfig(
        ptz_ids=default_ptz_ids(args.ptz_count),
        endpoint=args.endpoint,
    )
    server = PtzPlcSimulatorServer(config)
    await server.serve_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
    )
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print("[PTZ-SIM] stopped by user", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
