"""The pipeline seams: demo → ledger → API, and counts → recovery → valuation.

Two properties are load-bearing here and neither is obvious from reading a
single module.

First, a batch saved from the OpenCV demo must be retrievable through the API,
because until it was, an operator saved a batch on stage and it appeared
nowhere else. There is one INSERT in the codebase and both callers use it.

Second, every number derived downstream of a detection must be reachable only
through data that carries a citation. Recovery needs sourced yields; valuation
needs a sourced price. With neither configured — the shipped state — both must
refuse rather than return a plausible figure. The tests that supply data do so
from fixtures written here, never from the repository's own configs, so a test
can never be the reason production starts emitting numbers.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest
import yaml
from fastapi.testclient import TestClient

from app import api as api_mod
from app import batch as batch_mod
from app import ledger as ledger_mod
from app import materials as materials_mod
from app import pricing
from app.batch import BatchSession, recovery_estimate
from app.pricing import PriceQuote, StaticPriceProvider, value_recovery

CLASSES = ["PCB", "RAM", "CPU", "Connector"]


@pytest.fixture
def ledger_db(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_mod, "DB", tmp_path / "ledger.db")
    monkeypatch.setattr(batch_mod, "BATCH_DIR", tmp_path / "batches")
    ledger_mod.init_db()
    return ledger_mod.DB


def _record(**over):
    session = BatchSession(window=5, classes=CLASSES)
    for _ in range(5):
        session.add_frame({"PCB": 1, "CPU": 2}, 0.9)
    rec = session.record("Aurum Vision v-test", source="webcam")
    rec.update(over)
    return rec


# ---------------------------------------------------------------------------
# demo → ledger → API
# ---------------------------------------------------------------------------
class TestDemoBatchReachesTheLedger:
    def test_demo_save_writes_both_the_json_and_the_ledger(self, ledger_db):
        from app.demo import _persist

        session = BatchSession(window=5, classes=CLASSES)
        for _ in range(5):
            session.add_frame({"RAM": 1}, 0.8)
        rec = session.record("Aurum Vision v-test", source="webcam")

        path = _persist(session, rec)

        assert path.exists(), "the demo's own JSON artifact must still be written"
        assert json.loads(path.read_text())["batch_id"] == rec["batch_id"]
        assert ledger_mod.get(rec["batch_id"])["batch_id"] == rec["batch_id"]

    def test_a_demo_batch_is_retrievable_through_the_api(self, ledger_db, monkeypatch):
        """The property that was broken: saved on stage, visible in the dashboard."""
        from app.demo import _persist

        session = BatchSession(window=5, classes=CLASSES)
        for _ in range(5):
            session.add_frame({"PCB": 2}, 0.95)
        rec = session.record("Aurum Vision v-test", source="webcam")
        _persist(session, rec)

        weights = ledger_db.parent / "w.pt"
        weights.write_bytes(b"stub")
        monkeypatch.setattr(api_mod, "DEFAULT_WEIGHTS", weights)
        with TestClient(api_mod.app) as c:
            listed = c.get("/batches").json()
            assert rec["batch_id"] in [b["batch_id"] for b in listed["batches"]]
            fetched = c.get(f"/batches/{rec['batch_id']}").json()
            assert fetched["detections"]["PCB"] == 2
            assert fetched["source"] == "webcam"
            assert c.get("/stats").json()["batch_count"] == 1

    def test_saving_the_same_batch_twice_does_not_duplicate_it(self, ledger_db):
        from app.demo import _persist

        session = BatchSession(window=5, classes=CLASSES)
        session.add_frame({"CPU": 1}, 0.9)
        rec = session.record("v", source="webcam")
        _persist(session, rec)
        _persist(session, rec)
        with closing(sqlite3.connect(ledger_db)) as con:
            assert con.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1

    def test_a_ledger_failure_does_not_lose_the_batch_or_raise(self, ledger_db, capsys):
        """A locked database mid-demo must not end the demo."""
        from app import demo as demo_mod
        from app.demo import _persist

        def boom(_record):
            raise sqlite3.OperationalError("database is locked")

        session = BatchSession(window=5, classes=CLASSES)
        session.add_frame({"RAM": 1}, 0.9)
        rec = session.record("v", source="webcam")

        original = demo_mod.ledger.save
        demo_mod.ledger.save = boom
        try:
            path = _persist(session, rec)
        finally:
            demo_mod.ledger.save = original

        assert path.exists(), "the JSON must survive a ledger failure"
        assert "ledger write failed" in capsys.readouterr().out

    def test_the_ledger_has_exactly_one_insert_in_the_codebase(self):
        """Guards the single-persistence-path rule against a future second INSERT."""
        import re
        from pathlib import Path

        app_dir = Path(ledger_mod.__file__).parent
        statement = re.compile(r"INSERT\s+(OR\s+\w+\s+)?INTO", re.IGNORECASE)
        offenders = [
            p.name
            for p in app_dir.glob("*.py")
            if p.name != "ledger.py" and statement.search(p.read_text())
        ]
        assert offenders == [], f"SQL INSERT outside app/ledger.py: {offenders}"


# ---------------------------------------------------------------------------
# recovery
# ---------------------------------------------------------------------------
def _yield_config(tmp_path, monkeypatch, **entries):
    """Point the material layer at a throwaway database built from `entries`.

    Each entry is one class with one per-piece metal figure, which is the
    smallest thing the estimator will act on. Written out as real files rather
    than stubbed, so these tests exercise the same load-and-resolve path the
    shipped database goes through.
    """
    metals = {"gold": "Au", "copper": "Cu", "silver": "Ag"}
    evidence, components = [], {}
    for cls, spec in entries.items():
        metal = metals[spec["material"]]
        eid = f"{cls.upper()}-{metal.upper()}-T1"
        record = {
            "id": eid,
            "component": cls,
            "subtype": "test_fixture",
            "metal": metal,
            "quantity": "per_piece",
            "value": spec["value"],
            "unit": spec["unit"],
            "evidence_type": "measured",
            "confidence": "high",
        }
        if "source" in spec:
            record["source"] = spec["source"]
        evidence.append(record)
        components[cls] = {
            "default_subtype": "test_fixture",
            "subtypes": {"test_fixture": {"composition": {metal: eid}}},
        }

    ref = tmp_path / "material_reference.yaml"
    ref.write_text(
        yaml.safe_dump(
            {
                "enabled": True,
                "basis": "test fixture",
                "evidence": evidence,
                "components": components,
                "recovery": {"available": False, "reason": "test fixture", "factors": []},
            }
        )
    )
    src = tmp_path / "material_sources.yaml"
    src.write_text(
        yaml.safe_dump(
            {
                "sources": [
                    {
                        "id": "TEST FIXTURE",
                        "title": "Test fixture, not a real study",
                        "authors": ["Fixture"],
                        "year": 1970,
                        "journal": "n/a",
                        "url": "https://example.invalid/fixture",
                    },
                    {
                        "id": "T",
                        "title": "Test fixture, not a real study",
                        "authors": ["Fixture"],
                        "year": 1970,
                        "journal": "n/a",
                        "url": "https://example.invalid/fixture",
                    },
                ]
            }
        )
    )
    monkeypatch.setattr(materials_mod, "REFERENCE", ref)
    monkeypatch.setattr(materials_mod, "SOURCES", src)
    return ref


class TestRecoveryEstimate:
    def test_disabled_by_default_in_this_repository(self):
        """The shipped configuration must never produce a figure."""
        est = recovery_estimate({"PCB": 2, "RAM": 1})
        assert est["available"] is False
        assert "components" not in est
        assert est["reason"]

    def test_the_three_quantities_stay_distinguishable(self):
        est = recovery_estimate({"PCB": 2})
        assert est["detected_components"] == {"PCB": 2}  # measured by the model
        assert est["kind"] == "ESTIMATE"
        assert est["measured_material"]["available"] is False

    def test_estimated_recovery_is_never_labelled_measured(self, tmp_path, monkeypatch):
        _yield_config(
            tmp_path,
            monkeypatch,
            PCB={"material": "gold", "value": 0.02, "unit": "g", "source": "TEST FIXTURE"},
        )
        est = recovery_estimate({"PCB": 3})
        assert est["available"] is True
        assert est["kind"] == "ESTIMATE"
        assert est["measured_material"]["available"] is False
        assert "ESTIMATE ONLY" in est["disclaimer"]

    def test_calculation_is_count_times_per_unit_yield(self, tmp_path, monkeypatch):
        _yield_config(
            tmp_path,
            monkeypatch,
            CPU={"material": "gold", "value": 0.25, "unit": "g", "source": "TEST FIXTURE"},
        )
        line = recovery_estimate({"CPU": 4})["components"][0]
        assert line["count"] == 4
        assert line["per_unit"] == 0.25
        assert line["total"] == 1.0
        assert line["material"] == "gold"
        assert "CPU-AU-T1" in line["source"]  # the line carries its own citation

    def test_a_detected_class_with_no_cited_yield_blocks_the_estimate(self, tmp_path, monkeypatch):
        _yield_config(
            tmp_path,
            monkeypatch,
            PCB={"material": "gold", "value": 0.02, "unit": "g", "source": "TEST FIXTURE"},
        )
        est = recovery_estimate({"PCB": 1, "RAM": 2})
        assert est["available"] is False
        assert "RAM" in est["reason"]

    def test_a_yield_missing_its_source_is_treated_as_absent(self, tmp_path, monkeypatch):
        """An uncitable figure is not a usable figure."""
        _yield_config(tmp_path, monkeypatch, PCB={"material": "gold", "value": 0.02, "unit": "g"})
        est = recovery_estimate({"PCB": 1})
        assert est["available"] is False
        assert "source" in est["reason"]

    def test_a_missing_reference_file_is_not_a_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr(materials_mod, "REFERENCE", tmp_path / "absent.yaml")
        assert recovery_estimate({"PCB": 1})["available"] is False


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------
@pytest.fixture
def priced_recovery(tmp_path, monkeypatch):
    _yield_config(
        tmp_path,
        monkeypatch,
        CPU={"material": "gold", "value": 0.25, "unit": "g", "source": "TEST FIXTURE"},
    )
    return recovery_estimate({"CPU": 4})


def _provider(price=10.0, unit="g", currency="USD"):
    return StaticPriceProvider(
        {
            "gold": {
                "price_per_unit": price,
                "unit": unit,
                "currency": currency,
                "timestamp": "2026-01-01T00:00:00Z",
                "source": "TEST FIXTURE — not a market price",
            }
        },
        source="test-provider",
    )


class TestValuation:
    def test_disabled_by_default_in_this_repository(self):
        """No price data ships, so production valuation must refuse."""
        assert pricing.get_provider() is None

    def test_quantity_times_price_is_the_value(self, priced_recovery):
        out = value_recovery(priced_recovery, _provider(price=10.0))
        assert out["available"] is True
        # 4 CPUs x 0.25 g = 1.0 g, at 10.0 USD/g
        assert out["components"][0]["estimated_quantity"] == 1.0
        assert out["estimated_value"] == 10.0
        assert out["currency"] == "USD"

    def test_the_quote_travels_with_the_result(self, priced_recovery):
        price = value_recovery(priced_recovery, _provider())["components"][0]["price"]
        assert set(price) == {
            "material",
            "price_per_unit",
            "unit",
            "currency",
            "timestamp",
            "source",
        }
        assert price["timestamp"] == "2026-01-01T00:00:00Z"

    def test_result_carries_a_calculation_version(self, priced_recovery):
        assert value_recovery(priced_recovery, _provider())["calculation_version"]

    def test_result_is_labelled_an_estimate(self, priced_recovery):
        out = value_recovery(priced_recovery, _provider())
        assert out["kind"] == "ESTIMATE"
        assert "ESTIMATE ONLY" in out["disclaimer"]
        assert "not an assay" in out["disclaimer"]

    def test_no_price_source_means_no_value(self, priced_recovery):
        out = value_recovery(priced_recovery, provider=None)
        assert out["available"] is False
        assert "No price source configured" in out["reason"]
        assert "estimated_value" not in out

    def test_no_recovery_estimate_means_no_value(self):
        out = value_recovery({"available": False, "reason": "no yields"}, _provider())
        assert out["available"] is False
        assert "estimated_value" not in out

    def test_a_unit_mismatch_refuses_rather_than_converting(self, priced_recovery):
        """Grams priced per ounce would be wrong by 31x and look plausible."""
        out = value_recovery(priced_recovery, _provider(unit="ozt"))
        assert out["available"] is False
        assert "quoted none" in out["reason"]

    def test_mixed_currencies_are_refused(self, tmp_path, monkeypatch):
        _yield_config(
            tmp_path,
            monkeypatch,
            CPU={"material": "gold", "value": 0.25, "unit": "g", "source": "T"},
            PCB={"material": "copper", "value": 5.0, "unit": "g", "source": "T"},
        )
        rec = recovery_estimate({"CPU": 1, "PCB": 1})
        provider = StaticPriceProvider(
            {
                "gold": {"price_per_unit": 1.0, "unit": "g", "currency": "USD", "source": "T"},
                "copper": {"price_per_unit": 1.0, "unit": "g", "currency": "EUR", "source": "T"},
            },
            source="mixed",
        )
        out = value_recovery(rec, provider)
        assert out["available"] is False
        assert "currencies" in out["reason"]

    def test_static_provider_is_none_when_config_is_disabled(self, tmp_path):
        cfg = tmp_path / "p.yaml"
        cfg.write_text(yaml.safe_dump({"enabled": False, "prices": {"gold": {}}}))
        assert StaticPriceProvider.from_config(cfg) is None

    def test_static_provider_is_none_when_config_is_absent(self, tmp_path):
        assert StaticPriceProvider.from_config(tmp_path / "absent.yaml") is None

    def test_provider_returns_none_for_an_unknown_material(self):
        assert _provider().quote("platinum", "g") is None

    def test_quote_shape(self):
        q = _provider().quote("gold", "g")
        assert isinstance(q, PriceQuote)
        assert q.currency == "USD"


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------
class TestValuationEndpoint:
    @pytest.fixture
    def client(self, ledger_db, monkeypatch):
        weights = ledger_db.parent / "w.pt"
        weights.write_bytes(b"stub")
        monkeypatch.setattr(api_mod, "DEFAULT_WEIGHTS", weights)
        with TestClient(api_mod.app) as c:
            yield c

    def test_unknown_batch_is_404(self, client):
        assert client.get("/batches/AUR-NOPE/valuation").status_code == 404

    def test_valuation_is_unavailable_in_the_shipped_configuration(self, client, ledger_db):
        rec = _record()
        ledger_mod.save(rec)
        body = client.get(f"/batches/{rec['batch_id']}/valuation").json()
        assert body["valuation"]["available"] is False
        assert body["recovery_estimate"]["available"] is False
        assert "estimated_value" not in body["valuation"]

    def test_endpoint_separates_counts_from_estimates(self, client, ledger_db):
        rec = _record()
        ledger_mod.save(rec)
        body = client.get(f"/batches/{rec['batch_id']}/valuation").json()
        assert body["detected_components"] == rec["detections"]
        assert body["valuation"]["disclaimer"]

    def test_no_endpoint_reports_an_estimate_as_measured(self, client, ledger_db):
        """Nothing over the wire may describe a derived figure as measured."""
        rec = _record()
        ledger_mod.save(rec)
        for path in ("/batches", f"/batches/{rec['batch_id']}", "/stats"):
            blob = client.get(path).text.lower()
            assert '"measured_material":{"available":false' in blob.replace(" ", "") or (
                "measured_material" not in blob
            )
        val = client.get(f"/batches/{rec['batch_id']}/valuation").json()
        assert val["recovery_estimate"]["measured_material"]["available"] is False
