# Aurum

**Computer vision that identifies and counts e-waste components from a webcam,
an image folder or a video, and turns each session into a structured batch
record.**

E-waste changes hands by gross weight, so what is actually inside a board is
invisible at the point of collection. Aurum's vision layer makes it
machine-readable: point a camera at a pile of hardware and get back *what is
there and how much of it*, as JSON the rest of a recycling workflow can use.

![status](https://img.shields.io/badge/status-prototype-blue) ![license](https://img.shields.io/badge/license-MIT-green) ![tests](https://img.shields.io/badge/tests-186%20passing-brightgreen) ![mAP50](https://img.shields.io/badge/test%20mAP%4050-0.806-1E5B41) ![python](https://img.shields.io/badge/python-3.12-blue)

> **The weights are a release asset, not a tracked file.** `models/*.pt` is
> gitignored, so a fresh clone has no model until you download one. One command
> and a checksum: see
> [Model weights](#model-weights-read-this-before-running-anything).

---

## What it does

```
webcam / image folder / video
        ↓
YOLO11n detector          fine-tuned, 512 px inference, conf 0.35, IoU 0.5
        ↓
detections                class + confidence + bounding box
        ↓
BatchSession              median per-class count over a 45-frame window
        ↓
batch record (JSON)  →  OpenCV dashboard
                     →  FastAPI  →  SQLite ledger  →  React dashboard
```

It identifies **four** component classes, counts them, and emits a batch
record. It does **not** measure precious-metal content, estimate material
recovery, price anything, track objects across frames, or actuate any
hardware. See [What is and is not implemented](#what-is-and-is-not-implemented).

## Supported components

| Class | What counts as one |
|---|---|
| **PCB** | A whole board as one object — motherboards, expansion cards, bare or populated |
| **RAM** | A memory module — DIMM, SO-DIMM, RDIMM |
| **CPU** | A packaged processor — LGA, PGA or BGA, lid or pin side |
| **Connector** | A mating interface — headers, sockets, slots, edge connectors, rear port banks |

Four classes, not forty. Classes without enough reliable annotation were dropped
rather than shipped weak — including `gpu`, the single largest available label,
because in the source data a GPU is a shrouded card whose camera-facing surface
is a cooler shroud rather than an exposed board. Reasoning in
[docs/dataset.md](docs/dataset.md).

## Results on the held-out test split

206 unseen images, 340 instances, measured at **512 px** — the resolution the
model was trained at, read from the checkpoint rather than passed in.

| Metric | Value |
| --- | ---: |
| Precision | **0.876** |
| Recall | **0.724** |
| mAP@50 | **0.806** |
| mAP@50:95 | **0.594** |

| Class | Instances | Precision | Recall | mAP@50 | mAP@50:95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| CPU | 79 | 0.967 | 0.949 | 0.965 | 0.831 |
| PCB | 69 | 0.913 | 0.870 | 0.933 | 0.746 |
| RAM | 139 | 0.901 | 0.511 | 0.717 | 0.390 |
| Connector | 53 | 0.721 | 0.566 | 0.607 | 0.411 |

![Test performance by class](reports/figures/test_metrics_by_class.png)

Source: `reports/test_metrics.json`, written by `python -m ml.evaluate` straight
from the Ultralytics validator. Nothing here is recomputed or rounded up.

Measured on a split that shares **no duplicate cluster** with training data —
verified: 0 clusters spanning splits, 0 exact duplicates, 0 near-duplicates.

## The generalization gap — read this next to the table above

The test split above shares its **provenance** with training: same Roboflow
projects, same photographers, same benches. To probe whether the model survives
a genuinely different camera, it was run over **27 CC-licensed photographs from
Wikimedia Commons**, verified by perceptual hash to have zero overlap with
training.

**These images have no ground-truth boxes. No accuracy figure can be computed
from them and none is quoted.** What follows is detection behaviour only.

| At the documented threshold (conf 0.35) | |
| --- | ---: |
| Images with at least one detection | **12 of 27 (44%)** |
| PCB detections | 5 |
| RAM detections | 8 |
| CPU detections | **0** |
| Connector detections | **0** |

At a relaxed conf 0.15: detections on **18 of 27**, and **1** CPU.

**CPU scores 0.965 mAP@50 on the held-out test set and detects nothing here.**
A class can sit at the top of a benchmark and still fail outright on
photographs taken by someone else, with different framing, lighting and working
distance. Read the test table as generalization across photographs of the same
kind — not as readiness for a scrap dealer's bench. Closing this gap needs
images collected on a real bench; no threshold tuning substitutes for it.

Run it on your own photos: `python -m ml.realworld --path <folder>`.
Full detail in [docs/evaluation.md](docs/evaluation.md).

## Performance — inference throughput, not pipeline throughput

Every figure below times **`model.predict` only**. Capture, batch aggregation
and rendering are excluded, so these are *not* end-to-end camera FPS. The FPS
readout on the OpenCV dashboard uses the same inference-only definition.

| Measurement | Result |
| --- | ---: |
| Test images at native size, 512 px inference | 19.5 ms mean → **~51 FPS** |
| 1280×720 input, 512 px inference | 12.2 ms mean → **~82 FPS** |
| `AurumDetector.fps` over a 30-frame window | ~52.6 |

Measured on an Apple M4. Note that inference runs on **CPU** by default —
Ultralytics does not select MPS on its own, and `AurumDetector` only passes a
device when one is given. No end-to-end pipeline FPS is currently published,
because none has been measured with an artifact to back it.

## Dataset and the leakage-safe split

Six public [Roboflow Universe](https://universe.roboflow.com) datasets — 17,193
images under **CC BY 4.0** and **Public Domain** — normalized into the four
Aurum classes via an explicit label map where every source label is either
mapped or dropped with a written reason.

The split is the part worth scrutinising. Source datasets ship augmented copies
of one photograph as separate files, and the same photo appears across multiple
projects. Splitting at random would put rotations of the same RAM module in both
train and test. So images are grouped into **duplicate clusters** (source stem,
then SHA-256 and perceptual hash across datasets) and **clusters, not images,
are split 70/20/10**.

**Two cluster counts appear in the reports, and they measure different things:**

| Number | Meaning | Source |
| ---: | --- | --- |
| **4,353** | clusters *formed* over all 17,193 ingested images | `reports/dataset_stats.json` |
| **2,154** | clusters that *survive into the built dataset* after background capping and held-out pruning | `reports/dataset_validation.json` |

**Why 17,193 images become 5,496.** Two filters run after the split: 8,464
images carry no label that survives the label map, and 3,233 augmented copies
are removed from the held-out splits so no test image is a rotation of another
test image. What is left is **4,878 train / 412 valid / 206 test**. The held-out
splits are small on purpose — they count distinct photographed scenes, which is
the only thing worth measuring on.

`python -m ml.validate` re-checks all of this independently and exits non-zero
if any photograph reaches the test set from training. It is not decoration — it
caught 18 near-duplicates the first version of the grouping code missed.

Per-dataset counts, licenses and attributions: [docs/dataset.md](docs/dataset.md).

## Model weights — read this before running anything

`models/*.pt` is gitignored — the weights are **not** in Git history. They are
published as a release asset, identified by digest so the metrics above are
bound to one exact file rather than to a filename.

| | |
|---|---|
| Release | [`model-v0.1`](https://github.com/parth-sharma-10/aurum/releases/tag/model-v0.1) |
| Asset | `aurum_vision_v0_1_best.pt` |
| SHA-256 | `cd1a3c2cd2c99c1ff5315c073f01fd236767b4425ee5d99598e6f8fedee312e9` |
| Size | 5,454,554 bytes |
| Model version | Aurum Vision v0.1 |

### Download and verify

```bash
mkdir -p models
curl -L -o models/aurum_vision_v0_1_best.pt \
  https://github.com/parth-sharma-10/aurum/releases/download/model-v0.1/aurum_vision_v0_1_best.pt

shasum -a 256 models/aurum_vision_v0_1_best.pt
# cd1a3c2cd2c99c1ff5315c073f01fd236767b4425ee5d99598e6f8fedee312e9
```

The same digest is recorded in `models/aurum_vision_v0_1_meta.json` under
`artifact.sha256` and in `docs/model-card.md`. If your download disagrees with
those, the file is not the model the reports describe — do not use it.

Then run anything:

```bash
python run_demo.py --mode images --path <a folder of photos>
python -m uvicorn app.api:app --reload
```

### Or rebuild it from scratch

`ml/train.py`'s defaults **are** the release configuration (50 epochs / 512 px /
batch 32 / patience 15 / seed 1337), so no flags are needed and a rebuild
targets the same model the published metrics describe:

```bash
cp env.example .env                # add your free Roboflow API key
export ROBOFLOW_API_KEY="..."
python -m ml.ingest                # download the 6 source datasets
python -m ml.prepare               # normalize, deduplicate, split
python -m ml.validate              # prove the split is leak-free (gates training)
python -m ml.train                 # ~3 h on an Apple M4
python -m ml.evaluate
```

A retrained model will not be byte-identical — GPU non-determinism sees to that
— so `ml/train.py` records the digest of whatever it produced into
`models/aurum_vision_v0_1_meta.json`. Compare your `reports/test_metrics.json`
against the published one rather than expecting the same hash.

Inference resolution is read from the checkpoint, so a model trained at another
size will infer at that size rather than silently at 640.

## Installation

Prerequisites: **Python 3.12** (PyTorch has no stable 3.14 wheels), a webcam for
the live demo, ~1 GB disk to rebuild the dataset, and **Node 20.19+ or 22.12+** (what Vite 7
requires) for the web dashboard.

```bash
git clone https://github.com/parth-sharma-10/aurum.git
cd aurum

python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

No environment variable is needed to *run* against existing weights.
`env.example` documents every variable; copy it to `.env` (gitignored) if you
prefer a file.

macOS: grant your terminal camera permission in **System Settings → Privacy &
Security → Camera** before the first webcam run. Without it the camera reports
itself open and never delivers a frame; the demo detects exactly that and falls
back to image mode rather than exiting.

## The OpenCV demo

```bash
python run_demo.py                                        # webcam
python run_demo.py --mode images --path data/aurum/test/images
python run_demo.py --mode video  --path clip.mp4
```

![Aurum Vision live dashboard](reports/figures/live_webcam_inference.png)

Camera feed with boxes on the left; per-class counts, object total, mean
confidence, batch ID and mass on the right; model version, inference FPS and
status along the bottom. If no load cell is attached the mass panel reads
**SIMULATED SENSOR**.

Keys: `B` new batch · `S` save batch · `SPACE` pause · `←`/`→` (or `,`/`.`)
step images · `Q` quit

Useful flags: `--conf` (default 0.35), `--iou` (0.5), `--window` (45),
`--imgsz` (defaults to the checkpoint's), `--weight-mode`
(`auto|hx711|simulated|off`), `--no-window --frames N` for headless.

Presentation runbook, including failure recovery: [docs/demo.md](docs/demo.md).

## How a batch is composed

Per-frame detection flickers — a module drops out for two frames when a hand
crosses it. If the record used whatever the last frame said, it would be a
function of *when the operator clicked*. So `BatchSession` keeps a trailing
window of **45 frames** (~1.5 s at 30 fps) and the count for each class is the
**median** over that window. The median ignores brief dropouts and brief
double-counts without inventing anything.

A batch **closes** when the operator presses `S` in the demo, or when
`POST /batch/{id}/close` is called. In image mode the window is cleared whenever
the file changes, because a median across unrelated photographs is meaningless.

```json
{
  "batch_id": "AUR-48442B02",
  "detections": { "PCB": 0, "RAM": 0, "CPU": 1, "Connector": 0 },
  "total_objects": 1,
  "average_confidence": 0.8823,
  "frames_observed": 1,
  "counting_method": "median per-class count over a trailing 45-frame window",
  "started_at": "2026-08-16T18:17:42+00:00",
  "timestamp": "2026-08-16T18:17:42+00:00",
  "model_version": "Aurum Vision v0.1",
  "source": "api",
  "weight": { "grams": 1840.0, "kg": 1.84, "simulated": true,
              "source": "simulated load cell",
              "warning": "SIMULATED SENSOR — not a physical measurement" },
  "recovery_estimate": { "available": false, "reason": "No reference yield data loaded. …" }
}
```

## Persistence — two separate paths

This is worth being explicit about, because the two do not currently meet:

| Path | Writes JSON to `data/batches/` | Writes the SQLite ledger |
|---|---|---|
| OpenCV demo (`S` key, or `--no-window`) | **yes** | **yes** |
| API (`POST /batch/{id}/close`) | **yes** | **yes** |

Both go through the single `save()` in `app/ledger.py` — there is one `INSERT`
in the codebase, and a test fails if a second appears. A batch saved on stage
shows up in `/batches`, `/stats` and the browser dashboard within one poll.

If the ledger write fails (a locked database, say) the demo prints a warning and
keeps going: the JSON is already on disk, and a persistence hiccup should not
end a presentation.

The `batches` table is flat — `batch_id`, `created_at` (the *close* timestamp),
`model_version`, `total_objects`, `avg_confidence`, `weight_grams`,
`weight_simulated` — plus `record_json` holding the complete record, so nothing
is lost to the flattening.

## HTTP API

```bash
python -m uvicorn app.api:app --reload      # interactive docs at /docs
```

| Method | Path | Semantics |
| --- | --- | --- |
| `GET` | `/health` | `status` is `ok` or `model_missing`; returns model version, classes, weights path. Never 503s |
| `GET` | `/model` | Training metadata as recorded at training time, plus `reports/test_metrics.json` if present, plus the no-composition disclaimer |
| `POST` | `/detect` | multipart `file`; returns detections (class, confidence, `box_xyxy`), counts for **every** class, `total_objects`, `average_confidence`, `inference_ms` |
| `POST` | `/detect/annotated` | Same input; returns the drawn JPEG |
| `POST` | `/batch/start` | Opens an in-memory session; returns `batch_id` and `started_at` |
| `POST` | `/batch/{id}/frame` | multipart `file`; adds one observed frame; returns this frame's counts and the running `stable_counts` |
| `POST` | `/batch/{id}/close` | Finalizes, writes JSON **and** SQLite, returns the record. Optional `?weight_mode=simulated\|hx711\|off` (default `off`) and `?hx711_port=` |
| `GET` | `/batches` | Stored records, newest first, `?limit=` (default 50) |
| `GET` | `/batches/{id}` | One stored record |
| `GET` | `/stats` | Ledger aggregates, computed in SQL |
| `GET` | `/batches/{id}/valuation` | Estimated value of a stored batch, priced at request time. **Currently always unavailable** — see below |

Unknown batch ids return **404**; empty or undecodable uploads return **400**;
every endpoint that needs the model returns **503** when weights are absent.

```bash
curl -X POST localhost:8000/batch/start                      # -> {"batch_id": "AUR-..."}
curl -F "file=@board.jpg" localhost:8000/batch/AUR-.../frame
curl -X POST "localhost:8000/batch/AUR-.../close?weight_mode=simulated"
```

### `/stats`

```json
{
  "batch_count": 3,
  "total_count": 3,
  "total_weight": {
    "measured_grams": 0.0,
    "simulated_grams": 3680.0,
    "batches_with_weight": 2,
    "note": "Simulated grams come from a labelled stand-in for the HX711 load cell and are not physical measurements."
  },
  "component_breakdown": { "CPU": 3, "RAM": 0, "PCB": 0, "Connector": 0 },
  "bin_breakdown": {},
  "bin_breakdown_note": "Aurum does not implement physical bin routing or servo actuation. …"
}
```

Two deliberate properties:

- **Mass is never one number.** `measured_grams` and `simulated_grams` are
  separate fields, because summing them would produce a figure that reads as
  measured. Only a record that explicitly stored `weight_simulated = 0` counts
  as measured, so missing provenance falls to the cautious side.
- **`bin_breakdown` is empty by fact, not omission.** Aurum has no bin routing
  and no actuator, so no record carries a bin assignment.

Aggregation is three SQL statements (`COUNT`/`SUM`, a `GROUP BY` on the
simulated flag, and `json_each` over the stored record for per-class counts).
No row is deserialized in Python to add up integers.

## React dashboard

A small browser view of the ledger, in `frontend/`. React 19 + Vite 7 and
nothing else — no UI kit, no chart library, no state manager.

```bash
# terminal 1 — backend must be running first
python -m uvicorn app.api:app --reload

# terminal 2
cd frontend
npm install
npm run dev            # http://localhost:5173
```

It shows aggregate metrics from `/stats`, a ledger table from `/batches`, and
the complete stored record for any batch you select. It **polls every 5
seconds**; there is no websocket.

**It is not a live camera view.** There is no video stream, no live detection
overlay and no control over the detector. It visualizes **closed batch records
that reached the SQLite ledger through the API** — which, per
[Persistence](#persistence--two-separate-paths), excludes anything saved from
the OpenCV demo. Simulated mass is badged `SIMULATED` everywhere it appears and
is never added to measured mass.

Point it at a different backend with `VITE_AURUM_API=http://host:port npm run dev`.
`npm run build` emits a static bundle to `frontend/dist/` (gitignored).

### CORS

The API allows exactly two origins — `http://localhost:5173` and
`http://127.0.0.1:5173` — for **`GET` only**, without credentials. This is a
development policy for the Vite dev server, deliberately not a wildcard: the API
has no authentication, so `allow_origins=["*"]` would let any page a developer
visits read their local ledger. Vite is pinned to 5173 (`strictPort`); change
both sides together.

## Prototype constraints

Not production-ready, and specifically:

- **No authentication.** Any client that can reach the port can read and write.
- **No upload size limit** on `/detect` and `/batch/{id}/frame`.
- **Batch sessions are in-memory and process-local.** An open batch lives in a
  dict in one process: it is lost on restart, it never expires, and **more than
  one uvicorn worker is not safe** — a frame can land on a worker that has never
  heard of the batch. Only *closed* batches are durable.
- **CORS is scoped to localhost Vite origins** and nothing else.
- No rate limiting, no request logging, no TLS.

## Weight: simulated versus measured

Mass is optional and always labelled. With no load cell attached,
`SimulatedLoadCell` produces a drifting value that is flagged
`"simulated": true`, carries `"warning": "SIMULATED SENSOR — not a physical
measurement"`, renders as **SIMULATED SENSOR** in the OpenCV dashboard, badges
`SIMULATED` in the React dashboard, and lands in `simulated_grams` in `/stats`.

`HX711LoadCell` exists and reads calibrated grams from a serial line, but
**it has never been run against physical hardware**, and no Arduino sketch is
included in this repository. A class existing is not a working load cell.

## Recovery and valuation — both disabled, and why

The chain past detection is built but carries no data:

```
detected components   ← the only measured thing here
      ↓  × published reference yield   (configs/recovery_reference.yaml)
estimated recovery    ← ESTIMATE
      ↓  × price per unit              (configs/price_reference.yaml)
estimated value       ← ESTIMATE of an ESTIMATE
```

**Both config files ship empty and disabled**, so `recovery_estimate` returns
`{"available": false, "reason": ...}` with **no numeric field at all**, and
`/batches/{id}/valuation` returns an explicit refusal. Enabling either requires
figures with real citations, and none were available — so none were invented. A
plausible-looking number with nothing behind it is worse than no number, because
it gets quoted and outlives the conversation that qualified it.

Every batch record names three different quantities so they cannot be confused:

| Field | Meaning |
|---|---|
| `detected_components` | what the model counted — measured, by the model |
| `components[].total` | count × reference yield — an **estimate** |
| `measured_material` | **always unavailable.** Aurum has no assay or XRF |

Valuation is computed at request time, never stored on the record: a metal price
is time-varying external data, so baking one into a batch would make the record
silently wrong the next day. Each priced line carries the quote's material,
unit, currency, timestamp and source, plus a `calculation_version`.

`app/pricing.py` refuses rather than guesses — no provider, a material it cannot
quote, a unit mismatch (grams priced per troy ounce would be wrong by 31× and
look plausible), or mixed currencies all produce `available: false`.

**PMDI is not implemented and no formula for it exists in this repository.** It
is named only in the capability table below. Nothing computes it.

## What is and is not implemented

| Capability | Status |
|---|---|
| YOLO11n detection (PCB / RAM / CPU / Connector) | **implemented** |
| Webcam, image-folder and video inference | **implemented** |
| Median-window batch aggregation | **implemented** |
| Batch records as JSON | **implemented** |
| SQLite ledger | **implemented — via the API only** |
| FastAPI service (10 endpoints) | **implemented** |
| OpenCV dashboard | **implemented** |
| React dashboard (closed batches) | **implemented** |
| Leakage-safe dataset split + independent validator | **implemented** |
| External-image evaluation (no ground truth) | **implemented** |
| Simulated load cell | **implemented, labelled** |
| HX711 serial class | **exists, never verified against hardware** |
| Arduino integration / sketch | **not implemented** |
| Servo sorting, physical routing, bin actuation | **not implemented** |
| Material / recovery estimation | **not implemented** — mechanism and interfaces present, disabled, no cited yields |
| Valuation / pricing | **not implemented** — provider interface present, disabled, no price source |
| PMDI | **not implemented** |
| Carbon figures | **not implemented** |
| Object tracking across frames | **not implemented** |
| Cyber-physical state machine | **not implemented** |
| Live camera stream in the browser | **not implemented** |

## Project structure

```
app/         Runtime: detector, dashboard, demo loop, batch, weight, ledger, pricing, API
frontend/    React/Vite browser view of the ledger (reads the API only)
ml/          Pipeline: ingest → prepare → validate → train → evaluate → realworld → assets
configs/     Label map, pinned datasets, recovery + price references (both disabled)
scripts/     Doc generators, training monitor, external-image fetcher, Universe search
tests/       186 tests
docs/        dataset · training · evaluation · model-card · architecture · demo
reports/     Generated metrics, figures and validation output
models/      Training metadata (tracked) + weights (gitignored, released separately)
data/        Datasets, batch JSON and the SQLite ledger (all gitignored)
run_demo.py  One-command demo entry point
```

Key files: `configs/aurum_labels.yaml` (every label decision, with reasons),
`ml/prepare.py` (the leakage-safe split), `app/batch.py` (batch composition and
the recovery-estimate guard), `app/api.py` (HTTP surface and `/stats`).

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q                  # 186 tests
ruff check . && ruff format --check .

cd frontend && npm run build         # static bundle to frontend/dist/
```

`docs/model-card.md`, `docs/evaluation.md` and `docs/dataset.md` are
**generated** from the pipeline's own JSON output by `scripts/gen_*.py` — edit
the generator, never the document. CI verifies that when the metrics file is
removed they report results as pending rather than emitting a placeholder
figure.

## Documentation

| Document | What it covers |
| --- | --- |
| [docs/model-card.md](docs/model-card.md) | Intended use, measured results, failure modes, recovery-estimation status |
| [docs/evaluation.md](docs/evaluation.md) | Split design, leakage prevention, test and external results |
| [docs/dataset.md](docs/dataset.md) | Every source dataset, license, label mapping and exclusion reason |
| [docs/training.md](docs/training.md) | Reproducing the model end to end |
| [docs/architecture.md](docs/architecture.md) | How the runtime pieces fit together |
| [docs/demo.md](docs/demo.md) | Presentation runbook — setup, sequence, failure recovery, Q&A |

## Limitations

- **No composition sensing.** The model sees surfaces. It cannot tell a
  gold-plated connector from a tin-plated one of the same shape, and it does not
  determine composition, purity or recoverable value. Detection is a *precursor*
  to valuation, not a substitute for assay.
- **Measured domain gap.** On 27 photographs from a different source the model
  detects something in 44% of images and finds **zero CPUs** despite CPU scoring
  0.965 mAP@50 on the held-out test set.
- **Two weak classes.** `Connector` is deliberately broad (DIMM socket to RC
  plug) at 0.607 mAP@50. `RAM` recalls only 0.511 of its instances despite
  having the most training data — memory modules are commonly photographed in
  rows, and adjacent near-identical objects are hard to separate into counts.
- **Counting is per-frame detection, not tracking.** Stacked or occluding boards
  can undercount; the median window suppresses flicker, not occlusion.
- **Small test set.** Per-class figures rest on tens of instances. Treat
  differences of a few points as noise.
- **Weights are a separate download.** A clone is not runnable until the
  release asset is fetched; the checksum above is what makes that safe.
- **Prototype.** No claim is made about sustained field accuracy or throughput.

## Roadmap

Realistic next steps, none of which are implemented:

- Collect and annotate images on an actual collection bench to close the domain
  gap the external evaluation exposes.
- Object tracking across frames so counts survive occlusion.
- Populate `configs/recovery_reference.yaml` with cited yield figures and enable
  recovery estimation behind the existing guard.
- Wire the HX711 path to real hardware.

## License and attribution

Code is **MIT** — see [LICENSE](LICENSE).

Not covered by that license:

- **Datasets** — third-party works from Roboflow Universe under CC BY 4.0 and
  Public Domain, not redistributed here; `python -m ml.ingest` fetches them from
  source. Per-dataset attribution: [docs/dataset.md](docs/dataset.md).
- **External evaluation images** — CC BY / CC BY-SA / CC0 / Public Domain from
  Wikimedia Commons; per-file license and author in
  `reports/realworld_sources.json`.
- **Ultralytics YOLO11** — **AGPL-3.0**. This project depends on it but does not
  vendor it. Review AGPL obligations before distributing a combined or hosted
  work, or obtain an Ultralytics commercial license.
