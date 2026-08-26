"""Tests for the camera-less run.

The property that matters is not that the script produces six records. It is
that a scripted object takes the same path a seen object takes: if this file
could make an item reach a bin by a shorter route than the camera does, the
fallback would be demonstrating something the machine does not do.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.pipeline import scripted
from app.pipeline.session import DemoSession


@pytest.fixture
def session(monkeypatch):
    """A session with no detector and no camera, which is the whole point."""
    monkeypatch.setenv("AURUM_DEMO_MOCK_MASS", "true")
    return DemoSession(detector=None)


def run_all(session) -> list[dict]:
    return [scripted.step(session) for _ in range(len(scripted.SCRIPT))]


class TestTheScript:
    def test_a_scripted_object_cannot_carry_a_bin(self):
        """The decision engine decides. A script with a bin field on it would
        demonstrate the script rather than the machine, so there is no field
        for one to be put in."""
        fields = {f.name for f in dataclasses.fields(scripted.ScriptedObject)}
        assert fields == {"component_class", "confidence", "shows"}

    def test_every_object_says_what_it_is_there_to_show(self):
        assert all(o.shows for o in scripted.SCRIPT)

    def test_each_object_gets_a_box_of_its_own(self):
        """Overlapping boxes would be grouped into one physical object, which
        is correct behaviour and exactly wrong between two scripted items."""
        boxes = [scripted._box(i) for i in range(len(scripted.SCRIPT))]
        assert len(set(boxes)) == len(boxes)


class TestSteppingThrough:
    def test_it_routes_every_object_without_a_camera(self, session):
        results = run_all(session)
        assert len(results) == len(scripted.SCRIPT)
        assert all(r.get("decision") for r in results)

    def test_the_four_known_classes_are_graded(self, session):
        results = run_all(session)
        graded = [r for r in results if r["decision"]["decision"] in ("A", "B", "C")]
        assert len(graded) == 4

    def test_a_low_confidence_detection_is_refused_not_graded(self, session):
        """Object 5 exists to fail. If it ever grades, the threshold moved."""
        results = run_all(session)
        assert results[4]["decision"]["reason_code"] == "UNKNOWN_CONFIDENCE"

    def test_an_unciteable_class_is_refused(self, session):
        results = run_all(session)
        assert results[5]["decision"]["reason_code"] == "UNKNOWN_CLASS"

    def test_the_mass_is_labelled_simulated_never_measured(self, session):
        for result in run_all(session):
            assert result["weight_status"] == "SIMULATED"

    def test_running_off_the_end_is_refused_rather_than_wrapping(self, session):
        run_all(session)
        out = scripted.step(session)
        assert out["error"] == "SCRIPT_EXHAUSTED"
        assert out["remaining"] == 0

    def test_the_remaining_count_counts_down(self, session):
        assert scripted.step(session)["remaining"] == len(scripted.SCRIPT) - 1
        assert scripted.step(session)["remaining"] == len(scripted.SCRIPT) - 2

    def test_a_reset_starts_the_script_again(self, session):
        run_all(session)
        session.reset()
        assert session.scripted_index == 0
        assert scripted.step(session).get("error") is None

    def test_each_object_is_a_distinct_item(self, session):
        """One physical item, one identity — the rule a camera-seen item is
        held to. Two scripted objects sharing an id would be one item routed
        twice, which is the thing the whole pipeline exists to prevent."""
        ids = [r["item_id"] for r in run_all(session)]
        assert len(set(ids)) == len(scripted.SCRIPT)


class TestWithoutAStandInMass:
    def test_it_still_runs_and_says_which_setting_is_missing(self, monkeypatch):
        monkeypatch.setenv("AURUM_DEMO_MOCK_MASS", "false")
        result = scripted.step(DemoSession(detector=None))
        assert result["weight_status"] == "UNAVAILABLE"
        assert "AURUM_DEMO_MOCK_MASS" in result["mass_advice"]

    def test_with_a_mass_there_is_no_advice_to_give(self, session):
        assert scripted.step(session)["mass_advice"] is None
