"""Tests for the Arduino command layer and the servo actuator.

The property that matters most: **one physical item can never produce two
paddle movements.** A second movement lands on whatever is behind the first
item, so there are three independent barriers — the scheduler's per-item route,
the controller's per-item and per-command-id suppression, and the sketch's
recent-id list — and these tests exercise the two that live in Python.

The second property: **an ACK is not a movement.** Nothing here, and nothing
in the code under test, treats a returning write or an acknowledgement as
evidence that a servo physically moved.

No board is attached. `FakeTransport` answers the real protocol, so the layer
above is exercised rather than imitated.
"""

from __future__ import annotations

import pytest

from app import config
from app.hardware import (
    ActuationOutcome,
    ArduinoController,
    CommandState,
    FakeTransport,
    LinkState,
    ServoActuator,
    build_frame,
    new_command_id,
    parse_response,
)
from app.hardware.fault import FaultCode
from app.routing import RouteStatus, RoutingScheduler
from app.routing.geometry import Geometry, RoutingMode

T0 = 10.0

#: The moment each paddle's route actually fires, from the TEST geometry
#: below: 60 cm and 90 cm at 20 cm/s, less a 150 ms actuation delay. Named
#: rather than padded to "comfortably past it", because a route reached long
#: after its moment is refused as EXPIRED now - the item has gone by, and a
#: catch-up strikes whatever is behind it. "Some seconds later" stopped
#: being a physical thing for a test to ask for.
DUE_A = T0 + 2.85
DUE_B = T0 + 4.35


def cfg(**env):
    env.setdefault("AURUM_ARDUINO_ENABLED", "true")
    return config.load(environ={k: str(v) for k, v in env.items()})


def controller(transport=None, **env) -> ArduinoController:
    transport = FakeTransport(connected=True) if transport is None else transport
    return ArduinoController(transport=transport, cfg=cfg(**env))


def geometry() -> Geometry:
    """TEST geometry: 60 cm at 20 cm/s, 150 ms of paddle."""
    return Geometry(
        mode=RoutingMode.SIMULATED,
        belt_speed_cm_s=20.0,
        camera_to_load_cell_cm=25.0,
        camera_to_servo_a_cm=60.0,
        camera_to_servo_b_cm=90.0,
        servo_actuation_delay_ms=150.0,
        timing_offset_ms=0.0,
    )


def rig(transport=None, **env):
    """A scheduler and an actuator wired to a fake board."""
    transport = FakeTransport(connected=True) if transport is None else transport
    configuration = cfg(**env)
    scheduler = RoutingScheduler(geometry=geometry(), cfg=configuration)
    ctl = ArduinoController(transport=transport, cfg=configuration)
    return scheduler, ServoActuator(scheduler, controller=ctl, cfg=configuration), transport


class TestProtocolFrames:
    def test_a_move_frame_is_versioned_and_carries_both_ids(self):
        frame = build_frame("A", "AUR-ITEM-1", "CMD-ABC")
        assert frame == "AURUM/1 MOVE A AUR-ITEM-1 CMD-ABC"

    def test_command_ids_are_unique(self):
        assert len({new_command_id() for _ in range(2000)}) == 2000

    @pytest.mark.parametrize(
        ("line", "verb"),
        [
            ("AURUM/1 ACK CMD-1", "ACK"),
            ("AURUM/1 ERR CMD-1 BAD_TARGET", "ERR"),
            ("AURUM/1 PONG CMD-1", "PONG"),
        ],
    )
    def test_board_replies_parse(self, line, verb):
        assert parse_response(line).verb == verb

    def test_a_duplicate_ack_is_flagged(self):
        assert parse_response("AURUM/1 ACK CMD-1 DUP").duplicate is True

    @pytest.mark.parametrize(
        "line",
        [
            "W,1,10432,-261605,OK",  # a weight frame shares the link
            "AURUM/2 ACK CMD-1",  # a protocol we do not speak
            "ACK CMD-1",
            "AURUM/1 ACK",
            "",
            "   ",
            "garbage",
            None,
            42,
        ],
    )
    def test_anything_else_is_not_an_acknowledgement(self, line):
        assert parse_response(line) is None


