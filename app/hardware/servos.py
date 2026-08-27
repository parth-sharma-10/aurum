"""The bridge: a DUE route becomes a servo command, or explicitly does not.

    scheduler.due(now) -> ArduinoController.move() -> ACK -> scheduler.mark_executed()

This is the only place where software routing meets physical actuation, and it
is deliberately small. It computes nothing: distances, speeds and firing times
all come from the Phase 6 scheduler, and the protocol comes from
`app.hardware.arduino`. Its whole job is to decide *whether* to hand a route
across, and to record what came back.

Three rules it exists to enforce:

**Only an acknowledged command marks a route executed.** A `TIMED_OUT` or
`FAILED` command leaves the route unexecuted and honest about it.

**Nothing is ever attempted twice.** A route that has been handed across once
is never handed across again, whatever its outcome. Combined with the
controller's per-item command suppression and the sketch's recent-id list,
there are three independent barriers between one item and two paddle
movements — which is the failure that matters, because a second movement lands
on whatever is behind the first item.

**Bin C is not actuated.** It has no servo, its routes carry `NO_ACTION`, and
no frame is ever written for one.

An ACK still does not prove a servo moved. See `docs/hardware.md`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

from app import config as config_module
from app.hardware import verification
from app.hardware.arduino import ArduinoController, Command, CommandState
from app.hardware.fault import FaultCode
from app.routing.scheduler import RouteReason, RouteStatus, ScheduledRoute


class ActuationOutcome(StrEnum):
    """What happened when a route reached the hardware boundary."""

    #: Commanded and acknowledged. The route is now EXECUTED.
    ACTUATED = "ACTUATED"
    #: Bin C. No frame was written, on purpose.
    NO_ACTION = "NO_ACTION"
    #: Commanded, but the board did not acknowledge or reported an error.
    FAILED = "FAILED"
    #: Deliberately not attempted: already handed across, or not actionable.
    SKIPPED = "SKIPPED"
    #: Its moment passed while something else was being commanded. Refused
    #: rather than fired late, because a late paddle strikes the next item.
    EXPIRED = "EXPIRED"
    #: Refused because a hardware fault is latched. Nothing was sent.
    BLOCKED = "BLOCKED"


@dataclass
class ActuationResult:
    """One route's trip to the hardware boundary, and what came back."""

    item_id: str
    target: str | None
    outcome: ActuationOutcome
    reason: str
    command: Command | None = None
    route_status: str | None = None
    at: float | None = None

    def as_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "target": self.target,
            "servo": f"SERVO_{self.target}" if self.target in ("A", "B") else None,
            "outcome": str(self.outcome),
            "reason": self.reason,
            "route_status": self.route_status,
            "at": self.at,
            "command": self.command.as_dict() if self.command else None,
            "ack_meaning": (
                "ACTUATED means the board acknowledged a well-formed frame. It is "
                "not evidence that a servo physically moved."
            ),
        }


