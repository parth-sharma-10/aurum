# Architecture

Aurum Vision is one sensing layer of the wider Project Aurum workflow. Its job
is narrow on purpose: turn what is physically on the bench into a machine-
readable record that later stages (valuation, sorting, EPR ledger) can consume.

## Runtime pipeline

```mermaid
flowchart LR
    CAM[Webcam / image / video] --> DET[YOLO11n detector<br/>512 px inference]
    DET --> ID[Component identities<br/>+ confidence + boxes]
    ID --> CNT[Median count<br/>over frame window]
    CNT --> REC[Batch record JSON<br/>data/batches/]
    W[HX711 load cell<br/>or SIMULATED] -.-> REC
    CNT --> UI[OpenCV dashboard]
    REC --> LED[app/ledger.py<br/>single INSERT]
    API[FastAPI] --> LED
    LED --> DB[(SQLite ledger)]
    DB --> WEB[React dashboard]
    DB -.-> VAL[PMDI -> Valuation<br/>value UNAVAILABLE: no price provider]
```

**Both save paths meet at `app/ledger.py`.** The demo's `S` key and
`POST /batch/{id}/close` call the same `save()`, so a batch saved on stage
appears in `/batches`, `/stats` and the React dashboard. There is one `INSERT`
in the codebase and a test fails if a second appears.

The dotted branch computes what it can and refuses the rest. `app/valuation/`
produces PMDI and the price-independent `precious_mass_fraction_ppm` from cited
evidence, but `pmdi_value` stays `UNAVAILABLE` because no live price provider is
approved for this project. `configs/pricing.yaml` ships `provider: unavailable`
as a decision, not a placeholder.

PMDI is an **input to** the A/B/C decision policy, never the same thing as it.
`app/valuation/` contains no grading logic and a test fails if it ever does.
See [pmdi.md](pmdi.md).

The dashed line matters: mass is optional and, when no load cell is attached,
the reading is flagged `simulated: true` and rendered as **SIMULATED SENSOR**.
It is never presented as a measurement.

## Components and why each is there

| Piece | Role | Why this choice |
|---|---|---|
| **Ultralytics YOLO11n** | Object detection | Nano variant runs real-time on a laptop CPU/GPU. Fine-tuned from COCO weights; nothing trains from scratch. |
| **OpenCV** | Capture + dashboard rendering | The demo is one process with no browser and no server. A web UI adds two failure modes that kill live presentations. |
| **FastAPI** | HTTP surface for the rest of the stack | The seam where the Aurum backend consumes identifications and batch records. |
| **SQLite** | Batch ledger | Persists offline at the point of collection, which is the field constraint the concept doc calls out. Written by the API only. |
| **React + Vite** | Browser view of the ledger | Reads `/stats` and `/batches` over HTTP and computes nothing of its own. Not a live camera view. |

## Valuation subsystem

| File | Role |
|---|---|
| `app/valuation/prices.py` | Provider abstraction, price status model, staleness, unit conversion |
| `app/valuation/pmdi.py` | The PMDI calculation; precious / base / other split |
| `app/valuation/valuation.py` | PMDI plus the separate base-metal signal, packaged for audit |
| `app/config.py` | Every threshold and physical constant, `defaults -> YAML -> environment` |

## Item identity and lifecycle

```
frame -> detector -> ByteTrack -> TrackedItem (stable item_id) -> weight -> PMDI -> decision -> routing
```

A camera pointed at the conveyor produces a CPU in frame 1, a CPU in frame 2
and a CPU in frame 3. That is **one** physical object, and everything
downstream -- one weighing, one decision, one servo firing, one ledger row --
depends on saying so.

| File | Role |
|---|---|
| `app/vision/tracker.py` | `ItemTracker`, the lifecycle state machine; `DetectorTracker`, the ByteTrack adapter |
| `app/pipeline/item_pipeline.py` | Composes detector, tracker and lifecycle; the seam later phases hang off |
| `configs/tracking.yaml` | Track-loss tolerance and confirmation threshold |

### A `track_id` is not an `item_id`

**These are deliberately different things.** ByteTrack numbers tracks from 1 and
starts over every process, so using its number as the ledger identity would
collide across restarts -- two different CPUs from two different sessions both
filed as item 1. Aurum mints `AUR-ITEM-xxxxxxxx` and carries the `track_id`
alongside it for debugging only.

A track id recycled by the tracker after an item has finalized produces a **new**
item identity, never a revived one.

### Lifecycle

```
NEW  ->  TRACKING  ->  CONFIRMED  ->  LEAVING  ->  FINALIZED
```

| State | Meaning |
|---|---|
| `NEW` | Seen once. Not yet something to act on. |
| `TRACKING` | Seen repeatedly, below the confirmation threshold. |
| `CONFIRMED` | Eligible to be weighed, decided and routed. |
| `LEAVING` | Missing from recent frames, still inside tolerance. May return. |
| `FINALIZED` | Terminal. Handed over exactly once. |

