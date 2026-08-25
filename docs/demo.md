# Aurum — SIH demonstration runbook

The demonstration proves the **intelligence, measurement and actuation** chain
on real hardware:

```
real component -> camera -> class + AUR-ITEM id
                                |
              [ operator carries it to the pan ]
                                |
                  HX711 -> measured mass
                                |
        cited composition -> PMDI -> A / B / C
                                |
              A -> Servo A    B -> Servo B    C -> nothing
```

**There is no conveyor, and nothing in this demonstration pretends there is
one.** The operator moves the component between the camera, the load cell and
the bins. That is the honest scope: hopper, singulator, belt and physical
positioning are the next hardware stage.

What the operator does: carry the object from the camera to the pan, and take it
away again afterwards.
What the operator never does: press anything to make the machine work, or type a
class, a mass, or a bin. There is no code path that accepts any of the three,
and since the load cell drives the cycle there is no button in the normal path
either. *Measure & route now* still exists under **Developer controls** for a
bench with no working cell; it is labelled as a fallback because that is what it
is.

---

## What to claim, and what not to

> "Aurum demonstrates an intelligent e-waste identification, material
> estimation and automated routing prototype. The current prototype validates
> the perception, measurement, material intelligence and actuator-control
> pipeline. Mechanical conveying and singulation are planned for the next
> hardware integration stage."

Do **not** say "fully automated conveyor sorting system". There is no conveyor.

Do not say the system measures gold. It identifies a component and multiplies
a **cited contained composition** by a measured mass. That is an estimate from
published assays, and the dashboard labels it as one.

---

## Before the demonstration

### 0. Close the Arduino IDE Serial Monitor

Do this first, and keep it closed. The IDE reopens it automatically whenever
the board enumerates and then holds the port exclusively — it broke two uploads
and looked exactly like the board dropping off USB.

```bash
lsof /dev/cu.usbmodem101      # anything listed owns the port, not you
pkill -f serial-monitor       # release it
```

### 1. Flash the board

Two sketches exist, and the difference matters.

| Sketch | Use it for | Can it move a servo? |
|---|---|---|
| `hardware/arduino/aurum_weight/` | **calibration** | No — it has no servo code at all |
| `hardware/arduino/aurum_sorter/` | **the demonstration** | Yes, Servo A and Servo B |

Calibrate on the weight-only sketch. A bug in the weight path then cannot
swing a paddle while your hand is on the pan.

Both run at **115200 baud**, matching `configs/conveyor.yaml`.

```bash
arduino-cli compile --fqbn arduino:avr:uno hardware/arduino/aurum_sorter
arduino-cli upload -p /dev/cu.usbmodem101 --fqbn arduino:avr:uno hardware/arduino/aurum_sorter
```

`arduino-cli lib install Servo` once, if the sorter sketch will not compile.

### 2. Wiring check

| Signal | Pin |
|---|---|
| HX711 `DOUT` / `SCK` | `D2` / `D3` |
| HX711 `VCC` / `GND` | `5V` / `GND` |
| Servo A signal | `D9` |
| Servo B signal | `D10` |

Servos run from the external **AKSHA 5 V / 3 A** supply. Its ground is common
with the Arduino; its **+5 V rail is not connected to the Arduino 5 V pin**.
Do not change this — the other arrangement melted jumper wires once already.

### 3. Calibrate the load cell — two masses, not one

```bash
cd aurum && source .venv/bin/activate
pip install -r requirements.txt          # pyserial is required now
ls /dev/tty.usbmodem*                    # macOS; COM3 on Windows, /dev/ttyACM0 on Linux

python -m app.calibrate --port /dev/tty.usbmodem1101 \
    --reference-mass 180 --verify-mass 100
```

It tares an empty pan, records the counts for 180 g, derives counts/g, then
asks for a **different** known mass and checks the prediction.

Confirm the result:

```bash
grep verified configs/calibration.yaml     # must read: verified: true
```

**Until this says `true`, no PCB can reach Bin B.** An unverified factor
produces a `STABLE` reading, and a concentration estimate refuses it — by
design. The bench experiment that suggested ~361.9 counts/g is evidence the
hardware responds, not a calibration.

If verification fails, check the tare, the mounting, and that both masses are
what they claim to be before touching `--tolerance`.

### 4. If the load cell will not calibrate — the mock-mass fallback

As of 2026-08-22 the cell is **mechanically bypassed**: 180 g moves it 2.2
counts against a 64-count noise floor, and 400 g moves it backwards. See
`docs/hardware.md` for the measurements and the mounting fix.

