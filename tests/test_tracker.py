"""Tests for the item lifecycle.

The property the whole conveyor rests on: **one physical object gets one
identity, and closes exactly once.** A CPU seen in forty frames is one CPU, one
weighing, one decision, one servo firing and one ledger row.

No model, no camera and no weights are needed. `ItemTracker` is a pure state
machine over tracked detections, which is why it can be tested this way.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app import config
from app.vision.tracker import (
    ACTIVE_STATES,
    ItemState,
    ItemTracker,
    TrackedDetection,
    is_valid,
    new_item_id,
)

NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


def det(track_id=1, class_name="CPU", confidence=0.9, xyxy=(0, 0, 10, 10)):
    return TrackedDetection(track_id, class_name, confidence, xyxy)


@pytest.fixture
def tracker():
    """Small tolerances so a test does not have to simulate 15 blank frames."""
    return ItemTracker(max_missing_frames=3, min_detections_to_confirm=3)


def run(tracker, frames):
    """Feed a list of per-frame detection lists."""
    for frame_id, detections in enumerate(frames):
        tracker.update(detections, frame_id=frame_id, now=NOW)
    return tracker


class TestBasicIdentity:
    def test_one_detection_makes_one_item(self, tracker):
        active = tracker.update([det()], frame_id=0, now=NOW)
        assert len(active) == 1
        assert active[0].detection_count == 1

    def test_the_same_object_across_frames_stays_one_item(self, tracker):
        run(tracker, [[det()] for _ in range(10)])
        assert len(tracker.active) == 1
        assert tracker.active[0].detection_count == 10

    def test_two_objects_get_two_identities(self, tracker):
        run(tracker, [[det(1), det(2, "PCB")] for _ in range(4)])
        assert len({item.item_id for item in tracker.active}) == 2

    def test_an_empty_frame_creates_nothing(self, tracker):
        assert tracker.update([], frame_id=0, now=NOW) == []

    def test_no_detections_at_all_is_not_an_error(self, tracker):
        run(tracker, [[], [], []])
        assert tracker.active == []
        assert tracker.finalized == []


class TestItemIdIsNotTrackId:
    def test_the_item_id_is_aurum_s_own(self, tracker):
        item = tracker.update([det(track_id=17)], frame_id=0, now=NOW)[0]
        assert item.track_id == 17
        assert item.item_id.startswith("AUR-ITEM-")
        assert item.item_id != "17"

    def test_item_ids_are_unique_across_instances(self):
        """Track ids restart at 1 every process; ledger identities must not."""
        first = ItemTracker(max_missing_frames=3, min_detections_to_confirm=1)
        second = ItemTracker(max_missing_frames=3, min_detections_to_confirm=1)
        a = first.update([det(track_id=1)], frame_id=0, now=NOW)[0]
        b = second.update([det(track_id=1)], frame_id=0, now=NOW)[0]
        assert a.track_id == b.track_id == 1
        assert a.item_id != b.item_id

    def test_generated_ids_do_not_collide(self):
        assert len({new_item_id() for _ in range(2000)}) == 2000

    def test_the_item_id_is_stable_for_the_whole_life(self, tracker):
        first = tracker.update([det()], frame_id=0, now=NOW)[0].item_id
        run(tracker, [[det()] for _ in range(8)])
        assert tracker.active[0].item_id == first


class TestLifecycle:
    def test_the_states_progress_in_order(self, tracker):
        assert tracker.update([det()], frame_id=0, now=NOW)[0].state is ItemState.NEW
        assert tracker.update([det()], frame_id=1, now=NOW)[0].state is ItemState.TRACKING
        assert tracker.update([det()], frame_id=2, now=NOW)[0].state is ItemState.CONFIRMED

    def test_confirmation_uses_the_configured_threshold(self):
        tracker = ItemTracker(max_missing_frames=3, min_detections_to_confirm=5)
        run(tracker, [[det()] for _ in range(4)])
        assert tracker.active[0].state is ItemState.TRACKING
        tracker.update([det()], frame_id=4, now=NOW)
        assert tracker.active[0].state is ItemState.CONFIRMED

    def test_a_missing_frame_moves_the_item_to_leaving(self, tracker):
        run(tracker, [[det()] for _ in range(3)])
        tracker.update([], frame_id=3, now=NOW)
        assert tracker.active[0].state is ItemState.LEAVING

    def test_exceeding_the_tolerance_finalizes(self, tracker):
        run(tracker, [[det()] for _ in range(3)])
        run(tracker, [[] for _ in range(4)])
        assert tracker.active == []
        assert len(tracker.finalized) == 1
        assert tracker.finalized[0].state is ItemState.FINALIZED

    def test_every_active_state_is_in_the_active_set(self):
        assert ItemState.FINALIZED not in ACTIVE_STATES
        assert set(ACTIVE_STATES) == {
            ItemState.NEW,
            ItemState.TRACKING,
            ItemState.CONFIRMED,
            ItemState.LEAVING,
        }


class TestTrackLoss:
    def test_a_single_missed_frame_does_not_end_the_item(self, tracker):
        """An occluded frame is not a departure."""
        run(tracker, [[det()], [det()], [], [det()]])
        assert len(tracker.active) == 1
        assert tracker.finalized == []

    def test_the_item_keeps_its_identity_across_the_gap(self, tracker):
        first = tracker.update([det()], frame_id=0, now=NOW)[0].item_id
        run(tracker, [[], [], [det()]])
        assert tracker.active[0].item_id == first

    def test_reappearing_clears_the_missing_counter(self, tracker):
        run(tracker, [[det()], [], []])
        assert tracker.active[0].frames_since_seen == 2
        tracker.update([det()], frame_id=3, now=NOW)
        assert tracker.active[0].frames_since_seen == 0
        assert tracker.active[0].state is not ItemState.LEAVING

    def test_the_tolerance_boundary(self, tracker):
        """Exactly at the tolerance the item survives; one past it, it closes."""
        run(tracker, [[det()] for _ in range(3)])
        run(tracker, [[] for _ in range(3)])
        assert len(tracker.active) == 1
        tracker.update([], frame_id=99, now=NOW)
        assert tracker.active == []

    def test_a_reused_track_id_after_finalization_is_a_new_item(self, tracker):
        """ByteTrack can recycle a number; a closed item must not reopen."""
        run(tracker, [[det(track_id=1)] for _ in range(3)])
        run(tracker, [[] for _ in range(4)])
        closed = tracker.finalized[0].item_id
        tracker.update([det(track_id=1)], frame_id=50, now=NOW)
        assert tracker.active[0].item_id != closed


class TestDuplicatePrevention:
    def test_forty_frames_of_one_object_finalize_once(self, tracker):
        run(tracker, [[det()] for _ in range(40)])
        run(tracker, [[] for _ in range(4)])
        assert len(tracker.finalized) == 1

    def test_draining_hands_an_item_over_exactly_once(self, tracker):
        run(tracker, [[det()] for _ in range(3)])
        run(tracker, [[] for _ in range(4)])
        assert len(tracker.drain_finalized()) == 1
        assert tracker.drain_finalized() == []

    def test_finalizing_twice_is_a_no_op(self, tracker):
        item = tracker.update([det()], frame_id=0, now=NOW)[0]
        tracker.finalize(item)
        tracker.finalize(item)
        assert len(tracker.finalized) == 1

    def test_finalize_all_closes_everything_once(self, tracker):
        run(tracker, [[det(1), det(2, "PCB")] for _ in range(3)])
        tracker.finalize_all()
        tracker.finalize_all()
        assert len(tracker.finalized) == 2

    def test_a_finalized_item_is_no_longer_active(self, tracker):
        run(tracker, [[det()] for _ in range(3)])
        tracker.finalize_all()
        assert tracker.active == []


class TestConfidence:
    def test_confidence_is_the_mean_over_recent_observations(self, tracker):
        """Documented meaning: a lucky frame must not promote an item. Three
        observations is inside the window, so this is still a plain mean."""
        for frame_id, value in enumerate((0.5, 0.7, 0.9)):
            tracker.update([det(confidence=value)], frame_id=frame_id, now=NOW)
        item = tracker.active[0]
        assert item.confidence == pytest.approx(0.7)
        assert item.latest_confidence == 0.9
        assert item.max_confidence == 0.9

    def test_the_basis_is_stated_in_the_output(self, tracker):
        item = tracker.update([det()], frame_id=0, now=NOW)[0]
        basis = item.as_dict()["confidence_basis"]
        assert "mean over the last" in basis
        assert str(item.CONFIDENCE_WINDOW) in basis

    def test_the_placement_frames_stop_dragging_a_settled_item_down(self, tracker):
        """The rig's load cell sits UNDER the camera, so the tracker starts
        observing while the object is still being put down — hand over it,
        moving, half out of frame. A lifetime mean never forgot those, so a
        component that then sat still and read 0.9 kept a mean of 0.43 and was
        refused as UNKNOWN. A RAM did exactly that on 2026-08-26."""
        placement = [0.2] * 10  # being put down
        settled = [0.9] * 20  # sitting still, in plain view
        for frame_id, value in enumerate(placement + settled):
            tracker.update([det(confidence=value)], frame_id=frame_id, now=NOW)
        item = tracker.active[0]
        assert item.confidence == pytest.approx(0.9), "the recent view is the evidence"
        assert item.lifetime_confidence < 0.7, "the lifetime mean is still recorded"

    def test_an_unobserved_item_has_no_confidence(self):
        from app.vision.tracker import TrackedItem

        item = TrackedItem("AUR-ITEM-TEST", 1, 0, 0, "t", "t")
        assert item.confidence is None
        assert item.latest_confidence is None


class TestClassStability:
    def test_the_majority_class_wins_over_a_flicker(self, tracker):
        """A one-frame class flip must not change which bin an item is routed to."""
        frames = [[det(class_name="CPU")]] * 5
        frames.insert(2, [det(class_name="PCB")])
        run(tracker, frames)
        assert tracker.active[0].class_name == "CPU"

    def test_the_class_counts_are_kept_for_inspection(self, tracker):
        run(tracker, [[det(class_name="CPU")], [det(class_name="PCB")], [det(class_name="CPU")]])
        assert dict(tracker.active[0].class_counts) == {"CPU": 2, "PCB": 1}


class TestPositionAndMotion:
    def test_the_centre_follows_the_box(self, tracker):
        item = tracker.update([det(xyxy=(0, 0, 10, 20))], frame_id=0, now=NOW)[0]
        assert item.center == (5.0, 10.0)

    def test_velocity_is_reported_in_pixels_per_frame(self, tracker):
        tracker.update([det(xyxy=(0, 0, 10, 10))], frame_id=0, now=NOW)
        item = tracker.update([det(xyxy=(20, 0, 30, 10))], frame_id=1, now=NOW)[0]
        assert item.velocity == (20.0, 0.0)

    def test_velocity_is_not_converted_to_a_belt_speed(self, tracker):
        """Belt speed and pixel scale are UNMEASURED; converting would invent them."""
        tracker.update([det(xyxy=(0, 0, 10, 10))], frame_id=0, now=NOW)
        item = tracker.update([det(xyxy=(20, 0, 30, 10))], frame_id=1, now=NOW)[0]
        record = item.as_dict()
        assert "velocity_px_per_frame" in record
        assert not any("cm" in key for key in record)

    def test_a_single_observation_has_no_velocity(self, tracker):
        assert tracker.update([det()], frame_id=0, now=NOW)[0].velocity is None


class TestBadInput:
    """Malformed data is a skipped detection, never a stopped conveyor."""

    @pytest.mark.parametrize(
        "bad",
        [
            det(confidence=1.5),
            det(confidence=-0.1),
            det(confidence="high"),
            det(confidence=None),
            det(class_name=""),
            det(class_name=None),
            det(xyxy=(0, 0, 10)),
            det(xyxy=None),
            det(xyxy=(0, 0, "x", 10)),
            det(track_id="one"),
            det(track_id=None),
            "not a detection",
            None,
            42,
        ],
    )
    def test_invalid_detections_are_rejected_not_raised(self, tracker, bad):
        assert is_valid(bad) is False
        assert tracker.update([bad], frame_id=0, now=NOW) == []

    def test_rejections_are_counted_rather_than_hidden(self, tracker):
        tracker.update([det(confidence=2.0), det(class_name="")], frame_id=0, now=NOW)
        assert tracker.rejected_detections == 2

    def test_a_good_detection_alongside_a_bad_one_still_works(self, tracker):
        active = tracker.update([det(track_id=1), det(track_id="x")], frame_id=0, now=NOW)
        assert len(active) == 1
        assert tracker.rejected_detections == 1

    def test_none_instead_of_a_detection_list(self, tracker):
        assert tracker.update(None, frame_id=0, now=NOW) == []

    def test_a_boolean_is_not_a_track_id(self, tracker):
        """True == 1 in Python; it must not be mistaken for track 1."""
        assert is_valid(det(track_id=True)) is False


class TestConfiguration:
    def test_the_shipped_tolerances_load(self):
        tracker = ItemTracker(cfg=config.load(environ={}))
        assert tracker.max_missing_frames == 15
        assert tracker.min_detections_to_confirm == 3

    def test_the_tolerances_are_configurable(self):
        cfg = config.load(
            environ={"AURUM_TRACK_MAX_MISSING_FRAMES": "2", "AURUM_TRACK_MIN_DETECTIONS": "1"}
        )
        tracker = ItemTracker(cfg=cfg)
        assert tracker.max_missing_frames == 2
        assert tracker.min_detections_to_confirm == 1


class TestLookup:
    def test_an_item_can_be_found_by_its_id_while_active(self, tracker):
        item = tracker.update([det()], frame_id=0, now=NOW)[0]
        assert tracker.get(item.item_id) is item

    def test_an_item_can_still_be_found_after_finalization(self, tracker):
        item = tracker.update([det()], frame_id=0, now=NOW)[0]
        tracker.finalize_all()
        assert tracker.get(item.item_id) is item

    def test_an_unknown_id_returns_none(self, tracker):
        assert tracker.get("AUR-ITEM-NOPE") is None


class TestWeightSlot:
    """Phase 5 attaches a mass to this identity; it does not mint a new one."""

    def test_an_item_starts_with_no_weight(self, tracker):
        item = tracker.update([det()], frame_id=0, now=NOW)[0]
        assert item.weight_g is None
        assert item.weight_status is None

    def test_a_weight_attaches_with_its_status_and_timestamp(self, tracker):
        item = tracker.update([det()], frame_id=0, now=NOW)[0]
        item.attach_weight(42.7, "MEASURED")
        assert item.weight_g == 42.7
        assert item.weight_status == "MEASURED"
        assert item.weight_timestamp is not None

    def test_a_simulated_weight_keeps_its_label(self, tracker):
        item = tracker.update([det()], frame_id=0, now=NOW)[0]
        item.attach_weight(1840.0, "SIMULATED")
        assert item.as_dict()["weight_status"] == "SIMULATED"

    def test_the_decision_slot_starts_empty(self, tracker):
        """No grading logic lives in the tracker."""
        assert tracker.update([det()], frame_id=0, now=NOW)[0].decision is None
