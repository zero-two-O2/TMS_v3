# ADR-003: Shared-Memory Ring Buffer Architecture

## Status

Proposed

## Date

2026-08-16

## 1. Context

V3 will run up to 8 TV46L cameras, each producing high-rate thermal data and
(potentially) visible data. ADR-001 defines shared-memory frame transport as a
V3 requirement. ADR-002 defines the immutable `Frame` contract with the
requirement that multiple independent consumers (Processing, Observer, Recorder,
Diagnostics) read the same acquired stream without blocking acquisition.

The current acquisition foundation (ADR-001, `v3-acquisition.md`) already
isolates the transport behind a `FramePublisher` protocol:
`AcquisitionWorker.publish(frame) -> PublishResult` in
`src/thermal_monitor/camera/acquisition.py:47`. `InProcessLatestPublisher` is a
temporary development stand-in. This ADR designs the production transport that
replaces it.

V2 (reference only) used `queue.Queue(maxsize=2)` with drop-oldest backpressure
and no sequence tracking in the production path (`acquisition_engine.py:39`).
V3 explicitly rejects that model: it requires independent consumers, measurable
frame loss, latest-frame access, and bounded memory.

## 2. Goals

- High-throughput frame transport with minimal copying.
- Multiple independent consumers reading one camera stream at their own speed.
- A slow consumer must never block camera acquisition.
- Bounded, predictable memory usage.
- Latest-frame access and sequential access on the same transport.
- Per-camera monotonic sequence tracking with dropped/overwritten-frame detection.
- Immutable payload access with no consumer-to-consumer interference.
- Deterministic producer/consumer synchronization.
- One camera failing must not affect other cameras' frame transport.
- A design that permits zero-copy acquisition later without changing consumers.
- Support for later CPU and GPU processing without transport redesign.
- Support for future multiple-Python-process operation without redesign.

## 3. Non-goals

- **Not implemented here.** This ADR specifies architecture only.
- No GPU memory transport design (deferred; §17 only analyses constraints).
- No ring depth finalization (blocked on hardware measurement; §15, §24).
- No visible-stream acquisition decision (blocked on hardware; §18).
- No raw recording file format (separate ADR).
- No offline file format (separate ADR; §21 only defines the compatibility rule).
- No compression of payloads.
- No cross-camera transport (per-camera isolation; §13).
- No shared-memory persistence across application restarts (see §14).

## 4. Architecture Diagram

One ring buffer per camera, owned by the camera's acquisition worker:

```
                Camera 1              ...              Camera 8
                  │                                          │
        ┌─────────▼─────────┐                    ┌──────────▼─────────┐
        │ AcquisitionWorker │                    │ AcquisitionWorker  │
        │ (producer thread) │                    │ (producer thread)  │
        └─────────┬─────────┘                    └──────────┬─────────┘
                  │ publish(Frame)                          │ publish(Frame)
        ┌─────────▼─────────┐                    ┌──────────▼─────────┐
        │ RingBuffer<cam1>  │                    │ RingBuffer<cam8>   │
        │ bounded slots     │                    │ bounded slots      │
        └─┬────┬────┬────┬──┘                    └─┬────┬────┬────┬───┘
          │    │    │    │                         │    │    │    │
     ┌────▼┐ ┌▼───┐ ┌▼──┐ ┌▼────────┐         (independent)
     │Proc │ │Obs │ │Rec│ │Diag     │
     │seq N│ │seqM│ │   │ │         │
     └─────┘ └────┘ └───┘ └─────────┘
```

Each ring buffer is a private object created by, and shut down with, its camera
worker. There is no global transport, no shared producer lock between cameras,
and no shared consumer registry. Consumers obtain a handle to a specific
camera's buffer (by `camera_id`); they never reach into another camera's slots.

### 4.1 Data-flow note

```
Acquisition
    ↓
Shared-Memory Ring Buffer   (one per camera)
    ├── Processing     (sequential or latest per mode)
    ├── Observer       (latest only)
    ├── Recorder       (sequential + pre-alarm history)
    └── Diagnostics    (periodic sampling, gap/window stats)
```

## 5. Frame / Descriptor / Payload Relationship

### 5.1 What goes into shared memory

The complete Python `Frame` object (ADR-002) must NOT be placed in shared
memory: it contains dataclass instances, a `MappingProxyType`, and NumPy array
objects with process-local pointers. Python objects do not cross process
boundaries. Even in single-process operation, treating the descriptor as a
Python object would defeat the purpose of a shared, byte-layout transport.

The transport splits the frame the same way ADR-002 already does:

```
In-process (per consumer, immutable after publication)
  FrameView
  ├── descriptor: FrameDescriptor  (ADR-002 object, built from shared header)
  └── payload:    FramePayload     (read-only NumPy views over shared bytes)
```

```
In shared memory (owned by the ring buffer)
  Slot
  ├── SlotHeader        (fixed-size, versioned)
  │     ├── slot_state   (EMPTY / WRITING / PUBLISHED / VALID / INVALID)
  │     ├── generation   (monotonic per slot reuse)
  │     ├── sequence     (per-camera acquisition sequence)
  │     └── flags
  ├── DescriptorRegion  (fixed-size binary blob; see §5.3)
  └── PayloadRegion     (fixed-size byte areas: thermal, visible)
```

The producer writes payload bytes, then writes the descriptor region, then
atomically flips the slot to PUBLISHED. Consumers read PUBLISHED slots only and
validate `generation` + `sequence` (§6, §11).

### 5.2 Descriptor contents

The shared descriptor must contain everything needed to locate and interpret
the payload without reading the in-process `Frame`:

- `camera_id` (fixed-width string, serial-derived, e.g. `cam_HB25080011`)
- `sequence` (per-camera monotonic)
- `timestamp` (wall clock, seconds)
- `monotonic_timestamp` (perf_counter seconds)
- thermal metadata: `present`, `width`, `height`, `pixel_format`,
  `bits_per_channel`, `dtype`, `byte_count`, stream `sequence`,
  stream timestamps (`timestamp`, `monotonic_timestamp`, `hardware_timestamp`)
- visible metadata: same fields as thermal
- sync: `status` + `time_delta` (ADR-002 `SyncInfo`)
- acquisition metadata: `grab_started`, `grab_completed`, `converted_at`,
  `grab_duration_s` (mirrors current `descriptor.metadata`)
