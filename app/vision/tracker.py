"""Frame detections to physical item identities.

A camera pointed at a conveyor produces a CPU in frame 1, a CPU in frame 2 and
a CPU in frame 3. That is one physical object, not three, and everything
downstream — one weighing, one decision, one servo firing, one ledger row —
depends on saying so.

Two layers, split so the lifecycle can be tested without a model:

    DetectorTracker   thin adapter over Ultralytics' ByteTrack. Needs weights.
    ItemTracker       pure lifecycle state machine over TrackedDetection.
                      No model, no camera, no frames.

**A track id is not an item id.** ByteTrack numbers tracks from 1 and starts
over every process, so using its number as the ledger identity would collide
across restarts — two different CPUs from two different sessions both filed as
item 1. Aurum mints its own `AUR-ITEM-xxxxxxxx`, and the track id travels
alongside it for debugging.

**An item is not finalized because it blinked.** A detection miss, an occluded
frame or a dropped frame must not end a lifecycle, so a missing item moves to
LEAVING and only reaches FINALIZED after `tracking.max_missing_frames`
consecutive absences. Finalization happens exactly once per item.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from app import config as config_module


class ItemState(StrEnum):
    """Where an item is in its life. Every state here has something that sets it."""

    #: Seen once. Not yet enough evidence to act on.
    NEW = "NEW"
    #: Seen repeatedly, still below the confirmation threshold.
    TRACKING = "TRACKING"
    #: Enough consecutive observations to weigh, decide and route.
    CONFIRMED = "CONFIRMED"
    #: Missing from recent frames, inside the tolerance. May come back.
    LEAVING = "LEAVING"
    #: Terminal. Gone past tolerance, or finalized deliberately.
    FINALIZED = "FINALIZED"


#: States an item may still be observed in.
ACTIVE_STATES = (ItemState.NEW, ItemState.TRACKING, ItemState.CONFIRMED, ItemState.LEAVING)


@dataclass(frozen=True)
class TrackedDetection:
    """One detection in one frame, carrying the tracker's identity for it."""

    track_id: int
    class_name: str
    confidence: float
    xyxy: tuple[int, int, int, int]

    @property
    def center(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.xyxy
        return (x1 + x2) / 2.0, (y1 + y2) / 2.0


def is_valid(detection: object) -> bool:
    """Whether a detection can be trusted enough to carry an identity.

    Bad input is a skipped detection, never a crashed conveyor. A malformed
    box from a driver glitch must not take the sorter down mid-run.
    """
    if not isinstance(detection, TrackedDetection):
        return False
    if not isinstance(detection.track_id, int) or isinstance(detection.track_id, bool):
        return False
    if not detection.class_name or not isinstance(detection.class_name, str):
        return False
    conf = detection.confidence
    if not isinstance(conf, (int, float)) or isinstance(conf, bool):
        return False
    if not 0.0 <= conf <= 1.0:
        return False
    box = detection.xyxy
    if not isinstance(box, (tuple, list)) or len(box) != 4:
        return False
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box)


