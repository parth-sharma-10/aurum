"""Vision: detection to physical item identity.

`app.detector` finds objects in a frame. This package decides which frames
belong to the same physical object, which detected components make up one
physical object, and gives it the identity the load cell, the decision engine
and the ledger all use.
"""

from app.vision.assembly import Assembly, contained_fraction, group
from app.vision.tracker import (
    ACTIVE_STATES,
    DetectorTracker,
    ItemState,
    ItemTracker,
    TrackedDetection,
    TrackedItem,
    is_valid,
    new_item_id,
)

__all__ = [
    "ACTIVE_STATES",
    "Assembly",
    "DetectorTracker",
    "ItemState",
    "ItemTracker",
    "TrackedDetection",
    "TrackedItem",
    "contained_fraction",
    "group",
    "is_valid",
    "new_item_id",
]
