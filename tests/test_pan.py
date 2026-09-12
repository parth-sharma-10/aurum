"""The automatic weighing cycle: the load cell drives the machine.

Every test here drives the real `DemoSession`, the real tracker, the real
material database, the real PMDI and the real decision engine. Only the cell
and the serial port are fakes, and no test presses anything: if a mass is
attached to an item in this file, the state machine decided to attach it.

The properties under test:

**Nothing processes on arrival.** Crossing the presence threshold starts a
cycle; what gets attached is whatever the sensor returns once it has settled.

**One object, one cycle.** The machine will not accept a second item until the
first has physically left the pan.

**Every failure completes the cycle.** An unstable mass, a yanked USB cable or
a mass with no identity each leave the machine able to handle the next object.
A machine that needs rescuing after a wobble is not automatic.
"""

from __future__ import annotations

import threading
import time

import pytest

from app import config
from app.hardware import ArduinoController, FakeTransport
from app.hardware.fault import FaultCode
from app.hardware.link import BoardLink
from app.pipeline.pan import PanState
from app.pipeline.session import DemoSession
from app.routing.scheduler import RouteStatus
from app.vision.tracker import TrackedDetection
from app.weight import Calibration, RawSample

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

BOARD = (0, 0, 400, 300)


class ScriptedCell:
    """A load cell whose mass the test moves, as a hand would."""

    name = "hx711-serial"

    def __init__(self, grams: float = 0.0):
        self.connected = True
        self.grams = grams
        self.last_error = None
        self.reads = 0
        #: Non-zero once the mass has been made to drift and never settle.
        self._drift = 0.0
        #: How many reads still have to come back empty. See `miss`.
        self._missing = 0

    def place(self, grams: float) -> None:
        self.grams = grams
        self._drift = 0.0

    def wobble(self, step: float = 3.0) -> None:
        """A mass that will not settle: every reading differs from the last.

        Endless on purpose. A finite list eventually runs out and the cell goes
        quiet, which settles - and would have this test pass for the wrong
        reason.
        """
        self._drift = step

    def miss(self, reads: int | None = 1) -> None:
        """Return nothing for the next `reads` calls, while staying connected.

        `None` means from now on: a cell that is wired up and streaming
        nothing a converter will accept, which is what an open one does.

        Not a fault, and that is the whole point of it. `BoardLink.next_weight`
        gives up after `conveyor.arduino.timeout_s` - one second - while the
        sketch goes deaf for the 1.711 s of a paddle stroke and the manual path
        drains the same queue from another thread. A perfectly healthy cell
        really does return None from time to time.
        """
        self._missing = reads

    def unplug(self) -> None:
        self.connected = False

    def read(self):
        self.reads += 1
        if not self.connected:
            self.last_error = "serial read failed: the cable was pulled"
            return None
        if self._missing is None:
            return None
        if self._missing:
            self._missing -= 1
            return None
        if self._drift:
            self.grams += self._drift
        # One count of wander, centred, because a converting cell never repeats
        # a value bit-for-bit - `app.weight.repeats_exactly` refuses a frozen
        # series as the hardware fault it is. Far below any tolerance here.
        wander = (self.reads % 3) - 1
        return RawSample(raw_counts=TARE_COUNTS + self.grams * COUNTS_PER_GRAM + wander)

    def close(self):
        self.connected = False


class FakeLink:
    def __init__(self, cell: ScriptedCell):
        self.connected = True
        self.weight_reader = cell
        #: The conveyor motor, which this rig's fake never runs.
        self.belt_running = False
        self.belt_pwm = 0
        #: Every belt call this double was asked to make, so a test can assert
        #: the shutdown path actually stopped the motor rather than assuming it.
        self.belt_calls: list[tuple[bool, int]] = []

    def belt(self, run: bool, pwm: int = 0, budget_s: float = 2.0) -> bool:
        self.belt_calls.append((run, pwm))
        self.belt_running = bool(run)
        self.belt_pwm = pwm if run else 0
        return True

    def snapshot(self):
        return {"connected": self.connected}

    def disconnect(self):
        self.connected = False


def session(cell: ScriptedCell, transport=None, **env) -> DemoSession:
    cfg = config.load(
        environ={
            "AURUM_ARDUINO_ENABLED": "true",
            # What a settling cell really costs, shortened: a test suite should
            # not sit through five seconds per reading.
            "AURUM_WEIGHT_TIMEOUT_S": "0.2",
            "AURUM_WEIGHT_STABILITY_WINDOW_MS": "10",
            "AURUM_PAN_CLEAR_SAMPLES": "2",
            **env,
        }
    )
    transport = FakeTransport(connected=True) if transport is None else transport
    run = DemoSession(cfg=cfg, controller=ArduinoController(transport=transport, cfg=cfg))
    run.calibration = VERIFIED
    run.link = FakeLink(cell)
    return run


def show(run: DemoSession, *detections: TrackedDetection, frames: int = 4) -> None:
    """Hold components in front of the camera until the tracker confirms them."""
    for frame in range(frames):
        run.pipeline.process_detections(list(detections), frame_id=frame)


def hide(run: DemoSession, frames: int = 25) -> None:
    """The operator picks the object up and carries it to the pan."""
    for _ in range(frames):
        run.pipeline.process_detections([])


def det(track_id: int, cls: str, box, conf: float = 0.94) -> TrackedDetection:
    return TrackedDetection(track_id=track_id, class_name=cls, confidence=conf, xyxy=box)


def run_until(run: DemoSession, state: PanState, limit: int = 40) -> PanState:
    """Step the machine until it reaches a state, or give up and say so."""
    for _ in range(limit):
        if run.pan.state is state:
            return state
        run.pan.step()
    pytest.fail(f"never reached {state}; stuck in {run.pan.state} ({run.pan.reason})")


def cycle(run: DemoSession) -> dict:
    """One full automatic cycle, and the record it produced."""
    run_until(run, PanState.WAITING_FOR_CLEAR)
    assert run.zone.held is not None
    return run._routed[run.zone.held.assembly_id]


