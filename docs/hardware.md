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
| Servo A signal | `D9` | **Phase 7.** Not driven by any current sketch |
| Servo B signal | `D10` | **Phase 7.** Not driven by any current sketch |

### Power

- Servos are powered from an **external AKSHA 5 V / 3 A supply**.
- The Arduino ground and the servo-supply ground are **deliberately common**.
- The external **+5 V rail is NOT connected to the Arduino +5 V pin**.

Do not change this. An earlier setup attempt caused a high-current short that
melted jumper wires; the arrangement above is the corrected, working one.

The Phase 5 sketch does not reference the servo pins at all. Keeping actuation
out of the sketch that runs during weighing means a bug in the weight path
cannot move anything physical.

## Firmware

`hardware/arduino/aurum_weight/aurum_weight.ino` — **weight only**.

No external HX711 library: the part is two pins and a shift register, and the
bit-banged read is shorter than the dependency would be.

### Serial protocol

One line per sample, at 10 Hz:

```
W,<version>,<board_millis>,<raw_counts>,<status>
```

for example `W,1,10432,-261605,OK`.

| Field | Meaning |
|---|---|
| `W` | Weight frame. The only frame type in this phase. |
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

The first reading is never accepted: a cell settles, a belt vibrates, and a
hand leaving the pan takes a moment, so whichever number arrives first is the
least trustworthy in the series. Samples pass through a **median** filter
(mean would smear a spike in), then a stability window.

**Zero grams is a valid measurement.** An empty pan after tare weighs nothing,
and that is a real reading — never confused with an absent one.

## What has actually been validated

| Item | Status |
|---|---|
| HX711 responds to load | **Physically tested** — 180 g moved the reading by ~65 000 counts |
| Servo A movement | **Physically tested**, independently, outside this software |
| Servo B movement | **Physically tested**, independently, outside this software |
| Corrected power wiring | **Physically working** after the earlier short |
| Aurum weight sketch on the board | **NOT tested** — written this phase, never uploaded |
| Python ↔ Arduino serial link | **NOT tested** — no board attached to a machine running Aurum |
| Calibration workflow end to end | **NOT run** on hardware |
| Conveyor motion, belt speed, distances | **NOT measured** |

Everything in the "NOT" rows is software-tested against a scripted reader and
nothing more.
