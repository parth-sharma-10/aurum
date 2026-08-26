"""Tests for the shared serial link.

The demonstration rig is one Arduino carrying the HX711 and both servos, so
one port carries two protocols at once. The property that matters is that
neither is ever read as the other:

    W,1,10432,-261605,OK        is a mass, never an acknowledgement
    AURUM/1 ACK CMD-1           is an acknowledgement, never a mass

and that a consumer waiting on one keeps the other's queue moving, because a
single-threaded pump is only correct if it pumps for everybody.

No real port is opened: a fake stands in for `serial.Serial`, which is the one
thing here that has to be mocked because it is the boundary.
"""

from __future__ import annotations

import time

from app.hardware.link import BoardLink
from app.hardware.transport import LinkState
from app.weight import RawSample


class FakeSerial:
    """A scripted `serial.Serial`. Returns one line per `readline()`."""

    def __init__(self, lines=(), fail_on_read=False, fail_on_write=False):
        self.lines = list(lines)
        self.written: list[bytes] = []
        self.fail_on_read = fail_on_read
        self.fail_on_write = fail_on_write
        self.closed = False

    def readline(self):
        if self.fail_on_read:
            raise OSError("device disconnected")
        # An empty bytestring is what pyserial returns on timeout.
        return self.lines.pop(0).encode() if self.lines else b""

    def write(self, data):
        if self.fail_on_write:
            raise OSError("device disconnected")
        self.written.append(data)
        return len(data)

    def flush(self):
        return None

    def reset_input_buffer(self):
        return None

    def close(self):
        self.closed = True


class AckingSerial(FakeSerial):
    """A board that answers a CFG with a matching ACK, the way the sketch does.

    The command id is minted inside `configure_servos`, so a scripted reply
    cannot know it. Echoing the id back is the only way to exercise the
    happy path without reaching for a real port.
    """

    def write(self, data):
        super().write(data)
        frame = data.decode().strip().split()
        if len(frame) >= 6 and frame[1] == "CFG":
            self.lines.append(f"AURUM/1 ACK {frame[5]}\n")
        return len(data)


def link(lines=(), **kwargs) -> BoardLink:
    """A connected link over a fake port.

    `_serial` is set directly rather than through `connect()`, because
    `connect()` exists to import pyserial and open a real device and that is
    precisely what a test must not do.
    """
    board = BoardLink("/dev/fake", baudrate=115200)
    board._serial = FakeSerial(lines, **kwargs)
    board._state = LinkState.CONNECTED
    return board


class TestFrameRouting:
    def test_a_weight_frame_reaches_the_weight_queue(self):
        board = link(["W,1,10432,-261605,OK\n"])
        sample = board.next_weight()
        assert isinstance(sample, RawSample)
        assert sample.raw_counts == -261605

    def test_a_protocol_reply_reaches_the_response_queue(self):
        board = link(["AURUM/1 ACK CMD-1\n"])
        assert board.next_response() == "AURUM/1 ACK CMD-1"

    def test_a_weight_frame_is_never_returned_as_a_response(self):
        board = link(["W,1,10432,-261605,OK\n"])
        assert board.next_response() is None

    def test_an_acknowledgement_is_never_returned_as_a_mass(self):
        board = link(["AURUM/1 ACK CMD-1\n"])
        assert board.next_weight() is None

    def test_reading_one_stream_files_the_other_for_later(self):
        """The reason this is single-threaded and still correct."""
        board = link(["AURUM/1 ACK CMD-1\n", "W,1,10432,-261605,OK\n"])
        assert board.next_weight().raw_counts == -261605
        assert board.next_response() == "AURUM/1 ACK CMD-1"

    def test_interleaved_traffic_keeps_both_streams_intact(self):
        board = link(
            [
                "W,1,1,-100,OK\n",
                "AURUM/1 ACK CMD-1\n",
                "W,1,2,-200,OK\n",
                "AURUM/1 PONG CMD-2\n",
                "W,1,3,-300,OK\n",
            ]
        )
        masses, replies = [], []
        for _ in range(5):
            board.pump()
        while (sample := board.next_weight()) is not None:
            masses.append(sample.raw_counts)
        while (reply := board.next_response()) is not None:
            replies.append(reply)
        assert masses == [-100, -200, -300]
        assert replies == ["AURUM/1 ACK CMD-1", "AURUM/1 PONG CMD-2"]

    def test_a_failed_weight_frame_is_not_a_mass(self):
        """`ERR` means the cell did not become ready. Zero is not the answer."""
        board = link(["W,1,10432,0,ERR\n"])
        assert board.next_weight() is None
        assert board.dropped == 1

    def test_boot_noise_is_dropped_and_counted(self):
        board = link(["garbage from a boot banner\n"])
        assert board.next_weight() is None
        assert board.dropped == 1

    def test_a_timeout_yields_nothing_without_error(self):
        board = link([])
        assert board.pump() is False
        assert board.next_weight() is None


