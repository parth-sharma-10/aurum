# Aurum — Completion Plan

## NEXT SESSION CHECKPOINT

**Read this first. It is the resume point.**

| | |
|---|---|
| Branch | `feat/sih-demo-pipeline` |
| Phase 7 | **COMPLETE in software.** Not physically validated |
| Phases 8–9 | **COMPLETE for the demonstration's scope** |
| Tests | **790 passing**, ruff check clean, ruff format clean, frontend builds |
| Blocking | One bench session: flash, calibrate, watch a paddle move |

### SCOPE CHANGE — the demonstration has no conveyor

The SIH demonstration proves perception, measurement, material intelligence and
actuation. **The operator carries the component between stages.** Routing is
immediate: the decision is taken and the paddle moves.

Conveyor simulation, belt speed, camera-to-servo distances and firing times are
**out of scope**. `app/routing/` keeps that model intact for a future belt and
`app/pipeline/session.py` does not call it. Do not wire it in.

### DO NOT REDO PHASES 0-6. They are complete and committed.

Phase 0 checkpoint · 1 config · 2 PMDI/pricing/valuation · 3 A/B/C decision ·
4 tracking/item lifecycle · 5 HX711 software · 6 routing geometry + scheduler.

### What now exists end to end

- `hardware/arduino/aurum_sorter/aurum_sorter.ino` — HX711 at 10 Hz sharing one
  115200 link with `AURUM/1 MOVE`. Servos unattached until commanded, an 8-slot
  recent-id list for idempotency, `C` refused as `BAD_TARGET`, `CFG` for
  runtime servo angles. **ACK follows the stroke**, hence `ack_timeout_ms` 2 s.
- `app/hardware/link.py` — `BoardLink`: one port, two protocols, neither read
  as the other. Single-threaded; whoever waits also pumps.
- `app/pipeline/session.py` — `DemoSession`: the join. Camera thread → identity
  → mass → PMDI → decision → servo, every failure recorded on the item.
- API: `POST /session/start`, `/session/board/connect`, `/session/measure`,
  `/session/stop`; `GET /session`, `/session/stream`, `/session/frame`,
  `/arduino`.
- Frontend: the chain stage by stage, live feed with the item id drawn on the
  box, four hardware pills, and a routed-items table.
- Tests: `test_arduino.py` (37), `test_link.py` (21), `test_session.py` (23).
  The hardware layer had **zero** coverage before this.

### Fixed on the way

- Baud reconciled to 115200 everywhere.
- A settling bug: the stability window compared the clock against the oldest
  sample still inside it, which required a sample to land exactly on the
  boundary. A 450 ms window on a 10 Hz cell never settled however still the
  mass was; 500 ms only worked because 100 ms divides into it.
- `pyserial` uncommented in `requirements.txt`.

### FIRST ACTION NEXT SESSION — the bench

Software cannot advance this any further. In order:

1. Flash `aurum_weight`, confirm `W,1,…,OK` at 115200.
2. `python -m app.calibrate --port <PORT> --reference-mass 180 --verify-mass 100`
   until `verified: true`. **Until then no PCB can reach Bin B.**
3. Flash `aurum_sorter`, confirm weight frames still stream.
4. `POST /session/board/connect`, check `GET /arduino`.
5. CPU → watch **Servo A**. PCB → watch **Servo B**. RAM → watch **nothing**.

### PHYSICAL VALIDATION — still none, for anything

Verified by the user, independently of this software: HX711 responds (180 g ->
about 65 000 counts), Servo A moves, Servo B moves, power wiring corrected.

**Never done:** Arduino-Python communication · any servo moved by Aurum code ·
calibration workflow run · either sketch uploaded.

A passing test suite says the software is right about what it would send. It
does not say a paddle moved. Do not mark Phase 7 physically complete until
`Python -> Arduino -> ACK -> Servo A` has been **watched**, the same for B, and
`C` confirmed to move nothing.

---


Status: **Phase 0 complete.** Baseline green, all work pushed to
`feat/aurum-completion`.

This document is the contract for finishing Aurum end to end. It records what
each phase builds, how it is verified, what it depends on, and what could go
wrong. It also records every engineering approximation, so that no number in
this system is untraceable.

---

## Terminology

The canonical term is **PMDI — Precious Metal Density Index**. "PMBI" is not
used anywhere in this repository and never has been; a full-tree search returns
zero hits. No terminology migration is required.

