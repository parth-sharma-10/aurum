"""Shared test fixtures.

One job: keep the test suite out of the real databases. The EPR ledger is
written from deep inside the session - every stage of the pipeline appends an
event - so a test that exercises the chain would otherwise leave rows in
`data/aurum_epr.db` alongside real demonstration runs, and "how many items has
this machine sorted" would start counting fixtures.

Autouse rather than opt-in, because the cost of forgetting it is silent
contamination of an audit trail rather than a failing test.
"""

from __future__ import annotations

import pytest

from app import epr


@pytest.fixture(autouse=True)
def isolated_epr_ledger(tmp_path, monkeypatch):
    """Point the EPR ledger at a throwaway file for the length of one test."""
    monkeypatch.setattr(epr, "DB", tmp_path / "epr.db")
    yield
