# Hardware

What is physically built, what the software talks to, and what has actually
been validated. The last distinction matters most: **software-tested is not
physically validated**, and this document keeps them apart.

## Wiring as built

Bench-assembled and tested on 2026-08-22.

| Signal | Arduino pin | Notes |
|---|---|---|
| HX711 `DOUT` | `D2` | data |
| HX711 `SCK` | `D3` | clock |
| HX711 `VCC` / `GND` | `5V` / `GND` | powered from the Arduino |
| Servo A signal | `D9` | Physically present, bench-tested |
| Servo B signal | `D10` | Physically present, bench-tested |

**Serial: 115200 baud.**

### Servo parameters — bench values, not final geometry

| Servo | Pin | REST | PUSH | Hold |
|---|---|---|---|---|
| A | `D9` | 0 deg | 90 deg | 700 ms |
| B | `D10` | 0 deg | 90 deg | 700 ms |

These came from independent bench testing. They are **configurable engineering
parameters**, not validated mechanical geometry — no paddle has ever deflected
an item, because there is no belt for an item to travel on.

### Baud rate — reconciled

Everything now runs at **115200**: `configs/conveyor.yaml`, both sketches, and
`app.calibrate` (which reads `conveyor.arduino.baudrate`). The earlier 9600
discrepancy is resolved.

If you re-flash the older weight-only sketch from an unmerged checkout, check
its `Serial.begin()` first — a mismatched rate produces garbage, not silence,
which is the more confusing failure.

### Power

- Servos are powered from an **external AKSHA 5 V / 3 A supply**.
- The Arduino ground and the servo-supply ground are **deliberately common**.
- The external **+5 V rail is NOT connected to the Arduino +5 V pin**.

Do not change this. An earlier setup attempt caused a high-current short that
melted jumper wires; the arrangement above is the corrected, working one.

The Phase 5 sketch does not reference the servo pins at all. Keeping actuation
out of the sketch that runs during weighing means a bug in the weight path
cannot move anything physical — **use it for calibration.**

**NO PHYSICAL CONVEYOR CURRENTLY EXISTS.** There is no belt, no motor and no
frame. The SIH demonstration does not use one: the operator carries the
component from the camera to the load cell to the bins, and routing is
immediate rather than scheduled. `app/routing/` keeps the scheduled-route model
for the day a belt exists, and the demonstration session does not call it.

## Firmware

Two sketches, and the difference is a safety property rather than a
convenience.

| Sketch | Purpose | Can it move a servo? |
|---|---|---|
| `hardware/arduino/aurum_weight/` | **calibration** | No — it contains no servo code at all |
| `hardware/arduino/aurum_sorter/` | **the demonstration** | Yes: Servo A on `D9`, Servo B on `D10` |

**Calibrate on the weight-only sketch.** A bug in the weight path then cannot
swing a paddle while a hand is on the pan. That is the entire reason the older
sketch is kept rather than deleted.

Both run at **115200**, and neither uses an external HX711 library: the part is
two pins and a shift register, and the bit-banged read is shorter than the
dependency would be.

### The sorter sketch

One board, one port, two protocols sharing it. Servos are **not attached at
boot** — an unattached pin emits no pulses, so nothing can twitch while the
board resets or while the host is still deciding whether to actuate. A stroke
attaches, moves, holds, returns and detaches, so no holding current is drawn
between items.

`AURUM/1 CFG <rest> <push> <hold_ms>` sets the throw at runtime, so tuning the
paddle angles needs no reflash. The host sends it automatically on connect from
`conveyor.servo.*`.

The board keeps the last 8 command ids it acted on and answers a repeat with
`ACK <id> DUP` **without moving again** — an acknowledgement lost on the wire
must not let a resend swing the paddle into whatever is now in front of it.

`MOVE C` is refused with `BAD_TARGET`. There is no Servo C, and inventing one
in firmware would be the single most misleading thing this repository could do.

**The ACK follows the stroke, not the frame.** It means the board completed its
movement routine — which is why `conveyor.arduino.ack_timeout_ms` is 4 s
against a 700 ms hold: a MOVE was measured at 1.212 s on the attached board,
and 2 s produced false timeouts on a board that was answering correctly. It is
still not proof a servo physically moved: a
stripped horn or a dead supply rail acknowledges identically.

### Serial protocol

One line per sample, at 10 Hz:

```
W,<version>,<board_millis>,<raw_counts>,<status>
```

for example `W,1,10432,-261605,OK`.

The sorter sketch adds the command protocol on the same link:

