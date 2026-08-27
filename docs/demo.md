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
lsof /dev/cu.usbmodem1101     # anything listed owns the port, not you
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
arduino-cli upload -p /dev/cu.usbmodem1101 --fqbn arduino:avr:uno hardware/arduino/aurum_sorter
```

The port number follows the USB location, so it changes when the board moves
socket or hub. `ls /dev/cu.usbmodem*` every session — it was `usbmodem101` on
2026-08-26 and `usbmodem1101` on 2026-08-27, and a stale value fails as
"could not open", which reads exactly like a dead board.

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

The cell is mounted and **carries a verified calibration** as of 2026-08-26:
392.2167 counts/g, tared at −263 078 counts, referenced on 204 g and verified
against a second 170 g mass — predicted 171.130 g, error +1.130 g against a
1.5 g tolerance. That record is `configs/calibration.yaml` and it is measured
data about one physical rig, not a setting.

Re-run it only if the mount is disturbed. The backend must be stopped first —
it holds the serial port, and two readers get nothing.

```bash
cd aurum && source .venv/bin/activate
pip install -r requirements.txt          # pyserial is required now
ls /dev/cu.usbmodem*                     # macOS; COM3 on Windows, /dev/ttyACM0 on Linux

python -m app.calibrate --port /dev/cu.usbmodem1101 \
    --reference-mass 204 --verify-mass 170
```

It tares an empty pan, records the counts for the reference mass, derives
counts/g, then asks for a **different** known mass and checks the prediction.

Confirm the result:

```bash
grep verified configs/calibration.yaml     # must read: verified: true
```

**Until this says `true`, no PCB can reach Bin B.** An unverified factor
produces a `STABLE` reading, and a concentration estimate refuses it — by
design.

If verification fails, check the tare, the mounting, and that both masses are
what they claim to be before touching `--tolerance`.

**Check the empty pan before the demonstration.** It read 7.9–8.3 g with
nothing on it on 2026-08-27, and the arrival threshold is 5 g — so the pan
machine sits in `WAITING_FOR_CLEAR` for ever and **the automatic cycle never
arms**. `curl -s localhost:8000/session/pan` says so in one line. The fix is a
re-tare, which means the calibration run above, which means stopping the
backend. Do it the night before, not at the venue.

### 4. If the load cell will not calibrate — the mock-mass fallback

Not needed on this rig any more; kept for a bench where the cell is dead or
absent. It was the standing arrangement until 2026-08-26, when the mount was
rebuilt — see `docs/hardware.md`.

When the demonstration is sooner than the bench job:

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

## Running it — real components

This is the path to use. Real camera, real board, real load cell, real servos;
nothing simulated but the belt, which does not exist. `configs/bench-profile.sh`
is the checked-in configuration — read it, it explains every line it sets.

Three terminals.

```bash
# 0 — nothing may already own the port or the ports
lsof /dev/cu.usbmodem1101                          # must be empty
lsof -ti tcp:8000 -ti tcp:5173                     # must be empty

# 1 — the backend
cd aurum && source .venv/bin/activate
ls /dev/cu.usbmodem*                               # confirm the port, then edit
                                                   # AURUM_ARDUINO_PORT in the profile
set -a; source configs/bench-profile.sh; set +a
uvicorn app.api:app --port 8000

# 2 — the dashboard
cd aurum/frontend && npm install && npm run dev    # http://localhost:5173

