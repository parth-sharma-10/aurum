"""Batch composition: turn a stream of per-frame detections into one record.

Per-frame counts flicker — a RAM module drops out for two frames when a hand
crosses it. Writing whatever the last frame happened to say into a batch record
would make the record a function of when the operator clicked, so the batch
count for each class is the *median* count over a trailing window of frames.
The median ignores brief dropouts and brief double-counts without inventing
anything.

The record is deliberately about identification only. It carries component
identities, counts and confidences, plus an optional measured or simulated
mass. It does not carry a precious-metal figure: see `recovery_estimate`.
"""

from __future__ import annotations

import json
import statistics
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app import materials

ROOT = Path(__file__).resolve().parent.parent
BATCH_DIR = ROOT / "data" / "batches"


@dataclass
class BatchSession:
    """Accumulates frames for one batch."""

    window: int = 45  # ~1.5 s at 30 fps
    classes: list[str] = field(default_factory=list)
    batch_id: str = ""
    started_at: str = ""
    _counts: deque[dict[str, int]] = field(default_factory=deque, init=False)
    _confs: deque[float] = field(default_factory=deque, init=False)
    _frames_seen: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._counts = deque(maxlen=self.window)
        self._confs = deque(maxlen=self.window)
        if not self.batch_id:
            self.reset()

    def reset(self) -> None:
        self._counts.clear()
        self._confs.clear()
        self._frames_seen = 0
        self.batch_id = f"AUR-{uuid.uuid4().hex[:8].upper()}"
        self.started_at = datetime.now(UTC).isoformat(timespec="seconds")

    def new_scene(self) -> None:
        """Drop the count window without ending the batch.

        Used when the thing in front of the camera is replaced wholesale (a new
        still in image-demo mode). The batch keeps its identity; only the
        evidence the median is computed over is reset.
        """
        self._counts.clear()
        self._confs.clear()

    def add_frame(self, counts: dict[str, int], mean_conf: float) -> None:
        self._counts.append(dict(counts))
        if counts:
            self._confs.append(mean_conf)
        self._frames_seen += 1

    @property
    def frames_seen(self) -> int:
        return self._frames_seen

    @property
    def stable(self) -> bool:
        """True once there are enough frames for the median to mean anything."""
        return len(self._counts) >= min(self.window, 10)

    def stable_counts(self) -> dict[str, int]:
        if not self._counts:
            return dict.fromkeys(self.classes, 0)
        out = {}
        for c in self.classes:
            series = [f.get(c, 0) for f in self._counts]
            out[c] = int(statistics.median(series))
        return out

    def average_confidence(self) -> float:
        return round(statistics.fmean(self._confs), 4) if self._confs else 0.0

    def record(
        self, model_version: str, weight: dict | None = None, source: str = "webcam"
    ) -> dict:
        counts = self.stable_counts()
        rec = {
            "batch_id": self.batch_id,
            "detections": counts,
            "total_objects": sum(counts.values()),
            "average_confidence": self.average_confidence(),
            "frames_observed": self._frames_seen,
            "counting_method": (
                f"median per-class count over a trailing {self.window}-frame window"
            ),
            "started_at": self.started_at,
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "model_version": model_version,
            "source": source,
        }
        if weight is not None:
            rec["weight"] = weight
        rec["recovery_estimate"] = recovery_estimate(counts, weight)
        return rec

    def save(self, record: dict) -> Path:
        BATCH_DIR.mkdir(parents=True, exist_ok=True)
        path = BATCH_DIR / f"{record['batch_id']}.json"
        path.write_text(json.dumps(record, indent=2))
        return path


# Re-exported so a consumer of a batch record does not need to know that the
# material layer lives in another module. `app.materials` is the single owner.
DISCLAIMER = materials.DISCLAIMER
NOT_MEASURED = materials.NOT_MEASURED


def recovery_estimate(counts: dict[str, int], mass: dict | None = None) -> dict:
    """Component counts -> estimated material present, or an explicit refusal.

    Three quantities must never be confused, so all three are named in the
    output:

      detected_components  what the model counted — the only measured thing here
      components/material_estimate
                           counts x cited reference composition, an ESTIMATE
      recovery             kept separate, and unavailable unless a source
                           measured recovery from a feed matching the detection
      measured_material    always unavailable; Aurum has no assay

    The figures and their citations live in configs/material_reference.yaml,
    resolved through docs/sources/material_sources.yaml. This function is only
    the seam between batch composition and that layer; see `app.materials` for
    the rules it enforces. It fails closed: a detected class with no cited
    figure blocks the estimate rather than contributing a silent zero.

    `mass` is the batch weight record, consulted only for concentration-based
    evidence and only when the reading is a real measurement.
    """
    return materials.estimate(counts, mass)
