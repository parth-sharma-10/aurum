#!/usr/bin/env python3
"""Turn a raw bench capture into a labelled `data/raw/` source for ml.prepare.

    python scripts/label_bench_capture.py \
        --images data/bench_capture/perfboard \
        --out data/raw/aurum_bench --label PCB --review reports/bench_labels

Why the detector supplies the boxes: on the perfboard failure its *localisation*
is correct and only its *class* is wrong — it draws a tight box around the board
and calls it a CPU at 0.85. Reusing that geometry under the right class is
therefore accurate and contrastive: same pixels, corrected label, which is
exactly the CPU/PCB boundary the model has learned wrongly.

It is NOT blind trust. Every box is rendered into --review for a person to look
at before the source is fed to ml.prepare, and any frame the detector found
nothing in is reported rather than silently skipped.

`--label ""` writes an empty label file instead, i.e. a true background image.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.detector import AurumDetector  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def to_yolo(xyxy, w: int, h: int) -> tuple[float, float, float, float]:
    """Absolute xyxy -> normalised cx cy bw bh, clamped to the frame."""
    x1, y1, x2, y2 = xyxy
    x1, x2 = max(0, min(x1, w)), max(0, min(x2, w))
    y1, y2 = max(0, min(y1, h)), max(0, min(y2, h))
    return (
        ((x1 + x2) / 2) / w,
        ((y1 + y2) / 2) / h,
        (x2 - x1) / w,
        (y2 - y1) / h,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--images", type=Path, nargs="+", required=True)
    p.add_argument("--out", type=Path, required=True, help="data/raw/<key> to create")
    p.add_argument("--label", default="PCB", help='class for every box; "" = background')
    p.add_argument("--review", type=Path, help="render boxes here for human review")
    p.add_argument("--min-area", type=float, default=0.02, help="drop boxes under this frame frac")
    p.add_argument("--source-note", default="", help="recorded into _aurum_meta.json")
    p.add_argument(
        "--require-box",
        action="store_true",
        help=(
            "skip frames the detector found nothing in, instead of writing an empty "
            "label. Correct for a POSITIVE class: an image that contains a board and "
            "carries no box teaches the model the board is nothing."
        ),
    )
    a = p.parse_args(argv)

    frames = sorted(f for d in a.images for f in d.glob("*.jpg"))
    if not frames:
        print(f"No .jpg under {[str(d) for d in a.images]}", file=sys.stderr)
        return 1

    img_dir = a.out / "train" / "images"
    lbl_dir = a.out / "train" / "labels"
    img_dir.mkdir(parents=True, exist_ok=True)
    lbl_dir.mkdir(parents=True, exist_ok=True)
    if a.review:
        a.review.mkdir(parents=True, exist_ok=True)

    det = AurumDetector() if a.label else None
    rows, empty, skipped = [], 0, []

    for frame_path in frames:
        frame = cv2.imread(str(frame_path))
        if frame is None:
            print(f"unreadable: {frame_path.name}", file=sys.stderr)
            continue
        h, w = frame.shape[:2]
        shutil.copy2(frame_path, img_dir / frame_path.name)

        lines = []
        if det is not None:
            boxes = det.predict(frame).detections
            # Largest box wins: the object of interest is the whole board, and
            # a component sitting on it is not a separate target here.
            boxes.sort(
                key=lambda d: (d.xyxy[2] - d.xyxy[0]) * (d.xyxy[3] - d.xyxy[1]), reverse=True
            )
            for d in boxes[:1]:
                cx, cy, bw, bh = to_yolo(d.xyxy, w, h)
                if bw * bh < a.min_area:
                    continue
                lines.append(f"0 {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
                rows.append(
                    {
                        "image": frame_path.name,
                        "old_label": f"{d.cls} @ {d.conf:.3f}",
                        "proposed_label": a.label,
                        "reason": (
                            "Detector localisation is correct (tight box on the board) "
                            f"but the class is wrong: predicted {d.cls}."
                        ),
                        "bbox_xyxy": list(d.xyxy),
                    }
                )
                if a.review:
                    vis = frame.copy()
                    x1, y1, x2, y2 = d.xyxy
                    cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 220, 0), 3)
                    cv2.putText(
                        vis,
                        f"GT={a.label}  (was {d.cls} {d.conf:.2f})",
                        (x1, max(24, y1 - 10)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.8,
                        (0, 220, 0),
                        2,
                    )
                    cv2.imwrite(str(a.review / frame_path.name), vis)
        if not lines:
            empty += 1
            if a.require_box:
                # Nothing to say about this frame that is true. Do not ship it
                # as background under a positive-class source.
                (img_dir / frame_path.name).unlink(missing_ok=True)
                skipped.append(frame_path.name)
                continue
        (lbl_dir / f"{frame_path.stem}.txt").write_text("\n".join(lines) + ("\n" if lines else ""))

    names = [a.label] if a.label else []
    (a.out / "data.yaml").write_text(
        "train: train/images\nval: train/images\ntest: train/images\n"
        f"nc: {len(names)}\nnames: {json.dumps(names)}\n"
    )
    # Required, or ml/prepare.py:99-105 silently skips the whole directory.
    (a.out / "_aurum_meta.json").write_text(
        json.dumps(
            {
                "key": a.out.name,
                "source": "Aurum bench camera capture",
                "note": a.source_note
                or "Deployment failure frames captured from the rig; labelled by review.",
                "license": "internal",
                "url": "",
                "images": len(frames) - len(skipped),
                "skipped_no_box": skipped,
            },
            indent=2,
        )
        + "\n"
    )

    review_path = a.out / "label_review.json"
    review_path.write_text(json.dumps(rows, indent=2) + "\n")

    print(f"images   : {len(frames)} in, {len(frames) - len(skipped)} written -> {img_dir}")
    print(f"labelled : {len(rows)} as {a.label or 'background'}")
    print(
        f"no box   : {empty}"
        + (f" ({len(skipped)} skipped, need manual labels)" if skipped else "")
    )
    print(f"review   : {review_path}" + (f" + renders in {a.review}" if a.review else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
