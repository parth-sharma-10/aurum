"""Where the belt speed comes from, and how honest that answer is.

`app.routing.geometry` computes a firing time from a speed. This module is the
one place that decides what that speed *is*, and refuses to let the three very
different ways of knowing it be mistaken for each other:

    SIMULATION   a configured demo number. Not a measurement of anything.
    ENCODER      pulses off the real roller, differentiated over real time.
    MANUAL       a figure somebody measured with a tape and a stopwatch and
                 typed in. Real, but not live: it cannot notice the belt
                 slowing under load.
    NONE         there is no belt. The shipped default, because there is no
                 belt.

Every read returns a `BeltSpeed` carrying its status, so a dashboard can say
`SPEED 0.10 m/s (SIMULATED)` and never `SPEED 0.10 m/s`.

**The speed is read at scheduling time, not at start-up.** That is what makes
the ETA dynamic: `RoutingScheduler` asks this module for the current speed
each time it schedules, so a belt that has been slowed produces a later firing
time for the next item without anything being restarted. A speed captured once
into a frozen `Geometry` would silently keep firing to yesterday's belt.

**An encoder that has stopped reporting is not a belt that has stopped.** It
is an encoder nobody can hear, and the two must not be collapsed: a speed of
zero divides by zero and a stale speed fires early. `EncoderSpeed` reports
UNAVAILABLE past its timeout and the scheduler then refuses to route, which
sends the item to C. That is the fail-closed direction.

Nothing here talks to hardware. `EncoderSpeed.update()` is fed pulse counts by
whatever is reading them — the Arduino link today, a GPIO callback tomorrow —
so the arithmetic is testable without a roller.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from enum import StrEnum

from app import config as config_module
from app.routing.geometry import Geometry, RoutingMode

#: Metres per second is what the specification and the dashboard speak;
#: centimetres per second is what every existing config key and every existing
#: test speaks. Converted here rather than in either.
CM_PER_M = 100.0


class ConveyorMode(StrEnum):
    """How this machine moves items past the camera, if it does at all."""

    #: No belt. The operator carries the object from the camera to the pan and
    #: routing is immediate. The shipped default, and what the rig actually is.
    NONE = "NONE"
    #: A demonstration belt: configured speed, configured distances, every
    #: figure derived from it stamped SIMULATED.
    SIMULATION = "SIMULATION"
    #: A real belt with a rotary encoder on the roller.
    ENCODER = "ENCODER"
    #: A real belt with no encoder, running at a speed somebody measured once.
    MANUAL = "MANUAL"


class SpeedStatus(StrEnum):
    """What kind of number the belt speed is."""

    #: Differentiated from real encoder pulses.
    MEASURED = "MEASURED"
    #: A configured demonstration value. Not a measurement.
    SIMULATED = "SIMULATED"
    #: Measured once by hand and entered. Real, but not live.
    MANUAL = "MANUAL"
    #: The encoder has not reported inside its timeout.
    STALE = "STALE"
    #: No speed at all. The scheduler refuses and the item reaches C.
    UNAVAILABLE = "UNAVAILABLE"


#: Statuses a routing time may be computed from. STALE is excluded on purpose:
#: an encoder that went quiet is not a belt whose speed is still known, and
#: firing on its last reading is how a paddle strikes the next item along.
USABLE = frozenset({SpeedStatus.MEASURED, SpeedStatus.SIMULATED, SpeedStatus.MANUAL})


@dataclass(frozen=True)
class BeltSpeed:
    """One reading of how fast the belt is moving, and how that is known."""

    cm_s: float | None
    status: SpeedStatus
    source: str
    reason: str
    at: float | None = None
    #: Encoder only: how many pulses this reading was differentiated over.
    pulses: int | None = None

    @property
    def usable(self) -> bool:
        return self.status in USABLE and bool(self.cm_s) and self.cm_s > 0

    @property
    def m_s(self) -> float | None:
        return None if self.cm_s is None else self.cm_s / CM_PER_M

    def as_dict(self) -> dict:
        return {
            "cm_s": self.cm_s,
            "m_s": self.m_s,
            "status": str(self.status),
            "source": self.source,
            "reason": self.reason,
            "usable": self.usable,
            "at": self.at,
            "pulses": self.pulses,
        }


UNKNOWN_SPEED = BeltSpeed(
    cm_s=None,
    status=SpeedStatus.UNAVAILABLE,
    source="none",
    reason="No belt speed source is configured.",
)


class SimulatedSpeed:
    """A configured demonstration speed. Never a measurement.

    Exists so that an unmeasured machine can still show the whole timing model
    working, which is a different thing from claiming a belt was measured. The
    status it returns says which, on every reading, for ever.
    """

    name = "simulation"

    def __init__(self, cm_s: float, clock=time.monotonic) -> None:
        self.cm_s = cm_s
        self._clock = clock

    def read(self) -> BeltSpeed:
        if not _positive(self.cm_s):
            return replace(
                UNKNOWN_SPEED,
                source=self.name,
                reason=(
                    f"The simulated belt speed is {self.cm_s!r}, which is not a usable "
                    "speed. Set conveyor.simulation.belt_speed_cm_s."
                ),
            )
        return BeltSpeed(
            cm_s=float(self.cm_s),
            status=SpeedStatus.SIMULATED,
            source=self.name,
            reason=(
                f"SIMULATED belt speed {self.cm_s / CM_PER_M:.2f} m/s from "
                "conveyor.simulation.belt_speed_cm_s. Nothing was measured to get "
                "this; it is a demonstration value."
            ),
            at=self._clock(),
        )


class ManualSpeed:
    """A speed somebody measured by hand and entered. Real, but not live."""

    name = "manual"

    def __init__(self, cm_s: float | None, clock=time.monotonic) -> None:
        self.cm_s = cm_s
        self._clock = clock

    def set(self, cm_s: float | None) -> BeltSpeed:
        """Enter a new hand-measured speed. Returns the resulting reading."""
        self.cm_s = cm_s
        return self.read()

    def read(self) -> BeltSpeed:
        if not _positive(self.cm_s):
            return replace(
                UNKNOWN_SPEED,
                source=self.name,
                reason=(
                    "No hand-measured belt speed has been entered. Time a mark over a "
                    "measured length and set conveyor.manual.belt_speed_cm_s."
                ),
            )
        return BeltSpeed(
            cm_s=float(self.cm_s),
            status=SpeedStatus.MANUAL,
            source=self.name,
            reason=(
                f"Hand-measured belt speed {self.cm_s / CM_PER_M:.2f} m/s. Real, but "
                "entered once: it cannot notice the belt slowing under load."
            ),
            at=self._clock(),
        )


class EncoderSpeed:
    """Roller pulses differentiated into a belt speed.

        distance_per_pulse = roller_circumference_cm / pulses_per_revolution
        speed              = (pulses since last sample x distance_per_pulse)
                             / (seconds since last sample)

    Two samples are needed before there is a speed at all: one count is a
    position, not a velocity. Until the second arrives the reading is
    UNAVAILABLE, which is what it is.

    The geometry is configuration rather than an assumed encoder model,
    because nobody has bought one yet. An `UNMEASURED` circumference makes
    every reading UNAVAILABLE, so a missing tape measurement stops the belt
    being routed on rather than being papered over with a plausible default.
    """

    name = "encoder"

    def __init__(
        self,
        pulses_per_revolution: int,
        roller_circumference_cm: float | None,
        timeout_s: float = 2.0,
        clock=time.monotonic,
    ) -> None:
        self.pulses_per_revolution = pulses_per_revolution
        self.roller_circumference_cm = roller_circumference_cm
        self.timeout_s = timeout_s
        self._clock = clock
        self._last: tuple[int, float] | None = None
        self._speed: BeltSpeed | None = None
        self.updates = 0

    @property
    def distance_per_pulse_cm(self) -> float | None:
        if not _positive(self.roller_circumference_cm) or self.pulses_per_revolution <= 0:
            return None
        return float(self.roller_circumference_cm) / self.pulses_per_revolution

    def _problem(self) -> str | None:
        if not _positive(self.roller_circumference_cm):
            return (
                "The roller circumference is UNMEASURED, so a pulse count cannot become "
                "a distance. Measure it and set conveyor.encoder.roller_circumference_cm."
            )
        if self.pulses_per_revolution <= 0:
            return (
                f"conveyor.encoder.pulses_per_revolution is {self.pulses_per_revolution}, "
                "which cannot divide a revolution."
            )
        return None

    def update(self, pulses: int, at: float | None = None) -> BeltSpeed:
        """Feed a cumulative pulse count. Returns the resulting reading.

        Cumulative rather than incremental on purpose: a dropped update then
        costs accuracy over one interval instead of losing distance for ever.
        """
        at = self._clock() if at is None else at
        problem = self._problem()
        if problem is not None:
            self._speed = replace(UNKNOWN_SPEED, source=self.name, reason=problem)
            return self._speed

        self.updates += 1
        previous = self._last
        self._last = (int(pulses), float(at))
        if previous is None:
            self._speed = replace(
                UNKNOWN_SPEED,
                source=self.name,
                reason=(
                    "One pulse count is a position, not a velocity. Waiting for a second sample."
                ),
                at=at,
            )
            return self._speed

        last_count, last_at = previous
        elapsed = float(at) - last_at
        if elapsed <= 0:
            self._speed = replace(
                UNKNOWN_SPEED,
                source=self.name,
                reason=(
                    f"Two encoder samples {elapsed:.4f}s apart cannot be differentiated. "
                    "Check the sampling clock."
                ),
                at=at,
            )
            return self._speed

        delta = int(pulses) - last_count
        if delta < 0:
            # A counter that went backwards has wrapped or been reset. Treating
            # the wrap as reverse travel would schedule a firing time in the
            # past, so this interval is discarded and the next one is used.
            self._speed = replace(
                UNKNOWN_SPEED,
                source=self.name,
                reason=(
                    f"The pulse count went backwards ({last_count} -> {pulses}); the "
                    "counter wrapped or was reset. This interval is discarded."
                ),
                at=at,
            )
            return self._speed

        cm_s = delta * self.distance_per_pulse_cm / elapsed
        self._speed = BeltSpeed(
            cm_s=cm_s,
            status=SpeedStatus.MEASURED if cm_s > 0 else SpeedStatus.UNAVAILABLE,
            source=self.name,
            reason=(
                f"{delta} pulses over {elapsed:.3f}s at {self.distance_per_pulse_cm:.4f} cm/pulse."
                if cm_s > 0
                else f"The belt has not moved: {delta} pulses over {elapsed:.3f}s."
            ),
            at=at,
            pulses=delta,
        )
        return self._speed

    def read(self) -> BeltSpeed:
        """The last differentiated speed, or why there is not one."""
        problem = self._problem()
        if problem is not None:
            return replace(UNKNOWN_SPEED, source=self.name, reason=problem)
        if self._speed is None:
            return replace(
                UNKNOWN_SPEED,
                source=self.name,
                reason=(
                    "The encoder has never reported. Nothing is assumed about a belt "
                    "nobody can hear."
                ),
            )
        age = self._clock() - (self._speed.at or 0.0)
        if self._speed.at is not None and age > self.timeout_s:
            return replace(
                self._speed,
                status=SpeedStatus.STALE,
                reason=(
                    f"The last encoder sample was {age:.1f}s ago, past the "
                    f"{self.timeout_s:.1f}s health timeout. A belt nobody can hear is "
                    "not a belt whose speed is known."
                ),
            )
        return self._speed

    def snapshot(self) -> dict:
        return {
            "pulses_per_revolution": self.pulses_per_revolution,
            "roller_circumference_cm": self.roller_circumference_cm,
            "distance_per_pulse_cm": self.distance_per_pulse_cm,
            "timeout_s": self.timeout_s,
            "updates": self.updates,
            "last_count": None if self._last is None else self._last[0],
            "healthy": self.read().status is SpeedStatus.MEASURED,
        }


class Conveyor:
    """The belt: a mode, a speed source, and the geometry it feeds.

    One object the session, the scheduler and the dashboard all read, so
    "which mode are we in" is answered in one place rather than by three
    different config lookups that can disagree.
    """

    def __init__(
        self,
        mode: ConveyorMode,
        source=None,
        geometry: Geometry | None = None,
        cfg: config_module.Config | None = None,
    ) -> None:
        self.cfg = config_module.load() if cfg is None else cfg
        self.mode = mode
        self.source = source
        self.geometry = Geometry.from_config(self.cfg) if geometry is None else geometry

    @classmethod
    def from_config(cls, cfg: config_module.Config | None = None) -> Conveyor:
        cfg = config_module.load() if cfg is None else cfg
        mode = ConveyorMode(cfg["conveyor.mode"])
        return cls(mode=mode, source=_source_for(mode, cfg), cfg=cfg, geometry=None)

    @property
    def present(self) -> bool:
        """True when a belt exists at all. False is the shipped state."""
        return self.mode is not ConveyorMode.NONE

    @property
    def simulated(self) -> bool:
        return self.mode is ConveyorMode.SIMULATION

    def speed(self) -> BeltSpeed:
        if self.source is None:
            return replace(
                UNKNOWN_SPEED,
                reason=(
                    "conveyor.mode is NONE: this machine has no belt. The operator "
                    "carries the object from the camera to the pan and routing is "
                    "immediate."
                ),
            )
        return self.source.read()

    def live_geometry(self) -> Geometry:
        """The configured geometry with the CURRENT speed substituted in.

        This is the whole dynamic-ETA mechanism. `Geometry` is frozen and
        carries a speed; asking for it again produces a new one carrying the
        speed the belt has now, so an ETA computed from it moves when the belt
        does.
        """
        speed = self.speed()
        return replace(
            self.geometry,
            belt_speed_cm_s=speed.cm_s if speed.usable else None,
            belt_speed_basis=f"{speed.status} via {speed.source}: {speed.reason}",
        )

    def eta_seconds(self, distance_cm: float, speed: BeltSpeed | None = None) -> float | None:
        """Seconds for an item to cover `distance_cm` at the current speed.

            ETA = (actuator_position - object_position) / current_speed

        `None` when the speed is not usable, never a number. A default speed
        substituted here would be the exact fabrication this module exists to
        prevent.
        """
        speed = self.speed() if speed is None else speed
        if not speed.usable or not _finite(distance_cm) or distance_cm < 0:
            return None
        return distance_cm / speed.cm_s

    def snapshot(self) -> dict:
        """Everything the dashboard's conveyor panel renders."""
        speed = self.speed()
        geometry = self.live_geometry()
        return {
            "present": self.present,
            "mode": str(self.mode),
            "speed_source": getattr(self.source, "name", "none"),
            "speed": speed.as_dict(),
            "encoder": (self.source.snapshot() if isinstance(self.source, EncoderSpeed) else None),
            "geometry": geometry.as_dict(),
            "routable": geometry.belt_speed_problem() is None,
            "eta_to_servo_a_s": self.eta_seconds(geometry.camera_to_servo_a_cm or -1.0, speed),
            "eta_to_servo_b_s": self.eta_seconds(geometry.camera_to_servo_b_cm or -1.0, speed),
            "eta_to_load_cell_s": self.eta_seconds(geometry.camera_to_load_cell_cm or -1.0, speed),
            "note": (
                "No belt exists on this machine. The operator carries the object "
                "from the camera to the pan; the load cell starts the measurement "
                "and the routing is immediate, not scheduled."
                if not self.present
                else "Distances are measured from the camera field-of-view centre, "
                "along the belt, in the direction of travel."
            ),
        }