class TestSuccess:
    def test_a_move_is_acknowledged(self):
        ctl = controller()
        command = ctl.move("A", "AUR-ITEM-1")
        assert command.state is CommandState.ACKED
        assert command.acknowledged

    def test_servo_a_and_servo_b_both_command(self):
        board = FakeTransport(connected=True)
        ctl = controller(board)
        assert ctl.move("A", "AUR-ITEM-1").state is CommandState.ACKED
        assert ctl.move("B", "AUR-ITEM-2").state is CommandState.ACKED
        assert board.movements == [("A", board.movements[0][1]), ("B", board.movements[1][1])]

    def test_an_ack_is_never_described_as_movement(self):
        record = controller().move("A", "AUR-ITEM-1").as_dict()
        assert "not evidence that a servo physically moved" in record["ack_meaning"]

    def test_the_frame_and_reply_are_kept_for_audit(self):
        command = controller().move("A", "AUR-ITEM-1")
        assert command.raw_frame.startswith("AURUM/1 MOVE A AUR-ITEM-1")
        assert "ACK" in command.raw_response


class TestFailureModes:
    def test_no_acknowledgement_times_out(self):
        ctl = controller(
            FakeTransport(connected=True, silent=True), AURUM_ARDUINO_ACK_TIMEOUT_MS=20
        )
        command = ctl.move("A", "AUR-ITEM-1")
        assert command.state is CommandState.TIMED_OUT

    def test_a_timeout_is_not_retried(self):
        """A blind resend is how one item gets moved twice."""
        ctl = controller(
            FakeTransport(connected=True, silent=True), AURUM_ARDUINO_ACK_TIMEOUT_MS=20
        )
        command = ctl.move("A", "AUR-ITEM-1")
        assert "Not retried" in command.reason

    def test_a_board_error_fails_the_command(self):
        ctl = controller(FakeTransport(connected=True, fail_with="STALLED"))
        command = ctl.move("A", "AUR-ITEM-1")
        assert command.state is CommandState.FAILED
        assert command.error_code == "STALLED"

    def test_a_disconnected_board_fails_safely(self):
        command = controller(FakeTransport(connected=False)).move("A", "AUR-ITEM-1")
        assert command.state is CommandState.FAILED
        assert command.error_code == "NOT_CONNECTED"

    def test_unplugging_mid_run_does_not_raise(self):
        board = FakeTransport(connected=True)
        ctl = controller(board)
        ctl.move("A", "AUR-ITEM-1")
        board.unplug()
        assert ctl.move("B", "AUR-ITEM-2").error_code == "NOT_CONNECTED"

    def test_bin_c_is_not_an_actuator(self):
        command = controller().move("C", "AUR-ITEM-1")
        assert command.state is CommandState.FAILED
        assert command.error_code == "BAD_TARGET"
        assert command.servo is None if hasattr(command, "servo") else True

    @pytest.mark.parametrize("target", ["C", "D", "", "a", None])
    def test_an_invalid_target_never_writes_a_frame(self, target):
        board = FakeTransport(connected=True)
        controller(board).move(target, "AUR-ITEM-1")
        assert board.sent == []

    def test_actuation_disabled_refuses(self):
        """Ships off. Nothing moves until someone turns it on deliberately."""
        board = FakeTransport(connected=True)
        ctl = ArduinoController(transport=board, cfg=config.load(environ={}))
        command = ctl.move("A", "AUR-ITEM-1")
        assert command.error_code == "ACTUATION_DISABLED"
        assert board.sent == []


