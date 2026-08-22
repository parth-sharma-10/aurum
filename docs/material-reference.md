# Material reference

The authoritative description of Aurum's material layer: what it claims, what it
refuses to claim, and how to get from a number Aurum prints back to the page of
the paper it came from.

---

## 1. Why this exists

Aurum's vision model answers one question: *what components are in front of the
camera, and how many?* It cannot answer *what are they made of*. RGB imagery has
no access to composition — a gold-plated connector and a tin-plated one of the
same shape are the same pixels.

But component identity is not worthless for material estimation. If you know a
box contains four connectors, and the literature reports how much gold a
connector carries, you can produce an **estimate**. That estimate is only as good
as the reference data, which is why this layer exists as a separate, citable
thing rather than as constants inside the detector.

The rule the whole layer is built around:

> A plausible-looking number with nothing behind it is worse than no number,
> because it gets quoted and outlives the conversation that qualified it.

So every figure names a paper, and where no paper was found, the figure is
absent and the estimate refuses to compute.

## 2. Components covered

Aurum detects four classes. Their coverage is uneven, and the unevenness is the
honest result rather than something smoothed over:

| Class | Composition data | Usable without a scale? | Status |
| --- | --- | --- | --- |
| **CPU** | Au only | **yes** — per-piece figure | usable |
| **Connector** | Au (+ Ag, Cu, Ni, Al for gold fingers) | **yes** — per-piece figure | usable |
| **PCB** | Au, Ag, Pd, Cu, Ni, Sn, Al | no — concentration, needs a measured mass | conditional |
| **RAM** | **none** | — | **unavailable** |

Subtypes exist only where a source actually distinguished them:

```
PCB          generic_wpcb · desktop_motherboard_early_generation · server_motherboard
CPU          bga_pga_package
Connector    general_connector · high_grade_connector · gold_fingers
RAM          dimm_module (mass only)
```

There is no `laptop motherboard`, no `mobile PCB`, no `ceramic CPU`, no `LGA` or
`PGA` split, because no source read during this work measured them separately.
Inventing a subtype to look thorough would put a fabricated distinction in front
of a reviewer.

## 3. Metals covered

| Metal | PCB | CPU | Connector | RAM |
| --- | :-: | :-: | :-: | :-: |
| Au gold | ✅ | ✅ | ✅ | ❌ |
| Ag silver | ✅ | ❌ | ✅ (gold fingers) | ❌ |
| Pd palladium | ✅ (weak) | ❌ | ❌ | ❌ |
| Pt platinum | ❌ | ❌ | ❌ | ❌ |
| Cu copper | ✅ | ❌ | ✅ (gold fingers) | ❌ |
| Ni nickel | ✅ | ❌ | ✅ (gold fingers) | ❌ |
| Sn tin | ✅ | ❌ | ❌ | ❌ |
| Al aluminium | ✅ | ❌ | ✅ (gold fingers) | ❌ |

**Platinum appears nowhere.** Neither source read reports platinum in boards,
processors or connectors; it is associated with hard-disk platters and catalysts
rather than with these components. A zero would have been an invention, so the
metal is simply absent.

## 4. Source-selection methodology

Sources were ranked by evidence quality:

| Tier | What qualifies | Used for |
| --- | --- | --- |
| **HIGH** | Peer-reviewed experimental study; direct chemical analysis (ICP-MS, ICP-OES, XRF, AAS, fire assay); institutional material characterization | headline figures |
| **MEDIUM** | Peer-reviewed review aggregating primary studies; credible technical report; measurement reported without uncertainty or sample size | supporting figures, ranges |
| **LOW** | Secondary websites, commercial recycling claims, unsourced databases, community estimates | **not used for any value** |

Every source that supplies a number was opened and its tables read. Search-result
snippets were never used as evidence — an important discipline, because during
this work a snippet-level summary of one review reported PCB gold and silver
transposed relative to the canonical figures, which would have overstated gold
four-fold had it been trusted.

### Sources rejected, and why

