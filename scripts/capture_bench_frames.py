#!/usr/bin/env python3
"""Grab RAW frames from the bench camera, and prove they are actually diverse.

Single burst (non-interactive, usable from a script):

    python scripts/capture_bench_frames.py --out data/bench_capture/perfboard --n 12

Guided multi-pose capture (run this yourself — it waits for you at the bench):

    python scripts/capture_bench_frames.py --out data/bench_capture/perfboard \\
        --poses 8 --per-pose 3 --guided

Diversity report over what has already been captured:

    python scripts/capture_bench_frames.py --out data/bench_capture --report-only

The dashboard's `/session/frame` is NOT usable for capture: it serves
`annotate(frame, ...)`, so every frame carries a drawn box and a class label.
Training on those teaches the model about rectangles. This writes what the
sensor actually saw.

The camera is exclusive on macOS, so stop the Aurum session first:

    curl -X POST localhost:8000/session/stop

WHY POSES, NOT FRAMES. `ml/prepare.py` clusters by SHA-256 and perceptual hash
and then splits *clusters*, never images. Ten consecutive frames of one pose are
one training example with nine duplicates. `--poses` exists to make the unit of
capture the thing the pipeline actually counts, and `--report-only` re-runs the
repository's own `merge_groups_by_similarity` so the claim "we gained N examples"
can be checked rather than asserted.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_HAMMING = 5  # matches ml/prepare.py's --hamming default


def _open(index: int, width: int, height: int):
    cam = cv2.VideoCapture(index)
    if not cam.isOpened():
        print(
            f"Could not open camera {index}. Something else holds it — stop the "
            "Aurum session first (POST /session/stop).",
            file=sys.stderr,
        )
        return None
    cam.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cam.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return cam


def _burst(cam, out: Path, stem: str, count: int, delay: float) -> int:
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for i in range(count):
        ok, frame = cam.read()
        if not ok or frame is None:
            print(f"  frame {i}: camera returned nothing", file=sys.stderr)
            continue
        path = out / f"{stem}_{written:02d}.jpg"
        if cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 92]):
            written += 1
            print(f"  {path.name}  {frame.shape[1]}x{frame.shape[0]}", flush=True)
        time.sleep(delay)
    return written


def capture(args) -> int:
    cam = _open(args.index, args.width, args.height)
    if cam is None:
        return 1
    total = 0
    try:
        if args.poses <= 1 and not args.guided:
            total = _burst(cam, args.out, args.out.name, args.n, args.delay)
        else:
            for pose in range(1, args.poses + 1):
                print(f"\n=== pose {pose}/{args.poses} ===")
                print(
                    "  Reposition the object so it looks GENUINELY different — "
                    "rotate it, flip it, move it nearer/further, off-centre, "
                    "partly out of frame, partly covered."
                )
                try:
                    input("  Press Enter when ready (Ctrl-C to stop early)… ")
                except (EOFError, KeyboardInterrupt):
                    print("\n  stopped early")
                    break
                # Let exposure/auto-focus settle after the scene changed.
                for _ in range(5):
                    cam.read()
                total += _burst(
                    cam, args.out, f"{args.out.name}_pose{pose:02d}", args.per_pose, args.delay
                )
    finally:
        cam.release()
    print(f"\n{total} raw frames -> {args.out}")
    return 0 if total else 1


def report(root: Path, hamming: int) -> int:
    """Cluster what is on disk using the repository's own dedup, not a new one."""
    from ml.prepare import merge_groups_by_similarity

    dirs = [root] if any(root.glob("*.jpg")) else sorted(p for p in root.iterdir() if p.is_dir())
    if not dirs:
        print(f"No images under {root}", file=sys.stderr)
        return 1

    records = []
    for d in dirs:
        for img in sorted(d.glob("*.jpg")):
            # One group per pose, so the report answers "how many distinct
            # LOOKS do we have", not "how many shutter presses".
            pose = img.stem.rsplit("_", 1)[0]
            records.append({"path": img, "group": f"{d.name}::{pose}"})

    if not records:
        print(f"No images under {root}", file=sys.stderr)
        return 1

    merge_groups_by_similarity(records, hamming)

    print(f"\n{'source':<34} {'raw':>5} {'poses':>6} {'clusters':>9}")
    print("-" * 58)
    grand_raw = 0
    grand_clusters: set[str] = set()
    for d in dirs:
        rs = [r for r in records if r["path"].parent == d]
        if not rs:
            continue
        poses = {r["group"] for r in rs}
        clusters = {r["cluster"] for r in rs}
        grand_raw += len(rs)
        grand_clusters |= clusters
        flag = "  <-- collapsed" if len(clusters) < len(poses) else ""
        print(f"{d.name:<34} {len(rs):>5} {len(poses):>6} {len(clusters):>9}{flag}")
    print("-" * 58)
    print(f"{'TOTAL':<34} {grand_raw:>5} {'':>6} {len(grand_clusters):>9}")
    print(
        "\nClusters are what ml/prepare.py counts. Frames that collapse into one "
        "cluster are one training example, however many times the shutter fired."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True, help="directory to write into")
    p.add_argument("--n", type=int, default=12, help="frames, single-burst mode")
    p.add_argument("--poses", type=int, default=1, help="number of distinct poses")
    p.add_argument("--per-pose", type=int, default=3, help="frames per pose")
    p.add_argument("--guided", action="store_true", help="prompt between poses")
    p.add_argument("--index", type=int, default=0, help="camera index")
    p.add_argument("--delay", type=float, default=0.35, help="seconds between frames")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--report-only", action="store_true", help="cluster what already exists")
    p.add_argument("--hamming", type=int, default=DEFAULT_HAMMING)
    a = p.parse_args(argv)

    if a.report_only:
        return report(a.out, a.hamming)
    rc = capture(a)
    if rc == 0:
        report(a.out.parent if a.out.parent.name == "bench_capture" else a.out, a.hamming)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