class TestTheAutomaticCycle:
    def test_a_cpu_is_weighed_and_routed_with_nothing_pressed(self):
        transport = FakeTransport(connected=True)
        cell = ScriptedCell(0.0)
        run = session(cell, transport)
        show(run, det(1, "CPU", (10, 10, 90, 90)))

        assert run.pan.state is PanState.WAITING_FOR_OBJECT
        cell.place(42.7)
        record = cycle(run)

        assert record["class_name"] == "CPU"
        assert record["weight_g"] == pytest.approx(42.7)
        assert record["weight_status"] == "MEASURED"
        assert record["decision"]["decision"] == "A"
        assert transport.movements == [("A", record["actuation"]["command_id"])]

    def test_the_states_are_walked_in_order(self):
        cell = ScriptedCell(0.0)
        run = session(cell)
        show(run, det(1, "CPU", (10, 10, 90, 90)))
        run.pan.step()
        assert run.pan.state is PanState.WAITING_FOR_OBJECT

        cell.place(42.7)
        seen = [run.pan.state]
        for _ in range(10):
            state = run.pan.step()
            if state is not seen[-1]:
                seen.append(state)
            if state is PanState.WAITING_FOR_CLEAR:
                break

        assert seen == [
            PanState.WAITING_FOR_OBJECT,
            PanState.OBJECT_PRESENT,
            PanState.WEIGHING,
            PanState.WEIGHT_STABLE,
            PanState.PROCESSING,
            PanState.ROUTING,
            PanState.WAITING_FOR_CLEAR,
        ]

    def test_the_first_reading_over_the_threshold_is_not_the_one_recorded(self):
        """Crossing the threshold opens a cycle; it does not end one."""
        cell = ScriptedCell(0.0)
        run = session(cell)
        show(run, det(1, "CPU", (10, 10, 90, 90)))

        cell.place(6.0)  # just over pan.object_threshold_g
        run.pan.step()
        assert run.pan.state is PanState.OBJECT_PRESENT
        assert run.zone.held is not None
        assert run.zone.held.weight_g is None, "a mass was recorded on arrival"

        cell.place(42.7)  # the hand finished lowering it
        record = cycle(run)
        assert record["weight_g"] == pytest.approx(42.7)

    def test_a_pcb_below_the_threshold_never_starts_a_cycle(self):
        cell = ScriptedCell(1.0)
        run = session(cell)
        show(run, det(1, "PCB", BOARD))
        for _ in range(10):
            run.pan.step()
        assert run.pan.state is PanState.WAITING_FOR_OBJECT
        assert run.zone.held is None
        assert run._routed == {}


class TestIdentity:
    def test_the_identity_survives_the_object_leaving_the_camera(self):
        """The whole point of the latch: carried to the pan, still itself."""
        cell = ScriptedCell(0.0)
        run = session(cell)
        show(run, det(1, "CPU", (10, 10, 90, 90)))
        [expected] = run.assemblies
        expected_id = expected.assembly_id

        cell.place(42.7)
        run.pan.step()  # latch while it is still in view
        hide(run)  # carried away; the tracker finalizes it
        assert run.assemblies == [], "the tracker should have let go by now"

        record = cycle(run)
        assert record["item_id"] == expected_id
        assert record["actuation"]["item_id"] == expected_id

    def test_a_mass_with_no_confirmed_identity_is_never_processed(self):
        """Section 22: no item is invented to explain a number on the cell."""
        cell = ScriptedCell(0.0)
        run = session(cell)
        cell.place(500.0)

        for _ in range(10):
            run.pan.step()

        assert run.pan.state is PanState.WAITING_FOR_OBJECT
        assert run._routed == {}
        assert "no assembly has been confirmed" in run.pan.reason

    def test_a_single_frame_flicker_is_not_something_to_weigh(self):
        cell = ScriptedCell(0.0)
        run = session(cell)
        show(run, det(1, "RAM", (10, 10, 60, 200)), frames=1)
        cell.place(30.0)

        for _ in range(6):
            run.pan.step()

        assert run.zone.held is None
        assert run._routed == {}


class TestObjectRemoval:
    def test_a_second_object_is_refused_until_the_pan_is_clear(self):
        cell = ScriptedCell(0.0)
        run = session(cell)
        show(run, det(1, "CPU", (10, 10, 90, 90)))
        cell.place(42.7)
        first = cycle(run)

        # Still on the pan. A new object in view changes nothing.
        show(run, det(2, "PCB", BOARD), frames=4)
        for _ in range(10):
            run.pan.step()
        assert run.pan.state is PanState.WAITING_FOR_CLEAR
        assert list(run._routed) == [first["item_id"]]

    def test_removing_the_object_returns_the_machine_to_waiting(self):
        cell = ScriptedCell(0.0)
        run = session(cell)
        show(run, det(1, "CPU", (10, 10, 90, 90)))
        cell.place(42.7)
        cycle(run)

        cell.place(0.0)
        run_until(run, PanState.WAITING_FOR_OBJECT)
        assert run.zone.held is None
        assert run.pan.cycles == 1

    def test_two_objects_in_a_row_get_two_identities_and_two_movements(self):
        transport = FakeTransport(connected=True)
        cell = ScriptedCell(0.0)
        run = session(cell, transport)

        show(run, det(1, "CPU", (10, 10, 90, 90)))
        cell.place(42.7)
        first = cycle(run)
        cell.place(0.0)
        run_until(run, PanState.WAITING_FOR_OBJECT)
        hide(run)

        show(run, det(2, "CPU", (10, 10, 90, 90)))
        cell.place(38.0)
        second = cycle(run)

        assert first["item_id"] != second["item_id"]
        assert len(transport.movements) == 2
        assert run.pan.cycles == 2

    def test_an_object_that_is_never_removed_holds_the_machine_safely(self):
        """Not a deadlock to escape - a machine correctly refusing to guess."""
        cell = ScriptedCell(0.0)
        run = session(cell)
        show(run, det(1, "CPU", (10, 10, 90, 90)))
        cell.place(42.7)
        cycle(run)

        for _ in range(30):
            run.pan.step()
        assert run.pan.state is PanState.WAITING_FOR_CLEAR
        assert "Remove the object" in run.pan.reason