---

## Baseline at Phase 0

| Check | Result |
|---|---|
| `pytest -q` | **239 passed** |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 42 files already formatted |
| `npm run build` | built in 297 ms, 200.10 kB |
| Working tree | clean |
| Branch | `feat/aurum-completion`, pushed, 0 unpushed commits |
| Checkpoint | `6649611` |

The checkpoint newly tracks the entire material-evidence layer, which had been
untracked: `app/materials.py`, `configs/material_reference.yaml`,
`docs/sources/material_sources.yaml`, `docs/material-reference.md`,
`tests/test_materials.py`, `research/`.

---

## The PMDI formula, and what it can and cannot decide

The concept document (`Project Aurum: AI-Driven Cyber-Physical System…`, §4)
defines:

```
PMDI = (Σ (C_type × Y_estimated)) × P_spot
```

Per conveyor item, `C_type = 1`, so this reduces to `Y_estimated × P_spot` —
the precious-metal value of one component.

Four properties of this formula shape the whole design:

1. **It yields currency, not a density.** `count × grams × currency/gram` has
   units of currency. Nothing is divided by mass. Aurum therefore computes
   *both* the concept-document quantity (`pmdi_value`) and a true density
   (`precious_mass_fraction`), and names them separately.
2. **It requires a live price.** No `P_spot`, no PMDI. This is the source of
   the price-dependency conflict resolved below.
3. **It measures precious metal only.** It cannot separate Bin A from Bin B,
   because Bin B is defined on base metals. A separate `base_metal_value`
   signal carries that, and is never called PMDI.
4. **`Y_estimated` is defined as *yield*.** The repository holds *contained
   composition*. These are not the same quantity, and Aurum labels every
   figure `contained`, never `yield` or `recoverable`.

### Resolving the price-dependency conflict

The brief requires both that grading use value thresholds (§14) and that a
price outage must not stop the conveyor (§21). These conflict. The resolution:

`precious_mass_fraction` is **price-independent** and is the primary A/B
discriminator. `pmdi_value` is a secondary check applied only when a fresh
price is available. `configs/grading.yaml` carries an explicit
`price_unavailable_policy` with two supported values:

- `mass_fraction_only` — grade on the price-independent rule (default)
- `route_to_c` — refuse to grade without a price

Both are tested. Neither fabricates a price.

### The data conflict Phase 3 must resolve

On the currently cited evidence, **a PCB outranks a CPU on every precious-metal
metric**:

| Item | Precious metals cited | Precious mass | Fraction |
|---|---|---|---|
| PCB @ 1.8 kg | Au 400, Ag 1300, Pd 500 mg/kg | ~4.0 g | **~2 200 ppm** |
| CPU @ ~42.7 g | Au only, 4.71 mg/piece | 0.00471 g | **~110 ppm** |

This is an artefact of a **data gap, not of physics**: FIRSCHING2024 reports
gold per CPU package and no silver or palladium, so the CPU's precious total is
gold alone. Any evidence-driven rule will therefore send **PCB → A and
CPU → B**, which is the opposite of the intent stated in the brief (§5).

**DECIDED (Option 2 - class-aware engineering rules).** The evidence-driven
ranking stands exactly as the evidence says it is: PMDI keeps its scientific
meaning, and no cited figure is adjusted to make the bins behave. The A/B/C
bins are a *sorting policy* applied after PMDI, not a ranking of precious-metal
mass, so `grading.bin_a.preferred_classes` may route a class to the premium
stream on operational grounds.

Listing CPU there is an **engineering classification policy - not a scientific
PMDI threshold**, and `configs/grading.yaml` says so at the point of use. The
mechanism is general (class + evidence + metrics + confidence + configured
policy -> A/B/C), not a hardcoded `CPU -> A` special case, so it can be retired
when cited Ag/Pd figures for CPU packages exist.

---

## Prototype Engineering Assumptions

Every value here is an **engineering approximation**, chosen to make the
prototype behave sensibly. **None is a validated scientific cutoff.** Each is
configurable and each is tested at its boundary.

