"""End-to-end tests for the demonstration session.

This is the file the audit said did not exist: the one that crosses every
module rather than testing each in isolation. It walks the chain a judge will
watch, with the camera and the board replaced by fakes and nothing else
replaced at all — the tracker, the material database, PMDI, the valuation and
the decision engine are the real ones.

The properties under test are the demonstration's honesty claims:

**Nothing is entered by hand.** No test supplies a class, a mass or a bin. The
class comes from tracked detections, the mass from counts through a real
calibration, and the bin from the decision engine.

**An unverified calibration cannot produce a metal figure.** A settled reading
on a factor nobody checked against a second known mass is STABLE, not
MEASURED, and a concentration estimate refuses it.

**Bin C moves nothing.** Not "commands a servo that declines" — no frame is
written to the board at all.

**Failures are visible, not papered over.** No cell, no board and no evidence
each leave a reason on the item.
"""

from __future__ import annotations

import dataclasses

import pytest

from app import config
from app.errors import ErrorCode
from app.hardware import ArduinoController, FakeTransport
from app.pipeline.session import DemoSession
from app.vision.tracker import TrackedDetection
from app.weight import Calibration, RawSample

#: The bench experiment's factor, and a tare from the real empty-pan readings.
#: Fixture values standing in for a calibrated rig, not a claim about one.
COUNTS_PER_GRAM = 361.9
TARE_COUNTS = -261600.0

VERIFIED = Calibration(
    counts_per_gram=COUNTS_PER_GRAM,
    tare_counts=TARE_COUNTS,
    reference_mass_g=180.0,
    verified=True,
    verification_mass_g=100.0,
    verification_error_g=0.01,
)
UNVERIFIED = Calibration(
    counts_per_gram=COUNTS_PER_GRAM,
    tare_counts=TARE_COUNTS,
    reference_mass_g=180.0,
    verified=False,
)


class FakeCell:
    """A load cell that reports the counts a given mass would produce."""

    name = "hx711-serial"

    def __init__(self, grams: float | None):
        self.connected = True
        self.grams = grams
        self.last_error = None
        self.reads = 0

    def read(self):
        if self.grams is None:
            return None
        self.reads += 1
        # One count of wander, centred, because a converting cell never repeats
        # a value bit-for-bit - `app.weight.repeats_exactly` refuses a frozen
        # series as the hardware fault it is. Far below any tolerance here.
        wander = (self.reads % 3) - 1
        return RawSample(raw_counts=TARE_COUNTS + self.grams * COUNTS_PER_GRAM + wander)

    def close(self):
        self.connected = False


class FakeLink:
    """Stands in for `BoardLink`: a connected board with a cell on it."""

    def __init__(self, grams: float | None = 42.7, connected: bool = True):
        self.connected = connected
        self.weight_reader = FakeCell(grams)
        #: The conveyor motor, which this rig's fake never runs.
        self.belt_running = False
        self.belt_pwm = 0

    def snapshot(self):
        return {"connected": self.connected}

    def disconnect(self):
        self.connected = False


def session(
    grams: float | None = 42.7,
    calibration: Calibration = VERIFIED,
    board: bool = True,
    actuation: bool = True,
    transport=None,
) -> DemoSession:
    cfg = config.load(
        environ={
            "AURUM_ARDUINO_ENABLED": "true" if actuation else "false",
            # Real values, shortened: these are what a settling cell costs in
            # wall-clock, and a test suite should not sit through them.
            "AURUM_WEIGHT_TIMEOUT_S": "0.2",
            "AURUM_WEIGHT_STABILITY_WINDOW_MS": "10",
        }
    )
    transport = FakeTransport(connected=True) if transport is None else transport
    run = DemoSession(cfg=cfg, controller=ArduinoController(transport=transport, cfg=cfg))
    run.calibration = calibration
    if board:
        run.link = FakeLink(grams)
    return run


def present(run: DemoSession, component_class: str, confidence: float = 0.94, frames: int = 4):
    """Hold a component in front of the camera until the tracker confirms it."""
    for frame in range(frames):
        run.pipeline.process_detections(
            [TrackedDetection(1, component_class, confidence, (10, 10, 90, 90))], frame_id=frame
        )
    return run.pipeline.current_item