- payload layout: slot index, thermal byte offset + byte count, visible byte
  offset + byte count, frame validity flag

### 5.3 In-process vs compact shared descriptor — tradeoff

| Approach | In-process descriptor | Compact shared descriptor |
| --- | --- | --- |
| Build cost | Zero (reuse ADR-002 object) | One small struct pack/unpack per frame per consumer |
| Cross-process | Impossible | Possible (later) |
| Zero-copy later | Blocks it (descriptor must be in shm to be zero-copy) | Enables it |
| GPU path | Descriptor stays CPU | Descriptor stays CPU (payload is what moves) |
| Debuggability | Native Python | Requires a decoder + versioning |

**Decision: compact fixed-layout descriptor in shared memory.** The producer
writes it; consumers decode it into an ADR-002 `FrameDescriptor` object for the
parts they need. This keeps the ADR-002 contract the consumer-facing interface
while making the transport process-capable and zero-copy-ready. The cost is one
small pack/unpack per publish/consume — negligible next to ~1.5 MB of payload.

The descriptor region uses a fixed-size, versioned binary layout (`layout_major`,
`layout_minor` in the header). Version mismatch → refuse to attach (prevents
cross-version corruption).

## 6. Ring Slot Design

```
Slot i (fixed size = SLOT_BYTES)
┌──────────────────────────────────────────┐
│ SlotHeader        (fixed, ~256 B)        │
│ DescriptorRegion  (fixed, ~4 KB)         │
│ ThermalPayload    (fixed, THERMAL_BYTES) │
│ VisiblePayload    (fixed, VISIBLE_BYTES) │
└──────────────────────────────────────────┘
```

### 6.1 Fixed-size vs variable-size slots

**Decision: fixed-size slots.** Rationale:

- Predictable memory layout and pointer math (no allocator, no free list, no
  fragmentation).
- Bounded, provable worst-case memory usage.
- Alignment: payloads can be aligned to 64-byte boundaries, important for
  AVX/NumPy and for potential GPU staging.
- Crash recovery is simpler: a fixed map can be validated slot-by-slot.

Variable-size regions add a heap/allocator inside shared memory and make every
consumer boundary-check non-trivial. They only pay off if frame size varies
significantly (e.g., ROI crops). TV46L frames are fixed-geometry, so fixed slots
win.

### 6.2 Slot sizing rule

`THERMAL_BYTES` and `VISIBLE_BYTES` must be the **maximum** expected payload
size for that stream, padded (e.g., to 64 B). Actual byte counts are stored in
the descriptor so consumers can slice exactly. If hardware shows two visible
formats (RGB8 vs YUYV), allocate for the larger and record the smaller actual
count in the descriptor — a consumer never over-reads.

### 6.3 Slot state machine

```
EMPTY ──producer reserve──▶ WRITING ──producer commit──▶ PUBLISHED
 PUBLISHED ──consumer validated+done + producer needs slot──▶ EMPTY (reuse)
 PUBLISHED ──validation failure──▶ INVALID (sticky, logged) ──▶ EMPTY on producer reset
```

- `WRITING`: producer owns the slot exclusively; consumers must skip it.
- `PUBLISHED`: frame complete; consumers may attach.
- `EMPTY`: available for reuse.
- `INVALID`: producer or consumer detected corruption; logged once, slot
  recycled by the producer after diagnostics reads it.

## 7. Producer Algorithm

The producer is the `AcquisitionWorker` thread publishing through the
`FramePublisher` protocol. Publication order (all steps under the producer's
write lock / single-writer discipline):

```
1. acquire frame (existing driver grab)                          [unchanged]
2. pick next slot = (producer_cursor + 1) % depth
3. reserve: set slot_state = WRITING (under header lock), bump per-slot generation
4. copy thermal bytes into ThermalPayload region
   copy visible bytes into VisiblePayload region
5. write DescriptorRegion (offsets, dims, dtype, timestamps, sequence, sync, validity)
6. publish: set slot_state = PUBLISHED   ← release/visibility point
7. advance producer sequence (local int); write producer head sequence/position
8. return PublishResult(accepted=True, sequence, dropped=False,
                        overwritten_sequence=previously PUBLISHED sequence
                        in the slot we just reused)
```

### 7.1 Ordering requirements

- Payload bytes and descriptor MUST be fully written before `PUBLISHED` is set.
- `PUBLISHED` is the only flag consumers gate on; it is written last.
- The producer head (latest published sequence) is updated in the shared
  control block AFTER the slot publish.
- The per-slot `generation` is bumped at reserve time (step 2) so any consumer
  holding an old view of that slot detects reuse (see §11).

### 7.2 Overwrite semantics

If the slot being reused still holds a PUBLISHED frame that some consumer has
not read, that frame is **overwritten** by definition. The producer must never
wait for it. `PublishResult.overwritten_sequence` reports the lost sequence to
acquisition for gap accounting; consumers detect the loss independently via
their per-consumer sequence (§9).

### 7.3 Synchronization mechanism — Phase 1 (single process/thread)

Within one process, CPython threads share memory. The GIL guarantees that a
single `int` store is atomic and that `struct.pack_into` of a small header does
not tear at the byte level, **but it does not guarantee memory ordering across
threads and does not exist across processes**. We therefore do not rely on the
GIL. Phase 1 uses:

1. **A small `threading.Lock` per ring buffer for slot state transitions**
   (reserve / publish / reuse / pin count changes). The critical sections are a
   few integer/flag stores; contention is negligible at ≤9 FPS producer rate.
   This lock gives the producer the memory barrier semantics it needs inside
   one process. **It does not, by itself, establish visibility for a consumer
   read that does not acquire the same lock.** A consumer that must read a
   published slot safely either acquires the lock for its read, holds a pin
   (which uses the same lock/reader-count protocol), or copies the payload into
   consumer-owned memory under the same ordered protocol (§8.2, §8.3).

2. **Generation counters** (per slot, monotonic) instead of trusting sequence
   alone. Generation + sequence together make stale-view detection robust even
   if a consumer read the header before the producer finished writing.
   Generation validation detects a reused/stale slot **after** the fact; it
   does not make an unpinned read safe while the producer overwrites the slot
   (§8.2).

