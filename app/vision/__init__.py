"""Vision: detection to physical item identity.

`app.detector` finds objects in a frame. This package decides which frames
belong to the same physical object, and gives it the identity the load cell,
the decision engine and the ledger all use.
"""

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
    "DetectorTracker",
    "ItemState",
    "ItemTracker",
    "TrackedDetection",
    "TrackedItem",
    "is_valid",
    "new_item_id",
]