class TestTheChain:
    def test_a_cpu_is_identified_weighed_and_routed_to_servo_a(self):
        transport = FakeTransport(connected=True)
        run = session(grams=42.7, transport=transport)
        item = present(run, "CPU")

        result = run.measure_and_route()

        assert result["item_id"] == item.item_id
        assert result["class_name"] == "CPU"
        assert result["weight_g"] == pytest.approx(42.7)
        assert result["weight_status"] == "MEASURED"
        assert result["decision"]["decision"] == "A"
        assert result["actuation"]["servo"] == "SERVO_A"
        assert result["actuation"]["state"] == "ACKED"
        assert transport.movements == [("A", result["actuation"]["command_id"])]

    def test_a_pcb_reaches_servo_b_on_its_cited_precious_fraction(self):
        transport = FakeTransport(connected=True)
        run = session(grams=180.0, transport=transport)
        present(run, "PCB")

        result = run.measure_and_route()

        assert result["decision"]["decision"] == "B"
        assert result["decision"]["reason_code"] == "B_PRECIOUS_FRACTION"
        assert result["valuation"]["pmdi"]["precious_mass_fraction_ppm"] == 2200.0
        assert transport.movements[0][0] == "B"

    def test_an_unknown_class_reaches_bin_c_and_no_frame_is_written(self):
        """The claim that matters most: C is the board doing nothing.

        RAM used to be this test's example. It carries cited composition now,
        so the case is made with a class the database has never heard of.
        """
        transport = FakeTransport(connected=True)
        run = session(grams=30.0, transport=transport)
        present(run, "GPU")

        result = run.measure_and_route()

        assert result["decision"]["decision"] == "UNKNOWN"
        assert result["decision"]["physical_bin"] == "C"
        assert result["decision"]["reason_code"] == "UNKNOWN_CLASS"
        assert result["actuation"]["commanded"] is False
        assert result["actuation"]["servo"] is None
        assert transport.sent == []
        assert transport.movements == []

    def test_the_mass_lands_on_the_identity_the_camera_minted(self):
        """One object, one id, from the first frame to the servo command."""
        run = session()
        item = present(run, "CPU")
        result = run.measure_and_route()
        assert result["item_id"] == item.item_id
        assert run.pipeline.tracker.get(item.item_id).weight_g == pytest.approx(42.7)
        assert result["actuation"]["item_id"] == item.item_id

    def test_the_pmdi_is_computed_from_the_measured_mass(self):
        """Change the mass on the pan, and the estimate changes with it."""
        light = session(grams=90.0)
        present(light, "PCB")
        heavy = session(grams=180.0)
        present(heavy, "PCB")

        light_g = light.measure_and_route()["valuation"]["pmdi"]["precious_mass_g"]
        heavy_g = heavy.measure_and_route()["valuation"]["pmdi"]["precious_mass_g"]

        assert heavy_g == 2 * light_g

    def test_the_decision_carries_its_evidence(self):
        run = session(grams=180.0)
        present(run, "PCB")
        result = run.measure_and_route()
        assert result["valuation"]["pmdi"]["evidence_sources"]
        assert result["decision"]["signals"]["mass_status"] == "MEASURED"
        assert result["decision"]["threshold_note"]

    def test_a_weak_detection_is_refused_the_premium_bin(self):
        run = session()
        present(run, "CPU", confidence=0.4)
        result = run.measure_and_route()
        assert result["decision"]["decision"] != "A"


class TestOperatorRules:
    def test_no_confirmed_item_is_refused_with_an_explanation(self):
        run = session()
        present(run, "CPU", frames=1)  # NEW, not yet CONFIRMED
        result = run.measure_and_route()
        assert result["error"] == "NO_ITEM"
        assert "CONFIRMED" in result["reason"]

    def test_an_unknown_item_id_is_refused(self):
        run = session()
        present(run, "CPU")
        assert run.measure_and_route("AUR-ITEM-NOPE")["error"] == "UNKNOWN_ITEM"

    def test_one_item_is_weighed_and_routed_once(self):
        transport = FakeTransport(connected=True)
        run = session(transport=transport)
        present(run, "CPU")
        run.measure_and_route()
        second = run.measure_and_route()
        assert second["error"] == "ALREADY_PROCESSED"
        assert len(transport.movements) == 1