class TestFailures:
    def test_a_mass_that_will_not_settle_still_completes_the_cycle(self):
        cell = ScriptedCell(0.0)
        run = session(cell)
        show(run, det(1, "CPU", (10, 10, 90, 90)))
        cell.place(42.7)
        run.pan.step()  # OBJECT_PRESENT, latched
        cell.wobble()

        record = cycle(run)

        assert record["weight_status"] == "UNSTABLE"
        assert "Did not settle" in record["weight_reading"]["reason"]
        # An unsettled number is not a mass, so nothing downstream received one.
        assert record["valuation"]["weight_g"] is None
        assert record["valuation"]["pmdi"]["precious_mass_fraction_ppm"] is None
        # And the machine is ready for the next object rather than stuck.
        assert run.pan.state is PanState.WAITING_FOR_CLEAR

    def test_a_class_needing_a_mass_fails_closed_when_the_mass_will_not_settle(self):
        cell = ScriptedCell(0.0)
        run = session(cell)
        show(run, det(1, "PCB", BOARD))
        cell.place(180.0)
        run.pan.step()
        cell.wobble()

        record = cycle(run)

        assert record["weight_status"] == "UNSTABLE"
        assert record["decision"]["decision"] == "UNKNOWN"
        assert record["decision"]["physical_bin"] == "C"
        assert record["decision"]["reason_code"] == "UNKNOWN_WEIGHT"

    def test_the_cell_disconnecting_mid_weigh_is_reported_not_raised(self):
        cell = ScriptedCell(0.0)
        run = session(cell)
        show(run, det(1, "CPU", (10, 10, 90, 90)))
        cell.place(42.7)
        run.pan.step()
        cell.unplug()

        record = cycle(run)

        assert record["weight_status"] == "UNAVAILABLE"
        assert "cable" in record["weight_reading"]["reason"]
        # No mass is invented to fill the gap, and none is implied downstream.
        assert record["valuation"]["weight_g"] is None
        assert record["valuation"]["pmdi"]["precious_mass_fraction_ppm"] is None
        assert run.pan.state is PanState.WAITING_FOR_CLEAR

    def test_a_class_needing_a_mass_fails_closed_when_the_cell_disconnects(self):
        """The fail-closed half of the same failure.

        A processor is cited per piece, so its gold figure owes nothing to a
        scale and it can still be graded. A board is cited per kilogram, so
        without a mass there is nothing defensible to say and it goes to C.
        The difference is the evidence, not the hardware.
        """
        cell = ScriptedCell(0.0)
        run = session(cell)
        show(run, det(1, "PCB", BOARD))
        cell.place(180.0)
        run.pan.step()
        cell.unplug()

        record = cycle(run)

        assert record["weight_status"] == "UNAVAILABLE"
        assert record["decision"]["decision"] == "UNKNOWN"
        assert record["decision"]["physical_bin"] == "C"
        assert record["decision"]["reason_code"] == "UNKNOWN_WEIGHT"
        assert record["actuation"]["commanded"] is False

    def test_a_disconnected_cell_releases_rather_than_holding_an_object_forever(self):
        cell = ScriptedCell(0.0)
        run = session(cell)
        show(run, det(1, "CPU", (10, 10, 90, 90)))
        cell.place(42.7)
        run.pan.step()
        cell.unplug()
        cycle(run)

        run_until(run, PanState.WAITING_FOR_OBJECT)
        assert run.zone.held is None

    def test_no_board_at_all_leaves_the_machine_waiting_and_saying_why(self):
        cell = ScriptedCell(0.0)
        run = session(cell)
        run.link = None
        for _ in range(5):
            run.pan.step()
        assert run.pan.state is PanState.WAITING_FOR_OBJECT
        assert "No load cell is connected" in run.pan.reason

    def test_an_uncalibrated_cell_detects_nothing_and_says_so(self):
        """A count is not a mass. Nothing is inferred from an unscaled number."""
        cell = ScriptedCell(500.0)
        run = session(cell)
        run.calibration = Calibration()
        show(run, det(1, "CPU", (10, 10, 90, 90)))

        for _ in range(5):
            run.pan.step()

        assert run.pan.state is PanState.WAITING_FOR_OBJECT
        assert "not calibrated" in run.pan.reason
        assert run._routed == {}


class TestConcurrency:
    def test_the_session_lock_is_free_while_the_board_is_being_commanded(self):
        """Section 14: the vision pipeline must not freeze during actuation.

        The sketch acknowledges only after the paddle has finished its stroke,
        so a command blocks for most of a second. If that ran under the session
        lock the camera thread would stall with it, and the feed a judge is
        watching would visibly hitch on every routed item.
        """
        observed: list[bool] = []
        run: DemoSession | None = None

        class WatchingTransport(FakeTransport):
            def send(self, line: str) -> bool:
                if line.startswith("AURUM/1 MOVE"):
                    taken = threading.Event()

                    def probe():
                        got = run._lock.acquire(blocking=False)
                        observed.append(got)
                        if got:
                            run._lock.release()
                        taken.set()

                    threading.Thread(target=probe).start()
                    taken.wait(timeout=2.0)
                return super().send(line)

        cell = ScriptedCell(0.0)
        run = session(cell, WatchingTransport(connected=True))
        show(run, det(1, "CPU", (10, 10, 90, 90)))
        cell.place(42.7)
        cycle(run)

        assert observed == [True], "the session lock was held across the serial round trip"

    def test_two_clicks_on_one_item_produce_one_action(self):
        """`Measure & route now` claims the item, rather than asking and then acting.

        The check and the claim used to sit either side of a slow
        `_read_mass`, so two requests both passed "has this been handled" and
        both ran the whole chain: two valuations, two EPR trails, and a
        ROUTING_ERROR on the dashboard for an item that was only ever routed
        once. The dashboard reaches this endpoint from a button, and React
        StrictMode double-invokes an effect - two callers is the normal case,
        not a contrived one.
        """

        class SlowCell(ScriptedCell):
            """A cell that takes as long to answer as a real one does.

            The window between the check and the claim IS the settling read -
            up to `conveyor.weight.timeout_s`, five seconds on the bench. A
            fake that answers instantly closes it and the race never shows.
            """

            def read(self):
                time.sleep(0.05)
                return super().read()

        cell = SlowCell(0.0)
        run = session(cell, AURUM_DEMO_MOCK_MASS="true")
        show(run, det(1, "CPU", (10, 10, 90, 90)))
        cell.place(42.7)

        results: list[dict] = []
        start = threading.Barrier(4)

        def click():
            start.wait(timeout=5.0)
            results.append(run.measure_and_route())

        threads = [threading.Thread(target=click) for _ in range(3)]
        for thread in threads:
            thread.start()
        start.wait(timeout=5.0)
        for thread in threads:
            thread.join(timeout=10.0)

        routed = [r for r in results if not r.get("error")]
        refused = [r for r in results if r.get("error") == "ALREADY_PROCESSED"]
        assert len(routed) == 1, f"{len(routed)} callers each ran the chain"
        assert len(refused) == 2

    def test_routing_one_object_twice_keeps_the_first_action(self):
        """Both paths reach `_route` for one object, and one of them is a button.

        The automatic cycle routes what it weighed; `Measure & route now`
        routes what the camera confirmed. Press the button while the cycle is
        already handling that object and both arrive. Nothing moves twice - the
        scheduler and the board each refuse a second command for one item id -
        but the refusal OVERWROTE the good record with `ALREADY_ROUTED` and put
        a ROUTING_ERROR on the dashboard for an item that was routed exactly
        once. `tests/test_end_to_end.py` has been flaky on this for as long as
        it has driven a session whose pan thread is running.
        """
        cell = ScriptedCell(0.0)
        run = session(cell, AURUM_CONVEYOR_MODE="SIMULATION", AURUM_SIM_BELT_SPEED_CM_S="10.0")
        show(run, det(1, "CPU", (10, 10, 90, 90)))
        cell.place(42.7)
        run_until(run, PanState.WAITING_FOR_CLEAR)
        held = run.zone.held
        first = run._routed[held.assembly_id]["actuation"]
        assert first["scheduled"] is True

        again = run._route(held)

        assert again == first
        assert run._routed[held.assembly_id]["actuation"] == first
        codes = [e["error_code"] for e in run.errors.snapshot()["recent"]]
        assert "ROUTING_ERROR" not in codes, run.errors.snapshot()["recent"]

    def test_the_camera_thread_keeps_running_while_an_item_is_routed(self):
        """The same property, observed from the other side."""
        frames: list[int] = []
        run: DemoSession | None = None

        class SlowTransport(FakeTransport):
            def send(self, line: str) -> bool:
                if line.startswith("AURUM/1 MOVE"):
                    time.sleep(0.15)
                return super().send(line)

        cell = ScriptedCell(0.0)
        run = session(cell, SlowTransport(connected=True))
        show(run, det(1, "CPU", (10, 10, 90, 90)))

        stop = threading.Event()

        def camera():
            frame = 0
            while not stop.is_set():
                with run._lock:
                    run.pipeline.process_detections(
                        [det(1, "CPU", (10, 10, 90, 90))], frame_id=frame
                    )
                    frames.append(frame)
                frame += 1
                time.sleep(0.005)

        thread = threading.Thread(target=camera)
        thread.start()
        try:
            cell.place(42.7)
            cycle(run)
        finally:
            stop.set()
            thread.join(timeout=2.0)

        assert len(frames) > 5, "the camera thread was starved during actuation"


