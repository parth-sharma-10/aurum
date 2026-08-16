"""Fine-tune a YOLO detector on the prepared Aurum dataset.

Transfer learning from COCO-pretrained weights — nothing here trains from
scratch. Every knob is an environment variable with a working default, so a
second developer reproduces the run with `python -m ml.train` and no edits.

Usage:
    python -m ml.train
    AURUM_EPOCHS=120 AURUM_MODEL=yolo11s.pt python -m ml.train
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import torch
import yaml
from ultralytics import YOLO

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "aurum" / "data.yaml"
RUNS = ROOT / "runs"
MODELS = ROOT / "models"

MODEL_VERSION = "Aurum Vision v0.1"


def pick_device() -> str:
    if os.environ.get("AURUM_DEVICE"):
        return os.environ["AURUM_DEVICE"]
    if torch.cuda.is_available():
        return "0"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def config() -> dict:
    return {
        "model": os.environ.get("AURUM_MODEL", "yolo11n.pt"),
        "epochs": int(os.environ.get("AURUM_EPOCHS", 100)),
        "imgsz": int(os.environ.get("AURUM_IMGSZ", 640)),
        "batch": int(os.environ.get("AURUM_BATCH", 16)),
        "patience": int(os.environ.get("AURUM_PATIENCE", 25)),
        "seed": int(os.environ.get("AURUM_SEED", 1337)),
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
        plots=True,          # PR curves, confusion matrix, label distribution
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

    run_dir = RUNS / cfg["name"]
    weights = run_dir / "weights"
    MODELS.mkdir(exist_ok=True)
    for src, dst in (("best.pt", "aurum_vision_v0_1_best.pt"),
                     ("last.pt", "aurum_vision_v0_1_last.pt")):
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
        "seed": cfg["seed"],
        "device": cfg["device"],
        "trained_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "dataset": "data/aurum (see reports/dataset_stats.json)",
        "run_dir": str(run_dir.relative_to(ROOT)),
        "weights": "models/aurum_vision_v0_1_best.pt",
    }
    (MODELS / "aurum_vision_v0_1_meta.json").write_text(json.dumps(meta, indent=2))
    print(f"\nWrote models/aurum_vision_v0_1_meta.json")
    print("Next: python -m ml.evaluate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