| Rejected | Why |
| --- | --- |
| ScrapMonster, Alibaba buying guides, Phoenix Refining, Accio, and similar | Commercial scrap-trade pages. No methodology, no sample description, no analytical technique. LOW tier — discovery aids only. |
| Ormuž, Žmak & Ćurković (2026), *Materials* 19(3):538 | A review, and the composition figures as retrieved appeared internally inconsistent (Au/Ag labels transposed against the canonical Cu ≈ 20 % / Ag ≈ 1000 / Au ≈ 250 / Pd ≈ 110 ppm set). Rejected rather than propagated. |
| Ueberschaar & Rotter (2015), HDD rare-earth study | Wrong subject — hard disk drives and rare earths, not Aurum's classes. |
| Oliveira et al. (2022) — the paper's *own* ICP-OES numbers | Measured on separated **size fractions** after gravity/electrostatic concentration (Cu 56–80 %). These are process concentrates, not board composition; using them as board composition would overstate copper roughly three-fold. Only the paper's whole-board aggregation table is used. |
| Zinkowska et al. (2024) — leachate concentrations | Reported in mg/L in an HCl/H₂O₂ leachate. Converting to solid composition needs the leach volume and an assumption of complete leaching; neither is established. Only the module **mass** is taken. |

## 5. Measurement units

| Quantity | Unit stored | Meaning |
| --- | --- | --- |
| `concentration` | mg/kg | mass of metal per kg of material |
| `per_piece` | mg | mass of metal in one component |
| `mass` | g | mass of one component |

Identities used, and re-derived by tests rather than trusted:

```
1 mg/kg  =  1 ppm  =  1 g/tonne
1 %      =  10 000 mg/kg
1 kg     =  1 000 g
```

Every record keeps `original_value` and `original_unit` exactly as published,
plus a `conversion` string stating what was done. `tests/test_materials.py`
recomputes every percentage and ppm conversion from the original figure, so a
transcription slip fails CI rather than shipping.

**Derived ranges are marked as derived.** Where a source reports mean ± standard
deviation, the stored `minimum`/`maximum` are mean ∓ 1 SD with the lower bound
clamped at zero (a negative concentration is not physical). That is a derived
interval, not a measured min and max, and the record says so.

## 6. Composition versus recovery

These are different claims and the database keeps them in different sections.

```
composition   how much metal is PRESENT      ← measured by a paper
recovery      how much a process GOT OUT     ← measured by a paper, separately
```

A paper reporting `6434 mg/kg Au` in gold fingers does **not** say 6434 mg/kg is
recoverable. Aurum will never turn the first number into the second.

**The shipped database applies no recovery factor at all.** Three real,
cited recovery figures are on file, all from Lin et al. (2023), and all three are
marked `applies_to_detection: false`. The reason is in the measurement context:
they were measured on a feed that had been stamp-sheared off RAM modules and then
chemically decopperized. A connector that Aurum detected on a bench has had
neither step done to it. Applying the 88.7 % figure to it would silently assume an
entire pre-treatment chain that Aurum has not performed and cannot observe.

Those same three figures are the best argument for the refusal: on one identical
feed and reagent system, recovery moves from **12.7 %** without pre-treatment to
**88.7 %** with a copper pre-leach, to **98 %** under optimisation. Any single
"industry standard recovery rate" applied to a camera detection would be picking
one point off that curve for no stated reason.

So `recovery.available` is `false`, with a reason — and the factors stay visible
in the record for audit. Refusing to apply a figure is not the same as hiding it.

## 7. Evidence confidence

| Level | Meaning here |
| --- | --- |
| `high` | Direct chemical analysis of the material in question, method and sample described |
| `medium` | A measurement reported without uncertainty or sample count, or an aggregation across studies |
| `low` | Present for context only; not used for a headline figure |

An estimate reports the **weakest** confidence among the records that fed it —
one medium input makes the whole estimate medium. A test enforces that an
`aggregated` record can never be labelled `high`.

## 8. Known uncertainty

Read these before quoting anything the layer produces.

- **The strongest figures are the per-piece ones, and they are still `medium`.**
  Firsching et al. report means with **no standard deviation and no disclosed
  sample count**. `4.71 mg` is a real ICP-OES result; it is not a figure with an
  error bar.
- **`PCB-PD-001` is the weakest record in the database.** Palladium at
  500 ± 1100 mg/kg — a standard deviation more than double the mean. It is
  included only so palladium is not silently absent, and it is deliberately not
  used for any headline number.
- **`PCB-AU-001` (142 mg/kg) describes 1990s boards.** Bizzo et al. sampled XT,
  486 and Pentium hardware. The same paper notes PCB gold above 1000 ppm belongs
  to 1993–1995 studies while most later values fall below 100 ppm. This is not a
  figure for a modern board.
- **`CONN-AU-003` (6434 mg/kg) is a contact strip, not a connector.** It
  describes stamp-sheared gold-finger edge material. Its mass is not reported, so
  it cannot be converted into milligrams per detected component.
