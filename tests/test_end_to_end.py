"""The acceptance chain, end to end, with no hardware attached.

    DETECT -> TRACK -> IDENTIFY -> MASS -> COMPOSITION -> PRICE -> PMDI
      -> VALUATION -> A/B/C -> CONVEYOR -> ETA -> SCHEDULE -> ARDUINO -> ACK
      -> SERVO RESULT -> EPR LEDGER -> DASHBOARD

Two configurations, because the machine genuinely has two:

**No belt** (`conveyor.mode: NONE`, the shipped state) - the operator carries
the object from the camera to the pan and the paddle moves immediately.

**A mock belt** (`conveyor.mode: SIMULATION`) - the item is upstream of the
paddle, so the decision produces a `ScheduledRoute` with a firing time and
nothing moves until that moment arrives.

Both run the same pipeline and both end in the same EPR trail. Every mass here
is a labelled stand-in and every belt figure is a demonstration value; the
point of these tests is that the record says so at every stage.
"""

from __future__ import annotations

import pytest

from app import config, epr
from app.hardware.arduino import ArduinoController
from app.hardware.transport import FakeTransport
from app.pipeline.session import DemoSession
from app.vision.tracker import TrackedDetection

BOARD = (10, 10, 400, 300)


def cfg(**environ):
    environ.setdefault("AURUM_DEMO_MOCK_MASS", "true")
    environ.setdefault("AURUM_ARDUINO_ENABLED", "true")
    environ.setdefault("AURUM_TRACK_MIN_DETECTIONS", "1")
    return config.load(environ=environ)


def det(track_id: int, class_name: str, box) -> TrackedDetection:
    return TrackedDetection(track_id, class_name, 0.95, box)


def run(**environ) -> tuple[DemoSession, FakeTransport]:
    """A session with a fake board, a stand-in mass and no camera."""
    settings = cfg(**environ)
    board = FakeTransport(connected=True)
    session = DemoSession(cfg=settings, controller=ArduinoController(transport=board, cfg=settings))
    return session, board


def show(session: DemoSession, *detections) -> None:
    """Put detections in front of the tracker until the item is confirmed."""
    for frame in range(4):
        session.pipeline.process_detections(list(detections), frame_id=frame)


class TestTheChainWithNoBelt:
    """The shipped machine: the operator carries the object, routing is now."""

    def test_a_cpu_runs_the_whole_chain_and_reaches_servo_a(self):
        session, board = run()
        show(session, det(1, "CPU", (10, 10, 90, 90)))

        record = session.measure_and_route()

        assert record["class_name"] == "CPU"
        assert record["weight_status"] == "SIMULATED"
        assert record["valuation"]["pmdi"]["available"] is True
        assert record["decision"]["decision"] == "A"
        assert record["actuation"]["state"] == "ACKED"
        assert board.movements == [("A", record["actuation"]["command_id"])]

    def test_the_epr_trail_covers_every_stage(self):
        session, _ = run()
        show(session, det(1, "CPU", (10, 10, 90, 90)))
        record = session.measure_and_route()

        events = [e["event"] for e in epr.history(record["item_id"])]
        assert events == [
            "DETECTED",
            "CLASSIFIED",
            "WEIGHED",
            "COMPOSITION_LOOKUP",
            "PMDI_CALCULATED",
            "VALUE_CALCULATED",
            "BIN_ASSIGNED",
            "SERVO_TRIGGERED",
            "SORT_CONFIRMED",
        ]

    def test_no_route_is_scheduled_because_there_is_no_belt(self):
        session, _ = run()
        assert session.scheduler is None
        show(session, det(1, "CPU", (10, 10, 90, 90)))
        record = session.measure_and_route()
        assert "SERVO_SCHEDULED" not in [e["event"] for e in epr.history(record["item_id"])]

    def test_a_stand_in_mass_is_stamped_simulated_all_the_way_to_the_ledger(self):
        session, _ = run()
        show(session, det(1, "CPU", (10, 10, 90, 90)))
        record = session.measure_and_route()

        trail = epr.history(record["item_id"])
        weighed = next(e for e in trail if e["event"] == "WEIGHED")
        assert weighed["simulated"] is True
        assert weighed["payload"]["mock"] is True
        assert epr.items()[0]["simulated"] is True

    def test_every_event_carries_its_provenance(self):
        session, _ = run()
        show(session, det(1, "CPU", (10, 10, 90, 90)))
        record = session.measure_and_route()

        for event in epr.history(record["item_id"]):
            prov = event["provenance"]
            assert prov["software_version"]
            assert prov["pmdi_version"]
            assert prov["composition_db_schema"]
            assert prov["price_provider"]
            assert prov["hardware_mode"] in ("PHYSICAL", "SIMULATION")
            # That the stamp CARRIES the verification flag, not what it happens
            # to say: this rig's calibration state legitimately changes when the
            # cell is calibrated, and a provenance test must survive that.
            assert isinstance(prov["calibration"]["verified"], bool)
            assert prov["mock_mass_enabled"] is True


