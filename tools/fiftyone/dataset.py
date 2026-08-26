"""Turning captured failures into a FiftyOne dataset, and evaluating it.

    data/vision_errors/*.jpg + failures.jsonl
            |
            v
      build_dataset()  ->  fo.Dataset with `predictions` and, where a label
            |              exists, `ground_truth`
            v
       evaluate()      ->  dataset.evaluate_detections(...) -> TP / FP / FN
            |
            v
        launch()       ->  the app, filtered to whatever is worth looking at

**FiftyOne is imported inside the functions, never at module scope.** It is a
development dependency with a large install and a database server behind it,
and neither the API, the demonstration nor the test suite may depend on it
being present. `available()` answers whether it is, without raising.

**Predictions and ground truth never share a field.** A prediction is what the
model said; ground truth is what somebody labelled. FiftyOne's evaluation is
the thing that compares them, and it can only do that honestly if nothing
upstream has already merged them. A sample with no label simply has no
`ground_truth` field, which is the truthful state for a frame captured off a
running machine.

**Coordinates.** FiftyOne stores boxes as relative `[x, y, w, h]` in 0..1;
Aurum carries absolute `[x1, y1, x2, y2]` pixels. The conversion is here,
once, and is a pure function so it can be tested without FiftyOne installed.
"""

from __future__ import annotations

from pathlib import Path

from tools.fiftyone.failures import DEFAULT_DIR, VisionFailure, read_manifest

DATASET_NAME = "aurum-vision-errors"


def available() -> bool:
    """Whether FiftyOne can be imported. Never raises."""
    try:
        import fiftyone  # noqa: F401
    except Exception:
        return False
    return True


def require():
    """Import FiftyOne, or explain how to get it."""
    try:
        import fiftyone as fo
    except ImportError as exc:
        raise RuntimeError(
            "FiftyOne is not installed. It is a development tool and is not "
            "required to run Aurum:\n"
            "    pip install fiftyone\n"
            "See docs/evaluation.md."
        ) from exc
    return fo


def to_relative(xyxy, width: int, height: int) -> list[float]:
    """Absolute pixel corners to FiftyOne's relative [x, y, w, h].

    Clamped to the frame: a box that runs off the edge is a real detection of a
    partially visible object, and FiftyOne rejects coordinates outside 0..1.
    """
    if not xyxy or len(xyxy) != 4 or width <= 0 or height <= 0:
        raise ValueError(f"cannot convert box {xyxy!r} in a {width}x{height} frame")
    x1, y1, x2, y2 = (float(v) for v in xyxy)
    left = min(max(x1 / width, 0.0), 1.0)
    top = min(max(y1 / height, 0.0), 1.0)
    right = min(max(x2 / width, 0.0), 1.0)
    bottom = min(max(y2 / height, 0.0), 1.0)
    return [left, top, max(right - left, 0.0), max(bottom - top, 0.0)]


def to_detections(boxes, width: int, height: int):
    """A list of Aurum detection dicts to a `fo.Detections` field.

    A box that cannot be converted is skipped rather than guessed at, and the
    count of skipped boxes is not hidden: `build_dataset` reports it.
    """
    fo = require()
    out = []
    for box in boxes or []:
        try:
            bounding_box = to_relative(box.get("xyxy"), width, height)
        except (ValueError, TypeError):
            continue
        detection = fo.Detection(
            label=str(box.get("class_name") or "unknown"),
            bounding_box=bounding_box,
        )
        if box.get("confidence") is not None:
            detection.confidence = float(box["confidence"])
        if box.get("track_id") is not None:
            detection.index = int(box["track_id"])
        out.append(detection)
    return fo.Detections(detections=out)


def frame_size(path: str) -> tuple[int, int]:
    """A captured frame's pixel size. Read from the file, never assumed."""
    from PIL import Image

    with Image.open(path) as image:
        return image.width, image.height