```
host  ->  AURUM/1 MOVE <A|B> <item_id> <command_id>
host  ->  AURUM/1 PING <command_id>
host  ->  AURUM/1 CFG <rest_deg> <push_deg> <hold_ms>
board ->  AURUM/1 ACK <command_id> [DUP]
board ->  AURUM/1 ERR <command_id> <code>
board ->  AURUM/1 PONG <command_id>
```

`app/hardware/link.py` owns the one port and files each incoming line by type,
so a weight frame is never read as an acknowledgement and an acknowledgement is
never read as a mass.

| Field | Meaning |
|---|---|
| `W` | Weight frame. Shares the link with the `AURUM/1` command protocol. |
| `version` | Protocol version. Currently `1`. A different version is dropped. |
| `board_millis` | `millis()` on the Arduino, for ordering and drift checks. |
| `raw_counts` | **Raw 24-bit HX711 counts. Never grams.** |
| `status` | `OK`, or `ERR` when the cell did not become ready. |

Python drops any line that is not well-formed and `OK`. A bare number is
rejected too: it could be counts or grams, and guessing which is exactly the
assumption this project refuses to make. A failed read emits `ERR` with a zero
count rather than repeating the last good value, so a stuck cell reads as
absent and never as a mass.

**Raw counts, because calibration belongs in Python.** A calibration factor is
measured, auditable data. In `configs/calibration.yaml` it sits in version
control next to the workflow that produced it and the second known mass that
verified it. Compiled into firmware it is a number nobody can check.

## Calibration

```
empty pan -> tare -> known mass -> factor -> SECOND known mass -> verified
```

```
python -m app.calibrate --port COM3 --reference-mass 180 --verify-mass 100
```

**Two masses, not one.** A single reference mass proves the cell responds and
lets you compute a factor, but it cannot tell you whether the factor is right —
the mass you derived it from will always read back correctly. A second,
different mass is the only thing that catches a wrong factor, a non-linear
cell, or a tare taken with something still on the pan. The workflow refuses if
both masses are the same.

`verified` becomes true only when the prediction for the second mass lands
within `--tolerance`, default **1.5 g**. That default is measured on this rig,
not assumed — see *Why 1.5 g* below.

### The reading must be of the present, not of the backlog

The board streams a frame every 100 ms whether or not anyone is reading, and the
OS keeps the whole backlog. Each step of this workflow waits on a human placing
a mass, so by the time `_average` reads, `readline()` replays the stream **from
the beginning**, instantly, in order.

That produced three calibrations in a row that derived ~0.08, ~1.07 and
~1.57 counts/g from a cell that actually responds at ~392: every step averaged
the pan as it was before the mass landed, so all three bursts returned the same
empty-pan value.

`HX711SerialReader.drain()` fixes it, and flushing the buffer alone does not —
the kernel refills it from the backlog immediately. What separates history from
now is arrival time: a queued frame returns in 0 ms, a live one makes the reader
wait for the board to send it. `drain()` reads forward until a frame has to be
waited for, and `_average` **raises** if a burst completes faster than the board
could physically have sent it.

### Why 1.5 g

Measured 2026-08-26 by placing and re-placing the same masses. Placement moves
the reading far more than the electronics do:

| load | n | sample sd | max−min |
|---|---|---|---|
| 204 g | 2 | 0.431 g | 0.610 g |
| 170 g | 3 | 0.245 g | 0.488 g |
| 374 g (stacked) | 2 | — | 0.119 g |
| zero, warmed | 4 | — | 3.3 counts = 0.008 g |

The check derives a factor from **one** reference burst and tests it against
**one** verification burst, so both placements contribute:

```
factor uncertainty  0.431 g at 204 g = 0.211%, at 170 g -> 0.359 g
verification burst placement scatter              -> 0.245 g
combined in quadrature                            -> 0.435 g  (1 sigma)
```

3σ is 1.30 g, rounded to 1.5 g so a sound calibration does not fail on placement
luck. It stays far tighter than anything it exists to catch — a tare taken under
a 5 g object, or a factor wrong by more than 0.9%.

Nothing downstream needs better. At 170 g this is 0.88% relative; a CPU would
need an **88% mass error** to cross the 100 ppm Bin B threshold, and the tightest
plausibility window (Connector, 0.5 g minimum) is governed by zero stability at
0.008 g, not by the slope.

### Linearity: the response passes through the tare

The 204 g and 170 g bursts first disagreed by 0.53% on counts/g, which two
models fit equally: a mislabelled mass, or a constant offset on loading. Two
points cannot separate two-parameter models, so a third load was made by
stacking both masses — 374 g, no new hardware.