class TestTheChainOverTheMockBelt:
    def belt(self, **environ):
        environ.setdefault("AURUM_CONVEYOR_MODE", "SIMULATION")
        return run(**environ)

    def test_the_decision_produces_a_firing_time_and_nothing_moves_yet(self):
        session, board = self.belt()
        show(session, det(1, "CPU", (10, 10, 90, 90)))

        record = session.measure_and_route()

        assert record["decision"]["decision"] == "A"
        assert record["actuation"]["scheduled"] is True
        assert record["actuation"]["route"]["servo"] == "SERVO_A"
        # The object is ON THE PAN, 25 cm downstream of the camera line the
        # distances are measured from, so 35 cm of the 60 cm to servo A is left:
        # 3.5 s at 10 cm/s. Scheduling it from the camera - which is what this
        # test used to assert - fires the paddle 2.5 s after the item passed.
        assert record["actuation"]["route"]["geometry"]["travel_time_s"] == pytest.approx(3.5)
        assert record["actuation"]["route"]["geometry"]["distance_cm"] == pytest.approx(35.0)
        assert board.sent == []

    def test_the_route_is_stamped_simulated(self):
        session, _ = self.belt()
        show(session, det(1, "CPU", (10, 10, 90, 90)))
        record = session.measure_and_route()
        assert record["actuation"]["route"]["simulated"] is True
        assert record["actuation"]["route"]["mode"] == "SIMULATED"

    def test_draining_before_the_moment_moves_nothing(self):
        session, board = self.belt()
        show(session, det(1, "CPU", (10, 10, 90, 90)))
        session.measure_and_route()

        assert session.drain_routes(now=0.0) == []
        assert board.movements == []

    def test_draining_after_the_moment_fires_the_paddle_and_confirms_the_sort(self):
        session, board = self.belt()
        show(session, det(1, "CPU", (10, 10, 90, 90)))
        record = session.measure_and_route()

        fire_at = record["actuation"]["route"]["execute_at"]
        results = session.drain_routes(now=fire_at + 0.1)

        assert [r["outcome"] for r in results] == ["ACTUATED"]
        assert board.movements[0][0] == "A"
        events = [e["event"] for e in epr.history(record["item_id"])]
        assert "SERVO_SCHEDULED" in events
        assert "SORT_CONFIRMED" in events

    def test_the_paddle_moves_once_however_often_the_loop_runs(self):
        session, board = self.belt()
        show(session, det(1, "CPU", (10, 10, 90, 90)))
        record = session.measure_and_route()
        fire_at = record["actuation"]["route"]["execute_at"]

        for _ in range(5):
            session.drain_routes(now=fire_at + 0.1)

        assert len(board.movements) == 1

    def test_a_pcb_is_scheduled_further_down_the_belt(self):
        session, _ = self.belt()
        show(session, det(1, "PCB", BOARD))
        record = session.measure_and_route()
        assert record["decision"]["decision"] == "B"
        # 90 cm to servo B, less the 25 cm the object has already travelled
        # to reach the pan.
        assert record["actuation"]["route"]["geometry"]["travel_time_s"] == pytest.approx(6.5)

    def test_bin_c_schedules_no_movement_and_confirms_no_sort(self):
        session, board = self.belt()
        show(session, det(1, "GPU", (10, 10, 90, 90)))
        record = session.measure_and_route()

        assert record["decision"]["decision"] == "UNKNOWN"
        assert record["decision"]["physical_bin"] == "C"
        assert record["actuation"]["route"]["status"] == "NO_ACTION"
        session.drain_routes(now=0.0)
        assert board.movements == []
        assert "SORT_CONFIRMED" not in [e["event"] for e in epr.history(record["item_id"])]

    def test_changing_the_belt_speed_changes_what_the_next_item_is_scheduled_on(self):
        """The session's scheduler re-reads the speed rather than caching it."""
        session, _ = self.belt()
        show(session, det(1, "CPU", (10, 10, 90, 90)))
        first = session.measure_and_route()
        assert first["actuation"]["route"]["geometry"]["belt_speed_cm_s"] == pytest.approx(10.0)

        session.conveyor.source.cm_s = 20.0

        assert session.conveyor.eta_seconds(60.0) == pytest.approx(3.0)
        assert session.conveyor.live_geometry().belt_speed_cm_s == pytest.approx(20.0)
        assert session.snapshot()["routing"]["geometry"]["belt_speed_cm_s"] == pytest.approx(20.0)


