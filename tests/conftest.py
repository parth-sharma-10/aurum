"""Shared test fixtures.

One job: keep the test suite out of the real records. Three of them are written
from deep inside the session, so a test that exercises the chain reaches all
three without ever mentioning them.

The EPR ledger, because a test would otherwise leave rows in
`data/aurum_epr.db` alongside real demonstration runs and "how many items has
this machine sorted" would start counting fixtures.

The physical-movement record, because it is bench evidence: a test must not be
able to claim a paddle was watched moving, and must not read a claim a real
bench check made either — a test whose result depends on whether anybody has
been to the bench is not a test.

The in-flight command marker, because the session clears it at construction. A
test suite left to use the real one would wipe the marker a killed run left
behind, which is the exact signal it exists to preserve.

Autouse rather than opt-in, because the cost of forgetting is silent
contamination of an audit trail rather than a failing test.
"""

from __future__ import annotations

import pytest

from app import epr
from app.hardware import recovery, verification


@pytest.fixture(autouse=True)
def isolated_records(tmp_path, monkeypatch):
    """Point every persistent record at throwaway files, for one test."""
    monkeypatch.setattr(epr, "DB", tmp_path / "epr.db")
    monkeypatch.setattr(verification, "RECORD", tmp_path / "movement_verification.json")
    monkeypatch.setattr(recovery, "MARKER", tmp_path / "in_flight.json")
    yield