The fitted offset is **+80 ± 223 counts (~0.203 g), consistent with zero**. The
response is linear through the tare, the existing single-point model is sound,
and **two-point calibration is not needed**. The residual 0.53% is in the masses:
the nominal 170 g mass weighs approximately **170.70 g** if the 204 g mass is
taken as exact. Both are uncertified, so only their ratio is established —
absolute accuracy awaits a traceable mass.

### Current state: CALIBRATED AND VERIFIED, 2026-08-26

```yaml
counts_per_gram:      392.2166666666667
tare_counts:          -263078.25
reference_mass_g:     204.0
verified:             true
verification_mass_g:  170.0
verification_error_g: +1.1297752092806093   # tolerance 1.5 g
```

`has_factor: true`, `verified: true`, `present: true`. Readings can now reach
`MEASURED`, which is what lets a concentration-based metal estimate run and what
opens the pan machine's arrival gate.

**Read the margin honestly.** The +1.130 g error sits 0.37 g inside the 1.5 g
tolerance, and most of it is not cell error: the nominal 170 g mass weighs
approximately 170.7–171.1 g by the ratio measurement above. The cell's own
contribution is the ~0.435 g placement scatter.

**Both masses are uncertified**, so this establishes the *ratio* and the
linearity, not absolute accuracy. Every mass this rig reports inherits whatever
the 204 g reference actually weighs. That is fine for sorting — a CPU would need
an 88% mass error to change bins — and is not fine for anything claiming to be a
scale. A traceable mass would settle it.

A failed run still writes its record — deliberately, so a factor that was tried
and missed is not lost. Such a record has `has_factor: true` and
`present: false`, and cannot drive the machine.

## Weight states

| Status | Meaning | Usable for a metal estimate? |
|---|---|---|
| `RAW` | One unfiltered sample. | No |
| `UNSTABLE` | Still moving beyond `stability_tolerance_g`. | No |
| `STABLE` | Settled, but on an **unverified** calibration. | No |
| `SIMULATED` | From the labelled simulation. | No |
| `MEASURED` | Settled, on a **verified** calibration, real hardware. | **Yes** |
| `UNAVAILABLE` | Uncalibrated, timed out, disconnected, or bad data. | No |

`app/materials.py` accepts only `MEASURED` for concentration-based estimates.
A PCB weighed on an unverified calibration therefore produces no metal figure —
it routes to Bin C instead, which is the correct fail-closed behaviour.

### Three questions a calibration record answers

`Calibration` keeps them apart, because they are not the same question and
collapsing them either deletes the `STABLE` tier or lets a failed run drive the
machine:

| property | means | gates |
|---|---|---|
| `has_factor` | a factor and a tare exist, so counts convert | the arithmetic: `grams()`, `WeightSensor.read()`, pan arrival detection |
| `verified` | the second-mass check passed | `MEASURED` vs `STABLE` in `_settled()` |
| `present` | `has_factor` **and** `verified` | anything treating the cell as a trusted calibrated instrument |

A factor is derived *from* the reference mass, so that mass always reads back
correctly and an unverified factor can be arbitrarily wrong for every other
load. One failed run left 0.078 counts/g on disk, which reads an empty pan as
−2033 g; `present` is what stops such a record being mistaken for a calibration.

The first reading is never accepted: a cell settles, a bench vibrates, and a
hand leaving the pan takes a moment, so whichever number arrives first is the
least trustworthy in the series. Samples pass through a **median** filter
(mean would smear a spike in), then a stability window.

A reading settles when it has stayed inside `stability_tolerance_g` for a full
`stability_window_ms`, tracked as "how long since it last moved". An earlier
version compared the clock against the oldest sample still inside the window,
which quietly required a sample to land exactly on the boundary: a 450 ms
window fed by a 10 Hz cell then never settled at all, however still the mass
was, and the shipped 500 ms default only worked because 100 ms divides into it.
Tuning the window is now safe.

**Zero grams is a valid measurement.** An empty pan after tare weighs nothing,
and that is a real reading — never confused with an absent one.

## What has actually been validated

Software being complete is not the same as hardware being proven, and this
table tracks the second. **Update it only after watching the thing happen.**

Bench session of **2026-08-22**, on an Arduino Uno at `/dev/cu.usbmodem101`.