| Assumption | Starting value | Why | Source | How to replace |
|---|---|---|---|---|
| Bin A precious mass fraction | 1 000 ppm | Order of magnitude above a typical mixed-WPCB figure, so A means "notably enriched" | **none — engineering approximation** | `configs/grading.yaml` → `bin_a.min_precious_fraction_ppm` |
| Bin B precious mass fraction | 100 ppm | Above the analytical noise floor of the cited sources | **none — engineering approximation** | `bin_b.min_precious_fraction_ppm` |
| Bin A detection confidence | 0.75 | Above the model's operating conf of 0.35, so a servo needs a strong detection | **none — engineering approximation** | `bin_a.min_confidence` |
| Bin B detection confidence | 0.60 | Ditto, lower bar for the non-premium stream | **none — engineering approximation** | `bin_b.min_confidence` |
| Weight stability window | 500 ms, ±0.5 g | HX711 settling behaviour; must be tuned on the real cell | **none — engineering approximation** | `configs/conveyor.yaml` |
| Price staleness limit | 900 s | Spot metals move slowly enough that 15 min is defensible for a prototype | **none — engineering approximation** | `configs/price_reference.yaml` |
| Belt speed, all distances | `UNMEASURED` | Must be physically measured on the rig | **none — must be measured** | `configs/conveyor.yaml` |
| Servo actuation delay | `UNMEASURED` | Depends on the servo and linkage | **none — must be measured** | `configs/conveyor.yaml` |

Values marked `UNMEASURED` cause the routing layer to refuse to schedule and
route every item to C. That is deliberate: the system will not pretend to know
where an item is on a belt whose speed nobody has measured.

---

## Phases

### Phase 0 — Git checkpoint and repository protection ✅ COMPLETE

**Files:** none modified; all existing work committed.
**Implementation:** checkpoint commit `6649611`; feature branch
`feat/aurum-completion` created and pushed (a repository hook forbids pushing
to `main` directly).
**Tests:** 239 pass. **Acceptance:** working tree clean, 0 unpushed commits,
research present on the remote — all verified.
**Dependencies:** none. **Known risks:** none remaining.

---

### Phase 1 — Configuration foundation ✅ COMPLETE

**Files:** `app/config.py` (new), `env.example` (extend), `configs/conveyor.yaml`
(new), `configs/grading.yaml` (new), `tests/test_config.py` (new).

**Implementation:** one settings object resolving **defaults → YAML →
environment**, with the environment winning. Command-line arguments still beat
all three, applied by each caller at its own argparse site. Covers camera index and backend, YOLO confidence, host/port,
load-cell settings, `ARDUINO_PORT`/`ARDUINO_BAUD`, servo geometry, belt speed,
timing offset, price provider, cache duration, and `SIMULATION`. No new
dependency: a ~25-line `.env` parser, since `python-dotenv` would be one import
for one file format.

**Tests:** precedence (CLI beats env beats default), missing file, malformed
line, type coercion, `UNMEASURED` sentinel handling.

**Acceptance:** 289 tests pass (50 new), ruff check and format clean, API
starts, frontend builds. `config.load()` resolves 33 settings.

**Deliberately deferred:** `app/weight.py` and `ml/train.py` still read
`os.environ` directly. Migrating them belongs to Phase 5 and to the ML work
respectively - `ml/train.py`'s defaults are bound to the published model
release, and changing them here would reach outside this phase. Until then
`config.py` is the single resolution point for everything new, and reuses the
existing `AURUM_HX711_PORT` and `AURUM_CAMERA_BACKEND` names so the two paths
cannot diverge.

---

### Phase 2 — PMDI and pricing ✅ COMPLETE

**Files:** `app/valuation/__init__.py`, `app/valuation/pmdi.py`,
`app/valuation/prices.py`, `app/valuation/valuation.py` (new),
`app/pricing.py` (fold into `valuation/prices.py`, keep a shim),
`configs/price_reference.yaml`, `docs/pmdi.md` (new),
`tests/test_pmdi.py`, `tests/test_prices.py` (new).

**Implementation:** `pmdi.py` computes, per item, `precious_mass_g` (Au/Ag/Pd),
`base_mass_g` (Cu/Ni/Sn), `precious_mass_fraction`, and — when a fresh price
exists — `pmdi_value` and `base_metal_value`. Every output carries
`evidence_ids`, `confidence`, and a `status` from
`REAL | ESTIMATED | SIMULATED | UNAVAILABLE | STALE | APPROXIMATE`.
`prices.py` adds a provider protocol with timeout, cache, timestamp, and an
explicit stale flag. **No provider ships with fabricated prices**; the static
provider reads `configs/price_reference.yaml`, which stays `enabled: false`
until a real source is configured.

