"""The routing scheduler: which item, which paddle, at what moment.

    decision (A/B/C)  ->  routing feasibility  ->  scheduled action  ->  [Phase 7] servo

Three things that look alike and are not:

**A decision is not a route.** `decision = A` says the evidence and the policy
justify the premium stream. It does not say the machine can currently put the
item there. An unmeasured belt makes the item unroutable while leaving the
decision exactly as it was — this module never rewrites an A into a C to
express "I could not schedule it".

**A route is not an actuation.** Nothing here opens a serial port or moves a
servo. It produces a `ScheduledRoute` that Phase 7 consumes. A scheduled route
that becomes DUE means *the moment has arrived*, not *the paddle moved*.

**Bin C is not a target.** There is no Servo C. An item nobody routes reaches
the end of the belt and falls into C, so C produces `NO_ACTION` — the safe
outcome is the machine doing nothing, which is also what happens if this
software crashes.

One physical item gets at most one physical routing action, keyed on the
`AUR-ITEM-` identity the tracker minted. No second identity is created here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from app import config as config_module
from app.routing.geometry import Geometry, RoutingMode


class RouteStatus(StrEnum):
    """Where a routing action stands."""

    #: A time has been computed and is in the future.
    SCHEDULED = "SCHEDULED"
    #: The moment has arrived. Phase 7 acts on this; nothing has moved yet.
    DUE = "DUE"
    #: Handed to the actuation layer and acknowledged.
    EXECUTED = "EXECUTED"
    #: Deliberately no actuator involvement. Bin C's normal outcome.
    NO_ACTION = "NO_ACTION"
    #: Could not be scheduled. `reason_code` says why.
    UNSCHEDULED = "UNSCHEDULED"


class RouteReason(StrEnum):
    """Machine-readable justification. Every route carries exactly one."""

    ROUTE_A = "ROUTE_A"
    ROUTE_B = "ROUTE_B"
    NO_ROUTE_C = "NO_ROUTE_C"

    GEOMETRY_UNMEASURED = "GEOMETRY_UNMEASURED"
    BELT_SPEED_UNMEASURED = "BELT_SPEED_UNMEASURED"
    SERVO_GEOMETRY_UNMEASURED = "SERVO_GEOMETRY_UNMEASURED"
    CAMERA_LOAD_CELL_GEOMETRY_UNMEASURED = "CAMERA_LOAD_CELL_GEOMETRY_UNMEASURED"
    ACTUATION_DELAY_UNMEASURED = "ACTUATION_DELAY_UNMEASURED"

    INVALID_POSITION = "INVALID_POSITION"
    INVALID_DECISION = "INVALID_DECISION"
    ALREADY_ROUTED = "ALREADY_ROUTED"
    STALE_ITEM = "STALE_ITEM"
    TIMING_UNAVAILABLE = "TIMING_UNAVAILABLE"
    TIMING_EXPIRED = "TIMING_EXPIRED"
    #: Handed to the board and not acknowledged. The route is over: a failed
    #: command is not a licence to move the paddle again.
    ACTUATION_FAILED = "ACTUATION_FAILED"


#: Bin to paddle. C is absent on purpose: there is no Servo C.
SERVO_FOR_TARGET = {"A": "SERVO_A", "B": "SERVO_B"}


@dataclass
class ScheduledRoute:
    """One routing action, with every term of its arithmetic on show.

    Enough to answer, without re-deriving anything: which item, what class,
    what decision, which paddle, over what distance, at what belt speed, with
    what delay and offset, when, why, and whether any of it was simulated.
    """

    item_id: str
    decision: str | None
    status: RouteStatus
    reason_code: RouteReason
    reason: str
    mode: RoutingMode
    target: str | None = None
    servo: str | None = None
    component_class: str | None = None
    detected_at: float | None = None
    execute_at: float | None = None
    distance_cm: float | None = None
    belt_speed_cm_s: float | None = None
    travel_time_s: float | None = None
    actuation_delay_ms: float | None = None
    timing_offset_ms: float | None = None
    position_offset_cm: float | None = None
    executed_at: float | None = None

    @property
    def simulated(self) -> bool:
        return self.mode is RoutingMode.SIMULATED

    def seconds_remaining(self, now: float) -> float | None:
        """Countdown for a display. None when nothing is scheduled."""
        return None if self.execute_at is None else self.execute_at - now

    def status_at(self, now: float) -> RouteStatus:
        """SCHEDULED until its moment arrives, then DUE. Never self-executing."""
        if self.status in (RouteStatus.EXECUTED, RouteStatus.NO_ACTION, RouteStatus.UNSCHEDULED):
            return self.status
        return (
            RouteStatus.DUE
            if self.execute_at is not None and now >= self.execute_at
            else (RouteStatus.SCHEDULED)
        )

    def as_dict(self, now: float | None = None) -> dict:
        status = self.status if now is None else self.status_at(now)
        return {
            "item_id": self.item_id,
            "component_class": self.component_class,
            "decision": self.decision,
            "target": self.target,
            "servo": self.servo,
            "status": str(status),
            "reason_code": str(self.reason_code),
            "reason": self.reason,
            "mode": str(self.mode),
            "simulated": self.simulated,
            "detected_at": self.detected_at,
            "execute_at": self.execute_at,
            "seconds_remaining": None if now is None else self.seconds_remaining(now),
            "executed_at": self.executed_at,
            "geometry": {
                "distance_cm": self.distance_cm,
                "belt_speed_cm_s": self.belt_speed_cm_s,
                "travel_time_s": self.travel_time_s,
                "actuation_delay_ms": self.actuation_delay_ms,
                "timing_offset_ms": self.timing_offset_ms,
                "position_offset_cm": self.position_offset_cm,
            },
            "formula": (
                "execute_at = detected_at + distance_cm/belt_speed_cm_s "
                "- actuation_delay_ms/1000 + timing_offset_ms/1000"
            ),
            "note": (
                "A scheduled route is a time, not a movement. Phase 7 turns a "
                "DUE route into a servo command; nothing here actuates."
            ),
        }


class RoutingScheduler:
    """Holds pending routing actions, one per physical item.

    `lifecycle` is anything with `.get(item_id)` — in practice the item
    tracker. When supplied, an item the lifecycle has never heard of is refused
    as stale rather than routed on trust.
    """

    def __init__(
        self,
        geometry: Geometry | None = None,
        lifecycle=None,
        cfg: config_module.Config | None = None,
        conveyor=None,
    ) -> None:
        self.cfg = config_module.load() if cfg is None else cfg
        self.geometry = Geometry.from_config(self.cfg) if geometry is None else geometry
        self.lifecycle = lifecycle
        #: The belt, when there is one. Supplying it is what makes the ETA
        #: dynamic: the speed is re-read on every schedule, so slowing the
        #: belt moves the next item's firing time without a restart.
        self.conveyor = conveyor
        self._routes: dict[str, ScheduledRoute] = {}
        self._rejected: list[ScheduledRoute] = []

    def reset(self) -> None:
        """Drop every scheduled and refused route.

        A pending route is a promise to move a paddle for an object at a
        predicted position. Once the run is over that object is off the bench,
        so firing for it later would be a stroke at nothing.
        """
        self._routes.clear()
        self._rejected.clear()

    def _refresh(self) -> Geometry:
        """Re-read the belt speed from the conveyor, if one is attached.

        A `Geometry` is frozen and carries the speed it was built with. Without
        this the scheduler would keep firing to whatever the belt was doing
        when the process started, which is the failure that looks exactly like
        a timing bug and is not one.
        """
        if self.conveyor is not None:
            self.geometry = self.conveyor.live_geometry()
        return self.geometry

    # -- construction helpers ---------------------------------------------
    def _refuse(self, item_id, decision, code, reason, **extra) -> ScheduledRoute:
        """Record a refusal that is as explainable as a success.

        The intended target is kept. "We wanted A and could not schedule it" is
        a different fact from "there was no target", and collapsing them is
        exactly the decision/routability confusion this layer exists to avoid.
        A target is absent only when the decision named no valid bin.
        """
        route = ScheduledRoute(
            item_id=item_id,
            decision=decision,
            status=RouteStatus.UNSCHEDULED,
            reason_code=code,
            reason=reason,
            mode=self.geometry.mode,
            target=decision if decision in ("A", "B", "C") else None,
            **extra,
        )
        self._rejected.append(route)
        return route

    # -- scheduling --------------------------------------------------------
    def schedule(
        self,
        item_id: str,
        decision,
        detected_at: float,
        component_class: str | None = None,
        position_offset_cm: float | None = None,
        from_load_cell: bool = False,
        now: float | None = None,
    ) -> ScheduledRoute:
        """Turn a decision into a timed routing action, or explain why not.

        `from_load_cell` says the object is on the pan, not at the camera line.
        That is where every automatically routed object actually is - the pan
        machine only routes what it has just weighed - and distances here are
        measured from the camera. Scheduling it as though it were still at the
        camera adds the whole camera-to-pan distance to its travel and fires the
        paddle that much late: 60 cm instead of 35 cm at 10 cm/s is 6.0 s where
        the item arrives at 3.5 s.
        """
        self._refresh()
        target = _target_of(decision)
        now = detected_at if now is None else now

        if target is None:
            return self._refuse(
                item_id,
                _decision_label(decision),
                RouteReason.INVALID_DECISION,
                f"{_decision_label(decision)!r} is not a routable decision.",
                component_class=component_class,
            )

        if not item_id or not isinstance(item_id, str):
            return self._refuse(
                item_id,
                target,
                RouteReason.STALE_ITEM,
                "No item identity was supplied to route.",
                component_class=component_class,
            )

        if self.lifecycle is not None and self.lifecycle.get(item_id) is None:
            return self._refuse(
                item_id,
                target,
                RouteReason.STALE_ITEM,
                f"{item_id} is not in the item lifecycle; refusing to route an "
                "item the tracker does not know about.",
                component_class=component_class,
            )

        # Bin C is the fail-safe: no servo, and reaching it needs no action.
        # Recorded so the item still has an auditable routing outcome.
        if target == "C":
            route = ScheduledRoute(
                item_id=item_id,
                decision=target,
                status=RouteStatus.NO_ACTION,
                reason_code=RouteReason.NO_ROUTE_C,
                reason=(
                    "Bin C has no actuator. The item continues to the end of the "
                    "belt, which is the fail-safe outcome."
                ),
                mode=self.geometry.mode,
                target="C",
                servo=None,
                component_class=component_class,
                detected_at=detected_at,
            )
            if item_id in self._routes:
                return self._already(item_id, target, component_class)
            self._routes[item_id] = route
            return route

        if item_id in self._routes:
            return self._already(item_id, target, component_class)

        if not isinstance(detected_at, (int, float)) or isinstance(detected_at, bool):
            return self._refuse(
                item_id,
                target,
                RouteReason.TIMING_UNAVAILABLE,
                f"Detection time {detected_at!r} is not a number.",
                component_class=component_class,
            )
        if math.isnan(detected_at) or math.isinf(detected_at):
            return self._refuse(
                item_id,
                target,
                RouteReason.TIMING_UNAVAILABLE,
                "Detection time is not finite.",
                component_class=component_class,
            )

        geo = self.geometry
        speed_problem = geo.belt_speed_problem()
        if speed_problem:
            # When a belt is attached its own speed source knows more than
            # "UNMEASURED" - a stale encoder, an unset manual figure, a mode of
            # NONE - and that reason is more use to whoever has to fix it.
            detail = (
                self.conveyor.speed().reason
                if self.conveyor is not None
                else "Measure the belt and set conveyor.belt.speed_cm_s."
            )
            return self._refuse(
                item_id,
                target,
                RouteReason.BELT_SPEED_UNMEASURED,
                f"Cannot compute a routing time: {speed_problem}. {detail}",
                component_class=component_class,
                belt_speed_cm_s=geo.belt_speed_cm_s,
            )

        distance = geo.distance_to(target)
        if distance is None:
            return self._refuse(
                item_id,
                target,
                RouteReason.SERVO_GEOMETRY_UNMEASURED,
                f"The camera-to-servo-{target.lower()} distance is UNMEASURED. "
                "Measure it along the belt and set it in configs/conveyor.yaml.",
                component_class=component_class,
                belt_speed_cm_s=geo.belt_speed_cm_s,
            )
        if distance < 0:
            return self._refuse(
                item_id,
                target,
                RouteReason.SERVO_GEOMETRY_UNMEASURED,
                f"The camera-to-servo-{target.lower()} distance is negative "
                f"({distance}); a servo behind the camera cannot be reached.",
                component_class=component_class,
            )

        if geo.servo_actuation_delay_ms is None:
            return self._refuse(
                item_id,
                target,
                RouteReason.ACTUATION_DELAY_UNMEASURED,
                "The servo actuation delay is UNMEASURED. Time the paddle from "
                "command to in-stream and set conveyor.timing.servo_actuation_delay_ms.",
                component_class=component_class,
                distance_cm=distance,
                belt_speed_cm_s=geo.belt_speed_cm_s,
            )

        if from_load_cell and position_offset_cm is None:
            position_offset_cm = geo.camera_to_load_cell_cm
            if position_offset_cm is None:
                return self._refuse(
                    item_id,
                    target,
                    RouteReason.CAMERA_LOAD_CELL_GEOMETRY_UNMEASURED,
                    "The object is on the load cell, but the camera-to-load-cell "
                    "distance is UNMEASURED. Measure it along the belt and set "
                    "conveyor.geometry.camera_to_load_cell_cm. Assuming zero would "
                    "fire the paddle a whole pan-distance late.",
                    component_class=component_class,
                    distance_cm=distance,
                    belt_speed_cm_s=geo.belt_speed_cm_s,
                )

        offset_cm = 0.0
        if position_offset_cm is not None:
            checked = _finite_number(position_offset_cm)
            if checked is None:
                return self._refuse(
                    item_id,
                    target,
                    RouteReason.INVALID_POSITION,
                    f"Position offset {position_offset_cm!r} is not a usable number.",
                    component_class=component_class,
                )
            offset_cm = checked

        effective = distance - offset_cm
        if effective < 0:
            return self._refuse(
                item_id,
                target,
                RouteReason.INVALID_POSITION,
                f"The item is already past servo {target} by "
                f"{-effective:.2f} cm; there is nothing to schedule.",
                component_class=component_class,
                distance_cm=distance,
                position_offset_cm=offset_cm,
            )

        travel_s = geo.travel_time_s(effective)
        execute_at = (
            detected_at
            + travel_s
            - geo.servo_actuation_delay_ms / 1000.0
            + geo.timing_offset_ms / 1000.0
        )

        if execute_at < now:
            # Never fire late to catch up: the item has already gone past, and a
            # paddle moving behind it either hits nothing or hits the next item.
            return self._refuse(
                item_id,
                target,
                RouteReason.TIMING_EXPIRED,
                f"The firing time was {now - execute_at:.3f}s ago. Refusing to "
                "fire late: the item has passed, and a catch-up would strike "
                "whatever is behind it.",
                component_class=component_class,
                detected_at=detected_at,
                execute_at=execute_at,
                distance_cm=effective,
                belt_speed_cm_s=geo.belt_speed_cm_s,
                travel_time_s=travel_s,
                actuation_delay_ms=geo.servo_actuation_delay_ms,
                timing_offset_ms=geo.timing_offset_ms,
            )

        route = ScheduledRoute(
            item_id=item_id,
            decision=target,
            status=RouteStatus.SCHEDULED,
            reason_code=RouteReason.ROUTE_A if target == "A" else RouteReason.ROUTE_B,
            reason=(
                f"{effective:.2f} cm at {geo.belt_speed_cm_s:.2f} cm/s is "
                f"{travel_s:.3f} s of travel; firing "
                f"{geo.servo_actuation_delay_ms:.0f} ms early for the paddle, "
                f"offset {geo.timing_offset_ms:+.0f} ms."
            ),
            mode=geo.mode,
            target=target,
            servo=SERVO_FOR_TARGET[target],
            component_class=component_class,
            detected_at=detected_at,
            execute_at=execute_at,
            distance_cm=effective,
            belt_speed_cm_s=geo.belt_speed_cm_s,
            travel_time_s=travel_s,
            actuation_delay_ms=geo.servo_actuation_delay_ms,
            timing_offset_ms=geo.timing_offset_ms,
            position_offset_cm=offset_cm if position_offset_cm is not None else None,
        )
        self._routes[item_id] = route
        return route

    def _already(self, item_id, target, component_class) -> ScheduledRoute:
        existing = self._routes[item_id]
        return self._refuse(
            item_id,
            target,
            RouteReason.ALREADY_ROUTED,
            f"{item_id} already has a routing action "
            f"({existing.status}, target {existing.target}). One physical item "
            "gets one physical routing action.",
            component_class=component_class,
        )

    # -- queue -------------------------------------------------------------
    def get(self, item_id: str) -> ScheduledRoute | None:
        return self._routes.get(item_id)

    def pending(self) -> list[ScheduledRoute]:
        """Routes awaiting their moment, soonest first."""
        return sorted(
            (r for r in self._routes.values() if r.status is RouteStatus.SCHEDULED),
            key=lambda r: r.execute_at,
        )

    def due(self, now: float) -> list[ScheduledRoute]:
        """Routes whose moment has arrived and that nobody has executed yet."""
        return [r for r in self.pending() if now >= r.execute_at]

    def mark_executed(self, item_id: str, at: float | None = None) -> ScheduledRoute | None:
        """Record that the actuation layer took this route. Idempotent."""
        route = self._routes.get(item_id)
        if route is None or route.status is not RouteStatus.SCHEDULED:
            return route
        route.status = RouteStatus.EXECUTED
        route.executed_at = at
        return route

    def abandon(self, item_id: str, reason_code: RouteReason, reason: str) -> ScheduledRoute | None:
        """Take a route out of the queue that will never be actuated.

        Only an acknowledgement moves a route to EXECUTED, so a route the
        actuation layer failed or refused as late stays SCHEDULED for ever -
        `pending()` and `due()` keep offering it, and the dashboard shows a
        countdown for a paddle nobody will ever fire. `ServoActuator` already
        refuses to attempt it twice; this is the queue learning the same thing.

        The reason is kept, so the run's record still says what happened rather
        than the route simply vanishing.
        """
        route = self._routes.get(item_id)
        if route is None or route.status is not RouteStatus.SCHEDULED:
            return route
        route.status = RouteStatus.UNSCHEDULED
        route.reason_code = reason_code
        route.reason = reason
        self._rejected.append(route)
        return route

    def rejected(self) -> list[ScheduledRoute]:
        """Refusals, kept so a demo can show what was not routed and why."""
        return list(self._rejected)

    def snapshot(self, now: float) -> dict:
        """Everything an API or a dashboard needs about the routing queue."""
        self._refresh()
        return {
            "mode": str(self.geometry.mode),
            "simulated": self.geometry.simulated,
            "geometry": self.geometry.as_dict(),
            "routable": self.geometry.belt_speed_problem() is None,
            "pending": [r.as_dict(now) for r in self.pending()],
            "due": [r.as_dict(now) for r in self.due(now)],
            "routes": [r.as_dict(now) for r in self._routes.values()],
            "rejected": [r.as_dict(now) for r in self._rejected],
        }


def _finite_number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return None if math.isnan(out) or math.isinf(out) else out


def _decision_label(decision) -> str | None:
    """The bin letter a decision names, whatever shape it arrives in."""
    if decision is None:
        return None
    inner = getattr(decision, "decision", decision)
    return str(inner) if inner is not None else None


def _target_of(decision) -> str | None:
    label = _decision_label(decision)
    return label if label in ("A", "B", "C") else None
