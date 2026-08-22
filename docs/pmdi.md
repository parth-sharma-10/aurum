# PMDI — the Precious Metal Density Index

## What PMDI is

PMDI is Aurum's **precious-metal economic signal**: what the cited evidence
implies a component's contained precious metal is worth. It is one input to the
sorting decision. It is not the decision.

```
material evidence  ->  PMDI / precious metrics  ->  valuation  ->  decision policy  ->  A/B/C
```

Everything on the left of that arrow is evidence and arithmetic. Everything on
the right is policy. `app/valuation/` contains no grading logic, and a test
fails if the word "grade" ever appears in its output.

## Formula

From the Aurum concept document, §4 — the project's authoritative definition:

```
PMDI = (Σ (C_type × Y_estimated)) × P_spot
```

| Symbol | Meaning | Where it comes from in Aurum |
|---|---|---|
| `C_type` | Count of a component type | The detector. For one conveyor item, `C_type = 1` |
| `Y_estimated` | Estimated precious-metal yield per component | `configs/material_reference.yaml`, **as contained composition** — see below |
| `P_spot` | Spot price of the relevant metal | A `PriceProvider`. **None is configured.** |

### Two quantities, not one

The formula's units are `count × grams × currency/gram` = **currency**. Nothing
is divided by mass, so despite the name, the concept document's PMDI is a
**monetary value, not a density**. Aurum therefore reports both quantities and
never conflates them:

| Field | What it is | Needs a price? |
|---|---|---|
| `pmdi_value` | The concept document's figure. Currency. | **Yes** |
| `precious_mass_fraction_ppm` | The true density: precious-metal mass ÷ component mass, in ppm | **No** |

The second is why the conveyor keeps sorting when no price is available. It is
the primary A/B discriminator; `pmdi_value` is a secondary check applied only
when a fresh price exists.

## Inputs and units

Unit errors here are silent and large — a troy ounce is 31.1 g, so a per-ounce
price applied to grams is wrong by 31× and still looks plausible. Every
conversion is explicit.

| Evidence shape | Stored as | Conversion | Needs mass? |
|---|---|---|---|
| Per piece | mg/piece | `count × mg ÷ 1000` → g | No |
| Concentration | mg/kg | `mg/kg × (mass_g ÷ 1000) ÷ 1000` → g | **Yes, measured** |
| Component mass | g | — | — |
| Price | currency per g, kg or ozt | ÷ `{g: 1, kg: 1000, ozt: 31.1034768}` → currency/g | — |

A troy ounce is **exactly** 31.1034768 g by definition; that and the SI prefixes
are the only numeric constants in `app/valuation/prices.py`. A price unit with
no conversion factor raises rather than being guessed.

**Worked example — one CPU:**

```
CPU-AU-001:  4.71 mg Au per piece   (Firsching et al. 2024, ICP-OES)
1 piece   ->  4.71 mg  =  0.00471 g Au
measured mass 42.7 g
fraction  ->  0.00471 / 42.7 x 1e6  =  110.3 ppm precious
pmdi_value -> UNAVAILABLE (no price provider configured)
```

## Evidence

Every figure resolves to a cited record in `configs/material_reference.yaml`,
whose `source` resolves to a paper in `docs/sources/material_sources.yaml`. 22
records, 6 papers. Nothing enters a calculation without an evidence id, and the
ids travel all the way to the API response.

`app/valuation` performs **no unit arithmetic of its own** — `app/materials.py`
already does it, fails closed, and is separately tested. Redoing it would be a
second place for it to be wrong.

## Contained composition is not recovery yield

The formula calls `Y_estimated` a *yield*. **Aurum does not have yields.**

| | Meaning | In Aurum |
|---|---|---|
| Contained composition | How much metal is in the component | **What we have.** 22 cited records |
| Recovery yield | How much a process actually extracts | **Not applicable.** Refused |

The distinction is not pedantic — a recovery figure is always smaller than a
contained figure, so using one for the other overstates every result. The only
cited recovery figures (LIN2023, 88.7 % Au) were measured on a *decopperized,
stamp-sheared gold-finger feed* — a processed intermediate that no detected
component resembles. `app/materials.py` refuses to apply them, and every amount
this subsystem produces is labelled `basis: contained`.

## Pricing

**No live provider is approved for this project.** `configs/pricing.yaml` ships
`provider: unavailable`, which is a decision, not a placeholder.

| Provider | Status it produces | Purpose |
|---|---|---|
| `unavailable` | `UNAVAILABLE` | **The default.** Prices nothing, names the setting that would change it |
| `static` | `TEST` | Pinned prices from `configs/price_reference.yaml`, for testing the pipeline |

