# Every way this machine has been observed to fail

A demonstration of Aurum did not work. This is the record of what was wrong,
how each fault was found, and what was done about it — fourteen defects across
two passes over the same question.

`docs/demo.md` is the runbook: what to do. This is the reference behind it: why
those instructions say what they say. Where the two disagree, the runbook is
the one to follow at a bench and this is the one to fix.

**Every number here was measured.** Nothing in this document is an estimate of
how bad something might have been.

---

## What the machine was doing when it failed

Reported symptoms: the chain stalling partway, wrong output, and backend 500s.
Configuration: **a stand-in mass with a real board** —
`AURUM_DEMO_MOCK_MASS=true`, `AURUM_SIMULATION` unset, so
`HARDWARE_MODE=PHYSICAL`.

That combination matters, because it is the one the runbook actually
recommends: the HX711 on this rig has read open since 2026-08-27, so the
stand-in mass is on and the camera — not the load cell — starts every cycle.
Six of the fourteen defects below only exist on that path.

---

## How it was found without the rig

The 1499-test suite was green before any of this and stayed green throughout.
Every defect lived somewhere tests do not reach: two threads on one file
descriptor, a blocking call under a lock, a fallback arming on a transient
condition, a configured value that had gone stale.

So the hardware was rebuilt in software. A stand-in for
`hardware/arduino/aurum_sorter` runs on a **pty** — `pty.openpty()`, echo
disabled — and the real backend is pointed at it with `AURUM_ARDUINO_PORT`.
It copies the sketch's *timing*, not just its grammar:

- the boot banner, then `W,1,<millis>,<counts>,OK` at 10 Hz
- `PING` / `CFG` / `MOVE` / `BELT`, with the sketch's own reply shapes
- a **blocking 1.711 s `push()`**, during which the board is deaf
- counts with real noise, because a bit-for-bit repeat is correctly refused as
  a dead converter

Three harnesses drive it:

| Harness | What it does |
|---|---|
| **fault injection** | switches the board between `ok`, `open`, `err`, `silent`, `flood`, `noack`, `moveerr`, `slowack`, `gone` while the backend runs |
| **soak** | ten minutes of real cycles with the dashboard's own poll rate and an open MJPEG stream; watches RSS, file descriptors, error count |
| **probes** | one-question scripts: is `pump()` re-entered concurrently, does a re-acquired object get sorted twice, how long does a `step()` block the route drain |

That rig reproduced the main failure on its first run: **seven pan cycles
completed with nothing ever placed on the pan**, and the scripted stage
fallback running **one object out of six**.

---

## The fourteen

### 1. The camera trigger fired on a gap, not on a dead cell

**Presented as:** the paddle firing at an object still in the operator's hand;
then, when that object was placed on the pan, *"180 g is on the pan, but no
assembly has been confirmed by the camera."* The item is never sorted, and its
mass is a stand-in rather than the real one. All three reported symptoms.

`BoardLink.next_weight` gives up after `conveyor.arduino.timeout_s` — one
second. The sketch is deaf for the whole **1.711 s** of a paddle stroke, and
`measure_and_route` drains the same queue from the HTTP thread. So a perfectly
healthy cell returns nothing every so often. `PanMachine` treated any empty
read as "the cell cannot start cycles" and handed the arrival to the camera,
which latched whatever was confirmed, gave it a fabricated mass, decided a bin
and fired a paddle. The real arrival was then refused, because that id was
already in the zone's handled set.

**Measured:** 7 spurious cycles with an empty pan; 1 of 6 scripted objects.

**Fixed** by separating a *verdict* from a *gap*. No board, no calibration
factor, a disconnected reader and a stuck converter all name their own cause
and hand over at once. An unexplained silence must last
`demo.camera_trigger.quiet_s` (3 s). The same gap in `WAITING_FOR_CLEAR` was
releasing an object still sitting on the cell.

### 2. An empty pan became the object's mass

**Presented as:** `UNKNOWN_MASS_ANOMALY: -0 g is below the 20 g minimum
plausible for a PCB` — which sends the operator to look at the identity when
the pan is simply empty.

The automatic path only weighs after a mass crosses the arrival threshold.
`measure_and_route` — *Measure & route now*, the documented fallback for a
bench with no working cell — had no such gate, so on the very bench the runbook
describes it read the empty cell and passed on a settled, **`MEASURED` 0.0 g**.

