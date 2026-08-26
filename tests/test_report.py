"""Tests for the CSV export.

The property under test is not that the file parses. It is that no figure
leaves this module without the status of what it was derived from: a mass
column without `mass_status` looks measured and may be assumed, and a value
without `price_status` looks like a market price and may be a reference figure
from last week.
"""

from __future__ import annotations

import csv
import io

import pytest

from app import report
from app.pipeline import scripted
from app.pipeline.session import DemoSession


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setenv("AURUM_DEMO_MOCK_MASS", "true")
    session = DemoSession(detector=None)
    for _ in range(len(scripted.SCRIPT)):
        scripted.step(session)
    return session.snapshot()


def parsed(snapshot) -> list[dict]:
    return list(csv.DictReader(io.StringIO(report.to_csv(snapshot))))


class TestTheColumns:
    def test_every_figure_is_paired_with_what_backs_it(self):
        for figure, status in (
            ("mass_g", "mass_status"),
            ("contained_value", "price_status"),
            ("precious_value", "price_status"),
        ):
            assert figure in report.COLUMNS
            assert status in report.COLUMNS

    def test_an_empty_run_is_a_header_and_nothing_else(self):
        text = report.to_csv({"items": []})
        assert text.strip() == ",".join(report.COLUMNS)

    def test_a_snapshot_missing_everything_does_not_raise(self):
        """Records differ by how far the item got. Missing is an empty cell."""
        assert report.to_csv({}) == report.to_csv({"items": []})


class TestARun:
    def test_one_row_per_decided_item(self, run):
        assert len(parsed(run)) == len(scripted.SCRIPT)

    def test_rows_come_out_oldest_first(self, run):
        seen = [r["first_seen"] for r in parsed(run)]
        assert seen == sorted(seen)

    def test_a_simulated_mass_is_never_exported_as_measured(self, run):
        assert {r["mass_status"] for r in parsed(run)} == {"SIMULATED"}

    def test_a_refused_item_carries_its_reason_and_no_servo(self, run):
        refused = [r for r in parsed(run) if r["decision"] == "UNKNOWN"]
        assert len(refused) == 2
        assert all(r["servo"] == "" for r in refused)
        assert {r["reason_code"] for r in refused} == {
            "UNKNOWN_CONFIDENCE",
            "UNKNOWN_CLASS",
        }

    def test_a_class_with_no_citable_composition_exports_no_value(self, run):
        [heatsink] = [r for r in parsed(run) if r["class_name"] == "Heatsink"]
        assert heatsink["contained_value"] == ""
        assert heatsink["precious_value"] == ""
        assert heatsink["reason_code"] == "UNKNOWN_CLASS"

    def test_a_precious_figure_survives_an_unavailable_base_price(self, run):
        """The PCB's copper has no price, so it has no contained_value at all.
        Exporting only that column dropped the largest precious figure in the
        set from the row that had it."""
        [pcb] = [r for r in parsed(run) if r["class_name"] == "PCB"]
        assert pcb["contained_value"] == ""
        assert float(pcb["precious_value"]) > 0

    def test_nothing_that_was_not_commanded_reads_as_commanded(self, run):
        """No board is attached, so every row must say so."""
        assert {r["commanded"] for r in parsed(run)} == {"False"}
        assert {r["actuation_state"] for r in parsed(run)} == {""}


class TestTheFilename:
    def test_it_is_named_after_the_run(self, run):
        assert report.filename(run) == f"{run['epr']['session_id']}.csv"

    def test_a_snapshot_with_no_session_id_still_gets_a_name(self):
        assert report.filename({}).endswith(".csv")