class TestStartingAFreshRun:
    """`reset()` is the operator saying they swapped the object on the bench.

    Without it, a rig whose object never leaves the camera's view can route
    exactly once and then refuse forever - which reads as a broken button
    rather than as the safety rule it actually is.
    """

    def test_a_reset_run_can_route_the_next_object(self):
        transport = FakeTransport(connected=True)
        run = session(transport=transport)
        present(run, "CPU")
        run.measure_and_route()
        assert run.measure_and_route()["error"] == "ALREADY_PROCESSED"

        run.reset()
        present(run, "CPU")
        assert "error" not in run.measure_and_route()
        assert len(transport.movements) == 2

    def test_the_new_object_gets_its_own_identity(self):
        """One physical object cannot inherit the previous one's ledger row."""
        run = session()
        present(run, "CPU")
        first = run.measure_and_route()["item_id"]
        run.reset()
        present(run, "CPU")
        assert run.measure_and_route()["item_id"] != first

    def test_a_reset_forgets_what_was_on_the_pan(self):
        """A stale latch would short-circuit refresh() and block every item."""
        run = session()
        present(run, "CPU")
        run.zone.latch()
        run.reset()
        assert run.zone.held is None

    def test_a_reset_keeps_the_audit_identity_and_the_error_log(self):
        """The run's bookkeeping restarts; the record of what happened does not.

        The EPR ledger is written through `epr.record` and is not the session's
        to clear. What the session does own is the error log and the id every
        row was filed under, and a reset must leave both standing.
        """
        run = session()
        run.errors.record(ErrorCode.ARDUINO_ERROR, "board", "a failure worth keeping")
        session_id, before = run.session_id, run.errors.snapshot()["count"]

        run.reset()

        assert run.session_id == session_id
        assert run.errors.snapshot()["count"] == before

    def test_two_components_each_get_their_own_identity_and_movement(self):
        transport = FakeTransport(connected=True)
        run = session(transport=transport)
        first = present(run, "CPU")
        run.measure_and_route()
        # The first leaves the frame; a second component arrives on a new track.
        for frame in range(20):
            run.pipeline.process_detections([], frame_id=100 + frame)
        for frame in range(4):
            run.pipeline.process_detections(
                [TrackedDetection(2, "CPU", 0.9, (20, 20, 99, 99))], frame_id=200 + frame
            )
        second = run.pipeline.current_item
        run.measure_and_route()

        assert first.item_id != second.item_id
        assert len(transport.movements) == 2


class TestFailsClosed:
    def test_an_unverified_calibration_yields_no_metal_figure(self):
        """A factor nobody checked against a second mass is not a measurement."""
        run = session(grams=180.0, calibration=UNVERIFIED)
        present(run, "PCB")
        result = run.measure_and_route()
        assert result["weight_status"] == "STABLE"
        assert result["decision"]["physical_bin"] == "C"
        assert result["valuation"]["pmdi"]["available"] is False

    def test_an_uncalibrated_cell_reports_unavailable_not_zero(self):
        run = session(grams=180.0, calibration=Calibration())
        present(run, "PCB")
        result = run.measure_and_route()
        assert result["weight_status"] == "UNAVAILABLE"
        assert "not calibrated" in result["weight_reading"]["reason"]

    def test_no_board_means_no_mass_and_no_invented_one(self):
        run = session(board=False)
        present(run, "PCB")
        result = run.measure_and_route()
        assert result["weight_status"] == "UNAVAILABLE"
        assert result["weight_reading"]["usable"] is False
        assert "No load cell is connected" in result["weight_reading"]["reason"]

    def test_a_silent_cell_times_out_rather_than_reporting_a_mass(self):
        run = session(grams=None)
        present(run, "PCB")
        result = run.measure_and_route()
        assert result["weight_status"] == "UNAVAILABLE"
        assert result["decision"]["decision"] == "UNKNOWN"
        assert result["decision"]["physical_bin"] == "C"

    def test_a_disconnected_board_leaves_the_decision_and_drops_the_movement(self):
        """The decision is not rewritten to express "I could not move it"."""
        run = session(transport=FakeTransport(connected=False))
        present(run, "CPU")
        result = run.measure_and_route()
        assert result["decision"]["decision"] == "A"
        assert result["actuation"]["state"] == "FAILED"
        assert result["actuation"]["error_code"] == "NOT_CONNECTED"

    def test_actuation_disabled_refuses_without_pretending_to_move(self):
        transport = FakeTransport(connected=True)
        run = session(actuation=False, transport=transport)
        present(run, "CPU")
        result = run.measure_and_route()
        assert result["actuation"]["error_code"] == "ACTUATION_DISABLED"
        assert transport.movements == []

    def test_a_board_error_is_never_reported_as_a_movement(self):
        run = session(transport=FakeTransport(connected=True, fail_with="SERVO_STALL"))
        present(run, "CPU")
        result = run.measure_and_route()
        assert result["actuation"]["state"] == "FAILED"
        assert result["actuation"]["error_code"] == "SERVO_STALL"