def build_dataset(
    directory: Path | str = DEFAULT_DIR,
    name: str = DATASET_NAME,
    overwrite: bool = True,
) -> tuple[object, dict]:
    """Build a FiftyOne dataset from a capture directory.

    Returns `(dataset, report)`. The report names every sample that could not be
    added and why, because a dataset that silently dropped half its frames looks
    exactly like a model that only failed on the other half.
    """
    fo = require()
    records = read_manifest(directory)
    if overwrite and fo.dataset_exists(name):
        fo.delete_dataset(name)
    dataset = (
        fo.Dataset(name, persistent=True) if not fo.dataset_exists(name) else fo.load_dataset(name)
    )

    added, skipped = 0, []
    for record in records:
        path = record.get("frame_path")
        if not path or not Path(path).exists():
            skipped.append({"record": record.get("failure"), "why": f"no frame at {path!r}"})
            continue
        try:
            width, height = frame_size(path)
        except Exception as exc:
            skipped.append({"record": record.get("failure"), "why": f"unreadable frame: {exc}"})
            continue

        sample = fo.Sample(filepath=path)
        sample["failure"] = record.get("failure")
        sample["session_id"] = record.get("session_id")
        sample["item_id"] = record.get("item_id")
        sample["captured_at"] = record.get("timestamp")
        sample["decision"] = record.get("decision")
        sample["mass_status"] = record.get("mass_status")
        sample["price_status"] = record.get("price_status")
        sample["note"] = record.get("note")
        sample["predictions"] = to_detections(record.get("predictions"), width, height)
        if record.get("ground_truth"):
            sample["ground_truth"] = to_detections(record["ground_truth"], width, height)
        dataset.add_sample(sample)
        added += 1

    return dataset, {
        "dataset": name,
        "records_in_manifest": len(records),
        "samples_added": added,
        "skipped": skipped,
        "labelled": sum(1 for r in records if r.get("ground_truth")),
    }


def evaluate(
    dataset=None,
    name: str = DATASET_NAME,
    eval_key: str = "eval",
    directory: Path | str = DEFAULT_DIR,
) -> dict:
    """Run detection evaluation over whatever part of the dataset has labels.

    Refuses rather than reports zero when nothing is labelled: an evaluation
    over no ground truth produces a precision of 0.0 that reads like a broken
    model instead of an unlabelled dataset.
    """
    fo = require()
    if dataset is None:
        dataset = (
            fo.load_dataset(name) if fo.dataset_exists(name) else build_dataset(directory, name)[0]
        )

    labelled = dataset.exists("ground_truth")
    if len(labelled) == 0:
        return {
            "evaluated": False,
            "reason": (
                "No sample in this dataset carries ground truth. Label frames in the "
                "FiftyOne app, or import an annotated set, then run this again. An "
                "evaluation against no labels is not a score of zero - it is not a "
                "score at all."
            ),
            "samples": len(dataset),
        }

    results = labelled.evaluate_detections(
        "predictions",
        gt_field="ground_truth",
        eval_key=eval_key,
        compute_mAP=True,
    )
    return {
        "evaluated": True,
        "samples": len(labelled),
        "mAP": results.mAP(),
        "report": results.report(),
        "eval_key": eval_key,
        "false_positive_view": f"dataset.match(F('{eval_key}_fp') > 0)",
        "false_negative_view": f"dataset.match(F('{eval_key}_fn') > 0)",
    }


def launch(dataset=None, name: str = DATASET_NAME, directory: Path | str = DEFAULT_DIR):
    """Open the FiftyOne app on the error dataset. Blocks until closed."""
    fo = require()
    if dataset is None:
        dataset = (
            fo.load_dataset(name) if fo.dataset_exists(name) else build_dataset(directory, name)[0]
        )
    session = fo.launch_app(dataset)
    session.wait()
    return session


def summary(directory: Path | str = DEFAULT_DIR) -> dict:
    """What was captured, by category. Needs no FiftyOne at all."""
    records = read_manifest(directory)
    by_category: dict[str, int] = {}
    for record in records:
        key = record.get("failure", "UNKNOWN")
        by_category[key] = by_category.get(key, 0) + 1
    return {
        "directory": str(directory),
        "samples": len(records),
        "by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
        "labelled": sum(1 for r in records if r.get("ground_truth")),
        "categories_never_captured_at_runtime": sorted(
            str(f) for f in VisionFailure if str(f) not in by_category
        ),
        "fiftyone_installed": available(),
    }
