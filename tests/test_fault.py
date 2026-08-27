"""Tests for the latched hardware fault and the safety ladder above it.

The property under test: after something physical goes wrong, no servo moves
until a human resets it. Not "until the link comes back", not "until the next
item" — a command that went unacknowledged may have left a paddle half out, and
the machine does not know.
"""

from __future__ import annotations

import pytest

from app import config
from app.hardware.arduino import ArduinoController, CommandState
from app.hardware.fault import FaultCode, HardwareFault
from app.hardware.servos import ActuationOutcome, ServoActuator
from app.hardware.transport import FakeTransport, SimulatedTransport
from app.routing.geometry import Geometry, RoutingMode
from app.routing.scheduler import RoutingScheduler

T0 = 1_000.0

#: When the Servo A route below actually fires: 60 cm at 10 cm/s, less the
#: 150 ms actuation delay. The bridges here used to run their clock at
#: T0 + 100 as a shorthand for "past due", which is now 94 seconds late and
#: refused as EXPIRED - by then the item is nine metres down a belt.
DUE_A = T0 + 5.85


def cfg(**environ):
    environ.setdefault("AURUM_ARDUINO_ENABLED", "true")
    return config.load(environ=environ)


def controller(transport=None, **environ) -> ArduinoController:
    return ArduinoController(
        transport=transport if transport is not None else FakeTransport(connected=True),
        cfg=cfg(**environ),
    )


class TestTheLatch:
    def test_a_fresh_machine_has_no_fault(self):
        fault = HardwareFault()
        assert fault.active is False
        assert fault.refusal() is None

    def test_latching_stops_actuation(self):
        fault = HardwareFault()
        fault.latch(FaultCode.ACK_TIMEOUT, "no ACK")
        assert fault.active is True
        assert "ACK_TIMEOUT" in fault.refusal()

    def test_the_first_fault_is_the_one_that_explains_the_rest(self):
        """A consequence must not overwrite the cause."""
        fault = HardwareFault()
        fault.latch(FaultCode.ACK_TIMEOUT, "no ACK")
        fault.latch(FaultCode.ARDUINO_DISCONNECTED, "cable out")
        assert fault.current.code is FaultCode.ACK_TIMEOUT
        assert len(fault.history) == 2

    def test_a_reset_clears_it_and_is_recorded(self):
        fault = HardwareFault()
        fault.latch(FaultCode.WRITE_FAILED, "port gone")
        cleared = fault.reset(by="operator")
        assert fault.active is False
        assert cleared.code is FaultCode.WRITE_FAILED
        assert fault.resets[-1]["by"] == "operator"

    def test_resetting_a_clean_machine_records_nothing(self):
        fault = HardwareFault()
        assert fault.reset() is None
        assert fault.resets == []

    def test_the_history_survives_the_reset(self):
        """ "Why did nothing move for six items" must have an answer afterwards."""
        fault = HardwareFault()
        fault.latch(FaultCode.ACK_TIMEOUT, "no ACK")
        fault.reset()
        assert fault.snapshot()["faults"] == 1
        assert fault.snapshot()["history"][0]["code"] == "ACK_TIMEOUT"


