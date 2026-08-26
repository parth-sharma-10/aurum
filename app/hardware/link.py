"""One board, one port, two frame types.

The demonstration rig is a single Arduino carrying both the HX711 and the two
servos, so the weight stream and the command protocol share one USB serial
link. Opening the port twice does not work — the second open either fails or
steals lines from the first — so this module owns the port once and hands out
two views of it:

    BoardLink.weight_reader   -> the RawReader that app.weight.WeightSensor wants
    BoardLink.transport       -> the Transport that ArduinoController wants

Both views pump the same underlying port. Whichever one is asked for a line
reads it, files it by type, and returns only lines of its own type:

    W,1,10432,-261605,OK        -> the weight queue
    AURUM/1 ACK CMD-1A2B3C4D    -> the response queue
    anything else               -> dropped, and counted

That is why no thread and no lock appear here. A reader that is waiting is a
reader that is pumping, so a caller blocked on a stable mass keeps the ACK
queue moving and vice versa. Threading two consumers onto one serial port
would be the obvious design and the one that loses frames at 3am.

Nothing above `app.hardware` imports pyserial, including through this file.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from collections import deque

from app.hardware.transport import LinkState, Transport
from app.weight import RawSample, parse_weight_line

#: How many unread frames of each type to keep. The weight stream runs at
#: 10 Hz and a stale sample is worse than an absent one, so the queue is short
#: on purpose: a consumer that stops reading should see fresh data when it
#: comes back, not work through a minute of history first.
QUEUE_LIMIT = 64

#: Where the per-port advisory lock lives. One file per device node, so two
#: boards on two ports do not exclude each other.
LOCK_DIR = "/tmp"


def lock_path(port: str) -> str:
    return os.path.join(LOCK_DIR, f"aurum-{os.path.basename(port)}.lock")


class BoardLink:
    """The single serial connection to the Aurum sorter board.

    A failure is a state, never an exception. A USB cable on a bench gets
    knocked out mid-demonstration, and the correct response is an item that
    reports it could not be weighed or routed - not a traceback.
    """

    def __init__(self, port: str, baudrate: int = 115200, timeout_s: float = 1.0) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self._serial = None
        self._state = LinkState.DISCONNECTED
        #: The advisory lock on this port, held for the life of the link.
        self._lock = None
        self.last_error: str | None = None
        self._weight: deque[RawSample] = deque(maxlen=QUEUE_LIMIT)
        self._responses: deque[str] = deque(maxlen=QUEUE_LIMIT)
        #: Lines that were neither a weight frame nor a protocol reply. Boot
        #: banners and line noise live here rather than being misread as data.
        self.dropped = 0
        #: The angles the board ACKNOWLEDGED, or None while it is still running
        #: whatever the sketch booted with. Read from the snapshot: an operator
        #: measuring a throw needs to know which of the two is in force, and an
        #: unapplied CFG is otherwise invisible from outside this method.
        self.servo_config: tuple[int, int, int] | None = None
        self.weight_reader = _WeightView(self)
        self.transport = _CommandView(self)

    # -- link --------------------------------------------------------------
    @property
    def state(self) -> LinkState:
        return self._state

    @property
    def connected(self) -> bool:
        return self._state is LinkState.CONNECTED

    def _acquire_lock(self) -> str | None:
        """Take this port's advisory lock. None on success, else who holds it.

        macOS `cu.*` device nodes do not lock. A second process opens the same
        port successfully and the two then SPLIT the board's replies between
        them - each `readline()` takes whatever the other has not taken yet. The
        symptom is not a refused open: it is PING, CFG and MOVE all timing out
        against a port that looks perfectly healthy, which is what a failed
        bench run on 2026-08-26 turned out to be after an hour of suspecting the
        firmware. This module's own docstring said "this module owns the port
        once"; nothing made that true until here.

        `flock` is per open-file-description, so this also catches two
        `BoardLink`s inside one process, which is the same bug with a shorter
        cable.

        A lock file that cannot be OPENED is not treated as contention: that
        says nothing about who holds the port, and refusing on it would brick a
        working machine over a /tmp permission. Only a lock somebody else holds
        refuses.
        """
        path = lock_path(self.port)
        try:
            handle = open(path, "a+")  # noqa: SIM115 - held for the link's life
        except OSError:
            return None
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            owner = ""
            with contextlib.suppress(Exception):
                handle.seek(0)
                owner = handle.read().strip()
            handle.close()
            return owner or "another process"
        handle.seek(0)
        handle.truncate()
        handle.write(f"PID {os.getpid()}\n")
        handle.flush()
        self._lock = handle
        return None

    def _release_lock(self) -> None:
        """Give the port back. Safe on a link that never held it."""
        handle, self._lock = self._lock, None
        if handle is None:
            return
        with contextlib.suppress(Exception):
            fcntl.flock(handle, fcntl.LOCK_UN)
        with contextlib.suppress(Exception):
            handle.close()

    def connect(self) -> LinkState:
        try:
            import serial
        except ImportError:
            self._state = LinkState.DISCONNECTED
            self.last_error = "pyserial is not installed; `pip install pyserial`"
            return self._state

        owner = self._acquire_lock()
        if owner is not None:
            self._state = LinkState.DISCONNECTED
            self.last_error = (
                f"{self.port} is already owned by {owner}. Two readers on one "
                "macOS cu.* port do not fail to open - they split the board's "
                "replies, and every command times out against a healthy port. "
                "Stop the other process and try again."
            )
            return self._state

        self._state = LinkState.CONNECTING
        try:
            self._serial = serial.Serial(self.port, self.baudrate, timeout=self.timeout_s)
        except Exception as exc:
            self._serial = None
            self._state = LinkState.DISCONNECTED
            # Released, not kept: a link that failed to open owns nothing, and
            # holding it would refuse this process's own next attempt.
            self._release_lock()
            self.last_error = f"could not open {self.port}: {exc}"
            return self._state

        # Opening the port resets the board. Anything sent before it has booted
        # is swallowed by a bootloader that is not listening.
        time.sleep(2.0)
        with contextlib.suppress(Exception):
            self._serial.reset_input_buffer()
        self._state = LinkState.CONNECTED
        self.last_error = None
        return self._state

    def disconnect(self) -> None:
        with contextlib.suppress(Exception):
            if self._serial is not None:
                self._serial.close()
        self._serial = None
        self._state = LinkState.DISCONNECTED
        self._release_lock()
        self._weight.clear()
        self._responses.clear()
        # Reopening the port resets the board, so the angles it acknowledged
        # are gone with it. Keeping them would report a configuration that the
        # bootloader has already thrown away.
        self.servo_config = None

    close = disconnect

    def reconnect(self) -> bool:
        """Close a broken link and open it again. True if the board is back.

        The bench board drops off USB and re-enumerates under the same device
        node, which leaves this object holding a file descriptor that will
        never yield another byte. Reopening is the only way back, and doing it
        automatically is the difference between a demonstration that pauses for
        a few seconds and one that stops until somebody notices.

        Only ever called for a link that is already DEGRADED or DISCONNECTED —
        reopening a healthy port would reset the board for no reason.
        """
        if self._state is LinkState.CONNECTED:
            return True
        # Through `disconnect` rather than closing the descriptor here: it also
        # drops the queued frames and forgets the acknowledged servo angles,
        # both of which belong to a board that has since rebooted. Reporting
        # the old angles after a re-enumeration would be a stale claim, and the
        # caller reapplies them once the link is back.
        self.disconnect()
        return self.connect() is LinkState.CONNECTED

    def configure_servos(
        self, rest_deg: float, push_deg: float, hold_ms: float, budget_s: float = 4.0
    ) -> bool:
        """Push the servo angles to the board, so tuning needs no reflash.

        Every frame the sketch accepts carries a command id, including this
        one: the board answers `ACK <id>`, and a reply that cannot be matched
        to a request is a reply that cannot be trusted.

        The reply is consumed here rather than left queued. An unread CFG ACK
        is the next MOVE's problem: `_await` pops it, finds an id it did not
        ask about, discards it and reads on - which is correct but costs that
        command a round of its own budget for no reason.

        `budget_s` is an acknowledgement budget, not a read timeout, which is
        why it defaults far above `timeout_s`. The board interleaves its replies
        with a continuous stream of other lines, so reaching one is a matter of
        how long we are willing to read rather than how long a single read
        blocks. Measured on the bench: one second was not enough, and the
        caller should pass `conveyor.arduino.ack_timeout_ms`.

        Each poll gets what is LEFT of that budget, not `timeout_s`. Handing
        `_next` the shorter figure capped the whole wait at one read timeout —
        it returns None when its own budget ends just as it does when the port
        runs dry, and this loop cannot tell those apart, so it gave up on a
        board that was still streaming. Measured at 0.852 s in isolation against
        a 1.0 s `timeout_s`, which is why it passed on the bench and failed
        under the server.
        """
        from app.hardware.arduino import new_command_id, parse_response

        command_id = new_command_id()
        if not self.send(
            f"AURUM/1 CFG {int(rest_deg)} {int(push_deg)} {int(hold_ms)} {command_id}"
        ):
            return False
        deadline = time.monotonic() + budget_s
        while (remaining := deadline - time.monotonic()) > 0:
            line = self._next(self._responses, remaining)
            if line is None:
                break
            response = parse_response(line)
            if response is not None and response.command_id == command_id:
                if response.verb == "ACK":
                    self.servo_config = (int(rest_deg), int(push_deg), int(hold_ms))
                return response.verb == "ACK"
        self.last_error = f"the board did not acknowledge the servo configuration ({command_id})"
        return False

    # -- frames ------------------------------------------------------------
    def send(self, line: str) -> bool:
        if self._serial is None or self._state is not LinkState.CONNECTED:
            self.last_error = self.last_error or "not connected"
            return False
        try:
            self._serial.write(f"{line}\n".encode("ascii", errors="ignore"))
            self._serial.flush()
        except Exception as exc:
            self._state = LinkState.DEGRADED
            self.last_error = f"write failed: {exc}"
            return False
        return True

    def pump(self) -> bool:
        """Read at most one line and file it. False when nothing arrived.

        One line rather than "drain everything": a caller in a timed loop needs
        to be able to give up, and a board streaming at 10 Hz can always supply
        one more line.
        """
        if self._serial is None:
            return False
        try:
            raw = self._serial.readline()
        except Exception as exc:
            self._state = LinkState.DEGRADED
            self.last_error = f"read failed: {exc}"
            return False
        line = raw.decode("ascii", errors="ignore").strip()
        if not line:
            return False

        sample = parse_weight_line(line)
        if sample is not None:
            self._weight.append(sample)
            return True
        if line.startswith("AURUM/1 "):
            self._responses.append(line)
            return True
        # A W frame with status ERR lands here too, which is correct: it is a
        # real line the board sent, and it is not a mass.
        self.dropped += 1
        return True

    # Both accessors pump until a frame of THEIR type arrives, the port runs
    # dry, or the budget runs out. Pumping once would let the other stream
    # starve this one: ask for a mass while the board is answering a command,
    # and a single line read would be the ACK, leaving the caller with None
    # despite data being available.
    #
    # The budget is what makes that safe against a real board, which is never
    # idle: the sketch streams weight frames unprompted, so `pump()` always
    # succeeds and waiting for a reply that only answers a command never ends.
    # ArduinoController._await tests its deadline BETWEEN calls to receive(),
    # so a receive() that never returns took ACK_TIMEOUT with it - the latch
    # for a paddle that may already be half out could not fire on the one path
    # where a paddle exists.
    def _next(self, queue: deque, budget_s: float):
        """The next queued frame of this type, or None once the budget is spent.

        A quiet port is not a dead one, and `pump()` cannot tell them apart:
        it returns False both when a read times out with the board simply
        between frames, and when the port has gone. Treating the first as
        terminal is what capped `configure_servos` at one read timeout — and
        on the bench the first CFG after a port open takes 1.310 s, which is
        longer than one.

        So only two things end the wait: the budget, or the link actually
        leaving CONNECTED (which `pump()` sets on a read failure). A quiet
        port is paced by `readline`'s own timeout, not spun on.
        """
        deadline = time.monotonic() + budget_s
        while not queue and time.monotonic() < deadline:
            if not self.pump() and self._state is not LinkState.CONNECTED:
                break
        return queue.popleft() if queue else None

    def next_weight(self) -> RawSample | None:
        return self._next(self._weight, self.timeout_s)

    def next_response(self) -> str | None:
        return self._next(self._responses, self.timeout_s)

    def snapshot(self) -> dict:
        return {
            "port": self.port,
            "baudrate": self.baudrate,
            "state": str(self._state),
            "connected": self.connected,
            "last_error": self.last_error,
            "queued_weight_frames": len(self._weight),
            "queued_responses": len(self._responses),
            "dropped_lines": self.dropped,
            "servo_config_applied": self.servo_config is not None,
            "servo_config": (
                dict(zip(("rest_deg", "push_deg", "hold_ms"), self.servo_config, strict=True))
                if self.servo_config
                else None
            ),
        }


class _WeightView:
    """The `RawReader` face of a `BoardLink`, for `app.weight.WeightSensor`."""

    name = "hx711-serial"

    def __init__(self, link: BoardLink) -> None:
        self._link = link

    @property
    def connected(self) -> bool:
        return self._link.connected

    @property
    def last_error(self) -> str | None:
        return self._link.last_error

    def read(self) -> RawSample | None:
        return self._link.next_weight()

    def close(self) -> None:
        self._link.disconnect()


class _CommandView(Transport):
    """The `Transport` face of a `BoardLink`, for `ArduinoController`."""

    name = "board-serial"

    def __init__(self, link: BoardLink) -> None:
        self._link = link

    @property
    def state(self) -> LinkState:
        return self._link.state

    @property
    def last_error(self) -> str | None:
        return self._link.last_error

    def connect(self) -> LinkState:
        return self._link.connect()

    def send(self, line: str) -> bool:
        return self._link.send(line)

    def receive(self) -> str | None:
        return self._link.next_response()

    def disconnect(self) -> None:
        self._link.disconnect()

    close = disconnect