class TestMixedAssembly:
    """A motherboard: one object, several components, one mass, one decision.

    The configuration is written as geometry only. Nothing in the code under
    test knows that a large PCB with things on it is called a motherboard, and
    nothing here tells it.
    """

    @staticmethod
    def motherboard():
        return [
            det(1, "PCB", BOARD),
            det(2, "RAM", (20, 20, 60, 200)),
            det(3, "RAM", (70, 20, 110, 200)),
            det(4, "CPU", (180, 100, 250, 170)),
            det(5, "Connector", (300, 20, 330, 60)),
            det(6, "Connector", (300, 80, 330, 120)),
            det(7, "Connector", (300, 140, 330, 180)),
        ]

    def routed(self, transport=None):
        cell = ScriptedCell(0.0)
        run = session(cell, transport)
        show(run, *self.motherboard())
        cell.place(842.3)
        return run, cycle(run)

    def test_the_whole_board_is_one_item_with_one_id_and_one_mass(self):
        run, record = self.routed()
        assert record["is_assembly"] is True
        assert record["components"] == {"PCB": 1, "RAM": 2, "CPU": 1, "Connector": 3}
        assert record["weight_g"] == pytest.approx(842.3)
        assert record["weight_status"] == "MEASURED"
        assert len(run._routed) == 1, "one physical object produced more than one record"
        assert len(record["member_ids"]) == 7
        assert record["item_id"] in record["member_ids"]

    def test_the_boards_mass_is_never_attributed_to_the_board_alone(self):
        """Section 10 and 11, on the object they were written for."""
        _, record = self.routed()
        pmdi = record["valuation"]["pmdi"]

        assert pmdi["completeness"] == "PARTIAL_ESTIMATE"
        not_valued = {n["component"] for n in pmdi["not_valued"]}
        assert "PCB" in not_valued, "the board's mg/kg figure was applied to the whole object"
        assert "valued twice" in next(
            n["reason"] for n in pmdi["not_valued"] if n["component"] == "PCB"
        )

    def test_only_the_components_cited_per_piece_are_valued(self):
        """RAM joined this set when Charles et al. 2017 was obtained.

        The dividing line is the evidence BASIS, not the class: per-piece
        figures need no mass and are valued; the PCB's per-kilogram figure
        needs a mass that belongs to it alone, which an assembly's does not.
        """
        _, record = self.routed()
        pmdi = record["valuation"]["pmdi"]
        assert {v["component"] for v in pmdi["valued"]} == {"CPU", "Connector", "RAM"}
        assert {n["component"] for n in pmdi["not_valued"]} == {"PCB"}

    def test_the_estimate_scales_with_the_count_not_with_the_mass(self):
        """Three connectors are three connectors, whatever the board weighs."""
        cell = ScriptedCell(0.0)
        run = session(cell)
        show(run, det(1, "PCB", BOARD), det(2, "Connector", (300, 20, 330, 60)))
        cell.place(842.3)
        one = cycle(run)["valuation"]["pmdi"]["precious_mass_g"]

        _, three = self.routed()
        connectors = next(
            v for v in three["valuation"]["pmdi"]["valued"] if v["component"] == "Connector"
        )
        assert connectors["count"] == 3

        # The single-connector board also carries no CPU, so compare the
        # connector contribution alone rather than the totals.
        assert one > 0

    def test_the_two_modules_are_valued_on_count_not_on_assembly_mass(self):
        """Section 8's rule, on the object it was written for.

        The board weighs 842 g. The modules are valued as 2 x the per-module
        figures and nothing else: change the board's mass and the RAM
        contribution must not move by a milligram.
        """
        _, heavy = self.routed()

        cell = ScriptedCell(0.0)
        run = session(cell)
        show(run, *self.motherboard())
        cell.place(400.0)  # same board, half the mass
        light = cycle(run)

        assert heavy["weight_g"] != light["weight_g"]
        for record in (heavy, light):
            ram = next(v for v in record["valuation"]["pmdi"]["valued"] if v["component"] == "RAM")
            assert ram["count"] == 2
        assert (
            heavy["valuation"]["pmdi"]["precious_mass_g"]
            == light["valuation"]["pmdi"]["precious_mass_g"]
        )

    def test_the_ram_contribution_is_exactly_twice_one_module(self):
        _, record = self.routed()
        au = record["valuation"]["pmdi"]["precious_metals"]["Au"]
        # 2 modules x 18.0 mg + 1 CPU x 4.71 mg + 3 connectors x 0.914 mg
        assert au["grams"] == pytest.approx((2 * 18.0 + 4.71 + 3 * 0.914) / 1000)
        assert "2 x 18.0 mg per piece" in au["calculation"]

    def test_the_board_is_routed_automatically_with_nothing_pressed(self):
        transport = FakeTransport(connected=True)
        run, record = self.routed(transport)

        assert record["decision"]["decision"] in ("A", "B", "C")
        assert record["decision"]["reason_code"]
        # Whatever the bin, the machine reached it on its own and moved on.
        assert run.pan.state is PanState.WAITING_FOR_CLEAR
        assert run.pan.cycles == 1

    def test_a_board_with_no_modules_groups_and_routes_the_same_way(self):
        """Section 2's third configuration. No rule anywhere expects RAM."""
        cell = ScriptedCell(0.0)
        run = session(cell)
        show(
            run,
            det(1, "PCB", BOARD),
            det(2, "CPU", (180, 100, 250, 170)),
            det(3, "Connector", (300, 20, 330, 60)),
        )
        cell.place(700.0)
        record = cycle(run)

        assert record["components"] == {"PCB": 1, "CPU": 1, "Connector": 1}
        assert "RAM" not in record["components"]
        assert record["weight_status"] == "MEASURED"
        assert record["decision"]["reason_code"]

    def test_a_bare_board_alone_may_still_use_its_concentration(self):
        """Nothing else is detected on it, so the mass IS board material.

        The contrast that shows the refusal above is about attribution rather
        than about boards: the same class, the same evidence, a mass that this
        time belongs to it alone, and the figure is produced.
        """
        cell = ScriptedCell(0.0)
        run = session(cell)
        show(run, det(1, "PCB", BOARD))
        cell.place(180.0)
        record = cycle(run)

        pmdi = record["valuation"]["pmdi"]
        assert pmdi["completeness"] == "COMPLETE"
        assert pmdi["precious_mass_fraction_ppm"] == 2200.0
        assert record["decision"]["decision"] == "B"


