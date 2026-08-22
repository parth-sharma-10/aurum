"""Tests for the Arduino command layer.

This is the layer that moves physical objects, so the properties under test are
the ones whose failure has a physical consequence:

**One item, one movement.** A paddle that swings twice for one component hits
whatever is behind it. Duplicate suppression is tested per item and per command
id, and a timeout is tested to confirm it does NOT retry.

**An ACK is not proof of movement.** Nothing here asserts that a servo moved,
because nothing in software can know that. What is asserted is that a command
without an acknowledgement never reports success.

**Bin C is not a target.** There is no Servo C, and a C reaching this layer is
an error rather than a third kind of movement.

No serial port is opened. `FakeTransport` answers the real protocol so the
layer above is exercised rather than imitated.
"""

from __future__ import annotations

from app import config
from app.hardware import (
    PROTOCOL,
    ArduinoController,
    CommandState,
    FakeTransport,
    LinkState,
    build_frame,
    new_command_id,
    parse_response,
)

ITEM = "AUR-ITEM-0A1B2C3D"


def controller(transport=None, **environ) -> ArduinoController:
    """A controller with actuation ON, over a board that answers."""
    cfg = config.load(environ={"AURUM_ARDUINO_ENABLED": "true", **environ})
    return ArduinoController(
        transport=FakeTransport(connected=True) if transport is None else transport,
        cfg=cfg,
    )


class TestFrames:
    def test_a_move_frame_names_protocol_target_item_and_command(self):
        frame = build_frame("A", ITEM, "CMD-1234ABCD")
        assert frame == f"{PROTOCOL} MOVE A {ITEM} CMD-1234ABCD"

    def test_command_ids_are_unique(self):
        assert len({new_command_id() for _ in range(200)}) == 200

    def test_an_ack_is_parsed(self):
        response = parse_response(f"{PROTOCOL} ACK CMD-1")
        assert response.verb == "ACK"
        assert response.command_id == "CMD-1"
        assert response.duplicate is False

    def test_a_duplicate_ack_is_flagged(self):
        assert parse_response(f"{PROTOCOL} ACK CMD-1 DUP").duplicate is True

    def test_an_error_carries_its_code(self):
        response = parse_response(f"{PROTOCOL} ERR CMD-1 BAD_TARGET")
        assert (response.verb, response.code) == ("ERR", "BAD_TARGET")

    def test_a_weight_frame_is_not_a_response(self):
        """The two protocols share one link; neither may be read as the other."""
        assert parse_response("W,1,10432,-261605,OK") is None

    def test_noise_is_not_a_response(self):
        for line in ("", "   ", "ACK", "AURUM/2 ACK CMD-1", None, 17, b"AURUM/1 ACK CMD-1"):
            assert parse_response(line) is None


class TestAcknowledgement:
    def test_an_acknowledged_move_reports_acked(self):
        command = controller().move("A", ITEM)
        assert command.state is CommandState.ACKED
        assert command.acknowledged is True

    def test_the_frame_and_the_response_are_both_recorded(self):
        command = controller().move("B", ITEM)
        assert command.raw_frame == build_frame("B", ITEM, command.command_id)
        assert command.raw_response.startswith(f"{PROTOCOL} ACK")

    def test_servo_a_and_servo_b_are_distinct_targets(self):
        transport = FakeTransport(connected=True)
        board = controller(transport)
        board.move("A", "AUR-ITEM-1")
        board.move("B", "AUR-ITEM-2")
        assert [target for target, _ in transport.movements] == ["A", "B"]

    def test_the_record_says_an_ack_is_not_proof_of_movement(self):
        """The one claim this layer must never make."""
        record = controller().move("A", ITEM).as_dict()
        assert "not evidence that a servo physically moved" in record["ack_meaning"]

    def test_a_ping_round_trips(self):
        assert controller().ping() is True

    def test_a_ping_on_a_silent_board_is_false(self):
        assert controller(FakeTransport(connected=True, silent=True)).ping() is False


class TestBinC:
    def test_bin_c_is_refused_as_a_target(self):
        command = controller().move("C", ITEM)
        assert command.state is CommandState.FAILED
        assert command.error_code == "BAD_TARGET"

    def test_bin_c_sends_nothing_to_the_board(self):
        transport = FakeTransport(connected=True)
        controller(transport).move("C", ITEM)
        assert transport.sent == []
        assert transport.movements == []

    def test_an_unknown_target_is_refused(self):
        assert controller().move("D", ITEM).error_code == "BAD_TARGET"


