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

import os
import time

import pytest

from app.hardware import link as link_module
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

    def test_a_quiet_port_is_waited_on_for_the_whole_budget(self):
        """Changed deliberately. A quiet port is not a dead one, and `pump()`
        cannot tell them apart — it returns False both when a read times out
        between frames and when the port has gone. Giving up on the first
        empty read is what capped the wait at one read timeout, and the first
        CFG after a port open was measured at 1.310 s on the bench.

        The cost is that a genuinely silent board burns the acknowledgement
        budget, which is what a budget is for. A real `readline` blocks for
        `timeout_s` each time, so this is paced, not spun."""
        board = link()
        start = time.monotonic()
        assert board.configure_servos(0, 90, 700, budget_s=0.4) is False
        assert 0.4 <= time.monotonic() - start < 1.5

    def test_a_port_that_dies_mid_wait_gives_up_at_once(self):
        """The distinction that replaced it: DEGRADED is terminal, quiet is not."""
        board = link(fail_on_read=True)
        start = time.monotonic()
        assert board.configure_servos(0, 90, 700, budget_s=5.0) is False
        assert time.monotonic() - start < 1.0
        assert board.state is LinkState.DEGRADED


class TestTheConfigureServosContract:
    """The six cases the CFG path has to get right, after two hardware bugs
    in it. Case 2 is the one that actually broke the backend."""

    def serial_that_acks_after(self, quiet_reads: int, delay_s: float = 0.0):
        """A board that streams weight, then answers the CFG N reads later."""

        class Delayed(FakeSerial):
            reads = 0
            ack: str | None = None

            def readline(self):
                if delay_s:
                    time.sleep(delay_s)
                self.reads += 1
                if self.ack is not None and self.reads >= quiet_reads:
                    line, self.ack = self.ack, None
                    return line.encode()
                return b"W,1,10432,-261605,OK\n"

            def write(self, data):
                FakeSerial.write(self, data)
                frame = data.decode().strip().split()
                if len(frame) >= 6 and frame[1] == "CFG":
                    self.ack = f"AURUM/1 ACK {frame[5]}\n"
                return len(data)

        return Delayed()

    def board(self, serial, timeout_s: float = 0.02) -> BoardLink:
        made = BoardLink("/dev/fake", timeout_s=timeout_s)
        made._serial = serial
        made._state = LinkState.CONNECTED
        return made

    def test_1_a_normal_cfg_is_acknowledged(self):
        board = self.board(self.serial_that_acks_after(1))
        assert board.configure_servos(0, 90, 700, budget_s=1.0) is True
        assert board.servo_config == (0, 90, 700)

    def test_2_a_slow_first_cfg_is_still_acknowledged(self):
        """The bench case. Measured 1.310 s for the first CFG after a port
        open against a 1.0 s `timeout_s`; every later one took 0.408 s. The
        backend only ever does the first."""
        board = self.board(self.serial_that_acks_after(30, delay_s=0.005))
        assert board.configure_servos(0, 90, 700, budget_s=2.0) is True

    def test_3_a_board_that_never_answers_times_out(self):
        """Streaming happily, deaf to AURUM/1 — the wrong sketch, or none."""

        class Deaf(FakeSerial):
            def readline(self):
                return b"W,1,10432,-261605,OK\n"

        board = self.board(Deaf())
        start = time.monotonic()
        assert board.configure_servos(0, 90, 700, budget_s=0.3) is False
        assert "did not acknowledge" in board.last_error
        assert time.monotonic() - start < 1.5

    def test_4_a_dead_port_fails_without_burning_the_budget(self):
        board = self.board(FakeSerial(fail_on_read=True))
        start = time.monotonic()
        assert board.configure_servos(0, 90, 700, budget_s=5.0) is False
        assert time.monotonic() - start < 1.0

    def test_5_a_malformed_reply_is_never_read_as_an_acknowledgement(self):
        class Garbage(FakeSerial):
            def write(self, data):
                FakeSerial.write(self, data)
                # Right shape, wrong protocol version, and a bare ACK with no id.
                self.lines += ["AURUM/2 ACK CMD-1\n", "AURUM/1 ACK\n", "ACK\n"]
                return len(data)

        board = self.board(Garbage())
        assert board.configure_servos(0, 90, 700, budget_s=0.3) is False
        assert board.servo_config is None

    def test_6_an_ack_arriving_after_many_weight_frames_still_counts(self):
        board = self.board(self.serial_that_acks_after(50))
        assert board.configure_servos(0, 90, 700, budget_s=1.0) is True
        # The weight frames read while waiting are kept, not discarded.
        assert board.next_weight().raw_counts == -261605

    def test_an_unwritable_port_fails_before_it_waits_at_all(self):
        board = self.board(FakeSerial(fail_on_write=True))
        start = time.monotonic()
        assert board.configure_servos(0, 90, 700, budget_s=5.0) is False
        assert time.monotonic() - start < 0.2


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


