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
import threading
import time
from collections import deque

from app.hardware.transport import LinkState, Transport
from app.portlock import PortLock, contention_message
from app.weight import RawSample, StuckWatch, parse_weight_line

#: How many unread frames of each type to keep. The weight stream runs at
#: 10 Hz and a stale sample is worse than an absent one, so the queue is short
#: on purpose: a consumer that stops reading should see fresh data when it
#: comes back, not work through a minute of history first.
QUEUE_LIMIT = 64


class BoardLink:
    """The single serial connection to the Aurum sorter board.

    A failure is a state, never an exception. A USB cable on a bench gets
    knocked out mid-demonstration, and the correct response is an item that
    reports it could not be weighed or routed - not a traceback.
    """

    #: How long to wait after opening for the board to prove its firmware is
    #: running. Generous: an Uno bootloader is ~2 s and a slow one is worse,
    #: and the cost of waiting is a second at start-up against a CFG that
    #: silently does not apply.
    BOOT_TIMEOUT_S = 6.0

    def __init__(self, port: str, baudrate: int = 115200, timeout_s: float = 1.0) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self._serial = None
        self._state = LinkState.DISCONNECTED
        #: The advisory lock on this port, held for the life of the link.
        self._lock = PortLock(port)
        #: Guards opening and closing only - NOT pumping, which stays
        #: single-reader by design. The pan thread reopens a dropped board in
        #: `_heal_link` while the HTTP thread may be connecting the same link
        #: from the dashboard, and the two used to interleave: one would take
        #: the port lock between the other's release and re-acquire, and the
        #: loser reported "already owned by another process" about itself.
        self._gate = threading.RLock()
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
        #: What the conveyor motor is doing, as last acknowledged by the board.
        #: The firmware's watchdog can stop it without being told to, so this is
        #: what we last successfully commanded, not a guarantee - which is why
        #: the session re-asserts it rather than assuming it holds.
        self.belt_running = False
        self.belt_pwm = 0
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

        The mechanism, and the two bench sessions that paid for it, are in
        `app.portlock`. Kept as a method because the connect path reads better
        for it and every call site already exists.
        """
        return self._lock.acquire()

    def _release_lock(self) -> None:
        """Give the port back. Safe on a link that never held it."""
        self._lock.release()

    def connect(self) -> LinkState:
        with self._gate:
            return self._connect()

    def _connect(self) -> LinkState:
        # Opening the port resets the board and parks both paddles. Doing that
        # to a link that is already up - on every dashboard reload, say - would
        # interrupt a run for no reason, so a healthy link is left alone.
        if self._state is LinkState.CONNECTED and self._serial is not None:
            return self._state

        try:
            import serial
        except ImportError:
            self._state = LinkState.DISCONNECTED
            self.last_error = "pyserial is not installed; `pip install pyserial`"
            return self._state

        owner = self._acquire_lock()
        if owner is not None:
            self._state = LinkState.DISCONNECTED
            self.last_error = contention_message(self.port, owner)
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

        # Opening the port resets the board, and anything sent while its
        # bootloader is still running is swallowed by something that is not
        # listening.
        #
        # This used to be `time.sleep(2.0)` - a guess, and too short a one. The
        # first CFG after every open went unacknowledged, burning its whole
        # 4 s budget, while a second CFG a moment later was answered in 10 ms.
        # That looked like a flaky board for three sessions and was really a
        # fixed sleep racing a bootloader.
        #
        # So wait for the board to say something instead of guessing when it
        # might. The sorter prints a boot banner and then streams weight frames
        # at 10 Hz, so one line is proof the FIRMWARE is running - which is the
        # actual precondition for sending it a command.
        deadline = time.monotonic() + self.BOOT_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                if self._serial.readline().strip():
                    break
            except Exception as exc:
                self._serial = None
                self._state = LinkState.DISCONNECTED
                self._release_lock()
                self.last_error = f"the board stopped responding while booting: {exc}"
                return self._state

        self._drain_backlog()
        self._state = LinkState.CONNECTED
        self.last_error = None
        return self._state

    #: Bytes still waiting that count as "the backlog has drained". A board
    #: streaming its normal 10 Hz produces about 20 bytes per 100 ms, so this
    #: is a couple of frames' worth and not a quiet port.
    SETTLED_BYTES = 64

    #: How long to spend clearing a backlog before giving up and going on. The
    #: link is usable either way; this only decides how much rubbish the first
    #: command has to read past.
    DRAIN_TIMEOUT_S = 3.0

    def _drain_backlog(self) -> None:
        """Throw away whatever the board queued up while nobody was listening.

        One `reset_input_buffer()` is not enough. The bench board gets into a
        state where it prints a malformed weight fragment at full line rate -
        1,151,728 bytes of `58900,OK` were measured arriving in a single burst
        at 135 kB/s, twelve times what 115200 baud can carry, which is a
        backlog draining rather than live output. Every one of those lines is
        neither a weight frame nor a protocol reply, so `pump()` counts them as
        dropped, and the first CFG after an open spends its whole four-second
        budget reading rubbish and never reaches its own ACK.

        That is the "board did not acknowledge the servo configuration" that
        has been blamed on the firmware, the servo and the wiring in turn.

        Why the board does it is NOT understood - the fragment looks like a
        weight frame with its head cut off and a frozen `millis()`, and it
        pre-dates the current sketch. This does not fix that. It stops it from
        costing the next command its acknowledgement.
        """
        deadline = time.monotonic() + self.DRAIN_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                self._serial.reset_input_buffer()
                # Let anything still in flight arrive before deciding it is
                # over: resetting an empty buffer proves nothing when the sender
                # is mid-burst.
                time.sleep(0.05)
                if self._serial.in_waiting <= self.SETTLED_BYTES:
                    return
            except Exception:
                return

    def disconnect(self) -> None:
        with self._gate:
            self._disconnect()

    def _disconnect(self) -> None:
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
        # bootloader has already thrown away. The same reset stops the motor -
        # `setup()` calls `beltStop()` before anything else - so a link that has
        # gone down is a belt that is stopped, and saying otherwise would be a
        # claim about a moving machine that nothing supports.
        self.servo_config = None
        self.belt_running = False
        self.belt_pwm = 0

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
        with self._gate:
            return self._reconnect()

    def _reconnect(self) -> bool:
        if self._state is LinkState.CONNECTED:
            return True
        # Through `disconnect` rather than closing the descriptor here: it also
        # drops the queued frames and forgets the acknowledged servo angles,
        # both of which belong to a board that has since rebooted. Reporting
        # the old angles after a re-enumeration would be a stale claim, and the
        # caller reapplies them once the link is back.
        self._disconnect()
        return self._connect() is LinkState.CONNECTED

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
        # SERIALISED, because this method pops from a queue it shares with
        # every other reader and discards whatever is not its own id.
        #
        # Two concurrent callers therefore eat each other's acknowledgement:
        # each drains `_responses` looking for its own command, throws the
        # other's ACK away, and the loser reports a board that "did not
        # acknowledge" while the board answered both in 13 ms. React StrictMode
        # double-invoking an effect was enough to produce it, and so is any two
        # clients calling POST /session/board/connect together.
        with self._gate:
            return self._configure_servos(rest_deg, push_deg, hold_ms, budget_s)

    def _configure_servos(
        self, rest_deg: float, push_deg: float, hold_ms: float, budget_s: float
    ) -> bool:
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
                    # Cleared, because it is this method's own failure message
                    # and this method has just succeeded. Leaving it made the
                    # snapshot report `servo_config_applied: true` next to "the
                    # board did not acknowledge the servo configuration" - which
                    # is what the operator screen showed after every reconnect,
                    # since the first CFG on a fresh port routinely loses its
                    # ACK to the boot backlog and the second one works.
                    self.last_error = None
                    return True
                return False
        self.last_error = f"the board did not acknowledge the servo configuration ({command_id})"
        return False

    def belt(self, run: bool, pwm: int = 0, budget_s: float = 2.0) -> bool:
        """Start or stop the conveyor motor. True once the board acknowledges.

        Under the same gate as everything else that waits for a reply, because
        it drains the shared response queue exactly as `configure_servos` and
        `move` do - two exchanges at once discard each other's acknowledgement.

        `BELT RUN` is a LEASE, not a switch. The firmware stops the motor if it
        is not renewed inside `BELT_WATCHDOG_MS`, so the caller must keep
        asserting it while the belt should run - and a host that dies stops the
        belt by doing nothing. Renewal is why this is not duplicate-suppressed
        the way `move` is: it asserts a state rather than performing an action.
        """
        with self._gate:
            return self._belt(run, pwm, budget_s)

    def _belt(self, run: bool, pwm: int, budget_s: float) -> bool:
        from app.hardware.arduino import new_command_id, parse_response

        command_id = new_command_id()
        frame = (
            f"AURUM/1 BELT RUN {int(pwm)} {command_id}"
            if run
            else (f"AURUM/1 BELT STOP {command_id}")
        )
        if not self.send(frame):
            return False
        deadline = time.monotonic() + budget_s
        while (remaining := deadline - time.monotonic()) > 0:
            line = self._next(self._responses, remaining)
            if line is None:
                break
            response = parse_response(line)
            if response is not None and response.command_id == command_id:
                if response.verb == "ACK":
                    self.belt_running = bool(run)
                    self.belt_pwm = int(pwm) if run else 0
                    self.last_error = None
                    return True
                self.last_error = f"the board refused the belt command ({response.code})"
                return False
        self.last_error = f"the board did not acknowledge the belt command ({command_id})"
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
            "belt_running": self.belt_running,
            "belt_pwm": self.belt_pwm,
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
        # A frozen converter is not a link fault: frames keep arriving and
        # `connected` stays true, which is precisely why it needs catching
        # here. This is the reader the RUNNING machine uses - the pan machine
        # takes one sample per poll from it - so a rule applied only in
        # `WeightSensor` would never see the reading the operator is shown.
        self._stuck = StuckWatch()

    @property
    def connected(self) -> bool:
        return self._link.connected

    @property
    def stuck(self) -> bool:
        return self._stuck.error is not None

    @property
    def last_error(self) -> str | None:
        return self._stuck.error or self._link.last_error

    def read(self) -> RawSample | None:
        return self._stuck.accept(self._link.next_weight())

    def close(self) -> None:
        self._link.disconnect()


class _CommandView(Transport):
    """The `Transport` face of a `BoardLink`, for `ArduinoController`."""

    name = "board-serial"

    def __init__(self, link: BoardLink) -> None:
        super().__init__()
        self._link = link
        # THE LINK'S OWN GATE, not a lock of this view's own. `configure_servos`
        # is called on the BoardLink directly while `move` goes through this
        # view, so two separate locks would leave a CFG from the API thread
        # racing a MOVE from the pan thread - both draining the one response
        # queue, each discarding the other's ACK. One lock is what makes "one
        # exchange at a time on this port" true rather than nearly true.
        self._exchange_lock = link._gate

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