**Fixed** in `_process`, where both paths route through: a settled reading at or
below `conveyor.weight.pan.object_threshold_g` is not this object's mass. With a
stand-in configured the fallback then applies; without one the item is
`UNAVAILABLE` and routes to C on `UNKNOWN_WEIGHT`, which is honest.

### 3. Two clicks ran the whole chain twice

The "already handled" check and the claim sat either side of the settling read
— up to `conveyor.weight.timeout_s`, five seconds on the bench. Two callers
both passed: two valuations, two EPR trails, and a `ROUTING_ERROR` on the
dashboard for an item routed once. This endpoint is a button, and React
StrictMode double-invokes the effect behind it.

**Measured:** 3 concurrent callers, 3 full chain runs.

**Fixed** by claiming under the lock rather than asking.

### 4. Two threads read one serial port

`app/hardware/link.py` said no lock was needed because "a reader that is waiting
is a reader that is pumping". That is true of the *queues*. It is not true of
the *descriptor*: the pan thread takes a sample on every poll while the HTTP
thread runs a CFG or BELT exchange, and `_gate` — which serialises whole
exchanges — does not cover the weight pump.

**Measured:** 2,336 concurrent entries into `readline()` in 3 seconds.

`pyserial` builds a line out of repeated reads, so two callers each take part of
it and neither gets a frame. A torn weight frame is a mass that never settles; a
torn ACK is an `ACK_TIMEOUT` that latches a fault over a paddle that moved
perfectly well. **Fixed** with a lock held for exactly one `readline()` — not
across an exchange, so neither reader can starve the other.

> The concurrent access is demonstrated. The byte-tearing it enables is not, and
> cannot be without a real UART. Claimed as a race, not as observed corruption.

### 5. The snapshot held the camera's lock for 44 ms, 2.5 times a second

`/session` is polled every 400 ms and ran entirely under the lock the camera
thread takes for every frame.

| part | cost |
|---|---|
| `_provenance()` — re-parsing the 950-line composition database | **36 ms** |
| `pricing_snapshot()` — a network call under `pricing.provider: metalprice` | 3.4 ms, or 5 s on a bad network |
| everything else | < 1 ms |

**Fixed:** the provenance stamp is built once (only the calibration, which
`auto_tare` replaces, is refreshed), and the price lookup moved outside the
lock. 44 ms → 10 ms, and the lock-held part is ~6 ms.

### 6. `AURUM_CAMERA_INDEX=2` named a camera that does not exist

**The single most expensive line in the repository.** The camera is the *only
blocking check* in `/ready`, so a stale index is a machine whose dashboard
start-up stops at "Camera: could not start" with nothing downstream ever
running — and the error said only *"Could not open camera 2"*.

Probed on the demonstration laptop, 2026-09-11:

| index | `isOpened()` | delivers a frame |
|---|---|---|
| 0 | yes | **no** |
| 1 | yes | yes, 1920×1080 |
| 2 | **no** | — |

The indices then **shuffled again between two probes an hour apart in the same
session**. A positional device identifier is not configuration; it is a bet on
enumeration order.

**Fixed** the way the serial port already was. `AURUM_CAMERA_INDEX=auto` takes
the one index that *delivers a frame* — opening is not evidence — and **refuses
when two do**, naming both, because the wrong automatic choice is the built-in
camera pointing at the ceiling, which opens and reads and streams a plausible
wall. A named index is never second-guessed. A failure now names the indices
that work on this machine right now.

### 7. `/ready` reported a file as a load cell

`load cell calibrated` reads `configs/calibration.yaml`, which records a
measurement made on 2026-08-26 against two known masses. It says nothing about
whether the converter is converting today — and this rig's cell has read open
since 2026-08-27. `/ready` showed a full set of green ticks over a dead cell to
an operator thirty seconds before a demonstration.

**Fixed** with a second, advisory check: `load cell reading`, taken from the pan
machine's last poll rather than from a second reader on that port. The two are
meant to be read as a pair, and to disagree.

### 8. A port full of rubbish looked healthy

Against the documented backlog pathology — a headless weight fragment at full
line rate — the machine survived, recovered, and counted **94,398 unreadable
lines** while `/ready` stayed green and the error log stayed empty.
`dropped_lines` was the only number that knew, and nothing looked at it.

