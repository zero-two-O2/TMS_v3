"""TMS_v3 PTZ/PLC Simulator (development only -- NOT a Siemens emulator).

Headless OPC UA simulator exposing the Phase 3 ``SimulatorPtzMapping``
node set under ``urn:tms:ptz:sim`` for eight independent PTZ instances.

Run::

    python -m tools.ptz_plc_simulator

Requires the ``ptz`` extra (``pip install .[ptz]`` for asyncua).
"""

from thermal_monitor.ptz.mapping import SimulatorPtzMapping

from tools.ptz_plc_simulator.simulator_config import (
    SIMULATOR_NAMESPACE_URI,
    SimulatorConfig,
)

__all__ = ["SIMULATOR_NAMESPACE_URI", "SimulatorConfig", "SimulatorPtzMapping"]
