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
from datetime import UTC, datetime

import cv2

from app import config as config_module
from app.decision import engine as decision_engine
from app.hardware.arduino import ArduinoController
from app.hardware.link import BoardLink
from app.pipeline.association import SingleObjectZone
from app.pipeline.item_pipeline import ItemPipeline
from app.pipeline.pan import PanMachine, PanState
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
        self.pipeline = ItemPipeline(detector=detector, cfg=self.cfg)
        self.link = link
        self.controller = controller
        self.calibration = Calibration.load()

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
                self.controller = ArduinoController(transport=self.link.transport, cfg=self.cfg)
            self.start_pan()
        return {"connected": self.link.connected, "state": str(state), **self.link.snapshot()}

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
        if not self.cfg["conveyor.weight.pan.auto"]:
            return False
        if self._pan_thread is not None and self._pan_thread.is_alive():
            return True
        self._stop.clear()
        self._pan_thread = threading.Thread(target=self._pan_loop, name="aurum-pan", daemon=True)
        self._pan_thread.start()
        return True

    def _pan_loop(self) -> None:
        interval = self.cfg["conveyor.weight.pan.poll_interval_s"]
        while not self._stop.is_set():
            try:
                state = self.pan.step()
            except Exception as exc:  # a USB cable is not a reason to stop
                # Paced at the ordinary polling interval rather than a backoff
                # of its own: a sensor that raises is still just a sensor being
                # asked too often, and a second constant here would be one more
                # number nobody can configure.
                self.pan.reason = f"The pan machine hit an error and is retrying: {exc}"
                time.sleep(interval)
                continue
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
                time.sleep(0.2)
                continue
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

    def _process(self, assembly: Assembly, reading: WeightReading | None) -> dict:
        """Attach the mass, estimate from cited evidence, and take the decision.

        No servo is touched here. Splitting the decision from the movement is
        what lets the state machine report DECISION_READY separately from
        actuation, and what keeps the slow serial round trip out of this lock.
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
            return record

    def _route(self, assembly: Assembly) -> dict:
        """Act on a decision that has already been taken.

        Called with the lock released. `ArduinoController.move()` waits for an
        acknowledgement that the sketch sends only after the paddle has
        finished its stroke - up to `arduino.ack_timeout_ms` - and holding the
        session lock across that would stall the camera thread for a second at
        a time, which is the vision freeze this split exists to prevent.
        """
        decision = assembly.decision or {}
        target = str(decision.get("decision") or "C")
        actuation = self._actuate(assembly.assembly_id, target)
        with self._lock:
            assembly.actuation = actuation
            record = assembly.as_dict()
            self._routed[assembly.assembly_id] = record
        return actuation

    def _actuate(self, item_id: str, target: str) -> dict:
        """Turn a decision into a paddle movement, or record why it is not one."""
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
                "conveyor": {
                    "present": False,
                    "note": (
                        "No conveyor exists. The operator places the object on the pan; "
                        "the load cell starts the measurement and the routing is "
                        "immediate, not scheduled."
                    ),
                },
            }
