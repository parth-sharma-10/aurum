"""The Arduino command layer: frames, acknowledgement, and what an ACK means.

    ScheduledRoute (DUE) -> Command -> serial -> board -> ACK -> EXECUTED

Four words that are not synonyms, and the reason this layer exists:

    decision    policy chose Bin A
    route       geometry chose Servo A at a particular moment
    command     software asked the board to move
    execution   the board said it acted

**An ACK is not proof that a servo physically moved.** It is proof the board
received a well-formed frame and believes it acted on it. A stalled servo, a
disconnected signal wire or a dead supply rail all still ACK. Physical
movement is established by bench observation and recorded separately in
`docs/hardware.md` — never inferred from this module.

**Nothing is retried.** A command that times out stays `TIMED_OUT`. Resending
blind would be the one way to move a paddle twice for one item, and a second
movement lands on whatever is behind the first item. If a retry is ever wanted
it must be proven safe against the duplicate rules first.

Protocol, line-delimited and versioned:

    host  ->  AURUM/1 MOVE <A|B> <item_id> <command_id>
    host  ->  AURUM/1 CFG <rest_deg> <push_deg> <hold_ms> <command_id>
    host  ->  AURUM/1 PING <command_id>
    board ->  AURUM/1 ACK <command_id> [DUP]
    board ->  AURUM/1 ERR <command_id> <code>
    board ->  AURUM/1 PONG <command_id>

Weight frames (`W,1,...`, Phase 5) share the same link and are ignored here.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from enum import StrEnum

from app import config as config_module
from app.hardware import recovery
from app.hardware.fault import FaultCode, HardwareFault
from app.hardware.transport import (
    FakeTransport,
    LinkState,
    SerialTransport,
    SimulatedTransport,
    Transport,
)

PROTOCOL = "AURUM/1"
VALID_TARGETS = ("A", "B")


class CommandState(StrEnum):
    """The life of one actuation request."""

    CREATED = "CREATED"
    SENT = "SENT"
    #: The board acknowledged. NOT proof the paddle moved.
    ACKED = "ACKED"
    #: The board reported an error, or the frame could not be written.
    FAILED = "FAILED"
    #: No acknowledgement inside `arduino.ack_timeout_ms`.
    TIMED_OUT = "TIMED_OUT"
    #: Suppressed on purpose: this item or command was already actioned.
    SUPPRESSED = "SUPPRESSED"


@dataclass
class Command:
    """One actuation request, with everything needed to audit it."""

    command_id: str
    item_id: str
    target: str
    state: CommandState = CommandState.CREATED
    created_at: float | None = None
    sent_at: float | None = None
    settled_at: float | None = None
    error_code: str | None = None
    reason: str | None = None
    duplicate_of: str | None = None
    raw_frame: str | None = None
    raw_response: str | None = None

    @property
    def acknowledged(self) -> bool:
        return self.state is CommandState.ACKED

    def as_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "item_id": self.item_id,
            "target": self.target,
            "servo": f"SERVO_{self.target}" if self.target in VALID_TARGETS else None,
            "state": str(self.state),
            "created_at": self.created_at,
            "sent_at": self.sent_at,
            "settled_at": self.settled_at,
            "error_code": self.error_code,
            "reason": self.reason,
            "duplicate_of": self.duplicate_of,
            "frame": self.raw_frame,
            "response": self.raw_response,
            "ack_meaning": (
                "ACKED means the board received the frame and believes it acted. "
                "It is not evidence that a servo physically moved."
            ),
        }


def new_command_id() -> str:
    return f"CMD-{uuid.uuid4().hex[:8].upper()}"


def build_frame(target: str, item_id: str, command_id: str) -> str:
    return f"{PROTOCOL} MOVE {target} {item_id} {command_id}"


@dataclass
class Response:
    """One parsed board reply."""

    verb: str
    command_id: str
    code: str | None = None
    duplicate: bool = False


def parse_response(line: object) -> Response | None:
    """Parse a board reply, or None if the line is not one.

    Weight frames and noise return None rather than being coerced. A malformed
    reply must never be read as an acknowledgement.
    """
    if not isinstance(line, str):
        return None
    parts = line.strip().split()
    if len(parts) < 3 or parts[0] != PROTOCOL:
        return None
    verb, command_id = parts[1], parts[2]
    if verb == "ACK":
        return Response("ACK", command_id, duplicate=len(parts) > 3 and parts[3] == "DUP")
    if verb == "ERR":
        return Response("ERR", command_id, code=parts[3] if len(parts) > 3 else "UNKNOWN")
    if verb == "PONG":
        return Response("PONG", command_id)
    return None


class ArduinoController:
    """Sends actuation commands and waits for the board to acknowledge them.

    Duplicate protection runs at two levels. **Per item:** one physical item
    gets at most one movement, mirroring the scheduler's rule so a retry loop
    upstream cannot produce two. **Per command id:** a resend of an id already
    settled is suppressed rather than written again, and the sketch keeps its
    own recent-id list so an ACK lost on the wire cannot cause a second move.
    """

    def __init__(
        self,
        transport: Transport | None = None,
        cfg: config_module.Config | None = None,
        clock=time.monotonic,
        fault: HardwareFault | None = None,
        recovery_marker=None,
    ) -> None:
        #: Where the in-flight command marker lives. Injectable so a test does
        #: not write to the repository's own.
        self.recovery_marker = recovery_marker
        self.cfg = config_module.load() if cfg is None else cfg
        self.transport = transport if transport is not None else self._transport_from_config()
        self.ack_timeout_s = self.cfg["conveyor.arduino.ack_timeout_ms"] / 1000.0
        self.enabled = bool(self.cfg["conveyor.arduino.enabled"])
        self.simulation = bool(self.cfg["conveyor.runtime.simulation"])
        #: Shared with the servo bridge and the session, so all three agree
        #: about whether the machine is currently safe to actuate.
        self.fault = HardwareFault() if fault is None else fault
        self._clock = clock
        self._commands: dict[str, Command] = {}
        self._by_item: dict[str, str] = {}

    def _transport_from_config(self) -> Transport:
        if self.cfg["conveyor.runtime.simulation"]:
            # HARDWARE_MODE=SIMULATION. The whole protocol still runs and the
            # board still acknowledges, but no byte reaches a real port even if
            # one is configured - which is the difference between simulating
            # the machine and driving it.
            return SimulatedTransport(connected=True)
        port = self.cfg["conveyor.arduino.port"]
        if not port:
            # No port configured is a normal state, not an error: the software
            # runs and refuses to actuate rather than inventing a link.
            return FakeTransport(connected=False)
        return SerialTransport(
            port,
            baudrate=self.cfg["conveyor.arduino.baudrate"],
            timeout_s=self.cfg["conveyor.arduino.timeout_s"],
        )

    def servo_angle_problem(self) -> str | None:
        """Why the configured servo angles cannot be used, or None.

        An MG995 accepts 0-180 degrees. A value outside that is a typo that
        would be written to the board and either ignored or driven against a
        mechanical stop, and neither is something to discover during a
        demonstration.
        """
        for key in ("conveyor.servo.rest_angle_deg", "conveyor.servo.push_angle_deg"):
            angle = self.cfg[key]
            if not isinstance(angle, (int, float)) or isinstance(angle, bool):
                return f"{key} is {angle!r}, which is not an angle."
            if not 0.0 <= float(angle) <= 180.0:
                return f"{key} is {angle}, outside the 0-180 degrees a servo accepts."
        if self.cfg["conveyor.servo.rest_angle_deg"] == self.cfg["conveyor.servo.push_angle_deg"]:
            return (
                "conveyor.servo.rest_angle_deg and push_angle_deg are equal, so the "
                "paddle would not move at all."
            )
        return None

    # -- link --------------------------------------------------------------
    @property
    def state(self) -> LinkState:
        return self.transport.state

    @property
    def connected(self) -> bool:
        return self.transport.state is LinkState.CONNECTED

    def connect(self) -> LinkState:
        return self.transport.connect()

    def disconnect(self) -> None:
        self.transport.disconnect()

    def configure(self) -> Command:
        """Send the servo angles to the board.

        Keeps the bench values in `configs/conveyor.yaml` rather than compiled
        into the sketch, so tuning them on a real machine needs no reflash.
        """
        command_id = new_command_id()
        rest = self.cfg["conveyor.servo.rest_angle_deg"]
        push = self.cfg["conveyor.servo.push_angle_deg"]
        hold = self.cfg["conveyor.servo.actuation_ms"]
        command = Command(
            command_id=command_id,
            item_id="-",
            target="CFG",
            created_at=self._clock(),
            raw_frame=f"{PROTOCOL} CFG {rest:.0f} {push:.0f} {hold:.0f} {command_id}",
        )
        if not self.connected:
            command.state = CommandState.FAILED
            command.error_code = "NOT_CONNECTED"
            command.reason = f"No link to the board ({self.transport.state})."
            return command
        if not self.transport.send(command.raw_frame):
            command.state = CommandState.FAILED
            command.error_code = "WRITE_FAILED"
            command.reason = getattr(self.transport, "last_error", "the write failed")
            return command
        command.state = CommandState.SENT
        command.sent_at = self._clock()
        response = self._await(command_id)
        command.settled_at = self._clock()
        if response is None:
            command.state = CommandState.TIMED_OUT
            command.reason = "The board did not acknowledge the servo configuration."
        elif response.verb == "ERR":
            command.state = CommandState.FAILED
            command.error_code = response.code
            command.reason = f"The board reported {response.code}."
        else:
            command.state = CommandState.ACKED
            command.reason = (
                f"Board configured: rest {rest:.0f}, push {push:.0f}, hold {hold:.0f} ms."
            )
        return command

    def ping(self) -> bool:
        """Round-trip the link. False when the board does not answer."""
        if not self.connected:
            return False
        command_id = new_command_id()
        if not self.transport.send(f"{PROTOCOL} PING {command_id}"):
            return False
        response = self._await(command_id)
        return response is not None and response.verb == "PONG"

    # -- commands ----------------------------------------------------------
    def commands(self) -> list[Command]:
        return list(self._commands.values())

    def get(self, command_id: str) -> Command | None:
        return self._commands.get(command_id)

    def for_item(self, item_id: str) -> Command | None:
        command_id = self._by_item.get(item_id)
        return self._commands.get(command_id) if command_id else None

    def move(self, target: str, item_id: str, command_id: str | None = None) -> Command:
        """Ask the board to move a paddle, and report exactly what happened."""
        command_id = command_id or new_command_id()
        now = self._clock()

        if command_id in self._commands:
            existing = self._commands[command_id]
            return self._suppress(
                command_id,
                item_id,
                target,
                now,
                f"Command {command_id} was already {existing.state}; refusing to send it twice.",
                duplicate_of=command_id,
            )

        if item_id in self._by_item:
            prior = self._by_item[item_id]
            return self._suppress(
                command_id,
                item_id,
                target,
                now,
                f"{item_id} already has command {prior}. One physical item gets "
                "one physical movement.",
                duplicate_of=prior,
            )

        command = Command(
            command_id=command_id,
            item_id=item_id,
            target=target,
            created_at=now,
        )
        self._commands[command_id] = command

        if target not in VALID_TARGETS:
            command.state = CommandState.FAILED
            command.error_code = "BAD_TARGET"
            command.reason = f"{target!r} is not an actuator. Bin C has no servo."
            command.settled_at = now
            return command

        if not self.enabled:
            command.state = CommandState.FAILED
            command.error_code = "ACTUATION_DISABLED"
            command.reason = (
                "Actuation is disabled. Set conveyor.arduino.enabled to true "
                "once the board is connected and bench-verified."
            )
            command.settled_at = now
            return command

        refusal = self.fault.refusal()
        if refusal is not None:
            command.state = CommandState.FAILED
            command.error_code = "HARDWARE_FAULT"
            command.reason = refusal
            command.settled_at = now
            return command

        angle_problem = self.servo_angle_problem()
        if angle_problem is not None:
            command.state = CommandState.FAILED
            command.error_code = "INVALID_SERVO_STATE"
            command.reason = f"Refusing to command a servo: {angle_problem}"
            command.settled_at = now
            self.fault.latch(FaultCode.INVALID_SERVO_STATE, angle_problem, item_id, command_id)
            return command

        if not self.connected:
            command.state = CommandState.FAILED
            command.error_code = "NOT_CONNECTED"
            command.reason = (
                f"No link to the board ({self.transport.state}). "
                f"{getattr(self.transport, 'last_error', '') or ''}".strip()
            )
            command.settled_at = now
            # Deliberately NOT latched. Every other fault here stops the
            # machine because a paddle may be somewhere nobody knows — but
            # this branch returns BEFORE `build_frame` below, so no frame was
            # written and no paddle was asked to move. The physical state is
            # known: unchanged.
            #
            # Latching it meant a board that dropped off USB while idle bricked
            # the machine until a human pressed reset, which on a bench where
            # the link drops every few minutes is a stopped demonstration
            # rather than a safety measure. The item still fails, and says so.
            #
            # ACK_TIMEOUT below is the opposite case and still latches: there
            # the frame WAS written and the paddle may be half out.
            return command

        frame = build_frame(target, item_id, command_id)
        command.raw_frame = frame
        # Before the frame, not after: the gap this covers is the one between
        # the write and the reply, and a marker written afterwards would miss
        # every crash that happens inside it.
        recovery.mark(command_id, target, item_id, path=self.recovery_marker)
        # `finally` rather than a clear before each return: every branch below
        # is terminal, and the one that gets forgotten when a branch is added
        # later is the one that leaves a machine latched on its next start for
        # a command that finished perfectly well.
        try:
            if not self.transport.send(frame):
                command.state = CommandState.FAILED
                command.error_code = "WRITE_FAILED"
                command.reason = getattr(self.transport, "last_error", "the write failed")
                command.settled_at = self._clock()
                self.fault.latch(FaultCode.WRITE_FAILED, command.reason, item_id, command_id)
                return command

            command.state = CommandState.SENT
            command.sent_at = self._clock()
            self._by_item[item_id] = command_id

            response = self._await(command_id)
            command.settled_at = self._clock()
            if response is None:
                command.state = CommandState.TIMED_OUT
                command.reason = (
                    f"No acknowledgement within {self.ack_timeout_s * 1000:.0f} ms. "
                    "Not retried: a blind resend is how one item gets moved twice."
                )
                # Latching: the frame was written, so the paddle may have moved,
                # may be half out, may be jammed. Nothing else may be commanded
                # into a machine whose physical state nobody knows.
                self.fault.latch(FaultCode.ACK_TIMEOUT, command.reason, item_id, command_id)
                return command

            command.raw_response = f"{PROTOCOL} {response.verb} {response.command_id}"
            if response.verb == "ERR":
                command.state = CommandState.FAILED
                command.error_code = response.code
                command.reason = f"The board reported {response.code}."
                self.fault.latch(FaultCode.BOARD_ERROR, command.reason, item_id, command_id)
                return command

            command.state = CommandState.ACKED
            command.reason = (
                "The board acknowledged"
                + (" (already actioned; not moved again)" if response.duplicate else "")
                + ". This is not evidence that the servo physically moved."
            )
            return command
        finally:
            # The outcome is recorded on the command either way. What the
            # marker exists to catch is the case where there is no outcome
            # because the process stopped existing.
            recovery.clear(path=self.recovery_marker)

    def _suppress(self, command_id, item_id, target, now, reason, duplicate_of) -> Command:
        command = Command(
            command_id=command_id,
            item_id=item_id,
            target=target,
            state=CommandState.SUPPRESSED,
            created_at=now,
            settled_at=now,
            reason=reason,
            duplicate_of=duplicate_of,
        )
        return command

    def _await(self, command_id: str) -> Response | None:
        """Wait for this command's reply, ignoring weight frames and noise."""
        deadline = self._clock() + self.ack_timeout_s
        while self._clock() < deadline:
            line = self.transport.receive()
            if line is None:
                continue
            response = parse_response(line)
            if response is not None and response.command_id == command_id:
                return response
        return None

    def snapshot(self) -> dict:
        """Link and command state for the API."""
        recent = sorted(self._commands.values(), key=lambda c: c.created_at or 0.0, reverse=True)[
            :20
        ]
        last = recent[0] if recent else None
        return {
            "state": str(self.transport.state),
            "connected": self.connected,
            "transport": self.transport.name,
            "port": self.cfg["conveyor.arduino.port"],
            "baudrate": self.cfg["conveyor.arduino.baudrate"],
            "actuation_enabled": self.enabled,
            "hardware_mode": "SIMULATION" if self.simulation else "PHYSICAL",
            "fault": self.fault.snapshot(),
            "servo_angle_problem": self.servo_angle_problem(),
            "ack_timeout_ms": self.ack_timeout_s * 1000.0,
            "last_error": getattr(self.transport, "last_error", None),
            "last_command": last.as_dict() if last else None,
            "commands": [c.as_dict() for c in recent],
            "protocol": PROTOCOL,
            "note": (
                "An ACK means the board received a well-formed frame and believes "
                "it acted. Physical servo movement is verified on the bench and "
                "recorded in docs/hardware.md, never inferred from here."
            ),
        }
