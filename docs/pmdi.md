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
pmdi_value ->  0.00471 g x 16,062.00 INR/g  =  INR 75.65   (REFERENCE price)
```

**Worked example — one RAM module**, the case where per-piece evidence needs no
scale at all:

```
RAM-AU-001 .. RAM-CU-001  (Charles et al. 2017, Table 2, DIMMs 4-15, n=12, AAS)
  Au   18.0 mg -> 0.0180 g  x  16,062.00 INR/g  =  INR 289.12
  Ag   28.4 mg -> 0.0284 g  x     246.63 INR/g  =  INR   7.00
  Pd    1.2 mg -> 0.0012 g  x   4,095.256 INR/g =  INR   4.91
  Cu    3.4 g              x      1.3858 INR/g  =  INR   4.71
                              CONTAINED VALUE    =  INR 305.75
```

No mass appears anywhere in that calculation. That is the whole reason the
per-module basis was chosen: the detector counts modules, while the load cell
weighs the entire object on the pan. A module on an 842 g motherboard is worth
exactly what a loose module is worth, and **the board's mass is never treated as
the memory's mass**.

## Evidence

Every figure resolves to a cited record in `configs/material_reference.yaml`,
whose `source` resolves to a paper in `docs/sources/material_sources.yaml`. 26
records, 6 papers. Nothing enters a calculation without an evidence id, and the
ids travel all the way to the API response.

`app/valuation` performs **no unit arithmetic of its own** — `app/materials.py`
already does it, fails closed, and is separately tested. Redoing it would be a
second place for it to be wrong.

## Contained composition is not recovery yield

The formula calls `Y_estimated` a *yield*. **Aurum does not have yields.**

| | Meaning | In Aurum |
|---|---|---|
| Contained composition | How much metal is in the component | **What we have.** 26 cited records |
| Recovery yield | How much a process actually extracts | **Not applicable.** Refused |

The distinction is not pedantic — a recovery figure is always smaller than a
contained figure, so using one for the other overstates every result. The only
cited recovery figures (LIN2023, 88.7 % Au) were measured on a *decopperized,
stamp-sheared gold-finger feed* — a processed intermediate that no detected
component resembles. `app/materials.py` refuses to apply them, and every amount
this subsystem produces is labelled `basis: contained`.

## Pricing

**No live market feed is configured**, because none is available without a
licence or an API key. `configs/pricing.yaml` ships `provider: reference`: a
dated snapshot of real published prices, labelled as such everywhere it appears.

| Provider | Status it produces | Purpose |
|---|---|---|
| `reference` | `REFERENCE` | **The default.** A dated snapshot of published prices from `configs/price_reference.yaml` |
| `unavailable` | `UNAVAILABLE` | Prices nothing, names the setting that would change it. Set this to run on grams and ppm alone |
| `static` | `TEST` | Pinned fixture prices, for testing the pipeline |

`FallbackProvider(primary, fallback)` composes two of them, which is how a live
feed is added later: LIVE first, REFERENCE when it cannot answer. Whichever
provider actually answered is on the quote, so a fallback can never be mistaken
for a live price.

### Units and currency — the two 30x mistakes

```
price_per_unit / GRAMS_PER_UNIT[unit]   ->  currency per gram
              x fx[currency].rate       ->  reporting currency per gram