- **`PCB-MASS-001` is n = 1 and a server board.** Among the largest boards there
  are. It is recorded for context and deliberately **not** used as a default mass
  for a detected PCB.
- **Aurum cannot detect any subtype.** The default subtype is a stated
  assumption, not an observation. A `Connector` detection does not establish gold
  plating.

## 9. Component variation

Material content varies enormously, and the database is built to show that rather
than hide it behind a single number.

- A **ceramic processor** from the 1990s and a modern LGA package are not the
  same material. Aurum has one figure covering BGA/PGA packages generally, and
  the source class is a *package geometry*, not "CPU" — a chipset or GPU in a BGA
  package falls in the same class.
- A **gold-plated connector** and a tin-plated connector of identical shape are
  different objects with different value, and identical to a camera. This is why
  an unqualified `Connector` detection carries a typical value of 0.914 mg Au and
  an upper bound of 2.35 mg (the high-grade sub-class) rather than one number.
- A **PCB** may be a desktop motherboard, a laptop board, an expansion card, a
  telecom board or a mobile board. The generic figures carry standard deviations
  as large as their means, which is the correct picture rather than a defect.

Where a bound cannot be established for every contributing component, the
aggregate bound is `null`, never a partial sum — a "maximum" computed from only
some of the components can come out *below* the typical value, which is how a
range stops being a range and starts being a wrong number.

## 10. Evidence table

Every row has a real source. `Original` is the figure exactly as published.

| Evidence ID | Component | Subtype | Metal | Value | Unit | Original | Source | Evidence | Conf. |
| --- | --- | --- | --- | ---: | --- | --- | --- | --- | --- |
| `PCB-AU-001` | PCB | desktop_motherboard_early_generation | Au | 142 | mg/kg | 142 ppm | Bizzo et al. 2014 | measured | high |
| `PCB-AG-001` | PCB | desktop_motherboard_early_generation | Ag | 317 | mg/kg | 317 ppm | Bizzo et al. 2014 | measured | high |
| `PCB-CU-001` | PCB | desktop_motherboard_early_generation | Cu | 142000 | mg/kg | 14.2 % | Bizzo et al. 2014 | measured | high |
| `PCB-NI-001` | PCB | desktop_motherboard_early_generation | Ni | 4100 | mg/kg | 0.41 % | Bizzo et al. 2014 | measured | high |
| `PCB-SN-001` | PCB | desktop_motherboard_early_generation | Sn | 47900 | mg/kg | 4.79 % | Bizzo et al. 2014 | measured | high |
| `PCB-AU-002` | PCB | generic_wpcb | Au | 400 | mg/kg | 0.04 % | Oliveira et al. 2022 | aggregated | medium |
| `PCB-AG-002` | PCB | generic_wpcb | Ag | 1300 | mg/kg | 0.13 % | Oliveira et al. 2022 | aggregated | medium |
| `PCB-PD-001` | PCB | generic_wpcb | Pd | 500 | mg/kg | 0.05 % | Oliveira et al. 2022 | aggregated | medium |
| `PCB-CU-002` | PCB | generic_wpcb | Cu | 214400 | mg/kg | 21.44 % | Oliveira et al. 2022 | aggregated | medium |
| `PCB-NI-002` | PCB | generic_wpcb | Ni | 10300 | mg/kg | 1.03 % | Oliveira et al. 2022 | aggregated | medium |
| `PCB-SN-002` | PCB | generic_wpcb | Sn | 31400 | mg/kg | 3.14 % | Oliveira et al. 2022 | aggregated | medium |
| `PCB-AL-001` | PCB | generic_wpcb | Al | 30200 | mg/kg | 3.02 % | Oliveira et al. 2022 | aggregated | medium |
| `CPU-AU-001` | CPU | bga_pga_package | Au | 4.71 | mg | 4.71 mg per piece | Firsching et al. 2024 | measured | medium |
| `CONN-AU-001` | Connector | general_connector | Au | 0.914 | mg | 0.914 mg per piece | Firsching et al. 2024 | measured | medium |
| `CONN-AU-002` | Connector | high_grade_connector | Au | 2.35 | mg | 2.35 mg per piece | Firsching et al. 2024 | measured | medium |
| `CONN-AU-003` | Connector | gold_fingers | Au | 6434 | mg/kg | 6434 ppm | Lin et al. 2023 | measured | high |
| `CONN-AG-001` | Connector | gold_fingers | Ag | 93 | mg/kg | 93 ppm | Lin et al. 2023 | measured | high |
| `CONN-CU-001` | Connector | gold_fingers | Cu | 290447 | mg/kg | 290447 ppm | Lin et al. 2023 | measured | high |
| `CONN-NI-001` | Connector | gold_fingers | Ni | 18092 | mg/kg | 18092 ppm | Lin et al. 2023 | measured | high |
| `CONN-AL-001` | Connector | gold_fingers | Al | 26331 | mg/kg | 26331 ppm | Lin et al. 2023 | measured | high |
| `RAM-MASS-001` | RAM | dimm_module | — | 7.804 | g | 7.804 g | Zinkowska et al. 2024 | measured | medium |
| `PCB-MASS-001` | PCB | server_motherboard | — | 1800 | g | 1.8 kg | Oliveira et al. 2022 | measured | low |

