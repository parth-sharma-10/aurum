"""Capturing vision failures at run time, in a format FiftyOne can read later.

**FiftyOne is not imported here and must not be.** This module runs inside the
live pipeline, where the only acceptable cost is writing a JPEG and a line of
JSON. The conversion into a FiftyOne dataset, the evaluation and the app all
happen afterwards in `tools.fiftyone.dataset`, on a developer's machine, from
the files this leaves behind.

    live pipeline -> data/vision_errors/*.jpg + failures.jsonl
                                |
                        (later, offline)
                                v
                  tools.fiftyone.dataset -> FiftyOne -> evaluate_detections()

**Two kinds of failure category, and they are not interchangeable.**

Some are decidable from one frame with no ground truth: nothing was detected,
a box is degenerate, two boxes of one class overlap, a detection sits on the
frame edge. Those are captured here.

The rest — a false positive, a missed detection, a class confusion — are
*comparisons against a label*. Nothing at run time knows them, and a pipeline
that claimed to would be inventing the ground truth it is supposed to be
checked against. They are assigned by `evaluate_detections()` in the offline
step, or by a person in the FiftyOne app. `RUNTIME` and `REVIEW` below say
which is which, and `capture()` refuses to record a REVIEW category.

**Off by default.** Writing a frame per event costs disk and time, and a
demonstration should not be quietly filling a directory. `AURUM_VISION_CAPTURE`
turns it on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DIR = ROOT / "data" / "vision_errors"
MANIFEST = "failures.jsonl"


class VisionFailure(StrEnum):
    """Why a frame or a detection is worth looking at again."""

    # -- decidable at run time, from the frame alone --------------------
    NO_DETECTION = "NO_DETECTION"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    UNKNOWN_OBJECT = "UNKNOWN_OBJECT"
    INVALID_GEOMETRY = "INVALID_GEOMETRY"
    DUPLICATE_DETECTION = "DUPLICATE_DETECTION"
    PARTIAL_VISIBILITY = "PARTIAL_VISIBILITY"
    MULTIPLE_OBJECTS = "MULTIPLE_OBJECTS"
    TRACK_LOSS = "TRACK_LOSS"

    # -- assigned by evaluation against ground truth, or by a person ----
    FALSE_POSITIVE = "FALSE_POSITIVE"
    MISSED_DETECTION = "MISSED_DETECTION"
    CLASS_CONFUSION = "CLASS_CONFUSION"
    TRACK_SWITCH = "TRACK_SWITCH"
    MOTION_BLUR = "MOTION_BLUR"
    GLARE = "GLARE"
    OCCLUSION = "OCCLUSION"


#: Categories a single frame can justify with no label to compare against.
RUNTIME = frozenset(
    {
        VisionFailure.NO_DETECTION,
        VisionFailure.LOW_CONFIDENCE,
        VisionFailure.UNKNOWN_OBJECT,
        VisionFailure.INVALID_GEOMETRY,
        VisionFailure.DUPLICATE_DETECTION,
        VisionFailure.PARTIAL_VISIBILITY,
        VisionFailure.MULTIPLE_OBJECTS,
        VisionFailure.TRACK_LOSS,
    }
)

#: Categories that are a comparison, a sequence or a judgement. Nothing in the
#: live pipeline may assert one.
REVIEW = frozenset(VisionFailure) - RUNTIME


class CaptureError(ValueError):
    """A category was claimed that the evidence at hand cannot support."""


@dataclass
class VisionSample:
    """One captured frame and everything known about why it was captured."""

    failure: VisionFailure
    frame_path: str | None
    session_id: str | None = None
    item_id: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    frame_id: int | None = None
    predictions: list[dict] = field(default_factory=list)
    #: Filled in only where a label genuinely exists. Kept apart from
    #: `predictions` so nothing can read one as the other.
    ground_truth: list[dict] = field(default_factory=list)
    decision: str | None = None
    mass_status: str | None = None
    price_status: str | None = None
    note: str | None = None

    def as_dict(self) -> dict:
        return {
            "failure": str(self.failure),
            "frame_path": self.frame_path,
            "session_id": self.session_id,
            "item_id": self.item_id,
            "timestamp": self.timestamp,
            "frame_id": self.frame_id,
            "predictions": list(self.predictions),
            "ground_truth": list(self.ground_truth),
            "decision": self.decision,
            "mass_status": self.mass_status,
            "price_status": self.price_status,
            "note": self.note,
        }


def detection_dict(detection) -> dict:
    """One detection, in the shape the dataset builder expects.

    Accepts anything with the tracker's attribute names, so a `TrackedDetection`
    and a plain dict both work and neither needs to know about FiftyOne.
    """
    if isinstance(detection, dict):
        source = detection
    else:
        source = {
            "track_id": getattr(detection, "track_id", None),
            "class_name": getattr(detection, "class_name", None),
            "confidence": getattr(detection, "confidence", None),
            "xyxy": getattr(detection, "xyxy", None),
        }
    box = source.get("xyxy")
    return {
        "track_id": source.get("track_id"),
        "class_name": source.get("class_name"),
        "confidence": source.get("confidence"),
        "xyxy": list(box) if box else None,
    }


def is_degenerate(box) -> bool:
    """A box with no area, or with its corners the wrong way round."""
    if not box or len(box) != 4:
        return True
    x1, y1, x2, y2 = box
    return x2 <= x1 or y2 <= y1


def touches_edge(box, width: int, height: int, margin: int = 2) -> bool:
    """Whether a box runs into the frame boundary — the object is cut off."""
    if not box or len(box) != 4:
        return False
    x1, y1, x2, y2 = box
    return x1 <= margin or y1 <= margin or x2 >= width - margin or y2 >= height - margin


def iou(a, b) -> float:
    if is_degenerate(a) or is_degenerate(b):
        return 0.0
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    overlap = max(0.0, min(ax2, bx2) - max(ax1, bx1)) * max(0.0, min(ay2, by2) - max(ay1, by1))
    if overlap <= 0:
        return 0.0
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - overlap
    return overlap / union if union > 0 else 0.0


def classify_frame(
    detections,
    width: int,
    height: int,
    low_confidence: float = 0.5,
    duplicate_iou: float = 0.8,
    known_classes=None,
) -> list[tuple[VisionFailure, str]]:
    """What is wrong with this frame, from the frame alone.

    Returns `(category, why)` pairs, possibly empty. Every category returned is
    in `RUNTIME`, and each one names the specific observation that justifies it
    — a category with no observation behind it is exactly what this module
    exists to prevent.
    """
    found: list[tuple[VisionFailure, str]] = []
    boxes = [detection_dict(d) for d in detections or []]

    if not boxes:
        return [(VisionFailure.NO_DETECTION, "The model returned no detections for this frame.")]

    for box in boxes:
        name, conf, xyxy = box["class_name"], box["confidence"], box["xyxy"]
        if is_degenerate(xyxy):
            found.append(
                (VisionFailure.INVALID_GEOMETRY, f"{name}: box {xyxy} has no positive area.")
            )
            continue
        if conf is not None and conf < low_confidence:
            found.append(
                (
                    VisionFailure.LOW_CONFIDENCE,
                    f"{name} at {conf:.2f}, below the {low_confidence:.2f} review threshold.",
                )
            )
        if known_classes is not None and name not in known_classes:
            found.append(
                (
                    VisionFailure.UNKNOWN_OBJECT,
                    f"{name!r} has no cited material profile, so it cannot be routed.",
                )
            )
        if touches_edge(xyxy, width, height):
            found.append(
                (
                    VisionFailure.PARTIAL_VISIBILITY,
                    f"{name} touches the frame edge; part of the object is outside it.",
                )
            )

    for i, first in enumerate(boxes):
        for second in boxes[i + 1 :]:
            if first["class_name"] != second["class_name"]:
                continue
            overlap = iou(first["xyxy"], second["xyxy"])
            if overlap >= duplicate_iou:
                found.append(
                    (
                        VisionFailure.DUPLICATE_DETECTION,
                        f"Two {first['class_name']} boxes overlap at IoU {overlap:.2f}.",
                    )
                )

    return found


class FailureCapture:
    """Writes failure samples to a directory FiftyOne can be pointed at.

    Off unless `enabled`, and rate-limited per category, because the point is a
    set of interesting frames rather than a video of a working machine.
    """

    def __init__(
        self,
        directory: Path | str = DEFAULT_DIR,
        enabled: bool = False,
        session_id: str | None = None,
        per_category_limit: int = 50,
        write_frame=None,
    ) -> None:
        self.directory = Path(directory)
        self.enabled = enabled
        self.session_id = session_id
        self.per_category_limit = per_category_limit
        #: Injected so a test can capture without OpenCV and without a disk.
        self._write_frame = write_frame
        self.counts: dict[str, int] = {}
        self.samples: list[VisionSample] = []
        self.skipped = 0

    @property
    def manifest_path(self) -> Path:
        return self.directory / MANIFEST

    def _encode(self, frame, name: str) -> str | None:
        """Write one frame to disk. Returns its path, or None if there is none."""
        if frame is None:
            return None
        path = self.directory / name
        if self._write_frame is not None:
            self._write_frame(str(path), frame)
            return str(path)
        import cv2

        self.directory.mkdir(parents=True, exist_ok=True)
        return str(path) if cv2.imwrite(str(path), frame) else None

    def capture(
        self,
        failure: VisionFailure | str,
        frame=None,
        note: str | None = None,
        **metadata,
    ) -> VisionSample | None:
        """Record one failure sample. Returns None when nothing was written.

        Raises rather than records if a REVIEW category is claimed: those are
        comparisons against a label, and the live pipeline has none.
        """
        failure = VisionFailure(str(failure))
        if failure in REVIEW:
            raise CaptureError(
                f"{failure} is decided by comparing against ground truth, not by "
                "watching one frame. Capture the frame under a RUNTIME category and "
                "let tools.fiftyone.dataset.evaluate() assign this one."
            )
        if not self.enabled:
            return None
        if self.counts.get(str(failure), 0) >= self.per_category_limit:
            self.skipped += 1
            return None

        index = self.counts.get(str(failure), 0)
        self.counts[str(failure)] = index + 1
        name = f"{failure}_{self.session_id or 'run'}_{index:04d}.jpg"
        sample = VisionSample(
            failure=failure,
            frame_path=self._encode(frame, name),
            session_id=self.session_id,
            note=note,
            predictions=[detection_dict(d) for d in metadata.pop("detections", []) or []],
            ground_truth=[detection_dict(d) for d in metadata.pop("ground_truth", []) or []],
            **{k: v for k, v in metadata.items() if k in VisionSample.__annotations__},
        )
        self.samples.append(sample)
        self._append(sample)
        return sample

    def capture_frame(self, frame, detections, width, height, **kwargs) -> list[VisionSample]:
        """Classify a frame and capture whatever it turns out to be wrong with."""
        out = []
        for failure, why in classify_frame(detections, width, height, **kwargs):
            sample = self.capture(failure, frame=frame, note=why, detections=detections)
            if sample is not None:
                out.append(sample)
        return out

    def _append(self, sample: VisionSample) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample.as_dict()) + "\n")

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "directory": str(self.directory),
            "captured": len(self.samples),
            "by_category": dict(self.counts),
            "skipped_over_limit": self.skipped,
            "note": (
                "Runtime capture records what one frame can justify. False "
                "positives, missed detections and class confusion are comparisons "
                "against a label and are assigned by tools.fiftyone.dataset."
            ),
        }


def read_manifest(directory: Path | str = DEFAULT_DIR) -> list[dict]:
    """Every captured sample, from the JSONL manifest. Empty if none."""
    path = Path(directory) / MANIFEST
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            # A truncated final line from a killed process is not a reason to
            # lose the samples before it.
            continue
    return out