class TestFailureDoesNotProduceAFalseSort:
    def test_a_disconnected_board_records_a_failure_not_a_sort(self):
        settings = cfg()
        board = FakeTransport(connected=False)
        session = DemoSession(
            cfg=settings, controller=ArduinoController(transport=board, cfg=settings)
        )
        show(session, det(1, "CPU", (10, 10, 90, 90)))

        record = session.measure_and_route()

        assert record["decision"]["decision"] == "A"
        assert record["actuation"]["state"] == "FAILED"
        events = [e["event"] for e in epr.history(record["item_id"])]
        assert "SORT_FAILURE" in events
        assert "SORT_CONFIRMED" not in events

    def test_the_failure_reaches_the_error_log_with_a_code(self):
        settings = cfg()
        session = DemoSession(
            cfg=settings,
            controller=ArduinoController(transport=FakeTransport(connected=False), cfg=settings),
        )
        show(session, det(1, "CPU", (10, 10, 90, 90)))
        record = session.measure_and_route()

        entries = session.errors.for_item(record["item_id"])
        assert [str(e.code) for e in entries] == ["SERVO_ERROR"]
        assert entries[0].session_id == session.session_id

    def test_an_ack_timeout_latches_and_the_next_item_does_not_move(self):
        settings = cfg(AURUM_ARDUINO_ACK_TIMEOUT_MS="20")
        board = FakeTransport(connected=True, silent=True)
        session = DemoSession(
            cfg=settings, controller=ArduinoController(transport=board, cfg=settings)
        )
        show(session, det(1, "CPU", (10, 10, 90, 90)))
        first = session.measure_and_route()
        assert first["actuation"]["state"] == "TIMED_OUT"
        assert session.controller.fault.active

        # The board recovers. The machine does not, until somebody resets it.
        board.silent = False
        show(session, det(1, "CPU", (10, 10, 90, 90)), det(2, "CPU", (200, 200, 280, 280)))
        second_id = next(
            a.assembly_id for a in session.assemblies if a.assembly_id != first["item_id"]
        )
        second = session.measure_and_route(second_id)
        assert second["actuation"]["error_code"] == "HARDWARE_FAULT"
        assert len(board.movements) == 0

    def test_after_a_reset_the_machine_sorts_again(self):
        settings = cfg(AURUM_ARDUINO_ACK_TIMEOUT_MS="20")
        board = FakeTransport(connected=True, silent=True)
        session = DemoSession(
            cfg=settings, controller=ArduinoController(transport=board, cfg=settings)
        )
        show(session, det(1, "CPU", (10, 10, 90, 90)))
        first = session.measure_and_route()

        board.silent = False
        session.fault.reset(by="test")
        show(session, det(1, "CPU", (10, 10, 90, 90)), det(2, "CPU", (200, 200, 280, 280)))
        second_id = next(
            a.assembly_id for a in session.assemblies if a.assembly_id != first["item_id"]
        )
        second = session.measure_and_route(second_id)
        assert second["actuation"]["state"] == "ACKED"

    def test_the_ledger_aggregate_counts_confirmations_not_attempts(self):
        settings = cfg()
        session = DemoSession(
            cfg=settings,
            controller=ArduinoController(transport=FakeTransport(connected=False), cfg=settings),
        )
        show(session, det(1, "CPU", (10, 10, 90, 90)))
        session.measure_and_route()

        totals = epr.aggregates()
        assert totals["sort_confirmed"] == 0
        assert totals["sort_failed"] == 1


