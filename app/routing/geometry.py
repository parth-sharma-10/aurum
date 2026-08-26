"""Conveyor geometry, and the arithmetic that turns it into a firing time.

Nothing here talks to hardware. It answers one question — *when* should the
paddle for a given bin move — from numbers somebody measured with a tape and a
stopwatch.

**The timing model, in full:**

    travel_s     = distance_cm / belt_speed_cm_s
    execute_at   = detected_at
                 + travel_s                       when the item arrives
                 - servo_actuation_delay_ms/1000  send early: the paddle takes
                                                  time to physically get there
                 + timing_offset_ms/1000          the calibration knob

Sign convention, stated once and never re-derived: **negative
`timing_offset_ms` fires earlier, positive fires later.** The actuation delay
is *subtracted* because it is time the servo spends moving — to have the paddle
in the stream at arrival, the command has to leave before arrival.

Every term stays a named field on the result. None of them is folded into a
constant, because the whole point of a calibration knob is that somebody can
see what it is currently set to.

**Distances are measured from the camera's field-of-view centre**, along the
belt, in the direction of travel. The prototype treats every item as crossing
that line: correcting for where an item actually sat in the frame needs a
pixel-to-centimetre scale, and that is UNMEASURED. A caller who has measured
one can pass `position_offset_cm` per item.

**Constant configured belt speed is an engineering approximation.** There is no
encoder on this machine, so nothing here measures conveyor velocity — it reads
a number out of configuration and trusts it. That is adequate for a prototype
and is not a claim about the real belt.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from app import config as config_module

#: Which bin each target's distance comes from. Bin C has no entry: it has no
#: servo, and an item reaches it by nobody doing anything.
DISTANCE_KEY = {
    "A": "camera_to_servo_a_cm",
    "B": "camera_to_servo_b_cm",
}


class RoutingMode(StrEnum):
    """Where the geometry came from. Never inferred, always carried."""

    #: Values measured on the physical machine.
    REAL = "REAL"
    #: The demonstration profile. TEST values, never production geometry.
    SIMULATED = "SIMULATED"


def _finite(value) -> float | None:
    """A usable number, or None. Rejects UNMEASURED, NaN, inf and non-numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return None if math.isnan(out) or math.isinf(out) else out


@dataclass(frozen=True)
class Geometry:
    """The physical constants a routing time is computed from."""

    mode: RoutingMode
    belt_speed_cm_s: float | None = None
    camera_to_load_cell_cm: float | None = None
    camera_to_servo_a_cm: float | None = None
    camera_to_servo_b_cm: float | None = None
    servo_actuation_delay_ms: float | None = None
    timing_offset_ms: float = 0.0
    #: Where the belt speed came from, in words. Defaults to the configured
    #: constant this class reads for itself; `app.routing.conveyor` replaces it
    #: with the speed source's own account when a belt is attached, so a
    #: measured speed and a configured one never carry the same explanation.
    belt_speed_basis: str = (
        "configured constant - an engineering approximation. No speed source is "
        "attached, so nothing here measures belt velocity."
    )

    @property
    def simulated(self) -> bool:
        return self.mode is RoutingMode.SIMULATED

    def distance_to(self, target: str) -> float | None:
        key = DISTANCE_KEY.get(target)
        return None if key is None else getattr(self, key)

    def belt_speed_problem(self) -> str | None:
        """Why the belt speed cannot be used, or None if it can.

        Zero and negative are rejected explicitly. A zero would divide by zero;
        a negative would schedule a firing time in the past and look like a
        timing bug rather than the configuration error it is.
        """
        speed = self.belt_speed_cm_s
        if speed is None:
            return "belt speed is UNMEASURED"
        if speed == 0:
            return "belt speed is zero"
        if speed < 0:
            return f"belt speed is negative ({speed})"
        return None

    def travel_time_s(self, distance_cm: float) -> float:
        """Seconds for an item to cover `distance_cm`. Assumes constant speed."""
        return distance_cm / self.belt_speed_cm_s

    def as_dict(self) -> dict:
        return {
            "mode": str(self.mode),
            "belt_speed_cm_s": self.belt_speed_cm_s,
            "camera_to_load_cell_cm": self.camera_to_load_cell_cm,
            "camera_to_servo_a_cm": self.camera_to_servo_a_cm,
            "camera_to_servo_b_cm": self.camera_to_servo_b_cm,
            "servo_actuation_delay_ms": self.servo_actuation_delay_ms,
            "timing_offset_ms": self.timing_offset_ms,
            "belt_speed_basis": self.belt_speed_basis,
            "distance_origin": "camera field-of-view centre, along the belt",
        }

    @classmethod
    def from_config(cls, cfg: config_module.Config | None = None) -> Geometry:
        """The geometry in force: measured values, or the demo profile.

        The simulated profile is reachable *only* when
        `conveyor.runtime.simulation` is true. Without that, an unmeasured
        machine stays unmeasured — TEST distances must never quietly become
        production geometry.
        """
        cfg = config_module.load() if cfg is None else cfg
        # Two ways to be on the demonstration profile, and both are explicit.
        # `conveyor.runtime.simulation` is the whole machine in simulation;
        # `conveyor.mode: SIMULATION` is a demonstration BELT on an otherwise
        # real machine. Either way the mode returned is SIMULATED, so nothing
        # downstream can present these distances as measured.
        simulated = cfg["conveyor.runtime.simulation"] or cfg["conveyor.mode"] == "SIMULATION"
        section = "conveyor.simulation" if simulated else None
        if section is None:
            return cls(
                mode=RoutingMode.REAL,
                belt_speed_cm_s=_finite(cfg["conveyor.belt.speed_cm_s"]),
                camera_to_load_cell_cm=_finite(cfg["conveyor.geometry.camera_to_load_cell_cm"]),
                camera_to_servo_a_cm=_finite(cfg["conveyor.geometry.camera_to_servo_a_cm"]),
                camera_to_servo_b_cm=_finite(cfg["conveyor.geometry.camera_to_servo_b_cm"]),
                servo_actuation_delay_ms=_finite(cfg["conveyor.timing.servo_actuation_delay_ms"]),
                timing_offset_ms=_finite(cfg["conveyor.timing.offset_ms"]) or 0.0,
            )
        return cls(
            mode=RoutingMode.SIMULATED,
            belt_speed_cm_s=_finite(cfg[f"{section}.belt_speed_cm_s"]),
            camera_to_load_cell_cm=_finite(cfg[f"{section}.camera_to_load_cell_cm"]),
            camera_to_servo_a_cm=_finite(cfg[f"{section}.camera_to_servo_a_cm"]),
            camera_to_servo_b_cm=_finite(cfg[f"{section}.camera_to_servo_b_cm"]),
            servo_actuation_delay_ms=_finite(cfg[f"{section}.servo_actuation_delay_ms"]),
            timing_offset_ms=_finite(cfg[f"{section}.timing_offset_ms"]) or 0.0,
        )
