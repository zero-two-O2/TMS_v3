# V2 Alarm System — Technical Recovery Report

## Purpose

Analyze the V2 alarm implementation across `reference/TMS_v2/alarm/`, `reference/TMS_v2/observation/`, `reference/TMS_v2/tests/`, and `halcon_roi_validation.py` to determine exact state machine, transitions, threshold logic, and proven behavior for V3 design.

**Focus:** `halcon_roi_validation.py` (hardware-validated alarm path) and `alarm/` package (core engine).

---

## 1. Alarm State Machine

### 1.1 States (`alarm/state_machine.py:AlarmState`)
```python
class AlarmState(Enum):
    NORMAL = auto()       # No alarm condition
    PENDING = auto()      # Triggered but delay not elapsed
    ACTIVE = auto()       # Alarm confirmed, not acknowledged
    ACKNOWLEDGED = auto() # User acknowledged, still above threshold
    CLEARED = auto()      # Condition cleared, awaiting return to NORMAL
```

### 1.2 Valid Transitions (`_TRANSITIONS`)
| From | To (Allowed) |
|------|--------------|
| NORMAL | PENDING |
| PENDING | NORMAL, ACTIVE |
| ACTIVE | ACKNOWLEDGED, CLEARED |
| ACKNOWLEDGED | CLEARED |
| CLEARED | NORMAL |

**Invalid transitions silently ignored** (return `None` for transition).

### 1.3 State Machine Logic (`AlarmStateMachine.evaluate()`)
```python
def evaluate(is_triggered, should_clear, frame_timestamp_ms):
    if state == NORMAL:
        if is_triggered:
            if delay_ms > 0: state = PENDING; pending_start = timestamp
            else: state = ACTIVE
    
    elif state == PENDING:
        if not is_triggered: state = NORMAL; pending_start = None
        elif elapsed_ms >= delay_ms: state = ACTIVE; pending_start = None
    
    elif state == ACTIVE:
        if should_clear: state = CLEARED
    
    elif state == ACKNOWLEDGED:
        if should_clear: state = CLEARED
    
    elif state == CLEARED:
        state = NORMAL  # Auto-return to normal
    
    return new_state, transition (old_state if changed else None)
```

**Key behaviors:**
- **Delay timer:** Frame-timestamp based (ms); `PENDING` → `ACTIVE` only after `delay_ms` elapsed while continuously triggered
- **Auto-clear:** `CLEARED` → `NORMAL` automatic on next evaluation
- **Acknowledge:** Only from `ACTIVE` → `ACKNOWLEDGED`; does not clear alarm
- **Force clear:** `force_clear()` → `CLEARED` from any state
- **Reset:** `reset()` → `NORMAL` from any state

---

## 2. Alarm Conditions & Threshold Logic

### 2.1 Condition Types (`alarm/conditions.py`)
| Condition | Trigger | Clear | Measured Value |
|-----------|---------|-------|----------------|
| **HIGH** | `measured > threshold` | `measured ≤ threshold - hysteresis` | ROI **maximum** |
| **LOW** | `measured < threshold` | `measured ≥ threshold + hysteresis` | ROI **minimum** |
| **RANGE** | `\|measured - threshold\| > hysteresis` | `\|measured - threshold\| ≤ hysteresis × 0.5` | ROI **mean** |

### 2.2 Hysteresis Behavior
- **HIGH:** Must drop **below** `threshold - hysteresis` to clear
- **LOW:** Must rise **above** `threshold + hysteresis` to clear
- **RANGE:** Must enter **half-hysteresis band** (`±hysteresis/2`) to clear
- **Zero hysteresis:** Immediate clear at threshold boundary (tested)

### 2.3 NaN Handling
```python
if math.isnan(measured_value) or math.isnan(threshold):
    return False  # Never trigger or clear on NaN
```
- NaN statistics → alarm holds current state (does not clear)

---

## 3. Alarm Evaluation Pipeline