3. **No condition variables / no events for frame arrival.** Consumers poll or
   block on their own notification, never on a shared condition tied to the
   producer loop. The producer never blocks on any consumer.

### 7.3.1 Phase 3 (future) — multi-process Windows synchronization

The same control block layout is intended to be portable to a later
multi-process deployment, but **no cross-process lock-free safety is claimed
before it is designed and tested**. Phase 3 will design and test the actual
Windows synchronization mechanism (Windows named mutex/events, or an explicit
protocol around the packed header — including the barrier/ordering semantics of
the two-stage publish across processes) before any process-safe claim is made
(§10, §18). This is why the per-slot header uses plain packed integers rather
than Python objects.

Rationale summary: deterministic, bounded, non-blocking-for-the-producer,
cheap, and portable to the multi-process case without changing the slot layout.
We deliberately avoid lock-free CAS rings now: they are harder to verify, harder
to extend to GPU staging, and the producer rate (≤9 FPS per camera) does not
require them.

### 7.4 Producer never blocks

Every producer step is bounded and independent of consumers:
- slot pick: O(1)
- copy: fixed size
- publish: lock-guarded flag flip
- no consumer wait, no slow-consumer backpressure, no condition wait.

## 8. Consumer Algorithm

A consumer holds a `Consumer` object with its own position state. Its loop:

```
1. get_next() / get_latest()          (choose strategy, §12/§13)
2. validate sequence                  (expected vs slot.sequence)
3. validate slot generation           (slot.generation vs last-seen generation)
4. obtain read-only payload view      (np.ndarray view over shm bytes; no copy)
5. process                            (consumer-owned; may take any time)
6. release/advance consumer position  (update local sequence, last generation)
```

### 8.1 Read-only NumPy view

Where safe (the payload region layout is fixed at creation), the consumer maps
`ThermalPayload`/`VisiblePayload` bytes to a NumPy array via
`np.ndarray(shape, dtype, buffer=shm_bytes, offset=thermal_offset)` and sets
`writeable=False`. This is a **zero-copy view**: no frame-sized allocation, no
memcpy. The `FramePayload` constructor already enforces `writeable=False`
(`frame.py:107`), so a view-based `FramePayload` satisfies the ADR-002 contract
unchanged.

If a consumer must retain the data beyond the slot's lifetime, it makes one
explicit `copy()` — that is the consumer's choice, never the transport's.

### 8.2 The lifetime problem: producer overwrites while consumer reads

The producer can overwrite slot `i` while a consumer still holds a view into it.
The view's memory is the same physical bytes, so the consumer would silently
read torn data (thermal from frame N, visible from frame N+1, or a partially
written frame).

**What generation + sequence validation does and does NOT guarantee:**

- The producer bumps the slot's `generation` when it reserves the slot for
  reuse (before writing new bytes).
- A consumer that grabbed slot `i` at generation `G` verifies, on every access
  (or before the first read after any potential reuse), that the slot still has
  generation `G` and the expected sequence. If it changed, the view is stale:
  the consumer discards it, records an **overwritten** event, and re-reads the
  slot (or moves on).
- Validation therefore detects a **stale or reused slot**. It does **NOT**
  guarantee that a consumer can safely process a frame while the producer
  overwrites that slot. If the producer reuses slot `i` mid-read, the consumer
  may already hold mixed/changed data before the next validation runs.
  Generation + sequence validation detects the problem **after** it happens; it
  cannot prevent torn reads.

Consequently, the transport does not claim that any consumer can process an
unpinned slot safely. The rule is per-consumer:

- **Observer** may use an unpinned best-effort `FrameView` (`latest()`). It is
  display-oriented; a stale or torn intermediate frame is acceptable because
  the next refresh renders a new complete frame.
- **Diagnostics** may use unpinned access limited to metadata/header and
  counters where possible; it must not rely on payload integrity it cannot
  hold.
- **Processing** must either **pin** the slot for the duration of processing
  OR **copy** the payload into processing-owned memory before processing.
  ROI temperature / alarm analysis reading a frame while the producer
  overwrites it is unacceptable.
- **Recorder** pins only briefly, while copying the required frame data into
  recording-owned memory.

**Producer remains non-blocking.** If no reusable unpinned slot exists (e.g.,
all slots pinned), `publish()` returns `PublishResult(accepted=False)` and the
acquisition worker records the drop. Dropping a frame is always preferable to
corrupting an analysis frame.

### 8.3 Pins

A `pin(slot, generation)` increments a per-slot reader count in the shared
control block; the producer, before reusing a slot, skips slots with
`reader_count > 0` and advances to the next slot. Pins therefore slow the
producer only when absolutely needed and never block acquisition outright — at
worst, with all depth slots pinned, `publish()` returns `accepted=False`
(dropped) and the worker's existing `dropped` accounting applies.

Policy:
- **Processing pins** for the duration of its analysis, or copies the payload
  into processing-owned memory before processing. This is a correctness
  requirement for no-tear analysis, not an optimization (§8.2).
- **Recorder pins** only briefly, while copying the frame data it needs into
  recording-owned memory.
- **Observer** and **Diagnostics** use unpinned best-effort views with
  generation validation; a stale or overwritten result is acceptable for them.

The producer never waits: if no reusable unpinned slot exists, `publish()`
returns `accepted=False` and the drop is recorded. This keeps the common path
zero-blocking while making Processing reads safe.

### 8.4 Who owns what

- The ring buffer owns all shared memory for its lifetime.
- A consumer never holds a slot past a single access cycle unless pinned.
- Closing a consumer only releases its position/state; it never touches
  another consumer's state or the shared memory.

## 9. Per-Consumer Tracking

There is no global read pointer. Each consumer owns:

```
Consumer state (in-process, not shared)
  last_sequence: int          # last frame successfully consumed
  last_generation: int        # per-slot generation of that frame
  expected_sequence: int      # = last_sequence + 1 (for sequential mode)
  overwritten_count: int      # frames lost to slot reuse before read
  gap_count: int              # published sequence jumps detected
  stale_count: int            # generation mismatch while reading
```

Example:

```
Producer head sequence ......... 120
Processing → next expected 100 → catches 101..119 as overwritten/gap
Observer   → latest() → 120
Recorder   → next expected 95  → sees slots 96..119 already reused → gap
Diagnostics → samples 120 on demand
```