A `static` price is labelled **TEST** and is never reported as LIVE, SPOT, or a
current market price, whatever the file says.

### Price status model

| Status | Meaning |
|---|---|
| `LIVE` | From an approved market source. **No provider produces this today.** |
| `TEST` | Deterministic fixture data. Not a market quote. |
| `SIMULATED` | Generated for a demo, labelled as such. |
| `STALE` | Real but older than `pricing.max_age_seconds`. Carries its number; the caller decides. |
| `UNAVAILABLE` | No price. Never a zero. |
| `ERROR` | The source produced something unusable, e.g. an unconvertible unit. |

Staleness is **reported, not enforced** — whether a stale price may drive a
grade is policy, and policy lives in the decision engine.

### When a price is missing

`grading.policy.price_unavailable_policy` decides:

- `mass_fraction_only` (**default**) — grade on the price-independent fraction. Sorting continues.
- `route_to_c` — refuse to grade without a price.

A price outage never stops the conveyor and never produces a fabricated number.

### Partial price sets are refused

If gold is priced but silver and palladium are not, a PCB's `pmdi_value` is
`None`, not a gold-only subtotal. A total that quietly drops a metal still
reads as a total.

## PMDI is not A/B/C

**`PMDI ≠ bin`.** PMDI says what the evidence implies economically; the grading
policy says what the machine does about it. Two consequences worth stating:

On the current cited data a **PCB outranks a CPU on every precious-metal
metric** — roughly 2 200 ppm against 110 ppm — because FIRSCHING2024 reports
gold per CPU package and gives no silver or palladium, while the PCB profile
carries all three. That is a gap in the database, not a statement about physics,
and the ranking is left exactly as the evidence says it is.

Bin A therefore also consults `grading.bin_a.preferred_classes`, an
**engineering sorting policy, not a scientific PMDI threshold**. Listing CPU
there is not a claim that a CPU contains more precious metal than a PCB. Set
`grading.policy.class_aware: false` for the purely evidence-driven behaviour.

## Limitations

Stated plainly, because each one changes what a result means.

**RAM has no cited composition of any kind.** Eight metals missing. The closest
study (CHARLES2017, AAS on DRAM modules 1991–2008) was unreachable in full text,
so no number was taken from it. Any RAM detection returns `available: false`
and routes to C. A regression test fails if RAM ever acquires a value.

**CPU evidence is gold only.** No cited silver or palladium exists for processor
packages. FIRSCHING2024 reports means with no sample count and no standard
deviation, so every figure derived from it is capped at `medium` confidence.

**PCB needs a *measured* mass.** Its evidence is concentration-based, so without
a real load-cell reading the estimate is refused. A simulated mass is refused
too — deliberately, since a concentration times an invented mass produces an
invented quantity.

**No approved live price provider.** `pmdi_value` is permanently `UNAVAILABLE`
in production until one is configured.

**Grading thresholds are engineering approximations.** The A/B/C numbers in
`configs/grading.yaml` are configurable prototype starting points, labelled as
such, and are **not presented as universally validated scientific cutoffs**.

**Simulation is always labelled.** A simulated mass propagates
`weight_status: SIMULATED` and `overall_status: SIMULATED` through every layer
to the API and the dashboard.

## Status propagation

The output reports the **worst** thing true of any input, so nothing hides:

```
UNAVAILABLE  >  SIMULATED  >  STALE  >  ESTIMATED
```

A simulated mass with a stale price reports `SIMULATED`, not `ESTIMATED`.

## Where the code lives

| File | Role |
|---|---|
| `app/valuation/prices.py` | Providers, status model, staleness, unit conversion |
| `app/valuation/pmdi.py` | The PMDI calculation and precious/base/other split |
| `app/valuation/valuation.py` | PMDI plus the separate base-metal signal, packaged for audit |
| `configs/pricing.yaml` | Provider selection and staleness limit |
| `configs/price_reference.yaml` | Pinned prices — ships empty and disabled |

## API

| Endpoint | What it gives |
|---|---|
| `GET /prices` | Provider, staleness limit, and each metal's price or explicit refusal |
| `GET /batches/{id}/valuation` | `pmdi` block and `item_valuation`, both with evidence ids and statuses |

`GET /stats` deliberately carries **no** money or yield figure. It is the
aggregate endpoint a dashboard renders without any provenance attached, and a
number there would be quoted stripped of the evidence and simulation flags that
qualify it. That contract is enforced by a test.
