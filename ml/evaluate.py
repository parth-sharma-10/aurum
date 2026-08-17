"""Evaluate the trained model on the held-out test split.

Reports precision, recall, mAP@50 and mAP@50:95 overall and per class, straight
from Ultralytics' validator — nothing is recomputed by hand or rounded up. Also
saves the confusion matrix and PR curves, and writes out representative correct
detections and representative failures, because the failures are the part that
tells you what the model actually learned.

Usage:
    python -m ml.evaluate
    python -m ml.evaluate --split valid --weights models/aurum_vision_v0_1_best.pt
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import cv2
import yaml
from ultralytics import YOLO

from app.detector import resolve_imgsz

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "aurum" / "data.yaml"
REPORTS = ROOT / "reports"
DEFAULT_WEIGHTS = ROOT / "models" / "aurum_vision_v0_1_best.pt"


def iou(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def load_gt(label_path: Path, w: int, h: int) -> list[tuple[int, list[float]]]:
    out = []
    if not label_path.exists():
        return out
    for line in label_path.read_text().splitlines():
        p = line.split()
        if len(p) != 5:
            continue
        c = int(p[0])
        cx, cy, bw, bh = (float(v) for v in p[1:])
        out.append(
            (c, [(cx - bw / 2) * w, (cy - bh / 2) * h, (cx + bw / 2) * w, (cy + bh / 2) * h])
        )
    return out


def qualitative(
    model: YOLO,
    split_dir: Path,
    names: list[str],
    conf: float,
    out_dir: Path,
    imgsz: int,
    n_each: int = 12,
) -> dict:
    """Split test images into clean successes and instructive failures."""
    img_dir, lbl_dir = split_dir / "images", split_dir / "labels"
    good_dir, bad_dir = out_dir / "correct", out_dir / "failures"
    for d in (good_dir, bad_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    scored = []
    for img_path in sorted(img_dir.iterdir()):
        if img_path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            continue
        img = cv2.imread(str(img_path))
        if img is None:
            continue
        h, w = img.shape[:2]
        gt = load_gt(lbl_dir / f"{img_path.stem}.txt", w, h)
        if not gt:
            continue
        res = model.predict(img, conf=conf, imgsz=imgsz, verbose=False)[0]
        preds = []
        if res.boxes is not None and len(res.boxes):
            for bb, cc, kk in zip(
                res.boxes.xyxy.cpu().numpy(),
                res.boxes.conf.cpu().numpy(),
                res.boxes.cls.cpu().numpy().astype(int),
                strict=True,
            ):
                preds.append((int(kk), float(cc), bb.tolist()))

        matched_gt, matched_pred = set(), set()
        for gi, (gc, gb) in enumerate(gt):
            best, bj = 0.0, None
            for pj, (pc, _pconf, pb) in enumerate(preds):
                if pj in matched_pred or pc != gc:
                    continue
                v = iou(gb, pb)
                if v > best:
                    best, bj = v, pj
            if bj is not None and best >= 0.5:
                matched_gt.add(gi)
                matched_pred.add(bj)

        tp = len(matched_gt)
        fn = len(gt) - tp
        fp = len(preds) - len(matched_pred)
        score = tp / max(1, len(gt)) - 0.25 * fp
        scored.append((score, fp, fn, img_path, gt, preds))

    scored.sort(key=lambda t: -t[0])
    summary = {"correct": [], "failures": []}

    def render(item, dest):
        _, fp, fn, img_path, gt, preds = item
        img = cv2.imread(str(img_path))
        for gc, gb in gt:  # ground truth: thin white
            cv2.rectangle(
                img, (int(gb[0]), int(gb[1])), (int(gb[2]), int(gb[3])), (230, 230, 230), 1
            )
            cv2.putText(
                img,
                f"GT:{names[gc]}",
                (int(gb[0]), int(gb[1]) - 4),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (230, 230, 230),
                1,
                cv2.LINE_AA,
            )
        for pc, pconf, pb in preds:  # prediction: gold
            cv2.rectangle(
                img, (int(pb[0]), int(pb[1])), (int(pb[2]), int(pb[3])), (60, 175, 214), 2
            )
            cv2.putText(
                img,
                f"{names[pc]} {pconf:.2f}",
                (int(pb[0]), int(pb[3]) + 16),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (60, 175, 214),
                2,
                cv2.LINE_AA,
            )
        cv2.imwrite(str(dest / img_path.name), img)
        return {
            "image": img_path.name,
            "false_positives": fp,
            "false_negatives": fn,
            "n_gt": len(gt),
            "n_pred": len(preds),
        }

    for item in scored[:n_each]:
        summary["correct"].append(render(item, good_dir))
    for item in [s for s in scored if s[1] or s[2]][-n_each:]:
        summary["failures"].append(render(item, bad_dir))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--weights", default=str(DEFAULT_WEIGHTS))
    ap.add_argument("--split", default="test", choices=["test", "valid"])
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument(
        "--imgsz",
        type=int,
        default=None,
        help="inference size; defaults to the size the checkpoint was trained at",
    )
    args = ap.parse_args()

    weights = Path(args.weights).resolve()
    if not weights.exists():
        raise SystemExit(f"No weights at {weights}. Run `python -m ml.train` first.")

    names = yaml.safe_load(DATA.read_text())["names"]
    model = YOLO(str(weights))
    imgsz = resolve_imgsz(model, args.imgsz)

    print(f"Evaluating {weights.name} on the {args.split} split at {imgsz} px…")
    # Ultralytics looks up `split` as a key in data.yaml and only path-resolves
    # the canonical train/val/test keys, so the validation split must be asked
    # for as "val" even though its directory is data/aurum/valid.
    ul_split = "val" if args.split == "valid" else args.split
    m = model.val(
        data=str(DATA),
        split=ul_split,
        imgsz=imgsz,
        conf=0.001,
        iou=0.6,
        plots=True,
        verbose=True,
        project=str(ROOT / "runs"),
        name=f"eval_{args.split}",
        exist_ok=True,
    )

    box = m.box
    overall = {
        "precision": float(box.mp),
        "recall": float(box.mr),
        "mAP50": float(box.map50),
        "mAP50_95": float(box.map),
    }
    per_class = {}
    for i, c in enumerate(names):
        try:
            p, r, ap50, ap = box.class_result(i)
            per_class[c] = {
                "precision": float(p),
                "recall": float(r),
                "mAP50": float(ap50),
                "mAP50_95": float(ap),
            }
        except Exception:
            per_class[c] = None

    print(f"\n{'class':12s} {'P':>8s} {'R':>8s} {'mAP50':>8s} {'mAP50-95':>9s}")
    print(
        f"{'ALL':12s} {overall['precision']:8.3f} {overall['recall']:8.3f} "
        f"{overall['mAP50']:8.3f} {overall['mAP50_95']:9.3f}"
    )
    for c, v in per_class.items():
        if v:
            print(
                f"{c:12s} {v['precision']:8.3f} {v['recall']:8.3f} "
                f"{v['mAP50']:8.3f} {v['mAP50_95']:9.3f}"
            )

    print("\nRendering qualitative examples…")
    qual = qualitative(
        model,
        ROOT / "data" / "aurum" / args.split,
        names,
        args.conf,
        REPORTS / f"{args.split}_predictions",
        imgsz,
    )

    eval_dir = ROOT / "runs" / f"eval_{args.split}"
    REPORTS.mkdir(exist_ok=True)
    copied = []
    for art in (
        "confusion_matrix.png",
        "confusion_matrix_normalized.png",
        "BoxPR_curve.png",
        "BoxF1_curve.png",
        "BoxP_curve.png",
        "BoxR_curve.png",
    ):
        src = eval_dir / art
        if src.exists():
            shutil.copy2(src, REPORTS / art)
            copied.append(art)

    stats = json.loads((REPORTS / "dataset_stats.json").read_text())
    out = {
        "weights": str(weights.relative_to(ROOT)) if weights.is_relative_to(ROOT) else str(weights),
        "split": args.split,
        "imgsz": imgsz,
        "n_images": stats["splits"][args.split]["images"],
        "n_instances": stats["splits"][args.split]["boxes"],
        "conf_threshold_for_examples": args.conf,
        "metrics_overall": overall,
        "metrics_per_class": per_class,
        "artifacts": copied,
        "qualitative": qual,
    }
    (REPORTS / f"{args.split}_metrics.json").write_text(json.dumps(out, indent=2))
    print(f"\nWrote reports/{args.split}_metrics.json")
    print(f"Artifacts: {copied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