### 9.1 How a consumer detects each condition

- **Overwritten frames:** `slot.sequence != expected_sequence` and the slot
  generation is newer than `last_generation`. The frame that should have been
  `expected_sequence` no longer exists because the producer reused the slot.
  Counted as `overwritten_count`; if the gap spans multiple sequences, each
  missing sequence is counted.
- **Sequence gaps (acquisition-side):** the producer itself can skip (camera
  drop, NUC blackout, reconnect). A consumer with `expected_sequence` sees
  `slot.sequence > expected_sequence + 1`. The missing sequences are counted as
  `gap_count`. These are *acquisition gaps*, distinct from consumer
  overwrites; both are reported.
- **Stale data:** the consumer held a view; on revalidation the slot generation
  changed. Counted as `stale_count`, view discarded.
- **Invalid slots:** `slot_state == INVALID`, or the descriptor fails structural
  validation (magic/version/length mismatch). Counted and skipped; never read.

Counters are exposed per consumer (e.g., `Consumer.stats()` → dataclass) for
Diagnostics without any shared lock.

## 10. Synchronization Strategy (Phase 1: single process/thread)

- **One producer per ring buffer.** Single-writer discipline removes the need
  for producer-side locks on payload writes.
- **Per-ring `threading.Lock`** for slot state transitions only (reserve /
  publish / reuse / pin count changes). Critical sections are tens of
  instructions. Consumers that must not tear acquire the same lock for their
  read, or pin (same lock/reader-count protocol), or copy (§8.2, §8.3).
- **Per-slot packed-integer header** (state, generation, sequence) read by
  consumers for **stale/reuse detection**. The generation compare detects that
  a slot was reused; it does not, by itself, guarantee a torn-free unpinned
  read (§8.2). Unpinned reads are best-effort and permitted only where stale
  data is acceptable (Observer, Diagnostics).
- **Consumer state is in-process only** — no cross-consumer locking at all.
- **The GIL is treated as an implementation detail, not a guarantee.** Ordering
  comes from lock acquire/release, and the lock is acquired by the producer and
  by consumers that need a guaranteed-safe read. The packed-header generation
  protocol is for stale detection, not a substitute for synchronization.
- **Phase 3 (multi-process) synchronization is not designed or claimed here.**
  Cross-process behavior (Windows named mutex/events, or an explicit protocol
  over the packed header) will be designed and tested on Windows before any
  claim of process-safe lock-free operation is made (§7.3.1, §18).
- **Shutdown:** producer sets a shared `closed` flag, then stops. Consumers see
  `closed` and treat further reads as done. All ring locks are released before
  the buffer is torn down (see §19).

## 11. Sequence / Generation Strategy

- `sequence`: per-camera monotonic integer assigned by the acquisition worker
  (already implemented: `acquisition.py:439-441`). Monotonic across reconnects
  within a worker lifetime. Used for drop/gap detection and for consumer
  `next(expected_sequence)`.
- `generation`: per-slot monotonic counter incremented on every reuse. Used to
  detect that a previously-read slot now holds different data. Starts at 1 and
  wraps only after 2^63 reuses — not a practical concern.
- A consumer identifies a frame by the pair `(generation, sequence)`. This
  removes ambiguity when a slot is reused and its sequence coincidentally
  matches a consumer's expected value (possible after worker restart).
- Producer head (latest published sequence) lives in the shared control block
  so `latest()` and Diagnostics read it without scanning all slots.

## 12. Latest-Frame Semantics (`latest()`)

`latest()` returns the highest-sequence PUBLISHED slot at the moment of the
call. Semantics:

- Snapshot semantics: returns one complete frame (generation-validated), not a
  guarantee that it is still current on return.
- The consumer advances its `last_sequence` to the returned sequence. Frames
  between the consumer's previous position and the returned sequence are not
  consumed and are **not** counted as consumer overwrites — the consumer
  deliberately skipped them (latest-wins policy).
- Observer uses this exclusively: it wants the newest frame, and dropping
  intermediate frames is expected and free.
- Used for the UI refresh loop and for "show current" diagnostics.

## 13. Sequential-Consumer Semantics (`next(expected_sequence)`)

`next(expected_sequence)` returns the PUBLISHED slot whose `sequence` equals
`expected_sequence`, or None if that frame no longer exists (overwritten) or
has not yet been published. Semantics:

- Deterministic per-frame ordering with gap accounting.
- If the slot holding `expected_sequence` was overwritten before the consumer
  read it, the consumer sees `None`, increments `overwritten_count` for each
  skipped sequence, and advances `expected_sequence` to
  `producer_head + 1` (or re-reads latest) — i.e., it re-anchors to live data.
- If the frame simply has not arrived yet, `next()` returns None immediately
  (non-blocking) or waits briefly on the consumer's own poll interval. The
  consumer never blocks the producer.
- Processing uses this when it must not miss frames (e.g., alarm correlation).
- Recorder uses this to follow the stream and to detect gaps before an alarm.

Non-goal: `next()` is not a blocking `get()`. Any consumer that must wait polls
its own timer; nothing in the transport ever blocks the producer.

## 14. Pre-Alarm History Support

**The ring is not the recording system.** It is a bounded rolling history of the
last `depth` frames:

```
producer_head = N
[N-depth+1 ... N-1 N]   (all PUBLISHED until reused)
```

The product-required pre-alarm duration must NOT be assumed to fit entirely
inside the ring. The required pre-alarm window in frames follows from the
requirement:

```
FPS × pre_alarm_seconds = required_pre_alarm_frames
```

At 9 FPS: 10 s of pre-alarm = 90 frames; 20 s of post-alarm = 180 frames.
Sizing the ring to the full window per camera would cost ~1.54 MB × 180 × 8
cameras ≈ 2.2 GB — not the intended design. The ring therefore provides only
the bounded rolling history; the Recorder owns the recording memory:

When an alarm fires for camera C at sequence `N`:

1. Recorder queries the ring for the pre-alarm window `[N - pre_frames, N]`
   where `pre_frames` ≤ `depth - 1` (as much as the ring still holds).
2. For each sequence in the window that is still PUBLISHED (not yet
   overwritten), Recorder **pins and copies** the payload into its recording
   buffer (or writes directly to the recording stream).
