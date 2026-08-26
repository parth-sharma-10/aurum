"""Auditing the TRAINING corpus in FiftyOne, as opposed to runtime failures.

`tools/fiftyone/dataset.py` turns `data/vision_errors/` — frames a running
machine flagged — into `aurum-vision-errors`. This module is the other half:
it loads `data/aurum` (the labelled corpus) and the bench captures, runs the
shipped detector over them, and asks where the model is wrong and why.

    data/aurum/{train,valid,test} ──┐
    data/bench_capture/*           ─┼─> aurum-vision-audit ─> evaluate_detections()
    data/realworld                 ─┘                            |
                                                                 v
                                              per-class / per-source / per-pair
                                              FP, FN, confidence, duplicates

Two datasets, deliberately not one. A runtime capture has no ground truth and
never will; a corpus sample has ground truth by construction. Mixing them would
make `evaluate_detections()` quietly score labelled and unlabelled samples
together, and the resulting number would mean nothing.

**FiftyOne is imported inside functions, never at module scope** — same rule as
`dataset.py`, for the same reason: nothing in `app/` may depend on it.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from tools.fiftyone.dataset import require

ROOT = Path(__file__).resolve().parent.parent.parent
AURUM_DATA = ROOT / "data" / "aurum"
AUDIT_DATASET_NAME = "aurum-vision-audit"

#: `data.yaml` calls the held-out split `val`; the directory is `valid`.
SPLIT_ALIASES = {"valid": "val", "validation": "val"}


def field(sample, name: str, default=None):
    """A sample field, or `default` if the schema has never seen it.

    `Sample.get_field` raises `AttributeError` for a field that is not yet in
    the dataset schema, which is the normal state on the first pass and not an
    error worth propagating.
    """
    try:
        value = sample.get_field(name)
    except (AttributeError, KeyError):
        return default
    return default if value is None else value


def source_of(filename: str) -> str:
    """Which Roboflow export a sample came from.

    `ml/prepare.py` prefixes every copied file `{dataset}__{original}`, so the
    provenance is in the name and needs no manifest lookup. Pooled metrics hide
    the thing that matters here: whether one source carries a class.
    """
    return filename.split("__", 1)[0] if "__" in filename else "unknown"


def load_split(split: str = "test", name: str = AUDIT_DATASET_NAME, overwrite: bool = True):
    """Load one split of `data/aurum` with its labels as `ground_truth`."""
    fo = require()
    yaml_path = AURUM_DATA / "data.yaml"
    if not yaml_path.exists():
        raise RuntimeError(f"No dataset at {yaml_path}. Run `python -m ml.prepare` first.")

    if overwrite and fo.dataset_exists(name):
        fo.delete_dataset(name)
    dataset = (
        fo.Dataset(name, persistent=True) if not fo.dataset_exists(name) else fo.load_dataset(name)
    )

    dataset.add_dir(
        dataset_type=fo.types.YOLOv5Dataset,
        yaml_path=str(yaml_path),
        split=SPLIT_ALIASES.get(split, split),
        label_field="ground_truth",
        tags=[f"split:{split}"],
    )
    for sample in dataset.iter_samples(autosave=True, progress=False):
        sample["source"] = source_of(Path(sample.filepath).name)
        sample["split"] = split
    return dataset


def load_unlabelled(directory: Path, tag: str, name: str = AUDIT_DATASET_NAME):
    """Add a directory of images that has no ground truth at all.

    Bench captures and `data/realworld` are evidence, not labels. They are
    tagged and carry NO `ground_truth` field, so `evaluate_detections()`
    excludes them — which is correct, and is why the audit reports them
    separately rather than folding them into precision and recall.
    """
    fo = require()
    dataset = (
        fo.load_dataset(name) if fo.dataset_exists(name) else fo.Dataset(name, persistent=True)
    )
    images = sorted(
        p for p in directory.rglob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )
    added = 0
    for path in images:
        sample = fo.Sample(filepath=str(path), tags=[tag, "unlabelled"])
        sample["source"] = directory.name
        sample["split"] = tag
        dataset.add_sample(sample)
        added += 1
    return dataset, added


def predict(
    dataset, weights: str | None = None, conf: float = 0.35, field_name: str = "predictions"
):
    """Run the shipped detector over every sample and attach its output.

    Uses `app.detector.AurumDetector` rather than FiftyOne's model zoo, so the
    audit measures the thing the machine actually runs — same weights, same
    confidence, same image size resolved from the checkpoint.
    """
    import cv2

    from app.detector import AurumDetector
    from tools.fiftyone.dataset import to_detections

    det = AurumDetector(weights=weights, conf=conf) if weights else AurumDetector(conf=conf)
    n = 0
    for sample in dataset.iter_samples(autosave=True, progress=True):
        frame = cv2.imread(sample.filepath)
        if frame is None:
            continue
        height, width = frame.shape[:2]
        boxes = [
            {"class_name": d.cls, "confidence": d.conf, "xyxy": list(d.xyxy)}
            for d in det.predict(frame).detections
        ]
        sample[field_name] = to_detections(boxes, width, height)
        n += 1
    return n, det


def evaluate(dataset, eval_key: str = "eval", gt: str = "ground_truth", pred: str = "predictions"):
    """COCO detection evaluation over the labelled samples only."""
    labelled = dataset.exists(gt)
    if len(labelled) == 0:
        return None, None
    results = labelled.evaluate_detections(pred, gt_field=gt, eval_key=eval_key, compute_mAP=True)
    return labelled, results


def confidence_split(dataset, eval_key: str = "eval", pred: str = "predictions") -> dict:
    """Confidence of true positives vs false positives, per class.

    The question this answers is whether a threshold could separate them. If
    the two distributions overlap, threshold tuning is not a fix, and that is a
    measurement rather than an opinion.
    """
    tp: dict[str, list[float]] = {}
    fp: dict[str, list[float]] = {}
    for sample in dataset.iter_samples(progress=False):
        preds = field(sample, pred)
        for detection in getattr(preds, "detections", None) or []:
            outcome = field(detection, eval_key)
            conf = detection.confidence
            if conf is None or outcome is None:
                continue
            bucket = tp if outcome == "tp" else fp if outcome == "fp" else None
            if bucket is not None:
                bucket.setdefault(detection.label, []).append(float(conf))

    def stats(xs: list[float]) -> dict:
        xs = sorted(xs)
        n = len(xs)
        return {
            "n": n,
            "min": round(xs[0], 4),
            "p10": round(xs[n // 10], 4),
            "median": round(xs[n // 2], 4),
            "mean": round(sum(xs) / n, 4),
            "max": round(xs[-1], 4),
        }

    out = {}
    for label in sorted(set(tp) | set(fp)):
        out[label] = {
            "true_positive": stats(tp[label]) if tp.get(label) else None,
            "false_positive": stats(fp[label]) if fp.get(label) else None,
        }
    return out


def per_source(dataset, eval_key: str = "eval") -> dict:
    """TP/FP/FN totals grouped by the Roboflow export a sample came from."""
    counts: dict[str, Counter] = {}
    for sample in dataset.iter_samples(progress=False):
        src = field(sample, "source", "unknown")
        bucket = counts.setdefault(src, Counter())
        bucket["samples"] += 1
        for key in ("tp", "fp", "fn"):
            value = field(sample, f"{eval_key}_{key}", 0)
            if value:
                bucket[key] += int(value)
    return {
        src: {
            "samples": c["samples"],
            "tp": c["tp"],
            "fp": c["fp"],
            "fn": c["fn"],
            "precision": round(c["tp"] / (c["tp"] + c["fp"]), 4) if (c["tp"] + c["fp"]) else None,
            "recall": round(c["tp"] / (c["tp"] + c["fn"]), 4) if (c["tp"] + c["fn"]) else None,
        }
        for src, c in sorted(counts.items())
    }


def top_false_positives(dataset, eval_key: str = "eval", limit: int = 20) -> list[dict]:
    """The highest-confidence wrong detections, worst first."""
    rows = []
    for sample in dataset.iter_samples(progress=False):
        preds = field(sample, "predictions")
        for detection in getattr(preds, "detections", None) or []:
            if field(detection, eval_key) == "fp" and detection.confidence is not None:
                rows.append(
                    {
                        "image": Path(sample.filepath).name,
                        "source": field(sample, "source"),
                        "predicted": detection.label,
                        "confidence": round(float(detection.confidence), 4),
                    }
                )
    rows.sort(key=lambda r: r["confidence"], reverse=True)
    return rows[:limit]


def unlabelled_predictions(dataset, tag: str) -> dict:
    """What the model claims about images we deliberately gave no labels.

    For the bench captures this is the canonical failure measured: every
    detection here is unexplained by any ground truth, because there is none.
    """
    view = dataset.match_tags(tag)
    per_class: Counter = Counter()
    confs: dict[str, list[float]] = {}
    images_with_detection = 0
    for sample in view.iter_samples(progress=False):
        preds = field(sample, "predictions")
        dets = getattr(preds, "detections", None) or []
        if dets:
            images_with_detection += 1
        for d in dets:
            per_class[d.label] += 1
            if d.confidence is not None:
                confs.setdefault(d.label, []).append(round(float(d.confidence), 4))
    return {
        "images": len(view),
        "images_with_a_detection": images_with_detection,
        "detections_by_class": dict(per_class.most_common()),
        "mean_confidence_by_class": {
            k: round(sum(v) / len(v), 4) for k, v in sorted(confs.items())
        },
    }


def duplicates(dataset, threshold: float = 0.98) -> dict:
    """Near-duplicate detection via fiftyone.brain uniqueness.

    A second opinion, not a replacement: `ml/validate.py` already runs SHA-256
    and pHash cross-split leak checks and currently reports zero leaks. This
    looks at embedding similarity instead, which catches a different kind of
    resemblance.
    """
    import fiftyone.brain as fob

    fob.compute_uniqueness(dataset)
    values = [
        (Path(s.filepath).name, field(s, "uniqueness"), field(s, "source"))
        for s in dataset.iter_samples(progress=False)
        if field(s, "uniqueness") is not None
    ]
    values.sort(key=lambda v: v[1])
    return {
        "n_scored": len(values),
        "least_unique": [
            {"image": n, "uniqueness": round(u, 5), "source": s} for n, u, s in values[:20]
        ],
        "note": (
            "Low uniqueness means the sample closely resembles others in the set. "
            "It is a review signal, not a delete list."
        ),
    }


def run_audit(
    split: str = "test",
    conf: float = 0.35,
    weights: str | None = None,
    out: Path | None = None,
    with_duplicates: bool = True,
) -> dict:
    """The whole audit: load, predict, evaluate, and write a report.

    Labelled corpus and unlabelled evidence go into ONE FiftyOne dataset so the
    app can show them side by side, but `evaluate()` runs only over samples that
    actually have `ground_truth`. The bench captures are reported through
    `unlabelled_predictions` instead, because every detection on them is by
    definition unexplained.
    """
    out = out or ROOT / "reports" / "fiftyone_audit.json"

    dataset = load_split(split)
    report: dict = {
        "split": split,
        "conf": conf,
        "weights": weights or "models/aurum_vision_v0_1_best.pt",
        "labelled_samples": len(dataset),
    }

    evidence = {
        "bench_perfboard": ROOT / "data/bench_capture/perfboard",
        "bench_perfboard_poses_v2": ROOT / "data/bench_capture/perfboard_poses_v2",
        "bench_broken_pcb": ROOT / "data/bench_capture/broken_pcb",
        "bench_empty_scene": ROOT / "data/bench_capture/empty_scene",
        "realworld": ROOT / "data/realworld",
    }
    added = {}
    for tag, directory in evidence.items():
        if directory.is_dir():
            _, n = load_unlabelled(directory, tag)
            added[tag] = n
    report["unlabelled_samples"] = added

    n_pred, det = predict(dataset, weights=weights, conf=conf)
    report["predicted_on"] = n_pred
    report["model_version"] = det.model_version
    report["imgsz"] = det.imgsz

    labelled, results = evaluate(dataset)
    if results is None:
        report["evaluated"] = False
        report["reason"] = "no sample carried ground truth"
    else:
        report["evaluated"] = True
        report["mAP"] = round(float(results.mAP()), 4)
        classes = sorted({d.label for s in labelled for d in s["ground_truth"].detections})
        report["per_class"] = {}
        for cls in classes:
            m = results.metrics(classes=[cls])
            report["per_class"][cls] = {
                "precision": round(m["precision"], 4),
                "recall": round(m["recall"], 4),
                "fscore": round(m["fscore"], 4),
                "support": m.get("support"),
            }
        report["confidence_by_outcome"] = confidence_split(labelled)
        report["per_source"] = per_source(labelled)
        report["top_false_positives"] = top_false_positives(labelled, limit=20)

    report["unlabelled_predictions"] = {tag: unlabelled_predictions(dataset, tag) for tag in added}

    if with_duplicates:
        try:
            report["duplicates"] = duplicates(dataset)
        except Exception as exc:  # brain needs extra deps; never fail the audit for it
            report["duplicates"] = {"error": f"{type(exc).__name__}: {exc}"}

    import json

    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    report["_written_to"] = str(out)
    return report