| Item | Status |
|---|---|
| Board identity | **VERIFIED** — Uno, VID `0x2341` / PID `0x0043` |
| Weight sketch on the board | **VERIFIED** — flashed; 10/10 valid `W,1,…,OK` frames at 115200 |
| Sorter sketch on the board | **VERIFIED** — flashed; weight frames survive it |
| HX711 converting | **VERIFIED** — `board_millis` advancing, 64–157 count noise floor |
| **Python ↔ Arduino communication** | **VERIFIED** — `PING`→`PONG` over the real port |
| **Servo A moved by Aurum code** | **PHYSICALLY VERIFIED** — `ACK` in 709 ms, movement watched |
| **Servo B moved by Aurum code** | **PHYSICALLY VERIFIED** — `ACK`, movement watched |
| Duplicate suppression on the board | **VERIFIED** — replayed id answered `ACK … DUP`, no second stroke |
| Bin C writes nothing | **VERIFIED** — no bytes reached the board |
| Corrected power wiring | **WORKING** after the earlier short |
| **Load cell under load** | **VERIFIED 2026-08-26** — responds at ~392 counts/g, linear through the tare |
| **Calibration** | **VERIFIED 2026-08-26** — 392.2167 counts/g, second-mass error +1.130 g within 1.5 g |
| Physical conveyor | **DOES NOT EXIST** |

The 709 ms matters: the sketch blocks for `holdMs` during a stroke, so the
round-trip time is evidence the movement routine actually ran rather than an
ACK being echoed back.

### The load cell was mechanically bypassed, and no longer is

**2026-08-22 — no signal.** With the cell wired and converting normally:

| Load | Mean counts | Shift from tare | Expected shift |
|---|---|---|---|
| empty (tare) | −261 278.8 | — | — |
| **180 g** | −261 276.6 | **+2.2** | ~**+65 100** |
| **400 g** | −261 289.3 | **−10.5** | ~**+144 800** |

A 2.2-count shift against a 64–104 count noise floor, and 400 g moving it the
**opposite** way. Not a weak signal — no signal. A bar cell must be a
cantilever: one end bolted rigidly to a base, the other carrying the pan, with
an air gap beneath the free end so it can bend.

**2026-08-26 — the mounting was corrected and the cell responds.**

| Load | Mean counts | Shift from tare | counts/g |
|---|---|---|---|
| empty (tare, warmed) | −262 856.8 | — | — |
| **204 g** | −183 117.8 | **+79 739** | 390.9 |
| **170 g** | −196 051.0 | **+66 806** | 393.0 |
| **374 g** (stacked) | — | — | — |

Zero stability once warmed is 3.3 counts (0.008 g) across four bursts. The first
burst after the port opens sits ~60 counts low — the cell needs a moment, and
pairing a load against that warm-up zero skews the factor, so discard it.

`demo.mock_mass` remains available for a bench with no working cell, and is
labelled SIMULATED throughout — see the README. It is no longer the only option
here.

Software status, which is a different claim:

| Item | Status |
|---|---|
| Camera → detection → tracking → item id | **RUNNING** — real webcam, real model |
| Item id → mass → PMDI → decision → command | **RUNNING** — `app/pipeline/session.py` |
| Command layer, link layer, session | **TESTED** — 836 tests, hardware layer covered |
| Serial link against a real board | **VERIFIED** — no longer a fake |

### The five levels, kept apart

`SOFTWARE-TESTED` · `SIMULATION-VERIFIED` · `HARDWARE-BENCH-VERIFIED` ·
`PHYSICALLY-CALIBRATED` · `PHYSICAL-CONVEYOR-VALIDATED`

The actuation path has **not** reached level 3. A paddle has been commanded by
Aurum's own code over a real serial link and the board acknowledged, which is
level 3's first half. Its second half — a human watching the paddle move — has
never been done: there is no camera on the bench, and no observation is on
record. Level 3 is claimed the moment
`python -m scripts.bench_check --port <port> --move A --move B` is run and
answered, and not before.

**Level 4 is reached, with one qualification.** The cell is mounted, responds
linearly through the tare, and carries a calibration verified against a second
known mass by the project's own workflow. The qualification is traceability:
both masses are uncertified, so the factor is verified *self-consistently* and
inherits whatever the 204 g reference actually weighs. Level 5 needs a conveyor
that does not exist and is out of the demonstration's scope.

A passing test suite says the software is right about what it would send, never
that a paddle moved. Those are now separate claims with separate evidence, and
the tables above keep them apart.

### Flashing, without the IDE

`arduino-cli` avoids the Serial Monitor problem below:

```bash
arduino-cli compile --fqbn arduino:avr:uno hardware/arduino/aurum_sorter
arduino-cli upload -p /dev/cu.usbmodem101 --fqbn arduino:avr:uno hardware/arduino/aurum_sorter
```

`arduino-cli lib install Servo` once, if the sorter sketch will not compile.

### The Arduino IDE will fight you for the port