def _source_for(mode: ConveyorMode, cfg: config_module.Config):
    if mode is ConveyorMode.SIMULATION:
        return SimulatedSpeed(cfg["conveyor.simulation.belt_speed_cm_s"])
    if mode is ConveyorMode.MANUAL:
        return ManualSpeed(_number(cfg["conveyor.manual.belt_speed_cm_s"]))
    if mode is ConveyorMode.ENCODER:
        return EncoderSpeed(
            pulses_per_revolution=cfg["conveyor.encoder.pulses_per_revolution"],
            roller_circumference_cm=_number(cfg["conveyor.encoder.roller_circumference_cm"]),
            timeout_s=cfg["conveyor.encoder.timeout_s"],
        )
    return None


def _number(value) -> float | None:
    """A usable number, or None. UNMEASURED and NaN both become None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return None if math.isnan(out) or math.isinf(out) else out


def _finite(value) -> bool:
    return _number(value) is not None


def _positive(value) -> bool:
    number = _number(value)
    return number is not None and number > 0


def hardware_mode(cfg: config_module.Config | None = None) -> str:
    """SIMULATION or PHYSICAL — the one switch that decides whether hardware
    may be commanded at all. Kept here so the dashboard, the session and the
    actuation layer all read the same answer."""
    cfg = config_module.load() if cfg is None else cfg
    return "SIMULATION" if cfg["conveyor.runtime.simulation"] else "PHYSICAL"


__all__ = [
    "BeltSpeed",
    "Conveyor",
    "ConveyorMode",
    "EncoderSpeed",
    "ManualSpeed",
    "RoutingMode",
    "SimulatedSpeed",
    "SpeedStatus",
    "hardware_mode",
]
