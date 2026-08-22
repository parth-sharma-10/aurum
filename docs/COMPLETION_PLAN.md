# Aurum — Completion Plan

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

Phase 3 does not resolve this silently. It ships the evidence-driven rule as
the default and surfaces the conflict for an explicit product decision. The
three options are recorded in Phase 3's *Known risks*.

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

### Phase 1 — Configuration foundation

**Files:** `app/config.py` (new), `env.example` (extend), `configs/conveyor.yaml`
(new), `configs/grading.yaml` (new), `tests/test_config.py` (new).

**Implementation:** one settings object resolving **defaults → `.env` → CLI**,
with CLI winning. Covers camera index and backend, YOLO confidence, host/port,
load-cell settings, `ARDUINO_PORT`/`ARDUINO_BAUD`, servo geometry, belt speed,
timing offset, price provider, cache duration, and `SIMULATION`. No new
dependency: a ~25-line `.env` parser, since `python-dotenv` would be one import
for one file format.

**Tests:** precedence (CLI beats env beats default), missing file, malformed
line, type coercion, `UNMEASURED` sentinel handling.

**Acceptance:** no module reads `os.environ` directly except `config.py`;
`pytest`, `ruff`, frontend build all green.

**Dependencies:** none. **Known risks:** `app/weight.py` and `ml/train.py`
already read env vars; they must be migrated without changing training defaults,
which are bound to the published model release.

---

### Phase 2 — PMDI and pricing

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

**Acceptance:** `tests/test_api.py:249` currently asserts `"pmdi"` never appears
in API output. That test is updated, not deleted — it becomes an assertion that
PMDI appears only with its evidence and status attached.

**Dependencies:** Phase 1. **Known risks:** no price provider is chosen yet;
until one is, `pmdi_value` is permanently `UNAVAILABLE` and only the
price-independent path works. This does not block sorting.

---

### Phase 3 — Approximate A/B/C decision engine

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

**Known risks:** the PCB-outranks-CPU conflict documented above. Three options,
to be decided explicitly rather than silently:
1. **Accept the evidence-driven ordering** — PCB → A, CPU → B. Honest, and
   contradicts the brief's stated intent.
2. **Class-aware rules in `grading.yaml`** — labelled as an engineering
   approximation, not as research. Matches the brief's intent; is a proxy.
3. **Close the data gap** — find cited Ag and Pd figures for CPU packages.
   Correct, and the slowest.

---

### Phase 4 — Tracking and item lifecycle

**Files:** `app/vision/__init__.py`, `app/vision/tracker.py`,
`app/pipeline/item_pipeline.py`, `app/models/item.py` (new),
`tests/test_tracker.py`, `tests/test_pipeline_item.py` (new).

**Implementation:** `TrackedItem` as the canonical per-object record. Tracking
via Ultralytics' built-in `model.track()` (ByteTrack) — **no new dependency**.
`BatchSession` is untouched and keeps serving aggregate statistics; the two
paths coexist per the brief's §4.

**Tests:** stable IDs across frames; lost and reappearing tracks; one physical
item never produces two ledger records; state transitions.

**Acceptance:** an item retains identity from first detection to ledger write.

**Dependencies:** Phase 1. **Known risks:** ByteTrack ID switches on occlusion;
mitigated by the singulator, which is a physical, not software, guarantee.

---

### Phase 5 — HX711 weight integration

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

**Acceptance:** PCB valuation is computable from a real stable reading and
refused from a simulated one.

**Dependencies:** Phases 1, 4. **Known risks:** no HX711 hardware is attached;
everything is verified in simulation until it is.

---

### Phase 6 — Routing geometry and scheduler

**Files:** `app/routing/geometry.py`, `app/routing/scheduler.py` (new),
`configs/conveyor.yaml`, `tests/test_routing.py` (new).

**Implementation:** `fire_time = detected_at + distance/belt_speed + offset`,
held in a priority queue supporting many simultaneous items. Any `UNMEASURED`
geometry value makes the scheduler refuse and route to C.

**Tests:** timing arithmetic; multiple items in flight; A→B, B→A, C between
A and B; duplicate-fire prevention; a decision arriving after the item has
passed the servo; `UNMEASURED` → C.

**Acceptance:** the §18 simulator prints a correct firing schedule for a
mixed stream.

**Dependencies:** Phases 3, 4. **Known risks:** timing error from Python,
serial latency and inference jitter is roughly ±150–250 ms — ±2.5 cm at
10 cm/s, ±12 cm at 50 cm/s. Run the belt slow and make bin mouths wide.

---

### Phase 7 — Arduino and servo integration

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