**Close the Serial Monitor before doing anything else.** The IDE reopens it
automatically the instant the board enumerates, and it holds the port
exclusively. That caused two upload failures and one apparent "board vanished
from USB" during the first bench session, and it will silently break
calibration the same way.

```bash
lsof /dev/cu.usbmodem101      # anything listed here owns the port, not you
pkill -f serial-monitor       # releases it; the IDE reopens it on demand
```

### Bench session order

1. Flash `aurum_weight`. Confirm `W,1,…,OK` at 115200. **Calibrate on this
   sketch** — it has no servo code, so nothing can move near your hands.
2. Run `python -m app.calibrate` to `verified: true`.
3. Flash `aurum_sorter`. Confirm weight frames still stream.
4. `POST /session/board/connect`, then check `GET /arduino`.
5. CPU → watch **Servo A**. PCB → watch **Servo B**. RAM → watch **nothing**.

Steps 1, 3, 4 and 5 are done. Step 2 is blocked on the mounting fault.


## Routing geometry — future work, not the demonstration

**Not needed for the SIH demonstration**, which has no conveyor and routes
immediately. This checklist is what a belt would require, kept because
`app/routing/` already implements the timing model and none of it needs
rewriting when a machine exists.

None of these has been measured. Each goes into `configs/conveyor.yaml`, and
**no Python changes when they do**.

| # | Quantity | Config key | How to measure |
|---|---|---|---|
| 1 | Belt speed | `conveyor.belt.speed_cm_s` | Time a marked item over a known distance. Repeat 5x, take the median. |
| 2 | Camera to load cell | `conveyor.geometry.camera_to_load_cell_cm` | Along the belt, from the camera's field-of-view centre to the pan centre. |
| 3 | Camera to Servo A | `conveyor.geometry.camera_to_servo_a_cm` | Along the belt, FOV centre to the paddle's line of action. |
| 4 | Camera to Servo B | `conveyor.geometry.camera_to_servo_b_cm` | Same, for the second paddle. |
| 5 | Servo actuation delay | `conveyor.timing.servo_actuation_delay_ms` | Command sent to paddle physically in the stream. Film at high frame rate, or measure with a switch. |
| 6 | Timing offset | `conveyor.timing.offset_ms` | Calibrate last, on the running machine: watch where items land and trim. Negative fires earlier. |

Until 1, 3 and 5 exist, an A route is refused with a reason code naming the
missing quantity. Until 1, 4 and 5 exist, so is a B route. Bin C works
regardless, because it needs no actuator.

### Sequence

Measure 1–5 with the belt running and the servos idle. Set them, confirm the
`/routing` endpoint reports `routable: true`, then tune 6 with real items.

## Hardware status

See **What has actually been validated** above — deliberately one table rather
than two, because two copies of a status list drift apart and the optimistic
one always gets quoted.

The demonstration runs on the simulated conveyor profile. That is a model of a
machine, not a machine.

---

## The latched hardware fault

Once something physical goes wrong, **nothing moves until a human resets it**.

| Latches | Does not latch |
|---|---|
| the frame could not be written | bin C — no frame is sent, and that is normal |
| no ACK inside `ack_timeout_ms` | actuation disabled — the shipped state |
| the board answered `ERR` | a decision the engine could not make |
| the link was gone when a command was due | a price that was unavailable |
| servo angles outside 0–180°, or equal | |
| a route reached the boundary with no paddle | |

**Latched, not transient.** A link that recovers between two items does not
clear it. A command that went unacknowledged may have moved a paddle, may have
left it half out, may have jammed it against something — the next command
would be issued into a machine whose physical state nobody knows. Reconnecting
is not somebody having looked at the rig.

```
GET  /hardware              mode, link, fault, servo geometry
POST /hardware/fault/reset  clear it — deliberate, and recorded
```

The dashboard puts an active fault at the top of the page with the reset
button, because it is the reason nothing is moving and nothing else on screen
explains that.

The first fault is kept as `current` even when later ones are recorded: the
first is the one that explains everything after it, and overwriting it loses
the cause in favour of a consequence. `history` and `resets` survive the reset,
so "why did nothing move for six items" has an answer afterwards.

## HARDWARE_MODE=SIMULATION sends nothing to a port

With `conveyor.runtime.simulation` true, `ArduinoController` builds a
`SimulatedTransport` **regardless of `conveyor.arduino.port`**. The full
protocol runs and the board acknowledges, so the whole chain can be
demonstrated — and no byte reaches a real board however the rest is
configured. `GET /arduino` reports `transport: simulated` and
`hardware_mode: SIMULATION`, so a simulated ACK on screen is never mistaken
for a physical one.
