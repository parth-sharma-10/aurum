"""Tests for batch composition.

The batch record is what leaves this system and enters the Aurum ledger, so the
two things worth pinning down are: counts must not depend on which frame the
operator happened to stop on, and no recovery figure may appear without cited
reference data behind it.
"""

from __future__ import annotations

import pytest

from app.batch import BatchSession, recovery_estimate

CLASSES = ["PCB", "RAM", "CPU", "Connector"]


@pytest.fixture
def session():
    return BatchSession(window=9, classes=CLASSES)


class TestStableCounting:
    def test_empty_session_reports_zeros_not_missing_keys(self, session):
        assert session.stable_counts() == dict.fromkeys(CLASSES, 0)

    def test_steady_scene_reports_that_scene(self, session):
        for _ in range(9):
            session.add_frame({"PCB": 2, "RAM": 1}, 0.9)
        counts = session.stable_counts()
        assert counts["PCB"] == 2
        assert counts["RAM"] == 1
        assert counts["CPU"] == 0

    def test_single_frame_dropout_does_not_change_the_count(self, session):
        """A hand crossing the bench for one frame must not alter the record."""
        for _ in range(8):
            session.add_frame({"RAM": 2}, 0.9)
        session.add_frame({}, 0.0)  # the dropout
        assert session.stable_counts()["RAM"] == 2

    def test_single_frame_spurious_double_does_not_change_the_count(self, session):
        for _ in range(8):
            session.add_frame({"CPU": 1}, 0.9)
        session.add_frame({"CPU": 5}, 0.9)  # one bad frame
        assert session.stable_counts()["CPU"] == 1

    def test_count_does_not_depend_on_the_final_frame(self, session):
        """The whole point of the median: stopping on a bad frame is harmless."""
        for _ in range(8):
            session.add_frame({"PCB": 3}, 0.9)
        before = session.stable_counts()
        session.add_frame({"PCB": 0}, 0.0)
        assert session.stable_counts() == before

    def test_window_forgets_an_old_scene(self, session):
        for _ in range(9):
            session.add_frame({"PCB": 4}, 0.9)
        for _ in range(9):
            session.add_frame({"RAM": 1}, 0.9)
        counts = session.stable_counts()
        assert counts["PCB"] == 0
        assert counts["RAM"] == 1

    def test_new_scene_clears_evidence_but_keeps_batch_identity(self, session):
        for _ in range(9):
            session.add_frame({"PCB": 2}, 0.9)
        bid = session.batch_id
        session.new_scene()
        assert session.batch_id == bid
        assert session.stable_counts()["PCB"] == 0

    def test_reset_starts_a_new_batch_id(self, session):
        first = session.batch_id
        session.reset()
        assert session.batch_id != first
        assert session.batch_id.startswith("AUR-")


class TestConfidence:
    def test_confidence_ignores_frames_with_no_detections(self, session):
        session.add_frame({"PCB": 1}, 0.8)
        session.add_frame({}, 0.0)
        session.add_frame({"PCB": 1}, 0.9)
        assert session.average_confidence() == pytest.approx(0.85)

    def test_no_detections_gives_zero_not_a_crash(self, session):
        session.add_frame({}, 0.0)
        assert session.average_confidence() == 0.0


class TestRecord:
    def test_record_has_the_documented_shape(self, session):
        for _ in range(9):
            session.add_frame({"PCB": 2, "RAM": 1, "CPU": 1}, 0.93)
        rec = session.record("Aurum Vision v0.1")
        assert rec["detections"] == {"PCB": 2, "RAM": 1, "CPU": 1, "Connector": 0}
        assert rec["total_objects"] == 4
        assert rec["model_version"] == "Aurum Vision v0.1"
        assert rec["batch_id"].startswith("AUR-")
        assert "counting_method" in rec
        assert rec["frames_observed"] == 9

    def test_frames_observed_counts_every_frame_not_just_the_window(self, session):
        for _ in range(25):
            session.add_frame({"RAM": 1}, 0.9)
        assert session.record("v")["frames_observed"] == 25

    def test_simulated_weight_is_flagged_in_the_record(self, session):
        session.add_frame({"RAM": 1}, 0.9)
        rec = session.record(
            "v", weight={"kg": 1.84, "simulated": True, "warning": "SIMULATED SENSOR"}
        )
        assert rec["weight"]["simulated"] is True


class TestRecoveryEstimateGuard:
    def test_partial_without_reference_data_for_every_class(self):
        est = recovery_estimate({"PCB": 2, "RAM": 1})
        assert est["completeness"] != "COMPLETE"
        assert est["reason"]

    def test_disclaimer_is_present_even_when_unavailable(self):
        est = recovery_estimate({"PCB": 1})
        assert "ESTIMATE ONLY" in est["disclaimer"]
        assert "does not measure precious-metal content" in est["disclaimer"]

    def test_no_numeric_value_leaks_for_a_class_with_no_evidence(self):
        """Nothing a UI could mistake for a real figure.

        A partial estimate may carry numbers - for the components it could
        actually value. What it must never carry is a line, a total or a zero
        for a class the database says nothing about.
        """
        est = recovery_estimate({"PCB": 2, "RAM": 3, "CPU": 1})
        unvalued = {n["component"] for n in est["not_valued"]}
        assert {"PCB", "RAM"} <= unvalued
        for line in est.get("components", []):
            assert line["component"] not in unvalued
        for entry in est["not_valued"]:
            assert entry["reason"]

    def test_nothing_at_all_is_valued_when_no_class_has_evidence(self):
        est = recovery_estimate({"RAM": 3})
        assert est["available"] is False
        assert est["completeness"] == "INSUFFICIENT_EVIDENCE"
        assert "components" not in est
        assert est.get("basis") is None
