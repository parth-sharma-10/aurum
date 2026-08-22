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
        assert board._serial.written == [b"AURUM/1 CFG 0 90 700\n"]


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
