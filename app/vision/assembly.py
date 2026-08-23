"""Physical assemblies: deciding which detected components are one object.

A motherboard is not something the model recognises. It is a PCB with other
components sitting on it, and the only general way to say so from an image is
geometry: a component whose box lies inside a board's box is *on* that board.

    ASSEMBLY  (one physical object, one AUR-ITEM- id, one mass)
    |-- PCB          the container
    |-- RAM x 2
    |-- CPU x 1
    +-- Connector x 5

versus three objects lying separately on the bench, each its own assembly of
one. The distinction matters because they have different valuation
consequences: an assembly has ONE mass covering every child, so a
concentration figure for the board may not be multiplied by it.

**No motherboard rules live here.** Nothing counts RAM slots, expects a CPU, or
knows what a chipset looks like. A board with four modules, one module or none
groups by exactly the same geometry, and a class becomes a container by being
listed in `tracking.assembly.container_classes` rather than by being named in
this file's logic.

**Absence is never inferred.** An assembly's inventory contains what was
detected. A component that was occluded, unlit or simply missed is not
recorded as zero - it is not recorded at all, and `counts` says nothing about
it either way.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from app import config as config_module
from app.vision.tracker import TrackedItem


def _area(box) -> float:
    x1, y1, x2, y2 = box
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def contained_fraction(inner, outer) -> float:
    """How much of `inner`'s area lies inside `outer`, 0..1.

    Deliberately asymmetric. "Is this component on that board" is a question
    about the component's own extent, so a small part on a large board scores
    near 1.0 while the board scores near 0 against the part. IoU would answer
    a different question and would never fire for a chip on a motherboard.
    """
    if inner is None or outer is None:
        return 0.0
    area = _area(inner)
    if area <= 0:
        return 0.0
    ax1, ay1, ax2, ay2 = inner
    bx1, by1, bx2, by2 = outer
    overlap = max(0.0, min(ax2, bx2) - max(ax1, bx1)) * max(0.0, min(ay2, by2) - max(ay1, by1))
    return overlap / area


@dataclass
class Assembly:
    """One physical object: a container plus whatever sits on it.

    The id IS the physical item id. A standalone component is an assembly of
    one and keeps its own `AUR-ITEM-`, so nothing downstream needs to know
    whether it is holding a board or a bare chip.

    The result fields below are filled in by the session as the object moves
    through the machine. They live here rather than on a `TrackedItem` because
    the mass, the estimate and the servo command belong to the whole physical
    object, not to one of the boxes the camera drew on it.
    """

    assembly_id: str
    root: TrackedItem | None
    children: list[TrackedItem] = field(default_factory=list)

    weight_g: float | None = None
    weight_status: str | None = None
    weight_reading: dict | None = None
    valuation: dict | None = None
    decision: dict | None = None
    actuation: dict | None = None

    @property
    def members(self) -> list[TrackedItem]:
        """Every tracked item in this assembly, container first."""
        return ([self.root] if self.root is not None else []) + list(self.children)

    @property
    def item_id(self) -> str:
        """The physical item id, under the name the rest of the system uses."""
        return self.assembly_id

    @property
    def class_name(self) -> str | None:
        """The container's class, or the lone member's for an assembly of one.

        An assembly is identified by what carries it. It is not a claim that
        the object contains nothing else - `counts` is where the inventory is.
        """
        return self.root.class_name if self.root is not None else None

    @property
    def is_assembly(self) -> bool:
        """True when more than one component was found on this object."""
        return bool(self.children)

    @property
    def counts(self) -> dict[str, int]:
        """The component inventory: what was actually detected, and how many.

        Only detected components appear. Nothing is entered as zero, because
        "not seen" and "not there" are different facts and this cannot tell
        them apart.
        """
        tally: Counter = Counter()
        for member in self.members:
            if member.class_name:
                tally[member.class_name] += 1
        return dict(tally)

    @property
    def confidence(self) -> float | None:
        """The weakest member's mean confidence.

        The whole object is only as well identified as its least certain part:
        a servo fires on the assembly, so one shaky child must not be averaged
        away by four confident siblings.
        """
        values = [m.confidence for m in self.members if m.confidence is not None]
        return min(values) if values else None

    @property
    def bbox(self) -> tuple[int, int, int, int] | None:
        boxes = [m.bbox for m in self.members if m.bbox]
        if not boxes:
            return None
        return (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )

    def attach_weight(self, grams: float | None, status: str, timestamp: str | None = None) -> None:
        """Record one mass against the whole physical object.

        Also written through to the container, so `tracker.get(id).weight_g`
        keeps answering for the object the camera minted that id for. One mass,
        recorded once, reachable by either route - never two measurements.
        """
        self.weight_g = grams
        self.weight_status = status
        if self.root is not None:
            self.root.attach_weight(grams, status, timestamp)

    def as_dict(self) -> dict:
        """The record the API and the dashboard render.

        A superset of `TrackedItem.as_dict()`: the same keys mean the same
        things, so a standalone component serialises exactly as it did before
        assemblies existed, plus the inventory.
        """
        base = self.root.as_dict() if self.root is not None else {}
        return {
            **base,
            "item_id": self.assembly_id,
            "class_name": self.class_name,
            "confidence": self.confidence,
            "is_assembly": self.is_assembly,
            "components": self.counts,
            "member_ids": [m.item_id for m in self.members],
            "children": [
                {
                    "item_id": m.item_id,
                    "class_name": m.class_name,
                    "confidence": m.confidence,
                    "bbox": list(m.bbox) if m.bbox else None,
                }
                for m in self.children
            ],
            "bbox": list(self.bbox) if self.bbox else None,
            "weight_g": self.weight_g,
            "weight_status": self.weight_status,
            "weight_reading": self.weight_reading,
            "valuation": self.valuation,
            "decision": self.decision,
            "actuation": self.actuation,
        }


def _containers(items: list[TrackedItem], container_classes) -> list[TrackedItem]:
    return [i for i in items if i.class_name in container_classes and i.bbox]


def group(
    items: list[TrackedItem],
    cfg: config_module.Config | None = None,
) -> list[Assembly]:
    """Tracked items to physical assemblies, by spatial containment.

    A component belongs to the *smallest* container that holds enough of it,
    so a chip on a daughterboard on a motherboard attaches to the daughterboard
    rather than to both. Containers nest the same way, and the assembly is the
    outermost one - which is what a hand picks up and puts on the pan.

    A container must be strictly larger than what it contains. That is what
    makes the parent relation acyclic: two boxes cannot each be inside the
    other, so the walk to the root always terminates.
    """
    cfg = config_module.load() if cfg is None else cfg
    ratio = cfg["tracking.assembly.containment_ratio"]
    container_classes = set(cfg["tracking.assembly.container_classes"])

    items = [i for i in items if i.class_name]
    containers = _containers(items, container_classes)

    # ponytail: O(n * containers) per frame. n is one frame's detections;
    # a spatial index only earns its keep in the hundreds.
    parent: dict[str, TrackedItem] = {}
    for item in items:
        best: TrackedItem | None = None
        for container in containers:
            if container.item_id == item.item_id:
                continue
            if _area(container.bbox) <= _area(item.bbox):
                continue
            if contained_fraction(item.bbox, container.bbox) < ratio:
                continue
            if best is None or _area(container.bbox) < _area(best.bbox):
                best = container
        if best is not None:
            parent[item.item_id] = best

    def root_of(item: TrackedItem) -> TrackedItem:
        seen = {item.item_id}
        while item.item_id in parent:
            item = parent[item.item_id]
            if item.item_id in seen:
                break
            seen.add(item.item_id)
        return item

    grouped: dict[str, Assembly] = {}
    for item in items:
        root = root_of(item)
        assembly = grouped.get(root.item_id)
        if assembly is None:
            assembly = Assembly(assembly_id=root.item_id, root=root)
            grouped[root.item_id] = assembly
        if item.item_id != root.item_id:
            assembly.children.append(item)

    return list(grouped.values())