3. Frames already overwritten are reported as gaps and the recording is
   annotated with `missing N-k` (recording is evidence, not perfect history).
4. This whole operation is pull-based from the Recorder side and bounded: it
   copies at most `pre_frames` frames and never asks the producer to wait.
5. Post-alarm frames continue into the dedicated recording buffer/file; the
   recording owns that memory, not the ring.

Pre-alarm window size is a Recorder configuration, not a transport
configuration. `depth` must still be chosen so the ring covers the worst-case
consumer delay and the pre-alarm history the Recorder can capture before
overwrite — a hardware/requirements measurement (§24). **The final ring depth
is not selected here.**

## 15. Memory Calculation

All values are **estimates pending hardware verification** (ADR-002 and
`v2-frame-data.md` §8.1). Working assumptions:

- Thermal (IR_Data): 640 × 480 × 2 B = **614,400 B/frame** (V2-consistent).
- Visible (VL_Data): RGB8 640 × 480 × 3 B = **921,600 B/frame** (upper bound;
  YUYV would be 614,400 B/frame).
- Combined payload: **1,536,000 B/frame ≈ 1.46 MiB**.
- Descriptor + header + slack: assume **4 KiB/slot** (fixed region; slack
  absorbs future fields without relayout).
- Slot size = header + descriptor + thermal + visible ≈ 1,540,096 B.

### 15.1 Formulas

```
THERMAL_BYTES   = thermal_w * thermal_h * thermal_bytes_per_pixel
VISIBLE_BYTES   = visible_w * visible_h * visible_bytes_per_pixel
FRAME_BYTES     = THERMAL_BYTES + VISIBLE_BYTES
DESC_BYTES      = 4096                     # fixed descriptor+header+slack
SLOT_BYTES      = DESC_BYTES + FRAME_BYTES
CAMERA_BYTES    = SLOT_BYTES * depth
TOTAL_BYTES     = CAMERA_BYTES * num_cameras
```

Using the working estimates (thermal 614,400 B, visible 921,600 B):

| depth | slot bytes | 1 camera (B) | 1 camera (MiB) | 8 cameras (B) | 8 cameras (MiB) |
| --- | --- | --- | --- | --- | --- |
| 8 | 1,540,096 | 12,320,768 | 11.75 | 98,566,144 | 94.0 |
| 16 | 1,540,096 | 24,641,536 | 23.5 | 197,132,288 | 188.0 |
| 32 | 1,540,096 | 49,283,072 | 47.0 | 394,264,576 | 376.0 |
| 64 | 1,540,096 | 98,566,144 | 94.0 | 788,529,152 | 752.0 |

Split (per camera, per depth):

| depth | thermal (B) | visible (B) | descriptor+header (B) |
| --- | --- | --- | --- |
| 8 | 4,915,200 | 7,372,800 | 32,768 |
| 16 | 9,830,400 | 14,745,600 | 65,536 |
| 32 | 19,660,800 | 29,491,200 | 131,072 |
| 64 | 39,321,600 | 58,982,400 | 262,144 |

### 15.2 Notes

- Descriptor overhead is <0.3% of slot size — negligible in bytes but
  significant for correctness (it is what makes frames self-describing).
- 8 cameras at depth 32 (~376 MiB) is plausible on a modern workstation but
  must be confirmed against the real memory budget and the required pre-alarm
  capture window before choosing `depth`.
- **Ring depth is NOT chosen to hold the full pre-alarm window.** The
  requirement-derived pre-alarm history (e.g., 90 frames at 9 FPS × 10 s) lives
  in recording-owned memory, not the ring (§14).
- **Ring depth is not finalized here** (§18, §24).
- If visible is YUYV (614,400 B), all visible columns shrink by a third; the
  fixed layout still works because actual byte counts live in the descriptor.

## 16. Copy Analysis

### 16.1 Current path (in-process, `v3-acquisition.md` §11)

```
HALCON internal buffer
  → himage_as_numpy_array           (may be a view into HALCON)
  → driver .copy()                  Copy #1  (owned immutable array)
  → FramePayload (writeable=False)
  → publisher.publish(frame)
  → [future] ring slot write        Copy #2
```

### 16.2 Future path

```
HALCON
  → NumPy
  → shared-memory slot              (Copy #2 today)
  → consumers read via views         (no copy)
```

So today the shared-memory transport adds exactly one copy (the slot write).
Consumers then read zero-copy views. This is acceptable and correct.

### 16.3 Can Copy #2 be eliminated later?

Only if HALCON can deliver pixel bytes directly into the ring slot's preallocated
memory. Two sub-questions:

1. Does `himage_as_numpy_array` expose a writable buffer or a
   user-supplied-output variant? V2 evidence (`v2-frame-data.md` §2.2) says it
   "always copies" at the top level and "returns direct" in services — i.e., the
   returned buffer's ownership/aliasing with HALCON's internal buffer is
   **unverified and inconsistent between V2 paths**. No claim of zero-copy can
   be made today.
2. HALCON provides image creation from user buffers via operators like
   `gen_image1` (external memory, not owned by HALCON) and there are
   preallocation paths; whether the GigE acquisition can be directed into that
   external memory is **unverified** and must be tested on hardware.

**Design requirement (so zero-copy is possible later without touching
consumers):** the slot write is isolated behind a single `_write_payload(slot,
thermal, visible)` internal step inside the producer. Replacing that step with
"grab directly into slot memory" changes nothing for consumers — they already
read fixed-offset views. V3 therefore keeps the producer-internal boundary, but
makes no zero-copy claim until hardware verification (§24).

### 16.4 Summary of today's copies