class TestWhatLatchesAndWhatDoesNot:
    def test_an_ack_timeout_latches(self):
        ctl = controller(
            FakeTransport(connected=True, silent=True), AURUM_ARDUINO_ACK_TIMEOUT_MS="20"
        )
        assert ctl.move("A", "AUR-ITEM-1").state is CommandState.TIMED_OUT
        assert ctl.fault.current.code is FaultCode.ACK_TIMEOUT

    def test_a_write_failure_latches(self):
        """A write that fails may have put part of a frame on the wire, so what
        the board did with it is unknown. That still latches."""

        class WriteFails(FakeTransport):
            def send(self, line):
                self.last_error = "the write failed"
                return False

        ctl = controller(WriteFails(connected=True))
        assert ctl.move("A", "AUR-ITEM-1").error_code == "WRITE_FAILED"
        assert ctl.fault.current.code is FaultCode.WRITE_FAILED

    def test_a_link_that_is_already_down_does_not_latch(self):
        """Changed 2026-08-26. This case returns before `build_frame`, so
        nothing was written and no paddle was asked to move — the physical
        state is known, and known states do not latch. Latching it meant a
        board that dropped off USB while idle stopped the machine until a human
        clicked, which on a bench where the link drops every few minutes is a
        halted demonstration rather than a safety measure."""
        board = FakeTransport(connected=True)
        ctl = controller(board)
        board.unplug()
        assert ctl.move("A", "AUR-ITEM-1").error_code == "NOT_CONNECTED"
        assert board.sent == []
        assert ctl.fault.active is False

    def test_a_board_error_latches(self):
        ctl = controller(FakeTransport(connected=True, fail_with="JAMMED"))
        assert ctl.move("A", "AUR-ITEM-1").error_code == "JAMMED"
        assert ctl.fault.current.code is FaultCode.BOARD_ERROR

    def test_bin_c_does_not_latch(self):
        """C sends no frame and is the normal outcome for most items."""
        ctl = controller()
        assert ctl.move("C", "AUR-ITEM-1").error_code == "BAD_TARGET"
        assert ctl.fault.active is False

    def test_actuation_being_disabled_does_not_latch(self):
        """It is the shipped state. Latching would fault the machine on item one."""
        ctl = ArduinoController(
            transport=FakeTransport(connected=True),
            cfg=config.load(environ={"AURUM_ARDUINO_ENABLED": "false"}),
        )
        assert ctl.move("A", "AUR-ITEM-1").error_code == "ACTUATION_DISABLED"
        assert ctl.fault.active is False

    def test_a_successful_command_does_not_latch(self):
        ctl = controller()
        assert ctl.move("A", "AUR-ITEM-1").state is CommandState.ACKED
        assert ctl.fault.active is False


class TestTheLatchBlocksTheNextItem:
    def test_a_second_item_is_refused_after_a_timeout(self):
        ctl = controller(
            FakeTransport(connected=True, silent=True), AURUM_ARDUINO_ACK_TIMEOUT_MS="20"
        )
        ctl.move("A", "AUR-ITEM-1")
        second = ctl.move("B", "AUR-ITEM-2")
        assert second.error_code == "HARDWARE_FAULT"
        assert second.state is CommandState.FAILED

    def test_no_frame_is_written_while_the_fault_is_latched(self):
        board = FakeTransport(connected=True, silent=True)
        ctl = controller(board, AURUM_ARDUINO_ACK_TIMEOUT_MS="20")
        ctl.move("A", "AUR-ITEM-1")
        written = len(board.sent)
        ctl.move("B", "AUR-ITEM-2")
        assert len(board.sent) == written

    def test_after_a_reset_the_next_item_moves(self):
        board = FakeTransport(connected=True, silent=True)
        ctl = controller(board, AURUM_ARDUINO_ACK_TIMEOUT_MS="20")
        ctl.move("A", "AUR-ITEM-1")
        board.silent = False
        ctl.fault.reset(by="test")
        assert ctl.move("B", "AUR-ITEM-2").state is CommandState.ACKED


class TestServoAngleValidation:
    def test_the_shipped_angles_are_valid(self):
        assert controller().servo_angle_problem() is None

    @pytest.mark.parametrize(
        ("rest", "push"),
        [("0", "181"), ("200", "90"), ("90", "90")],
        ids=["push out of range", "rest out of range", "no travel"],
    )
    def test_an_impossible_angle_pair_is_refused(self, rest, push):
        ctl = controller(AURUM_SERVO_REST_ANGLE_DEG=rest, AURUM_SERVO_PUSH_ANGLE_DEG=push)
        assert ctl.servo_angle_problem() is not None

    def test_a_negative_angle_is_rejected_by_the_config_reader_first(self):
        """Two layers, and the outer one catches it before this file is reached."""
        with pytest.raises(config.ConfigError):
            cfg(AURUM_SERVO_REST_ANGLE_DEG="-5")

    def test_an_invalid_angle_pair_latches_rather_than_being_written(self):
        board = FakeTransport(connected=True)
        ctl = controller(board, AURUM_SERVO_PUSH_ANGLE_DEG="200")
        command = ctl.move("A", "AUR-ITEM-1")
        assert command.error_code == "INVALID_SERVO_STATE"
        assert board.sent == []
        assert ctl.fault.current.code is FaultCode.INVALID_SERVO_STATE