class TestSnapshot:
    def test_the_snapshot_carries_the_whole_chain_for_one_item(self):
        run = session(grams=180.0)
        present(run, "PCB")
        run.measure_and_route()

        item = run.snapshot()["items"][0]

        assert item["item_id"].startswith("AUR-ITEM-")
        assert item["class_name"] == "PCB"
        assert item["confidence"] is not None
        assert item["weight_g"] == pytest.approx(180.0)
        assert item["valuation"]["pmdi"]["precious_metals"]
        assert item["decision"]["decision"] == "B"
        assert item["actuation"]["servo"] == "SERVO_B"

    def test_the_snapshot_states_that_no_conveyor_exists(self):
        """The demonstration must not imply a belt it does not have."""
        conveyor = session().snapshot()["conveyor"]
        assert conveyor["present"] is False
        assert conveyor["mode"] == "NONE"
        assert "No belt exists" in conveyor["note"]
        assert "not scheduled" in conveyor["note"]
        # And no speed is offered in place of one.
        assert conveyor["speed"]["cm_s"] is None
        assert conveyor["speed"]["status"] == "UNAVAILABLE"

    def test_the_snapshot_reports_calibration_verification_honestly(self):
        assert session(calibration=UNVERIFIED).snapshot()["calibration"]["verified"] is False
        assert session(calibration=VERIFIED).snapshot()["calibration"]["verified"] is True

    def test_a_session_with_no_board_says_so(self):
        assert session(board=False).snapshot()["board"] == {"connected": False}