### 3.1 Core Evaluator (`alarm/evaluator.py:AlarmEvaluator`)
```python
def evaluate(roi_id, settings, stats, state_machine, frame_timestamp_ms):
    if not settings.enabled:
        return AlarmResult(active=False, state=NORMAL)
    
    if not stats.valid:
        return AlarmResult(active=state!=NORMAL, state=state)
    
    measured = _select_measured_value(settings.condition, stats)
    if math.isnan(measured):
        return AlarmResult(active=state!=NORMAL, state=state)
    
    evaluator = get_evaluator(settings.condition.name)
    is_triggered = evaluator.is_triggered(measured, settings.value, settings.hysteresis)
    should_clear = evaluator.should_clear(measured, settings.value, settings.hysteresis)
    
    new_state, transition = state_machine.evaluate(is_triggered, should_clear, frame_timestamp_ms)
    active = new_state in (PENDING, ACTIVE, ACKNOWLEDGED)
    
    return AlarmResult(roi_id, active, new_state, measured, settings.value)
```

### 3.2 Manager (`alarm/manager.py:AlarmManager`)
- **Thread-safe:** All public methods use `threading.Lock`
- **Per-ROI state machines:** `dict[roi_id, AlarmStateMachine]`
- **Per-ROI settings:** `dict[roi_id, ROIAlarmSettings]`
- **Event emission:** Callback on state transitions
- **History:** `AlarmHistory` records all `AlarmEvent` objects

**Key methods:**
| Method | Purpose |
|--------|---------|
| `register(roi_id, settings)` | Create/replace state machine with delay from settings |
| `evaluate(roi_id, stats, timestamp_ms)` | Single ROI evaluation |
| `evaluate_all(all_stats, timestamp_ms)` | Batch evaluation (used by Observation Runtime) |
| `acknowledge(roi_id)` | Manual ack: `ACTIVE` → `ACKNOWLEDGED` |
| `reset(roi_id)` | Force to `NORMAL` |
| `clear_all()` | Drop all machines, settings, history |

---

## 4. Alarm Events & History

### 4.1 Event Kinds (`alarm/events.py:AlarmEventKind`)
| Kind | Emitted On |
|------|------------|
| `ACTIVATED` | `NORMAL→PENDING` (delay>0) or `NORMAL/PENDING→ACTIVE` |
| `CLEARED` | Any state → `CLEARED` |
| `ACKNOWLEDGED` | `ACTIVE` → `ACKNOWLEDGED` |
| `RESET` | Manual `reset()` call |

### 4.2 Event Structure (`AlarmEvent`)
```python
@dataclass(frozen=True)
class AlarmEvent:
    roi_id: str
    kind: AlarmEventKind
    previous_state: str
    new_state: str
    measured_value: float
    threshold: float
    condition: str
    timestamp: datetime = now()
```

### 4.3 History (`alarm/history.py:AlarmHistory`)
- **Global list:** `_events` (all events in order)
- **Per-ROI index:** `_by_roi[roi_id]` → list of events
- **Methods:** `record()`, `all_events`, `events_for_roi()`, `count`, `clear()`

---

## 5. Thread-Safety

| Component | Thread-Safety | Mechanism |
|-----------|---------------|-----------|
| `AlarmStateMachine` | **NOT thread-safe** | Single-threaded use assumed |
| `AlarmEvaluator` | **Thread-safe** | Stateless; no shared data |
| `AlarmManager` | **Thread-safe** | `threading.Lock` on all public methods |
| `AlarmHistory` | **NOT thread-safe** | Caller must serialize (Manager does) |
| `ConditionEvaluator` | **Thread-safe** | Pure functions, no state |

**V2 Usage:** 
- `halcon_roi_validation.py` — single worker thread per camera (thread-safe by design)
- `observation/runtime.py` — GUI thread (single-threaded Qt event loop)

---

## 6. Integration with ROI Measurements

