"""Tests for the physical-movement claim.

The property under test is a negative one, and it is the point of the module:
nothing in software can produce PHYSICAL_MOVEMENT_VERIFIED. An ACK cannot, a
successful command cannot, a clean bench check cannot. Only a recorded human
observation can, because there is no encoder and no camera on the paddle.
"""

from __future__ import annotations

import json

import pytest

from app.hardware import verification
from app.hardware.verification import MovementVerification, Observation


@pytest.fixture
def record(tmp_path):
    return tmp_path / "movement_verification.json"


class TestTheDefaultClaim:
    def test_an_absent_record_is_unavailable_not_verified(self, record):
        assert verification.state_for("A", record) is MovementVerification.VERIFICATION_UNAVAILABLE

    def test_an_unreadable_record_fails_towards_unverified(self, record):
        """The safe direction. A corrupt file must not read as a verification."""
        record.write_text("{ not json")
        assert verification.observations(record) == []
        assert verification.state_for("A", record) is MovementVerification.VERIFICATION_UNAVAILABLE

    def test_a_record_of_the_wrong_shape_is_ignored(self, record):
        record.write_text(json.dumps({"servo": "A"}))
        assert verification.observations(record) == []

    def test_entries_that_do_not_parse_are_skipped_not_fatal(self, record):
        record.write_text(
            json.dumps(
                [{"nonsense": True}, {"servo": "A", "moved": True, "by": "x", "at": "2026-08-26"}]
            )
        )
        assert [o.servo for o in verification.observations(record)] == ["A"]

    def test_the_snapshot_says_how_to_produce_the_claim(self, record):
        snapshot = verification.snapshot(path=record)
        assert snapshot["verified"] == []
        assert "bench_check" in snapshot["how"]
        assert all(s["state"] == "VERIFICATION_UNAVAILABLE" for s in snapshot["servos"].values())


class TestRecordingAnObservation:
    def test_a_watched_paddle_becomes_verified(self, record):
        verification.record("A", moved=True, by="parth", rest_deg=0, push_deg=90, path=record)
        assert (
            verification.state_for("A", record) is MovementVerification.PHYSICAL_MOVEMENT_VERIFIED
        )

    def test_verifying_one_servo_says_nothing_about_the_other(self, record):
        verification.record("A", moved=True, by="parth", path=record)
        assert verification.state_for("B", record) is MovementVerification.VERIFICATION_UNAVAILABLE
        assert verification.snapshot(path=record)["verified"] == ["A"]

    def test_a_paddle_that_acknowledged_and_did_not_move_is_recorded_too(self, record):
        """The most valuable observation the bench can produce. Discarding it
        would leave a known-broken rig looking merely untested."""
        verification.record("A", moved=False, by="parth", path=record)
        assert verification.state_for("A", record) is MovementVerification.VERIFICATION_UNAVAILABLE
        assert verification.observations(record)[0].moved is False

    def test_nothing_is_ever_overwritten(self, record):
        verification.record("A", moved=True, by="parth", path=record)
        verification.record("A", moved=False, by="parth", path=record)
        assert len(verification.observations(record)) == 2

    def test_the_latest_observation_is_the_one_that_counts(self, record):
        """A servo seen moving in March and jammed in April is not verified."""
        verification.record("A", moved=True, by="parth", path=record)
        verification.record("A", moved=False, by="parth", path=record)
        assert verification.state_for("A", record) is MovementVerification.VERIFICATION_UNAVAILABLE

    def test_it_records_the_throw_that_was_verified(self, record):
        """A paddle verified at one throw is not a paddle verified at another."""
        verification.record(
            "A", moved=True, by="parth", rest_deg=0, push_deg=90, hold_ms=700, path=record
        )
        latest = verification.latest_for("A", record)
        assert (latest.rest_deg, latest.push_deg, latest.hold_ms) == (0, 90, 700)
        assert "0->90 deg" in latest.summary

    def test_the_record_survives_a_reread(self, record):
        written = verification.record("A", moved=True, by="parth", path=record)
        assert verification.observations(record) == [written]

    def test_it_creates_the_directory_it_needs(self, tmp_path):
        nested = tmp_path / "reports" / "movement_verification.json"
        verification.record("A", moved=True, by="parth", path=nested)
        assert nested.exists()


class TestTheSummaryLine:
    def test_an_unverified_servo_says_nobody_has_watched_it(self, record):
        detail = verification.snapshot(path=record)["servos"]["B"]["detail"]
        assert "No one has recorded watching SERVO_B move" in detail

    def test_a_failed_observation_does_not_read_as_a_success(self):
        observation = Observation(
            servo="A", moved=False, by="parth", at="2026-08-26T00:00:00+00:00"
        )
        assert "NOT seen to move" in observation.summary