# 3 — spare, for curl if the browser misbehaves
```

The profile resolves to `HARDWARE_MODE=PHYSICAL`, actuation on, mock mass
**off** so the load cell drives the cycle, and the belt left on `SIMULATION`
because the timing model is the only belt there is. It does not set
`AURUM_CAMERA_INDEX`; export it if the default is the wrong webcam.

Confirm the machine agrees before you trust the screen:

```bash
curl -s localhost:8000/ready | python3 -m json.tool
```

`"ready": true` with an empty `blocked_by`, `"hardware_mode": "PHYSICAL"`, and
eight checks green — vision model, camera, no latched fault, board link, servo
angles applied, load cell calibrated, paddle movement verified, actuation
enabled. Camera and board read *not started* until the dashboard opens and runs
its start-up sequence; that is expected, not a fault.

### Fallback — no hardware at all, `configs/demo-profile.sh`

The block above drives a real board. On a laptop with nothing attached, source
the checked-in demonstration profile instead:

```bash
cd aurum && source .venv/bin/activate
set -a; source configs/demo-profile.sh; set +a
uvicorn app.api:app --port 8000
```

That resolves to:

| Setting | Demonstration | The physical machine |
|---|---|---|
| `conveyor.mode` | `SIMULATION` | `ENCODER` |
| belt speed | **0.10 m/s, SIMULATED** | measured by the encoder |
| `HARDWARE_MODE` | `SIMULATION` — no byte reaches a port | `PHYSICAL` |
| mass | per-class stand-in, `SIMULATED` | HX711, `MEASURED` |

**0.10 m/s is a demonstration value, not a measurement.** Say so. It is slow on
purpose: the timing model is accurate to about ±200 ms, which is ±2 cm at this
speed and ±10 cm at 50 cm/s. The dashboard stamps every figure derived from it
`SIMULATED`, and so does the EPR ledger.

`Connect board` still works with the profile sourced — it builds an in-process
board rather than opening a port, so the decision → schedule → ETA → servo →
`SORT_CONFIRMED` half of the chain runs and is visible. There is still no load
cell, so the pan machine idles and the chain is driven from **Developer
controls → Measure & route now**.

`configs/conveyor.yaml` is untouched by any of this. Without the profile the
shipped machine is still `mode: NONE`, actuation off, geometry `UNMEASURED`.

### How the two profiles differ

| | `demo-profile.sh` | `bench-profile.sh` |
|---|---|---|
| belt, geometry | SIMULATED | SIMULATED — unchanged |
| mass | per-class stand-in, `SIMULATED` | **HX711, `MEASURED`** |
| transport | in-process board | **real serial, `/dev/cu.usbmodem1101`** |
| `HARDWARE_MODE` | `SIMULATION` | **`PHYSICAL`** |

The belt is a model in both, and every figure derived from it is still stamped
`SIMULATED` to the EPR ledger. The mass and the servo command are not: on the
bench profile they are a real reading off a verified cell and a real frame down
a real port.

**The two flags are not interchangeable.** `AURUM_SIMULATION` picks the
transport *and* the geometry, so turning it off alone drops the router onto the
`UNMEASURED` real geometry, which refuses to schedule — and the servo then never
fires, board attached or not. The bench profile leaves `AURUM_SIMULATION` unset
and keeps `AURUM_CONVEYOR_MODE=SIMULATION` for exactly that reason.

Verify the firmware before trusting a silent paddle: `aurum_sorter` answers
`AURUM/1 PING <id>` with `AURUM/1 PONG <id>`, and `aurum_weight` — which has no
servo code at all — does not.

Then, in the dashboard. It opens on the **operator** screen — the engineering
view is behind the mode switch, and `Developer controls` lives there.

Steps 1 and 2 run **by themselves** when the page loads: the start-up sequence
asks `/ready`, starts the camera, connects the board, and asks `/ready` again.
Do them by hand only if it fails.

| # | Action | What to expect |
|---|---|---|
| 1 | *(automatic)* **Start camera** | Live feed appears; pills show `CAMERA LIVE` |
| 2 | *(automatic)* **Connect board** | `BOARD LINKED`, `ACTUATION ON`, `CALIBRATED` all green |
| 3 | Hold the component to the camera | Box appears labelled `AUR-ITEM-xxxxxxxx CPU 0.94` |
| 4 | Wait for CONFIRMED | Three observations; "current item" fills in |
| 5 | Place it on the load cell | `Object detected on the pan` → `Measuring…` |
| 6 | **Do nothing** | A settled reading, `MEASURED`; the same item id gains a mass, a PMDI, a bin |
| 7 | Watch the paddle | Servo A or Servo B strokes; Bin C moves nothing |
| 8 | Take the object off | `Remove the object` → `Waiting for an object` |

`CAMERA LIVE`, `BOARD LINKED` and `ACTUATION ON` must be green before step 6. A
red or amber pill is the system telling you something is genuinely not ready —
it is not cosmetic.

Stages after the camera read "not weighed yet" / "waiting" until the object is
on the pan. That is pending, not failed. The banner at the top of the dashboard
is the machine's own account of where it is: if it says `Waiting for an object`
while something is sitting on the cell, read the reason underneath — an
uncalibrated cell and an unidentified mass both say so in plain words.

---

## What each component does, and why

Rehearse with all four so nothing is a surprise on camera.

| Component | Bin | Why | Needs a verified calibration? |
|---|---|---|---|
| **CPU** | **A** → Servo A | Configured premium class; cited gold per package (`CPU-AU-001`) is per-piece, so it needs no mass | No |
| **PCB** | **B** → Servo B | 2 200 ppm precious from cited composition, above the 100 ppm recoverable threshold | **Yes** — or a mock mass |
| **RAM** | **B** → Servo B | 18 mg gold per module, cited per-piece (`RAM-AU-001`, Charles et al. 2017, n=12 DIMMs), so it needs no mass | No |
| **Connector** | **A** → Servo A | Configured premium class, cited gold per piece | No |

**Weigh the actual pieces you plan to demonstrate, and check them against the
plausibility window** in `configs/grading.yaml` — CPU 5–500 g, RAM 3–200 g,
PCB **20**–5 000 g, Connector 0.5–200 g. Outside it the decision is `UNKNOWN`
with reason `UNKNOWN_MASS_ANOMALY` and the item goes to manual inspection. A
real 10.8 g PCB did exactly that on 2026-08-27: correctly identified, correctly
weighed, and refused because a 10.8 g board is not a plausible board. Bring one
over 20 g if you want to see Bin B.

**RAM routes to B, and this runbook used to say it routed to C.** It said the
system had no cited composition for a whole module and refused rather than
guessing. That stopped being true when `RAM-AU-001` was added: 18 mg of gold
per module, measured by Charles et al. across twelve DIMMs, on a per-piece
basis that needs no mass at all. Verified in simulation on 2026-08-27 — RAM
routes to **B** on `B_PRECIOUS_FRACTION`. Do not say "watch it get refused".

**The refusals are still the best thing in the demonstration** — there are just
two real ones now, and both are worth showing:

| Refusal | How to trigger it | What it proves |
|---|---|---|
| `UNKNOWN_CONFIDENCE` → C | Present a component badly — edge-on, moving, half out of frame — until confidence drops below the threshold | It will not act on a class it is not sure of |
| `UNKNOWN_MASS_ANOMALY` → C | The 10.8 g PCB, or anything outside its class's mass window | A mass that cannot be right makes the identity suspect, and it says so rather than valuing it |

`UNKNOWN_CLASS` is the third, but it needs an object of a class the model does
not know — a heatsink. Bring one and it goes to C on `UNKNOWN_CLASS`, which is
the cleanest refusal of the three: no class, no guess.

In every case the mock-mass fallback does not rescue the item: a stand-in mass
changes the arithmetic, never the evidence.

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

**0:50 — mass.** Place it on the cell and take your hand away. Point at
`MEASURED`.
*"That status means a settled reading on a calibration we verified against a
second known mass. Anything less reads STABLE, and the estimator refuses it."*

**1:30 — evidence.** Point at the evidence ids in the metals table. *"Every
figure traces to a published assay. Nothing here is a guessed constant."*

**2:00 — the decision.** Point at the bin and the reason code. *"The backend
decided that. Nobody typed it."*

**2:20 — the servo.** The paddle strokes. *"That is a real command over real
serial to a real Arduino."*

**2:45 — a refusal.** Present something badly, or use the 10.8 g PCB. It goes
to C and nothing moves. *"It will not act on a class it is not sure of, or on a
mass that cannot be right for that class. Bin C needs no actuator — it is what
happens when the software does nothing, which is also what happens if it
crashes."*

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
| `the board did not acknowledge the servo configuration` on the first connect | Known: the board dumps a large backlog when the port opens, and the first CFG's ACK is buried in it | **Press Connect board again.** The second attempt applies. Check `servo_config_applied: true` in `/session`, not `last_error`, which keeps the stale first message |
| `x.x g still on the pan` with nothing on it | Tare drift — it sat at ~8 g on 2026-08-27, over the 5 g arrival threshold, so the automatic cycle never arms | Stop the backend, clear the pan, re-run `python -m app.calibrate` |
| `UNKNOWN_MASS_ANOMALY` | The mass is outside the plausibility window for that class | Not a fault. Use a component inside the range — a PCB must clear 20 g |
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

**"Why did that one get rejected?"**
One of three reasons, and the dashboard names which: the class was below the
confidence threshold (`UNKNOWN_CONFIDENCE`), the mass was outside what is
plausible for that class (`UNKNOWN_MASS_ANOMALY`), or the class has no cited
composition in the database at all (`UNKNOWN_CLASS`). In none of them does the
system substitute a guess — it routes to C and records the reason.

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
