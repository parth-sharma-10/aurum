"""Six known objects and no camera, through the real pipeline.

A webcam that will not open is the most likely thing to go wrong on a stage,
and it takes the whole demonstration with it: no frame, no detection, no item,
nothing to weigh or route. This is the fallback, and what matters about it is
how little it replaces.

    camera -> detector -> tracker -> assembly -> mass -> PMDI -> decision -> route
    ^^^^^^^^^^^^^^^^^^^
    only this much is scripted

The detections are injected at the tracker's own input, so identity, assembly
grouping, the load cell, the composition database, the decision engine and the
actuation path are the ones that actually run. A scripted item and a seen item
are the same object by the time anything decides anything about it.

**The classes are scripted; nothing else is.** No bin appears in this file. The
decision engine is given a class and a mass and reaches its own conclusion, so
a scripted run can be wrong in front of an audience — which is the only way it
is worth showing.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.vision.tracker import TrackedDetection

#: Track ids for scripted objects. Far above anything a real tracker mints in a
#: demonstration, so a scripted item is recognisable in a ledger afterwards.
TRACK_ID_BASE = 9000


@dataclass(frozen=True)
class ScriptedObject:
    """One object the operator would otherwise be holding up to the camera."""

    component_class: str
    confidence: float
    #: What this one is in the script to show. Read out loud, or ignored.
    shows: str


#: Ordered, and the order is the argument. It opens on the class with the most
#: contained value, works down through the four the model knows, then spends
#: its last two objects on the cases a demonstration is usually arranged to
#: avoid: a detection the model is not sure about, and an object the evidence
#: database has no composition for at all.
SCRIPT: tuple[ScriptedObject, ...] = (
    ScriptedObject("CPU", 0.94, "the high-value class, confidently identified"),
    ScriptedObject("PCB", 0.88, "the bulk class: large mass, low precious fraction"),
    ScriptedObject("RAM", 0.91, "gold-plated edge contacts on a small mass"),
    ScriptedObject("Connector", 0.76, "the smallest mass in the set"),
    ScriptedObject("CPU", 0.38, "the same class the model is much less sure about"),
    ScriptedObject("Heatsink", 0.85, "a class the composition database cannot cite"),
)


def _box(index: int) -> tuple[int, int, int, int]:
    """A box of its own for each object, so grouping keeps them apart.

    Assembly grouping joins overlapping detections into one physical object,
    which is correct and is exactly what must not happen between two scripted
    items that are meant to be two items.
    """
    left = 40 + (index % 3) * 220
    top = 40 + (index // 3) * 200
    return (left, top, left + 160, top + 140)


def detections_for(index: int, obj: ScriptedObject) -> list[TrackedDetection]:
    return [
        TrackedDetection(
            track_id=TRACK_ID_BASE + index,
            class_name=obj.component_class,
            confidence=obj.confidence,
            xyxy=_box(index),
        )
    ]


def step(session) -> dict:
    """Put the next scripted object through the machine, and report what it did."""
    index = session.scripted_index
    if index >= len(SCRIPT):
        return {
            "error": "SCRIPT_EXHAUSTED",
            "reason": (
                f"All {len(SCRIPT)} scripted objects have been run. "
                "POST /track/reset to start the script again."
            ),
            "remaining": 0,
        }
    obj = SCRIPT[index]

    detections = detections_for(index, obj)
    # The tracker confirms on repeated sight, not on one frame, and that rule
    # is the pipeline's rather than this file's. Feeding it the frames it wants
    # keeps it that way; special-casing a scripted item into CONFIRMED would
    # make this the one path where identity works differently.
    for _ in range(session.pipeline.tracker.min_detections_to_confirm):
        session.inject_detections(detections)

    result = session.measure_and_route()
    session.scripted_index = index + 1
    # A scripted object cannot be put on a pan, so with no cell and no
    # `demo.mock_mass` there is no mass for it. The chain still runs and the
    # record still says UNAVAILABLE, but every figure that needs a mass is
    # missing and the reason is a setting, not a fault. Say which setting.
    advice = (
        None
        if result.get("weight_status") not in (None, "UNAVAILABLE")
        else (
            "No mass: nothing can be put on the pan for a scripted object. Set "
            "AURUM_DEMO_MOCK_MASS=true (configs/demo-profile.sh) for a per-class "
            "stand-in mass, labelled SIMULATED throughout."
        )
    )
    return {
        "scripted": {
            "index": index,
            "of": len(SCRIPT),
            "component_class": obj.component_class,
            "confidence": obj.confidence,
            "shows": obj.shows,
        },
        "remaining": len(SCRIPT) - session.scripted_index,
        "mass_advice": advice,
        "note": (
            "The class and confidence were scripted. The mass, the composition, "
            "the value and the bin were not: this item went through the same "
            "code a camera-seen item goes through."
        ),
        **result,
    }
