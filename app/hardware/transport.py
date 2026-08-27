"""The serial boundary. Nothing above this file imports pyserial.

Two implementations of one interface:

    SerialTransport   real USB serial to the Arduino
    FakeTransport     in-memory, scripted, for tests and dry runs

Keeping the port behind an interface is what lets the whole actuation chain —
command, ACK, timeout, duplicate suppression, reconnect — be tested without a
board attached, and it is why `app/routing/` can stay free of hardware.

A USB cable on a bench gets knocked out. Every failure here is a state the
caller can act on, never an exception that takes the pipeline down.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque
from enum import StrEnum


class LinkState(StrEnum):
    """Where the link to the board stands."""

    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    #: Open, but misbehaving — timeouts, malformed frames, refused writes.
    DEGRADED = "DEGRADED"


class Transport:
    """The interface the actuation layer talks to."""

    name = "transport"

    def __init__(self) -> None:
        self._exchange_lock = threading.RLock()

    @property
    def exchange_lock(self) -> threading.RLock:
        """Held for one whole send-then-await-the-reply exchange.

        The reply queue is SHARED by every reader, and a reader that pops a
        reply carrying somebody else's command id discards it and reads on. So
        two exchanges running at once eat each other's acknowledgement: each
        throws the other's ACK away and the loser reports a board that did not
        answer while the board answered both in milliseconds. That was found
        and fixed once for CFG alone; MOVE has the same shape and the same
        queue, and a MOVE that loses its ACK latches ACK_TIMEOUT and stops the
        machine over a paddle that moved perfectly well.

        An RLock rather than a Lock because `BoardLink` hands out its own link
        gate here, and that gate is already re-entered by `configure_servos`.
        """
        return self._exchange_lock

    @property
    def state(self) -> LinkState:  # pragma: no cover - interface
        raise NotImplementedError

    def connect(self) -> LinkState:  # pragma: no cover - interface
        raise NotImplementedError

    def send(self, line: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def receive(self) -> str | None:  # pragma: no cover - interface
        raise NotImplementedError

    def disconnect(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    close = disconnect


class SerialTransport(Transport):
    """USB serial to the Arduino.

    Deliberately thin: it moves lines and reports what the link is doing. It
    knows nothing about commands, servos or acknowledgements.
    """

    name = "serial"

    # 115200 to match the sketch (hardware/arduino/aurum_sorter/aurum_sorter.ino)
    # and BoardLink. A mismatched default reads as line noise, not as an error.
    def __init__(self, port: str, baudrate: int = 115200, timeout_s: float = 1.0) -> None:
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self._serial = None
        self._state = LinkState.DISCONNECTED
        self.last_error: str | None = None

    @property
    def state(self) -> LinkState:
        return self._state

    def connect(self) -> LinkState:
        """Open the port. A failure is a state, not an exception."""
        try:
            import serial
        except ImportError:
            self._state = LinkState.DISCONNECTED
            self.last_error = "pyserial is not installed; `pip install pyserial`"
            return self._state

        self._state = LinkState.CONNECTING
        try:
            self._serial = serial.Serial(self.port, self.baudrate, timeout=self.timeout_s)
        except Exception as exc:
            self._serial = None
            self._state = LinkState.DISCONNECTED
            self.last_error = f"could not open {self.port}: {exc}"
            return self._state

        # The board resets when the port opens; anything sent before it has
        # booted is discarded by a bootloader that is not listening.
        time.sleep(2.0)
        self._state = LinkState.CONNECTED
        self.last_error = None
        return self._state

    def send(self, line: str) -> bool:
        if self._serial is None or self._state is LinkState.DISCONNECTED:
            self.last_error = "not connected"
            return False
        try:
            self._serial.write(f"{line}\n".encode("ascii", errors="ignore"))
            self._serial.flush()
        except Exception as exc:
            self._state = LinkState.DEGRADED
            self.last_error = f"write failed: {exc}"
            return False
        return True

    def receive(self) -> str | None:
        if self._serial is None:
            return None
        try:
            raw = self._serial.readline()
        except Exception as exc:
            self._state = LinkState.DEGRADED
            self.last_error = f"read failed: {exc}"
            return None
        line = raw.decode("ascii", errors="ignore").strip()
        return line or None

    def disconnect(self) -> None:
        with contextlib.suppress(Exception):
            if self._serial is not None:
                self._serial.close()
        self._serial = None
        self._state = LinkState.DISCONNECTED

    close = disconnect


class FakeTransport(Transport):
    """An in-memory board for tests and dry runs.

    Answers the real protocol so the layer above is exercised rather than
    imitated. It can be told to go silent, to fail, or to be unplugged, because
    those are the cases that matter and a real board will not perform them on
    demand.
    """

    name = "fake"

    def __init__(
        self,
        auto_ack: bool = True,
        fail_with: str | None = None,
        silent: bool = False,
        connected: bool = False,
    ) -> None:
        super().__init__()
        self.sent: list[str] = []
        self._inbox: deque[str] = deque()
        self.auto_ack = auto_ack
        self.fail_with = fail_with
        self.silent = silent
        self._state = LinkState.CONNECTED if connected else LinkState.DISCONNECTED
        self.last_error: str | None = None
        #: command ids the board has already acted on, mirroring the sketch.
        self.executed: set[str] = set()
        self.movements: list[tuple[str, str]] = []
        self.servo_config: tuple | None = None
        self.connects = 0

    @property
    def state(self) -> LinkState:
        return self._state

    def connect(self) -> LinkState:
        self.connects += 1
        self._state = LinkState.CONNECTED
        self.last_error = None
        return self._state

    def unplug(self) -> None:
        """Simulate the cable coming out mid-run."""
        self._state = LinkState.DISCONNECTED
        self.last_error = "cable unplugged"

    def send(self, line: str) -> bool:
        if self._state is not LinkState.CONNECTED:
            self.last_error = "not connected"
            return False
        self.sent.append(line)
        if not self.silent:
            self._respond(line)
        return True

    def _respond(self, line: str) -> None:
        parts = line.split()
        if len(parts) < 2 or parts[0] != "AURUM/1":
            self._inbox.append("AURUM/1 ERR - BAD_FRAME")
            return
        verb = parts[1]
        if verb == "CFG":
            if len(parts) != 6:
                self._inbox.append(f"AURUM/1 ERR {parts[-1]} BAD_FRAME")
                return
            self.servo_config = (parts[2], parts[3], parts[4])
            self._inbox.append(f"AURUM/1 ACK {parts[5]}")
            return
        if verb == "PING":
            self._inbox.append(f"AURUM/1 PONG {parts[2] if len(parts) > 2 else '-'}")
            return
        if verb != "MOVE" or len(parts) != 5:
            self._inbox.append(f"AURUM/1 ERR {parts[-1]} BAD_FRAME")
            return

        _, _, target, _item_id, command_id = parts
        if self.fail_with:
            self._inbox.append(f"AURUM/1 ERR {command_id} {self.fail_with}")
            return
        if command_id in self.executed:
            # Idempotent: acknowledge without moving anything a second time.
            self._inbox.append(f"AURUM/1 ACK {command_id} DUP")
            return
        self.executed.add(command_id)
        self.movements.append((target, command_id))
        if self.auto_ack:
            self._inbox.append(f"AURUM/1 ACK {command_id}")

    def receive(self) -> str | None:
        return self._inbox.popleft() if self._inbox else None

    def feed(self, line: str) -> None:
        """Push a line as though the board had sent it."""
        self._inbox.append(line)

    def disconnect(self) -> None:
        self._state = LinkState.DISCONNECTED

    close = disconnect


class SimulatedTransport(FakeTransport):
    """HARDWARE_MODE=SIMULATION: the whole protocol, none of the wire.

    Distinct from `FakeTransport` only in its name, and the name is the point:
    a snapshot showing `transport: simulated` says the ACKs on screen came from
    software. A test fixture and a deliberately simulated run should not look
    identical in a report.
    """

    name = "simulated"
