"""The demonstration session: the one object that joins every stage.

    camera -> detector -> tracker -> components -> ASSEMBLY -> AUR-ITEM-xxxxxxxx
                                                                    |
              the operator puts the object on the pan and does nothing else
                                                                    v
                                                       HX711 -> measured mass
                                                                    |
                                          material evidence -> PMDI -> valuation
                                                                    |
                                                             A / B / C decision
                                                                    |
                                   A -> Servo A    B -> Servo B    C -> nothing
                                                                    |
                                             object removed -> ready for the next

**The load cell drives the machine.** There is no button in the normal path.
`app.pipeline.pan` watches the cell, latches the identity the camera minted,
waits for the mass to settle, and runs the rest of the chain on its own.
`measure_and_route()` survives as a developer fallback and calls exactly the
same two methods the automatic path does, so the fallback cannot drift away
from the real behaviour.

**The unit of work is an assembly, not a detection.** A motherboard is one
physical object with one id and one mass, whatever it happens to have on it.
`app.vision.assembly` decides which detected components are one object; nothing
in this file knows what a motherboard is.

**There is no conveyor, and this module does not pretend there is one.** The
operator carries the object from the camera to the load cell, which is why
nothing here computes a belt speed, a distance or a firing time. `app.routing`
keeps the scheduled-route model for the day a belt exists, and this session
does not use it - a timing model of a machine that does not exist would be
theatre. The assumption that the object on the pan is the one the camera last
confirmed lives in `app.pipeline.association`, alone, so a conveyor can replace
it without touching anything here.

What the operator may not do is choose the bin: the classes come from the
model, the mass from the cell, and the bin from `app.decision`. There is no
code path in this file that accepts a class, a mass or a bin as an input.

Every stage can fail, and each failure is recorded against the item rather than
raised. A demonstration where the load cell is unplugged should show an item
that could not be weighed, not a stack trace on a projector.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import cv2

from app import config as config_module
from app import epr
from app.decision import engine as decision_engine
from app.errors import ErrorCode, ErrorLog
from app.hardware.arduino import ArduinoController
from app.hardware.fault import HardwareFault
from app.hardware.link import BoardLink
from app.hardware.servos import ActuationOutcome, ServoActuator
from app.pipeline.association import SingleObjectZone
from app.pipeline.item_pipeline import ItemPipeline
from app.pipeline.pan import PanMachine, PanState
from app.routing.conveyor import Conveyor, hardware_mode
from app.routing.scheduler import RouteStatus, RoutingScheduler
from app.valuation import prices as prices_module
from app.valuation import valuation as valuation_module
from app.vision import assembly as assembly_module
from app.vision.assembly import Assembly
from app.vision.tracker import ItemState
from app.weight import Calibration, WeightReading, WeightSensor, WeightStatus

#: Bin to paddle. C is absent because there is no Servo C: an item Aurum
#: cannot justify routing is left alone, which is also what happens if this
#: process dies mid-run.
SERVO_TARGETS = ("A", "B")

#: Box colours for the annotated feed, BGR.
CLASS_COLOR = {
    "PCB": (120, 200, 90),
    "RAM": (230, 170, 60),
    "CPU": (80, 180, 240),
    "Connector": (200, 140, 220),
}


def _unavailable_reading(reason: str) -> WeightReading:
    """A refusal shaped like a reading, so downstream needs no special case."""
    return WeightReading(
        grams=0.0,
        simulated=False,
        source="none",
        status=WeightStatus.UNAVAILABLE,
        usable=False,
        reason=reason,
        timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def _vision_capture(cfg: config_module.Config, session_id: str):
    """The failure-capture sink, off unless someone asked for it.

    Imported from `tools/` rather than `app/`, which is the wrong direction for
    a dependency and is why the import is local: nothing in `app` may fail to
    load because a developer tool is missing. It never imports FiftyOne.
    """
    from tools.fiftyone.failures import FailureCapture

    directory = Path(cfg["tracking.capture.directory"])
    if not directory.is_absolute():
        directory = config_module.ROOT / directory
    return FailureCapture(
        directory=directory,
        enabled=bool(cfg["tracking.capture.enabled"]),
        session_id=session_id,
        per_category_limit=cfg["tracking.capture.per_category_limit"],
    )


def annotate(frame, detections, assemblies: list[Assembly]):
    """Draw boxes labelled with the assembly identity, not just the class.

    The assembly id is the thread the whole demonstration hangs on - the mass,
    the PMDI and the servo command all key off it - so every component of one
    physical object carries the same id on screen. Four boxes sharing one
    `AUR-ITEM-` is the hierarchy made visible.
    """
    out = frame.copy()
    by_track = {}
    for group in assemblies:
        for member in group.members:
            by_track[member.track_id] = group.assembly_id

    for det in detections:
        x1, y1, x2, y2 = det.xyxy
        colour = CLASS_COLOR.get(det.class_name, (60, 200, 220))
        cv2.rectangle(out, (x1, y1), (x2, y2), colour, 2)

        assembly_id = by_track.get(det.track_id)
        label = f"{det.class_name} {det.confidence:.2f}"
        if assembly_id is not None:
            label = f"{assembly_id}  {label}"
        (tw, th), base = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        ty = max(y1, th + 6)
        cv2.rectangle(out, (x1, ty - th - 6), (x1 + tw + 10, ty + base - 2), colour, -1)
        cv2.putText(
            out,
            label,
            (x1 + 5, ty - 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (20, 20, 20),
            1,
            cv2.LINE_AA,
        )
    return out


class DemoSession:
    """One run: one camera, one board, one set of physical identities.

    Construct it with nothing and it runs headless and hardware-free, which is
    what the tests do. Give it a detector and a port and it is the machine.
    """

    def __init__(
        self,
        detector=None,
        cfg: config_module.Config | None = None,
        link: BoardLink | None = None,
        controller: ArduinoController | None = None,
    ) -> None:
        self.cfg = config_module.load() if cfg is None else cfg
        self.session_id = f"AUR-RUN-{uuid.uuid4().hex[:8].upper()}"
        self.pipeline = ItemPipeline(detector=detector, cfg=self.cfg)
        self.link = link
        self.controller = controller
        self.calibration = Calibration.load()
        self.errors = ErrorLog(self.session_id)
        self.capture = _vision_capture(self.cfg, self.session_id)
        #: One latch, shared with whatever board layer this session ends up
        #: with, so the session and the controller cannot disagree about
        #: whether the machine is safe to actuate.
        self.fault = controller.fault if controller is not None else HardwareFault()

        # The belt, if there is one. With `conveyor.mode: NONE` - the shipped
        # state, because this machine has no belt - `scheduler` and `actuator`
        # stay None and routing is immediate. With a belt they are the path:
        # decision -> ScheduledRoute -> ServoActuator -> board.
        self.conveyor = Conveyor.from_config(self.cfg)
        self.scheduler: RoutingScheduler | None = None
        self.actuator: ServoActuator | None = None
        if self.conveyor.present:
            self.scheduler = RoutingScheduler(
                lifecycle=self.pipeline.tracker, cfg=self.cfg, conveyor=self.conveyor
            )

        self.zone = SingleObjectZone(
            source=lambda: self.assemblies,
            min_detections=self.pipeline.tracker.min_detections_to_confirm,
        )
        self.pan = PanMachine(
            zone=self.zone,
            sensor=lambda: self.weight_sensor,
            process=self._process,
            route=self._route,
            cfg=self.cfg,
        )

        self._source = None
        self._thread: threading.Thread | None = None
        self._pan_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._jpeg: bytes | None = None
        self.camera_error: str | None = None
        self.camera_label: str | None = None
        self.frames = 0
        self.started_at: str | None = None
        #: Assemblies already put through the chain, so one physical object
        #: cannot be weighed and routed twice by an impatient second click.
        self._handled: set[str] = set()
        #: Finished records, newest last. The run's ledger, kept here because
        #: the tracker deliberately forgets an item once it has been drained.
        self._routed: dict[str, dict] = {}
        #: Cached on first use: reading the evidence database per frame would
        #: parse a 950-line YAML file thirty times a second.
        self._classes: set[str] | None = None

    # -- hardware ----------------------------------------------------------
    def connect_board(self) -> dict:
        """Open the one serial link the HX711 and both servos share.

        Starts the automatic pan machine once there is a cell to watch, unless
        `conveyor.weight.pan.auto` is off.
        """
        port = self.cfg["conveyor.arduino.port"]
        if not port:
            return {
                "connected": False,
                "reason": (
                    "No board port is configured. Set conveyor.arduino.port or "
                    "AURUM_ARDUINO_PORT. Nothing is invented in its place."
                ),
            }
        if self.link is None:
            self.link = BoardLink(
                port,
                baudrate=self.cfg["conveyor.arduino.baudrate"],
                timeout_s=self.cfg["conveyor.arduino.timeout_s"],
            )
        state = self.link.connect()
        if self.link.connected:
            self.link.configure_servos(
                self.cfg["conveyor.servo.rest_angle_deg"],
                self.cfg["conveyor.servo.push_angle_deg"],
                self.cfg["conveyor.servo.actuation_ms"],
            )
            if self.controller is None:
                self.controller = ArduinoController(
                    transport=self.link.transport, cfg=self.cfg, fault=self.fault
                )
            self._ensure_actuator()
            self.start_pan()
        else:
            self.errors.record(
                ErrorCode.ARDUINO_ERROR,
                "board",
                self.link.last_error or f"the board did not open ({state})",
                port=port,
            )
        return {"connected": self.link.connected, "state": str(state), **self.link.snapshot()}

    def _ensure_actuator(self) -> ServoActuator | None:
        """The bridge from a DUE route to a servo command, if there is a belt.

        Built here rather than in the constructor because it needs a
        controller, and a controller needs a board that may not be attached
        when the session starts.
        """
        if self.scheduler is None or self.controller is None:
            return self.actuator
        if self.actuator is None:
            self.actuator = ServoActuator(self.scheduler, controller=self.controller, cfg=self.cfg)
        return self.actuator

    @property
    def weight_sensor(self) -> WeightSensor | None:
        """A sensor over the shared link, or None when no board is attached."""
        if self.link is None or not self.link.connected:
            return None
        return WeightSensor(
            self.link.weight_reader,
            calibration=self.calibration,
            cfg=self.cfg,
            simulated=False,
        )

    # -- the automatic machine --------------------------------------------
    def start_pan(self) -> bool:
        """Run the pan state machine on its own thread. True if it is running.

        Its own thread for a reason: `WeightSensor.read()` blocks until the
        mass settles and `ArduinoController.move()` blocks until the board
        acknowledges a completed stroke. Neither may run on the camera thread,
        and neither may hold the session lock while it waits.
        """
        if not self.cfg["conveyor.weight.pan.auto"] and not self.conveyor.present:
            return False
        if self._pan_thread is not None and self._pan_thread.is_alive():
            return True
        self._stop.clear()
        self._pan_thread = threading.Thread(target=self._pan_loop, name="aurum-pan", daemon=True)
        self._pan_thread.start()
        return True

    def _pan_loop(self) -> None:
        """The machine loop: step the pan, and fire whatever has arrived.

        Two jobs on one thread because they are the same job at two ends of the
        belt. Draining here rather than on a thread of its own is what keeps
        the firing decision on the same clock as the pan, and the polling
        interval - 50 ms by default - is well inside the +/-200 ms the timing
        model is accurate to anyway.
        """
        interval = self.cfg["conveyor.weight.pan.poll_interval_s"]
        automatic = bool(self.cfg["conveyor.weight.pan.auto"])
        while not self._stop.is_set():
            state = PanState.WAITING_FOR_OBJECT
            if automatic:
                try:
                    state = self.pan.step()
                except Exception as exc:  # a USB cable is not a reason to stop
                    # Paced at the ordinary polling interval rather than a
                    # backoff of its own: a sensor that raises is still just a
                    # sensor being asked too often, and a second constant here
                    # would be one more number nobody can configure.
                    self.pan.reason = f"The pan machine hit an error and is retrying: {exc}"
                    self.errors.record(ErrorCode.WEIGHT_ERROR, "pan", str(exc))
                    time.sleep(interval)
                    continue
            self.drain_routes()
            # Only the polling states need pacing; the rest are transitions
            # through work that has already taken its own time.
            if state in (PanState.WAITING_FOR_OBJECT, PanState.WAITING_FOR_CLEAR):
                time.sleep(interval)

    # -- camera ------------------------------------------------------------
    def start_camera(self, mode: str = "webcam", path: str | None = None) -> dict:
        """Open the camera and run detection and tracking in the background.

        A camera that will not open is reported, not raised: on macOS the usual
        cause is an ungranted permission, and the operator needs to read that
        rather than watch a server exit.
        """
        from app.demo import FrameSource

        if self._thread is not None and self._thread.is_alive():
            return {"running": True, "source": self.camera_label}
        if self.pipeline.detector_tracker is None:
            return {"running": False, "error": "This session has no detector."}

        try:
            self._source = FrameSource(
                mode=mode,
                path=path,
                camera=self.cfg["conveyor.camera.index"],
                width=self.cfg["conveyor.camera.width"],
                height=self.cfg["conveyor.camera.height"],
                image_seconds=0.0,
            )
        except RuntimeError as exc:
            self.camera_error = str(exc)
            return {"running": False, "error": str(exc)}

        self.camera_error = None
        self.camera_label = self._source.label
        self.started_at = datetime.now(UTC).isoformat(timespec="seconds")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="aurum-camera", daemon=True)
        self._thread.start()
        return {"running": True, "source": self.camera_label}

    def _loop(self) -> None:
        while not self._stop.is_set():
            ok, frame = self._source.read()
            if not ok or frame is None:
                time.sleep(0.05)
                continue
            try:
                detections = self.pipeline.detector_tracker.track(frame)
            except Exception as exc:  # a driver or model fault must not kill the run
                self.camera_error = f"tracking failed: {exc}"
                self.errors.record(ErrorCode.VISION_ERROR, "camera", str(exc))
                time.sleep(0.2)
                continue
            self._capture_failures(frame, detections)
            with self._lock:
                self.pipeline.process_detections(detections)
                self.frames += 1
                ok_enc, buf = cv2.imencode(
                    ".jpg",
                    annotate(frame, detections, self.assemblies),
                    [cv2.IMWRITE_JPEG_QUALITY, 80],
                )
                if ok_enc:
                    self._jpeg = buf.tobytes()

    def _capture_failures(self, frame, detections) -> None:
        """Keep frames worth looking at again. Never at the cost of the run.

        Off unless `tracking.capture.enabled`, in which case `FailureCapture`
        does its own rate limiting. A disk that fills or a directory that
        cannot be written is a lost sample, not a stopped machine.
        """
        if not self.capture.enabled:
            return
        try:
            height, width = frame.shape[:2]
            self.capture.capture_frame(
                frame,
                detections,
                width,
                height,
                low_confidence=self.cfg["tracking.capture.low_confidence"],
                known_classes=self._known_classes(),
            )
        except Exception as exc:
            self.errors.record(ErrorCode.VISION_ERROR, "capture", str(exc))

    def _known_classes(self) -> set[str]:
        """Classes the evidence database has a cited profile for.

        Derived from the database rather than listed here, so a class stops
        being an UNKNOWN_OBJECT the day composition is added for it.
        """
        if self._classes is None:
            from app import materials

            self._classes = set((materials.load().get("components") or {}).keys())
        return self._classes

    @property
    def assemblies(self) -> list[Assembly]:
        """The physical objects the camera can see right now.

        Derived on read rather than cached from the last frame. Grouping is a
        pure function over a handful of tracked items, and a cache here would
        buy nothing except a class of bug where the pan latches an object the
        tracker has already moved on from.
        """
        return assembly_module.group(self.pipeline.tracker.active, cfg=self.cfg)

    def latest_jpeg(self) -> bytes | None:
        with self._lock:
            return self._jpeg

    def stop(self) -> None:
        self._stop.set()
        for thread in (self._thread, self._pan_thread):
            if thread is not None:
                thread.join(timeout=3.0)
        self._thread = None
        self._pan_thread = None
        if self._source is not None:
            self._source.release()
            self._source = None
        if self.link is not None:
            self.link.disconnect()

    # -- the chain ---------------------------------------------------------
    @property
    def current_assembly(self) -> Assembly | None:
        """The object being handled: the latched one, or the newest confirmed.

        Deliberately does NOT skip objects already put through the chain. An
        object that is still on the bench is still the current one, and the
        manual path needs to find it in order to say it has already been
        routed - reporting "no item" for something in plain view is how the
        console reads as broken. Refusing to act on it twice is
        `measure_and_route`'s job, and the automatic path keeps its own
        exclusion in `SingleObjectZone`.
        """
        held = self.zone.held
        if held is not None:
            return held
        eligible = [
            a
            for a in self.assemblies
            if a.root is not None
            and (
                a.root.state is ItemState.CONFIRMED
                or (
                    a.root.state is ItemState.LEAVING
                    and a.root.detection_count >= self.pipeline.tracker.min_detections_to_confirm
                )
            )
        ]
        if not eligible:
            return None
        return max(
            eligible,
            key=lambda a: (a.root.state is ItemState.CONFIRMED, a.root.last_frame),
        )

    # -- the EPR trail -----------------------------------------------------
    def _provenance(self) -> dict:
        """The stamp that travels on every EPR event this run writes."""
        detector = self.pipeline.detector_tracker
        return epr.provenance(
            self.cfg,
            model_version=getattr(getattr(detector, "detector", detector), "model_version", None),
            calibration=self.calibration.as_dict(),
        )

    def _epr(self, item_id: str, event: epr.EprEvent, payload: dict, simulated: bool = False):
        """Append one event, and never let the ledger stop the machine.

        A locked database or a full disk is a reason to lose an audit row, not
        a reason to leave an object on the pan. The failure is recorded in the
        error log instead, where it is visible on the dashboard.
        """
        try:
            return epr.record(
                item_id,
                event,
                payload=payload,
                session_id=self.session_id,
                prov=self._provenance(),
                simulated=simulated,
            )
        except Exception as exc:
            self.errors.record(
                ErrorCode.CONFIG_ERROR, "epr", f"could not write {event}: {exc}", item_id=item_id
            )
            return None

    def _process(self, assembly: Assembly, reading: WeightReading | None) -> dict:
        """Attach the mass, estimate from cited evidence, and take the decision.

        No servo is touched here. Splitting the decision from the movement is
        what lets the state machine report DECISION_READY separately from
        actuation, and what keeps the slow serial round trip out of this lock.

        Each stage writes its own EPR event as it completes, rather than one
        summary at the end: an object that fails half way through must leave a
        trail up to the point it failed, not nothing.
        """
        with self._lock:
            self._handled.add(assembly.assembly_id)
            if reading is None:
                reading = _unavailable_reading("No reading was taken.")
            assembly.attach_weight(reading.grams, str(reading.status), reading.timestamp)
            assembly.weight_reading = reading.as_dict()

            # A mass is forwarded when it carries a real quantity - a settled
            # measurement, or a labelled stand-in. An UNAVAILABLE reading is
            # withheld entirely: its grams field is 0.0, and 0.0 with no
            # simulated flag would classify as MEASURED downstream.
            has_quantity = reading.status in (WeightStatus.MEASURED, WeightStatus.SIMULATED)
            valuation = valuation_module.value(
                assembly.counts,
                mass=reading.as_dict() if has_quantity else None,
                item_id=assembly.assembly_id,
            )
            decision = decision_engine.decide(
                assembly.class_name, assembly.confidence, valuation, cfg=self.cfg
            )
            assembly.valuation = valuation.as_dict()
            assembly.decision = decision.as_dict()
            record = assembly.as_dict()
            self._routed[assembly.assembly_id] = record

        # Outside the lock: SQLite writes must not stall the camera thread.
        self._record_evidence(assembly, reading, valuation, decision)
        return record

    def _record_evidence(self, assembly, reading, valuation, decision) -> None:
        """Write the perception, measurement and decision half of the trail."""
        item_id = assembly.assembly_id
        simulated = reading.simulated or reading.status is WeightStatus.SIMULATED
        self._epr(
            item_id,
            epr.EprEvent.DETECTED,
            {
                "bbox": list(assembly.bbox) if assembly.bbox else None,
                "members": len(assembly.members),
            },
        )
        self._epr(
            item_id,
            epr.EprEvent.CLASSIFIED,
            {
                "class_name": assembly.class_name,
                "confidence": assembly.confidence,
                "components": assembly.counts,
                "is_assembly": assembly.is_assembly,
            },
        )
        self._epr(item_id, epr.EprEvent.WEIGHED, reading.as_dict(), simulated=simulated)
        pmdi = valuation.pmdi
        self._epr(
            item_id,
            epr.EprEvent.COMPOSITION_LOOKUP,
            {
                "evidence_status": str(pmdi.evidence_status),
                "completeness": pmdi.completeness,
                "evidence_sources": list(pmdi.evidence_sources),
                "valued": list(pmdi.valued),
                "not_valued": list(pmdi.not_valued),
            },
            simulated=simulated,
        )
        self._epr(
            item_id,
            epr.EprEvent.PMDI_CALCULATED,
            {
                "precious_mass_g": pmdi.precious_mass_g if pmdi.available else None,
                "precious_mass_fraction_ppm": pmdi.precious_mass_fraction_ppm,
                "pmdi_value": pmdi.pmdi_value,
                "price_status": str(pmdi.price_status),
                "prices": {m: q.as_dict() for m, q in pmdi.prices.items()},
            },
            simulated=simulated,
        )
        self._epr(
            item_id,
            epr.EprEvent.VALUE_CALCULATED,
            {
                "total_value": valuation.total_value,
                "base_value": valuation.base_value,
                "currency": valuation.currency,
                "overall_status": str(valuation.overall_status),
            },
            simulated=simulated,
        )
        self._epr(
            item_id,
            epr.EprEvent.BIN_ASSIGNED,
            {
                "decision": str(decision.decision),
                "physical_bin": decision.physical_bin,
                "reason_code": str(decision.reason_code),
                "reason": decision.reason,
                "servo": decision.servo,
            },
            simulated=simulated,
        )

    def _route(self, assembly: Assembly) -> dict:
        """Act on a decision that has already been taken.

        Two paths, and which one runs is `conveyor.mode`:

            NONE          the operator carries the object, so the paddle moves
                          now. Nothing to schedule: there is no travel time.
            a real belt   the item is somewhere upstream of the paddle, so this
                          computes when it will arrive and hands a
                          `ScheduledRoute` to the machine loop, which fires it.

        Called with the lock released. `ArduinoController.move()` waits for an
        acknowledgement that the sketch sends only after the paddle has
        finished its stroke - up to `arduino.ack_timeout_ms` - and holding the
        session lock across that would stall the camera thread for a second at
        a time, which is the vision freeze this split exists to prevent.
        """
        decision = assembly.decision or {}
        # The PHYSICAL bin, not the decision. They differ for UNKNOWN, which is
        # a decision state and not a place: the item still reaches C, and the
        # record keeps both so the dashboard can show `UNKNOWN -> C`.
        target = str(decision.get("physical_bin") or decision.get("decision") or "C")
        actuation = (
            self._schedule(assembly, target)
            if self.scheduler is not None
            else self._actuate_now(assembly.assembly_id, target)
        )
        with self._lock:
            assembly.actuation = actuation
            record = assembly.as_dict()
            self._routed[assembly.assembly_id] = record
        return actuation

    def _schedule(self, assembly: Assembly, target: str) -> dict:
        """Hand the item to the belt's timing model. Nothing moves yet."""
        detected_at = time.monotonic()
        route = self.scheduler.schedule(
            assembly.assembly_id,
            target,
            detected_at,
            component_class=assembly.class_name,
        )
        self._epr(
            assembly.assembly_id,
            epr.EprEvent.SERVO_SCHEDULED,
            route.as_dict(now=detected_at),
            simulated=route.simulated,
        )
        if route.status is RouteStatus.UNSCHEDULED:
            self.errors.record(
                ErrorCode.ROUTING_ERROR,
                "routing",
                route.reason,
                item_id=assembly.assembly_id,
                reason_code=str(route.reason_code),
            )
        return {
            "commanded": False,
            "scheduled": route.status is RouteStatus.SCHEDULED,
            "target": target,
            "servo": route.servo,
            "route": route.as_dict(now=detected_at),
            "reason": route.reason,
        }

    def drain_routes(self, now: float | None = None) -> list[dict]:
        """Fire every route whose moment has arrived. Once each.

        The belt half of the machine loop. Safe to call when there is no belt,
        no board or nothing due: it returns an empty list rather than needing
        a caller to know which.
        """
        actuator = self._ensure_actuator()
        if actuator is None:
            return []
        results = actuator.execute_due(now)
        for result in results:
            self._record_actuation(result.item_id, result.as_dict(), result.outcome)
        return [r.as_dict() for r in results]

    def _record_actuation(self, item_id: str, actuation: dict, outcome=None) -> None:
        """Write the actuation half of the trail, and update the item record.

        SORT_CONFIRMED is written only when the contract was satisfied: a frame
        went out and the board acknowledged it. Anything else is a failure or
        is not written at all - bin C moves nothing and confirms nothing.
        """
        confirmed = outcome is ActuationOutcome.ACTUATED or (
            outcome is None and actuation.get("state") == "ACKED"
        )
        no_action = outcome is ActuationOutcome.NO_ACTION or (
            outcome is None and not actuation.get("commanded")
        )
        with self._lock:
            record = self._routed.get(item_id)
            if record is not None:
                record["actuation"] = actuation

        if actuation.get("commanded") or outcome is not None:
            self._epr(item_id, epr.EprEvent.SERVO_TRIGGERED, actuation)
        if confirmed:
            self._epr(item_id, epr.EprEvent.SORT_CONFIRMED, actuation)
            return
        if no_action:
            # Bin C. Nothing was commanded, nothing failed, and writing a
            # failure here would make the fail-safe outcome look like a fault.
            return
        self._epr(item_id, epr.EprEvent.SORT_FAILURE, actuation)
        self.errors.record(
            ErrorCode.SERVO_ERROR,
            "actuation",
            actuation.get("reason") or "the actuation did not complete",
            item_id=item_id,
            error_code=actuation.get("error_code"),
        )

    def _actuate_now(self, item_id: str, target: str) -> dict:
        """Turn a decision into a paddle movement, or record why it is not one.

        The no-belt path: the operator has already carried the object to the
        paddle, so there is no travel time to model and nothing to schedule.
        """
        actuation = self._actuate(item_id, target)
        self._record_actuation(item_id, actuation)
        return actuation

    def _actuate(self, item_id: str, target: str) -> dict:
        """Write one MOVE frame, or say why none was written."""
        if target not in SERVO_TARGETS:
            return {
                "commanded": False,
                "target": target,
                "servo": None,
                "reason": (
                    "Bin C has no actuator. The item is left where it is, which is the "
                    "fail-safe outcome and the same thing that happens if this software "
                    "stops."
                ),
            }
        if self.controller is None:
            return {
                "commanded": False,
                "target": target,
                "servo": f"SERVO_{target}",
                "reason": (
                    "No link to the board, so no command was sent. The decision stands; "
                    "the movement did not happen."
                ),
            }
        command = self.controller.move(target, item_id)
        return {
            "commanded": True,
            "target": target,
            "servo": f"SERVO_{target}",
            **command.as_dict(),
        }

    # -- the manual fallback ----------------------------------------------
    def measure_and_route(self, item_id: str | None = None) -> dict:
        """DEVELOPER FALLBACK. Weigh the object now, grade it and route it.

        Not the normal path: the load cell triggers a measurement on its own
        and no operator action is required. This exists for a bench with no
        working cell, for a mass that will not settle, and for driving the
        chain from a terminal - and it calls the same `_process` and `_route`
        the automatic machine does, so it cannot drift from the real
        behaviour.
        """
        assembly = self._resolve(item_id)
        if isinstance(assembly, dict):
            return assembly
        if assembly.assembly_id in self._handled:
            return {
                "error": "ALREADY_PROCESSED",
                "reason": (
                    f"{assembly.assembly_id} has already been weighed and routed. One "
                    "physical item gets one physical action."
                ),
                "item": assembly.as_dict(),
            }
        reading = self._read_mass(assembly)
        self._process(assembly, reading)
        self._route(assembly)
        return self._routed[assembly.assembly_id]

    def _resolve(self, item_id: str | None):
        """The assembly to act on, or a refusal explaining why there is none."""
        if item_id:
            for group in self.assemblies:
                if item_id == group.assembly_id or any(m.item_id == item_id for m in group.members):
                    return group
            held = self.zone.held
            if held is not None and item_id in (
                held.assembly_id,
                *(m.item_id for m in held.members),
            ):
                return held
            return {
                "error": "UNKNOWN_ITEM",
                "reason": f"{item_id} is not in this run's item lifecycle.",
            }
        assembly = self.current_assembly
        if assembly is None:
            return {
                "error": "NO_ITEM",
                "reason": (
                    "No confirmed item. Hold the object in front of the camera until "
                    "it is CONFIRMED - an object seen once is not yet something to weigh."
                ),
            }
        return assembly

    def mock_mass_for(self, component_class: str | None) -> float:
        """The stand-in mass for a class, in grams.

        Per class, because a precious fraction is metal over TOTAL mass: give a
        CPU a board's mass and its ppm drops sevenfold, which is a number a
        judge can check against the class and find wrong. Falls back to
        `demo.mock_mass.grams` for anything unlisted.
        """
        key = f"demo.mock_mass.{(component_class or '').lower()}_g"
        if key in config_module.SPEC:
            return float(self.cfg[key])
        return float(self.cfg["demo.mock_mass.grams"])

    def mock_mass_for_assembly(self, assembly: Assembly) -> float:
        """The stand-in mass for a whole object: every detected component's.

        A board with two modules and a processor does not weigh what a bare
        board weighs. Summing the per-class stand-ins keeps a fabricated mass
        at least internally consistent with the inventory beside it - which
        matters because the ppm figures are computed against it.
        """
        return sum(
            self.mock_mass_for(cls) * count for cls, count in (assembly.counts or {}).items()
        ) or float(self.cfg["demo.mock_mass.grams"])

    def _mock_mass(self, why: str, assembly: Assembly | None = None) -> WeightReading:
        """A labelled stand-in mass, for a demonstration with no usable cell.

        `simulated` is true and the status is SIMULATED, which is what carries
        the fabrication forward: PMDI, the valuation and the decision all end
        up stamped SIMULATED, and the dashboard shows it as such. Nothing here
        can reach MEASURED.
        """
        grams = (
            self.mock_mass_for_assembly(assembly)
            if assembly is not None
            else float(self.cfg["demo.mock_mass.grams"])
        )
        return WeightReading(
            grams=grams,
            simulated=True,
            source="demo mock mass",
            status=WeightStatus.SIMULATED,
            usable=False,
            mock=True,
            reason=(
                f"MOCK MASS - {grams:g} g was assumed, not measured. {why} "
                "Every figure derived from it is an illustration of the pipeline, "
                "not a measurement of this object."
            ),
            timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    def _read_mass(self, assembly: Assembly | None = None) -> WeightReading:
        mock = bool(self.cfg["demo.mock_mass.enabled"])
        sensor = self.weight_sensor
        if sensor is None:
            why = "No load cell is connected."
            return (
                self._mock_mass(why, assembly)
                if mock
                else _unavailable_reading(f"{why} Nothing is estimated in place of one.")
            )
        if not self.calibration.present:
            why = "The load cell is not calibrated."
            return (
                self._mock_mass(why, assembly)
                if mock
                else _unavailable_reading(
                    f"{why} Run `python -m app.calibrate` and verify against a second known mass."
                )
            )
        reading = sensor.read()
        # A cell that is connected and calibrated but could not settle still
        # falls back, so a flaky reading does not stall a demonstration.
        if mock and not reading.usable:
            return self._mock_mass(f"The cell returned {reading.status}.", assembly)
        return reading

    # -- reporting ---------------------------------------------------------
    def snapshot(self) -> dict:
        """Everything the dashboard renders, in one read."""
        with self._lock:
            current = self.current_assembly
            # Everything routed this run, plus anything currently in view that
            # has not been routed yet. Routed records win: they carry the mass,
            # the decision and the actuation.
            live = [
                a.as_dict()
                for a in sorted(
                    self.assemblies,
                    key=lambda a: a.root.first_frame if a.root else 0,
                    reverse=True,
                )
                if a.assembly_id not in self._routed
            ]
            items = list(reversed(list(self._routed.values()))) + live
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "started_at": self.started_at,
                "frames_processed": self.frames,
                "camera": {
                    "source": self.camera_label,
                    "error": self.camera_error,
                },
                "pan": self.pan.snapshot(),
                "automatic": bool(self.cfg["conveyor.weight.pan.auto"])
                and self._pan_thread is not None
                and self._pan_thread.is_alive(),
                "current_item": current.as_dict() if current else None,
                "items": items[:25],
                "confirmed_count": sum(
                    1
                    for a in self.assemblies
                    if a.root is not None and a.root.state is ItemState.CONFIRMED
                ),
                "calibration": self.calibration.as_dict(),
                "board": self.link.snapshot() if self.link else {"connected": False},
                "actuation": (
                    self.controller.snapshot()
                    if self.controller
                    else {"connected": False, "actuation_enabled": False}
                ),
                "mock_mass": {
                    "enabled": bool(self.cfg["demo.mock_mass.enabled"]),
                    "grams": float(self.cfg["demo.mock_mass.grams"]),
                    "per_class": {
                        cls: self.mock_mass_for(cls) for cls in ("CPU", "PCB", "RAM", "Connector")
                    },
                    "note": (
                        "A stand-in mass is being used because the load cell cannot "
                        "supply one. Every figure derived from it is SIMULATED and "
                        "none of it is a measurement of the object on screen."
                    ),
                },
                "conveyor": self.conveyor.snapshot(),
                "routing": (
                    self.scheduler.snapshot(now=time.monotonic())
                    if self.scheduler is not None
                    else None
                ),
                "hardware": {
                    "mode": hardware_mode(self.cfg),
                    "fault": self.fault.snapshot(),
                    "arduino_connected": bool(self.link and self.link.connected),
                    "actuation_enabled": bool(self.cfg["conveyor.arduino.enabled"]),
                    "servo": (
                        self.actuator.servo_settings
                        if self.actuator is not None
                        else {
                            "rest_angle_deg": self.cfg["conveyor.servo.rest_angle_deg"],
                            "push_angle_deg": self.cfg["conveyor.servo.push_angle_deg"],
                            "actuation_ms": self.cfg["conveyor.servo.actuation_ms"],
                        }
                    ),
                },
                "pricing": self.pricing_snapshot(),
                "errors": self.errors.snapshot(),
                "vision_capture": self.capture.snapshot(),
                "epr": {
                    "session_id": self.session_id,
                    "provenance": self._provenance(),
                },
            }

    def pricing_snapshot(self) -> dict:
        """What each metal costs right now, and whether that may be shown as live.

        Read at snapshot time rather than cached on the session: a price is
        time-varying external data, and the whole point of the status field is
        that it can change from LIVE to STALE without anybody restarting the
        dashboard. The provider does its own caching, so this does not cost a
        request per poll.
        """
        try:
            service = prices_module.PriceService.from_config(self.cfg)
            quotes = service.prices(("Au", "Ag", "Pd", "Cu"))
            return {
                "provider": getattr(service.provider, "name", "unknown"),
                "currency": self.cfg["pricing.currency"],
                "max_age_seconds": service.max_age_seconds,
                "metals": {metal: quote.as_dict() for metal, quote in sorted(quotes.items())},
            }
        except Exception as exc:
            self.errors.record(ErrorCode.PRICE_ERROR, "pricing", str(exc))
            return {"provider": None, "metals": {}, "error": str(exc)}