class TestTheSimulatedBoard:
    """HARDWARE_MODE=SIMULATION has no port, and must still run the machine.

    Without this path the whole decision-to-servo half was unreachable with no
    hardware attached: the session only ever built a controller off a real
    serial port, so a simulated run could detect, weigh, value and grade an
    item and then never actuate it.
    """

    def session(self, **environ):
        environ.setdefault("AURUM_SIMULATION", "true")
        environ.setdefault("AURUM_CONVEYOR_MODE", "SIMULATION")
        return DemoSession(cfg=cfg(**environ))

    def test_connecting_needs_no_port(self):
        run = self.session()
        assert run.connect_board()["connected"] is True

    def test_the_transport_is_simulated_not_serial(self):
        run = self.session()
        run.connect_board()
        assert run.controller.transport.name == "simulated"

    def test_a_configured_port_is_still_not_opened(self):
        run = self.session(AURUM_ARDUINO_PORT="/dev/definitely-not-a-real-port")
        run.connect_board()
        assert run.controller.transport.name == "simulated"

    def test_the_board_panel_says_there_is_no_serial_port(self):
        run = self.session()
        run.connect_board()
        board = run.snapshot()["board"]
        assert board["connected"] is True
        assert "no serial port" in board["port"]

    def test_a_physical_machine_with_no_port_still_refuses(self):
        run = DemoSession(cfg=cfg(AURUM_SIMULATION="false"))
        answer = run.connect_board()
        assert answer["connected"] is False
        assert "No board port is configured" in answer["reason"]

    def test_the_chain_reaches_a_servo_result(self):
        run = self.session()
        run.connect_board()
        show(run, det(1, "CPU", (10, 10, 90, 90)))
        record = run.measure_and_route()

        assert record["decision"]["decision"] == "A"
        fire_at = record["actuation"]["route"]["execute_at"]
        results = run.drain_routes(now=fire_at + 0.1)
        assert [r["outcome"] for r in results] == ["ACTUATED"]
        assert "SORT_CONFIRMED" in [e["event"] for e in epr.history(record["item_id"])]