### 6.1 Measured Value Selection
```python
def _select_measured_value(condition, stats):
    if condition == HIGH: return stats.maximum
    if condition == LOW: return stats.minimum
    if condition == RANGE: return stats.mean
    return stats.maximum
```

### 6.2 Statistics Requirements (`roi/runtime.py:RuntimeROIStatistics`)
```python
@dataclass
class RuntimeROIStatistics:
    valid: bool
    minimum: float
    maximum: float
    mean: float
    standard_deviation: float
    hotspot_x: int
    hotspot_y: int
    pixel_count: int
    ...
```

**Alarm only uses:** `valid`, `minimum`, `maximum`, `mean` — hotspot/pixel_count ignored.

### 6.3 Invalid Statistics Handling
```python
if not stats.valid:
    return AlarmResult(active=state!=NORMAL, state=state)
```
- **Invalid ROI stats** → alarm **holds current state** (does not clear)
- Prevents false clears during camera errors/frame drops

---

## 7. Observer Mode Integration (`observation/runtime.py`)

### 7.1 Alarm Manager per Camera
```python
self._alarms: dict[str, AlarmManager] = {}
# Created in add_camera()
alarm_manager = AlarmManager(
    on_event=lambda event, _cid=camera_id: self._on_alarm_event(_cid, event)
)
```

### 7.2 Frame Processing
```python
def process_frame(camera_id, temperature_image, frame_id):
    stats = manager.process_frame(temperature_image, frame_id)
    all_stats = {roi.configuration.roi_id: roi.statistics 
                 for roi in manager.get_active() 
                 if roi.statistics and roi.statistics.valid}
    timestamp_ms = int(time.monotonic() * 1000)
    results = self._alarms[camera_id].evaluate_all(all_stats, timestamp_ms)
    return stats, results
```

**Key points:**
- Only **valid, enabled ROIs** evaluated
- **Monotonic timestamp** (`time.monotonic()`) for delay timing
- **Batch evaluation** via `AlarmManager.evaluate_all()`
- **Event callback** propagates to GUI (`_on_alarm_event`)

### 7.3 Position Switch Handling
```python
def _load_position(camera_id, position_id):
    # ... load new ROI store ...
    stale = registered_rois - new_rois
    for roi_id in stale:
        alarm_manager.unregister(roi_id)  # Clears ACTIVE/PENDING
    for roi_id, config in new_rois.items():
        alarm_manager.register(roi_id, config.alarm)  # Fresh state machines
```
- **Stale ROIs unregistered** → `ACTIVE`/`PENDING` alarms cleared
- **New ROIs registered** → fresh `AlarmStateMachine` at `NORMAL`

---

## 8. Hardware-Validated Path (`halcon_roi_validation.py`)

### 8.1 Alarm Manager in CameraWorker
```python
# CameraWorker.__init__
self._alarm_manager = AlarmManager(
    limit=self._alarm_limit,
    enabled=self._alarm_enabled,
    use_max_temperature=self._alarm_use_max,
    on_event=self._on_alarm_event
)

# Per-frame in run()
self._alarm_manager.evaluate(statistics)  # statistics = List[ROIStatistics]
```

### 8.2 Alarm Configuration
```python
# ConfigManager keys → AlarmManager
alarm.enabled          → enabled
alarm.temperature_limit → limit
alarm.use_max_temperature → use_max_temperature (HIGH vs MAX)
nuc.auto_enabled       → auto NUC (not alarm)
nuc.interval_seconds   → auto NUC interval
```

### 8.3 Alarm Evaluation (Simplified)
```python
# AlarmManager.evaluate(statistics: List[ROIStatistics])
# Uses ROIStatistics: name, mean, deviation, minimum, maximum, range_val
# Condition: HIGH (maximum) vs LOW (minimum) based on use_max_temperature
```

### 8.4 Alarm Snapshots
```python
def _on_alarm_event(roi_name, active, current_max):
    if not active: return  # Only snapshot on NORMAL→ALARM
    
    # 2-second re-trigger suppression per (camera, position, roi)
    # Frame from ring buffer (temp_numpy + statistics)
    # SnapshotWorker saves PNG with overlays
```

