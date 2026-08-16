"""Normalize the raw datasets into one Aurum-class YOLO dataset, leak-free.

The whole point of this file is the split. Two things in the raw data will
silently destroy a random split:

  1. Roboflow emits augmented copies of the same source photograph as separate
     files, named `<stem>_jpg.rf.<hash>.jpg`. Three rotations of one RAM stick
     scattered across train/valid/test is not a held-out test set.
  2. The same photograph appears in more than one Universe project, because
     people re-upload each other's data.

So images are grouped before they are split: first by source stem within a
dataset, then groups are merged across datasets by perceptual hash. The split is
performed over *groups*, never over images, which is what makes the test set
genuinely unseen.

Usage:
    python -m ml.prepare
    python -m ml.prepare --seed 7 --hamming 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

import imagehash
import numpy as np
import yaml
from PIL import Image

from ml.labels import UnknownLabelError, load_label_map

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "aurum"
REPORTS = ROOT / "reports"

SPLITS = {"train": 0.70, "valid": 0.20, "test": 0.10}
IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# Roboflow encodes the original filename before the `.rf.` marker.
RF_STEM = re.compile(r"^(.*?)\.rf\.[0-9a-f]{6,}$", re.IGNORECASE)


def source_stem(path: Path) -> str:
    """Strip Roboflow's augmentation suffix to recover the source identity."""
    m = RF_STEM.match(path.stem)
    return (m.group(1) if m else path.stem).lower()


