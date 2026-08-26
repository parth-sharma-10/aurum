#!/usr/bin/env python3
"""Where does the model swap one class for another, in the data we already have?

    python scripts/class_confusion_audit.py --split test

The canonical bench failure is a CPU/PCB boundary defect seen twice over:

    perfboard hole-grid  -> CPU 0.85     (bench)
    real CPU pin-side    -> PCB 0.90     (data/realworld)

This asks whether the same mechanism is already visible in the shipped dataset
rather than only on the bench. It is a per-image class-set comparison, not a
detection metric: `ml/evaluate.py` owns mAP and should keep owning it. What this
adds is the *named pair* — which class gets swapped for which, and in which
images — so the pairs can be curated as a coherent hard-negative category.

Greedy IoU matching is deliberately NOT used. A swap here means "the image
contains a GT X and the model reported a Y that no GT explains", which is the
question being asked; box-level attribution is `ml/evaluate.py`'s job.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.detector import AurumDetector  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def gt_classes(label_path: Path, names: list[str]) -> Counter:
    out: Counter = Counter()
    if not label_path.exists():
        return out
    for row in label_path.read_text().split("\n"):
        parts = row.split()
        if len(parts) >= 5:
            idx = int(parts[0])
            if 0 <= idx < len(names):
                out[names[idx]] += 1
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--split", default="test", choices=("test", "valid", "train"))
    p.add_argument("--conf", type=float, default=0.35)
    p.add_argument("--out", type=Path, default=ROOT / "reports" / "class_confusion.json")
    p.add_argument("--top", type=int, default=12, help="worst examples to list per pair")
    a = p.parse_args(argv)

    import yaml

    names = yaml.safe_load((ROOT / "data/aurum/data.yaml").read_text())["names"]
    split_dir = ROOT / "data" / "aurum" / a.split
    images = sorted((split_dir / "images").glob("*.jpg"))
    if not images:
        print(f"No images in {split_dir}", file=sys.stderr)
        return 1

    det = AurumDetector(conf=a.conf)
    print(f"{len(images)} images, weights={det.weights.name}, conf={a.conf}", flush=True)

    swaps: Counter = Counter()
    examples: dict[str, list] = {}
    missed: Counter = Counter()
    spurious: Counter = Counter()
    background_fp = []

    for n, img_path in enumerate(images, 1):
        if n % 50 == 0:
            print(f"  {n}/{len(images)}", flush=True)
        frame = cv2.imread(str(img_path))
        if frame is None:
            continue
        gt = gt_classes(split_dir / "labels" / f"{img_path.stem}.txt", names)
        preds = det.predict(frame).detections
        pred_counts = Counter(d.cls for d in preds)

        # A class the model reported that no ground-truth box can account for.
        over = {
            c: pred_counts[c] - gt.get(c, 0) for c in pred_counts if pred_counts[c] > gt.get(c, 0)
        }
        # A class present in ground truth that the model under-reported.
        under = {c: gt[c] - pred_counts.get(c, 0) for c in gt if gt[c] > pred_counts.get(c, 0)}

        for c, k in over.items():
            spurious[c] += k
        for c, k in under.items():
            missed[c] += k

        if not gt and preds:
            best = max(preds, key=lambda d: d.conf)
            background_fp.append(
                {"image": img_path.name, "class": best.cls, "confidence": round(best.conf, 4)}
            )

        # A swap: something was missed AND something unexplained appeared.
        for miss_cls in under:
            for over_cls in over:
                if miss_cls == over_cls:
                    continue
                pair = f"{miss_cls}->{over_cls}"
                swaps[pair] += 1
                conf = max((d.conf for d in preds if d.cls == over_cls), default=0.0)
                examples.setdefault(pair, []).append(
                    {"image": img_path.name, "confidence": round(conf, 4)}
                )

    for pair in examples:
        examples[pair].sort(key=lambda e: e["confidence"], reverse=True)
        examples[pair] = examples[pair][: a.top]

    report = {
        "split": a.split,
        "weights": str(det.weights.name),
        "conf": a.conf,
        "n_images": len(images),
        "note": (
            "Per-image class-set comparison, not a detection metric. A 'swap' is an "
            "image where class X was under-reported and class Y appeared unexplained. "
            "mAP and box-level TP/FP remain ml/evaluate.py's responsibility."
        ),
        "swaps_by_pair": dict(swaps.most_common()),
        "unexplained_predictions_by_class": dict(spurious.most_common()),
        "under_reported_by_class": dict(missed.most_common()),
        "false_positives_on_background_images": sorted(
            background_fp, key=lambda e: e["confidence"], reverse=True
        )[: a.top],
        "n_background_images_with_a_detection": len(background_fp),
        "examples_by_pair": examples,
    }
    a.out.write_text(json.dumps(report, indent=2) + "\n")

    print(f"\n{'swap':<22} {'images':>7}")
    print("-" * 31)
    for pair, k in swaps.most_common(12):
        print(f"{pair:<22} {k:>7}")
    print(f"\nunexplained predictions : {dict(spurious.most_common())}")
    print(f"under-reported          : {dict(missed.most_common())}")
    print(f"background images with a detection: {len(background_fp)}")
    if background_fp:
        top = sorted(background_fp, key=lambda e: e["confidence"], reverse=True)[:5]
        for e in top:
            print(f"   {e['confidence']:.3f}  {e['class']:<10} {e['image'][:58]}")
    print(f"\n-> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