class TestDuplicateProtection:
    def test_the_same_item_is_only_commanded_once(self):
        board = FakeTransport(connected=True)
        ctl = controller(board)
        ctl.move("A", "AUR-ITEM-1")
        second = ctl.move("A", "AUR-ITEM-1")
        assert second.state is CommandState.SUPPRESSED
        assert len(board.movements) == 1

    def test_a_repeat_cannot_change_the_target(self):
        board = FakeTransport(connected=True)
        ctl = controller(board)
        ctl.move("A", "AUR-ITEM-1")
        assert ctl.move("B", "AUR-ITEM-1").state is CommandState.SUPPRESSED
        assert [t for t, _ in board.movements] == ["A"]

    def test_a_resent_command_id_is_suppressed(self):
        board = FakeTransport(connected=True)
        ctl = controller(board)
        command_id = new_command_id()
        ctl.move("A", "AUR-ITEM-1", command_id=command_id)
        assert ctl.move("A", "AUR-ITEM-2", command_id=command_id).state is CommandState.SUPPRESSED
        assert len(board.movements) == 1

    def test_the_board_itself_refuses_a_repeated_id(self):
        """The last barrier: an ACK lost on the wire must not move it twice."""
        board = FakeTransport(connected=True)
        frame = build_frame("A", "AUR-ITEM-1", "CMD-FIXED")
        board.send(frame)
        board.send(frame)
        assert len(board.movements) == 1
        assert board.receive() == "AURUM/1 ACK CMD-FIXED"
        assert board.receive() == "AURUM/1 ACK CMD-FIXED DUP"

    def test_different_items_command_independently(self):
        board = FakeTransport(connected=True)
        ctl = controller(board)
        ctl.move("A", "AUR-ITEM-1")
        ctl.move("B", "AUR-ITEM-2")
        assert len(board.movements) == 2


class TestLinkLifecycle:
    def test_a_fresh_transport_is_disconnected(self):
        assert FakeTransport().state is LinkState.DISCONNECTED

    def test_connecting_reports_connected(self):
        assert controller(FakeTransport()).connect() is LinkState.CONNECTED

    def test_ping_round_trips(self):
        assert controller().ping() is True

    def test_ping_fails_when_disconnected(self):
        assert controller(FakeTransport(connected=False)).ping() is False

    def test_ping_fails_when_the_board_is_silent(self):
        ctl = controller(
            FakeTransport(connected=True, silent=True), AURUM_ARDUINO_ACK_TIMEOUT_MS=20
        )
        assert ctl.ping() is False

    def test_reconnecting_alone_does_not_allow_a_later_command(self):
        """Changed 2026-08-26 by app/hardware/fault.py, deliberately.

        This used to assert that a reconnect was enough. It is not: a link that
        dropped mid-command left the paddle in a position nobody knows, and the
        latch exists so that the next item is not commanded into that. The
        cable coming back is not somebody having looked at the rig.
        """
        board = FakeTransport(connected=True)
        ctl = controller(board)
        board.unplug()
        assert ctl.move("A", "AUR-ITEM-1").error_code == "NOT_CONNECTED"
        ctl.connect()
        assert ctl.move("A", "AUR-ITEM-2").error_code == "HARDWARE_FAULT"
        assert board.connects >= 1

    def test_reconnecting_and_resetting_the_fault_allows_a_later_command(self):
        board = FakeTransport(connected=True)
        ctl = controller(board)
        board.unplug()
        ctl.move("A", "AUR-ITEM-1")
        ctl.connect()
        ctl.fault.reset(by="test")
        assert ctl.move("A", "AUR-ITEM-2").state is CommandState.ACKED


class TestServoConfiguration:
    def test_the_bench_angles_are_pushed_to_the_board(self):
        board = FakeTransport(connected=True)
        assert controller(board).configure().state is CommandState.ACKED
        assert board.servo_config == ("0", "90", "700")

    def test_the_angles_are_configurable_without_reflashing(self):
        board = FakeTransport(connected=True)
        ctl = controller(
            board,
            AURUM_SERVO_REST_ANGLE_DEG=10,
            AURUM_SERVO_PUSH_ANGLE_DEG=75,
            AURUM_SERVO_ACTUATION_MS=400,
        )
        ctl.configure()
        assert board.servo_config == ("10", "75", "400")

    def test_configuring_a_disconnected_board_fails_safely(self):
        assert controller(FakeTransport(connected=False)).configure().error_code == "NOT_CONNECTED"

    def test_the_angles_are_labelled_bench_values(self):
        scheduler, actuator, _ = rig()
        assert "BENCH/TEST" in actuator.servo_settings["basis"]
        assert "no conveyor exists" in actuator.servo_settings["basis"]


