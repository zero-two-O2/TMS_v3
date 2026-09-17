"""ptz.siemens -- production profile gate and Siemens verification contract.

No Siemens node IDs exist here by design. This module decides *which*
mapping a configured profile may use, validates production profiles,
and defines the exact information the PLC/Fluke developer must supply
before ``SiemensPtzMapping`` can be implemented. Anything Siemens stays
blocked until that verification passes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from thermal_monitor.ptz.errors import PtzValidationError
from thermal_monitor.ptz.mapping import (
    PtzMapping,
    PtzMappingError,
    SiemensPtzMapping,
    SimulatorPtzMapping,
)

# Fields the PLC/Fluke developer must confirm, grouped for the checklist.
# Each entry: (key, question answered by verified documentation).
REQUIRED_SIEMENS_FIELDS: tuple[tuple[str, str], ...] = (
    ("endpoint", "OPC UA endpoint URL of the CPU 1510SP-1 PN"),
    ("security", "Security policy and authentication (anonymous/user/cert)"),
    ("namespace_uri", "Namespace URI of the PLC program"),
    ("namespace_index", "Runtime namespace index on the live server"),
    ("target_pan", "Node ID, datatype and write access for target pan"),
    ("target_tilt", "Node ID, datatype and write access for target tilt"),
    ("velocity", "Single velocity node or per-axis nodes + semantics"),
    ("command", "Command/strobe handshake (values, edge vs level)"),
    ("stop", "STOP behavior (abort-in-place? acknowledge?)"),
    ("clear_error", "Error reset behavior and acknowledgement"),
    ("actual_pan", "Node ID, datatype, units, update rate"),
    ("actual_tilt", "Node ID, datatype, units, update rate"),
    ("moving", "Moving signal semantics"),
    ("position_reached", "Reached signal semantics and tolerance source"),
    ("ready", "Ready/available signal semantics"),
    ("tolerance", "Position tolerance value, units, per-axis?"),
    ("limits", "Pan/tilt limits and units"),
    ("calibration_request", "Calibration trigger semantics"),
    ("calibration_state", "Active/complete/failed signal semantics"),
    ("error_codes", "Error code table and reset behavior"),
    ("ptz_addressing", "How PTZ_01..PTZ_08 are addressed (one PLC?)"),
    ("comm_health", "Communication-health semantics for the client"),
)

# Versioned mapping-document contract. The verified document must carry
# this version; anything else is rejected at verification time.
SIEMENS_MAPPING_DOCUMENT_VERSION = "siemens-ptz-map/v1"


@dataclass(frozen=True, slots=True)
class SiemensMappingDocument:
    """Verified PLC mapping information. Empty until the PLC/Fluke
    developer supplies and signs off every required field."""

    version: str = ""
    values: dict = field(default_factory=dict)  # type: ignore[type-arg]

    def missing_fields(self) -> list[str]:
        keys = [key for key, _ in REQUIRED_SIEMENS_FIELDS]
        return [key for key in keys if not self.values.get(key)]


@dataclass(frozen=True, slots=True)
class ProfileStatus:
    """Human-readable readiness of one backend profile."""

    profile: str
    ready: bool
    endpoint: str = ""
    mapping: str = ""
    reason: str = ""


def validate_profile(
    profile: str,
    endpoint: str,
    security_mode: str,
    username: str,
) -> ProfileStatus:
    """Validate a configured backend profile without connecting.

    Never falls back across profiles: a configured Siemens profile that
    cannot be served fails loudly instead of silently using the simulator.
    """
    profile = (profile or "").strip() or "simulator"
    endpoint = (endpoint or "").strip()
    if profile == "simulator":
        if endpoint and (
            "127.0.0.1" not in endpoint.lower()
            and "localhost" not in endpoint.lower()
        ):
            return ProfileStatus(
                profile=profile,
                ready=False,
                endpoint=endpoint,
                mapping="simulator",
                reason="simulator profile requires a localhost endpoint",
            )
        return ProfileStatus(
            profile=profile,
            ready=True,
            endpoint=endpoint or "opc.tcp://127.0.0.1:4840",
            mapping="simulator",
            reason="development-only simulator mapping",
        )
    if profile == "generic":
        if not endpoint:
            return ProfileStatus(
                profile=profile,
                ready=False,
                reason="generic profile requires ptz.endpoint",
            )
        return ProfileStatus(
            profile=profile,
            ready=False,
            endpoint=endpoint,
            reason=(
                "generic profile has an endpoint but no verified mapping; "
                "provide a concrete PtzMapping before use"
            ),
        )
    if profile == "siemens":
        return ProfileStatus(
            profile=profile,
            ready=False,
            endpoint=endpoint,
            mapping="siemens (blocked)",
            reason=(
                "Siemens mapping is blocked: no verified PLC information. "
                "Complete the hardware checklist first."
            ),
        )
    return ProfileStatus(
        profile=profile, ready=False, reason=f"unsupported profile {profile!r}"
    )


def mapping_for_profile(
    profile: str,
    ptz_ids: tuple[str, ...],
    document: Optional[SiemensMappingDocument] = None,
) -> PtzMapping:
    """Return the mapping a profile is allowed to use.

    Simulator resolves to the development mapping. Every other profile
    raises until a verified mapping exists — including Siemens, even
    with a document, until the implementation lands (Phase 7 stops at
    the documented boundary).
    """
    if profile == "simulator":
        if not ptz_ids:
            raise PtzValidationError("simulator profile needs at least one ptz_id")
        return SimulatorPtzMapping(ptz_ids=tuple(ptz_ids))
    if profile == "siemens":
        missing = (document or SiemensMappingDocument()).missing_fields()
        raise PtzMappingError(
            "SiemensPtzMapping is blocked: "
            + (
                f"missing verified fields: {', '.join(missing)}"
                if missing
                else "no verified Siemens implementation exists yet"
            )
        )
    raise PtzMappingError(
        f"profile {profile!r} has no available mapping; "
        "provide a concrete PtzMapping before use"
    )


def verify_siemens_document(document: SiemensMappingDocument) -> list[str]:
    """Fail-fast verification of a mapping document. Returns the list of
    problems (empty means structurally complete — still not an
    implementation)."""
    problems: list[str] = []
    if document.version != SIEMENS_MAPPING_DOCUMENT_VERSION:
        problems.append(
            f"unsupported document version {document.version!r}; "
            f"expected {SIEMENS_MAPPING_DOCUMENT_VERSION!r}"
        )
    for missing in document.missing_fields():
        problems.append(f"missing verified field: {missing}")
    return problems


__all__ = [
    "ProfileStatus",
    "REQUIRED_SIEMENS_FIELDS",
    "SIEMENS_MAPPING_DOCUMENT_VERSION",
    "SiemensMappingDocument",
    "mapping_for_profile",
    "validate_profile",
    "verify_siemens_document",
]
