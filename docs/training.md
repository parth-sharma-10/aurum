# Training Aurum Vision

Everything needed to reproduce **Aurum Vision v0.1** from scratch. No step
depends on a path that exists only on the original machine.

## 0. Requirements

- **Python 3.12.** PyTorch does not publish stable wheels for 3.14, which is the
  default `python3` on some macOS setups. Check with `python3.12 --version`.
- A free **Roboflow API key** (roboflow.com → Settings → API Keys). Universe
  datasets are public but the download endpoint requires a key.
- ~1 GB of disk for `data/`, plus whatever the runs directory grows to.
- Apple Silicon (MPS), CUDA, or CPU. The pipeline picks the best available.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export ROBOFLOW_API_KEY="…"      # or add to ~/.zshrc
```

## 1. Ingest the source datasets

```bash
python -m ml.ingest
```

Downloads the six pinned Universe projects listed in `configs/datasets.yaml`
into `data/raw/<key>/` in YOLO format. Each gets an `_aurum_meta.json` recording
the license, image count and per-class instance counts exactly as the Roboflow
API reported them — this is what `dataset.md` is generated from, so the
documentation cannot drift from the download.

Versions are pinned. Re-running months later fetches the same export.

```bash
python -m ml.ingest --only computer_parts --force   # refresh one dataset
```

## 2. Normalize, deduplicate and split

```bash
python -m ml.prepare
```

Three things happen, in this order:

**Label normalization.** Every source label is resolved through
`configs/aurum_labels.yaml` into one of `PCB, RAM, CPU, Connector`, or is
explicitly dropped with a written reason. A label that is in neither list raises
and stops the run — silent label loss is indistinguishable from a dataset that
never had those labels.

**Grouping.** Roboflow ships augmented copies of one photograph as separate
files (`<stem>_jpg.rf.<hash>.jpg`), and the same photo often appears in more
than one Universe project. Images are grouped by source stem, then groups are
merged across datasets by SHA-256 and by perceptual hash (exact pairwise Hamming
over distinct pHashes, threshold 5). The result is *duplicate clusters*.

**Splitting.** Clusters — never individual images — are assigned 70/20/10,
rarest-class-first so CPU and Connector get their proportional share instead of
landing wherever a shuffle put them. Valid and test then keep one image per
cluster, so held-out counts reflect unique scenes rather than rotations of the
same RAM stick.

Tunables: `--seed`, `--hamming`, `--max-background-frac`.

## 3. Verify the split before spending GPU time

```bash
python -m ml.validate
```

Independently re-checks what `prepare` claims:

- no cluster spans two splits
- no exact file duplicate across splits
- no perceptual near-duplicate between train and held-out
- label files present, class indices in range, boxes normalized and non-degenerate
- every class has test instances

Exits non-zero on any failure, so it can gate training in CI. Results land in
`reports/dataset_validation.json`.

This check earns its keep: the first version of `prepare` hashed only one
representative image per group, and `validate` caught 18 near-duplicates
spanning train and held-out that would otherwise have inflated the reported
test metrics.

## 4. Train

```bash
python -m ml.train
```

Transfer learning from COCO-pretrained `yolo11n.pt` — nothing trains from
scratch. Configuration is by environment variable so a second developer changes
nothing in the source:

The defaults **are** the v0.1 release configuration (`RELEASE_CONFIG` in
`ml/train.py`, read back from the released checkpoint's own `train_args`), so
`python -m ml.train` targets the model the published metrics describe. Every
value is still overridable:

| Variable | Default | Notes |
|---|---|---|
| `AURUM_MODEL` | `yolo11n.pt` | nano; `yolo11s.pt` if you have the compute |
| `AURUM_EPOCHS` | `50` | release configuration |
| `AURUM_IMGSZ` | `512` | release configuration |
| `AURUM_BATCH` | `32` | release configuration |
| `AURUM_PATIENCE` | `15` | early stop; release configuration |
| `AURUM_SEED` | `1337` | release configuration |
| `AURUM_DEVICE` | auto | `mps`, `0`, `cpu` |
| `AURUM_WORKERS` | `8` | dataloader only; does not change the weights |
| `AURUM_RUN` | `aurum_vision_v0_1` | run directory name |

The release run itself was resumed from a checkpoint partway through, so it
recorded `workers: 0`. That is a loader setting, not a result-affecting one, and
it is the reason `AURUM_WORKERS` is not part of `RELEASE_CONFIG`.

`tests/test_detector.py::TestReleaseTrainingDefaults` pins these against
`models/aurum_vision_v0_1_meta.json`, so the code and the shipped model cannot
drift apart again silently.

512 px rather than 640 was a deliberate trade. The targets in this application
are large in frame — a RAM module held up to a bench camera — so the accuracy
cost is small, while training time drops by about a third and demo inference
gets faster on the same laptop. It is not the right default for the
microscope-scale PCB inspection datasets discussed in `dataset.md`.

Augmentation is mild and deliberate: horizontal flip and ±15° rotation, but
**no vertical flip** (an upside-down RAM module cannot sit in a slot) and
restrained HSV jitter (green board and gold contacts are real class signal).

Outputs: `runs/<name>/` with weights, curves and plots; `best.pt` and `last.pt`
are copied to `models/` alongside `aurum_vision_v0_1_meta.json`.

## 5. Evaluate on the held-out test split

```bash
python -m ml.evaluate
```

Runs the Ultralytics validator on `test` and writes
`reports/test_metrics.json`: precision, recall, mAP@50 and mAP@50:95, overall
and per class. Copies the confusion matrix and PR/F1 curves into `reports/`, and
renders representative correct detections and representative failures into
`reports/test_predictions/{correct,failures}/` with ground truth in white and
predictions in gold.

The reported numbers are whatever the validator produced. If they are
disappointing, they stay in the file.

## 6. Real-world spot check

```bash
python -m ml.realworld --path path/to/your/photos
```

Benchmark test images share their provenance with training images. This runs the
model over photographs you took yourself — cluttered bench, odd angles, mixed
lighting — and reports what it detected, so the gap between benchmark and reality
is visible rather than assumed.

## 7. Presentation figures

```bash
python -m ml.assets
python scripts/gen_data_sources.py
```

Writes `reports/figures/` and regenerates `dataset.md`. Every figure reads
a JSON or CSV the pipeline produced; if a stage has not run, its figure is
skipped rather than drawn from placeholder numbers.

## Full rebuild

```bash
export ROBOFLOW_API_KEY="…"
python -m ml.ingest
python -m ml.prepare
python -m ml.validate            # must pass before training
python -m ml.train               # defaults are the release configuration
python -m ml.evaluate
python -m ml.assets
python scripts/gen_data_sources.py
```
