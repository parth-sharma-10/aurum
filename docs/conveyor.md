# The conveyor: modes, speed, geometry and the firing time

**There is no physical belt.** This machine is a camera, a load cell, an
Arduino and two paddles, and the operator carries the object between stages.
`conveyor.mode` ships `NONE`, which is a fact about the hardware rather than a
setting nobody got round to.

What follows is the software that models a belt, so that the timing chain can
be demonstrated, tested and — when a belt exists — driven. Every figure it
produces on the demonstration profile is a **TEST value** and says so on every
reading.

---

## The four modes

| `conveyor.mode` | Belt speed comes from | Status on every reading |
|---|---|---|
| `NONE` *(default)* | there is no belt | `UNAVAILABLE` |
| `SIMULATION` | `conveyor.simulation.belt_speed_cm_s` | `SIMULATED` |
| `ENCODER` | roller pulses, differentiated over real time | `MEASURED` |
| `MANUAL` | `conveyor.manual.belt_speed_cm_s`, measured once by hand | `MANUAL` |

```bash
export AURUM_CONVEYOR_MODE=SIMULATION      # the demonstration belt
export AURUM_SIM_BELT_SPEED_CM_S=10        # 0.10 m/s. A TEST value.
```

The status is the point. A dashboard may print `0.10 m/s (SIMULATED)`; it may
never print `0.10 m/s`. `MANUAL` is a real measurement that is *not live* — it
cannot notice the belt slowing under load — and that is a third thing again.

### Why `NONE` is the default

Turning a belt on with no belt attached makes the machine wait six seconds and
fire a paddle at nothing. Same posture as `conveyor.arduino.enabled` and
`demo.mock_mass.enabled`: the honest setting ships, and the demonstration
setting is one environment variable away.

---

## The demonstration geometry

TEST values, in `configs/conveyor.yaml` under `conveyor.simulation`. **Nothing
here was measured.** They are reachable only when `conveyor.runtime.simulation`
is true *or* `conveyor.mode` is `SIMULATION`, and every route computed from
them is stamped `mode: SIMULATED`.

| Distance | Value |
|---|---|
| camera → load cell | 25 cm |
| camera → servo A | 60 cm |
| camera → servo B | 90 cm |
| servo actuation delay | 150 ms |

Distances are measured from the camera's field-of-view centre, along the belt,
in the direction of travel.

With `conveyor.mode: NONE` the real `conveyor.geometry` block applies, and it
is `UNMEASURED` — so the scheduler refuses and every item reaches C. That is
the fail-closed design, not a bug.

---

## The timing model

Unchanged from Phase 6. The conveyor supplies a speed to it; it does not
replace it.

```
travel_s   = distance_cm / belt_speed_cm_s
execute_at = detected_at
           + travel_s                       when the item arrives
           - servo_actuation_delay_ms/1000  send early: the paddle takes time
           + timing_offset_ms/1000          the calibration knob
```

Negative `timing_offset_ms` fires earlier, positive fires later. The actuation
delay is *subtracted*, because to have the paddle in the stream at arrival the
command has to leave before arrival.

### The ETA is dynamic

```
distance_to_actuator = actuator_position - object_position
ETA                  = distance_to_actuator / current_belt_speed
```

`RoutingScheduler` re-reads the speed from the conveyor on **every** schedule.
Slowing the belt moves the next item's firing time without restarting
anything. A speed captured once into a frozen `Geometry` would keep firing to
yesterday's belt, which looks exactly like a timing bug and is not one.

Nothing sleeps. `session.drain_routes()` fires whatever has arrived, and it is
called from the same loop that watches the pan — 50 ms, well inside the
±200 ms the model is accurate to.

**Timing error.** Python, serial latency and inference jitter come to roughly
±200 ms, which is ±2 cm at 10 cm/s and ±10 cm at 50 cm/s. Run a demonstration
belt slow and make the bin mouths wide.

---

## The encoder

```
distance_per_pulse = roller_circumference_cm / pulses_per_revolution
speed              = pulses_since_last_sample × distance_per_pulse / elapsed_s
```

No encoder model is assumed, because nobody has bought one. Both figures are
configuration:

```yaml
conveyor:
  encoder:
    pulses_per_revolution: 20
    roller_circumference_cm: UNMEASURED
    sample_interval_s: 0.25
    timeout_s: 2.0
```

Four behaviours worth knowing:

- **Two samples are needed.** One pulse count is a position, not a velocity.
  Until the second arrives the reading is `UNAVAILABLE`, which is what it is.
- **An `UNMEASURED` circumference makes every reading `UNAVAILABLE`.** A
  missing tape measurement is not papered over with a plausible default.
- **A counter that goes backwards is discarded.** It wrapped or was reset;
  reading it as reverse travel would schedule a firing time in the past.
- **A silent encoder goes `STALE`, not "still moving at the last speed".** Past
  `timeout_s` the scheduler refuses and the item reaches C. A belt nobody can
  hear is not a belt whose speed is known, and firing on its last reading is
  how a paddle strikes the next item along.

---

## What the API and the dashboard show

`GET /conveyor` and the `conveyor` block of `GET /session`:

```json
{"present": false, "mode": "NONE",
 "speed": {"cm_s": null, "m_s": null, "status": "UNAVAILABLE",
           "reason": "conveyor.mode is NONE: this machine has no belt..."},
 "eta_to_servo_a_s": null,
 "geometry": {"belt_speed_basis": "UNAVAILABLE via none: ..."}}
```

`GET /routing` adds the queue: what is scheduled, what is due, what was
refused and why.

---

## When a real belt arrives

1. Measure the belt speed. Time a marked item over a known distance, five
   times, take the median. Set `conveyor.belt.speed_cm_s`, or fit an encoder
   and set `conveyor.encoder.roller_circumference_cm`.
2. Measure the three distances along the belt from the camera's FOV centre.
3. Time the paddle from command sent to in-stream; set
   `conveyor.timing.servo_actuation_delay_ms`.
4. Set `conveyor.mode` to `ENCODER` or `MANUAL`. Leave
   `conveyor.runtime.simulation` false so the TEST distances stay unreachable.
5. Tune `conveyor.timing.offset_ms` against where items actually land. No model
   of a belt predicts its own latency correctly.

Until every one of those is a measured number, the scheduler refuses and items
reach C. That is deliberate: the machine will not pretend to know where an
item is on a belt nobody has measured.
