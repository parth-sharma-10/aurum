"""Tests for what happens after a process dies mid-command.

An ACK timeout latches the machine because the paddle's position is unknown. A
kill between the write and the reply is the same unknown with less to go on:
the timeout leaves a record on the command, and a kill leaves nothing. These
tests exercise the marker that closes that gap, and the property that matters
is the negative one — a command that settled must NOT latch the next run.
"""

from __future__ import annotations

import pytest

from app import config
from app.hardware import recovery
from app.hardware.arduino import ArduinoController
from app.hardware.fault import FaultCode
from app.hardware.transport import FakeTransport
from app.pipeline.session import DemoSession


@pytest.fixture
def marker(tmp_path):
    return tmp_path / "in_flight.json"


def controller(marker, transport=None, **env) -> ArduinoController:
    env.setdefault("AURUM_ARDUINO_ENABLED", "true")
    transport = FakeTransport(connected=True) if transport is None else transport
    return ArduinoController(
        transport=transport,
        cfg=config.load(environ={k: str(v) for k, v in env.items()}),
        recovery_marker=marker,
    )


class TestTheMarker:
    def test_nothing_is_pending_on_a_clean_machine(self, marker):
        assert recovery.pending(marker) is None

    def test_an_acknowledged_command_leaves_nothing_behind(self, marker):
        """The property that keeps this from latching every restart."""
        controller(marker).move("A", "AUR-ITEM-1")
        assert recovery.pending(marker) is None

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"transport": FakeTransport(connected=True, fail_with="STALLED")}, "FAILED"),
            (
                {
                    "transport": FakeTransport(connected=True, silent=True),
                    "AURUM_ARDUINO_ACK_TIMEOUT_MS": 20,
                },
                "TIMED_OUT",
            ),
        ],
    )
    def test_a_command_that_settled_badly_still_clears(self, marker, kwargs, expected):
        """A bad outcome is an outcome. Only an absent one is a recovery case."""
        command = controller(marker, **kwargs).move("A", "AUR-ITEM-1")
        assert str(command.state) == expected
        assert recovery.pending(marker) is None

    def test_a_refused_command_never_writes_a_marker(self, marker):
        """Bin C writes no frame, so there is no in-flight window to cover."""
        controller(marker).move("C", "AUR-ITEM-1")
        assert recovery.pending(marker) is None

    def test_an_unreadable_marker_still_counts_as_pending(self, marker):
        """Its presence is the signal. "Something was in flight but I cannot
        say what" is not a reason to report the machine is safe."""
        marker.write_text("{ truncated mid-write")
        assert recovery.pending(marker) is not None

    def test_a_marker_that_cannot_be_written_does_not_stop_the_sort(self, tmp_path):
        """Losing crash protection is bad; refusing to actuate because a disk
        is full is worse, and the operator is at the rig either way."""
        unwritable = tmp_path / "file" / "in_flight.json"
        unwritable.parent.write_text("this is a file, not a directory")
        assert controller(unwritable).move("A", "AUR-ITEM-1").acknowledged


class TestTheNextStart:
    def test_an_interrupted_command_latches_the_new_session(self):
        recovery.mark("CMD-DEAD", "A", "AUR-ITEM-1")
        run = DemoSession(detector=None)
        assert run.fault.active is True
        assert run.fault.current.code is FaultCode.RECOVERY_REQUIRED

    def test_it_names_the_paddle_whose_position_is_unknown(self):
        recovery.mark("CMD-DEAD", "B", "AUR-ITEM-1")
        run = DemoSession(detector=None)
        assert "SERVO_B" in run.fault.current.reason
        assert "CMD-DEAD" in run.fault.current.reason

    def test_a_clean_shutdown_starts_clean(self):
        assert DemoSession(detector=None).fault.active is False

    def test_the_marker_is_consumed_so_a_reset_actually_resets(self):
        """Left in place it would latch again on the next start, and no reset
        the operator could perform would ever clear it."""
        recovery.mark("CMD-DEAD", "A", "AUR-ITEM-1")
        DemoSession(detector=None)
        assert recovery.pending() is None
        assert DemoSession(detector=None).fault.active is False

    def test_a_latched_recovery_refuses_to_actuate(self, marker):
        """The point of the latch, and the only test that proves it works."""
        recovery.mark("CMD-DEAD", "A", "AUR-ITEM-1")
        run = DemoSession(detector=None)
        assert run.fault.refusal() is not None
