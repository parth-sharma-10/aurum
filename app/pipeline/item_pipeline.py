"""The item pipeline: frames in, physical item lifecycles out.

    frame -> detector -> ByteTrack -> TrackedItem (stable item_id)
                                          |
                                          +-- Phase 5 attaches a mass here
                                          +-- Phase 6/7 route it here
                                          +-- the ledger records it here

What this phase establishes is the **identity** every later stage hangs off.
The load cell, the decision engine and the servo all act on one `item_id`, so
none of them invents an identity system of its own.

Two entry points, because the whole thing must run without hardware:

    process_frame(frame)          needs a model. Used by the camera path.
    process_detections(dets)      needs nothing. Used by tests and simulation.

They meet at the same lifecycle, so a simulated run exercises the code a real
run uses rather than a parallel imitation of it.

No grading logic lives here. `app.decision` owns that, and a later phase calls
it with the identity this module produces.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from app import config as config_module
from app.vision.tracker import (
    DetectorTracker,
    ItemState,
    ItemTracker,
    TrackedDetection,
    TrackedItem,
)


class ItemPipeline:
    """Composes the detector, the tracker and the item lifecycle.

    `on_finalized` is called once per physical item, when its lifecycle closes.
    That is the seam a later phase writes a ledger row from — once per item, not
    once per frame it appeared in.
    """

    def __init__(
        self,
        detector=None,
        cfg: config_module.Config | None = None,
        on_finalized: Callable[[TrackedItem], None] | None = None,
    ) -> None:
        self.cfg = config_module.load() if cfg is None else cfg
        self.detector = detector
        self.tracker = ItemTracker(cfg=self.cfg)
        self.detector_tracker = DetectorTracker(detector, cfg=self.cfg) if detector else None
        self.on_finalized = on_finalized
        self.frames_processed = 0

    @property
    def simulated(self) -> bool:
        """True when no model backs this pipeline, or simulation is configured."""
        return self.detector is None or bool(self.cfg["conveyor.runtime.simulation"])

    @property
    def active_items(self) -> list[TrackedItem]:
        return self.tracker.active

    @property
    def current_item(self) -> TrackedItem | None:
        """The confirmed item seen most recently, or None.

        Confirmed rather than merely present: an object seen once is not yet
        something to weigh or route, and showing it as "current" would invite a
        downstream stage to act on a flicker.
        """
        confirmed = [i for i in self.tracker.active if i.state is ItemState.CONFIRMED]
        if not confirmed:
            return None
        return max(confirmed, key=lambda i: (i.last_frame, i.detection_count))

    def process_detections(
        self,
        detections: list[TrackedDetection],
        frame_id: int | None = None,
        now: datetime | None = None,
    ) -> list[TrackedItem]:
        """Advance the lifecycle by one frame's worth of tracked detections."""
        self.frames_processed += 1
        active = self.tracker.update(detections, frame_id=frame_id, now=now)
        for item in self.tracker.drain_finalized():
            if self.on_finalized is not None:
                self.on_finalized(item)
        return active

    def process_frame(self, frame, frame_id: int | None = None) -> list[TrackedItem]:
        """Detect, track and advance the lifecycle for one camera frame."""
        if self.detector_tracker is None:
            raise RuntimeError(
                "This pipeline has no detector. Construct it with one, or call "
                "process_detections() for a simulated run."
            )
        return self.process_detections(self.detector_tracker.track(frame), frame_id=frame_id)

    def attach_weight(self, item_id: str, grams: float | None, status: str) -> TrackedItem | None:
        """Record a mass against an item. Phase 5 owns the load cell itself."""
        item = self.tracker.get(item_id)
        if item is not None:
            item.attach_weight(grams, status)
        return item

    def finish(self) -> list[TrackedItem]:
        """Close every open item, e.g. when a run ends.

        Without this, items still on the belt when the operator stops would
        never finalize, and never reach the ledger.
        """
        self.tracker.finalize_all()
        closed = self.tracker.drain_finalized()
        for item in closed:
            if self.on_finalized is not None:
                self.on_finalized(item)
        return closed

    def reset(self) -> None:
        """Start a fresh run: new item identities, tracker numbering restarted."""
        self.tracker = ItemTracker(cfg=self.cfg)
        self.frames_processed = 0
        if self.detector_tracker is not None:
            self.detector_tracker.reset()

    def snapshot(self) -> dict:
        """Everything a dashboard or an API needs about the current run."""
        current = self.current_item
        return {
            "frames_processed": self.frames_processed,
            "simulated": self.simulated,
            "active_count": len(self.tracker.active),
            "current_item": current.as_dict() if current else None,
            "items": [item.as_dict() for item in self.tracker.active],
            "rejected_detections": self.tracker.rejected_detections,
            "tracking_policy": {
                "tracker": self.cfg["tracking.tracker"],
                "max_missing_frames": self.tracker.max_missing_frames,
                "min_detections_to_confirm": self.tracker.min_detections_to_confirm,
                "note": (
                    "max_missing_frames and min_detections_to_confirm are "
                    "engineering approximations, not research-derived."
                ),
            },
        }
