# Siemens PLC Integration Checklist (CPU 1510SP-1 PN / ET 200SP)

Complete with the PLC/Fluke developer. Nothing here may be filled from
assumptions or from the TMS_v3 simulator. Each item needs a verified
value plus the document/version it came from.

## 1. Connection

- [ ] OPC UA endpoint URL: ___________________________
- [ ] Security policy: ___________________________
- [ ] Authentication (anonymous / username / certificate): ___________
- [ ] Test credentials location (never in source control): ___________

## 2. Namespace and nodes

- [ ] Namespace URI: ___________________________
- [ ] Runtime namespace index: ___________________________
- [ ] Node list document + version: ___________________________
- [ ] For EVERY field below: node ID, datatype, access (R/W)

| Field | Node ID | Datatype | Access | Verified |
|---|---|---|---|---|
| Target pan | | | | |
| Target tilt | | | | |
| Velocity (single?) | | | | |
| Pan velocity | | | | |
| Tilt velocity | | | | |
| Command/strobe | | | | |
| STOP | | | | |
| Clear error | | | | |
| Actual pan | | | | |
| Actual tilt | | | | |
| Moving | | | | |
| Position reached | | | | |
| Ready / available | | | | |
| Calibration request | | | | |
| Calibration active | | | | |
| Calibration complete | | | | |
| Error flag | | | | |
| Error code | | | | |

## 3. Semantics

- [ ] Velocity mode: single / per-axis? ___________________________
- [ ] Command handshake (values, edge vs level): ___________________________
- [ ] STOP behavior: ___________________________
- [ ] Position-reached definition + tolerance source: ___________________________
- [ ] Tolerance value + units: ___________________________
- [ ] Pan/tilt limits + units: ___________________________
- [ ] Calibration sequence + durations: ___________________________
- [ ] Calibration failure signals: ___________________________
- [ ] Error code table + reset behavior: ___________________________
- [ ] PTZ_01..PTZ_08 addressing on one CPU: ___________________________
- [ ] Communication-health signal for the client: ___________________________

## 4. Acceptance run (operator-confirmed movements only)

- [ ] Read-only diagnostics pass (`run_read_only_checks`)
- [ ] Limits confirmed from machine plate before any movement
- [ ] Single-axis movement + STOP verified per PTZ
- [ ] Position-reached verified against tolerance
- [ ] Calibration verified (trigger, active, complete, failure path)
- [ ] Error injection/reset verified
- [ ] Communication-loss + reconnect verified
- [ ] All 8 PTZ units verified independently

## 5. Sign-off

- [ ] Mapping document filled at `siemens-ptz-map/v1`
- [ ] `verify_siemens_document` returns zero problems
- [ ] Simulator results explicitly excluded from hardware evidence
- [ ] Name / date: ___________________________
