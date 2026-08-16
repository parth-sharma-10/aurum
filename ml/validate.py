"""Verify the prepared dataset before spending GPU time on it.

This is the check that makes the reported test metrics believable. It proves,
rather than assumes, that no photograph reaches the test set from training:

  * no cluster spans two splits (the grouping contract)
  * no exact file duplicate across splits (SHA-256)
  * no perceptual near-duplicate across splits (independent re-check of the
    grouping, at a stricter threshold than prepare used)

It also catches the boring failures that silently wreck a training run: missing
label files, out-of-range class indices, denormalized or inverted boxes.

Exit code is non-zero if any check fails, so it can gate training in CI.

Usage:
    python -m ml.validate
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import imagehash
import yaml
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "aurum"
REPORTS = ROOT / "reports"
IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--hamming", type=int, default=5, help="cross-split perceptual distance treated as a leak"
    )
    args = ap.parse_args()

    if not (OUT / "data.yaml").exists():
        print("data/aurum/data.yaml missing. Run `python -m ml.prepare`.", file=sys.stderr)
        return 1

    cfg = yaml.safe_load((OUT / "data.yaml").read_text())
    classes = cfg["names"]
    nc = cfg["nc"]
    manifest = json.loads((REPORTS / "dataset_manifest.json").read_text())
    cluster_of = {m["file"]: m["cluster"] for m in manifest}

    failures: list[str] = []
    warnings: list[str] = []

    # --- structural checks -------------------------------------------------
    images: dict[str, list[Path]] = {}
    for split in ("train", "valid", "test"):
        d = OUT / split / "images"
        images[split] = sorted(p for p in d.iterdir() if p.suffix.lower() in IMG_EXT)
        print(f"{split:6s} {len(images[split]):5d} images")

    box_stats = Counter()
    n_boxes = 0
    for split, paths in images.items():
        for p in paths:
            lbl = OUT / split / "labels" / f"{p.stem}.txt"
            if not lbl.exists():
                failures.append(f"missing label file for {split}/{p.name}")
                continue
            for ln, line in enumerate(lbl.read_text().splitlines(), 1):
                parts = line.split()
                if not parts:
                    continue
                if len(parts) != 5:
                    failures.append(f"{split}/{lbl.name}:{ln} has {len(parts)} fields, expected 5")
                    continue
                ci = int(parts[0])
                cx, cy, w, h = (float(v) for v in parts[1:])
                if not 0 <= ci < nc:
                    failures.append(f"{split}/{lbl.name}:{ln} class index {ci} outside 0..{nc - 1}")
                if not (0 < w <= 1.0001 and 0 < h <= 1.0001):
                    failures.append(f"{split}/{lbl.name}:{ln} box w/h {w:.3f}x{h:.3f} out of range")
                if not (-0.001 <= cx <= 1.001 and -0.001 <= cy <= 1.001):
                    failures.append(
                        f"{split}/{lbl.name}:{ln} centre {cx:.3f},{cy:.3f} out of range"
                    )
                box_stats[(split, classes[ci] if 0 <= ci < nc else ci)] += 1
                n_boxes += 1

    # --- leakage check 1: cluster containment ------------------------------
    split_of_cluster: dict[str, str] = {}
    spanning: dict[str, set[str]] = defaultdict(set)
    for split, paths in images.items():
        for p in paths:
            c = cluster_of.get(p.name)
            if c is None:
                warnings.append(f"{split}/{p.name} not in manifest")
                continue
            spanning[c].add(split)
            split_of_cluster[c] = split
    multi = {c: s for c, s in spanning.items() if len(s) > 1}
    if multi:
        failures.append(
            f"{len(multi)} cluster(s) span multiple splits, e.g. {list(multi.items())[:3]}"
        )
    print(f"\ncluster containment : {len(spanning)} clusters, {len(multi)} spanning splits")

    # --- leakage check 2: exact duplicates across splits --------------------
    sha_index: dict[str, tuple[str, str]] = {}
    exact_leaks = []
    for split, paths in images.items():
        for p in paths:
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
            prev = sha_index.get(sha)
            if prev and prev[0] != split:
                exact_leaks.append((prev, (split, p.name)))
            else:
                sha_index[sha] = (split, p.name)
    if exact_leaks:
        failures.append(
            f"{len(exact_leaks)} exact duplicate image(s) across splits, e.g. {exact_leaks[:2]}"
        )
    print(f"exact duplicates    : {len(exact_leaks)} across splits")

    # --- leakage check 3: perceptual near-duplicates across splits ----------
    ph: dict[str, list[tuple[str, int]]] = {}
    for split, paths in images.items():
        vals = []
        for p in paths:
            with Image.open(p) as im:
                vals.append((p.name, int(str(imagehash.phash(im.convert("RGB"))), 16)))
        ph[split] = vals

    near_leaks = []
    train_bands: dict[tuple[int, int], list[tuple[str, int]]] = defaultdict(list)
    for name, h in ph["train"]:
        for b in range(8):
            train_bands[(b, (h >> (8 * b)) & 0xFF)].append((name, h))
    for split in ("valid", "test"):
        for name, h in ph[split]:
            cands = set()
            for b in range(8):
                for tn, th in train_bands.get((b, (h >> (8 * b)) & 0xFF), ()):
                    cands.add((tn, th))
            for tn, th in cands:
                if hamming(h, th) <= args.hamming:
                    near_leaks.append((split, name, tn, hamming(h, th)))
                    break
    if near_leaks:
        failures.append(
            f"{len(near_leaks)} near-duplicate(s) between train and "
            f"held-out at Hamming<={args.hamming}, e.g. {near_leaks[:3]}"
        )
    print(f"near-duplicates     : {len(near_leaks)} train<->heldout at Hamming<={args.hamming}")

    # --- class presence -----------------------------------------------------
    print(f"\n{'class':12s} {'train':>8s} {'valid':>8s} {'test':>8s}")
    for c in classes:
        row = [box_stats.get((s, c), 0) for s in ("train", "valid", "test")]
        print(f"{c:12s} {row[0]:8d} {row[1]:8d} {row[2]:8d}")
        if row[2] == 0:
            failures.append(f"class {c} has no instances in the test set")
        elif row[2] < 30:
            warnings.append(
                f"class {c} has only {row[2]} test instances; its per-class metrics will be noisy"
            )
    print(
        f"{'TOTAL':12s} {sum(box_stats.get(('train', c), 0) for c in classes):8d} "
        f"{sum(box_stats.get(('valid', c), 0) for c in classes):8d} "
        f"{sum(box_stats.get(('test', c), 0) for c in classes):8d}"
    )

    # --- verdict ------------------------------------------------------------
    report = {
        "images": {s: len(v) for s, v in images.items()},
        "boxes": n_boxes,
        "clusters": len(spanning),
        "clusters_spanning_splits": len(multi),
        "exact_duplicates_across_splits": len(exact_leaks),
        "near_duplicates_train_to_heldout": len(near_leaks),
        "hamming_threshold": args.hamming,
        "failures": failures,
        "warnings": warnings,
    }
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "dataset_validation.json").write_text(json.dumps(report, indent=2))

    print()
    for w in warnings:
        print(f"WARN  {w}")
    for f in failures:
        print(f"FAIL  {f}")
    if failures:
        print(f"\n{len(failures)} check(s) FAILED — do not train on this dataset.")
        return 1
    print("\nAll leakage and integrity checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