---

## 9. Alarm Configuration (Per-ROI)

### 9.1 Settings Model (`roi/alarm_settings.py`)
```python
@dataclass
class ROIAlarmSettings:
    enabled: bool = True
    condition: ROIAlarmCondition = ROIAlarmCondition.HIGH  # HIGH/LOW/RANGE
    value: float = 80.0          # Threshold
    hysteresis: float = 2.0      # Hysteresis band
    delay_ms: int = 0            # Delay before ACTIVE (ms)
```

### 9.2 Global/Default Config (`configuration/settings.py`)
```python
DEFAULT_HIGH_TEMPERATURE: float = 80.0
DEFAULT_LOW_TEMPERATURE: float = 0.0
# ConfigManager reads from SQL dbo.application_settings:
alarm.enabled → alarm_enabled
alarm.temperature_limit → alarm_temperature_limit
alarm.use_max_temperature → alarm_use_max_temperature
```

---

## 10. Proven by Tests

### 10.1 `tests/test_alarm.py` — Comprehensive Coverage
| Test Class | Coverage |
|------------|----------|
| `TestHighConditionEvaluator` | Trigger/clear boundaries, hysteresis, NaN, measured value selection |
| `TestLowConditionEvaluator` | Mirror of HIGH |
| `TestRangeConditionEvaluator` | Range trigger/clear, half-hysteresis, mean selection |
| `TestGetEvaluator` | Registry, case-insensitive, custom registration |
| `TestAlarmStateMachine` | All transitions: delay, pending, active, ack, clear, reset, force_clear |
| `TestStateTransitionValidation` | Valid/invalid transition matrix |
| `TestAlarmEvent` | Immutability, fields, enum values |
| `TestAlarmEvaluator` | Integration: disabled, invalid stats, HIGH/LOW/RANGE lifecycle, NaN |
| `TestAlarmManager` | Full lifecycle, multi-ROI independence, events, history, hysteresis, delay, ack, reset, clear_all, evaluate_all, register updates, chatter prevention |
| `TestEdgeCases` | Negative thresholds, zero hysteresis, infinity, missing ROI in evaluate_all |

**Total: ~80 test methods** — all passing in V2 CI.

### 10.2 `tests/test_alarm_processor.py` — Legacy Integration
- Tests `AlarmProcessor` (older processing-layer alarm)
- Covers: HIGH/LOW/RANGE, delay timer, alarm events, active count, multiple ROIs, clear, reset
- **Note:** This tests a separate `AlarmProcessor` in `processing/alarm_processor.py` — different from core `alarm/` package

---

## 11. Camera/Processing Failure Behavior

| Failure Mode | Alarm Behavior |
|--------------|----------------|
| **Frame drop** | No evaluation → alarm holds state (no auto-clear) |
| **Invalid ROI stats** | `stats.valid=False` → holds state (tested) |
| **Camera disconnect** | Observation Runtime: `remove_camera()` → `unregister()` all ROIs → alarms cleared |
| **Processing exception** | `halcon_roi_validation.py`: logged, frame skipped, alarm not evaluated that frame |
| **NaN temperature** | `measured=NaN` → `is_triggered=False`, `should_clear=False` → holds state |
| **Position switch** | Stale ROIs unregistered → alarms cleared; new ROIs start at NORMAL |

---

## 12. Exact Class/Function Reference

