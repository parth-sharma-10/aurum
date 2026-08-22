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
movement routine — which is why `conveyor.arduino.ack_timeout_ms` is 2 s
against a 700 ms hold. It is still not proof a servo physically moved: a
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
within `--tolerance` (default **0.1 g**, an engineering approximation, not
research-derived).

### Current state: NOT CALIBRATED

`configs/calibration.yaml` ships `UNMEASURED`.

A bench experiment on 2026-08-22 with a 180 g reference mass produced roughly
**361.9 counts/g** (empty ≈ −261 600 counts, loaded ≈ −196 470). That is
evidence the hardware responds correctly and is recorded in the file's notes.
**It is not a calibration:** it was not produced by this workflow, and it was
never checked against a second known mass. The software therefore continues to
report `UNMEASURED`, and no reading can reach `MEASURED` until the workflow is
run on the machine.

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

| Item | Status |
|---|---|
| HX711 hardware response | **VERIFIED** — 180 g moved the reading by ~65 000 counts |
| Initial calibration experiment | **VERIFIED** — factor ≈ 361.9 counts/g derived |
| **Final calibration validation** | **NOT VERIFIED** — no independent second-mass check |
| Servo A movement (D9) | **BENCH VERIFIED**, independently, outside this software |
| Servo B movement (D10) | **BENCH VERIFIED**, independently, outside this software |
| Corrected power wiring | **WORKING** after the earlier short |
| Weight sketch on the board | **NOT tested** — never uploaded |
| Sorter sketch on the board | **NOT tested** — written, never uploaded |
| **Python ↔ Arduino communication** | **NOT VERIFIED** — never run against a board |
| Servo moved by Aurum code | **NEVER** |
| Calibration workflow end to end | **NOT run** on hardware |
| Physical conveyor | **DOES NOT EXIST** |

Software status, which is a different claim:

| Item | Status |
|---|---|
| Camera → detection → tracking → item id | **RUNNING** — exercised over HTTP on the test images |
| Item id → mass → PMDI → decision → command | **RUNNING** — `app/pipeline/session.py`, covered end to end |
| Command layer, link layer, session | **TESTED** — `tests/test_arduino.py`, `test_link.py`, `test_session.py` |
| Serial link against a real board | **UNTESTED** — a fake serial stands in |

### The five levels, kept apart

`SOFTWARE-TESTED` · `SIMULATION-VERIFIED` · `HARDWARE-BENCH-VERIFIED` ·
`PHYSICALLY-CALIBRATED` · `PHYSICAL-CONVEYOR-VALIDATED`

Aurum reaches level 3 for the servos and the HX711 — bench-verified outside
this software — and **level 4 and 5 for nothing at all.** Level 4 needs the
calibration workflow run on the machine; level 5 needs a conveyor that does not
exist and is not in the demonstration's scope.

Everything in the "NOT" rows is software-tested against a fake serial port and
nothing more. A passing test suite says the software is right about what it
would send, never that a paddle moved.

### The first bench session, in order

1. Flash `aurum_weight`. Confirm `W,1,…,OK` lines in the Serial Monitor at 115200.
2. Run `python -m app.calibrate` to `verified: true`.
3. Flash `aurum_sorter`. Confirm weight frames still stream.
4. `POST /session/board/connect`, then check `GET /arduino` reports `CONNECTED`.
5. Present a CPU, weigh it, and watch **Servo A**.
6. Present a PCB, weigh it, and watch **Servo B**.
7. Present a RAM module and confirm **nothing moves**.

Only after 5, 6 and 7 have been watched may the rows above change.


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