@dataclass
class TrackedItem:
    """One physical object, followed across frames.

    Confidence is exposed three ways because they mean different things and
    downstream must not have to guess which it got. `confidence` — the one the
    decision engine reads — is the **mean over every observation**: a single
    lucky frame must not promote an item into the premium bin, and a single
    unlucky one must not demote it.
    """

    item_id: str
    track_id: int
    first_frame: int
    last_frame: int
    first_seen: str
    last_seen: str
    state: ItemState = ItemState.NEW
    detection_count: int = 0
    frames_since_seen: int = 0
    bbox: tuple[int, int, int, int] | None = None
    center: tuple[float, float] | None = None
    previous_center: tuple[float, float] | None = None
    confidences: list[float] = field(default_factory=list)
    class_counts: Counter = field(default_factory=Counter)

    #: Filled by Phase 5. Declared here so the load cell attaches to an
    #: existing identity rather than inventing a second one.
    weight_g: float | None = None
    weight_status: str | None = None
    weight_timestamp: str | None = None

    #: The full weight record, when the item was weighed through the sensor.
    #: Carries the status and the reason a refusal happened.
    weight_reading: dict | None = None

    #: Filled by the session from app.decision. No grading logic lives here.
    decision: dict | None = None

    #: The PMDI/valuation evidence the decision was taken on, as
    #: `app.valuation.Valuation.as_dict()`. Kept beside the decision so a
    #: dashboard can show the reasoning, not just the verdict.
    valuation: dict | None = None

    #: What the actuation layer did about that decision, as
    #: `app.hardware.Command.as_dict()` - or an explicit record of why no
    #: command was sent. Bin C leaves a reason here, never a servo.
    actuation: dict | None = None

    @property
    def class_name(self) -> str | None:
        """The majority class over every observation.

        A tracker can flip an object's class for a frame. Taking the majority
        keeps a flicker from changing which bin the item is routed to.
        """
        return self.class_counts.most_common(1)[0][0] if self.class_counts else None

    #: How many recent observations `confidence` averages over. A lifetime mean
    #: was wrong for the rig this runs on: the load cell sits UNDER the camera,
    #: so the tracker starts observing while the object is still being placed —
    #: a hand over it, moving, half out of frame. Those frames score badly, and
    #: because the mean never forgot them a component that then sat perfectly
    #: still and read 0.9 could keep a lifetime mean of 0.43 and be refused as
    #: UNKNOWN. A RAM did exactly that on 2026-08-26.
    #:
    #: For a stationary object the recent view IS the evidence; the frames from
    #: while it was being put down are not. `max_confidence` and the full list
    #: are still kept, so nothing is lost.
    CONFIDENCE_WINDOW = 15

    @property
    def confidence(self) -> float | None:
        """Mean confidence over the recent window. The decision engine reads this."""
        if not self.confidences:
            return None
        window = self.confidences[-self.CONFIDENCE_WINDOW :]
        return sum(window) / len(window)

    @property
    def lifetime_confidence(self) -> float | None:
        """Mean over every observation, including placement. Kept for the record."""
        if not self.confidences:
            return None
        return sum(self.confidences) / len(self.confidences)

    @property
    def latest_confidence(self) -> float | None:
        return self.confidences[-1] if self.confidences else None

    @property
    def max_confidence(self) -> float | None:
        return max(self.confidences) if self.confidences else None

    @property
    def age_frames(self) -> int:
        return self.last_frame - self.first_frame + 1

    @property
    def velocity(self) -> tuple[float, float] | None:
        """Pixels per frame between the last two observations.

        Deliberately **not** converted to cm/s. That needs a belt speed and a
        pixel scale, both of which are UNMEASURED; converting would mean
        inventing them.
        """
        if self.center is None or self.previous_center is None:
            return None
        return (
            self.center[0] - self.previous_center[0],
            self.center[1] - self.previous_center[1],
        )

    def attach_weight(self, grams: float | None, status: str, timestamp: str | None = None) -> None:
        """Record a mass against this identity. Phase 5 owns the load cell."""
        self.weight_g = grams
        self.weight_status = status
        self.weight_timestamp = timestamp or datetime.now(UTC).isoformat(timespec="seconds")

    def as_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "track_id": self.track_id,
            "class_name": self.class_name,
            "state": str(self.state),
            "confidence": self.confidence,
            "latest_confidence": self.latest_confidence,
            "max_confidence": self.max_confidence,
            "lifetime_confidence": self.lifetime_confidence,
            "confidence_basis": (
                f"mean over the last {self.CONFIDENCE_WINDOW} observations - the object as "
                "it now sits, not as it was being put down"
            ),
            "detection_count": self.detection_count,
            "frames_since_seen": self.frames_since_seen,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "first_frame": self.first_frame,
            "last_frame": self.last_frame,
            "age_frames": self.age_frames,
            "bbox": list(self.bbox) if self.bbox else None,
            "center": list(self.center) if self.center else None,
            "velocity_px_per_frame": list(self.velocity) if self.velocity else None,
            "weight_g": self.weight_g,
            "weight_status": self.weight_status,
            "weight_timestamp": self.weight_timestamp,
            "weight_reading": self.weight_reading,
            "decision": self.decision,
            "valuation": self.valuation,
            "actuation": self.actuation,
        }


def new_item_id() -> str:
    """A ledger identity independent of any tracker's numbering."""
    return f"AUR-ITEM-{uuid.uuid4().hex[:8].upper()}"