**Fixed:** the link counts what it *could* read as well as what it could not,
over a **100-line window** rather than lifetime totals — a lifetime ratio could
not recover, since 94,398 bad lines would need 94,398 good ones after them.
`board traffic readable` in `/ready` says so. Verified live: red during the
flood, green **15 s** after the board comes right, with the lifetime counters
still recording that it happened.

### 9. One physical object, three paddle strokes

On the bench state the runbook describes — cell open, stand-in mass, so the
camera starts every cycle — a RAM module shown, lost by the tracker and shown
again produced **three strokes and three EPR item ids**. A re-acquired object is
a new id, which the zone has never handled and cannot refuse. RAM is the class
the runbook already warns "flickers in and out" at 0.51 recall.

**Fixed** with a rate limit, and it is labelled as one: nothing can tell two
objects from one object seen twice out of an image alone.
`demo.camera_trigger.cooldown_s` is five seconds — shorter than the operator's
own loop, far longer than a tracker drop — and the refusal states the assumption
and offers the load cell, which has no cooldown because a pan *can* report the
object leaving.

### 10. A weigh starved the paddle queue

Both shipped profiles run `conveyor.mode: SIMULATION`, so a decision becomes a
`ScheduledRoute` that fires seconds later — 3.0 s to Servo A, 6.0 s to Servo B
from the load cell at the demonstration's 10 cm/s. Those strokes were fired
from the same loop that blocks on a settling mass.

**Measured:** one `step()` through `WEIGHING` held the loop for **548 ms**
against a `conveyor.routing.late_tolerance_ms` of **200**. A route whose moment
landed inside a weigh was refused as `TIMING_EXPIRED` and the item was never
sorted.

A ten-minute soak of 120 cycles caught it in the wild:

> `SERVO_ERROR | Its moment was 0.663s ago, past the 0.200s tolerance. Refusing
> to fire late: the item has passed, and a catch-up strikes whatever is behind
> it.`

Refusing to fire late is correct and stays — a paddle behind the item strikes
the next one. What was wrong was being late for a reason that has nothing to do
with the belt. **Fixed:** the drain has its own clock, and the scheduler copies
its route dict before walking it so an insert on the pan thread cannot break the
iteration.

| soak | cycles | polls | non-200 | errors |
|---|---|---|---|---|
| before | 120 | 2,874 | 0 | **1** |
| after | 96 | 2,284 | 0 | **0** |

### 11. A failed price fetch was retried on every call

A success is cached for fifteen minutes; a failure was worth nothing at all. The
dashboard prices four metals inside a `/session` snapshot it asks for every
400 ms, so on a venue network that cannot reach the feed every poll made a fresh
request and waited out its own five-second timeout.

**Fixed:** a refusal is held for `pricing.metalprice.retry_seconds` (30 s). A
cached snapshot still wins over a hold, so an outage degrades to the last real
price rather than to nothing, and every call still says what is wrong.

### 12. Two dashboard tabs started two cameras and two board links

`App.jsx` opens the camera and connects the board by itself on load. A second
tab, a refresh, or Retry pressed twice runs both again — and both entry points
were check-then-act, with both checks running before either acted.

**Measured:** 2 camera opens, 2 `BoardLink`s.

Two captures on one device halve the frame rate and leak a thread `stop()`
cannot join. Two `BoardLink`s on one port fight over an advisory lock that is
per open-file-description, so the loser reports *"already owned by another
process"* about its own process — a message that has already cost bench
sessions.

**Fixed** with one start-up lock over both transitions. Deliberately **not** the
session lock: both hold for seconds, and the camera thread takes that lock for
every frame.

### 13. Routing one object twice overwrote the record that worked

The automatic cycle routes what it weighed; *Measure & route now* routes what
the camera confirmed. Press the button mid-cycle and both arrive. Nothing moved
twice — the scheduler refuses a second route per item id, and the board refuses
a second command — but the refusal replaced a good actuation record with
`ALREADY_ROUTED` and put a `ROUTING_ERROR` on the dashboard for an item routed
exactly once.

**Fixed:** `_route` is idempotent **by item id**, not by object — `assemblies`
regroups on every read, so the two callers routinely hold two different
`Assembly` instances of one item — and it *claims* before it works, because the
scheduling and the serial round trip both run with the lock released.

