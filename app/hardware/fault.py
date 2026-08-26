"""The latched hardware fault: once something physical goes wrong, nothing moves.

    write failed / no ACK / board gone / bad schedule  ->  LATCHED  ->  no servo

**Latched, not transient.** A link that recovers between two items does not
clear this. A paddle that failed to acknowledge might have moved, might be
half way out, might be jammed against something; the next command would be
issued into a machine whose physical state nobody knows. The fault stays until
a human looks at the rig and resets it, which is the whole difference between
a fault and an error.

**Not everything that fails is a fault.** Bin C sends no frame and is normal.
Actuation being disabled is the shipped state and would otherwise latch the
machine on its first item. A decision the engine could not make is a decision,
not a hardware problem. Only things that mean *the machine is not in a state I
can reason about* belong here.

**Clearing it is an event, not a side effect.** `reset()` records who did it
and when, and the history survives, so "why did nothing move for six items"
has an answer after the demonstration rather than during it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum


class FaultCode(StrEnum):
    """What kind of physical problem latched the machine."""

    #: The link to the board went away.
    ARDUINO_DISCONNECTED = "ARDUINO_DISCONNECTED"
    #: The frame could not be written to the port.
    WRITE_FAILED = "WRITE_FAILED"
    #: No acknowledgement inside the configured window. The paddle's physical
    #: state is now unknown, which is the reason this latches at all.
    ACK_TIMEOUT = "ACK_TIMEOUT"
    #: The board answered ERR.
    BOARD_ERROR = "BOARD_ERROR"
    #: Servo angles outside what the hardware accepts.
    INVALID_SERVO_STATE = "INVALID_SERVO_STATE"
    #: A route reached the hardware boundary that should never have.
    INVALID_SCHEDULE = "INVALID_SCHEDULE"
    #: A belt speed that cannot be used to compute a firing time.
    INVALID_SPEED = "INVALID_SPEED"
    #: The encoder stopped reporting while the machine required it.
    ENCODER_FAILURE = "ENCODER_FAILURE"
    #: The last process died with a command in flight. Same unknown as an
    #: ACK_TIMEOUT, with even less to go on: the timeout leaves a record and a
    #: kill leaves nothing.
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    #: A human hit stop. Not a malfunction, and latched for the same reason
    #: one is: the operator stopped the machine because of something happening
    #: in front of them, and software cannot see whether it is over.
    EMERGENCY_STOP = "EMERGENCY_STOP"


@dataclass(frozen=True)
class Fault:
    """One latching event, with enough context to find the cable."""

    code: FaultCode
    reason: str
    at: float
    item_id: str | None = None
    command_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "code": str(self.code),
            "reason": self.reason,
            "at": self.at,
            "item_id": self.item_id,
            "command_id": self.command_id,
        }


@dataclass
class HardwareFault:
    """The machine's latched physical-fault state.

    Shared by whatever needs to ask "may I move something": the Arduino
    controller checks it before writing a frame, the servo bridge checks it
    before handing a route across, and the dashboard renders it. One object, so
    the three can never disagree about whether the machine is safe.
    """

    clock: object = time.monotonic
    _current: Fault | None = None
    history: list[Fault] = field(default_factory=list)
    #: Every reset, so a run can be reconstructed afterwards.
    resets: list[dict] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self._current is not None

    @property
    def current(self) -> Fault | None:
        return self._current

    def latch(
        self,
        code: FaultCode,
        reason: str,
        item_id: str | None = None,
        command_id: str | None = None,
    ) -> Fault:
        """Record a fault and stop the machine actuating.

        A second fault while one is already latched is recorded in the history
        but does not replace the first: the first is the one that explains
        everything after it, and overwriting it loses the cause in favour of a
        consequence.
        """
        fault = Fault(
            code=code,
            reason=reason,
            at=self.clock(),
            item_id=item_id,
            command_id=command_id,
        )
        self.history.append(fault)
        if self._current is None:
            self._current = fault
        return fault

    def reset(self, by: str = "operator") -> Fault | None:
        """Clear the latch. Returns the fault that was cleared, if any.

        Nothing in this codebase calls this automatically, and nothing should:
        a fault that clears itself is a fault nobody reads.
        """
        cleared = self._current
        if cleared is not None:
            self.resets.append({"by": by, "at": self.clock(), "cleared": cleared.as_dict()})
        self._current = None
        return cleared

    def refusal(self) -> str | None:
        """Why an actuation must not proceed, or None if it may."""
        if self._current is None:
            return None
        return (
            f"A hardware fault is latched ({self._current.code}): "
            f"{self._current.reason} No servo will move until it is reset. The "
            "machine does not know where the paddle physically is."
        )

    def snapshot(self, limit: int = 10) -> dict:
        return {
            "active": self.active,
            "code": str(self._current.code) if self._current else None,
            "reason": self._current.reason if self._current else None,
            "since": self._current.at if self._current else None,
            "item_id": self._current.item_id if self._current else None,
            "faults": len(self.history),
            "history": [f.as_dict() for f in self.history[-limit:][::-1]],
            "resets": self.resets[-limit:][::-1],
            "note": (
                "Latched on purpose. A link that recovers between two items does "
                "not clear this: a command that went unacknowledged may have moved "
                "a paddle, and the next one would be issued into a machine whose "
                "physical state nobody knows."
            ),
        }
