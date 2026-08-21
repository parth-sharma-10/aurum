"""Run the model over images it has no provenance relationship with.

Held-out test images came from the same Universe projects as the training
images: same photographers, same benches, same lighting. Good numbers there say
the model generalizes across *photographs*, not that it generalizes to a scrap
dealer's table.

This script runs the detector over a folder of images supplied by the operator —
ideally photos taken on the day, on the actual bench — and writes annotated
outputs plus a summary of what was found and at what confidence. It reports
detections, not accuracy: there are no ground-truth boxes for these images, so
the output is evidence to look at, not a metric to quote.

Usage:
    python -m ml.realworld --path ~/Desktop/bench_photos
    python -m ml.realworld --path photos --conf 0.25
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import cv2

from app.dashboard import draw_detections
from app.detector import DEFAULT_WEIGHTS, AurumDetector

ROOT = Path(__file__).resolve().parent.parent
IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".heic"}


def _rel(p: Path) -> str:
    """Repo-relative path for the summary. An absolute path would bake one
    developer's home directory into a committed report."""
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--path", required=True, help="folder of unseen photographs")
    ap.add_argument("--weights", default=str(DEFAULT_WEIGHTS))
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--out", default=str(ROOT / "reports" / "realworld"), help="annotated images")
    ap.add_argument(
        "--summary-out",
        default=str(ROOT / "reports" / "realworld_summary.json"),
        help="where to write the JSON summary; give a second path when sweeping --conf "
        "so a comparison run does not overwrite the headline one",
    )
    args = ap.parse_args()

    src = Path(args.path).expanduser()
    images = sorted(
        p for p in ([src] if src.is_file() else src.rglob("*")) if p.suffix.lower() in IMG_EXT
    )
    if not images:
        raise SystemExit(f"No images found under {src}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    det = AurumDetector(args.weights, conf=args.conf)
    det.warmup()
    print(f"{det.model_version} — {len(images)} image(s) from {src}\n")

    totals = Counter()
    confs: dict[str, list[float]] = {c: [] for c in det.classes}
    rows = []
    n_empty = 0

    for p in images:
        img = cv2.imread(str(p))
        if img is None:
            print(f"  ! could not read {p.name}")
            continue
        res = det.predict(img)
        cv2.imwrite(str(out / p.name), draw_detections(img, res.detections))

        counts = res.counts
        totals.update(counts)
        for d in res.detections:
            confs[d.cls].append(d.conf)
        if not res.detections:
            n_empty += 1

        rows.append(
            {
                "image": p.name,
                "counts": counts,
                "n_objects": len(res.detections),
                "mean_confidence": round(res.mean_confidence, 4),
                "inference_ms": round(res.inference_ms, 1),
            }
        )
        desc = ", ".join(f"{k}×{v}" for k, v in sorted(counts.items())) or "nothing detected"
        print(f"  {p.name:45.45s} {desc}")

    print(f"\n{'class':12s} {'detections':>11s} {'mean conf':>11s}")
    for c in det.classes:
        cs = confs[c]
        mean = sum(cs) / len(cs) if cs else 0.0
        print(
            f"{c:12s} {totals.get(c, 0):11d} {mean:11.3f}"
            if cs
            else f"{c:12s} {totals.get(c, 0):11d} {'--':>11s}"
        )
    print(f"\nImages with no detection: {n_empty}/{len(rows)}")

    summary = {
        "model_version": det.model_version,
        "weights": _rel(Path(args.weights).resolve()),
        "conf_threshold": args.conf,
        "source": str(src),
        "n_images": len(rows),
        "images_with_no_detection": n_empty,
        "total_detections": dict(totals),
        "mean_confidence_per_class": {
            c: (round(sum(v) / len(v), 4) if v else None) for c, v in confs.items()
        },
        "mean_inference_ms": round(sum(r["inference_ms"] for r in rows) / max(1, len(rows)), 1),
        "per_image": rows,
        "caveat": (
            "Detections only. These images have no ground-truth annotations, so "
            "nothing here is an accuracy measurement and no figure from this run "
            "may be quoted as model accuracy."
        ),
    }
    summary_path = Path(args.summary_out)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"\nAnnotated images -> {out}")
    print(f"Summary -> {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
