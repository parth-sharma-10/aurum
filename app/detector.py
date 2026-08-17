"""Model wrapper used by both the live demo and the API.

Keeps model loading, warm-up and FPS accounting in one place so the demo UI and
the HTTP service report the same numbers from the same code path.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from ultralytics import YOLO

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WEIGHTS = ROOT / "models" / "aurum_vision_v0_1_best.pt"
DEFAULT_META = ROOT / "models" / "aurum_vision_v0_1_meta.json"

# Used only when a checkpoint does not record what it was trained at.
FALLBACK_IMGSZ = 640


def resolve_imgsz(model: YOLO, requested: int | None) -> int:
    """Inference size to use: the caller's, else the size the model was trained at.

    Inferring at a different resolution than training costs real accuracy — this
    model measures 0.806 mAP@50 at its trained 512 px and 0.742 at 640 — so the
    default is read from the checkpoint rather than hardcoded, which is how the
    two silently diverged before.
    """
    if requested is not None:
        return requested
    args = getattr(model.model, "args", None) or {}
    if not isinstance(args, dict):
        args = getattr(args, "__dict__", {})
    trained = args.get("imgsz")
    return int(trained) if isinstance(trained, (int, float)) else FALLBACK_IMGSZ


@dataclass
class Detection:
    cls: str
    conf: float
    xyxy: tuple[int, int, int, int]


@dataclass
class FrameResult:
    detections: list[Detection] = field(default_factory=list)
    inference_ms: float = 0.0

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.detections:
            out[d.cls] = out.get(d.cls, 0) + 1
        return out

    @property
    def mean_confidence(self) -> float:
        if not self.detections:
            return 0.0
        return float(np.mean([d.conf for d in self.detections]))


class AurumDetector:
    def __init__(
        self,
        weights: Path | str = DEFAULT_WEIGHTS,
        conf: float = 0.35,
        iou: float = 0.5,
        imgsz: int | None = None,
        device: str | None = None,
    ) -> None:
        weights = Path(weights)
        if not weights.exists():
            raise FileNotFoundError(
                f"No weights at {weights}. Train first (`python -m ml.train`) or pass --weights."
            )
        self.weights = weights
        self.conf, self.iou, self.device = conf, iou, device
        self.model = YOLO(str(weights))
        self.imgsz = resolve_imgsz(self.model, imgsz)
        self.classes: list[str] = [self.model.names[i] for i in sorted(self.model.names)]

        self.meta: dict = {}
        if DEFAULT_META.exists():
            # A malformed metadata file must not stop inference; the model
            # still works, it just reports itself as unversioned.
            with contextlib.suppress(json.JSONDecodeError):
                self.meta = json.loads(DEFAULT_META.read_text())
        self.model_version = self.meta.get("model_version", "Aurum Vision (unversioned)")

        self._frame_times: deque[float] = deque(maxlen=30)
        self._warm = False

    def warmup(self) -> None:
        """Run one throwaway inference so the first real frame isn't 3s slow."""
        if self._warm:
            return
        blank = np.zeros((self.imgsz, self.imgsz, 3), dtype=np.uint8)
        self.predict(blank)
        self._frame_times.clear()
        self._warm = True

    def predict(self, frame: np.ndarray) -> FrameResult:
        t0 = time.perf_counter()
        kw = {"conf": self.conf, "iou": self.iou, "imgsz": self.imgsz, "verbose": False}
        if self.device:
            kw["device"] = self.device
        res = self.model.predict(frame, **kw)[0]
        dt = time.perf_counter() - t0
        self._frame_times.append(dt)

        dets: list[Detection] = []
        if res.boxes is not None and len(res.boxes):
            xyxy = res.boxes.xyxy.cpu().numpy()
            confs = res.boxes.conf.cpu().numpy()
            clss = res.boxes.cls.cpu().numpy().astype(int)
            for (x1, y1, x2, y2), c, k in zip(xyxy, confs, clss, strict=True):
                dets.append(
                    Detection(
                        cls=self.model.names[int(k)],
                        conf=float(c),
                        xyxy=(int(x1), int(y1), int(x2), int(y2)),
                    )
                )
        return FrameResult(detections=dets, inference_ms=dt * 1000.0)

    @property
    def fps(self) -> float:
        if not self._frame_times:
            return 0.0
        return 1.0 / max(1e-6, float(np.mean(self._frame_times)))