Fixing it is a bench job. When the demonstration is sooner than the bench job:

```bash
export AURUM_DEMO_MOCK_MASS=true
```

An item that cannot be weighed is then given a per-class stand-in mass —
**CPU 25 g · PCB 180 g · RAM 30 g · Connector 5 g** — so the rest of the
pipeline can be shown running. Per class, because a precious fraction is metal
over *total* mass: one flat value made a CPU read 26 ppm where 188 is right.

The number is fabricated and the system says so everywhere: the reading is
`SIMULATED` and never `usable`, the PMDI and valuation carry
`overall_status: SIMULATED`, and the console shows a `MOCK MASS` pill plus a
banner listing the assumed masses.

**Say it out loud during the demo.** The screen already admits it, and
volunteering it is far stronger than being caught by a question:

> "The load-cell mount is being rebuilt, so I'm using an assumed mass — you'll
> see it flagged SIMULATED throughout. The class, the cited composition and the
> routing decision are real. The mass is not."

It ships **off**. Without the flag, a PCB routes to Bin C on
`UNKNOWN_WEIGHT`, which is the correct fail-closed behaviour.

---

## Running it

Three terminals.

```bash
# 1 — the backend
cd aurum && source .venv/bin/activate
export AURUM_ARDUINO_PORT=/dev/cu.usbmodem101     # ls /dev/cu.usbmodem* to find yours
export AURUM_ARDUINO_ENABLED=true                 # actuation ships OFF
export AURUM_CAMERA_INDEX=0                       # 0 was the external webcam here
export AURUM_DEMO_MOCK_MASS=true                  # ONLY while the load cell is broken
uvicorn app.api:app --port 8000

# 2 — the dashboard
cd aurum/frontend && npm install && npm run dev    # http://localhost:5173

# 3 — spare, for curl if the browser misbehaves
```

Then, in the dashboard:

| # | Action | What to expect |
|---|---|---|
| 1 | **Start camera** | Live feed appears; pills show `CAMERA LIVE` |
| 2 | **Connect board** | `BOARD LINKED` and `ACTUATION ON` green. `NOT CALIBRATED` stays amber while the cell is broken |
| 3 | Hold the component to the camera | Box appears labelled `AUR-ITEM-xxxxxxxx CPU 0.94` |
| 4 | Wait for CONFIRMED | Three observations; "current item" fills in |
| 5 | Place it on the load cell | `Object detected on the pan` → `Measuring…` |
| 6 | **Do nothing** | `842.3 g stable`; the same item id gains a mass, a PMDI, a bin |
| 7 | Watch the paddle | Servo A or Servo B strokes; Bin C moves nothing |
| 8 | Take the object off | `Remove the object` → `Waiting for an object` |

`CAMERA LIVE`, `BOARD LINKED` and `ACTUATION ON` must be green before step 6. A
red or amber pill is the system telling you something is genuinely not ready —
it is not cosmetic. `NOT CALIBRATED` and `MOCK MASS` are expected while the load
cell is bypassed, and are exactly what you should be narrating.

Stages after the camera read "not weighed yet" / "waiting" until the object is
on the pan. That is pending, not failed. The banner at the top of the dashboard
is the machine's own account of where it is: if it says `Waiting for an object`
while something is sitting on the cell, read the reason underneath — an
uncalibrated cell and an unidentified mass both say so in plain words.

---

## What each component does, and why

Rehearse with all three so nothing is a surprise on camera.

| Component | Bin | Why | Needs a verified calibration? |
|---|---|---|---|
| **CPU** | **A** → Servo A | Configured premium class; cited gold per package (`CPU-AU-001`) is per-piece, so it needs no mass | No |
| **PCB** | **B** → Servo B | 2 200 ppm precious from cited composition, above the 100 ppm recoverable threshold | **Yes** — or a mock mass |
| **RAM** | **C** → nothing | No cited composition exists for a whole RAM module. Routing it anywhere would be a guess | No |
| **Connector** | **A** → Servo A | Configured premium class, cited gold per piece | No |

**RAM going to C is the best thing in the demonstration.** It is not a
failure — it is the system refusing to invent data it does not have, and
saying exactly why: `UNKNOWN_MATERIAL`. Point at it deliberately. Note
that the mock-mass fallback does not rescue it: a stand-in mass changes the
arithmetic, never the evidence.