class TestMockMassFallback:
    """The demonstration stand-in mass.

    It exists so a dead load cell does not cost the whole demonstration, and it
    is dangerous for exactly that reason: it makes a fabricated number flow
    through a pipeline whose entire value is that it refuses fabricated numbers.
    So the tests below pin both halves - that it works, and that it stays
    labelled and stays off by default.
    """

    def mock_session(self, grams=180.0, transport=None):
        cfg = config.load(
            environ={
                "AURUM_ARDUINO_ENABLED": "true",
                "AURUM_DEMO_MOCK_MASS": "true",
                "AURUM_DEMO_MOCK_MASS_G": str(grams),
                "AURUM_WEIGHT_TIMEOUT_S": "0.2",
                "AURUM_WEIGHT_STABILITY_WINDOW_MS": "10",
            }
        )
        transport = FakeTransport(connected=True) if transport is None else transport
        run = DemoSession(cfg=cfg, controller=ArduinoController(transport=transport, cfg=cfg))
        run.calibration = Calibration()  # uncalibrated, as the real rig is
        return run

    def test_it_is_off_by_default(self):
        """A fabricated mass must never be something you get by accident."""
        assert config.load(environ={})["demo.mock_mass.enabled"] is False

    def test_a_pcb_reaches_bin_b_on_the_stand_in_mass(self):
        transport = FakeTransport(connected=True)
        run = self.mock_session(transport=transport)
        present(run, "PCB")
        result = run.measure_and_route()
        assert result["weight_g"] == 60.0
        assert result["decision"]["decision"] == "B"
        assert transport.movements[0][0] == "B"

    def test_the_mass_is_labelled_simulated_not_measured(self):
        run = self.mock_session()
        present(run, "PCB")
        result = run.measure_and_route()
        assert result["weight_status"] == "SIMULATED"
        assert result["weight_reading"]["mock"] is True
        assert result["weight_reading"]["usable"] is False
        assert "not measured" in result["weight_reading"]["reason"]

    def test_everything_derived_from_it_is_stamped_simulated(self):
        """The fabrication must not wash out somewhere downstream."""
        run = self.mock_session()
        present(run, "PCB")
        result = run.measure_and_route()
        assert result["valuation"]["pmdi"]["overall_status"] == "SIMULATED"
        assert result["valuation"]["pmdi"]["mass_status"] == "SIMULATED"

    def test_the_ppm_still_comes_from_cited_evidence(self):
        """Only the mass is invented. The composition behind it is not."""
        run = self.mock_session()
        present(run, "PCB")
        result = run.measure_and_route()
        pmdi = result["valuation"]["pmdi"]
        assert pmdi["precious_mass_fraction_ppm"] == 2200.0
        assert pmdi["evidence_sources"]

    def test_an_unknown_class_still_reaches_c_under_a_stand_in_mass(self):
        """A stand-in mass cannot conjure evidence that does not exist."""
        transport = FakeTransport(connected=True)
        run = self.mock_session(transport=transport)
        present(run, "GPU")
        result = run.measure_and_route()
        assert result["decision"]["decision"] == "UNKNOWN"
        assert result["decision"]["physical_bin"] == "C"
        assert result["decision"]["reason_code"] == "UNKNOWN_CLASS"
        assert transport.movements == []

    def test_with_the_flag_off_a_pcb_still_reaches_c(self):
        """The guarantee that matters when the flag is not set."""
        run = session(board=False)
        present(run, "PCB")
        result = run.measure_and_route()
        assert result["decision"]["decision"] == "UNKNOWN"
        assert result["decision"]["physical_bin"] == "C"
        assert result["decision"]["reason_code"] == "UNKNOWN_WEIGHT"

    def test_an_unmarked_simulated_mass_is_still_refused(self):
        """The permission rides on the reading, so it cannot be forged by config."""
        from app import materials

        unmarked = {"grams": 180.0, "simulated": True, "status": "SIMULATED"}
        assert materials.estimate({"PCB": 1}, mass=unmarked)["available"] is False

    def test_the_snapshot_announces_the_stand_in(self):
        snapshot = self.mock_session().snapshot()["mock_mass"]
        assert snapshot["enabled"] is True
        assert snapshot["grams"] == 180.0
        assert "none of it is a measurement" in snapshot["note"]


class TestPerClassMockMass:
    """A stand-in mass is per class, because ppm is metal over TOTAL mass.

    One flat value made a CPU read 26 ppm instead of 188 - a number a judge can
    check against the class and find wrong, which is worse than showing nothing.
    """

    def mock_cfg(self):
        return config.load(
            environ={
                "AURUM_ARDUINO_ENABLED": "true",
                "AURUM_DEMO_MOCK_MASS": "true",
                "AURUM_WEIGHT_TIMEOUT_S": "0.2",
                "AURUM_WEIGHT_STABILITY_WINDOW_MS": "10",
            }
        )

    def run_for(self, component_class, transport=None):
        cfg = self.mock_cfg()
        transport = FakeTransport(connected=True) if transport is None else transport
        run = DemoSession(cfg=cfg, controller=ArduinoController(transport=transport, cfg=cfg))
        run.calibration = Calibration()
        present(run, component_class)
        return run.measure_and_route()

    def test_each_class_gets_its_own_stand_in(self):
        masses = {c: self.run_for(c)["weight_g"] for c in ("CPU", "PCB", "RAM", "Connector")}
        assert masses["CPU"] == 22.0
        assert masses["PCB"] == 60.0
        assert masses["RAM"] == 20.0
        assert masses["Connector"] == 5.0

    def test_a_cpu_reaches_bin_a_and_fires_servo_a(self):
        transport = FakeTransport(connected=True)
        result = self.run_for("CPU", transport)
        assert result["decision"]["decision"] == "A"
        assert result["actuation"]["servo"] == "SERVO_A"
        assert transport.movements[0][0] == "A"

    def test_a_connector_reaches_bin_a_and_fires_servo_a(self):
        transport = FakeTransport(connected=True)
        result = self.run_for("Connector", transport)
        assert result["decision"]["decision"] == "A"
        assert transport.movements[0][0] == "A"

    def test_the_cpu_fraction_is_computed_against_a_cpu_sized_mass(self):
        """4.71 mg of gold in 22 g is 214 ppm. In 60 g it would read 78.

        The number moved on 2026-08-27 when the stand-in masses were set from
        published figures - Intel's LGA1155 guide gives 21.5 g typical, so 22 g
        replaced a round 25 g. CPU evidence is PER PIECE, so the fraction is a
        fixed mass of gold over this number and tracks it directly.
        """
        pmdi = self.run_for("CPU")["valuation"]["pmdi"]
        assert pmdi["precious_mass_fraction_ppm"] == pytest.approx(214.1, abs=0.5)

    def test_an_unknown_class_falls_back_to_the_default(self):
        cfg = self.mock_cfg()
        run = DemoSession(cfg=cfg)
        assert run.mock_mass_for("Widget") == cfg["demo.mock_mass.grams"]
        assert run.mock_mass_for(None) == cfg["demo.mock_mass.grams"]

    def test_the_snapshot_publishes_the_per_class_table(self):
        cfg = self.mock_cfg()
        per_class = DemoSession(cfg=cfg).snapshot()["mock_mass"]["per_class"]
        assert per_class == {"CPU": 22.0, "PCB": 60.0, "RAM": 20.0, "Connector": 5.0}