**Tests:** formula against hand-computed values; unit conversion mg↔g↔kg;
missing composition → `available: false`, never zero; price success, timeout,
stale, absent; cache hit and expiry; evidence propagation.

**Acceptance:** 289 → 371 tests (82 new). `/prices` and the `pmdi` block on
`/batches/{id}/valuation` verified live against a running server.

**On the `"pmdi"` guard in `tests/test_api.py`:** reviewed and **kept**, not
deleted. It is a deliberate contract scoped to `/stats` — the aggregate endpoint
a dashboard renders with no provenance attached, where a money figure would be
quoted stripped of the evidence, staleness and simulation flags that qualify it.
PMDI *is* exposed, on `/batches/{id}/valuation` and `/prices`, where every
figure travels with its evidence ids and status. The test's docstring now
records that reasoning, and `TestPmdiIsExposedWithItsProvenance` asserts the
other half.

**Dependencies:** Phase 1. **Known risks:** no price provider is chosen yet;
until one is, `pmdi_value` is permanently `UNAVAILABLE` and only the
price-independent path works. This does not block sorting.

---

### Phase 3 — Approximate A/B/C decision engine ✅ COMPLETE

**Files:** `app/decision/__init__.py`, `app/decision/engine.py` (new),
`configs/grading.yaml`, `tests/test_decision.py` (new).

**Implementation:** the §7 priority ladder, in order: invalid detection → C;
no material evidence → C; required measurement missing → C; stale or invalid
data → C; then compute the precious estimate; then the A threshold; then the B
threshold; else C. Output is a `GradeDecision` carrying `grade`, `reason`,
`confidence`, `target_bin`, `servo`, and the full evidence chain — enough to
answer "why did this go to Bin A?" without re-deriving anything.

**Tests:** all three grades; unknown class; low confidence; missing evidence;
invalid mass; RAM → C; PCB with measured mass; PCB with simulated mass;
**and boundary tests** — just below A → not A, exactly A → A, just above A → A,
just below B → C, exactly B → B.

**Acceptance:** every threshold read from config, none hardcoded; every
decision carries a human-readable reason; C is reachable from every failure
path.

**Dependencies:** Phases 1–2.

**Resolved:** Option 2 was chosen and implemented. `class_aware` selects which
gate Bin A uses — `preferred_classes` membership when true, the fraction and
value thresholds when false. Both are configuration; neither edits the
evidence. With the default policy CPU and Connector reach A while PCB reaches
B; with `class_aware: false` the evidence-driven ordering returns and PCB
reaches A. Both behaviours are tested, and the reason code on every decision
says which gate fired.

**Remaining risk:** the data gap itself. CPU evidence is gold only, so the
class-aware policy is standing in for evidence that does not exist yet. It can
be retired the day cited Ag/Pd figures for CPU packages are added.

---

### Phase 4 — Tracking and item lifecycle ✅ COMPLETE

**Files:** `app/vision/__init__.py`, `app/vision/tracker.py`,
`app/pipeline/item_pipeline.py`, `app/models/item.py` (new),
`tests/test_tracker.py`, `tests/test_pipeline_item.py` (new).

**Implementation:** `TrackedItem` as the canonical per-object record. Tracking
via Ultralytics' built-in `model.track()` (ByteTrack) — **no new dependency**.
`BatchSession` is untouched and keeps serving aggregate statistics; the two
paths coexist per the brief's §4.

**Tests:** stable IDs across frames; lost and reappearing tracks; one physical
item never produces two ledger records; state transitions.

**Acceptance:** 539 tests (91 new). Verified with the real model and real
ByteTrack: two RAM modules held identity across ten translated frames, velocity
tracked at 6 px/frame, finalized exactly twice, a second finish closed nothing.

**New dependency:** `lap==0.5.13`, required by ByteTrack for its linear
assignment step. Ultralytics AutoUpdate pip-installs it on first use, which is a
network call in the middle of a demo -- it is now pinned in `requirements.txt`
so it installs up front instead.

**Known risks:** ByteTrack ID switches on occlusion, mitigated by the
singulator, which is a physical rather than a software guarantee. Tracking has
**not** been validated against a real camera on a real conveyor -- only against
the real model on translated stills.

---

### Phase 5 — HX711 weight integration ✅ COMPLETE