class TestDuplicateProtection:
    def test_a_second_command_for_one_item_is_suppressed(self):
        board = controller()
        board.move("A", ITEM)
        second = board.move("A", ITEM)
        assert second.state is CommandState.SUPPRESSED

    def test_the_suppressed_command_names_the_one_that_won(self):
        board = controller()
        first = board.move("A", ITEM)
        assert board.move("A", ITEM).duplicate_of == first.command_id

    def test_one_item_moves_the_paddle_once(self):
        transport = FakeTransport(connected=True)
        board = controller(transport)
        for _ in range(5):
            board.move("A", ITEM)
        assert len(transport.movements) == 1

    def test_a_different_bin_for_the_same_item_is_still_suppressed(self):
        """Re-deciding an item does not earn it a second physical movement."""
        board = controller()
        board.move("A", ITEM)
        assert board.move("B", ITEM).state is CommandState.SUPPRESSED

    def test_resending_a_settled_command_id_is_suppressed(self):
        board = controller()
        first = board.move("A", ITEM)
        assert board.move("A", "AUR-ITEM-OTHER", first.command_id).state is CommandState.SUPPRESSED

    def test_the_board_acknowledges_a_replayed_id_without_moving(self):
        """Mirrors the sketch's recent-id list: an ACK lost on the wire is safe."""
        transport = FakeTransport(connected=True)
        transport.send(build_frame("A", ITEM, "CMD-REPLAY"))
        transport.send(build_frame("A", ITEM, "CMD-REPLAY"))
        assert len(transport.movements) == 1
        assert transport.receive() == f"{PROTOCOL} ACK CMD-REPLAY"
        assert transport.receive() == f"{PROTOCOL} ACK CMD-REPLAY DUP"

    def test_two_different_items_each_get_a_movement(self):
        transport = FakeTransport(connected=True)
        board = controller(transport)
        board.move("A", "AUR-ITEM-1")
        board.move("A", "AUR-ITEM-2")
        assert len(transport.movements) == 2


class TestFailure:
    def test_a_silent_board_times_out(self):
        command = controller(
            FakeTransport(connected=True, silent=True), AURUM_ARDUINO_ACK_TIMEOUT_MS="20"
        ).move("A", ITEM)
        assert command.state is CommandState.TIMED_OUT
        assert command.acknowledged is False

    def test_a_timeout_is_never_retried(self):
        """A blind resend is how one item gets moved twice."""
        transport = FakeTransport(connected=True, silent=True)
        controller(transport, AURUM_ARDUINO_ACK_TIMEOUT_MS="20").move("A", ITEM)
        assert len(transport.sent) == 1

    def test_a_board_error_is_reported_as_failed(self):
        command = controller(FakeTransport(connected=True, fail_with="SERVO_STALL")).move("A", ITEM)
        assert command.state is CommandState.FAILED
        assert command.error_code == "SERVO_STALL"

    def test_a_disconnected_board_never_reports_success(self):
        command = controller(FakeTransport(connected=False)).move("A", ITEM)
        assert command.state is CommandState.FAILED
        assert command.error_code == "NOT_CONNECTED"
        assert command.acknowledged is False

    def test_a_cable_pulled_mid_run_fails_the_next_command(self):
        transport = FakeTransport(connected=True)
        board = controller(transport)
        board.move("A", "AUR-ITEM-1")
        transport.unplug()
        second = board.move("A", "AUR-ITEM-2")
        assert second.state is CommandState.FAILED
        assert "cable unplugged" in second.reason

    def test_actuation_disabled_refuses_and_says_how_to_enable_it(self):
        cfg = config.load(environ={"AURUM_ARDUINO_ENABLED": "false"})
        board = ArduinoController(transport=FakeTransport(connected=True), cfg=cfg)
        command = board.move("A", ITEM)
        assert command.error_code == "ACTUATION_DISABLED"
        assert "conveyor.arduino.enabled" in command.reason

    def test_actuation_disabled_sends_nothing(self):
        transport = FakeTransport(connected=True)
        cfg = config.load(environ={"AURUM_ARDUINO_ENABLED": "false"})
        ArduinoController(transport=transport, cfg=cfg).move("A", ITEM)
        assert transport.sent == []

    def test_a_reply_for_another_command_is_not_taken_as_this_one(self):
        transport = FakeTransport(connected=True, silent=True)
        transport.feed(f"{PROTOCOL} ACK CMD-SOMEONE-ELSE")
        command = controller(transport, AURUM_ARDUINO_ACK_TIMEOUT_MS="20").move("A", ITEM)
        assert command.state is CommandState.TIMED_OUT

    def test_weight_frames_on_the_shared_link_do_not_settle_a_command(self):
        transport = FakeTransport(connected=True, silent=True)
        for _ in range(5):
            transport.feed("W,1,10432,-261605,OK")
        command = controller(transport, AURUM_ARDUINO_ACK_TIMEOUT_MS="20").move("A", ITEM)
        assert command.state is CommandState.TIMED_OUT


class TestLink:
    def test_a_fresh_controller_is_disconnected(self):
        assert controller(FakeTransport(connected=False)).connected is False

    def test_connect_reports_the_state(self):
        assert controller(FakeTransport(connected=False)).connect() is LinkState.CONNECTED

    def test_no_configured_port_yields_a_fake_transport_that_cannot_actuate(self):
        """A missing port is a normal state: run, and refuse to actuate."""
        board = ArduinoController(cfg=config.load(environ={"AURUM_ARDUINO_ENABLED": "true"}))
        assert board.connected is False
        assert board.move("A", ITEM).error_code == "NOT_CONNECTED"

    def test_lookup_by_item_finds_the_command(self):
        board = controller()
        command = board.move("A", ITEM)
        assert board.for_item(ITEM).command_id == command.command_id

    def test_the_snapshot_reports_the_link_and_the_last_command(self):
        board = controller()
        board.move("A", ITEM)
        snapshot = board.snapshot()
        assert snapshot["connected"] is True
        assert snapshot["actuation_enabled"] is True
        assert snapshot["last_command"]["item_id"] == ITEM
        assert snapshot["last_command"]["servo"] == "SERVO_A"