class ReadForever(BaseException):
    """Raised by the cap below. Derives from BaseException on purpose.

    `pump()` turns any `Exception` into a degraded link and returns False,
    which would quietly convert a runaway loop into a passing test. This has
    to escape that handler to be worth anything.
    """


class EndlessWeightSerial:
    """A board that never goes idle: a weight frame on every read, forever.

    `FakeSerial` runs dry and then returns b"" — an idle port, which a real
    sketch streaming at its own pace never produces. The read cap is what
    turns the old unbounded loop into a failing test rather than a suite that
    hangs until someone kills it.
    """

    READ_CAP = 2_000_000

    def __init__(self):
        self.reads = 0
        self.written: list[bytes] = []

    def readline(self):
        self.reads += 1
        if self.reads > self.READ_CAP:
            raise ReadForever("the accessor never gave up; it read the stream forever")
        return b"W,1,10432,-261605,OK\n"

    def write(self, data):
        self.written.append(data)
        return len(data)

    def flush(self):
        return None

    def reset_input_buffer(self):
        return None

    def close(self):
        return None


def streaming_link(budget_s: float = 0.02) -> BoardLink:
    """A connected link over a board whose weight stream never pauses."""
    board = BoardLink("/dev/fake", baudrate=115200, timeout_s=budget_s)
    board._serial = EndlessWeightSerial()
    board._state = LinkState.CONNECTED
    return board


class TestABoardThatNeverGoesIdle:
    """The real-hardware condition: `pump()` always succeeds, so "port ran dry"
    is an exit that never comes. Waiting has to be bounded by a clock instead.
    """

    def test_waiting_for_a_reply_gives_up_rather_than_spinning_forever(self):
        # The sketch only speaks AURUM/1 in answer to a command, so a reply
        # that was never requested never arrives however long we read.
        assert streaming_link().next_response() is None

    def test_it_gives_up_within_a_bound_the_caller_can_plan_around(self):
        board = streaming_link(budget_s=0.02)
        start = time.monotonic()
        board.next_response()
        # Generous: the assertion is "bounded", not a benchmark of the budget.
        assert time.monotonic() - start < 1.0

    def test_a_reply_already_queued_is_returned_without_waiting_at_all(self):
        board = streaming_link()
        board._responses.append("AURUM/1 ACK CMD-1")
        assert board.next_response() == "AURUM/1 ACK CMD-1"
        assert board._serial.reads == 0

    def test_the_weight_stream_still_gets_through(self):
        # The bound must not cost us the frames the board IS sending.
        assert streaming_link().next_weight().raw_counts == -261605


class TestViews:
    def test_the_weight_view_reads_samples(self):
        board = link(["W,1,10432,-261605,OK\n"])
        assert board.weight_reader.read().raw_counts == -261605

    def test_the_command_view_reads_replies(self):
        board = link(["AURUM/1 ACK CMD-1\n"])
        assert board.transport.receive() == "AURUM/1 ACK CMD-1"

    def test_both_views_report_the_one_link_state(self):
        board = link()
        assert board.weight_reader.connected is True
        assert board.transport.state is LinkState.CONNECTED
        board.disconnect()
        assert board.weight_reader.connected is False
        assert board.transport.state is LinkState.DISCONNECTED

    def test_sending_writes_a_newline_terminated_ascii_frame(self):
        board = link()
        assert board.transport.send("AURUM/1 PING CMD-1") is True
        assert board._serial.written == [b"AURUM/1 PING CMD-1\n"]

    def test_servo_angles_are_pushed_as_a_config_frame(self):
        """Tuning the throw must not require reflashing the board."""
        board = link()
        board.configure_servos(0, 90, 700)
        frame = board._serial.written[0].decode().strip().split()
        assert frame[:5] == ["AURUM/1", "CFG", "0", "90", "700"]

    def test_the_config_frame_carries_a_command_id(self):
        """The sketch rejects a CFG with no id as BAD_FRAME, and should."""
        board = link()
        board.configure_servos(0, 90, 700)
        frame = board._serial.written[0].decode().strip().split()
        assert len(frame) == 6
        assert frame[5].startswith("CMD-")

    def test_configuring_consumes_its_own_acknowledgement(self):
        """Left queued, the CFG ACK becomes the next MOVE's to discard."""
        board = link()
        board._responses.append("AURUM/1 ACK CMD-WHATEVER")
        # The queued reply carries an id this call never asked about.
        board._serial.lines = []
        assert board.configure_servos(0, 90, 700) is False
        assert not board._responses

    def test_an_unacknowledged_configuration_reports_failure(self):
        board = link()
        assert board.configure_servos(0, 90, 700) is False
        assert "did not acknowledge" in board.last_error

    def test_an_acknowledged_configuration_reports_success(self):
        board = link()
        board._serial = AckingSerial()
        assert board.configure_servos(0, 90, 700) is True
        assert not board._responses

    def test_the_applied_angles_are_readable_from_the_snapshot(self):
        """Otherwise nothing outside this method can say which angles are live."""
        board = link()
        assert board.snapshot()["servo_config_applied"] is False
        board._serial = AckingSerial()
        board.configure_servos(0, 90, 700)
        assert board.snapshot()["servo_config"] == {
            "rest_deg": 0,
            "push_deg": 90,
            "hold_ms": 700,
        }

    def test_reconnecting_forgets_the_angles_the_board_no_longer_holds(self):
        board = link()
        board._serial = AckingSerial()
        board.configure_servos(0, 90, 700)
        board.disconnect()
        assert board.snapshot()["servo_config_applied"] is False