class ItemTracker:
    """The lifecycle state machine. No model, no camera, no frames."""

    def __init__(
        self,
        max_missing_frames: int | None = None,
        min_detections_to_confirm: int | None = None,
        cfg: config_module.Config | None = None,
    ) -> None:
        cfg = config_module.load() if cfg is None else cfg
        self.max_missing_frames = (
            cfg["tracking.max_missing_frames"] if max_missing_frames is None else max_missing_frames
        )
        self.min_detections_to_confirm = (
            cfg["tracking.min_detections_to_confirm"]
            if min_detections_to_confirm is None
            else min_detections_to_confirm
        )
        self._items: dict[int, TrackedItem] = {}
        self._finalized: list[TrackedItem] = []
        self._finalized_ids: set[str] = set()
        self.frame_id = 0
        self.rejected_detections = 0

    @property
    def active(self) -> list[TrackedItem]:
        return [i for i in self._items.values() if i.state in ACTIVE_STATES]

    @property
    def finalized(self) -> list[TrackedItem]:
        return list(self._finalized)

    def get(self, item_id: str) -> TrackedItem | None:
        for item in list(self._items.values()) + self._finalized:
            if item.item_id == item_id:
                return item
        return None

    def update(
        self, detections, frame_id: int | None = None, now: datetime | None = None
    ) -> list[TrackedItem]:
        """Fold one frame's tracked detections into the item set.

        Returns the items still active after this frame. Items that aged out
        move to `finalized` and are reported once by `drain_finalized()`.
        """
        now = datetime.now(UTC) if now is None else now
        self.frame_id = self.frame_id + 1 if frame_id is None else frame_id
        stamp = now.isoformat(timespec="seconds")

        seen: set[int] = set()
        for detection in detections or []:
            if not is_valid(detection):
                self.rejected_detections += 1
                continue
            seen.add(detection.track_id)
            self._observe(detection, stamp)

        for track_id, item in list(self._items.items()):
            if track_id in seen or item.state is ItemState.FINALIZED:
                continue
            item.frames_since_seen += 1
            if item.frames_since_seen > self.max_missing_frames:
                self._finalize(item)
            else:
                item.state = ItemState.LEAVING

        return self.active

    def _observe(self, detection: TrackedDetection, stamp: str) -> None:
        item = self._items.get(detection.track_id)
        if item is None or item.state is ItemState.FINALIZED:
            # A track id reused after finalization is a different physical
            # object; it gets its own identity rather than reviving a closed one.
            item = TrackedItem(
                item_id=new_item_id(),
                track_id=detection.track_id,
                first_frame=self.frame_id,
                last_frame=self.frame_id,
                first_seen=stamp,
                last_seen=stamp,
            )
            self._items[detection.track_id] = item

        item.previous_center = item.center
        item.center = detection.center
        item.bbox = tuple(int(v) for v in detection.xyxy)
        item.confidences.append(float(detection.confidence))
        item.class_counts[detection.class_name] += 1
        item.detection_count += 1
        item.frames_since_seen = 0
        item.last_frame = self.frame_id
        item.last_seen = stamp

        if item.detection_count >= self.min_detections_to_confirm:
            item.state = ItemState.CONFIRMED
        elif item.detection_count > 1:
            item.state = ItemState.TRACKING
        else:
            item.state = ItemState.NEW

    def _finalize(self, item: TrackedItem) -> None:
        """Close an item. Idempotent: one physical item finalizes exactly once."""
        if item.item_id in self._finalized_ids:
            return
        item.state = ItemState.FINALIZED
        self._finalized_ids.add(item.item_id)
        self._finalized.append(item)

    def finalize(self, item: TrackedItem) -> None:
        """Close an item deliberately, e.g. when the run ends."""
        self._finalize(item)

    def finalize_all(self) -> list[TrackedItem]:
        """Close every active item. Used when a run stops."""
        for item in self.active:
            self._finalize(item)
        return self.finalized

    def drain_finalized(self) -> list[TrackedItem]:
        """Hand over newly finalized items exactly once.

        The seam that keeps one physical item from producing several ledger
        rows: an item leaves here on one call and never on a second.
        """
        out, self._finalized = self._finalized, []
        return out


class DetectorTracker:
    """Ultralytics ByteTrack over the project's existing detector.

    Wraps `AurumDetector`'s model rather than loading a second one, so tracking
    and plain inference report the same classes at the same resolution.
    """

    def __init__(self, detector, tracker: str | None = None, cfg=None) -> None:
        cfg = config_module.load() if cfg is None else cfg
        self.detector = detector
        self.tracker = cfg["tracking.tracker"] if tracker is None else tracker

    def track(self, frame) -> list[TrackedDetection]:
        """One frame in, tracked detections out. Empty list when nothing is seen."""
        model = self.detector.model
        results = model.track(
            frame,
            persist=True,
            tracker=self.tracker,
            conf=self.detector.conf,
            iou=self.detector.iou,
            imgsz=self.detector.imgsz,
            verbose=False,
        )
        if not results:
            return []
        boxes = results[0].boxes
        # `id` is None until the tracker has associated a box with a track,
        # which is normal on the first frames and is not an error.
        if boxes is None or boxes.id is None or not len(boxes):
            return []

        xyxy = boxes.xyxy.cpu().numpy()
        confs = boxes.conf.cpu().numpy()
        classes = boxes.cls.cpu().numpy().astype(int)
        ids = boxes.id.cpu().numpy().astype(int)
        return [
            TrackedDetection(
                track_id=int(track_id),
                class_name=model.names[int(cls)],
                confidence=float(conf),
                xyxy=(int(x1), int(y1), int(x2), int(y2)),
            )
            for (x1, y1, x2, y2), conf, cls, track_id in zip(xyxy, confs, classes, ids, strict=True)
        ]

    def reset(self) -> None:
        """Drop ByteTrack's state so a new run starts numbering afresh.

        Tolerates a model that has not run yet: `predictor` does not exist
        until the first inference, and having nothing to clear is not an error.
        """
        model = getattr(self.detector, "model", None)
        predictor = getattr(model, "predictor", None)
        for track in getattr(predictor, "trackers", []) or []:
            track.reset()
