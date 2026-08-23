"""Camera-to-load-cell association: which object is on the pan.

The camera and the load cell are different sensing systems that never see each
other. Something has to say that the mass now settling on the cell belongs to
the assembly the camera identified a moment ago, and getting that wrong means
attaching a board's mass to a bare CPU's identity.

**This module is where the simplifying assumption lives, and it is the only
place it lives.** With no conveyor, the rig is a controlled weighing zone
handling one object at a time, so:

    the most recently confirmed assembly is the object being weighed

That is `SingleObjectZone`, and it is true only because of how the rig is
operated. It is stated here, in one class with four methods, so that a
position-based, tracking-based or timestamp-based association can replace it
without touching the state machine, the session or the valuation layer.

**Latching is the other half of identity.** Once an object is picked up and
carried to the pan it leaves the camera's view, and the tracker finalizes it
after `tracking.max_missing_frames`. If the pan asked "what is confirmed right
now" at that moment it would get nothing, and a new identity would be minted
for the mass - the exact failure the item id exists to prevent. So the zone
takes hold of the assembly and keeps holding it until the object physically
leaves the pan.
"""

from __future__ import annotations

from app.vision.assembly import Assembly
from app.vision.tracker import ItemState


class SingleObjectZone:
    """The object on the pan, on the assumption there is at most one.

    ponytail: single-object weighing zone. Multiple concurrent objects need a
    real association strategy (position, zone entry/exit, or track handover);
    replace this class, not its callers.
    """

    #: Named so a dashboard, a log or a reviewer can see which assumption
    #: produced an association rather than having to infer it.
    strategy = "single-object-weighing-zone"

    def __init__(self, source, min_detections: int = 3) -> None:
        """`source()` yields the assemblies the camera can currently see.

        A callable rather than a stream of pushed frames, so nothing has to
        remember to notify this object. The zone asks at the only moment the
        answer matters - when a mass has just landed on the pan - and cannot
        be left holding a stale candidate by a caller that skipped a step.
        """
        self._source = source
        self._min_detections = min_detections
        self._candidate: Assembly | None = None
        self._held: Assembly | None = None
        self._handled: set[str] = set()

    # -- from the camera ---------------------------------------------------
    def refresh(self) -> Assembly | None:
        """Re-read what the camera sees and pick the eligible candidate.

        A no-op while an object is latched: the camera is looking at the next
        thing on the bench, not at what is on the pan.
        """
        if self._held is not None:
            return None
        eligible = [
            a for a in self._source() if self._confirmed(a) and a.assembly_id not in self._handled
        ]
        self._candidate = (
            max(eligible, key=lambda a: a.root.last_frame if a.root else 0) if eligible else None
        )
        return self._candidate

    def _confirmed(self, assembly: Assembly) -> bool:
        """Confirmed enough to weigh: the container cleared the tracker's bar.

        A CONFIRMED container is enough. Children may still be flickering -
        RAM sits near 0.5 recall - and refusing the whole board because one
        module blinked would make a motherboard unweighable.

        A container that is LEAVING still counts, but only if it was seen
        enough times to have been confirmed. That is the difference between an
        object the operator has just picked up off the bench and a single-frame
        flicker that was never anything: both reach LEAVING, and only one of
        them is a thing to weigh.
        """
        root = assembly.root
        if root is None:
            return False
        if root.state is ItemState.CONFIRMED:
            return True
        return root.state is ItemState.LEAVING and root.detection_count >= self._min_detections

    # -- the pan -----------------------------------------------------------
    def latch(self) -> Assembly | None:
        """Take hold of the candidate for one weighing cycle, or None.

        After this the tracker may finalize every member and the camera may
        look elsewhere; the identity is held here until `release()`.
        """
        self.refresh()
        if self._held is None and self._candidate is not None:
            self._held, self._candidate = self._candidate, None
        return self._held

    @property
    def held(self) -> Assembly | None:
        return self._held

    def release(self) -> None:
        """The object has left the pan. Its id may never be latched again."""
        if self._held is not None:
            self._handled.add(self._held.assembly_id)
        self._held = None

    def handled(self, assembly_id: str) -> bool:
        return assembly_id in self._handled

    def snapshot(self) -> dict:
        return {
            "strategy": self.strategy,
            "assumption": (
                "The most recently confirmed assembly is the object on the pan. "
                "True only because this rig handles one object at a time and has "
                "no conveyor."
            ),
            "held": self._held.assembly_id if self._held else None,
            "candidate": self._candidate.assembly_id if self._candidate else None,
            "handled_count": len(self._handled),
        }