class TestConfiguringABoardThatKeepsTalking:
    """The bench condition, and the one that broke the backend.

    A real sketch streams weight frames the whole time it is answering, so the
    ACK is several reads deep rather than the first thing on the port. Polling
    at `timeout_s` and quitting on the first empty poll capped the wait at one
    read timeout however large a budget the caller passed.
    """

    def board(self, ack_after_reads: int = 20):
        class TalkativeSerial(FakeSerial):
            reads = 0
            ack: str | None = None

            def readline(self):
                # Real time has to pass: the budget being tested is wall-clock.
                time.sleep(0.005)
                self.reads += 1
                if self.ack is not None and self.reads >= ack_after_reads:
                    line, self.ack = self.ack, None
                    return line.encode()
                return b"W,1,10432,-261605,OK\n"

            def write(self, data):
                FakeSerial.write(self, data)
                frame = data.decode().strip().split()
                if len(frame) >= 6 and frame[1] == "CFG":
                    self.ack = f"AURUM/1 ACK {frame[5]}\n"
                return len(data)

        board = BoardLink("/dev/fake", timeout_s=0.02)
        board._serial = TalkativeSerial()
        board._state = LinkState.CONNECTED
        return board

    def test_a_late_acknowledgement_is_still_heard_inside_the_budget(self):
        # 20 reads at 5 ms is ~0.1 s: far past `timeout_s`, far inside the
        # budget. This returned False in 0.025 s before the fix.
        board = self.board()
        assert board.configure_servos(0, 90, 700, budget_s=1.0) is True

    def test_the_weight_frames_read_while_waiting_are_kept_not_dropped(self):
        board = self.board()
        board.configure_servos(0, 90, 700, budget_s=1.0)
        assert board.next_weight().raw_counts == -261605
        assert board.dropped == 0

    def test_it_still_gives_up_once_the_budget_really_is_spent(self):
        board = self.board(ack_after_reads=10_000)
        start = time.monotonic()
        assert board.configure_servos(0, 90, 700, budget_s=0.2) is False
        assert time.monotonic() - start < 1.0
        assert "did not acknowledge" in board.last_error

    def test_a_dry_port_is_not_waited_out_for_the_whole_budget(self):
        """`break` was there for a reason: a silent port must not burn 4 s."""
        board = link()
        start = time.monotonic()
        assert board.configure_servos(0, 90, 700, budget_s=5.0) is False
        assert time.monotonic() - start < 1.0


class TestFailure:
    def test_an_unconnected_link_refuses_to_send(self):
        board = BoardLink("/dev/fake")
        assert board.send("AURUM/1 PING CMD-1") is False

    def test_a_write_failure_degrades_the_link_rather_than_raising(self):
        board = link(fail_on_write=True)
        assert board.send("AURUM/1 PING CMD-1") is False
        assert board.state is LinkState.DEGRADED
        assert "write failed" in board.last_error

    def test_a_read_failure_degrades_the_link_rather_than_raising(self):
        board = link(fail_on_read=True)
        assert board.pump() is False
        assert board.state is LinkState.DEGRADED
        assert "read failed" in board.last_error

    def test_a_degraded_link_yields_no_mass(self):
        """A dead cable must read as an absent mass, never as a stale one."""
        board = link(fail_on_read=True)
        assert board.next_weight() is None

    def test_disconnecting_closes_the_port_and_drops_queued_frames(self):
        board = link(["W,1,1,-100,OK\n"])
        board.pump()
        port = board._serial
        board.disconnect()
        assert port.closed is True
        assert board.next_weight() is None

    def test_a_missing_pyserial_is_a_state_not_an_exception(self, monkeypatch):
        monkeypatch.setitem(__import__("sys").modules, "serial", None)
        board = BoardLink("/dev/fake")
        assert board.connect() is LinkState.DISCONNECTED
        assert "pyserial" in board.last_error

    def test_the_snapshot_reports_the_link_without_inventing_a_reading(self):
        board = link()
        snapshot = board.snapshot()
        assert snapshot["connected"] is True
        assert snapshot["baudrate"] == 115200
        assert snapshot["queued_weight_frames"] == 0