### Recovery factors — on file, none applied

| Factor ID | Component | Subtype | Metal | Rate | Feed | Scale | Applied? | Source |
| --- | --- | --- | --- | ---: | --- | --- | --- | --- |
| `CONN-AU-REC-001` | Connector | gold_fingers | Au | 88.7 % | Decopperized gold fingers (after a copper pre-leach) | laboratory | **no** | Lin et al. 2023 |
| `CONN-AU-REC-002` | Connector | gold_fingers | Au | 12.7 % | As-stamped gold fingers, no pre-treatment | laboratory | **no** | Lin et al. 2023 |
| `CONN-AU-REC-003` | Connector | gold_fingers | Au | 98 % | Gold fingers under optimized leach conditions | laboratory | **no** | Lin et al. 2023 |

## 11. Bibliography, and how an ID maps to a record

The canonical machine-readable bibliography is
[`docs/sources/material_sources.yaml`](sources/material_sources.yaml). The trail
is:

```
Aurum estimate  →  evidence id  →  configs/material_reference.yaml
                →  source id    →  docs/sources/material_sources.yaml
                →  DOI / URL    →  the paper  →  the original table
```

Worked example, end to end:

```
Aurum reports    Connector x3 → Au typical 0.002742 g
evidence         CONN-AU-001, CONN-AU-002
record           value: 0.914, unit: mg, quantity: per_piece
source           FIRSCHING2024
paper            Firsching, Ottenweller, Leisner & Rüger (2024),
                 Waste Management & Research 42(9)
DOI              10.1177/0734242X241257084
original text    "just one general connector class (C) with 0.914 mg of gold
                  per piece and the sub-class 'high-grade connectors' (Chg)
                  with 2.35 mg"
arithmetic       3 × 0.914 mg = 2.742 mg = 0.002742 g   (typical)
                 3 × 2.35  mg = 7.05  mg = 0.00705  g   (upper bound)
```

### Bizzo, Figueiredo & de Andrade (2014)