There is no `MEASURING` state: nothing drives one yet. Phase 5 attaches a mass
to an item while it is `CONFIRMED`, through the `weight_g` / `weight_status` /
`weight_timestamp` slots the model already carries.

**An item is not finalized because it blinked.** A detection miss or an
occluded frame moves it to `LEAVING`; only
`tracking.max_missing_frames` consecutive absences finalize it. Finalization is
idempotent, and `drain_finalized()` hands each item over exactly once -- that is
what stops one physical component becoming several ledger rows.

### Confidence has one documented meaning

`TrackedItem.confidence` is the **mean over every observation**, and that is the
figure the decision engine reads. A single lucky frame must not promote an item
into the premium bin, nor a single unlucky one demote it. `latest_confidence`
and `max_confidence` are also exposed, and the output states which basis was
used.

The majority class over all observations wins, so a one-frame class flip cannot
change which bin an item is routed to.

### Engineering approximations

| Setting | Value | Source |
|---|---|---|
| `tracking.max_missing_frames` | 15 (~0.5 s at 30 fps) | **none -- engineering approximation** |
| `tracking.min_detections_to_confirm` | 3 | **none -- engineering approximation** |

Velocity is reported in **pixels per frame** and deliberately not converted to
cm/s: that needs a belt speed and a pixel scale, both `UNMEASURED`.

## Decision policy

| File | Role |
|---|---|
| `app/decision/engine.py` | The A/B/C ladder, reason codes, and the explainable `Decision` record |
| `configs/grading.yaml` | Thresholds and the class-aware policy |

```
material evidence -> PMDI / valuation -> DECISION POLICY -> A / B / C -> routing (Phase 6)
```

The three are separate on purpose. PMDI says what the evidence implies
economically; the decision engine says what the machine does about it; routing
translates a bin into a servo firing. Nothing in `app/decision/` changes a cited
or measured quantity, and no class name or threshold appears in its logic --
both come from configuration.

**Bin C is the fail-safe and has no servo.** An item nobody routes reaches the
end of the belt and falls into C, so every refusal is also the safe hardware
state. See [pmdi.md](pmdi.md) for the ladder and the reason codes.

Grading thresholds in `configs/grading.yaml` are **configurable engineering
approximations** for the prototype, not validated scientific cutoffs. The
`preferred_classes` mechanism is an engineering sorting policy and says so at
the point of use.

## Module map

```
app/
  detector.py    Model wrapper. Loading, warm-up, FPS accounting — shared by
                 the demo and the API so both report the same numbers.
  dashboard.py   Pure rendering. Takes detections + counts, returns a frame.
  demo.py        Frame sources (webcam / video / images) and the key loop.
  batch.py       Batch composition + the recovery-estimate guard.
  weight.py      HX711 backend, or a clearly-labelled simulation.
  ledger.py      Every read and write of the SQLite store. One INSERT.
  pricing.py     Price providers and estimated_quantity x price. Disabled by
                 default; no price data ships.
  api.py         FastAPI endpoints. Holds no SQL of its own.

frontend/
  src/App.jsx    Header, metric row, ledger table, batch-record modal.
  src/index.css  Design tokens, panels, badges, ledger table. The palette is
                 converted from the BGR constants in app/dashboard.py so the
                 browser and OpenCV views stay one instrument.

ml/
  ingest.py      Download pinned Roboflow datasets, record their metadata.
  labels.py      Source label -> Aurum class. Unreviewed labels raise.
  prepare.py     Normalize, cluster duplicates, split by cluster.
  validate.py    Independent leakage + integrity check. Gates training.
  train.py       Transfer learning from COCO-pretrained YOLO11.
  evaluate.py    Held-out metrics, confusion matrix, correct/failure examples.
  realworld.py   Inference over external images with no ground truth.
  assets.py      Figures, all generated from real pipeline output.
```

## Two design decisions worth explaining

**Counts are a median over a trailing frame window, not the last frame.**
Per-frame detection flickers — a hand crosses the bench and a module drops out
for two frames. If the batch record used whatever the last frame said, the
record would be a function of *when the operator clicked*. The median ignores
brief dropouts and brief double-counts without inventing anything. This is
unit-tested (`tests/test_batch.py`), including the case where the operator stops
on a bad frame.

**Splits are assigned over duplicate clusters, not images.** Source datasets
ship augmented copies of one photograph as separate files, and the same photo
appears across multiple Universe projects. Splitting those at random puts
rotations of the same RAM module in both train and test. See
[evaluation.md](evaluation.md) for the mechanism and the verification.

## Where the boundary is

Aurum Vision stops at identification. There is no endpoint that returns a metal
content, because the model does not produce one. Recovery estimation is a
separate, currently **disabled** mechanism — see
[model-card.md](model-card.md#recovery-estimation) and
`configs/material_reference.yaml`.