```

A troy ounce is **31.1034768 g**. The avoirdupois ounce (28.3495 g) appears
nowhere in this project and must never be used for metal — it is a 9 % error
that produces an entirely plausible-looking number. A unit with no conversion
factor raises rather than being guessed, and a currency with no rate on file is
an error rather than an assumed parity of 1.

### Price status model

| Status | Meaning |
|---|---|
| `LIVE` | From a live market feed. **No provider produces this today.** |
| `REFERENCE` | A real published price, with a real date, used deliberately after it. **The default.** Never presented as current — and never degraded by age either, because a snapshot is not a feed that went quiet. |
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
there is not a claim that a CPU contains more precious metal than a PCB.

### What `class_aware` actually switches

It selects which gate Bin A uses. Both gates are configuration; neither edits
the evidence.

| Mode | Bin A gate | CPU (110 ppm) | PCB (2 200 ppm) |
|---|---|---|---|
| `class_aware: true` (default) | membership of `preferred_classes` | **A** — `A_PREFERRED_CLASS` | **B** — `B_PRECIOUS_FRACTION` |
| `class_aware: false` | fraction and value thresholds alone | **B** — `B_PRECIOUS_FRACTION` | **A** — `A_PRECIOUS_FRACTION` |

The second row is the purely evidence-driven ordering, and it is one setting
away at all times. The reason code on every decision says which gate fired.

## The decision ladder

Walked in order, never short-circuited. A strong signal does not excuse a
failed safety check: a preferred class cannot rescue an unmeasured weight, and
a high fraction cannot rescue a weak detection.

```
1. detection valid?          no -> C_UNKNOWN_CLASS / C_INVALID_DATA
2. cited composition?        no -> C_UNSUPPORTED_MATERIAL / C_MISSING_EVIDENCE
3. required measurement?     no -> C_UNMEASURED_WEIGHT
4. data and price valid?     no -> C_MISSING_EVIDENCE / C_PRICE_UNAVAILABLE
5. Bin A policy              yes -> A_PREFERRED_CLASS | A_PRECIOUS_FRACTION | A_PMDI_VALUE
6. Bin B policy              yes -> B_PRECIOUS_FRACTION | B_BASE_METAL_VALUE | B_SUPPORTED_RECOVERABLE
7. otherwise                     -> C_LOW_CONFIDENCE / C_BELOW_THRESHOLD
```

Step 3 is derived from the database, not from a hardcoded list: a class needs a
measured mass when its default subtype's composition is cited as a
concentration. Add per-piece evidence for a class and it stops needing one.

### Reason codes

| Code | Meaning |
|---|---|
| `A_PREFERRED_CLASS` | Configured premium class, confidence cleared. Engineering policy. |
| `A_PRECIOUS_FRACTION` | Fraction met the premium ppm threshold. |
| `A_PMDI_VALUE` | PMDI value met the premium value threshold. |
| `B_PRECIOUS_FRACTION` | Fraction met the recoverable ppm threshold. |
| `B_BASE_METAL_VALUE` | Cited base-metal content is worth recovering. |
| `B_SUPPORTED_RECOVERABLE` | Per-piece evidence, no mass needed, no density formable. |
| `C_UNKNOWN_CLASS` | No cited material profile for this class. |
| `C_UNSUPPORTED_MATERIAL` | Class known, no cited composition. **RAM today.** |
| `C_MISSING_EVIDENCE` | The material estimate is unavailable. |
| `C_UNMEASURED_WEIGHT` | Concentration evidence without a measured mass. |
| `C_LOW_CONFIDENCE` | Below the minimum for any routed bin. |
| `C_PRICE_UNAVAILABLE` | `route_to_c` policy with no current price. |
| `C_INVALID_DATA` | Confidence missing, non-numeric or outside 0..1. |
| `C_BELOW_THRESHOLD` | Fraction below the recoverable threshold, no base-metal value. |

### Fail-closed, by physical design

Bin C has **no servo**. An item nobody routes reaches the end of the belt and
falls into it, so C is reached by the machine *doing nothing*. Every refusal
above is therefore also the safe hardware state, not just the safe software
state.

C does not mean worthless. It means Aurum cannot justify routing the item into
A or B.

### Price unavailable

`grading.policy.price_unavailable_policy` decides, and both modes are tested:

| Mode | Behaviour with no price |
|---|---|
| `mass_fraction_only` (default) | Decide on the price-independent fraction. **The conveyor keeps sorting.** |
| `route_to_c` | Refuse to grade. Everything falls to C. |

A stale price does not count as current for `route_to_c`.

## Limitations

Stated plainly, because each one changes what a result means.

**RAM rests on one study of twelve modules, none later than 2008.** CHARLES2017
(AAS on DRAM modules 1991–2008) is the only whole-module characterisation found,
and it contains no DDR4 or DDR5. The DDR2–DDR5 subtypes are defined and empty; a
detection resolves to an unspecified module, because a generation cannot be read
from an image. Palladium is likely **overstated** for a modern module and copper
**understated**, and both directions are recorded in the evidence notes rather
than corrected by guesswork. Nickel, tin and aluminium were not analysed at all —
that is ignorance, unlike platinum, which the authors looked for and did not
find. A test fails if RAM ever acquires a figure without a citation.

**CPU evidence is gold only.** No cited silver or palladium exists for processor
packages. FIRSCHING2024 reports means with no sample count and no standard
deviation, so every figure derived from it is capped at `medium` confidence.

**PCB needs a *measured* mass.** Its evidence is concentration-based, so without
a real load-cell reading the estimate is refused. A simulated mass is refused
too — deliberately, since a concentration times an invented mass produces an
invented quantity.

**Prices are a dated reference snapshot, not a market feed.** `pmdi_value` is
produced, and every quote behind it is stamped `REFERENCE` with its source and
date. Nothing in the system claims a live price. The palladium quote has the
weakest provenance of the four — a dealer spot bid rather than a benchmark
print, because LBMA's tabulated data is licence-gated.

**Contained is not recoverable.** No cited recovery factor was measured on a
component as Aurum detects it, so `recoverable_value` is a refusal carrying that
reason — never a number, and never a zero. The >95 % gold recovery figure that
appears in the literature is a secondary citation about WEEE processing in
general, and applying it would convert a measured quantity into a process
assumption while leaving it looking like a measurement.

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
| `app/valuation/prices.py` | Providers, status model, staleness, unit **and currency** conversion |
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