class TestTheBeltStopsWhenTheMachineDoes:
    """A belt is the one part that keeps acting after the software stops.

    The firmware's 3 s lease is the last resort, not the mechanism: three
    seconds of belt after a human hit stop is three seconds too many.
    """

    def test_shutdown_stops_the_belt_before_dropping_the_link(self):
        run = session(ScriptedCell(0.0))
        run.link.belt(True, 120)
        run.link.belt_calls.clear()
        run.stop()
        assert (False, 0) in run.link.belt_calls

    def test_a_belt_that_will_not_stop_does_not_cost_the_disconnect(self):
        """Shutdown must not be stoppable by one sub-step."""

        run = session(ScriptedCell(0.0))

        def refuse(*_args, **_kwargs):
            raise RuntimeError("the belt is not answering")

        run.link.belt = refuse
        run.stop()
        assert run.link.connected is False


class TestTheThread:
    """The machine as it actually runs: its own thread, nothing driving it.

    Everything above calls `step()` so the lifecycle can be asserted exactly.
    This runs the loop that ships, because a state machine that only advances
    when a test pushes it is not an automatic machine.
    """

    def test_an_object_placed_on_the_pan_is_sorted_by_the_running_loop(self):
        transport = FakeTransport(connected=True)
        cell = ScriptedCell(0.0)
        run = session(cell, transport, AURUM_PAN_POLL_INTERVAL_S="0.01")
        show(run, det(1, "CPU", (10, 10, 90, 90)))

        assert run.start_pan() is True
        try:
            cell.place(42.7)
            # Wait for the ACTUATION, not merely for the record. `_process`
            # publishes the item as soon as the decision is taken and `_route`
            # fills in the movement afterwards, so polling for the record alone
            # can catch it between the two.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if any(r.get("actuation") for r in run._routed.values()):
                    break
                time.sleep(0.02)

            assert run._routed, f"nothing was sorted; pan is {run.pan.state} ({run.pan.reason})"
            [record] = run._routed.values()
            assert record["actuation"], f"decided but never routed; pan is {run.pan.state}"
            assert record["weight_status"] == "MEASURED"
            assert transport.movements == [("A", record["actuation"]["command_id"])]

            # And it returns to waiting once the object is taken away.
            cell.place(0.0)
            deadline = time.monotonic() + 5.0
            while run.zone.held is not None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert run.pan.state is PanState.WAITING_FOR_OBJECT
        finally:
            run.stop()

    def test_the_loop_does_not_start_when_automatic_operation_is_switched_off(self):
        cell = ScriptedCell(0.0)
        run = session(cell, AURUM_PAN_AUTO="false")
        show(run, det(1, "CPU", (10, 10, 90, 90)))

        assert run.start_pan() is False
        cell.place(42.7)
        time.sleep(0.1)
        assert run._routed == {}
        # The manual fallback still works, and produces the same record.
        record = run.measure_and_route()
        assert record["weight_status"] == "MEASURED"
        assert record["decision"]["decision"] == "A"

    def test_the_loop_survives_a_failing_sensor_rather_than_dying(self):
        """A USB cable on a bench gets knocked. The thread must outlive it."""
        cell = ScriptedCell(0.0)
        run = session(cell, AURUM_PAN_POLL_INTERVAL_S="0.01")

        class Exploding:
            name = "hx711-serial"
            connected = True
            last_error = None
            calls = 0

            def read(self):
                Exploding.calls += 1
                raise OSError("the port went away")

        run.link.weight_reader = Exploding()
        run.start_pan()
        try:
            time.sleep(0.2)
            assert run._pan_thread.is_alive()
            assert Exploding.calls > 1, "the loop stopped retrying"
            assert "error" in (run.pan.reason or "")
        finally:
            run.stop()