| Component | File | Key Functions |
|-----------|------|---------------|
| **State Machine** | `alarm/state_machine.py` | `AlarmState`, `AlarmStateMachine.evaluate()`, `acknowledge()`, `force_clear()`, `reset()`, `is_valid_transition()` |
| **Conditions** | `alarm/conditions.py` | `HighConditionEvaluator`, `LowConditionEvaluator`, `RangeConditionEvaluator`, `get_evaluator()`, `register_evaluator()` |
| **Evaluator** | `alarm/evaluator.py` | `AlarmEvaluator.evaluate()`, `_select_measured_value()` |
| **Manager** | `alarm/manager.py` | `AlarmManager.register()`, `evaluate()`, `evaluate_all()`, `acknowledge()`, `reset()`, `clear_all()`, `get_state()`, `active_count`, `active_roi_ids`, `history` |
| **Events** | `alarm/events.py` | `AlarmEventKind`, `AlarmEvent` |
| **History** | `alarm/history.py` | `AlarmHistory.record()`, `all_events`, `events_for_roi()`, `clear()` |
| **Result** | `alarm/result.py` | `AlarmResult` |
| **Interfaces** | `alarm/interfaces.py` | `ConditionEvaluator` (ABC), `AlarmEventHandler` (Protocol) |
| **Observation Integration** | `observation/runtime.py` | `ObservationRuntime.add_camera()`, `process_frame()`, `_load_position()`, `_on_alarm_event()` |
| **Validation Tool** | `halcon_roi_validation.py` | `CameraWorker._alarm_manager`, `_on_alarm_event()`, `_execute_nuc()`, snapshot logic |
| **Settings** | `roi/alarm_settings.py` | `ROIAlarmSettings`, `ROIAlarmCondition` enum |

---

## 13. V3 Alarm Behavior That Should Be Preserved

### Core State Machine (Proven)
1. **5-state machine** (NORMAL→PENDING→ACTIVE→ACKNOWLEDGED→CLEARED→NORMAL)
2. **Frame-timestamp delay** (`delay_ms`) with `PENDING` intermediate state
3. **Hysteresis per condition** (HIGH: threshold-hysteresis; LOW: threshold+hysteresis; RANGE: half-band)
4. **NaN-safe** — never triggers/clears on NaN; holds state on invalid stats
5. **Auto-clear** — `CLEARED` → `NORMAL` automatic
6. **Acknowledgment** — `ACTIVE` → `ACKNOWLEDGED` manual, doesn't clear
7. **Per-ROI independence** — separate state machines, settings, history

### Integration Patterns (Proven)
8. **Batch evaluation** — `evaluate_all(stats_dict, timestamp)` for frame-synchronous processing
9. **Event callback** — `on_event(roi_id, AlarmEvent)` for GUI/history
10. **Position-aware lifecycle** — unregister stale ROIs on position switch
11. **Snapshot on activation** — 2-second re-trigger suppression per (camera, position, ROI)
12. **Thread-safe manager** — `Lock` protects all mutable state

### Configuration (Proven)
13. **Per-ROI settings** — condition, threshold, hysteresis, delay, enabled
14. **Three conditions** — HIGH (max), LOW (min), RANGE (mean)
15. **Global defaults** — with per-camera/per-ROI override via SQL

### What V3 Can Simplify
- **Two AlarmManager implementations** (`alarm/manager.py` vs `processing/alarm_processor.py`) → unify
- **Legacy `RuntimeROIStatistics` coupling** → use generic statistics dict
- **Dual validation mode** → replace with golden-file tests
- **SQL-coupled config loading** → decouple via config provider interface

---

## 14. V3 Requirements Derived from V2

| Requirement | Source |
|-------------|--------|
| Stateless alarm evaluation function | `AlarmEvaluator.evaluate()` is pure given settings + stats |
| Frame-synchronous batch evaluation | `AlarmManager.evaluate_all()` used per frame |
| Per-ROI state machines with generation | `AlarmManager._machines` dict; cleared on position switch |
| Event sourcing for audit trail | `AlarmHistory` records all transitions |
| Hysteresis + delay as first-class config | `ROIAlarmSettings` fields |
| NaN/Invalid statistics handling | Explicit checks in evaluator |
| Position-switch clears stale alarms | `ObservationRuntime._load_position()` |
| Snapshot suppression (2s) | `halcon_roi_validation.py:_on_alarm_event()` |

(End of alarm system report)