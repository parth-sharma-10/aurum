# Aurum Vision

**Real-time e-waste component identification for Project Aurum.**

Aurum Vision is the sensing layer of the Aurum workflow: a webcam-speed object
detector that says *what physically entered the recycling workflow* — how many
circuit boards, memory modules, processors and connectors are on the bench — and
emits a structured batch record the rest of the Aurum stack can consume.

```
CAMERA → YOLO DETECTION → COMPONENT IDENTITY → COUNTS + CONFIDENCE → BATCH RECORD
```

---

## What this is, and what it is not

**What it does.** Identifies four visible component categories from RGB imagery,
counts them, attaches confidences, and produces a batch record with an optional
mass reading.

**What it does not do.** It does not measure precious-metal content. No RGB
camera can. An image tells you *that a RAM module is present*, not how much gold
is in it. Any recovery figure downstream of this model is an **estimate** formed
by multiplying component counts by published reference yields, and it is
labelled as such everywhere it appears — including in the JSON.

This is a **prototype**. It is not production-ready, not industrial-grade
sorting, and its performance figures are what a small fine-tune on public data
achieved, no more.

---

## Why component identification is the first step

E-waste changes hands by gross weight, so the value inside a board is invisible
at the point of collection. Every downstream Aurum module — component-aware
valuation, servo sorting, the EPR ledger — needs one thing first: a machine-
readable answer to *what is in this pile*. That is the gap Aurum Vision fills,
and the reason it stops at identification rather than pretending to assay.

---

## Classes

| Class | What counts as one |
|---|---|
| **PCB** | A whole board as one object — motherboards, expansion cards, bare or populated |
| **RAM** | A memory module — DIMM, SO-DIMM, RDIMM |
| **CPU** | A packaged processor — LGA, PGA or BGA, lid or pins |
| **Connector** | A mating interface — headers, sockets, slots, edge connectors, rear port banks |

Four classes, not forty. Each survived a data-availability check; classes without
enough reliable annotation were dropped rather than shipped weak. `Battery` was
considered and rejected — the available annotations mix 9V cells, coin cells,
laptop packs and RC LiPo packs, which are not one visual category. Full reasoning
in [`DATA_SOURCES.md`](DATA_SOURCES.md).

---

## Quick start

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Run the demo

```bash
python run_demo.py                    # webcam
```

If no camera is available it falls back automatically to image-demo mode. To
force it:

```bash
python run_demo.py --mode images --path data/aurum/test/images
python run_demo.py --mode video  --path clip.mp4
```

**Keys:** `B` new batch · `S` save batch · `SPACE` pause · `←/→` step images · `Q` quit

The dashboard shows the live feed with boxes and confidences, per-class counts,
total objects, mean confidence, FPS, model version and batch ID.

### Run the API

```bash
uvicorn app.api:app --port 8000
open http://127.0.0.1:8000/docs
```

| Endpoint | Purpose |
|---|---|
| `GET /health` | model loaded, classes |
| `GET /model` | version, metadata, test metrics, disclaimer |
| `POST /detect` | image → detections, counts, confidences |
| `POST /detect/annotated` | image → annotated JPEG |
| `POST /batch/start` · `/batch/{id}/frame` · `/batch/{id}/close` | accumulate a batch |
| `GET /batches` | batch history from SQLite |

### Reproduce the model

See [`TRAINING.md`](TRAINING.md). Short version:

```bash
export ROBOFLOW_API_KEY="…"
python -m ml.ingest && python -m ml.prepare && python -m ml.validate
AURUM_EPOCHS=50 AURUM_BATCH=32 AURUM_IMGSZ=512 python -m ml.train
python -m ml.evaluate
```

---

## Batch record

Every session produces one of these:

```json
{
  "batch_id": "AUR-559179A3",
  "detections": { "PCB": 2, "RAM": 1, "CPU": 1, "Connector": 0 },
  "total_objects": 4,
  "average_confidence": 0.934,
  "counting_method": "median per-class count over a trailing 45-frame window",
  "model_version": "Aurum Vision v0.1",
  "weight": { "kg": 1.841, "simulated": true,
              "warning": "SIMULATED SENSOR — not a physical measurement" },
  "recovery_estimate": { "available": false, "reason": "No reference yield data loaded." }
}
```

Counts are the **median** over a trailing frame window, not whatever the last
frame happened to say — otherwise the record would be a function of when the
operator clicked. Mass comes from an HX711 load cell when one is attached and is
flagged `simulated: true` when it is not; the dashboard prints **SIMULATED
SENSOR** rather than a value label in that case.

`recovery_estimate` reports `available: false` until
`configs/recovery_reference.yaml` is populated with *cited* per-component yield
figures. No placeholder numbers were invented to fill it.

---

## Repository layout

```
configs/
  aurum_labels.yaml        canonical source-label → Aurum-class map, with drop reasons
  datasets.yaml            pinned source datasets + why each excluded one was excluded
  recovery_reference.yaml  reference yields (empty until cited figures are added)
ml/
  ingest.py     download pinned Roboflow Universe datasets + record their metadata
  labels.py     label normalization; unreviewed labels raise rather than vanish
  prepare.py    normalize, cluster duplicates, split 70/20/10 by cluster
  validate.py   independent leakage + integrity check; gates training
  train.py      transfer learning from COCO-pretrained YOLO11
  evaluate.py   held-out metrics, confusion matrix, correct + failure examples
  realworld.py  run over your own unseen photographs
  assets.py     presentation figures, all from real pipeline output
app/
  detector.py   model wrapper shared by demo and API
  batch.py      batch composition and the recovery-estimate guard
  dashboard.py  OpenCV dashboard rendering
  demo.py       webcam / video / image demo
  weight.py     HX711 load cell, or a clearly-labelled simulation
  api.py        FastAPI service + SQLite batch ledger
run_demo.py     one-command entry point
```

---

## Data and leakage

Six public Roboflow Universe datasets (CC BY 4.0 and Public Domain), normalized
into four classes. Full provenance, licenses and per-class counts in
[`DATA_SOURCES.md`](DATA_SOURCES.md), generated from the download metadata rather
than typed by hand.

The split is the part worth scrutinising. Roboflow ships augmented copies of one
photograph as separate files, and the same photo appears across multiple Universe
projects. Splitting those at random would put rotations of the same RAM stick in
both train and test. So images are grouped into duplicate clusters — by source
stem, then by SHA-256 and perceptual hash across datasets — and **clusters, not
images, are split 70/20/10**. Held-out splits keep one image per cluster.

`python -m ml.validate` re-checks this independently and exits non-zero if any
photograph reaches the test set from training. It is not decoration: it caught 18
near-duplicates that the first version of the grouping code missed.

---

## Evaluation

Actual held-out numbers, the confusion matrix, PR curves and both correct and
failing predictions are in [`MODEL_CARD.md`](MODEL_CARD.md) and `reports/`.

Nothing in this repository quotes an accuracy figure that was not produced by
`python -m ml.evaluate` on the test split.

---

## Limitations

- RGB vision identifies **visible component categories**. It does not determine
  composition, purity, or precious-metal mass.
- Training data is public internet photography of PC hardware — product shots,
  build photos, teardowns. A scrap dealer's bench looks different. Run
  `python -m ml.realworld --path <your photos>` before trusting it in the field.
- `Connector` is the weakest class: it has the fewest instances and the widest
  visual range, from a DIMM socket to an RC plug.
- Counts are per-frame detections, not object tracking. Two boards stacked and
  occluding each other may count as one.
- Prototype scope. No claim is made about throughput, sustained field accuracy,
  or readiness for industrial sorting.