class ServoActuator:
    """Drains due routes into servo commands, once each.

    Holds no geometry and no timing of its own: `scheduler.due(now)` decides
    which routes have arrived, and this decides whether each one may cross.
    """

    def __init__(
        self,
        scheduler,
        controller: ArduinoController | None = None,
        cfg: config_module.Config | None = None,
        clock=time.monotonic,
    ) -> None:
        self.cfg = config_module.load() if cfg is None else cfg
        self.scheduler = scheduler
        self.controller = ArduinoController(cfg=self.cfg) if controller is None else controller
        self._clock = clock
        #: The same latch the controller checks, so the bridge and the board
        #: layer can never disagree about whether the machine may move.
        self.fault = self.controller.fault
        #: Items already handed to the hardware boundary, whatever the outcome.
        #: A failure is not a licence to try again.
        self._attempted: set[str] = set()
        self.results: list[ActuationResult] = []

    @property
    def servo_settings(self) -> dict:
        """The bench angles, carried so a report can show what the board runs."""
        return {
            "rest_angle_deg": self.cfg["conveyor.servo.rest_angle_deg"],
            "push_angle_deg": self.cfg["conveyor.servo.push_angle_deg"],
            "actuation_ms": self.cfg["conveyor.servo.actuation_ms"],
            "basis": (
                "BENCH/TEST values from independent servo testing. Not calibrated "
                "against a conveyor - no conveyor exists."
            ),
        }

    @property
    def late_tolerance_s(self) -> float:
        return self.cfg["conveyor.routing.late_tolerance_ms"] / 1000.0

    def late_by(self, route: ScheduledRoute, now: float) -> float | None:
        """How far past its moment this route is, or None if it is still good.

        None for a route with no firing time at all: Bin C and every refusal
        carry no `execute_at`, and they are refused for their own reasons
        further down rather than as late.
        """
        if route.execute_at is None:
            return None
        late_by = now - route.execute_at
        return late_by if late_by > self.late_tolerance_s else None

    def _record(self, result: ActuationResult) -> ActuationResult:
        self.results.append(result)
        return result

    def actuate(self, route: ScheduledRoute, now: float | None = None) -> ActuationResult:
        """Hand one route to the board, or say why it was not handed across."""
        now = self._clock() if now is None else now

        if route.item_id in self._attempted:
            return self._record(
                ActuationResult(
                    route.item_id,
                    route.target,
                    ActuationOutcome.SKIPPED,
                    "Already handed to the hardware boundary once. A failed "
                    "command is not a licence to move the paddle again.",
                    route_status=str(route.status),
                    at=now,
                )
            )

        # Bin C, and every refusal the scheduler recorded, stop here. A route
        # the scheduler would not schedule - TIMING_EXPIRED above all - must
        # never reach the board: a late paddle strikes the next item along.
        if route.status is RouteStatus.NO_ACTION:
            self._attempted.add(route.item_id)
            return self._record(
                ActuationResult(
                    route.item_id,
                    route.target,
                    ActuationOutcome.NO_ACTION,
                    "Bin C has no actuator. No command was sent.",
                    route_status=str(route.status),
                    at=now,
                )
            )
        if route.status is not RouteStatus.SCHEDULED:
            return self._record(
                ActuationResult(
                    route.item_id,
                    route.target,
                    ActuationOutcome.SKIPPED,
                    f"Route status is {route.status} ({route.reason_code}); only a "
                    "SCHEDULED route may be actuated.",
                    route_status=str(route.status),
                    at=now,
                )
            )

        # A latched fault stops the machine here as well as at the board layer.
        # Twice on purpose: this one keeps the item out of `_attempted`, so a
        # route blocked by a fault can still be actuated after a reset, whereas
        # one that reached the board never can.
        refusal = self.fault.refusal()
        if refusal is not None:
            return self._record(
                ActuationResult(
                    route.item_id,
                    route.target,
                    ActuationOutcome.BLOCKED,
                    refusal,
                    route_status=str(route.status),
                    at=now,
                )
            )

        # The scheduler refuses to SCHEDULE a route whose moment has passed.
        # Nothing refused to ACTUATE one, and the board blocks for the whole
        # stroke: three routes due together were commanded 1.2 s apart, and the
        # last of them fired 45 cm of belt past its bin reporting ACTUATED.
        # This is the same rule as TIMING_EXPIRED, applied where the delay
        # actually accumulates.
        late_by = self.late_by(route, now)
        if late_by is not None:
            self._attempted.add(route.item_id)
            # Out of the queue as well as out of `_attempted`. Only an ACK moves
            # a route to EXECUTED, so without this it stays SCHEDULED for ever
            # and the dashboard counts down to a stroke nobody will make.
            self.scheduler.abandon(
                route.item_id,
                RouteReason.TIMING_EXPIRED,
                f"Refused as {late_by:.3f}s late at the hardware boundary.",
            )
            return self._record(
                ActuationResult(
                    route.item_id,
                    route.target,
                    ActuationOutcome.EXPIRED,
                    f"Its moment was {late_by:.3f}s ago, past the "
                    f"{self.late_tolerance_s:.3f}s tolerance. Refusing to fire late: "
                    "the item has passed, and a catch-up strikes whatever is behind "
                    "it. The item is not sorted, and says so.",
                    route_status=str(route.status),
                    at=now,
                )
            )

        if route.servo is None or route.target not in ("A", "B"):
            # A SCHEDULED route with no paddle should be unreachable. If one
            # arrives, the scheduler and this layer disagree about the machine,
            # and that is exactly the state to stop in rather than write into.
            self.fault.latch(
                FaultCode.INVALID_SCHEDULE,
                f"A SCHEDULED route for {route.item_id} named target {route.target!r}, "
                "which is not an actuator.",
                route.item_id,
            )
            return self._record(
                ActuationResult(
                    route.item_id,
                    route.target,
                    ActuationOutcome.FAILED,
                    f"Target {route.target!r} is not an actuator; refusing to guess.",
                    route_status=str(route.status),
                    at=now,
                )
            )
        if route.execute_at is None or now < route.execute_at:
            return self._record(
                ActuationResult(
                    route.item_id,
                    route.target,
                    ActuationOutcome.SKIPPED,
                    "The firing moment has not arrived.",
                    route_status=str(route.status),
                    at=now,
                )
            )

        self._attempted.add(route.item_id)
        command = self.controller.move(route.target, route.item_id)

        if command.state is CommandState.ACKED:
            self.scheduler.mark_executed(route.item_id, at=now)
            return self._record(
                ActuationResult(
                    route.item_id,
                    route.target,
                    ActuationOutcome.ACTUATED,
                    command.reason or "The board acknowledged.",
                    command=command,
                    route_status=str(RouteStatus.EXECUTED),
                    at=now,
                )
            )

        # Not acknowledged: the route is never recorded as done on the
        # strength of a write that returned - but it does leave the queue, so
        # the failure is a finished outcome rather than a permanent countdown.
        self.scheduler.abandon(
            route.item_id,
            RouteReason.ACTUATION_FAILED,
            command.reason or f"The command ended {command.state}.",
        )
        return self._record(
            ActuationResult(
                route.item_id,
                route.target,
                ActuationOutcome.FAILED,
                command.reason or f"The command ended {command.state}.",
                command=command,
                route_status=str(route.status),
                at=now,
            )
        )

    def execute_due(self, now: float | None = None) -> list[ActuationResult]:
        """Actuate every route whose moment has arrived, once each.

        A route that failed at the board stays SCHEDULED - the only thing that
        clears it is an acknowledgement - so `scheduler.due()` keeps offering
        it. Already-attempted items are dropped here rather than being handed
        to `actuate()` for a SKIPPED result: the machine loop runs at 20 Hz,
        and every SKIPPED it returns became another EPR failure row and another
        error-log entry for the same one item. `actuate()` keeps its own guard
        for a direct call.
        """
        now = self._clock() if now is None else now
        # Each route is judged at the moment it is actually reached, not at the
        # moment the batch opened. `actuate` blocks for the whole stroke -
        # 1.212 s measured on the attached board - so the second route in a
        # batch is reached more than a second after the first, and sharing one
        # timestamp let it fire long after its window while still looking on
        # time. The caller's `now` stays the frame of reference and elapsed
        # time is added to it, so a caller that supplies its own clock is not
        # quietly measured against a different one.
        started = self._clock()
        results = []
        for route in self.scheduler.due(now):
            if route.item_id in self._attempted:
                continue
            results.append(self.actuate(route, now=now + (self._clock() - started)))
        return results

    def snapshot(self, limit: int = 20) -> dict:
        """Recent actuation history, for the API."""
        recent = self.results[-limit:][::-1]
        return {
            "arduino": self.controller.snapshot(),
            "servo": self.servo_settings,
            "fault": self.fault.snapshot(),
            "attempted_items": len(self._attempted),
            "last_actuation": recent[0].as_dict() if recent else None,
            "recent": [r.as_dict() for r in recent],
            # Was a paragraph of prose that nothing could check and nothing
            # could render as a state, and which went stale the day a servo was
            # first commanded. This is the same claim with a shape.
            "movement_verification": verification.snapshot(),
        }