class TestActuator:
    def test_a_due_a_route_is_actuated_and_marked_executed(self):
        scheduler, actuator, board = rig()
        scheduler.schedule("AUR-ITEM-1", "A", T0)
        results = actuator.execute_due(now=DUE_A)
        assert [r.outcome for r in results] == [ActuationOutcome.ACTUATED]
        assert scheduler.get("AUR-ITEM-1").status is RouteStatus.EXECUTED
        assert board.movements[0][0] == "A"

    def test_a_due_b_route_is_actuated(self):
        scheduler, actuator, board = rig()
        scheduler.schedule("AUR-ITEM-2", "B", T0)
        assert actuator.execute_due(now=DUE_B)[0].outcome is ActuationOutcome.ACTUATED
        assert board.movements[0][0] == "B"

    def test_bin_c_sends_no_frame(self):
        scheduler, actuator, board = rig()
        route = scheduler.schedule("AUR-ITEM-3", "C", T0)
        result = actuator.actuate(route, now=T0 + 5)
        assert result.outcome is ActuationOutcome.NO_ACTION
        assert board.sent == []

    def test_a_route_that_has_not_arrived_is_skipped(self):
        scheduler, actuator, board = rig()
        route = scheduler.schedule("AUR-ITEM-1", "A", T0)
        assert actuator.actuate(route, now=T0).outcome is ActuationOutcome.SKIPPED
        assert board.sent == []

    def test_an_expired_route_never_reaches_the_board(self):
        """The scheduler refuses it; the actuator must not resurrect it."""
        scheduler, actuator, board = rig()
        route = scheduler.schedule("AUR-ITEM-1", "A", T0, now=T0 + 600)
        assert route.status is RouteStatus.UNSCHEDULED
        assert actuator.actuate(route, now=T0 + 600).outcome is ActuationOutcome.SKIPPED
        assert board.sent == []

    def test_an_unscheduled_route_never_reaches_the_board(self):
        scheduler = RoutingScheduler(geometry=Geometry(mode=RoutingMode.REAL), cfg=cfg())
        board = FakeTransport(connected=True)
        actuator = ServoActuator(scheduler, controller=controller(board), cfg=cfg())
        route = scheduler.schedule("AUR-ITEM-1", "A", T0)
        assert actuator.actuate(route, now=T0 + 5).outcome is ActuationOutcome.SKIPPED
        assert board.sent == []

    def test_a_failed_command_does_not_mark_the_route_executed(self):
        board = FakeTransport(connected=True, fail_with="STALLED")
        scheduler, actuator, _ = rig(board)
        scheduler.schedule("AUR-ITEM-1", "A", T0)
        assert actuator.execute_due(now=DUE_A)[0].outcome is ActuationOutcome.FAILED
        assert scheduler.get("AUR-ITEM-1").status is RouteStatus.SCHEDULED

    def test_a_timeout_does_not_mark_the_route_executed(self):
        board = FakeTransport(connected=True, silent=True)
        scheduler, actuator, _ = rig(board, AURUM_ARDUINO_ACK_TIMEOUT_MS=20)
        scheduler.schedule("AUR-ITEM-1", "A", T0)
        assert actuator.execute_due(now=DUE_A)[0].outcome is ActuationOutcome.FAILED
        assert scheduler.get("AUR-ITEM-1").status is RouteStatus.SCHEDULED

    def test_a_failure_is_not_retried_on_the_next_tick(self):
        """A failed route stays SCHEDULED, so `due()` keeps offering it.

        The loop must drop it silently rather than produce a SKIPPED result
        every tick: the machine loop runs at 20 Hz and each result became an
        EPR failure row and an error-log entry for the same one item.
        """
        board = FakeTransport(connected=True, fail_with="STALLED")
        scheduler, actuator, _ = rig(board)
        scheduler.schedule("AUR-ITEM-1", "A", T0)
        actuator.execute_due(now=DUE_A)
        for tick in range(6, 12):
            assert actuator.execute_due(now=T0 + tick) == []
        assert len(board.movements) == 0

    def test_actuating_a_failed_route_directly_still_refuses(self):
        board = FakeTransport(connected=True, fail_with="STALLED")
        scheduler, actuator, _ = rig(board)
        route = scheduler.schedule("AUR-ITEM-1", "A", T0)
        actuator.execute_due(now=DUE_A)
        again = actuator.actuate(route, now=DUE_A + 1)
        assert again.outcome is ActuationOutcome.SKIPPED
        assert "not a licence to move the paddle again" in again.reason
        assert len(board.movements) == 0

    def test_draining_twice_moves_nothing_twice(self):
        scheduler, actuator, board = rig()
        scheduler.schedule("AUR-ITEM-1", "A", T0)
        actuator.execute_due(now=DUE_A)
        actuator.execute_due(now=DUE_B)
        assert len(board.movements) == 1

    def test_several_items_actuate_independently(self):
        scheduler, actuator, board = rig()
        scheduler.schedule("AUR-ITEM-1", "A", T0)
        scheduler.schedule("AUR-ITEM-2", "B", T0)
        scheduler.schedule("AUR-ITEM-3", "C", T0)
        # Two drains, because the paddles fire 1.5 s apart and the machine
        # loop runs at 20 Hz. One call catching both would mean one of them
        # fired a second and a half after the item went past it.
        results = actuator.execute_due(now=DUE_A) + actuator.execute_due(now=DUE_B)
        assert {r.outcome for r in results} == {ActuationOutcome.ACTUATED}
        assert len(results) == 2
        assert scheduler.get("AUR-ITEM-3").status is RouteStatus.NO_ACTION

    def test_a_disconnected_board_leaves_routes_unexecuted(self):
        board = FakeTransport(connected=False)
        scheduler, actuator, _ = rig(board)
        scheduler.schedule("AUR-ITEM-1", "A", T0)
        assert actuator.execute_due(now=DUE_A)[0].outcome is ActuationOutcome.FAILED
        assert scheduler.get("AUR-ITEM-1").status is RouteStatus.SCHEDULED