class TestSimulationSendsNothingToAPort:
    """HARDWARE_MODE=SIMULATION must exercise the protocol and no wire."""

    def simulated(self, **environ):
        environ.setdefault("AURUM_SIMULATION", "true")
        environ.setdefault("AURUM_ARDUINO_ENABLED", "true")
        environ.setdefault("AURUM_ARDUINO_PORT", "/dev/definitely-not-a-real-port")
        return ArduinoController(cfg=config.load(environ=environ))

    def test_a_configured_port_is_ignored_in_simulation(self):
        ctl = self.simulated()
        assert isinstance(ctl.transport, SimulatedTransport)
        assert ctl.transport.name == "simulated"

    def test_the_protocol_still_runs_and_the_board_still_acknowledges(self):
        ctl = self.simulated()
        ctl.connect()
        assert ctl.move("A", "AUR-ITEM-1").state is CommandState.ACKED

    def test_the_snapshot_says_which_mode_it_is_in(self):
        assert self.simulated().snapshot()["hardware_mode"] == "SIMULATION"

    def test_a_physical_machine_says_so(self):
        assert controller().snapshot()["hardware_mode"] == "PHYSICAL"


def geometry() -> Geometry:
    return Geometry(
        mode=RoutingMode.SIMULATED,
        belt_speed_cm_s=10.0,
        camera_to_servo_a_cm=60.0,
        camera_to_servo_b_cm=90.0,
        servo_actuation_delay_ms=150.0,
    )


class TestTheServoBridgeRespectsTheFault:
    def bridge(self, board=None):
        settings = cfg()
        ctl = ArduinoController(
            transport=board if board is not None else FakeTransport(connected=True), cfg=settings
        )
        queue = RoutingScheduler(geometry=geometry(), cfg=settings)
        return queue, ServoActuator(queue, controller=ctl, cfg=settings, clock=lambda: DUE_A)

    def test_a_due_route_actuates_normally(self):
        queue, bridge = self.bridge()
        route = queue.schedule("AUR-ITEM-1", "A", T0)
        assert bridge.actuate(route).outcome is ActuationOutcome.ACTUATED

    def test_a_latched_fault_blocks_the_route_without_sending(self):
        board = FakeTransport(connected=True)
        queue, bridge = self.bridge(board)
        bridge.fault.latch(FaultCode.ACK_TIMEOUT, "an earlier item went unacknowledged")
        route = queue.schedule("AUR-ITEM-1", "A", T0)
        result = bridge.actuate(route)
        assert result.outcome is ActuationOutcome.BLOCKED
        assert board.sent == []

    def test_a_blocked_route_can_still_be_actuated_after_a_reset(self):
        """Blocked is not attempted: nothing reached the board, so nothing is spent."""
        queue, bridge = self.bridge()
        bridge.fault.latch(FaultCode.ACK_TIMEOUT, "earlier item")
        route = queue.schedule("AUR-ITEM-1", "A", T0)
        bridge.actuate(route)
        bridge.fault.reset(by="test")
        assert bridge.actuate(route).outcome is ActuationOutcome.ACTUATED

    def test_a_route_that_reached_the_board_is_never_retried(self):
        board = FakeTransport(connected=True, silent=True)
        settings = cfg(AURUM_ARDUINO_ACK_TIMEOUT_MS="20")
        ctl = ArduinoController(transport=board, cfg=settings)
        queue = RoutingScheduler(geometry=geometry(), cfg=settings)
        bridge = ServoActuator(queue, controller=ctl, cfg=settings, clock=lambda: DUE_A)
        route = queue.schedule("AUR-ITEM-1", "A", T0)
        assert bridge.actuate(route).outcome is ActuationOutcome.FAILED
        ctl.fault.reset(by="test")
        assert bridge.actuate(route).outcome is ActuationOutcome.SKIPPED

    def test_the_bridge_and_the_board_share_one_latch(self):
        queue, bridge = self.bridge()
        assert bridge.fault is bridge.controller.fault

    def test_the_snapshot_carries_the_fault(self):
        _, bridge = self.bridge()
        bridge.fault.latch(FaultCode.ENCODER_FAILURE, "the encoder went quiet")
        assert bridge.snapshot()["fault"]["code"] == "ENCODER_FAILURE"