### 14. The operator screen gave away the strongest claim in the demonstration

*"Weights are assumed, not measured"* was keyed off `demo.mock_mass.enabled`,
which the bench profile ships **ON** as a fallback. A live cell still wins on
every pass — so the caveat appeared underneath a `MEASURED` mass taken off a
verified calibration, telling an audience the number was fabricated when it was
not.

**Fixed:** `massCaveat(state)` reads the reading, not the setting, and lives in
`status.js` beside `loadCellSilent`, which exists for the same reason.

---

## Also changed

**The dashboard polled on `setInterval`,** which does not wait for the request
it started. A five-second snapshot at 400 ms puts a dozen requests in flight,
past the browser's six-per-origin cap. Now a self-scheduling timer. Not a
measured failure — defect 11 was the thing that would have caused it.

**Documentation that disagreed with the machine:**

| Said | Was |
|---|---|
| stand-in masses CPU 25 g / PCB 180 g / RAM 30 g | **22 / 60 / 20** since 2026-08-27, in four files |
| the bench profile has mock mass **off** | both profiles ship it **on**, as a fallback |
| `/ready` has eight checks | **ten** |
| "the external webcam is index 2" | index 2 did not exist |
| "re-probe with `for i in 0 1 2 3`" | `auto` does it, and refuses when ambiguous |

---

## Hypotheses tested and discarded

Recorded so they are not investigated again.

| Suspected | Tested | Result |
|---|---|---|
| SQLite contention in the EPR ledger | 11,662 writes + 2,862 reads, 6 threads, 12 s | **0** "database is locked". No WAL needed |
| Threadpool starvation from the MJPEG feed | 45 concurrent `/session/stream` | `/health` still answered in 0.03 s |
| `scripted.step()` routing the wrong object | a competing confirmed object in view | resolves correctly; the cascade was defect 1 |
| The valuation layer | read `pmdi.py` and `materials.py` end to end | multi-class, zero-mass and simulated-mass refusals all correct |
| `scripts/bench_check.py` exit code | ran it against a held port | exits 1 correctly; the earlier reading was `$?` from a pipe |

---

## A test that was lying

`tests/test_end_to_end.py` called `connect_board()` — which starts the machine
loop — and then drove the same `PanMachine` by hand. Two threads stepping one
state machine failed **about one run in five**, on `main` as well: stashing all
of the above and running it six times gave four failures.

Fixed as a test (`AURUM_PAN_AUTO=false` in the two affected classes; 20
consecutive clean runs). It did point at a real product race, which is defect 13
and has its own deterministic test.

**Quantify a flake, then stash and quantify again, before deciding whose it is.**

---

## Known and unfixed

- **`/session/start?mode=images` never advances.** `DemoSession.start_camera`
  hardcodes `image_seconds=0.0` — "0 = manual" per the CLI's own help — but the
  API exposes no way to advance, so the folder is frozen on its first file.
  `run_demo.py` passes 3.0 and is unaffected. Undocumented path; left alone
  rather than changed on a guess.
- **A real belt would still meet defect 10's root cause.** The drain thread
  removes the starvation. A physical belt whose firing moment falls inside a
  1.7 s stroke is a different problem, and there is no belt yet.
- **Defect 9 is bounded, not solved.** A cooldown limits one physical object to
  one stroke per five seconds. Telling two objects apart needs an association
  strategy that is not "the most recently confirmed assembly" —
  `app/pipeline/association.py` is where that would go.

---

## Verification

- **1,541 tests passing**, three consecutive full-suite runs, no flakes
- **30 new tests**, each of which fails without its fix
- `ruff check` clean, `ruff format` clean, frontend builds
- Live on the pty rig: the scripted fallback runs all six objects
  (CPU→A, PCB→B, RAM→B, Connector→A, low-confidence CPU→C, Heatsink→C), and a
  real 180 g arrival gives PCB → `MEASURED` → Bin B → a real `MOVE B` frame down
  the port → ACK → `SORT_CONFIRMED`, 10 EPR events, 0 dropped lines, no latched
  fault, zero spurious cycles
- Every fault mode — open cell, powered-down HX711, silent board, backlog flood,
  unacknowledged MOVE, cable out — leaves the backend up, every endpoint
  answering 200, and something true on the screen
