"""Tests for the item pipeline.

What matters here is the contract the later phases depend on: the identity a
frame produces is the same identity the load cell, the decision engine and the
ledger will act on, and it is handed over exactly once.

The detector is stubbed. A pipeline must be exercisable with no model, no
camera and no weights, because that is also how the demo runs when hardware
fails.
"""

from __future__ import annotations

import numpy as np
import pytest

from app import config
from app.pipeline import ItemPipeline
from app.vision.tracker import ItemState, TrackedDetection


def det(track_id=1, class_name="CPU", confidence=0.9, xyxy=(0, 0, 10, 10)):
    return TrackedDetection(track_id, class_name, confidence, xyxy)


class StubDetectorTracker:
    """Stands in for ByteTrack: replays a scripted list of per-frame detections."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.resets = 0

    def track(self, _frame):
        frame = self.script[self.calls] if self.calls < len(self.script) else []
        self.calls += 1
        return frame

    def reset(self):
        self.resets += 1


@pytest.fixture
def fast_cfg():
    """Short tolerances so a test need not simulate fifteen blank frames."""
    return config.load(
        environ={"AURUM_TRACK_MAX_MISSING_FRAMES": "2", "AURUM_TRACK_MIN_DETECTIONS": "2"}
    )


@pytest.fixture
def pipeline(fast_cfg):
    return ItemPipeline(cfg=fast_cfg)


class TestSimulatedOperation:
    def test_a_pipeline_runs_with_no_detector_at_all(self, pipeline):
        assert pipeline.simulated is True
        active = pipeline.process_detections([det()], frame_id=0)
        assert len(active) == 1

    def test_frames_are_counted(self, pipeline):
        for frame_id in range(4):
            pipeline.process_detections([det()], frame_id=frame_id)
        assert pipeline.frames_processed == 4

    def test_process_frame_without_a_detector_says_what_to_do(self, pipeline):
        with pytest.raises(RuntimeError, match="process_detections"):
            pipeline.process_frame(np.zeros((4, 4, 3), dtype=np.uint8))

    def test_simulation_configuration_marks_a_real_detector_as_simulated(self, fast_cfg):
        cfg = config.load(
            environ={
                "AURUM_SIMULATION": "true",
                "AURUM_TRACK_MAX_MISSING_FRAMES": "2",
                "AURUM_TRACK_MIN_DETECTIONS": "2",
            }
        )
        pipeline = ItemPipeline(detector=object(), cfg=cfg)
        assert pipeline.simulated is True


class TestIdentityFlowsThrough:
    def test_the_same_item_id_is_returned_every_frame(self, pipeline):
        first = pipeline.process_detections([det()], frame_id=0)[0].item_id
        for frame_id in range(1, 6):
            assert pipeline.process_detections([det()], frame_id=frame_id)[0].item_id == first

    def test_two_objects_keep_separate_identities(self, pipeline):
        for frame_id in range(3):
            pipeline.process_detections([det(1), det(2, "PCB")], frame_id=frame_id)
        assert len({item.item_id for item in pipeline.active_items}) == 2

    def test_the_finalized_item_carries_the_identity_it_had_while_tracking(self, pipeline):
        closed = []
        pipeline.on_finalized = closed.append
        tracked = pipeline.process_detections([det()], frame_id=0)[0].item_id
        for frame_id in range(1, 6):
            pipeline.process_detections([], frame_id=frame_id)
        assert [item.item_id for item in closed] == [tracked]


class TestCurrentItem:
    def test_there_is_no_current_item_before_anything_is_seen(self, pipeline):
        assert pipeline.current_item is None

    def test_an_unconfirmed_item_is_not_current(self, pipeline):
        """One sighting is a flicker, not something to weigh or route."""
        pipeline.process_detections([det()], frame_id=0)
        assert pipeline.active_items[0].state is ItemState.NEW
        assert pipeline.current_item is None

    def test_a_confirmed_item_becomes_current(self, pipeline):
        for frame_id in range(2):
            pipeline.process_detections([det()], frame_id=frame_id)
        assert pipeline.current_item is not None
        assert pipeline.current_item.state is ItemState.CONFIRMED

    def test_the_most_recently_seen_confirmed_item_wins(self, pipeline):
        for frame_id in range(3):
            pipeline.process_detections([det(1)], frame_id=frame_id)
        for frame_id in range(3, 6):
            pipeline.process_detections([det(1), det(2, "PCB")], frame_id=frame_id)
        assert pipeline.current_item.track_id in (1, 2)
        assert pipeline.current_item.state is ItemState.CONFIRMED


class TestFinalizationHappensOnce:
    def test_the_callback_fires_once_per_physical_item(self, pipeline):
        closed = []
        pipeline.on_finalized = closed.append
        for frame_id in range(20):
            pipeline.process_detections([det()], frame_id=frame_id)
        for frame_id in range(20, 25):
            pipeline.process_detections([], frame_id=frame_id)
        assert len(closed) == 1

    def test_finish_closes_items_still_on_the_belt(self, pipeline):
        """Items in view when the operator stops must still reach the ledger."""
        for frame_id in range(3):
            pipeline.process_detections([det(1), det(2, "PCB")], frame_id=frame_id)
        assert len(pipeline.finish()) == 2

    def test_finishing_twice_closes_nothing_more(self, pipeline):
        for frame_id in range(3):
            pipeline.process_detections([det()], frame_id=frame_id)
        assert len(pipeline.finish()) == 1
        assert pipeline.finish() == []

    def test_the_callback_does_not_fire_twice_on_a_second_finish(self, pipeline):
        closed = []
        pipeline.on_finalized = closed.append
        pipeline.process_detections([det()], frame_id=0)
        pipeline.finish()
        pipeline.finish()
        assert len(closed) == 1


class TestWeightAttachment:
    def test_a_mass_attaches_to_an_existing_identity(self, pipeline):
        item = pipeline.process_detections([det()], frame_id=0)[0]
        updated = pipeline.attach_weight(item.item_id, 42.7, "MEASURED")
        assert updated is item
        assert item.weight_g == 42.7

    def test_attaching_to_an_unknown_id_returns_none_rather_than_raising(self, pipeline):
        assert pipeline.attach_weight("AUR-ITEM-NOPE", 1.0, "MEASURED") is None

    def test_a_weight_survives_finalization(self, pipeline):
        closed = []
        pipeline.on_finalized = closed.append
        item = pipeline.process_detections([det()], frame_id=0)[0]
        pipeline.attach_weight(item.item_id, 42.7, "MEASURED")
        pipeline.finish()
        assert closed[0].weight_g == 42.7


class TestDetectorIntegration:
    def test_frames_are_routed_through_the_tracking_adapter(self, fast_cfg):
        pipeline = ItemPipeline(cfg=fast_cfg)
        pipeline.detector_tracker = StubDetectorTracker([[det()], [det()], [det()]])
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        for frame_id in range(3):
            pipeline.process_frame(frame, frame_id=frame_id)
        assert pipeline.detector_tracker.calls == 3
        assert len(pipeline.active_items) == 1

    def test_a_reset_starts_a_fresh_run(self, fast_cfg):
        pipeline = ItemPipeline(cfg=fast_cfg)
        stub = StubDetectorTracker([[det()]])
        pipeline.detector_tracker = stub
        first = pipeline.process_frame(np.zeros((4, 4, 3), dtype=np.uint8), frame_id=0)[0].item_id
        pipeline.reset()
        assert pipeline.active_items == []
        assert pipeline.frames_processed == 0
        assert stub.resets == 1
        stub.script = [[det()]]
        stub.calls = 0
        second = pipeline.process_frame(np.zeros((4, 4, 3), dtype=np.uint8), frame_id=0)[0].item_id
        assert second != first


class TestSnapshot:
    def test_the_snapshot_reports_the_run(self, pipeline):
        for frame_id in range(3):
            pipeline.process_detections([det()], frame_id=frame_id)
        snapshot = pipeline.snapshot()
        assert snapshot["frames_processed"] == 3
        assert snapshot["active_count"] == 1
        assert snapshot["current_item"]["item_id"].startswith("AUR-ITEM-")

    def test_the_snapshot_labels_simulation(self, pipeline):
        assert pipeline.snapshot()["simulated"] is True

    def test_the_snapshot_states_the_tracking_policy_is_approximate(self, pipeline):
        note = pipeline.snapshot()["tracking_policy"]["note"]
        assert "engineering approximations" in note

    def test_rejected_detections_are_visible(self, pipeline):
        pipeline.process_detections([det(confidence=5.0)], frame_id=0)
        assert pipeline.snapshot()["rejected_detections"] == 1

    def test_no_grading_happens_in_the_pipeline(self, pipeline):
        """A/B/C belongs to app.decision and is filled in by a later phase."""
        for frame_id in range(3):
            pipeline.process_detections([det()], frame_id=frame_id)
        assert pipeline.current_item.decision is None
        blob = str(pipeline.snapshot()).lower()
        assert "bin_a" not in blob
        assert "servo" not in blob