# ---------------------------------------------------------------------------
# Union-find over image groups
# ---------------------------------------------------------------------------
class DisjointSet:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# ---------------------------------------------------------------------------
# Loading + normalization
# ---------------------------------------------------------------------------
def load_records(lm) -> tuple[list[dict], dict]:
    """Read every raw dataset, remap labels, and report what was dropped."""
    records: list[dict] = []
    audit = {
        "datasets": {},
        "dropped_labels": Counter(),
        "kept_labels": Counter(),
        "unknown_labels": Counter(),
    }

    for ds_dir in sorted(p for p in RAW.iterdir() if p.is_dir()):
        meta_path = ds_dir / "_aurum_meta.json"
        if not meta_path.exists():
            print(
                f"  ! {ds_dir.name}: no _aurum_meta.json, skipping (not produced by ml.ingest)",
                file=sys.stderr,
            )
            continue
        meta = json.loads(meta_path.read_text())
        names = yaml.safe_load((ds_dir / "data.yaml").read_text())["names"]
        if isinstance(names, dict):
            names = [names[i] for i in sorted(names)]

        # Resolve this dataset's index -> Aurum class once, up front, so an
        # unreviewed label fails loudly here instead of silently per-box.
        idx_to_aurum: dict[int, str | None] = {}
        for i, raw_name in enumerate(names):
            try:
                idx_to_aurum[i] = lm.resolve(raw_name)
            except UnknownLabelError as exc:
                audit["unknown_labels"][f"{ds_dir.name}:{raw_name}"] += 1
                raise SystemExit(f"\n{exc}\n  (dataset: {ds_dir.name})") from exc

        n_img = n_box = 0
        for split in ("train", "valid", "test"):
            img_dir, lbl_dir = ds_dir / split / "images", ds_dir / split / "labels"
            if not img_dir.is_dir():
                continue
            for img in sorted(img_dir.iterdir()):
                if img.suffix.lower() not in IMG_EXT:
                    continue
                lbl = lbl_dir / f"{img.stem}.txt"
                boxes = []
                if lbl.exists():
                    for line in lbl.read_text().splitlines():
                        parts = line.split()
                        if len(parts) < 5:
                            continue
                        idx = int(float(parts[0]))
                        raw_name = names[idx] if idx < len(names) else f"<{idx}>"
                        aurum = idx_to_aurum.get(idx)
                        if aurum is None:
                            audit["dropped_labels"][raw_name] += 1
                            continue
                        audit["kept_labels"][raw_name] += 1
                        # Roboflow segmentation exports carry polygons; take the
                        # bounding box of the polygon so seg projects are usable.
                        coords = [float(v) for v in parts[1:]]
                        if len(coords) > 4:
                            xs, ys = coords[0::2], coords[1::2]
                            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
                            cx, cy, w, h = (x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0
                        else:
                            cx, cy, w, h = coords[:4]
                        if w <= 0 or h <= 0:
                            continue
                        boxes.append((aurum, cx, cy, w, h))
                        n_box += 1
                records.append(
                    {
                        "path": img,
                        "dataset": ds_dir.name,
                        "orig_split": split,
                        "group": f"{ds_dir.name}::{source_stem(img)}",
                        "boxes": boxes,
                    }
                )
                n_img += 1

        audit["datasets"][ds_dir.name] = {
            "images": n_img,
            "boxes_kept": n_box,
            "license": meta.get("license"),
            "url": meta.get("url"),
        }
        print(f"  {ds_dir.name:28s} {n_img:6d} images, {n_box:6d} Aurum boxes")

    return records, audit


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------
def merge_groups_by_similarity(records: list[dict], max_hamming: int) -> dict:
    """Union image groups that are the same photograph across datasets.

    Exact byte duplicates are caught by SHA-256. Near-duplicates (re-encoded,
    resized, re-compressed re-uploads) are caught by perceptual hash using
    multi-index bucketing: two 64-bit hashes within Hamming distance 7 must
    share at least one of eight 8-bit bands, so only those pairs are compared.
    """
    ds = DisjointSet()
    for r in records:
        ds.find(r["group"])

    # Every image is hashed, not one representative per group. A group holds
    # several augmentations of one photo, and only some of them may resemble an
    # image in another dataset; hashing only the first copy lets the others slip
    # through. (Verified: representative-only hashing left 18 near-duplicates
    # spanning train and held-out.)
    n_img = len(records)
    print(f"  hashing {n_img} images…")
    sha_to_group: dict[str, str] = {}
    phashes: list[tuple[str, int]] = []
    for i, r in enumerate(records):
        if i and i % 2000 == 0:
            print(f"    {i}/{n_img}")
        gid = r["group"]
        try:
            sha = hashlib.sha256(r["path"].read_bytes()).hexdigest()
            if sha in sha_to_group:
                ds.union(sha_to_group[sha], gid)
            else:
                sha_to_group[sha] = gid
            with Image.open(r["path"]) as im:
                ph = int(str(imagehash.phash(im.convert("RGB"))), 16)
            phashes.append((gid, ph))
        except Exception as exc:
            print(f"    ! hash failed for {r['path'].name}: {exc}", file=sys.stderr)

    # Exact all-pairs Hamming over the *distinct* hashes. An earlier version
    # bucketed by 8-bit bands and skipped oversized buckets for speed; that
    # skipped precisely the buckets full of white-background product shots,
    # which is where the duplicates actually were. Product photography makes
    # the pigeonhole shortcut worthless here, so do the honest O(n^2) scan —
    # on ~10^4 distinct hashes it is seconds of vectorised work.
    by_hash: dict[int, list[str]] = defaultdict(list)
    for gid, ph in phashes:
        by_hash[ph].append(gid)
    for gids in by_hash.values():  # identical hash => same image
        for other in gids[1:]:
            ds.union(gids[0], other)

    uniq = np.array(sorted(by_hash), dtype=np.uint64)
    print(f"    {len(uniq)} distinct perceptual hashes; exact pairwise scan…")
    popcnt = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)

    n_merged = 0
    chunk = 256
    for start in range(0, len(uniq), chunk):
        block = uniq[start : start + chunk]
        xor = block[:, None] ^ uniq[None, :]
        dist = popcnt[xor.view(np.uint8).reshape(xor.shape + (8,))].sum(axis=-1)
        # Only look forward to avoid doing every pair twice.
        rows, cols = np.nonzero(dist <= max_hamming)
        for r, c in zip(rows, cols, strict=True):
            gi_all, gj_all = by_hash[int(block[r])], by_hash[int(uniq[c])]
            if start + r >= c:
                continue
            gi, gj = gi_all[0], gj_all[0]
            if ds.find(gi) != ds.find(gj):
                ds.union(gi, gj)
                n_merged += 1

    for r in records:
        r["cluster"] = ds.find(r["group"])

    n_groups = len({r["group"] for r in records})
    n_clusters = len({r["cluster"] for r in records})
    print(
        f"  {n_groups} stem-groups -> {n_clusters} clusters "
        f"({n_merged} near-duplicate merges at Hamming<={max_hamming})"
    )
    return {
        "images_hashed": n_img,
        "stem_groups": n_groups,
        "clusters": n_clusters,
        "merges": n_merged,
    }


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------
def split_clusters(records: list[dict], classes: list[str], seed: int) -> dict[str, str]:
    """Assign whole clusters to splits, balancing the rarest classes first.

    Clusters are handled rarest-class-first and each is given to whichever split
    is furthest below its quota for that class. Rare classes (CPU, Connector)
    therefore get their proportional share instead of landing wherever a random
    shuffle happened to put them.
    """
    by_cluster: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        for cls, *_ in r["boxes"]:
            by_cluster[r["cluster"]][cls] += 1
        by_cluster[r["cluster"]]  # ensure background clusters exist

    totals = Counter()
    for c in by_cluster.values():
        totals.update(c)
    rarity = {cls: totals.get(cls, 0) for cls in classes}

    def cluster_key(item):
        cid, counts = item
        present = [c for c in classes if counts.get(c)]
        # Rarest present class first; background clusters last.
        return (min((rarity[c] for c in present), default=10**9), -sum(counts.values()))

    rng = random.Random(seed)
    ordered = sorted(by_cluster.items(), key=cluster_key)
    # Break ties deterministically but without alphabetical bias.
    rng.shuffle(ordered)
    ordered.sort(key=cluster_key)

    assigned: dict[str, str] = {}
    have: dict[str, Counter] = {s: Counter() for s in SPLITS}
    n_have = Counter()
    n_total = len(ordered)

    for cid, counts in ordered:
        present = [c for c in classes if counts.get(c)]
        if present:
            target = min(present, key=lambda c: rarity[c])
            deficits = {
                s: SPLITS[s] * (sum(have[x][target] for x in SPLITS) + counts[target])
                - have[s][target]
                for s in SPLITS
            }
        else:  # background-only cluster: balance on raw image count
            deficits = {s: SPLITS[s] * (sum(n_have.values()) + 1) - n_have[s] for s in SPLITS}
        best = max(deficits, key=lambda s: (deficits[s], SPLITS[s]))
        assigned[cid] = best
        have[best].update(counts)
        n_have[best] += 1

    assert len(assigned) == n_total
    return assigned


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument(
        "--hamming", type=int, default=5, help="perceptual-hash distance treated as the same photo"
    )
    ap.add_argument(
        "--max-background-frac",
        type=float,
        default=0.10,
        help="cap on images with no Aurum object, per split",
    )
    args = ap.parse_args()

    lm = load_label_map()
    print(f"Aurum classes: {lm.classes}\n")

    print("Loading raw datasets:")
    records, audit = load_records(lm)
    if not records:
        print("No raw data found. Run `python -m ml.ingest` first.", file=sys.stderr)
        return 1

    print("\nGrouping to prevent leakage:")
    group_stats = merge_groups_by_similarity(records, args.hamming)

    print("\nSplitting by cluster:")
    assignment = split_clusters(records, lm.classes, args.seed)

    # Cap background-only images so they don't drown the real objects.
    rng = random.Random(args.seed)
    by_split: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_split[assignment[r["cluster"]]].append(r)

    kept: dict[str, list[dict]] = {}
    n_bg_dropped = 0
    n_aug_dropped = 0
    for split, rs in by_split.items():
        # Held-out splits keep one image per cluster. Roboflow ships augmented
        # copies of the same photograph; counting three rotations of one RAM
        # stick as three test images would overstate how much genuinely
        # independent evidence the reported metrics rest on. Train keeps all
        # copies, because there the augmentation is the point.
        if split in ("valid", "test"):
            best: dict[str, dict] = {}
            for r in sorted(rs, key=lambda r: r["path"].name):
                # Prefer a representative that actually has objects.
                cur = best.get(r["cluster"])
                if cur is None or (not cur["boxes"] and r["boxes"]):
                    best[r["cluster"]] = r
            n_aug_dropped += len(rs) - len(best)
            rs = list(best.values())

        fg = [r for r in rs if r["boxes"]]
        bg = [r for r in rs if not r["boxes"]]
        cap = int(len(fg) * args.max_background_frac / max(1e-9, 1 - args.max_background_frac))
        rng.shuffle(bg)
        n_bg_dropped += max(0, len(bg) - cap)
        kept[split] = fg + bg[:cap]

    # --- write ------------------------------------------------------------
    if OUT.exists():
        shutil.rmtree(OUT)
    idx = lm.class_to_index
    manifest = []
    for split, rs in kept.items():
        (OUT / split / "images").mkdir(parents=True, exist_ok=True)
        (OUT / split / "labels").mkdir(parents=True, exist_ok=True)
        for r in rs:
            name = f"{r['dataset']}__{r['path'].name}"
            shutil.copy2(r["path"], OUT / split / "images" / name)
            lines = [
                f"{idx[c]} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}" for c, cx, cy, w, h in r["boxes"]
            ]
            (OUT / split / "labels" / f"{Path(name).stem}.txt").write_text(
                "\n".join(lines) + ("\n" if lines else "")
            )
            manifest.append(
                {
                    "split": split,
                    "file": name,
                    "dataset": r["dataset"],
                    "cluster": r["cluster"],
                    "n_boxes": len(r["boxes"]),
                }
            )

    (OUT / "data.yaml").write_text(
        yaml.safe_dump(
            {
                "path": str(OUT.resolve()),
                "train": "train/images",
                "val": "valid/images",
                "test": "test/images",
                "nc": len(lm.classes),
                "names": lm.classes,
            },
            sort_keys=False,
        )
    )

    # --- report -----------------------------------------------------------
    REPORTS.mkdir(exist_ok=True)
    stats = {
        "seed": args.seed,
        "hamming": args.hamming,
        "label_map_version": lm.version,
        "classes": lm.classes,
        "grouping": group_stats,
        "background_images_dropped": n_bg_dropped,
        "augmented_copies_dropped_from_heldout": n_aug_dropped,
        "per_dataset": audit["datasets"],
        "kept_source_labels": dict(audit["kept_labels"].most_common()),
        "dropped_source_labels": dict(audit["dropped_labels"].most_common()),
        "splits": {},
    }

    print(f"\n{'split':8s} {'images':>7s} {'bg':>5s} " + " ".join(f"{c:>10s}" for c in lm.classes))
    for split in ("train", "valid", "test"):
        rs = kept.get(split, [])
        cc = Counter()
        for r in rs:
            for c, *_ in r["boxes"]:
                cc[c] += 1
        bg = sum(1 for r in rs if not r["boxes"])
        stats["splits"][split] = {
            "images": len(rs),
            "background": bg,
            "boxes": dict(cc),
            "datasets": dict(Counter(r["dataset"] for r in rs)),
        }
        print(
            f"{split:8s} {len(rs):7d} {bg:5d} "
            + " ".join(f"{cc.get(c, 0):10d}" for c in lm.classes)
        )

    (REPORTS / "dataset_stats.json").write_text(json.dumps(stats, indent=2))
    (REPORTS / "dataset_manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"\nDropped source labels (top 10): {audit['dropped_labels'].most_common(10)}")
    print(f"Background-only images dropped: {n_bg_dropped}")
    print(f"Augmented copies dropped from valid/test: {n_aug_dropped}")
    print(f"\nWrote {OUT}/data.yaml and reports/dataset_stats.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
