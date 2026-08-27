"""The pan state machine: the load cell drives the machine, not a button.

    WAITING_FOR_OBJECT -> OBJECT_PRESENT -> WEIGHING -> WEIGHT_STABLE
        -> PROCESSING -> ROUTING -> WAITING_FOR_CLEAR -> WAITING_FOR_OBJECT

An operator places an assembly on the pan and does nothing else. The cell sees
the mass arrive, the reading settles, the estimate and the decision run, the
paddle moves, and the machine waits for the object to be taken away before it
will accept another.

**The stability judgement is not here.** `app.weight.WeightSensor` already
owns it - median filter, tolerance, window, calibration, and the status ladder
that decides whether a number may drive a concentration estimate. Re-deriving
any of that here would be a second place for it to be wrong. This module adds
exactly the two things the sensor has no opinion about: has something arrived,
and has it gone.

**The first non-zero reading is never processed.** Crossing
`pan.object_threshold_g` only opens the cycle; what gets attached to the item
is whatever `WeightSensor.read()` returns after it has settled, which may
equally be a refusal.

**A refusal still completes the cycle.** An unstable reading, a disconnected
cell or a timeout produces a reading that says so, the decision engine routes
the item on that basis - to C, because an unmeasured mass cannot justify
anything else - and the machine returns to waiting. Stalling in WEIGHING with
an object on the pan would be a machine that needs a human to rescue it.

`step()` performs at most one transition and returns the new state, so the
whole lifecycle is testable synchronously with no threads and no sleeping.
"""

from __future__ import annotations

import time
from enum import StrEnum

from app import config as config_module
from app.weight import WeightStatus


class PanState(StrEnum):
    """Where the pan is in its cycle. Every state here has something that sets it."""

    #: Empty, or not enough mass to be an object. Nothing is latched.
    WAITING_FOR_OBJECT = "WAITING_FOR_OBJECT"
    #: Mass has arrived and an identity has been latched to it.
    OBJECT_PRESENT = "OBJECT_PRESENT"
    #: Collecting readings. Nothing is attached to the item yet.
    WEIGHING = "WEIGHING"
    #: The sensor returned a settled reading - or an explicit refusal to settle.
    WEIGHT_STABLE = "WEIGHT_STABLE"
    #: Estimating and deciding. No servo has been asked to move.
    PROCESSING = "PROCESSING"
    #: The command is being issued. Not proof a paddle moved.
    ROUTING = "ROUTING"
    #: Done with this object. Will not accept another until the pan is clear.
    WAITING_FOR_CLEAR = "WAITING_FOR_CLEAR"