**RAM is also the hardest class to get on camera.** The model scores 0.51 recall
on it — the worst of the four — so a module flickers in and out where a CPU
holds steady at 0.95. Present it **flat-on and still**, on a plain background,
and give it a couple of seconds to reach CONFIRMED. A confirmed item now
survives a brief flicker, so it stays measurable once it has been seen enough
times, but a module held edge-on may never be detected at all.

Bin A for CPU is an **engineering sorting policy** (`configs/grading.yaml`),
not a claim that a CPU holds more precious metal than a PCB. On the cited
evidence a PCB actually outranks it. Say so if asked — it is in the config
file's own comments.

---

## Talking through it (3–4 minutes)

**0:00 — the problem.** E-waste changes hands by weight, and what is inside a
board is invisible at the point of collection.

**0:20 — identity.** Hold up the CPU. Point at `AUR-ITEM-` on the video. *"That
identity is minted once. The mass, the estimate and the servo command all key
off it, so one physical object cannot become three ledger rows."*

**0:50 — mass.** Place it on the cell. Press Measure. Point at `MEASURED`.
*"That status means a settled reading on a calibration we verified against a
second known mass. Anything less reads STABLE, and the estimator refuses it."*

**1:30 — evidence.** Point at the evidence ids in the metals table. *"Every
figure traces to a published assay. Nothing here is a guessed constant."*

**2:00 — the decision.** Point at the bin and the reason code. *"The backend
decided that. Nobody typed it."*

**2:20 — the servo.** The paddle strokes. *"That is a real command over real
serial to a real Arduino."*

**2:45 — RAM.** Run the RAM module. It goes to C, nothing moves. *"No cited
composition exists for a whole DRAM module, so it refuses rather than guessing.
Bin C needs no actuator — it is what happens when the software does nothing,
which is also what happens if it crashes."*

**3:15 — the honest boundary.** *"No conveyor yet. I carried these between
stages. Conveying and singulation are the next hardware stage — what is proven
today is perception, measurement, material intelligence and actuation."*

---

## When something goes wrong

| Symptom | Cause | Do this |
|---|---|---|
| `CAMERA OFF`, permission error | macOS camera permission | System Settings → Privacy & Security → Camera → your terminal. **Grant it before the venue.** |
| `NO BOARD` | Wrong port, or cable | `ls /dev/tty.usbmodem*`, re-export `AURUM_ARDUINO_PORT`, press Connect board again |
| `NOT CALIBRATED` | `verified: false` | Re-run `python -m app.calibrate`. Do not hand-edit the YAML |
| `ACTUATION OFF` | Safety default | `export AURUM_ARDUINO_ENABLED=true` and restart the backend |
| Mass reads `UNSTABLE` | Bench vibration, or a hand still on the pan | Let it settle and press Measure again |
| Mass reads `UNAVAILABLE` | Cell not responding | Check D2/D3, re-seat the HX711, confirm the sorter sketch is flashed |
| `ALREADY_PROCESSED` | That item was already routed | Take the component out of frame, let the track drop, present it again |
| Servo does not move but state is `ACKED` | Mechanical or supply-side | Check the external 5 V rail and the horn. An ACK is not proof of movement |
| Everything is unplugged | — | Every item routes to C and nothing moves. The demo degrades to software, and the dashboard says why on every card |

**Record a backup video the night before.** It is the difference between a
hiccup and a dead demonstration.

For a software-only fallback with no board at all, the chain still runs and
labels every refusal:

```bash
uvicorn app.api:app --port 8000       # no AURUM_ARDUINO_PORT set
```

Mass reads `UNAVAILABLE`, PCB routes to C, and the actuator card says *"No link
to the board, so no command was sent."* Say that out loud rather than letting
anyone believe a servo moved.

---

## Questions you should expect

**"Can it tell how much gold is in that board?"**
No. It identifies the component and multiplies a published composition by a
measured mass. That is an estimate of *contained* metal, labelled as one — not
a recovery yield and not an assay.

**"Why did the RAM get rejected?"**
No source giving whole-module composition could be read in full, so the
database has a stated gap rather than an invented number. The system refuses
rather than guessing.

**"Is the conveyor part built?"**
No. I moved those by hand. Perception, measurement, material intelligence and
actuation are what this prototype validates.

**"What's your accuracy?"**
Only the figures in `docs/model-card.md`, measured on a held-out test split:
0.806 mAP@50 overall. RAM recall is 0.51 — quote that too rather than the
headline alone.

**"Does the ACK mean the servo moved?"**
No. It means the board completed its stroke routine and said so. A stripped
horn acknowledges identically. That is why we watch the paddle.