class TestCalibrationReload:
    """Confirmed stale on 2026-08-26: editing calibration.yaml under a live
    session changed nothing until uvicorn was restarted, and nothing said so."""

    @staticmethod
    def recalibrated(monkeypatch, counts_per_gram: float) -> Calibration:
        """Stand in for somebody having calibrated the cell and saved the file."""
        swapped = dataclasses.replace(VERIFIED, counts_per_gram=counts_per_gram)
        monkeypatch.setattr(Calibration, "load", classmethod(lambda cls: swapped))
        return swapped

    def test_a_changed_factor_reaches_the_session(self, monkeypatch):
        run = session()
        self.recalibrated(monkeypatch, 999.0)

        out = run.reload_calibration()
        assert out["changed"] is True
        assert out["before"]["counts_per_gram"] == VERIFIED.counts_per_gram
        assert run.calibration.counts_per_gram == 999.0

    def test_reloading_an_unchanged_file_says_nothing_changed(self, monkeypatch):
        run = session()
        self.recalibrated(monkeypatch, VERIFIED.counts_per_gram)
        assert run.reload_calibration()["changed"] is False

    def test_the_new_factor_is_what_the_next_reading_uses(self, monkeypatch):
        """Reassigning the attribute has to be enough — a cached sensor would
        keep the old factor and make the reload a lie."""
        run = session()
        self.recalibrated(monkeypatch, 999.0)
        run.reload_calibration()
        assert run.snapshot()["calibration"]["counts_per_gram"] == 999.0


class TestApplyingTheServoAngles:
    """The first CFG after a port opens loses its acknowledgement often enough
    to be the normal case: the board dumps a backlog when the port is opened
    and the ACK is buried in the tail of it. Measured on this bench on
    2026-08-27 - the first attempt burned its whole 4 s budget and failed, a
    second moments later was answered at once, and the operator's only remedy
    was to press Connect board a second time.
    """

    class CountingLink:
        """A board that acknowledges only from the nth attempt onwards."""

        def __init__(self, succeed_from: int):
            self.succeed_from = succeed_from
            self.attempts = 0

        def configure_servos(self, rest, push, hold, budget_s=4.0):
            self.attempts += 1
            return self.attempts >= self.succeed_from

    def test_a_first_attempt_that_is_not_acknowledged_is_offered_again(self):
        run = session()
        run.link = self.CountingLink(succeed_from=2)
        assert run._apply_servo_config() is True
        assert run.link.attempts == 2

    def test_it_stops_as_soon_as_the_board_answers(self):
        """CFG parks both paddles at rest, so a needless second one is a
        needless movement."""
        run = session()
        run.link = self.CountingLink(succeed_from=1)
        assert run._apply_servo_config() is True
        assert run.link.attempts == 1

    def test_a_board_that_never_answers_reports_failure(self):
        """Retrying must not turn a dead board into a configured one."""
        run = session()
        run.link = self.CountingLink(succeed_from=99)
        assert run._apply_servo_config() is False
        assert run.link.attempts == run.SERVO_CONFIG_ATTEMPTS
