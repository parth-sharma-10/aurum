"""Generate the figures for the SIH deck from real pipeline outputs.

Every chart here reads a JSON or CSV that the pipeline actually produced. There
is no hardcoded number in this file: if training has not run, the training-curve
figure is skipped rather than drawn from placeholder data.

Usage:
    python -m ml.assets
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
FIGS = REPORTS / "figures"

# Validated categorical palette (light mode), slots 1-4.
# Verified with the data-viz validator: all checks pass; contrast warns on
# slots 3-4, so every bar carries a direct label.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e2e1dd"

plt.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "font.size": 10,
        "text.color": INK,
        "axes.labelcolor": INK2,
        "axes.edgecolor": GRID,
        "xtick.color": INK2,
        "ytick.color": INK2,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.dpi": 160,
    }
)


def _style(ax, title: str, subtitle: str = "") -> None:
    ax.set_title(title, loc="left", fontsize=13, fontweight="bold", pad=18 if subtitle else 10)
    if subtitle:
        ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9, color=INK2)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _save(fig, name: str) -> None:
    FIGS.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGS / name, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote reports/figures/{name}")


# ---------------------------------------------------------------------------
def class_distribution(stats: dict) -> None:
    classes = stats["classes"]
    splits = ["train", "valid", "test"]
    fig, ax = plt.subplots(figsize=(8, 4.2))
    w = 0.26
    for i, sp in enumerate(splits):
        vals = [stats["splits"][sp]["boxes"].get(c, 0) for c in classes]
        xs = [j + (i - 1) * w for j in range(len(classes))]
        bars = ax.bar(
            xs, vals, w * 0.92, label=sp, color=SERIES[i], edgecolor=SURFACE, linewidth=1.5
        )
        ax.bar_label(bars, fontsize=7.5, color=INK2, padding=2)
    ax.set_xticks(range(len(classes)), classes)
    ax.set_ylabel("annotated instances")
    ax.legend(frameon=False, ncol=3, loc="upper right")
    _style(
        ax,
        "Aurum class distribution by split",
        "Held-out splits contain one image per duplicate cluster",
    )
    _save(fig, "class_distribution.png")


def dataset_sources(stats: dict) -> None:
    per = stats["per_dataset"]
    names = sorted(per, key=lambda k: -per[k]["images"])
    imgs = [per[n]["images"] for n in names]
    boxes = [per[n]["boxes_kept"] for n in names]

    fig, ax = plt.subplots(figsize=(8.5, 4.4))
    y = range(len(names))
    b1 = ax.barh(
        [i + 0.2 for i in y],
        imgs,
        0.38,
        color=SERIES[0],
        label="images ingested",
        edgecolor=SURFACE,
        linewidth=1.5,
    )
    b2 = ax.barh(
        [i - 0.2 for i in y],
        boxes,
        0.38,
        color=SERIES[1],
        label="Aurum-class boxes",
        edgecolor=SURFACE,
        linewidth=1.5,
    )
    ax.bar_label(b1, fontsize=8, color=INK2, padding=3)
    ax.bar_label(b2, fontsize=8, color=INK2, padding=3)
    ax.set_yticks(list(y), [n.replace("_", " ") for n in names])
    ax.set_xlabel("count")
    ax.legend(frameon=False, loc="lower right")
    _style(
        ax,
        "Source datasets after Aurum label normalization",
        "Boxes counted only for PCB / RAM / CPU / Connector",
    )
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.grid(axis="y", visible=False)
    _save(fig, "dataset_sources.png")


def label_normalization(stats: dict) -> None:
    kept = stats["kept_source_labels"]
    dropped = stats["dropped_source_labels"]
    top_k = list(kept.items())[:10]
    top_d = list(dropped.items())[:10]

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    for ax, data, color, title in (
        (
            axes[0],
            top_k,
            SERIES[2],
            f"Kept — mapped to an Aurum class ({sum(kept.values()):,} boxes)",
        ),
        (
            axes[1],
            top_d,
            SERIES[3],
            f"Dropped — reviewed, out of scope ({sum(dropped.values()):,} boxes)",
        ),
    ):
        labels = [k for k, _ in data][::-1]
        vals = [v for _, v in data][::-1]
        bars = ax.barh(labels, vals, color=color, edgecolor=SURFACE, linewidth=1.5)
        ax.bar_label(bars, fontsize=8, color=INK2, padding=3)
        _style(ax, title)
        ax.grid(axis="x", color=GRID, linewidth=0.8)
        ax.grid(axis="y", visible=False)
        ax.set_xlabel("source annotations")
    fig.suptitle(
        "Label normalization: every source label is mapped or explicitly dropped",
        x=0.01,
        ha="left",
        fontsize=13,
        fontweight="bold",
    )
    fig.tight_layout()
    _save(fig, "label_normalization.png")


def training_curves(results_csv: Path) -> None:
    import csv

    rows = list(csv.DictReader(results_csv.open()))
    if not rows:
        print("  ! results.csv empty, skipping training curves")
        return
    key = {k.strip(): k for k in rows[0]}

    def col(name):
        return [float(r[key[name]]) for r in rows if r.get(key[name], "").strip()]

    ep = col("epoch")
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))

    axes[0].plot(ep, col("train/box_loss"), color=SERIES[0], lw=2, label="train box")
    axes[0].plot(ep, col("val/box_loss"), color=SERIES[1], lw=2, ls="--", label="val box")
    axes[0].plot(ep, col("train/cls_loss"), color=SERIES[2], lw=2, label="train cls")
    axes[0].plot(ep, col("val/cls_loss"), color=SERIES[3], lw=2, ls="--", label="val cls")
    axes[0].set_xlabel("epoch")
    axes[0].set_ylabel("loss")
    axes[0].legend(frameon=False, fontsize=8)
    _style(axes[0], "Training / validation loss")

    m50, m5095 = col("metrics/mAP50(B)"), col("metrics/mAP50-95(B)")
    axes[1].plot(ep, m50, color=SERIES[0], lw=2, label="mAP@50")
    axes[1].plot(ep, m5095, color=SERIES[1], lw=2, label="mAP@50:95")
    best = max(range(len(m50)), key=lambda i: m50[i])
    axes[1].scatter([ep[best]], [m50[best]], color=SERIES[0], zorder=5, s=30)
    axes[1].annotate(
        f"best {m50[best]:.3f} @ epoch {int(ep[best])}",
        (ep[best], m50[best]),
        textcoords="offset points",
        xytext=(-10, 10),
        fontsize=8,
        color=INK2,
        ha="right",
    )
    axes[1].set_xlabel("epoch")
    axes[1].set_ylabel("mAP (validation)")
    axes[1].legend(frameon=False, fontsize=8)
    _style(axes[1], "Validation mAP")

    fig.suptitle(
        "Aurum Vision v0.1 — training history", x=0.01, ha="left", fontsize=13, fontweight="bold"
    )
    fig.tight_layout()
    _save(fig, "training_curves.png")


def test_metrics_chart(metrics: dict) -> None:
    per = {k: v for k, v in metrics["metrics_per_class"].items() if v}
    classes = list(per)
    fields = [
        ("precision", "Precision"),
        ("recall", "Recall"),
        ("mAP50", "mAP@50"),
        ("mAP50_95", "mAP@50:95"),
    ]

    fig, ax = plt.subplots(figsize=(9, 4.4))
    w = 0.2
    for i, (fk, fl) in enumerate(fields):
        vals = [per[c][fk] for c in classes]
        xs = [j + (i - 1.5) * w for j in range(len(classes))]
        bars = ax.bar(
            xs, vals, w * 0.9, label=fl, color=SERIES[i], edgecolor=SURFACE, linewidth=1.5
        )
        ax.bar_label(bars, fmt="%.2f", fontsize=7, color=INK2, padding=2)
    ov = metrics["metrics_overall"]
    ax.set_xticks(range(len(classes)), classes)
    ax.set_ylim(0, 1.08)
    ax.set_ylabel("score")
    ax.legend(frameon=False, ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.12))
    _style(
        ax,
        "Held-out test performance by class",
        f"n={metrics['n_images']} unseen images · overall mAP@50 {ov['mAP50']:.3f}, "
        f"mAP@50:95 {ov['mAP50_95']:.3f}",
    )
    _save(fig, "test_metrics_by_class.png")


def architecture_diagram() -> None:
    fig, ax = plt.subplots(figsize=(11, 3.4))
    ax.set_xlim(0, 116)
    ax.set_ylim(0, 30)
    ax.axis("off")

    stages = [
        ("CAMERA", "1080p webcam\n@ collection bench", SERIES[0]),
        ("YOLO11n", "fine-tuned detector\n512 px input", SERIES[1]),
        ("COMPONENT ID", "PCB · RAM · CPU\nConnector + confidence", SERIES[2]),
        ("BATCH RECORD", "median counts\nover frame window", SERIES[3]),
        ("AURUM PIPELINE", "FastAPI + SQLite\nEPR ledger", "#4a3aa7"),
    ]
    x = 3
    for i, (title, sub, color) in enumerate(stages):
        box = FancyBboxPatch(
            (x, 8),
            18,
            13,
            boxstyle="round,pad=0.6",
            linewidth=2,
            edgecolor=color,
            facecolor=SURFACE,
        )
        ax.add_patch(box)
        ax.text(x + 9, 17.5, title, ha="center", fontsize=9.5, fontweight="bold", color=color)
        ax.text(x + 9, 12.5, sub, ha="center", fontsize=7.5, color=INK2)
        if i < len(stages) - 1:
            ax.add_patch(
                FancyArrowPatch(
                    (x + 18.6, 14.5),
                    (x + 21.8, 14.5),
                    arrowstyle="-|>",
                    mutation_scale=13,
                    linewidth=1.6,
                    color=INK2,
                )
            )
        x += 22.4

    ax.text(3, 26, "AURUM VISION — inference pipeline", fontsize=13, fontweight="bold", color=INK)
    ax.text(
        3,
        23.2,
        "Identification only. The batch record carries component "
        "identities and counts, never a measured metal content.",
        fontsize=8.5,
        color=INK2,
    )
    ax.text(
        3,
        3.5,
        "Optional: HX711 load cell contributes mass to the batch "
        "record and is flagged SIMULATED when no hardware is attached.",
        fontsize=7.5,
        color=INK2,
        style="italic",
    )
    _save(fig, "architecture.png")


def test_examples(split: str = "test", per_row: int = 4, rows_each: int = 2) -> None:
    """Composite grid of correct detections and failures from ml.evaluate.

    The individual annotated images stay out of git — they are derived from
    CC BY 4.0 source photographs and are regenerable. One composite figure
    gives a README reader the visual evidence without the repository
    redistributing the source dataset.
    """
    import matplotlib.image as mpimg

    base = REPORTS / f"{split}_predictions"
    good, bad = base / "correct", base / "failures"
    if not good.is_dir() or not bad.is_dir():
        print(f"  ! {base} missing — run ml.evaluate")
        return

    def pick(d):
        return sorted(p for p in d.iterdir() if p.suffix.lower() in {".jpg", ".png"})

    sets = [("Correct detections", pick(good)), ("Failure cases", pick(bad))]
    if not any(s[1] for s in sets):
        print("  ! no prediction images to compose")
        return

    n_rows = rows_each * 2
    fig, axes = plt.subplots(n_rows, per_row, figsize=(per_row * 3.1, n_rows * 2.5))
    axes = axes.reshape(n_rows, per_row)

    for block, (title, files) in enumerate(sets):
        for r in range(rows_each):
            for c in range(per_row):
                ax = axes[block * rows_each + r, c]
                ax.axis("off")
                i = r * per_row + c
                if i < len(files):
                    ax.imshow(mpimg.imread(files[i]))
                if r == 0 and c == 0:
                    ax.set_title(
                        title, loc="left", fontsize=12, fontweight="bold", color=INK, pad=10
                    )

    fig.suptitle(
        "Aurum Vision v0.1 — held-out test predictions\n"
        "ground truth in white, model predictions in gold",
        x=0.01,
        ha="left",
        fontsize=13,
        fontweight="bold",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    _save(fig, "test_examples.png")


def main() -> int:
    FIGS.mkdir(parents=True, exist_ok=True)
    print("Generating presentation figures:")

    architecture_diagram()

    stats_path = REPORTS / "dataset_stats.json"
    if stats_path.exists():
        stats = json.loads(stats_path.read_text())
        class_distribution(stats)
        dataset_sources(stats)
        label_normalization(stats)
    else:
        print("  ! reports/dataset_stats.json missing — run ml.prepare")

    results = ROOT / "runs" / "aurum_vision_v0_1" / "results.csv"
    if results.exists():
        training_curves(results)
    else:
        print("  ! no results.csv — run ml.train")

    met = REPORTS / "test_metrics.json"
    if met.exists():
        test_metrics_chart(json.loads(met.read_text()))
        test_examples()
    else:
        print("  ! reports/test_metrics.json missing — run ml.evaluate")

    print(f"\nFigures in {FIGS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
