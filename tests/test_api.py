"""Tests for the FastAPI surface.

Focus is on the contract and the failure paths, because those are what break
silently in a demo: a corrupt upload, an unknown batch id, or a service started
before the model was trained. Endpoints that need real weights are exercised
with a stub detector so the suite runs on a clean checkout with no model
present.
"""

from __future__ import annotations

import io
import sqlite3
from contextlib import closing

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import api as api_mod
from app import batch as batch_mod
from app import ledger as ledger_mod
from app.detector import Detection, FrameResult


class StubModel:
    """The slice of the Ultralytics model surface the tracker adapter uses.

    `AurumDetector` exposes `.model`, and `app/vision/tracker.py` calls
    `.track()` on it. The stub mirrors that rather than being special-cased in
    production code.
    """

    names = {0: "PCB", 1: "RAM", 2: "CPU", 3: "Connector"}

    def __init__(self, detections):
        self._dets = detections
        self.predictor = None

    def track(self, _frame, **_kwargs):
        # `boxes = None` is the real "the tracker has not associated anything
        # yet" case, which the adapter must handle without a torch tensor in
        # sight. Identity behaviour itself is tested in tests/test_tracker.py
        # against the pure state machine.
        class _Result:
            boxes = None

        return [_Result()]


class StubDetector:
    """Deterministic stand-in so API tests do not depend on trained weights."""

    model_version = "Aurum Vision v-test"
    classes = ["PCB", "RAM", "CPU", "Connector"]
    meta: dict = {}
    conf = 0.35
    iou = 0.5
    imgsz = 512

    def __init__(self, detections=None):
        self._dets = (
            detections
            if detections is not None
            else [
                Detection(cls="PCB", conf=0.91, xyxy=(10, 10, 100, 100)),
                Detection(cls="RAM", conf=0.87, xyxy=(120, 20, 200, 60)),
            ]
        )

        self.model = StubModel(self._dets)

    def warmup(self):
        return None

    def predict(self, frame):
        return FrameResult(detections=list(self._dets), inference_ms=12.5)


