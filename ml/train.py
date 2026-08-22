"""Fine-tune a YOLO detector on the prepared Aurum dataset.

Transfer learning from COCO-pretrained weights — nothing here trains from
scratch. Every knob is an environment variable with a working default, so a
second developer reproduces the run with `python -m ml.train` and no edits.

Usage:
    python -m ml.train
    AURUM_EPOCHS=120 AURUM_MODEL=yolo11s.pt python -m ml.train
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import torch
import yaml
from ultralytics import YOLO

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "aurum" / "data.yaml"
RUNS = ROOT / "runs"
MODELS = ROOT / "models"

MODEL_VERSION = "Aurum Vision v0.1"

# The configuration the released v0.1 model was actually trained at. Every value
# is read back from that checkpoint's own `train_args`, not remembered: it is the
# artifact that decides what the published metrics describe. These are the
# defaults so `python -m ml.train` reproduces the released model rather than a
# different one that no report describes; each stays overridable by environment
# variable. `workers` is deliberately absent — it is a dataloader knob that does
# not change the resulting weights, and the released run recorded 0 for it after
# being resumed.
RELEASE_CONFIG = {
    "model": "yolo11n.pt",
    "epochs": 50,
    "imgsz": 512,
    "batch": 32,
    "patience": 15,
    "seed": 1337,
}


def artifact_info(path: Path) -> dict:
    """Identify a weights file by its content.

    A filename proves nothing about which weights produced a set of metrics.
    Recording the digest and size next to the metrics is what lets someone
    check, later, that the file they downloaded is the file that was measured.
    """
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
    }


def pick_device() -> str:
    if os.environ.get("AURUM_DEVICE"):
        return os.environ["AURUM_DEVICE"]
    if torch.cuda.is_available():
        return "0"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def config() -> dict:
    """Training configuration: the release values, unless overridden."""
    r = RELEASE_CONFIG
    return {
        "model": os.environ.get("AURUM_MODEL", r["model"]),
        "epochs": int(os.environ.get("AURUM_EPOCHS", r["epochs"])),
        "imgsz": int(os.environ.get("AURUM_IMGSZ", r["imgsz"])),
        "batch": int(os.environ.get("AURUM_BATCH", r["batch"])),
        "patience": int(os.environ.get("AURUM_PATIENCE", r["patience"])),
        "seed": int(os.environ.get("AURUM_SEED", r["seed"])),
        "device": pick_device(),
        "workers": int(os.environ.get("AURUM_WORKERS", 8)),
        "name": os.environ.get("AURUM_RUN", "aurum_vision_v0_1"),
    }


def main() -> int:
    if not DATA.exists():
        raise SystemExit("data/aurum/data.yaml missing. Run ml.prepare then ml.validate.")

    cfg = config()
    print(f"{MODEL_VERSION} — training")
    for k, v in cfg.items():
        print(f"  {k:10s} {v}")

    # A long run can die for reasons unrelated to the model — a machine sleeping,
    # a full disk, a file briefly unavailable. Ultralytics checkpoints the
    # optimizer state into last.pt every epoch, so the run can pick up where it
    # stopped instead of discarding hours of work.
    last = RUNS / cfg["name"] / "weights" / "last.pt"
    resume = os.environ.get("AURUM_RESUME", "").lower() in ("1", "true", "yes")
    if resume:
        if not last.exists():
            raise SystemExit(f"AURUM_RESUME set but no checkpoint at {last}")
        print(f"  resuming from {last.relative_to(ROOT)}")
        model = YOLO(str(last))
        model.train(resume=True)
        return _finalize(cfg)

    model = YOLO(cfg["model"])  # COCO-pretrained; transfer learning only

    model.train(
        data=str(DATA),
        epochs=cfg["epochs"],
        imgsz=cfg["imgsz"],
        batch=cfg["batch"],
        patience=cfg["patience"],
        seed=cfg["seed"],
        device=cfg["device"],
        workers=cfg["workers"],
        project=str(RUNS),
        name=cfg["name"],
        exist_ok=True,
        plots=True,  # PR curves, confusion matrix, label distribution
        val=True,
        # Mild geometric augmentation only. Components are photographed from
        # arbitrary angles, but vertical flips would create RAM modules that
        # cannot physically sit in a slot, and heavy HSV shift destroys the
        # colour cues (green board, gold contacts) the classes rely on.
        fliplr=0.5,
        flipud=0.0,
        degrees=15.0,
        scale=0.4,
        hsv_h=0.015,
        hsv_s=0.5,
        hsv_v=0.3,
        mosaic=1.0,
        close_mosaic=10,
    )

    return _finalize(cfg)


def _finalize(cfg: dict) -> int:
    """Copy the chosen weights into models/ and write the version metadata.

    Shared by a fresh run and a resumed one, so a resumed run produces the
    same artifacts and the same metadata file as an uninterrupted one.
    """
    run_dir = RUNS / cfg["name"]
    weights = run_dir / "weights"
    MODELS.mkdir(exist_ok=True)
    for src, dst in (
        ("best.pt", "aurum_vision_v0_1_best.pt"),
        ("last.pt", "aurum_vision_v0_1_last.pt"),
    ):
        if (weights / src).exists():
            shutil.copy2(weights / src, MODELS / dst)
            print(f"  saved models/{dst}")

    classes = yaml.safe_load(DATA.read_text())["names"]
    meta = {
        "model_version": MODEL_VERSION,
        "architecture": cfg["model"],
        "framework": f"ultralytics {__import__('ultralytics').__version__}",
        "torch": torch.__version__,
        "classes": classes,
        "image_size": cfg["imgsz"],
        "epochs_requested": cfg["epochs"],
        "batch": cfg["batch"],
        "patience": cfg["patience"],
        "seed": cfg["seed"],
        "device": cfg["device"],
        "trained_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "dataset": "data/aurum (see reports/dataset_stats.json)",
        "run_dir": str(run_dir.relative_to(ROOT)),
        "weights": "models/aurum_vision_v0_1_best.pt",
        "artifact": artifact_info(MODELS / "aurum_vision_v0_1_best.pt"),
        "metrics": "reports/test_metrics.json (regenerate with `python -m ml.evaluate`)",
    }
    (MODELS / "aurum_vision_v0_1_meta.json").write_text(json.dumps(meta, indent=2))
    print("\nWrote models/aurum_vision_v0_1_meta.json")
    print("Next: python -m ml.evaluate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