class TestReporting:
    def test_the_snapshot_carries_link_and_command_state(self):
        scheduler, actuator, _ = rig()
        scheduler.schedule("AUR-ITEM-1", "A", T0)
        actuator.execute_due(now=DUE_A)
        snapshot = actuator.snapshot()
        assert snapshot["arduino"]["connected"] is True
        assert snapshot["last_actuation"]["outcome"] == "ACTUATED"
        assert snapshot["servo"]["push_angle_deg"] == 90.0

    def test_the_snapshot_reports_verification_as_a_state_not_a_paragraph(
        self, tmp_path, monkeypatch
    ):
        """It used to be prose claiming no servo had ever been commanded, which
        stopped being true on 2026-08-26 and which nothing could check. The
        claim now has a shape, and an ACK still cannot produce it.

        Pointed at a temporary record: reading the repository's own would make
        this test's result depend on whether anybody has been to the bench."""
        from app.hardware import verification

        monkeypatch.setattr(verification, "RECORD", tmp_path / "none.json")
        _, actuator, _ = rig()
        claim = actuator.snapshot()["movement_verification"]
        assert set(claim["servos"]) == {"A", "B"}
        assert all(s["state"] == "VERIFICATION_UNAVAILABLE" for s in claim["servos"].values())
        assert claim["verified"] == []

    def test_the_controller_snapshot_explains_what_an_ack_means(self):
        assert "never inferred from here" in controller().snapshot()["note"]

    def test_the_configured_baudrate_matches_the_hardware(self):
        """115200 is what the physical board runs at; a mismatch is garbage."""
        assert config.load(environ={})["conveyor.arduino.baudrate"] == 115200


