# V3 Recording & Offline Architecture (Stage 5B Design)

This document is the **design specification** for the V3 persistent recording
format and the Offline playback/reprocessing pipeline. It is the long-lived
data contract that the `RecordingWriter`, `RecordingReader`, and
`OfflineFrameSource` will implement in Stage 5C–5G.

The format is a **persistent, self-contained, inspectable, versioned container**
for raw acquisition data. It is independent of:

- Python pickle
- Python object layout / class identity
- HALCON objects
- Shared-memory (SHM) slot layout
- NumPy object serialization
- SQL Server availability
- current V3 Python classes

A recording must remain readable even if V3 classes change later.

---

## Table of Contents

1. [Requirements](#1-requirements)
2. [Design Principles](#2-design-principles)
3. [Recording Lifecycle](#3-recording-lifecycle)
4. [Recording Directory Structure](#4-recording-directory-structure)
5. [Manifest Schema](#5-manifest-schema)
6. [Recording Unit & IR/VL Representation](#6-recording-unit--irvl-representation)
7. [Frame Record Schema](#7-frame-record-schema)
8. [Chunk Layout](#8-chunk-layout)
9. [Raw Payload Representation](#9-raw-payload-representation)
10. [Index Schema](#10-index-schema)
11. [Metadata Snapshots](#11-metadata-snapshots)
12. [ROI Snapshot](#12-roi-snapshot)
13. [PTZ Snapshot](#13-ptz-snapshot)
14. [Calibration Snapshot](#14-calibration-snapshot)
15. [Alarm Events](#15-alarm-events)
16. [Pre-Alarm / Post-Alarm Semantics](#16-pre-alarm--post-alarm-semantics)
17. [Integrity](#17-integrity)
18. [Crash Recovery](#18-crash-recovery)
19. [Versioning](#19-versioning)
20. [SQL Server Boundary](#20-sql-server-boundary)
21. [Offline Frame Reconstruction](#21-offline-frame-reconstruction)
22. [Offline Reprocessing](#22-offline-reprocessing)
23. [Offline Configuration (Historical vs Current)](#23-offline-configuration-historical-vs-current)
24. [Shared Memory Boundary](#24-shared-memory-boundary)
25. [Offline Module Location & Migration Plan](#25-offline-module-location--migration-plan)
26. [Performance Estimates](#26-performance-estimates)
27. [Open Decisions](#27-open-decisions)
28. [Migration from V2 / Current V3 Format](#28-migration-from-v2--current-v3-format)
29. [Inspectability](#29-inspectability)

---

## 1. Requirements

Every recorded frame must preserve enough information to reconstruct a V3
`Frame` **without** the original camera. At minimum:

| Category | Fields |
|---|---|
| Frame identity | `camera_id`, `sequence`, `timestamp_wall`, `timestamp_monotonic`, recording-relative timestamp |
| Thermal | raw uint16 data, `dtype`, `width`, `height`, pixel format |
| Visible | raw data, `dtype`, `width`, `height`, pixel format |
| Synchronization | IR/visible sync status, stream timestamps, sync metadata, association info |
| Acquisition | acquisition metadata, frame metadata, packet/drop information |
| Camera | camera identity, camera configuration snapshot |
| PTZ | pan, tilt, zoom, focus, current `position_id` |
| ROI context | ROI configuration applicable at recording time |
| Alarm | alarm event, alarm rule, measured value, threshold, severity, timestamp |

**Raw thermal data MUST remain available.**  
**Raw visible data MUST remain available when present.**

The recording must NOT store only rendered/display images, and must NOT perform
lossy conversion (`YUV422 → RGB`, `thermal → temperature`).

---

## 2. Design Principles

1. **Raw is authoritative.** Derived data (temperature, statistics, rendered
   images, pairing) is always secondary.
2. **Physical records preserve truth.** Independent IR/VL stream records match
   the alternating TV46L wire behavior.
3. **Logical association is derived.** IR/VL pairing is metadata, never a
   destruction of physical identity.
4. **Self-contained.** A recording must be fully interpretable from its own
   files; no live SQL or live config required.
5. **Append-only.** All payload data is written append-only into bounded
   chunks; nothing is rewritten in place.
6. **Crash-safe by construction.** A valid frame is only committed after its
   full bytes + checksum are present; truncation is always detectable.
7. **Deterministic.** Identical input produces identical bytes (no pickle, no
   dict iteration order dependence, no object addresses).
8. **Low file count.** Chunked append-only payloads instead of one file per
   frame.
9. **Human-inspectable.** `manifest.json` (and configuration snapshots) are
   readable JSON.

---

## 3. Recording Lifecycle

```
                    ┌──────────────────────────────────────────────┐
                    │            Application Start                 │
                    └──────────────────────┬───────────────────────┘
                                           │
                                           ▼
                     ┌───────────────────────────────┐
                     │    Recorder (per camera)       │  state: IDLE
                     │    RollingFrameBuffer          │
                     └──────────────────────┬────────┘
                                            │ feed frames continuously
                                            │ (buffers recent N frames)
                                            ▼
                     ┌───────────────────────────────┐
                     │         ARMED                 │  armed / idle buffering
                     └──────────────────────┬────────┘
                                            │ ALARM triggers
                                            ▼
   ┌────────────────────────────────────────────────────────────┐
   │  WRITE_STARTED:                                            │
   │  1. create recording dir + manifest (status=WRITING)       │
   │  2. snapshot config/ROI/PTZ/calibration/alarm              │
   │  3. drain pre-alarm buffer -> chunk records                │
   │  4. alarm trigger frame (from same feed) is recorded       │
   │  5. continue post-alarm frames as they arrive              │
   │     until post_alarm_seconds elapsed or manual stop        │
   │  6. close chunks, write index, seal manifest (COMPLETE)    │
   └────────────────────────────────────────────────────────────┘
                                            │
                                            ▼
                                COMPLETE | INCOMPLETE
```

### Recorded frame states

- `IDLE` — no recording active; rolling buffer may still be filling.
- `WRITING` — recording directory exists, manifest says `WRITING`, chunks are
  being appended. A crash here leaves an **INCOMPLETE** recording.
- `COMPLETE` — index written, manifest sealed, `finalization` marker present.
- `INCOMPLETE` — manifest says `WRITING` at open time, or finalization marker
  missing.
- `CORRUPTED` — structural/checksum validation failed.

### Pre-alarm timing fix (Stage 5A finding #11)

The Stage 5A audit found: *"alarm frame itself written on next feed_frame"*.

The corrected state machine **never drops the trigger frame**:

1. The rolling buffer always holds the last `pre_alarm_seconds` of frames.
2. When an alarm fires, `trigger_recording()` first opens the sink and drains
   the buffer.
3. The trigger frame **is** the frame that caused the alarm. It must be either
   (a) already in the buffer (when the alarm was evaluated on that frame) or
   (b) written as the first post-open frame.
4. The state machine is defined as: **the alarm frame is the frame the alarm
   evaluator produced the event for.** The `Recorder.feed_frame` call for that
   frame is the trigger. The Recorder writes it exactly once. A flag
   `trigger_written` guarantees the trigger frame is not double-counted and
   not lost.

Concretely, `Recorder` tracks `_trigger_sequence`. When `feed_frame(frame)`
arrives with `frame.descriptor.sequence == _trigger_sequence`, that frame is
written immediately and the pre-alarm drain happens *before* it, ensuring the
on-disk order is:

```
[pre-alarm frames ...] [trigger frame] [post-alarm frames ...]
```

---

## 4. Recording Directory Structure

```
recordings/
└── <recording_id>/                      # e.g. rec_20260818_093000_0001
    ├── manifest.json                    # REQUIRED. Recording-level metadata + status.
    ├── index.bin                        # REQUIRED. Binary frame index (append-only).
    ├── chunks/
    │   ├── chunk_000.tmsr               # Append-only payload chunk (records)
    │   ├── chunk_001.tmsr
    │   └── ...
    ├── config/
    │   ├── cameras.json                 # Camera configuration snapshots
    │   ├── rois.json                    # ROI snapshots per camera/position
    │   ├── ptz.json                     # PTZ state-change records
    │   ├── calibration.json             # Calibration identity/context
    │   └── alarm.json                   # Alarm configuration snapshot
    └── events/
        └── alarms.json                  # Persistent alarm events (references frames)
```

### File inventory

| File | Purpose | Required? |
|---|---|---|
| `manifest.json` | Human-readable recording identity, format version, time range, stream summary, status, finalization marker | yes |
| `index.bin` | Compact binary index: per-record `(camera_id, stream, sequence, timestamp, chunk, offset, length, checksum)` | yes |
| `chunks/chunk_NNN.tmsr` | Append-only concatenation of binary frame records | yes (≥1) |
| `config/cameras.json` | Immutable camera config snapshots | yes |
| `config/rois.json` | Immutable ROI snapshots | yes |
| `config/ptz.json` | PTZ state-change records with timestamps | yes (empty allowed) |
| `config/calibration.json` | Calibration identity/version/hash per camera | yes (empty allowed) |
| `config/alarm.json` | Alarm rule configuration at recording time | yes (empty allowed) |
| `events/alarms.json` | Alarm events referencing recorded frames | yes (empty allowed) |

**Why chunks instead of one file per frame:** at 8 cameras × ~9 FPS × 2 streams
(IR+VL) ≈ 144 records/s, one-file-per-frame would create ~144 files/second.
Bounded chunk files (e.g. up to 64 MiB payload each) keep the file count in
the hundreds even for long recordings and make crash recovery tractable.

---

## 5. Manifest Schema

`manifest.json` is UTF-8 JSON. It is small (no per-frame data).

```jsonc
{
  "format_version": { "major": 1, "minor": 0 },

  "recording_id": "rec_20260818_093000_0001",
  "created_at": "2026-08-18T09:30:00.123Z",

  "application": {
    "name": "Thermal Monitoring System V3",
    "version": "3.0.0"
  },

  "status": "COMPLETE",            // WRITING | COMPLETE | INCOMPLETE | CORRUPTED
  "finalized_at": "2026-08-18T09:31:00.456Z",

  "trigger": "ALARM",              // MANUAL | ALARM | SCHEDULED | DIAGNOSTIC
  "trigger_alarm_id": "alarm_ab12cd34ef56",

  "cameras": ["cam_001", "cam_002"],
  "streams": {
    "cam_001": { "IR": true, "VL": true },
    "cam_002": { "IR": true, "VL": false }
  },

  "start_time": "2026-08-18T09:29:50.000Z",
  "end_time": "2026-08-18T09:31:00.000Z",
  "frame_count": 612,              // total records across all cameras/streams
  "duration_seconds": 70.0,

  "pre_alarm_seconds": 10.0,
  "post_alarm_seconds": 20.0,

  "chunk_count": 3,
  "index_count": 612,

  "sync_groups": 300,              // optional derived logical pairs

  "sql_metadata": {                // lightweight mirror for discovery (optional)
    "recording_id": "rec_20260818_093000_0001",
    "path": "recordings/rec_20260818_093000_0001",
    "status": "COMPLETE"
  }
}
```

### What belongs where (manifest vs index vs record header)

| Information | Manifest | Index | Record header | Rationale |
|---|---|---|---|---|
| format_version | ✔ | — | ✔ | manifest for human/reader gate; record header for per-record versioning |
| recording_id | ✔ | — | — | recording-level identity |
| time range | ✔ | derived | — | manifest summary; index holds exact per-record |
| stream summary | ✔ | derived | — | manifest quick-view |
| frame_count | ✔ | ✔ | — | manifest summary; index `count` |
| per-frame sequence/timestamp | — | ✔ | ✔ | index for seek; header for validation |
| per-frame payload location | — | ✔ (chunk+offset+len) | ✔ (offset/len implicit) | index = seek; header = integrity |
| payload dimensions/dtype/pixel format | — | ✔ | ✔ | index for reconstruction planning; header for validation |
| per-frame checksum | — | ✔ | ✔ | both store it; header is authoritative for the payload |
| camera/ROI/PTZ/calibration snapshots | references | — | — | separate immutable JSON files |

**Design rule:** the manifest contains **recording-level** facts. The index
contains **record-level** facts needed for seek/reconstruction. The record
header contains **per-record facts needed to validate and self-describe one
record**. Redundancy between index and header is intentional (crash recovery
can rebuild one from the other).

---

## 6. Recording Unit & IR/VL Representation

### Physical record (source of truth)

A **frame record** is one independently acquired payload from one camera and
one stream:

- `stream_type = "IR"` or `"VL"` (visible)

Each record carries its own `sequence`, `timestamp`, `payload`, `metadata`.
The recording is an append-only sequence of physical records. This matches the
alternating TV46L wire behavior:

```
IR  (cam_1, seq 10, t=1000)
VL  (cam_1, seq 10, t=1005)
IR  (cam_1, seq 11, t=1011)
VL  (cam_1, seq 11, t=1016)
```

### Logical association (derived, optional)

The recording MAY contain optional association metadata that groups a physical
IR record and a physical VL record into a logical pair:

```jsonc
{
  "sync_group_id": 123,
  "sync_status": "synchronized",     // synchronized | acceptable | degraded | unknown
  "time_delta_ms": 5.0,
  "ir":  { "camera_id": "cam_1", "sequence": 10, "stream": "IR" },
  "vl":  { "camera_id": "cam_1", "sequence": 10, "stream": "VL" }
}
```

Association does **not** create a new physical record and does **not** merge
the payloads. It references existing records by `(camera_id, stream, sequence)`.

- Association is optional and derived — acquisition has not yet established
  hard synchronization semantics, so we must not invent them.
- If association metadata is absent, `sync_status = "unknown"`.
- The index remains the source of physical truth.

### Recommendation (open question resolved)

Store **separate IR/VL physical records** + **optional logical sync groups**.
This is the least lossy representation: physical truth is preserved, and the
processing layer can build a logical pair later.

**Consequence for V3 Frame contract (ADR-002):** `core.frame.Frame` currently
models *one* payload pair (`thermal` + `visible` inside a single `Frame`). For
independent stream records, reconstruction produces a `Frame` with exactly one
stream populated:

- IR record → `Frame` with `thermal` populated, `visible.present = False`,
  `sync.status = MISSING_VISIBLE`.
- VL record → `Frame` with `visible` populated, `thermal.present = False`,
  `sync.status = MISSING_THERMAL`.

This does **not** require changing ADR-002. The existing `SyncStatus` values
already cover missing thermal / missing visible. A logical pair can later be
merged into a `Frame` with both populated and `sync.status = SYNCHRONIZED`
using the sync-group metadata. Documented here; no ADR change required.

---

## 7. Frame Record Schema

Each frame record is a **fixed binary layout** with a variable-length JSON
metadata tail. No pickle, no NumPy object serialization.

### On-disk record layout (within a chunk)

```
┌──────────────────────────────────────────────────────────────┐
│ Record header (fixed, 128 bytes)                              │
├──────────────────────────────────────────────────────────────┤
│  magic           : 4 bytes   = 0x54 0x4D 0x53 0x52 ("TMSR")   │
│  record_version  : 1 byte    (u8)  = 1                        │
│  record_type     : 1 byte    (u8)  = 0x01 (frame record)      │
│  stream_type     : 1 byte    (u8)  = 0x01 IR | 0x02 VL        │
│  reserved        : 1 byte    (u8)  = 0                        │
│  camera_id_len   : 2 bytes   (u16 LE)                         │
│  camera_id       : camera_id_len bytes  (UTF-8)               │
│  sequence        : 8 bytes   (u64 LE)  (per camera, per stream)│
│  timestamp       : 8 bytes   (f64 LE)  (wall clock, epoch s)  │
│  monotonic       : 8 bytes   (f64 LE)  (recording-relative s) │
│  payload_offset  : 8 bytes   (u64 LE)  (within chunk file)    │
│  payload_length  : 8 bytes   (u64 LE)                         │
│  width           : 4 bytes   (u32 LE)                         │
│  height          : 4 bytes   (u32 LE)                         │
│  pixel_format    : 4 bytes   (u32 LE)  (enum, see below)      │
│  dtype_code      : 1 byte    (u8)     (see below)             │
│  bits_per_channel: 1 byte    (u8)                             │
│  sync_status     : 1 byte    (u8)     (see below)             │
│  sync_group_id   : 8 bytes   (s64 LE)  (-1 = none)            │
│  metadata_len    : 4 bytes   (u32 LE)                         │
│  reserved2       : 12 bytes                                   │
├──────────────────────────────────────────────────────────────┤
│  Metadata tail (variable)                                     │
│  JSON UTF-8: acquisition metadata, packet info, drop info,    │
│  PTZ reference, position_id, sync time_delta, etc.            │
├──────────────────────────────────────────────────────────────┤
│  Payload (payload_length bytes)                               │
│  raw thermal bytes OR raw visible bytes (raw, lossless)       │
├──────────────────────────────────────────────────────────────┤
│  Checksum : 4 bytes  (CRC32 of header + metadata + payload)   │
└──────────────────────────────────────────────────────────────┘
```

### Enums

| `stream_type` | Value |
|---|---|
| IR | `0x01` |
| VL (visible) | `0x02` |

| `pixel_format` | Value |
|---|---|
| `IR_Data` (uint16 raw) | `0x0001` |
| `YUV422_8` | `0x0002` |
| `RGB8` | `0x0003` |
| `Mono8` | `0x0004` |
| (reserved) | ... |

| `dtype_code` | Value |
|---|---|
| `uint8` | `0x01` |
| `uint16` | `0x02` |
| `float32` | `0x03` |
| `uint32` | `0x04` |

| `sync_status` | Value |
|---|---|
| `unknown` | `0x00` |
| `synchronized` | `0x01` |
| `acceptable` | `0x02` |
| `degraded` | `0x03` |
| `missing_thermal` | `0x04` |
| `missing_visible` | `0x05` |

### Checksum scope

- **Hashed:** header bytes (0..126, i.e. everything except the checksum field
  itself) + metadata tail + payload bytes.
- **Stored:** 4-byte CRC32 (zlib) at the end of the record.
- **Verified:** on read, on `RecordingReader` open (background), and on demand.

### What the header does NOT contain

- Dimensions are in the header AND index (validation on both paths).
- `dtype` code + `bits_per_channel` give full type info.
- No temperature, no calibrated values, no rendered image — those are derived.

---

## 8. Chunk Layout

A chunk is an append-only file of records, each self-describing.

### Chunk file (`chunk_NNN.tmsr`)

```
┌──────────────────────────────────────────────┐
│ Chunk header (fixed, 32 bytes)                │
│  magic     : 4 bytes  = "TMSR"                │
│  version   : 2 bytes  (u16 LE)                │
│  chunk_seq : 8 bytes  (u64 LE)                │
│  record_count : 8 bytes (u64 LE)              │
│  reserved  : 10 bytes                         │
├──────────────────────────────────────────────┤
│  Record 0 (header + metadata + payload + crc) │
│  Record 1                                    │
│  ...                                         │
├──────────────────────────────────────────────┤
│  Chunk trailer (optional, 16 bytes)           │
│  end_magic  : 4 bytes = "TMSQ" (end marker)   │
│  record_count : 8 bytes (u64 LE)              │
│  chunk_crc  : 4 bytes                        │
└──────────────────────────────────────────────┘
```

### Design choices

- **Maximum chunk size:** 64 MiB of payload (configurable constant, tuned for
  crash recovery: at ~1.2 MB per IR+VL pair, a 64 MiB chunk holds ~50 pairs).
  Roll over to a new chunk when the limit is reached.
- **Rollover:** when adding a record would exceed the limit, finalize the
  current chunk (write trailer, update index entry for the chunk), then start
  `chunk_{n+1}`.
- **Record boundaries:** records never span chunk boundaries. A record that
  would overflow rolls the chunk first.
- **Recovery after incomplete chunk:** on open, if the last chunk lacks a
  valid trailer, truncate at the last record whose CRC validates, mark the
  remaining tail as truncated, and set manifest status accordingly.
- **Index entries:** the index stores `(chunk_seq, offset_in_chunk, length)`
  for each record; seek opens the chunk and seeks to the offset.

---

## 9. Raw Payload Representation

- Thermal payload = **raw uint16 array bytes**, `width × height`, stored
  row-major, byte-for-byte identical to the camera's raw data.
- Visible payload = **raw camera bytes** (e.g. YUV422_8 `640×480`), stored
  as-is. **No** `YUV422 → RGB`. **No** JPEG/PNG. **No** lossy compression.
- Dimensions, dtype code, pixel format, and byte count are in the record
  header and index.
- **Lossless compression is explicitly out of scope** for the authoritative
  payload (may be discussed separately; the format reserves no field for it
  yet so future evolution is additive).
- The payload is the **source of truth**; temperature and rendered images are
  derived and never replace it.

---

## 10. Index Schema

`index.bin` is a compact append-only binary index. It must support:

- sequential playback
- seek by timestamp
- seek by camera + sequence
- seek by camera + timestamp
- stream filtering

It is **not** a true O(1) seek structure for timestamps; it is a **sorted
array** (by `timestamp`, tie-broken by `sequence`) over which the reader does
**binary search**. That is sufficient for the target scale (thousands to
millions of records; binary search is O(log n)).

### Index file layout

```
┌──────────────────────────────────────────────┐
│ Index header (fixed, 32 bytes)                │
│  magic        : 4 bytes = "TMSI"              │
│  version      : 2 bytes (u16 LE)              │
│  record_count : 8 bytes (u64 LE)              │
│  entry_size   : 2 bytes (u16 LE) = 56         │
│  reserved     : 16 bytes                      │
├──────────────────────────────────────────────┤
│  Entry 0  (fixed 56 bytes each)               │
│  Entry 1                                    │
│  ...                                        │
└──────────────────────────────────────────────┘
```

### Index entry (56 bytes, little-endian)

```
┌──────────────────────────────────────────────┐
│  camera_id_ref : 4 bytes (u32 LE)  (dictionary index) │
│  stream_type   : 1 byte                        │
│  flags         : 1 byte                        │
│  sequence      : 8 bytes (u64 LE)              │
│  timestamp     : 8 bytes (f64 LE)              │
│  chunk_seq     : 4 bytes (u32 LE)              │
│  offset        : 8 bytes (u64 LE)  (within chunk) │
│  payload_length: 8 bytes (u64 LE)              │
│  checksum      : 4 bytes (u32 LE)              │
│  width         : 4 bytes (u32 LE)              │
│  height        : 4 bytes (u32 LE)              │
│  pixel_format  : 2 bytes (u16 LE)              │
│  reserved      : 6 bytes                       │
└──────────────────────────────────────────────┘
```

### String dictionary

`camera_id` and pixel-format strings are not repeated in every entry. The
manifest (or a small dictionary section in the index header) maps
`camera_id_ref → "cam_001"`.

### Sort order & seeking

- Primary sort key: `timestamp`; secondary: `sequence`; tertiary: `stream_type`.
- **Binary search by timestamp** — `bisect` on the sorted array.
- **Seek by (camera, sequence):** the index is also grouped logically by
  camera; the reader builds an in-memory `dict[(camera_id, stream)] -> list of
  (sequence → index position)` lazily on open. This gives O(log n) sequence
  seek per camera/stream without a database.
- **Stream filtering:** index is memory-mappable; filter on `stream_type` byte.

### What the index does NOT contain

- No raw payload bytes.
- No metadata JSON.
- No per-record redundant camera strings (dictionary refs only).

---

## 11. Metadata Snapshots

Config snapshots are immutable JSON files written once when the recording
opens. They capture the state **at recording time** so offline never depends
on live SQL config.

### General schema pattern

```jsonc
{
  "snapshot_timestamp": "2026-08-18T09:29:50.000Z",
  "source": "sql_server" | "file" | "manual",
  "schema_version": 1,
  "items": [ ... ]
}
```

### 11.1 Camera configuration snapshot (`config/cameras.json`)

```jsonc
{
  "snapshot_timestamp": "...",
  "schema_version": 1,
  "items": [
    {
      "camera_id": "cam_001",
      "identity": {
        "camera_id": "cam_001",
        "serial_number": "TV46L-...",
        "model": "TV46L-1-26010003@9Hz",
        "vendor": "Fluke Process Instruments",
        "firmware": "...",
        "user_name": "IR1"
      },
      "name": "Boiler north",
      "description": "...",
      "enabled": true,
      "streams": { "thermal": true, "visible": true },
      "acquisition": {            // non-exhaustive, snapshot only
        "width": 640, "height": 480,
        "thermal_pixel_format": "IR_Data",
        "visible_pixel_format": "YUV422_8",
        "frame_rate": 9.0
      },
      "metadata": { "tags": {} }
    }
  ]
}
```

This is an explicit-data snapshot — never a pickled `CameraConfig`.

### 11.2 Calibration snapshot (`config/calibration.json`)

Design decision: **store an immutable calibration reference AND, if available
and small, the calibration coefficients.** See [§14 Calibration Snapshot].

### 11.3 Alarm config snapshot (`config/alarm.json`)

```jsonc
{
  "snapshot_timestamp": "...",
  "schema_version": 1,
  "items": [
    {
      "rule_id": "rule_boiler_high",
      "roi_id": "roi_boiler_1",
      "camera_id": "cam_001",
      "condition": "above",
      "severity": "critical",
      "threshold": 80.0,
      "threshold_low": null,
      "threshold_high": null,
      "unit": "celsius",
      "enabled": true,
      "description": "Boiler over 80C"
    }
  ]
}
```

---

## 12. ROI Snapshot

`config/rois.json` must support **all V3 geometry types**: Rectangle1,
Rectangle2, Circle, Ellipse, Polygon.

```jsonc
{
  "snapshot_timestamp": "...",
  "schema_version": 1,
  "items": [
    {
      "roi_id": "roi_boiler_1",
      "camera_id": "cam_001",
      "position_id": "preset_1",
      "name": "Boiler hot spot",
      "enabled": true,
      "geometry": {
        "shape": "rectangle1",
        "parameters": { "y1": 10, "x1": 20, "y2": 200, "x2": 300 }
      },
      "temperature_limits": {
        "unit": "celsius",
        "min_warning": 60.0,
        "max_warning": 75.0,
        "min_critical": null,
        "max_critical": 80.0,
        "rate_of_change_limit": null
      },
      "alarm_enabled": true,
      "metadata": {}
    },
    {
      "roi_id": "roi_tank_2",
      "camera_id": "cam_002",
      "position_id": "preset_1",
      "name": "Tank ellipse",
      "geometry": {
        "shape": "ellipse",
        "parameters": { "center_y": 240, "center_x": 320, "phi": 0.5, "radius1": 80, "radius2": 40 }
      },
      "temperature_limits": {},
      "alarm_enabled": true
    },
    {
      "roi_id": "roi_conveyor",
      "camera_id": "cam_003",
      "position_id": "preset_2",
      "geometry": {
        "shape": "polygon",
        "parameters": { "points": [[10,10],[300,40],[280,200],[20,180]] }
      },
      "temperature_limits": {},
      "alarm_enabled": false
    }
  ]
}
```

### Design decisions

- **Explicit data only** — no Python classes, no `ROIGeometry` objects.
- `shape` uses V3 `ROIShape` values (`rectangle1`, `rectangle2`, `circle`,
  `ellipse`, `polygon`).
- Parameters are the documented per-shape keys from `ROIGeometry`.
- Threshold/alarm limits are stored per-ROI (the historical alarm config), so
  offline re-evaluation has the original context.

---

## 13. PTZ Snapshot

PTZ state can change during a recording, so **one PTZ state for the whole
recording is wrong**. Use **state-change records with timestamps**.

`config/ptz.json`:

```jsonc
{
  "snapshot_timestamp": "...",
  "schema_version": 1,
  "items": [
    {
      "camera_id": "cam_001",
      "timestamp": 1755509400.0,       // wall clock, epoch seconds
      "position_id": "preset_1",
      "pan": 12.5,
      "tilt": -3.0,
      "zoom": 4.0,
      "focus": 1.0,
      "source": "preset" | "manual" | "scan"
    },
    {
      "camera_id": "cam_001",
      "timestamp": 1755509420.0,
      "position_id": "preset_2",
      "pan": -40.0,
      "tilt": 10.0,
      "zoom": 2.5,
      "focus": 1.0,
      "source": "preset"
    }
  ]
}
```

### Design rules

- PTZ state is **event-based / state-change records**, not per-frame.
- The reader can reconstruct PTZ state at any frame timestamp by finding the
  latest PTZ record with `timestamp <= frame.timestamp` (binary search over
  sorted-by-timestamp PTZ records).
- If no PTZ record exists before a frame, PTZ state is `unknown` for that
  frame; the manifest can record an initial PTZ state if known.
- The current `position_id` used by ROI resolution is derived from the
  effective PTZ state at the frame's timestamp, and per-frame `position_id`
  references may also be embedded in record metadata.

---

## 14. Calibration Snapshot

Raw thermal data must remain authoritative. The recording must preserve enough
calibration context to reproduce the original analysis.

```jsonc
{
  "snapshot_timestamp": "...",
  "schema_version": 1,
  "items": [
    {
      "camera_id": "cam_001",
      "calibration_id": "cal_v3_cam001_2026-07-01",
      "calibration_version": 1,
      "calibration_hash": "sha256:4f3c...",       // immutable identity
      "source": "sql_server" | "file",
      "effective_date": "2026-07-01T00:00:00Z",
      "segments": [                                // optional embedded coefficients
        {
          "range_index": 0,
          "calibration_min": -20.0,
          "calibration_max": 1200.0,
          "segments": [
            { "u0": 1234.5, "u1": 12.34, "u2": -0.0001, "start_temp": -20.0, "end_temp": 1200.0 }
          ]
        }
      ],
      "embedded": true,                            // whether segments above are present
      "notes": ""
    }
  ]
}
```

### Design decision

- **Best-effort embedding:** if the calibration source exposes coefficients
  (the V2 `CameraCalibration` polynomial segments), embed them. This makes the
  recording **fully self-contained** for re-analysis.
- **Hash/ID always stored:** even without embedded coefficients, the
  `calibration_id` + `calibration_hash` allow the offline system to locate an
  immutable calibration copy if available.
- **Offline must NOT depend on the current SQL calibration.** The recording
  must remain interpretable with the embedded context alone. If coefficients
  are absent, offline re-analysis can either use a user-supplied calibration
  or report that the historical calibration is unavailable — but the raw data
  is never lost.

---

## 15. Alarm Events

Persistent alarm events are stored in `events/alarms.json`. They **reference**
recorded frames — never duplicate the raw payload.

```jsonc
{
  "format_version": { "major": 1, "minor": 0 },
  "items": [
    {
      "alarm_id": "alarm_ab12cd34ef56",
      "camera_id": "cam_001",
      "roi_id": "roi_boiler_1",
      "rule_id": "rule_boiler_high",
      "alarm_type": "temperature_high",          // condition
      "severity": "critical",
      "threshold": 80.0,
      "unit": "celsius",
      "measured_value": 92.4,
      "start_sequence": 105,                      // per camera+stream
      "start_timestamp": 1755509410.0,
      "end_sequence": 118,
      "end_timestamp": 1755509415.0,
      "state": "active" | "cleared" | "ended",
      "frame_reference": {                        // index entry of trigger frame
        "stream": "IR",
        "sequence": 105,
        "chunk_seq": 1,
        "offset": 123456
      },
      "original": true,                           // from live analysis
      "reanalysis": null                          // set on derived re-analysis
    }
  ]
}
```

### Design rules

- Original alarm events are **immutable** — they record historical fact.
- Offline re-analysis creates **new derived** alarm events (see
  [§22 Offline Reprocessing]) that reference the same frames but are stored
  separately (e.g. `reanalysis_id`), never overwriting the original.
- `frame_reference` points into the index (chunk + offset) so the alarm can be
  correlated with the exact trigger frame.

---

## 16. Pre-Alarm / Post-Alarm Semantics

### Exact semantics

```
pre_alarm_seconds = 10
post_alarm_seconds = 20
```

1. Continuous rolling buffer holds the last `pre_alarm_seconds` of frames
   (per camera/stream).
2. On alarm trigger:
   - Recording opens (manifest `WRITING`).
   - Pre-alarm buffer drains to chunk → these are the frames immediately
     preceding the trigger.
   - The **trigger frame** (the frame that caused the alarm) is written.
   - Post-alarm frames continue for `post_alarm_seconds`.
3. On completion: index sealed, manifest `COMPLETE`.

### State machine (fixes the Stage 5A "alarm frame written on next feed" bug)

```
state: IDLE
  └─ arm() → ARMED (buffer starts filling)
  └─ feed_frame() → buffer.add(frame)  (always, when not recording)

state: ARMED
  └─ feed_frame() → buffer.add(frame)
  └─ trigger_recording(alarm, trigger_frame) →
        if recording already: extend post-alarm window (no new dir)
        else:
          1. create dir, manifest WRITING
          2. drain buffer → write records
          3. write trigger_frame record        ← trigger frame NOT lost
          4. state → WRITING

state: WRITING
  └─ feed_frame(frame) →
        if frame.sequence == trigger_sequence: skip (already written once)
        else: write record; post_alarm_frames_remaining -= 1
  └─ if post_alarm_frames_remaining <= 0 or manual stop:
        finalize() → close chunks, write index, seal manifest COMPLETE
        state → IDLE
```

### Ring / buffer relationship

The shared-memory ring is the **temporary live transport / rolling history**.
The Recorder owns the persistent data: it copies pre-alarm frames out of the
rolling buffer into the recording chunk on trigger. The ring is never the
permanent recording system.

---

## 17. Integrity

- **Checksum:** CRC32 (zlib) of header + metadata + payload, stored in each
  record trailer (4 bytes) and mirrored in the index entry.
- **What is hashed:** everything in the record except the checksum field.
- **Where stored:** record trailer (authoritative) + index entry (for quick
  validation without reading the payload).
- **When verified:**
  - On `RecordingReader.open`: validate manifest, then index entries vs chunk
    offsets.
  - On frame read: verify CRC32 before exposing the Frame.
  - On demand: full recording scan/verification tool.

### Integrity failures

| Check | Failure → status |
|---|---|
| Manifest missing / unparseable | `CORRUPTED` |
| Index entry beyond chunk EOF | `CORRUPTED` |
| Record CRC mismatch | `CORRUPTED` (or per-record skipped + flagged) |
| Payload length ≠ header length | `CORRUPTED` |
| dtype/dimensions mismatch with manifest | `CORRUPTED` |

---

## 18. Crash Recovery

A partially written recording must be detectable and never silently treated as
valid.

### Detection mechanisms

1. **Manifest status** — written `WRITING` at open, atomically rewritten
   `COMPLETE` only at finalize. If the manifest says `WRITING` (or the
   `finalized_at` marker is absent) at open time, the recording is
   **INCOMPLETE**.
2. **Chunk trailer** — each chunk ends with `TMSQ` end-marker + record count +
   chunk CRC. A chunk without a valid trailer is truncated; the reader stops
   there and flags the recording incomplete.
3. **Record length / CRC** — if the last record's declared `payload_length`
   extends past EOF, the last record is incomplete → truncated tail detected.
4. **Index vs chunk count mismatch** — index entries pointing past chunk EOF
   are invalid → recording flagged.

### Reader behavior on incomplete/corrupt

- `open()` returns a valid reader with `status = INCOMPLETE` or `CORRUPTED`
  (never raises silently).
- The reader exposes valid records up to the truncation point, and the last
  valid record is the boundary. Subsequent seeks past the boundary fail
  cleanly with a `RecordingReadError`.
- A corrupted record mid-file is either skipped with a warning (when the
  reader is configured for tolerant scan) or surfaced immediately.

### Atomicity

- Manifest is small; rewrite on finalize is effectively atomic (write temp +
  rename). `finalized_at` present ⇔ complete.
- The index is written last, after all chunks are closed.

---

## 19. Versioning

### Version strategy

```
FORMAT_MAJOR . FORMAT_MINOR
```

- **Major:** changes that break backward/forward compatibility (record layout,
  chunk layout, index layout, manifest required fields).
- **Minor:** additive changes (new optional metadata keys, new pixel formats,
  new sync status codes) that old readers can safely ignore.

### Compatibility rules

| Reader version | Recording version | Result |
|---|---|---|
| same major, minor ≥ recording minor | ✔ read normally | |
| same major, minor < recording minor | ✔ read (ignore unknown optional fields) | |
| older major than recording | **reject cleanly** with clear error | |
| newer major than recording | reject (unknown format) | |

### Rules

- Compatibility is a function of the **format version only**, never of Python
  package versions.
- Every record header carries `record_version` (currently 1) so the reader can
  validate per-record.
- Evolving the format later = bump minor for additive, major for breaking.

---

## 20. SQL Server Boundary

SQL Server stores **application-level recording metadata** for discovery and
indexing:

| Field | Purpose |
|---|---|
| `recording_id` | PK / key |
| `camera_id(s)` | which cameras |
| `start_time`, `end_time` | time range |
| `alarm_summary` | trigger alarm, count |
| `storage_location` | path to the recording directory |
| `status` | COMPLETE / INCOMPLETE / CORRUPTED |

SQL does **not** store raw frame payloads as BLOBs. The recording storage owns
the raw payload.

### Rules

- All SQL lives in repositories under `storage/repositories/` (already exists:
  `recording.py`).
- A recording must remain **readable without SQL Server** — SQL is a discovery
  convenience only. The manifest is the authoritative metadata.
- The repository never reads frame payloads from the DB; it only maps
  metadata rows to/from `RecordingMetadata`.

---

## 21. Offline Frame Reconstruction

### Index entry → Frame

```
index entry
   │  (camera_id_ref → camera_id, stream, sequence, timestamp,
   │   chunk_seq, offset, payload_length, checksum, width, height)
   ▼
open chunk_<chunk_seq>.tmsr
   │  seek to offset
   ▼
read record header (fixed 128B)  ── validate magic/version
   ▼
read metadata tail (JSON)
   ▼
read payload_length bytes
   ▼
verify CRC32(header + metadata + payload)
   ▼
reconstruct FramePayload (np.frombuffer, read-only, reshape (height, width))
   ▼
reconstruct FrameDescriptor (StreamMetadata for the one stream; other stream
present=False; sync from header / sync group metadata)
   ▼
return Frame  (identical V3 contract)
```

### ADR-002 consequence

Reconstruction produces a V3 `Frame`. For a single-stream record:

- IR record → `Frame(thermal=<array>, visible=None)` with
  `visible.present=False`, `sync.status=MISSING_VISIBLE`.
- VL record → `Frame(visible=<array>, thermal=None)` with
  `thermal.present=False`, `sync.status=MISSING_THERMAL`.

A logical pair (via `sync_group_id`) may be reconstructed as a
`Frame(thermal=<IR>, visible=<VL>)` with `sync.status=SYNCHRONIZED` when both
records exist. **ADR-002 does not need to change** — the existing contract
already models missing streams.

---

## 22. Offline Reprocessing

The pipeline is source-agnostic. Offline reuses the exact same processing
code:

```
Recording
   ↓
RecordingReader
   ↓
OfflineFrameSource
   ↓
Frame
   ↓
TemperatureConverter (current or historical calibration)
   ↓
ROIResolver (current or historical ROI config)
   ↓
HalconROIAdapter
   ↓
AnalysisResult
   ↓
AlarmEvaluator
   ↓
new derived AnalysisResult / AlarmEvent (never overwrite original)
```

- **No** `OfflineTemperatureConverter`, `OfflineROIProcessor`,
  `OfflineAlarmEvaluator`. The same pipeline classes are used.
- The offline pipeline can select either the historical calibration/ROI
  snapshots (from the recording) or a current configuration supplied by the
  user — both are available to the same `TemperatureConverter` /
  `ROIResolver` interfaces.
- Original alarm events and original analysis are **preserved**; reprocessing
  produces new derived results.

---

## 23. Offline Configuration (Historical vs Current)

Two distinct concepts:

### A. Historical configuration (recording-embedded)

- Camera config snapshot, ROI snapshot, PTZ state, calibration snapshot, alarm
  config — captured at recording time.
- Stored in `config/*.json` inside the recording.
- **Never modified** by offline analysis.

### B. Current Offline analysis configuration (user-chosen)

- What the user selects while analyzing: different calibration, different ROI
  set, different alarm thresholds.
- Passed to the pipeline at analysis time; never written back into the
  recording.

### Rule

> Offline analysis must never modify the historical recording data. If a user
> wants to persist a new analysis, it is stored as a **derived re-analysis**
> (new file/result set), separate from the recording.

---

## 24. Shared Memory Boundary

```
SharedMemoryRingBuffer
        = live transport / rolling history (temporary)

RecordingFormat
        = persistent storage (authoritative)
```

- The recording format does **not** copy the SHM slot layout.
- The recorder consumes `Frame` objects (ADR-002 contract) and writes the
  recording format; it never reads SHM layout internals.
- The ring may feed the recorder's rolling buffer, but the recorder owns
  persistent data and copies frames it needs on trigger.
- They are decoupled by the Frame contract only.

---

## 25. Offline Module Location & Migration Plan

### Current state (Stage 5A audit)

`OfflineFrameSource` currently lives in `processing/sources.py`
(`src/thermal_monitor/processing/sources.py:49`).

### Target layout

```
src/thermal_monitor/
├── processing/
│   ├── sources.py            # KEEP: LiveFrameSource, SyntheticFrameSource (FrameSource protocol)
│   └── pipeline.py           # FrameSource Protocol
├── offline/
│   ├── __init__.py           # exports RecordingReader, OfflineFrameSource, PlaybackService
│   ├── source.py             # OfflineFrameSource (MOVED here) — implements FrameSource protocol
│   ├── reader.py             # RecordingReader — reads recording dir, index, chunks
│   └── playback.py           # PlaybackService — play/pause/stop/seek/speed
├── storage/
│   ├── recording/
│   │   ├── writer.py         # RecordingWriter (new)
│   │   ├── format.py         # binary format constants + struct layout + enums
│   │   ├── index.py          # IndexWriter / IndexReader
│   │   └── chunks.py         # ChunkWriter / ChunkReader
│   └── ...
```

### Migration plan (after Stage 5B approval)

1. **Stage 5C:** implement `storage/recording/` writer + reader + format.
2. **Stage 5D:** move `OfflineFrameSource` from `processing/sources.py` to
   `offline/source.py`; re-export from `processing.sources` (or update
   importers) to avoid breaking existing tests.
3. **Stage 5E:** implement `offline/playback.py` PlaybackService.
4. **Stage 5F/G:** round-trip and pipeline tests.
5. `services/offline.py` is updated to delegate to the new
   `offline/source.py` + `offline/playback.py`; the old pickle-based
   `FileRecordingSink` path is deprecated.

---

## 26. Performance Estimates

### Measured rates (current hardware)

| Stream | Resolution | Bytes/frame | Measured FPS |
|---|---|---|---|
| Thermal | 640 × 480 uint16 | 614,400 | ~7.25 |
| Visible | 640 × 480 YUV422_8 | 614,400 | ~8.94 |

### Per-camera raw payload rate

- Thermal: 614,400 × 7.25 ≈ **4.45 MB/s**
- Visible: 614,400 × 8.94 ≈ **5.49 MB/s**
- Total per camera ≈ **~10 MB/s**

### Scenario estimates (raw payload only; excludes headers/index/manifest)

| Scenario | One camera | Eight cameras |
|---|---|---|
| 10 seconds | ~100 MB | ~800 MB |
| 30 seconds | ~300 MB | ~2.4 GB |
| 1 minute | ~600 MB | ~4.8 GB |
| 1 hour | ~36 GB | ~288 GB |

> These figures validate the need for **chunked files** (not one file per
> frame) and for keeping raw frames out of SQL. At 8 cameras the raw rate is
> **~72 MB/s ≈ 576 Mbps**, well within disk/SSD sustained write throughput but
> significant for storage budgeting. Disk space planning is a separate
> operational concern (purge/retention policy), not a reason to change the
> raw-data requirement.

---

## 27. Open Decisions

| # | Decision | Status / recommendation |
|---|---|---|
| 1 | IR/VL representation | **Recommended:** separate physical IR/VL records + optional `sync_group_id` logical groups (least lossy, matches alternating wire). Await approval. |
| 2 | CRC32 vs CRC32C | Use CRC32 (zlib) — sufficient, stdlib, no crypto. |
| 3 | Chunk size | 64 MiB payload target (configurable constant). Validate during Stage 5C performance tests. |
| 4 | Index in memory vs mmap | mmap for large recordings; in-memory list for small. Decide in implementation based on measured index sizes. |
| 5 | Calibration embedding | Embed polynomial coefficients when available; always store `calibration_id` + hash. |
| 6 | Compression | Out of scope for authoritative payload; future additive format feature. |
| 7 | PTZ per-frame vs event | Event-based state-change records (recommended). |
| 8 | Recording-relative timestamp | Use `timestamp - recording_start` as `recording_relative_timestamp`; store in metadata tail so exact value is preserved. |
| 9 | Association semantics | `sync_group_id` optional; `sync_status` default `unknown` until acquisition defines pairing. |

---

## 28. Migration from V2 / Current V3 Format

### Current V3 format (to be replaced)

- `FileRecordingSink` (`storage/recording.py:294`) writes a single file
  `*.tmsrec` with:
  - Header: `{magic, version, metadata}` JSON
  - Frames: `[frame_length(4B), pickle_frame]` repeated.
- Uses `pickle` → not deterministic, not forward-compatible, not inspectable.
- No index, no chunking, no integrity, single-camera assumption.

### Migration path

1. New format (this design) becomes the only write target for Stage 5C+.
2. A **reader shim** for old `*.tmsrec` pickle files can be provided for
   transitional inspection, but is **read-only** and explicitly marked
   deprecated. Existing tests that rely on `OfflineFrameSource.from_recording_file`
   are updated to the new writer/reader.
3. The old `FileRecordingSink` class remains only for backward-compatible
   tests; it is removed once no test/consumer depends on it.
4. V2 recordings (if any raw format exists in `reference/TMS_v2/`) are
   **not** directly readable by V3 — the new format is V3-native.

---

## 29. Inspectability

A developer can inspect a recording without running the application:

- `manifest.json` — human-readable JSON.
- `config/*.json` — human-readable snapshots.
- `events/alarms.json` — human-readable.
- `index.bin` — binary but fully documented (this spec); a small CLI tool
  (`python -m thermal_monitor.storage.recording.tools inspect <dir>`) is
  proposed for Stage 5C+ to dump manifest/index summaries and to run a
  full-CRC verification pass.

---

## Appendix A — Binary Layout Constants (reference)

| Constant | Value |
|---|---|
| Record magic | `0x54 0x4D 0x53 0x52` = `"TMSR"` |
| Chunk magic | `"TMSR"` (header) / `"TMSQ"` (trailer end marker) |
| Index magic | `"TMSI"` |
| Record header size | 128 bytes fixed |
| Index entry size | 56 bytes fixed |
| Record version | 1 |
| Payload chunk max | 64 MiB (target) |
| Checksum | CRC32 (zlib), 4 bytes |
| Endianness | little-endian |
