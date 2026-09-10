"""Stage 8D Phase 5 tests: native GVCP discovery (HALCON untouched).

No hardware required. Broadcast answers and per-camera clients are faked;
the service logic (dedupe, serial identity, local-interface reporting,
static classification incl. the discovery-shy .16 case) is real.
"""

from __future__ import annotations

import pytest

from thermal_monitor.camera.tv46_gvcp import TV46DeviceInfo
from thermal_monitor.services import discovery as discovery_mod
from thermal_monitor.services.discovery import (
    DiscoveredCamera,
    GvcpDiscoveryService,
)


def _info(ip: str, serial: str) -> TV46DeviceInfo:
    return TV46DeviceInfo(
        ip_address=ip,
        mac_address="34:08:e1:d8:db:be",
        serial_number=serial,
        model_name="TV46L-1-26010003@9Hz",
        manufacturer_name="Fluke Process Instruments",
        device_version="1.0.8",
    )


class FakeClient:
    """Scripted per-camera client: discover_ok controls DISCOVERY answer."""

    def __init__(self, discover_ok=True, read_ok=True, connect_ok=True, ip="192.168.42.99",
                 serial="HBTEST0001"):
        self._discover_ok = discover_ok
        self._read_ok = read_ok
        self._connect_ok = connect_ok
        self._ip = ip
        self._serial = serial
        self.closed = False

    def connect(self):
        if not self._connect_ok:
            raise RuntimeError("no route")
        return True

    def close(self):
        self.closed = True

    def discover_once(self):
        if not self._discover_ok:
            raise RuntimeError("timeout")
        return _info(self._ip, self._serial)

    def read_register(self, address):
        if not self._read_ok:
            raise RuntimeError("timeout")
        return 10000


def _service(monkeypatch, broadcasts, clients, local_ips=("192.168.42.100",)):
    """Service with faked broadcast answers and per-IP clients."""
    import thermal_monitor.camera.tv46_gvcp as gvcp_mod

    monkeypatch.setattr(
        gvcp_mod,
        "broadcast_discover",
        lambda **kwargs: list(broadcasts.get(kwargs.get("local_ip"), [])),
    )

    def factory(ip, local):
        client = clients[ip]
        if callable(client):
            return client()
        return client

    return GvcpDiscoveryService(
        local_ips=list(local_ips), timeout=0.1, attempts=1, client_factory=factory,
        sleep=lambda s: None,
    )


class TestBroadcastDiscovery:
    def test_found_cameras_carry_serial_identity_and_interface(self, monkeypatch):
        svc = _service(
            monkeypatch,
            {"192.168.42.100": [(_info("192.168.42.13", "HB25080011"), "192.168.42.100")]},
            {},
        )
        found = svc.discover_broadcast()
        assert len(found) == 1
        cam = found[0]
        assert cam.serial_number == "HB25080011"
        assert cam.ip_address == "192.168.42.13"
        assert cam.camera_id == "cam_HB25080011"
        assert cam.local_ip == "192.168.42.100"
        assert cam.source == "gvcp"
        assert cam.vendor == "Fluke Process Instruments"

    def test_duplicates_suppressed_by_serial(self, monkeypatch):
        svc = _service(
            monkeypatch,
            {
                "192.168.42.100": [
                    (_info("192.168.42.13", "HB25080011"), "192.168.42.100"),
                    (_info("192.168.42.13", "HB25080011"), "192.168.42.100"),
                ]
            },
            {},
        )
        found = svc.discover_broadcast()
        assert len(found) == 1

    def test_no_answers_returns_empty(self, monkeypatch):
        svc = _service(monkeypatch, {}, {})
        assert svc.discover_broadcast() == []


class TestStaticVerification:
    def test_static_verified(self, monkeypatch):
        svc = _service(monkeypatch, {}, {"192.168.42.13": FakeClient()})
        states = svc.verify_static(["192.168.42.13"])
        assert len(states) == 1
        assert states[0].state == "static-verified"
        assert states[0].camera is not None
        assert states[0].camera.serial_number == "HBTEST0001"

    def test_discovery_shy_camera_is_nonresponsive_not_dropped(self, monkeypatch):
        # The known .16 behavior: ignores DISCOVERY, answers register reads.
        svc = _service(
            monkeypatch,
            {},
            {"192.168.42.16": FakeClient(discover_ok=False, read_ok=True)},
        )
        states = svc.verify_static(["192.168.42.16"])
        assert states[0].state == "gvcp-nonresponsive"
        assert states[0].camera is None

    def test_dead_ip_is_unreachable(self, monkeypatch):
        svc = _service(
            monkeypatch,
            {},
            {"192.168.42.99": FakeClient(discover_ok=False, read_ok=False)},
        )
        states = svc.verify_static(["192.168.42.99"])
        assert states[0].state == "unreachable"

    def test_unroutable_ip_is_unreachable(self, monkeypatch):
        svc = _service(monkeypatch, {}, {"10.9.9.9": FakeClient(connect_ok=False)})
        states = svc.verify_static(["10.9.9.9"])
        assert states[0].state == "unreachable"


class TestScanMerge:
    def test_scan_merges_broadcast_and_static_without_duplicates(self, monkeypatch):
        svc = _service(
            monkeypatch,
            {"192.168.42.100": [(_info("192.168.42.13", "HB25080011"), "192.168.42.100")]},
            {
                "192.168.42.13": FakeClient(ip="192.168.42.13", serial="HB25080011"),
                "192.168.42.16": FakeClient(discover_ok=False, read_ok=True),
                "192.168.42.99": FakeClient(discover_ok=False, read_ok=False),
            },
        )
        report = svc.scan(static_ips=["192.168.42.13", "192.168.42.16", "192.168.42.99"])
        assert len(report.found) == 1  # only .13 answers discovery here
        assert report.nonresponsive == ["192.168.42.16"]
        assert report.unreachable == ["192.168.42.99"]
        states = {s.ip_address: s.state for s in report.states}
        assert states["192.168.42.13"] == "discovered"
        assert states["192.168.42.16"] == "gvcp-nonresponsive"

    def test_discovered_camera_contract_compatible_with_halcon_path(self, monkeypatch):
        svc = _service(
            monkeypatch,
            {"192.168.42.100": [(_info("192.168.42.13", "HB25080011"), "192.168.42.100")]},
            {},
        )
        (cam,) = svc.discover_broadcast()
        # Same identity contract the runtime/UI uses for HALCON results.
        assert cam.camera_id == "cam_HB25080011"
        assert cam.stable_identity == "HB25080011"
        assert isinstance(cam.local_ip, str) and cam.local_ip
