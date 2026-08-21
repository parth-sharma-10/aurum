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

ROOT = Path(__file__).resolve().parent.parent
BATCH_DIR = ROOT / "data" / "batches"
REFERENCE = ROOT / "configs" / "recovery_reference.yaml"


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
        rec["recovery_estimate"] = recovery_estimate(counts)
        return rec

    def save(self, record: dict) -> Path:
        BATCH_DIR.mkdir(parents=True, exist_ok=True)
        path = BATCH_DIR / f"{record['batch_id']}.json"
        path.write_text(json.dumps(record, indent=2))
        return path


DISCLAIMER = (
    "ESTIMATE ONLY — derived from component counts and published reference "
    "yields. Aurum Vision does not measure precious-metal content; RGB imagery "
    "cannot determine composition."
)

# Aurum never produces this. It is stated in every record so that "we did not
# measure it" is a field a consumer can read, not an absence it has to infer.
NOT_MEASURED = {
    "available": False,
    "reason": (
        "Aurum does not measure material content. Establishing a measured "
        "quantity requires destructive assay or XRF, neither of which is part "
        "of this system."
    ),
}


def recovery_estimate(counts: dict[str, int]) -> dict:
    """Component counts -> estimated material recovery, or an explicit refusal.

    Three quantities must never be confused, so all three are named in the
    output:

      detected_components  what the model counted — the only measured thing here
      estimated_recovery   counts x published reference yield, an ESTIMATE
      measured_material    always unavailable; Aurum has no assay

    Aurum's claim is that identification plus reference yield data gives an
    estimate. That requires yield data with a citation. Until
    configs/recovery_reference.yaml is populated with sourced figures this
    returns unavailable rather than a number, because a plausible-looking figure
    with no source behind it is worse than no figure at all: it gets quoted.
    """
    unavailable = {
        "available": False,
        "kind": "ESTIMATE",
        "basis": None,
        "detected_components": dict(counts),
        "measured_material": NOT_MEASURED,
        "reason": (
            "No reference yield data loaded. Populate configs/recovery_reference.yaml "
            "with cited per-component figures to enable estimation."
        ),
        "disclaimer": DISCLAIMER,
    }
    if not REFERENCE.exists():
        return unavailable

    import yaml

    ref = yaml.safe_load(REFERENCE.read_text()) or {}
    if not ref.get("enabled"):
        return unavailable

    yields = ref.get("per_component") or {}
    missing = [c for c, n in counts.items() if n and c not in yields]
    if missing:
        return {**unavailable, "reason": f"No cited reference yield for: {sorted(missing)}"}

    # A yield entry without a material, unit and source cannot be priced and
    # cannot be audited, so it is treated as missing rather than partially used.
    incomplete = [
        c
        for c, n in sorted(counts.items())
        if n and not all(yields[c].get(k) for k in ("material", "unit", "source"))
    ]
    if incomplete:
        return {
            **unavailable,
            "reason": (
                f"Reference yield for {sorted(incomplete)} is missing material, unit "
                f"or source; every entry needs all three to be usable."
            ),
        }

    lines = []
    for c, n in sorted(counts.items()):
        if not n:
            continue
        y = yields[c]
        lines.append(
            {
                "component": c,
                "count": n,
                "material": y["material"],
                "per_unit": y.get("value"),
                "unit": y["unit"],
                "total": round(n * float(y.get("value", 0)), 6),
                "source": y["source"],
            }
        )
    return {
        "available": True,
        "kind": "ESTIMATE",
        "basis": ref.get("basis", "published reference yields"),
        "detected_components": dict(counts),
        "components": lines,
        "measured_material": NOT_MEASURED,
        "disclaimer": DISCLAIMER,
    }