class TestCloselySpacedItems:
    """Two items due together, and a board that blocks for the whole stroke.

    The scheduler refuses to SCHEDULE a route whose moment has passed. Nothing
    refused to ACTUATE one, and every command blocks: three routes due together
    were commanded 1.2 s apart, and the last fired 45 cm of belt past its bin
    reporting ACTUATED.
    """

    class Clock:
        """A clock the board advances, the way a real 1.212 s stroke does."""

        def __init__(self, t=T0):
            self.t = t

        def __call__(self):
            return self.t

    def rig_with_a_slow_board(self, stroke_s=1.212, **env):
        clock = self.Clock()

        class SlowBoard(FakeTransport):
            def send(self, line):
                sent = super().send(line)
                if " MOVE " in line:
                    clock.t += stroke_s
                return sent

        board = SlowBoard(connected=True)
        configuration = cfg(**env)
        scheduler = RoutingScheduler(geometry=geometry(), cfg=configuration)
        ctl = ArduinoController(transport=board, cfg=configuration, clock=clock)
        actuator = ServoActuator(scheduler, controller=ctl, cfg=configuration, clock=clock)
        return scheduler, actuator, board, clock

    def test_the_first_of_two_still_actuates(self):
        scheduler, actuator, board, clock = self.rig_with_a_slow_board()
        scheduler.schedule("AUR-ITEM-1", "A", T0)
        scheduler.schedule("AUR-ITEM-2", "A", T0)
        clock.t = DUE_A
        results = actuator.execute_due(now=DUE_A)
        assert results[0].outcome is ActuationOutcome.ACTUATED
        assert board.movements[0][0] == "A"

    def test_the_second_is_refused_rather_than_fired_a_stroke_late(self):
        scheduler, actuator, board, clock = self.rig_with_a_slow_board()
        scheduler.schedule("AUR-ITEM-1", "A", T0)
        scheduler.schedule("AUR-ITEM-2", "A", T0)
        clock.t = DUE_A
        results = actuator.execute_due(now=DUE_A)
        assert results[1].outcome is ActuationOutcome.EXPIRED
        assert "Refusing to fire late" in results[1].reason
        assert len(board.movements) == 1

    def test_a_refused_route_is_never_marked_executed(self):
        """It was not sorted. A record saying otherwise is the worst outcome."""
        scheduler, actuator, _, clock = self.rig_with_a_slow_board()
        scheduler.schedule("AUR-ITEM-1", "A", T0)
        scheduler.schedule("AUR-ITEM-2", "A", T0)
        clock.t = DUE_A
        actuator.execute_due(now=DUE_A)
        assert scheduler.get("AUR-ITEM-2").status is not RouteStatus.EXECUTED

    def test_a_fast_board_gets_both_through(self):
        """The guard must not refuse work the machine can actually do."""
        scheduler, actuator, board, clock = self.rig_with_a_slow_board(stroke_s=0.01)
        scheduler.schedule("AUR-ITEM-1", "A", T0)
        scheduler.schedule("AUR-ITEM-2", "A", T0)
        clock.t = DUE_A
        results = actuator.execute_due(now=DUE_A)
        assert {r.outcome for r in results} == {ActuationOutcome.ACTUATED}
        assert len(board.movements) == 2

    def test_the_tolerance_is_configurable_because_a_bin_mouth_is_physical(self):
        scheduler, actuator, board, clock = self.rig_with_a_slow_board(
            AURUM_ROUTING_LATE_TOLERANCE_MS=5_000
        )
        scheduler.schedule("AUR-ITEM-1", "A", T0)
        scheduler.schedule("AUR-ITEM-2", "A", T0)
        clock.t = DUE_A
        results = actuator.execute_due(now=DUE_A)
        assert {r.outcome for r in results} == {ActuationOutcome.ACTUATED}

    def test_bin_c_is_never_expired_it_has_no_moment(self):
        scheduler, actuator, _, clock = self.rig_with_a_slow_board()
        route = scheduler.schedule("AUR-ITEM-3", "C", T0)
        assert actuator.actuate(route, now=T0 + 600).outcome is ActuationOutcome.NO_ACTION

    def test_a_latched_fault_is_reported_before_lateness(self):
        """BLOCKED keeps the item retryable; EXPIRED spends it. A latched
        machine is the operator's problem to fix, and the bigger fact."""
        scheduler, actuator, _, clock = self.rig_with_a_slow_board()
        route = scheduler.schedule("AUR-ITEM-1", "A", T0)
        actuator.fault.latch(FaultCode.ACK_TIMEOUT, "an earlier item")
        assert actuator.actuate(route, now=T0 + 600).outcome is ActuationOutcome.BLOCKED