def png_bytes(w=64, h=48, color=(30, 90, 40)) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(np.full((h, w, 3), color, dtype=np.uint8)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client with a stub model and an isolated on-disk database.

    DEFAULT_WEIGHTS is pointed at a real file inside tmp_path because /health
    reports readiness by stat-ing that path. Left alone, these tests would pass
    on a machine that happens to have trained weights and fail on one that does
    not — which is precisely what happened the first time CI ran them.
    """
    monkeypatch.setattr(ledger_mod, "DB", tmp_path / "batches.db")
    monkeypatch.setattr(batch_mod, "BATCH_DIR", tmp_path / "batches")

    weights = tmp_path / "aurum_vision_v0_1_best.pt"
    weights.write_bytes(b"stub weights")
    monkeypatch.setattr(api_mod, "DEFAULT_WEIGHTS", weights)

    stub = StubDetector()
    monkeypatch.setattr(api_mod, "detector", lambda: stub)
    api_mod._sessions.clear()
    # `_demo` is a process-wide global and `_sessions.clear()` does not touch
    # it, so the DemoSession — its latched hardware fault included — used to
    # survive into every later test in this file. Nothing noticed until a test
    # latched a fault: after that the machine was stopped for everything that
    # ran afterwards, and which tests failed depended on collection order.
    monkeypatch.setattr(api_mod, "_demo", None)
    with TestClient(api_mod.app) as c:
        yield c


class TestHealthAndModel:
    def test_health_reports_classes(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["classes"] == ["PCB", "RAM", "CPU", "Connector"]

    def test_model_endpoint_carries_the_disclaimer(self, client):
        """The API must never describe itself as measuring metal content."""
        body = client.get("/model").json()
        assert "does not measure precious-metal composition" in body["disclaimer"]


class TestDetect:
    def test_detect_returns_counts_and_confidences(self, client):
        r = client.post("/detect", files={"file": ("x.png", png_bytes(), "image/png")})
        assert r.status_code == 200
        body = r.json()
        assert body["counts"] == {"PCB": 1, "RAM": 1, "CPU": 0, "Connector": 0}
        assert body["total_objects"] == 2
        assert 0.0 <= body["average_confidence"] <= 1.0
        assert all(len(d["box_xyxy"]) == 4 for d in body["detections"])

    def test_counts_cover_every_class_even_when_absent(self, client):
        body = client.post("/detect", files={"file": ("x.png", png_bytes(), "image/png")}).json()
        assert set(body["counts"]) == set(StubDetector.classes)

    def test_undecodable_upload_is_rejected_with_400(self, client):
        r = client.post("/detect", files={"file": ("x.png", b"not an image", "image/png")})
        assert r.status_code == 400
        assert "decode" in r.json()["detail"].lower()

    def test_empty_upload_is_rejected_with_400(self, client):
        r = client.post("/detect", files={"file": ("x.png", b"", "image/png")})
        assert r.status_code == 400

    def test_missing_file_field_is_a_422(self, client):
        assert client.post("/detect").status_code == 422

    def test_annotated_endpoint_returns_an_image(self, client):
        r = client.post("/detect/annotated", files={"file": ("x.png", png_bytes(), "image/png")})
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert len(r.content) > 100


class TestBatchLifecycle:
    def test_full_batch_flow_persists_to_sqlite(self, client):
        bid = client.post("/batch/start").json()["batch_id"]
        assert bid.startswith("AUR-")

        for _ in range(12):
            r = client.post(
                f"/batch/{bid}/frame",
                files={"file": ("x.png", png_bytes(), "image/png")},
            )
            assert r.status_code == 200

        rec = client.post(f"/batch/{bid}/close").json()
        assert rec["detections"]["PCB"] == 1
        assert rec["total_objects"] == 2
        assert rec["frames_observed"] == 12

        listed = client.get("/batches").json()
        assert listed["count"] == 1
        assert client.get(f"/batches/{bid}").json()["batch_id"] == bid

        with closing(sqlite3.connect(ledger_mod.DB)) as con:
            rows = con.execute("SELECT batch_id FROM batches").fetchall()
        assert [r[0] for r in rows] == [bid]

    def test_frame_against_unknown_batch_is_404(self, client):
        r = client.post(
            "/batch/AUR-NOPE/frame", files={"file": ("x.png", png_bytes(), "image/png")}
        )
        assert r.status_code == 404

    def test_closing_unknown_batch_is_404(self, client):
        assert client.post("/batch/AUR-NOPE/close").status_code == 404

    def test_closing_twice_is_404_the_second_time(self, client):
        bid = client.post("/batch/start").json()["batch_id"]
        client.post(f"/batch/{bid}/frame", files={"file": ("x.png", png_bytes(), "image/png")})
        assert client.post(f"/batch/{bid}/close").status_code == 200
        assert client.post(f"/batch/{bid}/close").status_code == 404

    def test_unknown_batch_id_lookup_is_404(self, client):
        assert client.get("/batches/AUR-NOPE").status_code == 404

    def test_a_record_never_reports_more_than_the_evidence_covers(self, client):
        """Guard against an unbacked metal-content number appearing over the wire.

        The stub detects one PCB and one RAM module. RAM is cited per module,
        so it is valued; PCB is cited per kilogram against a mass covering both
        classes, so it is refused. The wire format must show that split rather
        than a single number that looks like it covers the batch.
        """
        bid = client.post("/batch/start").json()["batch_id"]
        client.post(f"/batch/{bid}/frame", files={"file": ("x.png", png_bytes(), "image/png")})
        est = client.post(f"/batch/{bid}/close").json()["recovery_estimate"]

        assert est["completeness"] == "PARTIAL_ESTIMATE"
        assert [v["component"] for v in est["valued"]] == ["RAM"]
        assert [n["component"] for n in est["not_valued"]] == ["PCB"]
        assert "ESTIMATE ONLY" in est["disclaimer"]

    def test_simulated_weight_is_flagged_over_the_wire(self, client):
        bid = client.post("/batch/start").json()["batch_id"]
        client.post(f"/batch/{bid}/frame", files={"file": ("x.png", png_bytes(), "image/png")})
        rec = client.post(f"/batch/{bid}/close?weight_mode=simulated").json()
        assert rec["weight"]["simulated"] is True
        assert "SIMULATED" in rec["weight"]["warning"]


def _closed_batch(client, weight_mode="off"):
    bid = client.post("/batch/start").json()["batch_id"]
    client.post(f"/batch/{bid}/frame", files={"file": ("x.png", png_bytes(), "image/png")})
    client.post(f"/batch/{bid}/close?weight_mode={weight_mode}")
    return bid


class TestStats:
    """The aggregate the browser dashboard reads.

    Its job is to stay inside what the ledger actually recorded: counts and
    mass that exist, an empty bin breakdown because routing is not implemented,
    and simulated mass kept apart from measured mass so a UI cannot add them
    into one number that reads as a measurement.
    """

    def test_empty_ledger_reports_zeros_not_nulls(self, client):
        body = client.get("/stats").json()
        assert body["batch_count"] == 0
        assert body["total_count"] == 0
        assert body["total_weight"]["measured_grams"] == 0.0
        assert body["total_weight"]["simulated_grams"] == 0.0
        assert body["component_breakdown"] == {}

    def test_counts_aggregate_across_batches(self, client):
        _closed_batch(client)
        _closed_batch(client)
        body = client.get("/stats").json()
        assert body["batch_count"] == 2
        assert body["total_count"] == 4  # the stub detects PCB + RAM per frame

    def test_component_breakdown_comes_from_the_stored_records(self, client):
        """Every class the record names, including the ones counted zero.

        A class that was looked for and not found is evidence; dropping it would
        make an absent component indistinguishable from an unsupported one.
        """
        _closed_batch(client)
        body = client.get("/stats").json()
        assert body["component_breakdown"] == {"PCB": 1, "RAM": 1, "CPU": 0, "Connector": 0}

    def test_simulated_mass_is_never_added_to_measured_mass(self, client):
        _closed_batch(client, weight_mode="simulated")
        w = client.get("/stats").json()["total_weight"]
        assert w["simulated_grams"] > 0
        assert w["measured_grams"] == 0.0
        assert w["batches_with_weight"] == 1
        assert "not physical measurements" in w["note"]

    def test_batches_without_a_reading_are_not_counted_as_weighed(self, client):
        _closed_batch(client, weight_mode="off")
        w = client.get("/stats").json()["total_weight"]
        assert w["batches_with_weight"] == 0
        assert w["simulated_grams"] == 0.0

    def test_bin_breakdown_is_empty_because_a_batch_carries_no_bin(self, client):
        """Bins are per item, and these aggregates are per batch.

        The demonstration session assigns a bin to an `AUR-ITEM-`, never to a
        stored batch record, so a bin figure appearing here would be an
        invention rather than an aggregate.
        """
        _closed_batch(client, weight_mode="simulated")
        body = client.get("/stats").json()
        assert body["bin_breakdown"] == {}
        assert "a batch carries no bin assignment" in body["bin_breakdown_note"]

    def test_stats_exposes_no_valuation_or_recovery_figure(self, client):
        """A deliberate contract on /stats, reviewed and kept in Phase 2.

        /stats is the aggregate endpoint a dashboard renders without reading
        any provenance. A money figure or a metal yield here would be quoted
        with none of the evidence, staleness or simulation flags that qualify
        it, which is exactly how an invented PMDI constant got screenshotted
        elsewhere in this project's history.

        The ban is scoped to /stats, not to the API. PMDI is a first-class
        concept and IS exposed, on /batches/{id}/valuation and /prices, where
        every figure travels with its evidence ids and status. See
        TestPmdiIsExposedWithItsProvenance below.
        """
        _closed_batch(client, weight_mode="simulated")
        blob = client.get("/stats").text.lower()
        for forbidden in ("value", "price", "gold_g", "recovery_g", "carbon", "pmdi"):
            assert forbidden not in blob


class TestCORS:
    """Only the Vite dev origin may reach this service, and only by GET or POST.

    POST was added when the dashboard began driving the demonstration session
    (start the camera, connect the board, weigh the item). The origin list did
    not widen, and no method beyond those two is allowed.
    """

    def test_dev_origin_is_allowed(self, client):
        r = client.get("/stats", headers={"Origin": "http://localhost:5173"})
        assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"

    def test_unknown_origin_gets_no_allow_header(self, client):
        r = client.get("/stats", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in r.headers

    def test_credentials_are_not_allowed(self, client):
        r = client.get("/stats", headers={"Origin": "http://localhost:5173"})
        assert "access-control-allow-credentials" not in r.headers

    def test_preflight_refuses_a_destructive_method(self, client):
        r = client.options(
            "/batches",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "DELETE",
            },
        )
        assert r.status_code == 400
        assert r.headers.get("access-control-allow-methods") == "GET, POST"

    def test_preflight_allows_the_session_post(self, client):
        r = client.options(
            "/session/measure",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert r.status_code == 200
        assert "POST" in r.headers.get("access-control-allow-methods", "")


class TestMissingModel:
    def test_health_reports_model_missing_when_weights_absent(self, tmp_path, monkeypatch):
        """A service started before training must say so, not crash obscurely."""
        monkeypatch.setattr(ledger_mod, "DB", tmp_path / "b.db")
        monkeypatch.setattr(api_mod, "DEFAULT_WEIGHTS", tmp_path / "absent.pt")
        monkeypatch.setattr(api_mod, "_detector", None)
        with TestClient(api_mod.app) as c:
            r = c.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "model_missing"

    def test_detect_returns_503_when_weights_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ledger_mod, "DB", tmp_path / "b.db")
        monkeypatch.setattr(api_mod, "DEFAULT_WEIGHTS", tmp_path / "absent.pt")
        monkeypatch.setattr(api_mod, "_detector", None)
        with TestClient(api_mod.app) as c:
            r = c.post("/detect", files={"file": ("x.png", png_bytes(), "image/png")})
        assert r.status_code == 503
        assert "not trained" in r.json()["detail"].lower()


class TestPmdiIsExposedWithItsProvenance:
    """PMDI is a first-class project concept and the API exposes it.

    The counterpart to TestStats.test_stats_exposes_no_valuation_or_recovery_figure:
    that guard keeps money and yields off the un-annotated aggregate endpoint;
    these confirm the figures are available where their evidence travels with
    them.
    """

    def test_the_valuation_endpoint_carries_a_pmdi_block(self, client):
        batch_id = _closed_batch(client, weight_mode="simulated")
        body = client.get(f"/batches/{batch_id}/valuation").json()
        assert "pmdi" in body
        assert body["pmdi"]["formula"] == "PMDI = (sum(C_type x Y_estimated)) x P_spot"

    def test_every_pmdi_figure_travels_with_its_status(self, client):
        batch_id = _closed_batch(client, weight_mode="simulated")
        pmdi = client.get(f"/batches/{batch_id}/valuation").json()["pmdi"]
        for field in ("evidence_status", "mass_status", "price_status", "overall_status"):
            assert pmdi[field]

    def test_a_simulated_batch_stays_labelled_simulated(self, client):
        batch_id = _closed_batch(client, weight_mode="simulated")
        body = client.get(f"/batches/{batch_id}/valuation").json()
        assert body["item_valuation"]["weight_status"] in ("SIMULATED", "UNMEASURED")

    def test_a_simulated_mass_never_contributes_to_a_figure(self, client):
        """A fabricated mass must not become money — and here it does not.

        The value that appears comes from RAM's PER-MODULE evidence, which
        needs no scale at all, so the simulated mass never enters the
        arithmetic. The PCB line, which WOULD have consumed that mass, is
        refused. What the invented mass does reach is the status: the whole
        record is stamped SIMULATED so nothing can be quoted as measured.
        """
        batch_id = _closed_batch(client, weight_mode="simulated")
        body = client.get(f"/batches/{batch_id}/valuation").json()

        assert body["item_valuation"]["weight_status"] == "SIMULATED"
        assert body["item_valuation"]["overall_status"] == "SIMULATED"
        # Nothing whose arithmetic used the mass was valued.
        assert [n["component"] for n in body["pmdi"]["not_valued"]] == ["PCB"]
        for amounts in body["pmdi"]["precious_metals"].values():
            assert "per piece" in amounts["calculation"]

    def test_the_legacy_valuation_key_still_works(self, client):
        """Existing consumers of this endpoint must not break."""
        batch_id = _closed_batch(client, weight_mode="simulated")
        body = client.get(f"/batches/{batch_id}/valuation").json()
        assert "recovery_estimate" in body
        assert body["valuation"]["available"] is False


class TestPricesEndpoint:
    def test_it_reports_the_configured_provider(self, client):
        body = client.get("/prices").json()
        assert body["provider"] == "reference"

    def test_every_price_is_labelled_reference_and_never_live(self, client):
        """A dated snapshot may be shown. It may never be called current."""
        body = client.get("/prices").json()
        for metal, quote in body["prices"].items():
            assert quote["status"] != "LIVE", metal
            if quote["price_per_gram"] is not None:
                assert quote["status"] == "REFERENCE", metal
                assert quote["currency"] == "INR", metal
                assert quote["timestamp"], f"{metal} has a price with no date"
                assert "not a live market quote" in quote["reason"], metal

    def test_every_quoted_price_names_its_source(self, client):
        body = client.get("/prices").json()
        for metal, quote in body["prices"].items():
            if quote["price_per_gram"] is not None:
                assert "Source:" in quote["reason"], f"{metal} has a price with no source"

    def test_turning_the_provider_off_names_the_setting_that_would_change_it(
        self, client, monkeypatch
    ):
        """The refusal path still exists and still explains itself."""
        from app.valuation.prices import PriceService, UnavailableProvider

        quote = PriceService(provider=UnavailableProvider()).price("Au")
        assert quote.status.value == "UNAVAILABLE"
        assert "AURUM_PRICE_PROVIDER" in quote.reason

    def test_it_distinguishes_a_reference_price_from_a_live_one(self, client):
        """Re-pointed 2026-08-26: a live provider now exists.

        The claim this guarded - "no live market data source is approved" -
        became false when app/valuation/metalprice.py shipped. What it was
        really protecting is that a figure which is not a current market quote
        can never be read as one, and that survives the change.
        """
        body = client.get("/prices").json()
        assert "Only LIVE is a current market price" in body["note"]
        assert body["configured_provider"] == "reference"
        for metal, quote in body["prices"].items():
            if quote["price_per_gram"] is not None:
                assert quote["status"] == "REFERENCE", metal
                assert "not a live market quote" in quote["reason"], metal


class TestDecisionIsExposed:
    """The routing verdict travels with the evidence that produced it."""

    def test_the_valuation_endpoint_carries_a_decision(self, client):
        batch_id = _closed_batch(client, weight_mode="simulated")
        decision = client.get(f"/batches/{batch_id}/valuation").json()["decision"]
        assert decision["decision"] in ("A", "B", "C", "UNKNOWN")
        assert decision["physical_bin"] in ("A", "B", "C")
        assert decision["reason_code"]
        assert decision["reason"]

    def test_the_decision_carries_its_signals_and_policy(self, client):
        batch_id = _closed_batch(client, weight_mode="simulated")
        decision = client.get(f"/batches/{batch_id}/valuation").json()["decision"]
        assert "component_class" in decision["signals"]
        assert "class_aware" in decision["policy"]
        assert "preferred_classes" in decision["policy"]

    def test_thresholds_are_labelled_as_engineering_approximations(self, client):
        batch_id = _closed_batch(client, weight_mode="simulated")
        note = client.get(f"/batches/{batch_id}/valuation").json()["decision"]["threshold_note"]
        assert "not presented as universally validated" in note

    def test_a_mixed_batch_cannot_be_routed(self, client):
        """Two classes in one record have no single component class to route."""
        batch_id = _closed_batch(client, weight_mode="simulated")
        decision = client.get(f"/batches/{batch_id}/valuation").json()["decision"]
        if decision["signals"]["component_class"] is None:
            assert decision["decision"] == "UNKNOWN"
            assert decision["physical_bin"] == "C"
            assert decision["reason_code"] == "UNKNOWN_CLASS"

    def test_bin_c_names_no_servo(self, client):
        batch_id = _closed_batch(client, weight_mode="simulated")
        decision = client.get(f"/batches/{batch_id}/valuation").json()["decision"]
        if decision["physical_bin"] == "C":
            assert decision["servo"] is None


class TestTrackingEndpoints:
    """Tracking is stateful; these confirm identity survives across requests."""

    @pytest.fixture(autouse=True)
    def _fresh_pipeline(self, client):
        client.post("/track/reset")
        yield
        client.post("/track/reset")

    def test_a_frame_returns_active_items(self, client):
        body = client.post("/track", files={"file": ("f.png", png_bytes(), "image/png")}).json()
        assert "active_items" in body
        assert body["frames_processed"] == 1

    def test_items_reports_the_tracking_policy_as_approximate(self, client):
        body = client.get("/items").json()
        assert "engineering approximations" in body["tracking_policy"]["note"]

    def test_the_current_item_endpoint_explains_an_absence(self, client):
        body = client.get("/items/current").json()
        if body["current_item"] is None:
            assert "not yet something to weigh or route" in body["reason"]

    def test_a_reset_clears_the_run(self, client):
        client.post("/track", files={"file": ("f.png", png_bytes(), "image/png")})
        assert client.post("/track/reset").json()["frames_processed"] == 0
        assert client.get("/items").json()["frames_processed"] == 0

    def test_item_ids_are_aurum_identities_not_tracker_numbers(self, client):
        client.post("/track", files={"file": ("f.png", png_bytes(), "image/png")})
        for item in client.get("/items").json()["items"]:
            assert item["item_id"].startswith("AUR-ITEM-")
            assert item["item_id"] != str(item["track_id"])

    def test_existing_detect_endpoint_is_unaffected(self, client):
        """The stateless path must keep working alongside the stateful one."""
        body = client.post("/detect", files={"file": ("f.png", png_bytes(), "image/png")}).json()
        assert "detections" in body
        assert "item_id" not in body


class TestRoutingEndpoint:
    """Routing state is visible over HTTP. Nothing here actuates anything."""

    def test_it_reports_the_geometry_mode(self, client):
        body = client.get("/routing").json()
        assert body["mode"] in ("REAL", "SIMULATED")

    def test_an_unmeasured_machine_reports_itself_unroutable(self, client):
        """The shipped configuration has no measured geometry."""
        body = client.get("/routing").json()
        assert body["routable"] is False
        assert body["geometry"]["belt_speed_cm_s"] is None

    def test_the_belt_speed_is_not_described_as_measured(self, client):
        """The basis string names where the speed came from, always.

        Re-pointed 2026-08-26. The claim it used to assert - "this machine has
        no encoder" - is now something app/routing/conveyor.py decides rather
        than something this string can state. On the shipped configuration
        there is no belt at all, and that is what it must say.
        """
        basis = client.get("/routing").json()["geometry"]["belt_speed_basis"]
        assert "UNAVAILABLE" in basis
        assert "no belt" in basis.lower()

    def test_the_endpoint_carries_the_belt(self, client):
        conveyor = client.get("/routing").json()["conveyor"]
        assert conveyor["mode"] == "NONE"
        assert conveyor["present"] is False

    def test_the_queue_starts_empty(self, client):
        body = client.get("/routing").json()
        assert body["pending"] == []
        assert body["due"] == []


class TestEmergencyStop:
    """The operator's halt. It latches the same fault a lost ACK does, because
    after either one nobody can be sure where a paddle is."""

    def test_it_latches_the_machine(self, client):
        assert client.get("/hardware").json()["fault"]["active"] is False
        body = client.post("/hardware/estop").json()
        assert body["stopped"] is True
        assert body["fault"]["code"] == "EMERGENCY_STOP"
        assert client.get("/hardware").json()["fault"]["active"] is True

    def test_it_stops_the_belt_rather_than_waiting_for_the_pan_loop(self, client):
        """Latching stops SERVO commands. A turning motor is not a servo.

        Before this, the belt ran until the pan loop next noticed the fault -
        and not at all if that thread was dead or pan.auto was off.
        """
        session = api_mod.demo_session()
        calls = []
        session.link = type(
            "L",
            (),
            {
                "connected": True,
                "belt": lambda _self, run, pwm=0, budget_s=2.0: calls.append(run) or True,
            },
        )()
        body = client.post("/hardware/estop").json()
        assert body["belt_stopped"] is True
        assert calls == [False]

    def test_a_latched_machine_refuses_to_actuate(self, client):
        """The point of the button, and the only test that proves it works."""
        client.post("/hardware/estop")
        session = api_mod.demo_session()
        assert session.fault.refusal() is not None

    def test_it_does_not_release_the_camera_or_the_port(self, client):
        """`/session/stop` does that. An e-stop that closes the link takes away
        the ability to command a paddle back to rest."""
        client.post("/hardware/estop")
        assert client.get("/session").json()["running"] is False
        # Distinct endpoints, distinct effects: the fault is what stopped it.
        assert client.get("/hardware").json()["fault"]["code"] == "EMERGENCY_STOP"

    def test_pressing_it_twice_stays_latched_on_the_first_press(self, client):
        first = client.post("/hardware/estop").json()["fault"]["at"]
        client.post("/hardware/estop")
        fault = client.get("/hardware").json()["fault"]
        assert fault["since"] == first
        assert fault["faults"] == 2

    def test_it_records_who_stopped_the_machine(self, client):
        body = client.post("/hardware/estop", params={"by": "bench operator"}).json()
        assert "bench operator" in body["fault"]["reason"]

    def test_resetting_clears_it_and_the_reset_is_recorded(self, client):
        client.post("/hardware/estop")
        cleared = client.post("/hardware/fault/reset").json()
        assert cleared["cleared"]["code"] == "EMERGENCY_STOP"
        assert cleared["fault"]["active"] is False
        assert cleared["fault"]["resets"][0]["by"] == "dashboard"


class TestReadiness:
    """`/health` says the service started. `/ready` says the machine can be
    demonstrated, which is a different and larger question."""

    def test_a_missing_camera_blocks_readiness(self, client):
        body = client.get("/ready").json()
        assert body["ready"] is False
        assert "camera" in body["blocked_by"]

    def test_no_board_is_advisory_not_blocking(self, client):
        """The shipped configuration. Calling it 'not ready' would make the
        honest state look like a broken one."""
        body = client.get("/ready").json()
        assert "board link" in body["advisory"]
        assert "board link" not in body["blocked_by"]

    def test_a_latched_fault_blocks_readiness(self, client):
        assert "no latched fault" not in client.get("/ready").json()["blocked_by"]
        client.post("/hardware/estop")
        body = client.get("/ready").json()
        assert body["ready"] is False
        assert "no latched fault" in body["blocked_by"]

    def test_clearing_the_fault_unblocks_that_check(self, client):
        client.post("/hardware/estop")
        client.post("/hardware/fault/reset")
        assert "no latched fault" not in client.get("/ready").json()["blocked_by"]

    def test_every_check_explains_itself(self, client):
        """A check that fails without saying why is a check nobody can act on."""
        for check in client.get("/ready").json()["checks"]:
            assert check["detail"], check["name"]
            assert isinstance(check["blocking"], bool)

    def test_the_detail_agrees_with_the_verdict(self, client):
        """The board detail used to read 'linked' while reporting not ready."""
        by_name = {c["name"]: c for c in client.get("/ready").json()["checks"]}
        board = by_name["board link"]
        assert board["ready"] is False
        assert "not connected" in board["detail"]

    def test_it_does_not_claim_anything_about_physical_movement(self, client):
        names = [c["name"] for c in client.get("/ready").json()["checks"]]
        assert not any("moved" in n or "physical" in n for n in names)
        assert "bench_check" in client.get("/ready").json()["note"]
