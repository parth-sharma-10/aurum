"""Tests for the bench validation harness.

The harness exists to be run against a real board, so what is tested here is
the only part that is not the board: that an answering board reads as PASS, a
silent one reads as FAIL, and that neither an ACK nor a missing port is ever
reported as a verified paddle movement.
"""

from __future__ import annotations

from app import config as config_module
from app.hardware.link import BoardLink
from app.hardware.transport import LinkState
from scripts import bench_check

CFG = config_module.load()


class AnsweringSerial:
    """A board that streams weight and answers PING and CFG, as the sketch does."""

    def __init__(self):
        self.written: list[bytes] = []
        self._replies: list[str] = []

    def readline(self):
        if self._replies:
            return self._replies.pop(0).encode()
        return b"W,1,10432,-261605,OK\n"

    def write(self, data):
        self.written.append(data)
        frame = data.decode().strip().split()
        if len(frame) >= 3 and frame[1] == "PING":
            self._replies.append(f"AURUM/1 PONG {frame[2]}\n")
        elif len(frame) >= 6 and frame[1] == "CFG":
            self._replies.append(f"AURUM/1 ACK {frame[5]}\n")
        return len(data)

    def flush(self):
        return None

    def reset_input_buffer(self):
        return None

    def close(self):
        return None


def board(serial=None, timeout_s: float = 0.01) -> BoardLink:
    link = BoardLink("/dev/fake", timeout_s=timeout_s)
    link._serial = AnsweringSerial() if serial is None else serial
    link._state = LinkState.CONNECTED
    return link


class SilentSerial(AnsweringSerial):
    """Open, powered, and saying nothing — a wrong port, or a board not flashed."""

    def readline(self):
        return b""

    def write(self, data):
        self.written.append(data)
        return len(data)


class TestChecks:
    def test_an_answering_board_passes_ping(self):
        assert bench_check.check_ping(CFG, board())[0] == "PASS"

    def test_the_configured_angles_are_reported_as_applied(self):
        verdict, detail = bench_check.check_config(CFG, board())
        assert verdict == "PASS"
        assert "rest_deg" in detail

    def test_a_silent_board_fails_the_configuration_check(self):
        verdict, detail = bench_check.check_config(CFG, board(SilentSerial()))
        assert verdict == "FAIL"
        assert "did not acknowledge" in detail

    def test_a_streaming_cell_passes(self):
        verdict, detail = bench_check.check_weight(board())
        assert verdict == "PASS"
        assert "5/5 frames" in detail

    def test_a_cell_that_sends_nothing_is_a_failure_not_a_zero(self):
        assert bench_check.check_weight(board(SilentSerial()))[0] == "FAIL"

    def test_actuation_disabled_is_refused_rather_than_bypassed(self):
        """The safety gate is the point; the harness must not step around it."""
        assert CFG["conveyor.arduino.enabled"] is False
        verdict, detail = bench_check.check_move(CFG, board(), "A")
        assert verdict == "FAIL"
        assert "AURUM_ARDUINO_ENABLED" in detail


class TestReport:
    def test_a_clean_run_exits_zero(self):
        assert bench_check._report([("serial link", "PASS", "ok")]) == 0

    def test_any_failure_exits_non_zero(self):
        results = [("serial link", "PASS", "ok"), ("cell", "FAIL", "silent")]
        assert bench_check._report(results) == 1

    def test_an_uncommanded_paddle_is_unverified_not_passed(self):
        """The standing open item: an ACK is not a movement, and neither is silence."""
        assert bench_check._report([("physical movement", bench_check.UNVERIFIED, "-")]) == 0

    def test_a_missing_port_is_refused_rather_than_guessed(self):
        """No port is the shipped state, and it must not become a guessed one."""
        assert CFG["conveyor.arduino.port"] is None
        assert bench_check.main([]) == 2