class TestReopeningADroppedLink:
    """The bench board re-enumerates on USB every few minutes, leaving this
    process holding a descriptor that will never yield another byte. Reopening
    is the only way back."""

    def test_a_healthy_link_is_never_reopened(self):
        """Reopening resets the board. Doing that to a working link would park
        the paddles and drop the weight stream for no reason."""
        board = link(["W,1,1,-100,OK\n"])
        port = board._serial
        assert board.reconnect() is True
        assert board._serial is port
        assert port.closed is False

    def test_a_degraded_link_is_closed_before_it_is_reopened(self):
        board = link(fail_on_read=True)
        board.pump()
        assert board.state is LinkState.DEGRADED
        port = board._serial
        board.reconnect()  # connect() needs a real device, so it fails
        assert port.closed is True, "the dead descriptor must not be leaked"

    def test_reopening_a_port_that_is_gone_reports_failure(self):
        board = link(fail_on_read=True)
        board.pump()
        assert board.reconnect() is False
        assert board.connected is False

    def test_the_angles_are_forgotten_when_the_board_drops(self):
        """The board reboots when it re-enumerates, so whatever it acknowledged
        before is gone. Reporting the old ones would be a stale claim."""
        board = link()
        board._serial = AckingSerial()
        board.configure_servos(0, 90, 700)
        assert board.snapshot()["servo_config_applied"] is True
        board._serial = FakeSerial(fail_on_read=True)
        board.pump()
        board.reconnect()
        assert board.snapshot()["servo_config_applied"] is False


class TestOnePortOneOwner:
    """macOS `cu.*` device nodes do not lock.

    A second process opens the same port successfully and the two then split
    the board's replies between them - PING, CFG and MOVE all time out against
    a port that looks perfectly healthy. That is not hypothetical: it is what a
    failed bench run on 2026-08-26 turned out to be, after the firmware, the
    servo and the wiring had each been suspected first.

    These tests use a temporary lock directory so they never touch the real
    one, and never open a device.
    """

    @pytest.fixture(autouse=True)
    def _isolated_lock_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(link_module, "LOCK_DIR", str(tmp_path))

    def test_a_second_link_on_the_same_port_is_refused(self):
        first = BoardLink("/dev/cu.fake")
        assert first._acquire_lock() is None

        second = BoardLink("/dev/cu.fake")
        owner = second._acquire_lock()

        assert owner is not None
        assert str(os.getpid()) in owner

    def test_the_refusal_says_what_is_wrong_rather_than_could_not_open(self):
        """ "Could not open" would send whoever reads it to the cable. The port
        opens fine; the problem is that somebody else is already reading it."""
        first = BoardLink("/dev/cu.fake")
        first._acquire_lock()

        second = BoardLink("/dev/cu.fake")
        assert second.connect() is LinkState.DISCONNECTED
        assert "already owned by" in second.last_error
        assert "split" in second.last_error

    def test_disconnecting_gives_the_port_back(self):
        first = BoardLink("/dev/cu.fake")
        first._acquire_lock()
        first.disconnect()

        assert BoardLink("/dev/cu.fake")._acquire_lock() is None

    def test_a_failed_open_does_not_keep_the_lock(self):
        """Otherwise a board that was unplugged and plugged back in could never
        be reconnected: the process would be refused by its own stale lock."""
        board = BoardLink("/dev/cu.definitely-not-a-device")
        assert board.connect() is LinkState.DISCONNECTED
        assert "could not open" in board.last_error

        assert BoardLink("/dev/cu.definitely-not-a-device")._acquire_lock() is None

    def test_two_different_ports_do_not_exclude_each_other(self):
        assert BoardLink("/dev/cu.boardA")._acquire_lock() is None
        assert BoardLink("/dev/cu.boardB")._acquire_lock() is None

    def test_releasing_a_lock_that_was_never_taken_is_harmless(self):
        BoardLink("/dev/cu.fake")._release_lock()

    def test_an_unwritable_lock_directory_does_not_block_the_machine(self, monkeypatch):
        """A lock file that cannot be OPENED says nothing about who holds the
        port. Refusing on it would brick a working rig over a /tmp permission."""
        monkeypatch.setattr(link_module, "LOCK_DIR", "/proc/nonexistent-for-aurum")
        assert BoardLink("/dev/cu.fake")._acquire_lock() is None