class TestReplayedHardwareStream:
    """The automatic cycle driven by real HX711 protocol lines.

    Everything above replaces the reader. This replaces only the serial port,
    so the bytes go through `parse_weight_line`, through `BoardLink`'s two-queue
    multiplexing, into `WeightSensor` and out to the servo command - the same
    path a real board takes.

    This is what "verified in software" means for the weighing path, and it is
    NOT a substitute for the bench. The cell on the physical rig is mechanically
    bypassed (180 g moved the reading by 2.2 counts against a 64-104 count noise
    floor), so no mass has ever been measured. See docs/hardware.md.
    """

    class ReplayPort:
        """A serial port that emits W frames and records what was written."""

        def __init__(self, counts_for):
            self._counts_for = counts_for
            self.written: list[str] = []
            self.millis = 0
            self.closed = False

        def readline(self) -> bytes:
            # The board answers a command before its next sample, which is what
            # makes the two streams share one port in the first place.
            for line in self.written:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "MOVE":
                    self.written.remove(line)
                    return f"AURUM/1 ACK {parts[4]}\n".encode()
                if len(parts) >= 3 and parts[1] == "CFG":
                    self.written.remove(line)
                    return f"AURUM/1 ACK {parts[5]}\n".encode()
            self.millis += 100
            # Centred one-count wander: see ScriptedCell.read.
            counts = self._counts_for() + (self.millis // 100 % 3) - 1
            return f"W,1,{self.millis},{counts},OK\n".encode()

        def write(self, payload: bytes) -> int:
            self.written.append(payload.decode().strip())
            return len(payload)

        def flush(self):
            return None

        def reset_input_buffer(self):
            return None

        def close(self):
            self.closed = True

    def link(self, grams_box):
        from app.hardware.transport import LinkState

        board = BoardLink("replay", baudrate=115200, timeout_s=0.1)
        board._serial = self.ReplayPort(lambda: int(TARE_COUNTS + grams_box[0] * COUNTS_PER_GRAM))
        board._state = LinkState.CONNECTED
        return board

    def test_a_cpu_is_sorted_from_raw_protocol_lines_alone(self):
        grams = [0.0]
        cfg = config.load(
            environ={
                "AURUM_ARDUINO_ENABLED": "true",
                "AURUM_WEIGHT_TIMEOUT_S": "0.5",
                "AURUM_WEIGHT_STABILITY_WINDOW_MS": "10",
                "AURUM_PAN_CLEAR_SAMPLES": "2",
            }
        )
        board = self.link(grams)
        run = DemoSession(cfg=cfg, link=board)
        run.calibration = VERIFIED
        run.controller = ArduinoController(transport=board.transport, cfg=cfg)
        show(run, det(1, "CPU", (10, 10, 90, 90)))

        grams[0] = 42.7
        record = cycle(run)

        assert record["weight_status"] == "MEASURED"
        assert record["weight_g"] == pytest.approx(42.7, abs=0.05)
        assert record["decision"]["decision"] == "A"
        assert record["actuation"]["state"] == "ACKED"
        moves = [w for w in board._serial.written if " MOVE " in w]
        assert not moves, "the MOVE frame was never answered"

    def test_a_board_of_several_components_survives_the_same_path(self):
        grams = [0.0]
        cfg = config.load(
            environ={
                "AURUM_ARDUINO_ENABLED": "true",
                "AURUM_WEIGHT_TIMEOUT_S": "0.5",
                "AURUM_WEIGHT_STABILITY_WINDOW_MS": "10",
                "AURUM_PAN_CLEAR_SAMPLES": "2",
            }
        )
        board = self.link(grams)
        run = DemoSession(cfg=cfg, link=board)
        run.calibration = VERIFIED
        run.controller = ArduinoController(transport=board.transport, cfg=cfg)
        show(run, *TestMixedAssembly.motherboard())

        grams[0] = 842.3
        record = cycle(run)

        assert record["components"] == {"PCB": 1, "RAM": 2, "CPU": 1, "Connector": 3}
        assert record["weight_status"] == "MEASURED"
        assert record["weight_g"] == pytest.approx(842.3, abs=0.5)
        assert record["valuation"]["pmdi"]["completeness"] == "PARTIAL_ESTIMATE"


class BeltLink(FakeLink):
    """A FakeLink whose conveyor motor records what it was asked to do.

    Models the real `BoardLink.belt` contract: an ACK flips `belt_running`, and
    a RUN is a lease the caller must keep renewing.
    """

    def __init__(self, cell: ScriptedCell):
        FakeLink.__init__(self, cell)
        self.calls: list[tuple[bool, int]] = []

    def belt(self, run: bool, pwm: int = 0, budget_s: float = 2.0) -> bool:
        self.calls.append((bool(run), int(pwm)))
        self.belt_running = bool(run)
        self.belt_pwm = int(pwm) if run else 0
        return True


class TestTheConveyorMotor:
    """Stop-and-go, and the stopping half is the safety-critical one.

    Measured on this bench, same pan, motor toggled: 0.084 g of noise stopped
    against 36.044 g running - about 430x. Components here weigh 5-200 g, so a
    mass taken while the belt runs is noise rather than a light object.
    """

    @staticmethod
    def belted(cell, **env):
        run = session(cell, AURUM_BELT_MOTOR_ENABLED="true", **env)
        run.link = BeltLink(cell)
        return run

    def test_the_belt_runs_while_the_machine_waits_for_an_object(self):
        run = self.belted(ScriptedCell(0.0))
        run._drive_belt(PanState.WAITING_FOR_OBJECT)
        assert run.link.belt_running is True
        assert run.link.calls == [(True, 120)]

    def test_the_belt_runs_again_to_carry_a_sorted_object_away(self):
        """WAITING_FOR_CLEAR is not idle: it is the object leaving."""
        run = self.belted(ScriptedCell(0.0))
        run._drive_belt(PanState.WAITING_FOR_CLEAR)
        assert run.link.belt_running is True

    @pytest.mark.parametrize(
        "state",
        [
            PanState.OBJECT_PRESENT,
            PanState.WEIGHING,
            PanState.WEIGHT_STABLE,
            PanState.PROCESSING,
            PanState.ROUTING,
        ],
    )
    def test_the_belt_is_stopped_for_every_state_that_handles_an_object(self, state):
        run = self.belted(ScriptedCell(0.0))
        run._drive_belt(PanState.WAITING_FOR_OBJECT)
        run.link.calls.clear()
        run._drive_belt(state)
        assert run.link.belt_running is False
        assert run.link.calls == [(False, 0)]

    def test_a_latched_fault_stops_the_belt(self):
        run = self.belted(ScriptedCell(0.0))
        run._drive_belt(PanState.WAITING_FOR_OBJECT)
        run.fault.latch(FaultCode.ACK_TIMEOUT, "a paddle may be half out", "AUR-ITEM-1")
        run._drive_belt(PanState.WAITING_FOR_OBJECT)
        assert run.link.belt_running is False

    def test_the_lease_is_renewed_rather_than_asserted_once(self):
        """The firmware expires a RUN after its watchdog, so a belt that is
        commanded once and never again stops on its own."""
        run = self.belted(ScriptedCell(0.0), AURUM_BELT_MOTOR_KEEPALIVE_S="0")
        for _ in range(3):
            run._drive_belt(PanState.WAITING_FOR_OBJECT)
        assert run.link.calls == [(True, 120)] * 3

    def test_an_unexpired_lease_is_not_renewed_every_pass(self):
        """The machine loop runs at 20 Hz; renewing on every pass would be 20
        command frames a second competing with the weight stream."""
        run = self.belted(ScriptedCell(0.0), AURUM_BELT_MOTOR_KEEPALIVE_S="60")
        for _ in range(5):
            run._drive_belt(PanState.WAITING_FOR_OBJECT)
        assert run.link.calls == [(True, 120)]

    def test_a_motor_that_is_not_enabled_is_never_commanded(self):
        """Default off, like actuation. A rig with nothing wired to D5/D7/D8
        must not be sent belt frames at all."""
        run = session(ScriptedCell(0.0))
        run.link = BeltLink(ScriptedCell(0.0))
        run._drive_belt(PanState.WAITING_FOR_OBJECT)
        assert run.link.calls == []

    def test_weighing_is_refused_while_the_belt_is_running(self):
        """The backstop under `_drive_belt`. A mass read over a running motor is
        noise, and nothing downstream could tell it from a light object."""
        cell = ScriptedCell(42.7)
        run = self.belted(cell)
        run.link.belt_running = True
        run.pan.state = PanState.WEIGHING
        assert run.pan.step() is PanState.WEIGHT_STABLE
        assert run.pan._reading is None
        assert "conveyor is running" in run.pan.reason

    def test_stop_belt_works_from_any_state(self):
        run = self.belted(ScriptedCell(0.0))
        run._drive_belt(PanState.WAITING_FOR_OBJECT)
        assert run.stop_belt() is True
        assert run.link.belt_running is False


class TestTheHardwareFallback:
    """A dead load cell must not cost the demonstration.

    The cell is what starts a cycle. With an open or absent one nothing ever
    arrives on the pan, so the automatic chain never runs at all - and a
    stand-in mass alone cannot help, because it substitutes a mass for an
    object already being handled, not the arrival itself.

    With the fallback armed the camera starts the cycle instead, for exactly
    as long as the cell cannot. Everything it produces stays SIMULATED.
    """

    def test_a_dead_cell_still_sorts_when_the_fallback_is_armed(self):
        transport = FakeTransport(connected=True)
        cell = ScriptedCell(0.0)
        run = session(cell, transport, AURUM_DEMO_MOCK_MASS="true")
        cell.unplug()
        show(run, det(1, "CPU", (10, 10, 90, 90)))

        record = cycle(run)

        assert record["class_name"] == "CPU"
        # The stand-in for a CPU, not the flat default: a precious fraction is
        # metal over TOTAL mass, so the wrong mass is a wrong ppm.
        assert record["weight_g"] == pytest.approx(run.mock_mass_for("CPU"))
        assert record["weight_status"] == "SIMULATED"
        assert run.pan.snapshot()["trigger"] == "camera"
        # The whole point of the fallback: a real decision, really actuated.
        assert record["decision"]["decision"] == "A"
        assert transport.movements == [("A", record["actuation"]["command_id"])]

    def test_the_fallback_produces_a_pmdi_figure(self):
        """A stand-in mass must still reach the number the demonstration shows."""
        cell = ScriptedCell(0.0)
        run = session(cell, AURUM_DEMO_MOCK_MASS="true")
        cell.unplug()
        show(run, det(1, "CPU", (10, 10, 90, 90)))

        pmdi = cycle(run)["valuation"]["pmdi"]

        assert pmdi["precious_mass_fraction_ppm"] is not None
        assert pmdi["pmdi_value"] is not None
        # Fabricated, and it must say so everywhere it surfaces.
        assert pmdi["mass_status"] == "SIMULATED"

    def test_a_live_cell_is_preferred_over_the_camera(self):
        """The fallback is armed, but a reading cell still drives the machine."""
        cell = ScriptedCell(0.0)
        run = session(cell, AURUM_DEMO_MOCK_MASS="true")
        show(run, det(1, "CPU", (10, 10, 90, 90)))
        cell.place(42.7)

        record = cycle(run)

        assert record["weight_status"] == "MEASURED"
        assert record["weight_g"] == pytest.approx(42.7)
        assert run.pan.snapshot()["trigger"] == "load-cell"

    def test_one_missed_frame_does_not_hand_the_machine_to_the_camera(self):
        """A gap in the weight stream is not a cell that cannot start cycles.

        This is what broke the demonstration. The board goes deaf for the whole
        1.711 s of a paddle stroke while `next_weight` gives up after one
        second, so a healthy cell returns None every so often - and arming the
        camera on the first one latched the object still in the operator's
        hand, gave it a stand-in mass and sorted it. The real arrival was then
        refused as "no assembly has been confirmed", because that id had
        already been handled.
        """
        cell = ScriptedCell(0.0)
        run = session(cell, AURUM_DEMO_MOCK_MASS="true")
        show(run, det(1, "CPU", (10, 10, 90, 90)))
        cell.miss(1)

        run.pan.step()

        assert run.pan.state is PanState.WAITING_FOR_OBJECT
        assert run.zone.held is None
        assert run.pan.snapshot()["trigger"] == "load-cell"

    def test_a_cell_that_stays_quiet_still_hands_over(self):
        """The fallback still exists: a cell that never speaks loses the machine."""
        cell = ScriptedCell(0.0)
        run = session(cell, AURUM_DEMO_MOCK_MASS="true", AURUM_DEMO_CAMERA_TRIGGER_QUIET_S="0")
        show(run, det(1, "CPU", (10, 10, 90, 90)))
        cell.miss(None)

        record = cycle(run)

        assert record["weight_status"] == "SIMULATED"
        assert run.pan.snapshot()["trigger"] == "camera"

    def test_a_missed_frame_does_not_release_an_object_still_on_the_pan(self):
        """The same gap at the other end of the cycle.

        Releasing on one empty read marks the object handled while it is still
        sitting on the cell, and the next confirmed assembly - the one the
        operator has already picked up - is latched against that mass.
        """
        cell = ScriptedCell(0.0)
        run = session(cell, AURUM_DEMO_MOCK_MASS="true")
        show(run, det(1, "CPU", (10, 10, 90, 90)))
        cell.place(42.7)
        cycle(run)
        held = run.zone.held
        assert held is not None

        cell.miss(1)
        run.pan.step()

        assert run.pan.state is PanState.WAITING_FOR_CLEAR
        assert run.zone.held is held

    def test_without_the_fallback_a_dead_cell_stalls_as_before(self):
        """The fallback ships off, and off it changes nothing."""
        cell = ScriptedCell(0.0)
        run = session(cell)
        cell.unplug()
        show(run, det(1, "CPU", (10, 10, 90, 90)))

        for _ in range(10):
            run.pan.step()

        assert run.pan.state is PanState.WAITING_FOR_OBJECT
        assert run.pan.snapshot()["trigger"] == "load-cell"


class TestAWeighMustNotStarveTheRouteQueue:
    """The pan loop weighs and fires paddles on ONE thread, and weighing blocks.

    With `conveyor.mode: SIMULATION` - what both shipped profiles use - a
    decision becomes a `ScheduledRoute` that fires seconds later: 3.0 s to
    Servo A and 6.0 s to Servo B from the load cell at the demonstration's
    10 cm/s. Those strokes are fired from the same loop that blocks on a
    settling mass, and `conveyor.routing.late_tolerance_ms` is 200 ms.

    MEASURED on this rig: one `step()` through WEIGHING held the loop for
    548 ms - 2.7x the tolerance - with a cell answering every 20 ms. A real
    HX711 at 10 Hz is slower and its timeout is five seconds. So a route whose
    moment lands inside a weigh is refused as EXPIRED and the item is never
    sorted, which is what an operator who reloads the pan before the previous
    paddle has fired gets.
    """

    def test_a_route_due_during_a_weigh_still_fires(self):
        class SlowCell(ScriptedCell):
            """A cell that takes as long to settle as a real one does."""

            def read(self):
                time.sleep(0.08)
                return super().read()

        cell = SlowCell(0.0)
        transport = FakeTransport(connected=True)
        run = session(
            cell,
            transport,
            AURUM_CONVEYOR_MODE="SIMULATION",
            AURUM_SIM_BELT_SPEED_CM_S="10.0",
            AURUM_WEIGHT_TIMEOUT_S="3.0",
            AURUM_WEIGHT_STABILITY_WINDOW_MS="500",
        )
        assert run.scheduler is not None, "this test needs the belt model"

        def wait_for(predicate, seconds=8.0, why=""):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if predicate():
                    return True
                time.sleep(0.02)
            pytest.fail(why or "timed out")

        run.start_pan()
        try:
            # Object one: weighed, decided, and now counting down to its paddle.
            show(run, det(1, "CPU", (10, 10, 90, 90)))
            cell.place(42.7)
            wait_for(lambda: run.pan.state is PanState.WAITING_FOR_CLEAR, why="no first cycle")
            first = run.zone.held.assembly_id
            route = run.scheduler.get(first)
            assert route.status is RouteStatus.SCHEDULED
            # Held off until the second object is on the pan, so the moment it
            # is finally given lands INSIDE a weigh rather than by luck.
            route.execute_at = time.monotonic() + 3600.0

            # The operator takes it off and loads the next one without waiting
            # for the paddle - which is the whole scenario.
            cell.place(0.0)
            hide(run)
            wait_for(lambda: run.pan.state is PanState.WAITING_FOR_OBJECT, why="pan never cleared")
            show(run, det(2, "PCB", BOARD))
            cell.place(180.0)

            wait_for(lambda: run.pan.state is PanState.WEIGHING, why="never started weighing")
            route.execute_at = time.monotonic() + 0.05

            wait_for(
                lambda: run.scheduler.get(first).status is not RouteStatus.SCHEDULED,
                why="the route was never acted on at all",
            )
        finally:
            run.stop()

        earlier = run.scheduler.get(first)
        assert earlier.status is RouteStatus.EXECUTED, (
            f"the stroke was lost while the machine weighed: "
            f"{earlier.status} / {earlier.reason_code}"
        )
        assert transport.movements[0][0] == "A"


class TestTheCameraTriggerCooldown:
    """One physical object the tracker loses and re-acquires is a NEW id.

    Measured on the bench state the runbook describes - cell open, stand-in
    mass, so the camera starts every cycle - one RAM module shown, lost and
    shown again produced THREE paddle strokes and three EPR item ids. RAM
    scores 0.51 recall and the runbook says it "flickers in and out", so this
    is the normal case for the hardest class in the set, not a contrived one.

    The camera cannot tell two objects from one object seen twice; nothing can,
    from an image alone. What it can do is refuse to sort twice in less time
    than a human takes to swap an object. That bounds the damage without
    pretending to have solved identity - and the pan, when it works, needs none
    of this, because a pan CAN say the object left.
    """

    def armed(self, **env):
        """A session on the camera trigger: the cell is gone, the mass is a stand-in."""
        cell = ScriptedCell(0.0)
        transport = FakeTransport(connected=True)
        run = session(cell, transport, AURUM_DEMO_MOCK_MASS="true", **env)
        cell.unplug()
        return run, transport

    def turn(self, run, track_id):
        """Show an object, run its cycle out, and let the tracker lose it."""
        show(run, det(track_id, "RAM", (40, 40, 200, 180)))
        for _ in range(40):
            run.pan.step()
            if run.pan.state is PanState.WAITING_FOR_CLEAR:
                break
        for _ in range(6):
            run.pan.step()
        hide(run)

    def test_the_same_module_seen_twice_strokes_the_paddle_once(self):
        run, transport = self.armed()

        self.turn(run, 7)
        self.turn(run, 8)
        self.turn(run, 9)

        assert len(transport.movements) == 1, (
            f"one physical object, {len(transport.movements)} paddle strokes"
        )

    def test_the_refusal_says_what_it_assumed(self):
        run, _ = self.armed()
        self.turn(run, 7)
        self.turn(run, 8)

        assert "cooldown" in (run.pan.reason or "").lower(), run.pan.reason

    def test_with_the_cooldown_off_it_behaves_as_before(self):
        """The rate limit is the whole mitigation; nothing else changed."""
        run, transport = self.armed(AURUM_DEMO_CAMERA_TRIGGER_COOLDOWN_S="0")

        self.turn(run, 7)
        self.turn(run, 8)

        assert len(transport.movements) == 2

    def test_the_load_cell_path_is_not_rate_limited(self):
        """A pan reports the object leaving. It needs no assumption about time."""
        cell = ScriptedCell(0.0)
        transport = FakeTransport(connected=True)
        run = session(cell, transport, AURUM_DEMO_MOCK_MASS="true")

        for track_id in (1, 2):
            show(run, det(track_id, "CPU", (10, 10, 90, 90)))
            cell.place(42.7)
            run_until(run, PanState.WAITING_FOR_CLEAR)
            cell.place(0.0)
            for _ in range(6):
                run.pan.step()
            hide(run)

        assert len(transport.movements) == 2


class TestTheEmptyPan:
    """Nothing on the cell is not a measurement of the object.

    The automatic path only weighs after a mass crosses
    `conveyor.weight.pan.object_threshold_g`. The developer fallback has no
    such gate, so on the bench the runbook actually describes - a stand-in
    mass, the operator never using the pan - `Measure & route now` handed the
    decision engine a settled, MEASURED 0.0 g. A PCB was then refused with
    `UNKNOWN_MASS_ANOMALY: -0 g is below the 20 g minimum plausible for a
    PCB`, which reads as a broken identity rather than an empty pan.
    """

    def test_an_empty_pan_does_not_become_this_object_s_mass(self):
        cell = ScriptedCell(0.0)
        run = session(cell, AURUM_DEMO_MOCK_MASS="true")
        show(run, det(1, "PCB", BOARD))

        record = run.measure_and_route()

        assert record["weight_status"] == "SIMULATED"
        assert record["decision"]["reason_code"] != "UNKNOWN_MASS_ANOMALY"
        assert record["decision"]["decision"] == "B"

    def test_with_no_stand_in_it_refuses_rather_than_weighing_zero(self):
        """Fail-closed, and for the right stated reason."""
        cell = ScriptedCell(0.0)
        run = session(cell)
        show(run, det(1, "PCB", BOARD))

        record = run.measure_and_route()

        assert record["weight_status"] == "UNAVAILABLE"
        assert record["decision"]["reason_code"] == "UNKNOWN_WEIGHT"

    def test_a_real_mass_is_still_a_real_mass(self):
        """The gate is the pan's arrival threshold, not a floor on every mass."""
        cell = ScriptedCell(0.0)
        run = session(cell, AURUM_DEMO_MOCK_MASS="true")
        show(run, det(1, "PCB", BOARD))
        cell.place(42.7)

        record = run.measure_and_route()

        assert record["weight_status"] == "MEASURED"
        assert record["weight_g"] == pytest.approx(42.7)


class TestAutomaticTare:
    def test_it_rezeroes_against_the_cell_as_it_reads_today(self):
        cell = ScriptedCell(0.0)
        run = session(cell)
        run.link = FakeLink(cell)

        result = run.auto_tare()

        assert result["tared"] is True
        # The empty pan, wherever the cell now says that is.
        assert result["tare_counts"] == pytest.approx(TARE_COUNTS, abs=1.0)
        # The factor is measured against a known mass and is never re-derived.
        assert run.calibration.counts_per_gram == COUNTS_PER_GRAM

    def test_it_refuses_a_cell_that_is_not_reading(self):
        """A dead converter must not be averaged into a fabricated zero."""
        cell = ScriptedCell(0.0)
        run = session(cell)
        cell.unplug()

        result = run.auto_tare()

        assert result["tared"] is False
        # The recorded zero survives, so a restart is still calibrated.
        assert run.calibration.tare_counts == TARE_COUNTS