**Files:** `app/sensing/weight.py` (moved from `app/weight.py`),
`tests/test_weight.py` (extend).

**Implementation:** tare, calibration, raw reading buffer, median filter,
stability detection over a configurable window, timeout, and the state set
`RAW | UNSTABLE | STABLE | SIMULATED | MEASURED | UNAVAILABLE`. The first
reading is never accepted. Simulated mass propagates a `simulated: true` flag
all the way to the ledger and the dashboard, and `app/materials.py` continues
to refuse concentration maths on simulated mass.

**Tests:** stable vs unstable series; calibration; tare; timeout; invalid
reading; simulated mode; simulated mass never satisfies a concentration
calculation.

**Acceptance:** 612 tests (73 new). PCB valuation is computable from a
`MEASURED` reading and refused from `SIMULATED`, `STABLE`, `UNSTABLE` or
`UNAVAILABLE` ones.

**Module move deferred.** This phase kept the code in `app/weight.py` rather
than moving it to `app/sensing/weight.py`. The move is mechanical but touches
`app/api.py`, `app/demo.py` and two test modules, and doing it inside a phase
that also changes behaviour would mix a rename into a functional diff. It is
folded into the Phase 10 cleanup as one move-only commit.

**Known risks:** the hardware exists and responds -- a 180 g mass moved the
cell by ~65 000 counts on the bench -- but **nothing in this phase has run
against it.** The sketch has never been uploaded, the serial link has never
been opened by Aurum, and the calibration workflow has never been executed.
`configs/calibration.yaml` stays UNMEASURED until it is.

---

### Phase 6 — Routing geometry and scheduler ✅ COMPLETE

**Files:** `app/routing/geometry.py`, `app/routing/scheduler.py` (new),
`configs/conveyor.yaml`, `tests/test_routing.py` (new).

**Implementation:** `fire_time = detected_at + distance/belt_speed + offset`,
held in a priority queue supporting many simultaneous items. Any `UNMEASURED`
geometry value makes the scheduler refuse and route to C.

**Tests:** timing arithmetic; multiple items in flight; A→B, B→A, C between
A and B; duplicate-fire prevention; a decision arriving after the item has
passed the servo; `UNMEASURED` → C.

**Acceptance:** 705 tests (93 new). The full demonstration chain runs end to
end on the simulated profile: camera to tracking to identity to weight to
PMDI to A/B/C to a scheduled route with a countdown that becomes DUE.

**Known risks:** timing error from Python, serial latency and inference jitter
is roughly +/-150-250 ms - +/-2.5 cm at 10 cm/s, +/-12 cm at 50 cm/s. Run the
belt slow and make bin mouths wide. And the larger one: **the conveyor does not
exist.** Every routing number so far comes from a TEST profile, and none of the
six physical quantities has been measured.

---

### Phase 7 — Arduino and servo integration  IN PROGRESS

**STATUS: PARTIAL. Do not read this phase as complete.**

**Implemented so far** (committed, lint clean, 705 existing tests still pass):

- `app/hardware/transport.py` — `Transport` interface, `SerialTransport`,
  `FakeTransport`. Link states DISCONNECTED / CONNECTING / CONNECTED /
  DEGRADED. Nothing above this file imports pyserial.
- `app/hardware/arduino.py` — versioned line protocol, `Command` lifecycle
  (CREATED / SENT / ACKED / FAILED / TIMED_OUT / SUPPRESSED), ACK waiting
  against `arduino.ack_timeout_ms`, two-level duplicate protection (per item
  and per command id), no automatic retry.
- `conveyor.arduino.enabled` — **actuation ships OFF.** Nothing can be
  commanded to move until it is deliberately switched on.

Protocol:

```
host  ->  AURUM/1 MOVE <A|B> <item_id> <command_id>
host  ->  AURUM/1 PING <command_id>
board ->  AURUM/1 ACK <command_id> [DUP]
board ->  AURUM/1 ERR <command_id> <code>
board ->  AURUM/1 PONG <command_id>
```

Weight frames (`W,1,...`) share the link and are ignored by the command layer.

**NOT yet done in this phase:**

1. `app/hardware/servos.py` — the bridge that takes a DUE `ScheduledRoute`
   from the Phase 6 scheduler, issues the command, and calls
   `mark_executed()`. **Routing and actuation are not yet joined.**