class PanMachine:
    """Drives one object at a time from arrival to removal, automatically.

    Everything physical arrives as a callable so this class owns no hardware:
    `sensor()` yields the current `WeightSensor` or None, and `process` and
    `route` are the session's two halves of handling an object. That is also
    what lets the whole lifecycle run in a test against a fake cell.
    """

    def __init__(
        self,
        zone,
        sensor,
        process,
        route,
        cfg: config_module.Config | None = None,
        clock=time.monotonic,
        belt_running=None,
    ) -> None:
        self.cfg = config_module.load() if cfg is None else cfg
        self.zone = zone
        self._sensor = sensor
        self._process = process
        self._route = route
        self._clock = clock
        #: Is the conveyor motor turning? A callable rather than a flag, for the
        #: same reason `sensor` is: this class owns no hardware. Defaults to
        #: "no belt", so a rig without a motor behaves exactly as before.
        self._belt_running = belt_running if belt_running is not None else (lambda: False)

        self.object_threshold_g = self.cfg["conveyor.weight.pan.object_threshold_g"]
        self.clear_threshold_g = self.cfg["conveyor.weight.pan.clear_threshold_g"]
        self.clear_samples = self.cfg["conveyor.weight.pan.clear_samples"]
        self.poll_interval_s = self.cfg["conveyor.weight.pan.poll_interval_s"]

        self.state = PanState.WAITING_FOR_OBJECT
        self.reason: str | None = None
        self.grams: float | None = None
        self.since: float = self._clock()
        self.cycles = 0
        self._clear_run = 0
        self._reading = None

    # -- helpers -----------------------------------------------------------
    def _to(self, state: PanState, reason: str | None = None) -> PanState:
        if state is not self.state:
            self.state = state
            self.since = self._clock()
        self.reason = reason
        return self.state

    def _live_grams(self) -> tuple[float | None, str | None]:
        """One unfiltered sample, in grams, or None and why not.

        Crude on purpose: this only answers "is something there". A single
        sample is the right cost for a question asked several times a second,
        and nothing is ever attached to an item from it.
        """
        sensor = self._sensor()
        if sensor is None:
            return None, "No load cell is connected, so nothing can be detected on the pan."
        # `has_factor`, not `present`: this only answers "has something
        # arrived". An unverified factor is good enough to notice a mass, and
        # the reading that follows is labelled STABLE and refused downstream by
        # anything that needs a measurement.
        if not sensor.calibration.has_factor:
            return None, (
                "The load cell is not calibrated, so counts cannot be read as a mass. "
                "Run `python -m app.calibrate`."
            )
        sample = sensor.reader.read()
        if sample is None:
            if not getattr(sensor.reader, "connected", True):
                return None, (
                    getattr(sensor.reader, "last_error", None)
                    or "The load-cell reader disconnected."
                )
            # Two causes, and the operator cannot tell them apart from here:
            # no weight frames are arriving at all, or every one carries a count
            # outside what a 24-bit converter can represent and is refused. The
            # second is what an unplugged, shorted or unwired cell reads, and it
            # is worth naming - it presents as a healthy link and a confident
            # constant, which is the most misleading shape a sensor fault takes.
            return None, (
                "No usable reading from the load cell. Either no weight frames "
                "are arriving, or every one is off the converter's scale - which "
                "is what an open or shorted cell reads. Check the HX711 wiring: "
                "DOUT/SCK on D2/D3, and the cell's four wires into the amplifier."
            )
        grams = sensor.calibration.grams(sample.raw_counts)
        return grams, None

    # -- the cycle ---------------------------------------------------------
    def step(self) -> PanState:
        """Advance the machine by at most one transition."""
        if self.state is PanState.WAITING_FOR_OBJECT:
            return self._waiting_for_object()
        if self.state is PanState.OBJECT_PRESENT:
            return self._to(PanState.WEIGHING, "Collecting readings until the mass settles.")
        if self.state is PanState.WEIGHING:
            return self._weighing()
        if self.state is PanState.WEIGHT_STABLE:
            return self._to(PanState.PROCESSING, "Estimating from cited evidence and deciding.")
        if self.state is PanState.PROCESSING:
            return self._processing()
        if self.state is PanState.ROUTING:
            return self._routing()
        return self._waiting_for_clear()

    def _waiting_for_object(self) -> PanState:
        grams, problem = self._live_grams()
        self.grams = grams
        if grams is None:
            return self._to(PanState.WAITING_FOR_OBJECT, problem)
        if grams <= self.object_threshold_g:
            return self._to(PanState.WAITING_FOR_OBJECT, None)

        assembly = self.zone.latch()
        if assembly is None:
            # Mass with no identity. Weighing it would produce a measurement
            # belonging to nothing, so the machine waits rather than inventing
            # an item for it.
            return self._to(
                PanState.WAITING_FOR_OBJECT,
                f"{grams:.1f} g is on the pan, but no assembly has been confirmed by the "
                "camera. Hold the object in front of the camera first - a mass with no "
                "identity is not something this system will process.",
            )
        return self._to(
            PanState.OBJECT_PRESENT,
            f"{assembly.assembly_id} is on the pan ({grams:.1f} g and rising).",
        )

    def _weighing(self) -> PanState:
        # THE BELT MUST BE STOPPED TO WEIGH, and this is the backstop rather
        # than the mechanism: the machine loop stops the motor when the pan
        # leaves WAITING_FOR_OBJECT, one iteration before this runs. If it
        # somehow has not, refuse - measured on this bench, a running motor
        # takes the reading from 0.084 g of noise to 36.044 g, and components
        # here weigh 5-200 g. A mass read over a running belt is not a light
        # object, it is noise, and nothing downstream could tell the difference.
        if self.cfg["conveyor.belt.motor.weigh_requires_stopped"] and self._belt_running():
            self._reading = None
            return self._to(
                PanState.WEIGHT_STABLE,
                "Refusing to weigh while the conveyor is running: the motor swamps the "
                "load cell. The belt should have stopped before this point.",
            )
        sensor = self._sensor()
        if sensor is None:
            self._reading = None
            return self._to(
                PanState.WEIGHT_STABLE, "The load cell went away while the object was on it."
            )
        # Blocks until settled or until conveyor.weight.timeout_s. Same call,
        # same thresholds and same status ladder the manual path uses.
        reading = sensor.read()
        self._reading = reading
        self.grams = reading.grams if reading.status is not WeightStatus.UNAVAILABLE else None
        settled = reading.status in (WeightStatus.MEASURED, WeightStatus.STABLE)
        return self._to(
            PanState.WEIGHT_STABLE,
            f"{reading.grams:.1f} g, {reading.status}."
            if settled
            else f"No usable mass: {reading.status}. {reading.reason or ''}".strip(),
        )

    def _processing(self) -> PanState:
        assembly = self.zone.held
        if assembly is None:
            return self._to(PanState.WAITING_FOR_CLEAR, "Nothing was latched to process.")
        self._process(assembly, self._reading)
        decision = (assembly.decision or {}).get("decision")
        return self._to(PanState.ROUTING, f"Decision: bin {decision}." if decision else None)

    def _routing(self) -> PanState:
        assembly = self.zone.held
        if assembly is not None:
            self._route(assembly)
        self.cycles += 1
        self._clear_run = 0
        actuation = (assembly.actuation if assembly else None) or {}
        return self._to(
            PanState.WAITING_FOR_CLEAR,
            (actuation.get("reason") or "Routed.") + " Remove the object to continue.",
        )

    def _waiting_for_clear(self) -> PanState:
        grams, problem = self._live_grams()
        self.grams = grams
        if grams is None:
            # A cell that has gone away cannot report the pan emptying. Release
            # rather than hold the identity forever: the next object gets a
            # fresh cycle, and this one keeps whatever record it earned.
            self.zone.release()
            self._clear_run = 0
            return self._to(PanState.WAITING_FOR_OBJECT, problem)
        if grams > self.clear_threshold_g:
            self._clear_run = 0
            return self._to(
                PanState.WAITING_FOR_CLEAR,
                f"{grams:.1f} g still on the pan. Remove the object to continue.",
            )
        self._clear_run += 1
        if self._clear_run < self.clear_samples:
            return self._to(PanState.WAITING_FOR_CLEAR, "The pan is clearing.")
        self.zone.release()
        self._clear_run = 0
        return self._to(PanState.WAITING_FOR_OBJECT, "Ready for the next object.")

    # -- reporting ---------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "state": str(self.state),
            "reason": self.reason,
            "grams": None if self.grams is None else round(self.grams, 1),
            "seconds_in_state": round(self._clock() - self.since, 2),
            "cycles_completed": self.cycles,
            "automatic": True,
            "belt_running": bool(self._belt_running()),
            "thresholds": {
                "object_threshold_g": self.object_threshold_g,
                "clear_threshold_g": self.clear_threshold_g,
                "clear_samples": self.clear_samples,
                "note": (
                    "Arrival and clearance only. Whether a reading has SETTLED is "
                    "decided by conveyor.weight.stability_* in app/weight.py."
                ),
            },
            "association": self.zone.snapshot(),
        }