class TestTheDashboardPayload:
    def test_the_snapshot_carries_every_panel_the_dashboard_renders(self):
        session, _ = run()
        show(session, det(1, "CPU", (10, 10, 90, 90)))
        session.measure_and_route()

        state = session.snapshot()
        for key in ("conveyor", "hardware", "pricing", "errors", "epr", "calibration", "pan"):
            assert key in state, key

    def test_the_current_item_keeps_its_chain_after_it_is_routed(self):
        """`assemblies` is regrouped on every read, so the object the camera
        can still see is a fresh Assembly with no mass, decision or actuation
        on it. Showing that blanked the whole chain the moment an item
        finished - which is exactly when it is worth reading."""
        session, _ = run()
        show(session, det(1, "CPU", (10, 10, 90, 90)))
        record = session.measure_and_route()

        current = session.snapshot()["current_item"]
        assert current["item_id"] == record["item_id"]
        assert current["decision"]["decision"] == "A"
        assert current["weight_status"] == "SIMULATED"

    def test_the_conveyor_panel_never_implies_a_measured_belt(self):
        session, _ = run(AURUM_CONVEYOR_MODE="SIMULATION")
        conveyor = session.snapshot()["conveyor"]
        assert conveyor["mode"] == "SIMULATION"
        assert conveyor["speed"]["status"] == "SIMULATED"
        assert conveyor["speed"]["m_s"] == pytest.approx(0.10)

    def test_the_hardware_panel_carries_the_mode_and_the_fault(self):
        session, _ = run()
        hardware = session.snapshot()["hardware"]
        assert hardware["mode"] == "PHYSICAL"
        assert hardware["fault"]["active"] is False

    def test_the_pricing_panel_says_which_prices_are_current(self):
        session, _ = run()
        pricing = session.snapshot()["pricing"]
        assert pricing["currency"] == "INR"
        for metal, quote in pricing["metals"].items():
            assert quote["status"] in ("LIVE", "REFERENCE", "STALE", "UNAVAILABLE", "ERROR"), metal


class TestTheDemonstrationWithNothingPluggedIn:
    """Camera only: no board, no load cell, no belt.

    This is `configs/demo-profile.sh`, and it is the state the bench is in
    whenever the board is not enumerating. The whole point of the fallback is
    that this still runs: the camera supplies the arrival the pan cannot, the
    stand-in supplies the mass the cell cannot, and the belt model supplies the
    travel time the motor cannot.

    Everything it produces is stamped SIMULATED and none of it may be quoted as
    a measurement. What is real here is the decision: the same engine, the same
    cited evidence, the same thresholds.
    """

    def run(self) -> DemoSession:
        return DemoSession(
            cfg=cfg(
                AURUM_SIMULATION="true",
                AURUM_CONVEYOR_MODE="SIMULATION",
                AURUM_SIM_BELT_SPEED_CM_S="10.0",
                AURUM_ARDUINO_ENABLED="true",
                AURUM_DEMO_MOCK_MASS="true",
            )
        )

    def sorted_cpu(self, run: DemoSession) -> dict:
        from app.pipeline.pan import PanState

        run.connect_board()
        det = TrackedDetection(track_id=1, class_name="CPU", confidence=0.94, xyxy=(10, 10, 90, 90))
        for frame in range(6):
            run.pipeline.process_detections([det], frame_id=frame)
        for _ in range(60):
            if run.pan.state is PanState.WAITING_FOR_CLEAR:
                break
            run.pan.step()
        held = run.zone.held
        assert held is not None, f"nothing was latched: {run.pan.reason}"
        return run._routed[held.assembly_id]

    def test_the_chain_runs_with_no_hardware_at_all(self):
        run = self.run()
        # The precondition, asserted rather than assumed: there is no cell.
        run.connect_board()
        assert run.weight_sensor is None

        record = self.sorted_cpu(self.run())

        assert record["class_name"] == "CPU"
        assert run.pan.snapshot()["trigger_fallback_armed"] is True

    def test_it_reaches_a_bin_and_schedules_a_paddle(self):
        record = self.sorted_cpu(self.run())
        assert record["decision"]["decision"] == "A"
        # A scheduled route is a time, not a movement - but it is the thing the
        # demonstration shows, and without a belt model there would be none.
        assert record["actuation"]["scheduled"] is True
        assert record["actuation"]["servo"] == "SERVO_A"

    def test_it_produces_the_pmdi_figures(self):
        pmdi = self.sorted_cpu(self.run())["valuation"]["pmdi"]
        assert pmdi["precious_mass_fraction_ppm"] is not None
        assert pmdi["pmdi_value"] is not None

    def test_nothing_it_produces_claims_to_be_measured(self):
        record = self.sorted_cpu(self.run())
        assert record["weight_status"] == "SIMULATED"
        assert record["valuation"]["overall_status"] == "SIMULATED"
        assert record["actuation"]["route"]["simulated"] is True