2. `hardware/arduino/aurum_sorter/aurum_sorter.ino` — the combined
   weight + servo sketch. The Phase 5 weight-only sketch is unchanged and
   remains the one to use for calibration.
3. `tests/test_arduino.py` — the full Phase 7 matrix. Behaviour was verified
   by hand only (ACK, per-item duplicate suppression, C rejected as
   BAD_TARGET, actuation-disabled refusal).
4. API endpoints (`GET /arduino`, `GET /actuation`).
5. Servo angles as configurable parameters. Bench values (REST 0 deg,
   PUSH 90 deg) are **not** final mechanical geometry.

**PHYSICAL VALIDATION: NONE.** Arduino to Python communication has never been
run. No servo has been moved by any Aurum code. Servo A and Servo B move on
the bench under independent testing only.

**Correction to earlier notes:** Servo B physically exists, is wired to D10 and
is bench-tested. Any earlier statement doubting its existence is outdated.


**Files:** `app/hardware/arduino.py`, `app/hardware/servos.py`,
`hardware/arduino/aurum_sorter.ino` (new), `docs/hardware.md` (new),
`tests/test_arduino.py` (new).

**Implementation:** the §20 protocol — `FIRE,A,104` / `ACK,A,104`, `PING`/`PONG`,
`STATUS` — with message ids, acknowledgement, timeout, reconnect and
duplicate-command protection. Backend decides; Arduino only actuates. Safe
startup: servos are not energised until a `STATUS,OK` handshake completes.

**Tests:** command serialization; ACK; timeout; reconnect; duplicate protection;
disconnected Arduino **never** produces a ledger record claiming the item was
sorted.

**Acceptance:** a simulated serial port drives the full protocol; real hardware
verified when the board arrives.

**Dependencies:** Phase 6. **Known risks:** board and servo count still unknown;
servo power draw may need a separate 5 V supply.

---

### Phase 8 — API and frontend integration

**Files:** `app/api.py` (extend), `frontend/src/*` (extend),
`tests/test_api.py` (extend).

**Implementation:** add the §25 endpoints alongside all existing ones, which
keep working. Dashboard shows the full reasoning chain — class, confidence,
mass, evidence, PMDI, value, grade, routing, hardware — with explicit
`MEASURED / ESTIMATED / SIMULATED / APPROXIMATE / STALE / UNAVAILABLE` badges
and a persistent `SIMULATED MODE` banner.

**Tests:** every new endpoint; existing endpoints unchanged; simulated records
never appear in production aggregates.

**Dependencies:** Phases 2–7. **Known risks:** the React dashboard is currently
read-only and polls; live item state may need WebSockets, used only where
polling genuinely fails.

---

### Phase 9 — End-to-end integration

**Files:** `app/pipeline/item_pipeline.py` (complete), `run_demo.py` (extend),
`tests/test_end_to_end.py` (new).

**Implementation:** wire camera → tracker → weight → evidence → PMDI →
valuation → decision → scheduler → Arduino → ledger → dashboard, with the
single-INSERT ledger invariant preserved.

**Tests:** the §30 acceptance flow in simulation; RAM → C; PCB → B or C per
config; low confidence → C; price outage → sorting continues; Arduino
disconnected → no false "sorted" record; simulation isolation.

**Dependencies:** all prior phases.

---

### Phase 10 — Tests, documentation and demo hardening

**Files:** `README.md`, `docs/architecture.md`, `docs/pmdi.md`,
`docs/hardware.md`, `docs/demo.md`, `docs/material-reference.md`.

**Implementation:** make every document match the implementation. Fix the three
stale references to the deleted `configs/recovery_reference.yaml`
(`docs/architecture.md:31`, `:102`, `configs/price_reference.yaml:7`). Update
`README.md:761` and `:823`, which state PMDI is unimplemented. Carry the
Prototype Engineering Assumptions table above into the README verbatim, with
the required statement:

> The A/B/C threshold values are configurable engineering approximations for
> the current prototype and are not presented as universally validated
> scientific cutoffs.

**Acceptance:** `pytest`, `ruff`, frontend build, API startup all green; no
document contradicts the code.

---

## Blocked on physical measurement, not on software

These stay `UNMEASURED` in config and force items to C until measured on the
real rig: belt speed; camera→load-cell, camera→servo-A and camera→servo-B
distances; servo actuation delay; HX711 calibration factor. Also outstanding:
the Arduino board model and servo count, and the choice of metal price provider.