- HALCON→NumPy conversion: 0 or 1 copies (HALCON-internal; measured later).
- Driver-owned copy (Copy #1): **required today** for immutability
  (current `driver.py` behavior; V2 services path was a correctness bug).
- Slot write (Copy #2): 1 frame-sized memcpy per publish — ~1.5 MB × 9 FPS ≈
  **~14 MB/s per camera**, ~110 MB/s for 8 cameras. Negligible on any
  workstation; zero-copy is a later optimization, not a requirement.
- Consumer reads: 0 copies (views).

## 17. CPU / GPU Implications

Possible paths:

```
A: shm → CPU NumPy view → CPU processing → GPU (only final results/overlays)
B: shm → GPU upload (copy) → GPU processing
C: HALCON → CPU shared memory (slot) → GPU upload (copy)
```

Where copies occur:

- A: zero extra frame copies on CPU; GPU receives only small derived results.
- B: one full-frame copy per upload (CPU→GPU). At 640×480×2 thermal, that is
  614 KB/frame/consumer. Fine for a few consumers, wasteful if every consumer
  uploads every frame.
- C: same as B; the CPU slot is the natural staging point.

The transport must not prevent efficient GPU processing:

1. **Payload regions are byte-contiguous with known offsets.** GPU upload
  (`cudaMemcpyAsync` / CuPy / torch) can take the shared slot's byte range
  directly as the source pointer — no intermediate CPU re-pack needed.
2. **The descriptor stays CPU-side.** GPUs never need the descriptor; only the
  payload. This keeps GPU staging simple.
3. **Alignment:** payload offsets are 64-byte aligned (slot layout) so upload
  and later zero-copy NVLink/pinned staging are not blocked by misalignment.
4. Do not upload through Python `FramePayload` objects in the hot loop; expose
  `(ptr, size, shape, dtype)` from the view for GPU interop.

Recommendation: keep CPU-first. At 9 FPS and 640×480, GPU is not required for
correctness; a later ADR decides GPU transport once CPU profiling exists. The
shared-memory layout above is GPU-compatible by construction, so no redesign is
needed if a GPU path is chosen.

## 18. Windows / Python Considerations

- **Named shared memory:** use `multiprocessing.shared_memory.SharedMemory`
  (Python 3.8+, named segments) with names derived from the stable `camera_id`:
  `TMS_{camera_id}_frames` (e.g. `TMS_cam_HB25080011_frames`). The name is
  serial-based, never IP-based (§13).
- **Single-process today:** the ring can map the same named segment; consumers
  inside the process attach by name or by reference. The design does not require
  multiprocessing now, but naming + a portable control-block layout mean it can
  be enabled later without relayout.
- **Synchronization across processes (Phase 3, later):** the packed-integer
  header + generation protocol is intended to be portable, but **no
  cross-process safety claim is made here**. Phase 3 designs and tests the
  actual Windows synchronization mechanism (Windows named mutex/events, or a
  bounded spin on generation inside the control block; the Python API masks the
  choice) before any process-safe guarantee is stated (§7.3.1, §10).
- **Cleanup / stale segments:** named `SharedMemory` segments persist until the
  last handle closes. To avoid manual deletion after crashes:
  - **Windows cleanup model:** `SharedMemory.unlink()` has **no effect** on
    Windows. Windows releases the shared memory when **all handles to the
    segment are closed** (the OS tears it down on last-handle close). The ring's
    deterministic `close()` therefore closes every handle the ring holds; the OS
    reclaims the segment. Cleanup is "close all handles → Windows releases the
    memory", not `unlink()`.
  - Attach is **existence-checked**: before opening `TMS_<id>_frames`, the owner
    validates the header magic/version; a stale segment from a dead process is
    **detected and recreated** by the new owner, never reused blindly.
  - **Stale-owner detection is an application-level ownership mechanism, not
    shared-memory cleanup.** A heartbeat `pid`/`last_updated` field in the
    control block lets a new owner detect a dead producer and take ownership.
    Old segments are therefore recycled automatically; no user manual deletion
    is required (§19).
- **Windows specifics:** no POSIX shm assumptions; no anonymous mmap
  inheritance assumptions; all cleanup follows the "close all handles →
  Windows releases the memory" model; stale-owner detection is application-level
  (above). Segment sizes are rounded up by the OS; sizing math uses the logical
  sizes.

## 19. Crash Recovery

Scenarios and handling:

| Crash | Effect | Recovery |
| --- | --- | --- |
| Producer (acquisition) crashes | Ring frozen with last frames PUBLISHED | New worker attach detects stale `pid`, takes ownership, resets slots, resumes; consumers re-anchor via `latest()`/`next(head)`. |
| Consumer crashes | Its in-process position state is gone | New consumer starts at `latest()` (Observer/Diagnostics) or at `producer_head` with gap accounting (Processing/Recorder). No shared state to corrupt. |
| Application crashes | All processes die | Named segments remain but are detected stale by header magic + `pid`/heartbeat on next start; recycled automatically. |
| Camera worker crash only | That camera's ring owned by dead thread | Same as producer crash; other cameras' rings untouched (isolation, §13). |
| Consumer restart | Fresh position | Starts at head; previous losses are counted as gaps. |
| Stale shared memory | Segment from previous run | Magic/version mismatch OR stale heartbeat → recreate. Never block startup on manual cleanup. |

Rules:

- The ring control block carries `magic`, `layout_version`, `owner_pid`,
  `created_monotonic` so a new owner can classify a segment as fresh, stale, or
  corrupt.
- Slot state `WRITING` at recovery time is always reset to `EMPTY` by the new
  owner — no partially written frame survives a crash.
- Consumers attach by name and validate magic/version before mapping; mismatch
  → error surfaced to the consumer, not a crash.

## 20. Eight-Camera Scaling

- **One ring buffer per camera** (not one global ring). Rationale: bounded
  failure domain (camera 1's producer fault cannot corrupt camera 2's transport),
  independent depths/geometry per camera, independent shutdown, and no global
  lock or shared head.
- **Naming:** `TMS_{camera_id}_frames`, deterministic from the serial-based
  `camera_id` (V2: `cam_{serial}`). Never IP-derived (`model.py` `CameraIdentity`
  is already serial-based). Same buffer name on every host in a deployment →
  reproducible diagnostics.
- **Resource math:** 8 rings = 8 independent workers, 8 producer threads, 8
  segments. Total memory scales linearly with depth (§15 table). No superlinear
  costs.
- **Cross-camera features (later):** alarm correlation across cameras or
  synchronized recording reads from multiple rings by sequence; the per-ring
  design does not preclude it — a coordinator consumer attaches to N rings.
- Reconnect of one camera resets only its own ring state; its consumers
  re-anchor locally. Sequence monotonicity per camera is preserved across
  reconnects by the worker (`acquisition.py` `_sequence` is never reset).

## 21. Offline Compatibility

Live and offline must produce the same logical `Frame` (ADR-002). The transport
is only the delivery mechanism:

```
Live:    Shared memory slot → FrameView (read-only views, ADR-002 Frame)
Offline: Raw recording file → FrameView (read-only views, ADR-002 Frame)
Processing sees only FrameView — source is invisible.
```

- `FrameView` (the consumer-side result of both) is the seam. Live decoding
  reads the slot descriptor/payload; offline decoding reads the recording
  container. Both construct the same `FrameDescriptor`/`FramePayload` contract.
- The ring buffer's descriptor fields (§5.2) are exactly the fields the
  recorder must persist and the offline reader must reconstruct — the offline
  container format will reuse the descriptor layout so round-tripping is
  lossless.
- No processing algorithm is duplicated; ADR-002 already mandates the shared
  pipeline. ADR-003 only requires that the slot descriptor be a superset of
  what offline reconstruction needs.

## 22. Proposed API

Minimal, ownership-explicit. No managers/factories/providers/services wrappers.

```
SharedMemoryRingBuffer        # owns one named segment; one producer
    create(camera_id, thermal_spec, visible_spec, depth)
    attach(camera_id)                       # validate magic/version
    producer() -> Producer
    consumer(name) -> Consumer
    close()                                 # close all handles; OS releases on
                                            # last-handle close (Windows); not reusable
    stats() -> RingBufferStats

Producer                        # obtained from ring.producer()
    reserve() -> SlotWriter
    publish(writer, frame) -> PublishResult   # implements FramePublisher.publish
    close()

Consumer                        # per-consumer position state
    latest() -> FrameView | None
    next(expected_sequence) -> FrameView | None
    pin(slot) -> PinnedView                    # bounded reader count (Processing, Recorder)
    release(view)
    stats() -> ConsumerStats
    close()

SlotWriter                      # produced by reserve()
    descriptor_region / thermal_region / visible_region
    commit() -> PublishResult

FrameView                       # TRANSIENT view + decoded ADR-002 descriptor
    descriptor: FrameDescriptor
    thermal: np.ndarray | None     # read-only view
    visible: np.ndarray | None     # read-only view
    generation: int
    valid(): bool                   # generation/sequence check passed
    copy() -> Frame                 # consumer-owned deep copy
    #
    # LIFETIME CONTRACT: FrameView is NOT a durable frame.
    # It is a transient shared-memory view, valid only until the producer
    # reuses its slot. Unpinned views are best-effort; a consumer that must
    # not read torn data pins for the duration of access, or copies into
    # consumer-owned memory before processing (§8.2, §8.3).
```

Mapping to the existing boundary:

- The ring buffer's `Producer.publish(writer, frame)` implements the existing
  `FramePublisher` protocol (`acquisition.py:47`) so `AcquisitionWorker` and its
  tests require no change.
- `PublishResult` (`model.py:113`) is returned unchanged (now with
  `overwritten_sequence` populated).
- Ownership rule: `RingBuffer` owns memory; `Producer` owns the write path;
  `Consumer` owns only its position; `FrameView` is a **transient** view valid
  only until the slot is reused (generation check on access). A consumer that
  needs to retain data must `copy()` it or hold an explicit `pin()`. `FrameView`
  is never handed to another consumer thread for deferred processing unless
  copied.

## 23. Open Questions

1. Should `depth` be fixed per deployment or per-camera configurable? (Per-camera
   config is preferred so a Recorder-heavy camera can be sized independently.)
2. Is the pre-alarm window product-required (frames) — and what is the worst-case
   consumer delay that `depth` must cover?
3. RESOLVED (§8.2/§8.3): Processing requires guaranteed no-tear reads and pins
   (or copies) for the duration of its analysis; Observer and Diagnostics are
   unpinned best-effort.
4. Should Diagnostics read slot payloads at all, or only headers/counters?
5. Should `latest()` be allowed to skip frames without counting them as losses
   for gap metrics (already proposed), or should Observer's skips be counted?
6. What is the acceptable producer-side cost of a `WRITING` skip when all slots
   are pinned? `accepted=False` drop vs. a short spin?
7. Cross-process deployment: is a future watchdog/host process planned, or is
   the crash-recovery of §19 sufficient?
8. Do we need per-consumer blocking notifications (event) in the multi-process
   case, or is polling acceptable? (Deferred to that ADR.)

## 24. Hardware Verification Requirements

Must be measured on real TV46L hardware before the ring depth and slot geometry
are finalized (mirrors `v2-frame-data.md` §9 and `v3-acquisition.md` §12):

1. Exact thermal geometry / `pixel_format` / `bits_per_channel` for `IR_Data`
   (confirm 640×480/16-bit → 614,400 B).
2. Visible stream format: RGB8 vs packed YUV422, resolution, bytes/frame.
   → freezes `VISIBLE_BYTES`.
3. Visible FPS and whether IR+visible can be acquired simultaneously or only
   time-sliced (affects slot write rate and whether both regions are ever
   simultaneously valid).
4. Hardware timestamps availability (GigE/PTP) — descriptor `hardware_timestamp`.
5. Packet-loss behavior / GigE counters; socket size and `num_buffers` for 8
   concurrent cameras without loss.
6. `himage_as_numpy_array` ownership/aliasing — whether a zero-copy
   direct-into-slot write is possible (defines §16.3).
7. Actual copy cost (HALCON→NumPy, NumPy→slot) at full rate and 8 cameras;
   measured CPU/memory/bandwidth budget.
8. NUC blackout duration and whether frames stop during NUC → required
   `depth` to preserve pre-alarm history across a NUC.
9. Frame-rate feature limits (is 9 FPS optimal; visible native rate).
10. Reconnection behavior (error codes, recovery reliability, device string vs
    IP reuse) — affects producer restart and ring takeover (§19).
11. Startup one-time NUC requirement (affects first-frame timing).
12. Actual maximum sustainable throughput with 4-8 cameras to validate the
    §15 memory table and the §16 copy budget.

---

## Blockers

- **Ring depth cannot be chosen** until hardware verifies per-frame sizes
  (§24 #1–#2), visible acquisition mode (#3), and the required pre-alarm window
  (§23 #2). Working estimate depth-32/8-cam ≈ 376 MiB is provisional only.
- **Zero-copy acquisition is unproven.** V2 evidence is contradictory about
  `himage_as_numpy_array` ownership; do not claim zero-copy until tested
  (§16.3, §24 #6).
- **Visible stream format and rate are unknown** (RGB8 vs YUYV; simultaneous vs
  time-sliced). This blocks finalizing `VISIBLE_BYTES` and per-frame rates
  (§24 #2–#3).
- **Existing `FramePublisher.latest()` convenience** (`acquisition.py:69`)
  returns a Python `Frame`. The shared-memory implementation will not; the
  protocol note already flags this, but `InProcessLatestPublisher.latest()`
  callers must migrate to `Consumer.latest()` returning `FrameView` when the
  transport lands.

## Important Design Decisions

1. **Per-camera ring buffers**, named `TMS_{camera_id}_frames`, serial-derived
   identity, one producer each — no global ring (§13, §20).
2. **Fixed-size slots** with fixed payload regions and 64-byte alignment —
   predictable layout, bounded memory, GPU-compatible (§6, §17).
3. **Compact shared descriptor** (versioned binary), decoded into ADR-002
   `FrameDescriptor` per consumer — enables cross-process and zero-copy later
   without changing the frame contract (§5.3).
4. **No global read pointer**; per-consumer in-process position state
   (`last_sequence`, `generation`, counters) (§9).
5. **Generation + sequence validation for stale/reuse detection** — it does
   NOT make unpinned reads safe; consumers that must not tear (Processing,
   Recorder) pin or copy, Observer/Diagnostics are best-effort, and the
   producer never waits — `accepted=False` when no slot is reusable (§8.2,
   §8.3, §11).
6. **Synchronization (Phase 1):** single-writer + per-ring lock for slot state
   flips + packed-header generation protocol; GIL not relied upon; Phase 3
   multi-process Windows synchronization is designed and tested later, not
   claimed now (§7.3, §10).
7. **Two consumption strategies** — `latest()` (snapshot, skip-free) and
   `next(expected_sequence)` (sequential, gap-accounting) (§12, §13).
8. **Pins required for no-tear consumers** — Processing and Recorder pin or
   copy; Observer/Diagnostics are unpinned best-effort; common path is
   zero-blocking (§8.3).
9. **Producer-internal slot write is the zero-copy seam**; consumers always read
   views, so zero-copy can be introduced later without consumer changes (§16.3).
10. **Ring as bounded rolling history, not the recording system**; Recorder
    pulls + pins + copies pre-alarm frames into recording-owned memory, never
    blocking acquisition; final depth not selected here (§14).
11. **Crash recovery by header validation + ownership takeover** — no manual
    shared-memory deletion after crashes (§19).
12. **`FramePublisher`/`PublishResult` boundaries reused unchanged** so
    `AcquisitionWorker` and its 32 tests do not change (§22).

## Hardware Questions

1. Exact thermal geometry, `pixel_format`, `bits_per_channel` (confirm 614,400 B)?
2. Visible format (RGB8 vs YUYV) and bytes/frame — which is real?
3. Can IR + visible be acquired simultaneously (dual handle) or only time-sliced?
   If time-sliced, what are the actual source-switch latencies?
4. True native visible FPS?
5. Does the camera expose a usable GigE/PTP hardware timestamp?
6. What GigE counters exist and what socket size / `num_buffers` prevents loss
   with 8 concurrent cameras?
7. Does `himage_as_numpy_array` alias HALCON's internal buffer, and can HALCON
   acquire directly into externally allocated memory (zero-copy)?
8. NUC duration and frame-stop behavior during NUC?
9. Frame-rate feature min/max/increment; is 9 FPS optimal?
10. Reconnection: which error surfaces, does close+open reliably recover, is
    the device string reusable across IP changes?
11. Actual copy cost (`himage_as_numpy_array` + `.copy()`) at full rate?
12. Real per-frame sizes and CPU/memory/bandwidth at 8 cameras to validate the
    memory table?

## Implementation Plan

Phase 0 — hardware measurement (blocking):
1. Stand on hardware: measure §24 items #1–#5, #8, #11 (frame sizes, formats,
   sync mode, timestamps, NUC). Freeze `THERMAL_BYTES`, `VISIBLE_BYTES`,
   per-frame rates.
2. Investigate `himage_as_numpy_array` aliasing / direct-into-slot writes
   (§16.3). Record the result; design the zero-copy seam regardless.

Phase 1 — in-process ring (no multiprocessing):
1. Implement `core/shm/` module: `SharedMemoryRingBuffer`, `Producer`,
   `Consumer`, `SlotWriter`, `FrameView` per §22 (new domain: the transport
   belongs under `core` per ADR-001, not `camera`).
2. Slot layout: fixed-size, versioned descriptor, aligned payload regions.
3. Producer implementing the existing `FramePublisher` protocol; swap
   `InProcessLatestPublisher` for the ring in a camera worker; keep all 32
   tests green.
4. `latest()` and `next(expected_sequence)` with generation validation.
5. Per-consumer counters (overwritten / gap / stale / invalid) + `ConsumerStats`.
6. Pin support (Processing no-tear reads, Recorder copies), pre-alarm window
   pull test with a fake producer.
7. Ring buffer tests: overwrite detection, gap detection, slow-consumer
   non-blocking, NUC-style producer stalls, shutdown, close-all-handles release.

Phase 2 — hardening:
1. Crash simulation: kill producer/consumer mid-run; verify takeover and
   stale-segment recycling (§19).
2. Multi-consumer throughput: 8 cameras × depth per §15; validate copy budget
   (§16.4) and 64-byte alignment.
3. Descriptor versioning test (reject mismatched layout).
4. GPU staging smoke test (upload slot bytes to GPU, no transport change) — only
   if a GPU decision is made.

Phase 3 — optional multi-process:
1. Enable attach-by-name from a second Python process; add Windows named
   event/mutex for notification if polling is insufficient (§23 #8).
2. Stale-owner takeover across processes (§19).

Phase 4 — offline integration:
1. Persist slot descriptor fields in the recording container (format ADR).
2. Offline `FrameView` reader producing ADR-002 `Frame`; verify the shared
   pipeline path against recorded slots.

No ring buffer implementation is performed as part of this ADR.