**Characterization of Printed Circuit Boards for Metal and Energy Recovery after
Milling and Mechanical Separation**
*Materials* 7(6):4555–4566 · DOI [10.3390/ma7064555](https://doi.org/10.3390/ma7064555)

Used by Aurum for: PCB gold, silver, copper, nickel and tin concentration
(`PCB-AU-001`, `PCB-AG-001`, `PCB-CU-001`, `PCB-NI-001`, `PCB-SN-001`).

Evidence type: direct experimental characterization — ~12 kg of PCBs from
discarded desktop computers (XT, 486, Pentium), milled to 9 mm, aqua regia
digestion, AAS and ICP.

### Lin, Ali & Werner (2023)

**Investigation of the Bimodal Leaching Response of RAM Chip Gold Fingers in
Ammonia Thiosulfate Solution**
*Materials* 16(14):4940 · DOI [10.3390/ma16144940](https://doi.org/10.3390/ma16144940)

Used by Aurum for: gold-finger gold, silver, copper, nickel and aluminium
concentration (`CONN-AU-003`, `CONN-AG-001`, `CONN-CU-001`, `CONN-NI-001`,
`CONN-AL-001`), and for the three recorded — but unapplied — gold recovery rates
(`CONN-AU-REC-001/002/003`).

Evidence type: direct chemical assay by roasting and acid digestion, 0.5 g per
sample, on gold-finger edges stamp-sheared from waste RAM modules.

### Firsching, Ottenweller, Leisner & Rüger (2024)

**X-ray transmission imaging of waste printed circuit boards for value estimation
in recycling using machine learning**
*Waste Management & Research* 42(9) · DOI [10.1177/0734242X241257084](https://doi.org/10.1177/0734242X241257084)

Used by Aurum for: CPU gold per piece (`CPU-AU-001`), connector gold per piece
(`CONN-AU-001`), high-grade connector gold per piece (`CONN-AU-002`).

Evidence type: ICP-OES on components dismantled from 104 waste PCBs from PCs,
servers and mobile phones. **The only source found that reports gold as mass per
component piece** — the unit Aurum's counts actually need. Reported as means with
no standard deviation and no disclosed sample count, hence `medium` confidence.

### Oliveira, Bellopede, Tori, Zanetti & Marini (2022)

**Gravity and Electrostatic Separation for Recovering Metals from Obsolete
Printed Circuit Board**
*Materials* 15(5):1874 · DOI [10.3390/ma15051874](https://doi.org/10.3390/ma15051874)

Used by Aurum for: generic waste PCB composition ranges (`PCB-AU-002`,
`PCB-AG-002`, `PCB-PD-001`, `PCB-CU-002`, `PCB-NI-002`, `PCB-SN-002`,
`PCB-AL-001`) and one reference board mass (`PCB-MASS-001`).

Evidence type: the authors' weighted average with standard deviation compiled
across prior studies — an aggregation, hence `medium`. The paper's own ICP-OES
size-fraction measurements are **not** used; see §4.

### Zinkowska, Hubicki & Wójcik (2024)

**Impregnated Polymeric Sorbent for the Removal of Noble Metal Ions from Model
Chloride Solutions and the RAM Module**
*Materials* 17(6):1234 · DOI [10.3390/ma17061234](https://doi.org/10.3390/ma17061234)

Used by Aurum for: the mass of one spent RAM module, 7.8040 g (`RAM-MASS-001`).

Evidence type: a single weighed module. The paper's ICP-OES figures are leachate
concentrations and are **not** used as composition; see §4.

### Charles, Douglas, Hallin, Matthews & Liversage (2017)

**An investigation of trends in precious metal and copper content of RAM modules
in WEEE: Implications for long term recycling potential**
*Waste Management* 60:505–520 · DOI [10.1016/j.wasman.2016.11.018](https://doi.org/10.1016/j.wasman.2016.11.018)

Used by Aurum for: **nothing numeric.** This is the most directly relevant study
found for whole RAM modules — Au, Ag, Pd and Cu in DRAM modules placed on the
market 1991–2008, by AAS after comminution and acid digestion. It is nominally
CC-BY, but every route to the full text was blocked during this work, so **its
tables were never read and no number is taken from it.** Its abstract was
verified and supports only qualitative claims: stable gold and silver over time,
an 80 % fall in palladium across 1991–2008, and a 0.23 g/module/year rise in
copper. It is cited here as evidence that RAM composition **varies by
generation**, and as the reason RAM has no figure.

## 12. Limitations

**Where the evidence is strong**

- Gold-finger composition (`CONN-AU-003` and siblings): direct assay, method and
  sample mass stated, five metals from one coherent measurement.
- Older desktop PCB composition (`PCB-*-001`): direct measurement on a described
  ~12 kg sample.

**Where the evidence is weak**

- Every per-piece figure is a mean with no reported spread or sample count.
- Generic PCB figures carry standard deviations as large as, or larger than,
  their means.
- Palladium rests on a single aggregated record whose SD is 2× its mean.
- Two mass records are each n = 1.

**Where component variation prevents a precise estimate**

- Aurum cannot see subtype. Connector grade, board type and processor generation
  are all invisible to it, and each moves the answer by more than the difference
  between the metals being estimated.
- The CPU figure comes from a package-geometry class (BGA/PGA), which is not
  exactly Aurum's CPU class.

**Where recovery data is unavailable**

- Everywhere. No cited recovery factor matches a whole detected component. The
  three factors on file were measured on a liberated, decopperized feed.

**Where Aurum still requires a physical assay**

- Any claim about an **individual** object. The layer produces a population-level
  reference estimate; it cannot tell you what the connector in your hand
  contains. Establishing that requires XRF or destructive assay, neither of which
  is part of this system.

**The single largest gap**

- **RAM has no composition data at all**, despite being one of the four detected
  classes and despite gold fingers being its most valuable feature. Closing it
  means obtaining the Charles et al. (2017) tables, or an equivalent
  whole-module characterization.
