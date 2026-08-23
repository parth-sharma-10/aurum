"""The demonstration session: the one object that joins every stage.

    camera -> detector -> tracker -> AUR-ITEM-xxxxxxxx
                                          |
        operator moves the item to the pan|
                                          v
                             HX711 -> measured mass
                                          |
                        material evidence -> PMDI -> valuation
                                          |
                                   A / B / C decision
                                          |
                         A -> Servo A   B -> Servo B   C -> nothing

**There is no conveyor, and this module does not pretend there is one.** The
operator carries the component from the camera to the load cell, which is why
nothing here computes a belt speed, a distance or a firing time. Routing is
immediate: the decision is taken and the paddle moves. `app.routing` keeps the
scheduled-route model for the day a belt exists, and this session does not use
it - a timing model of a machine that does not exist would be theatre.

What the operator may do is move the object and press "measure". What the
operator may not do is choose the bin: the class comes from the model, the mass
from the cell, and the bin from `app.decision`. There is no code path in this
file that accepts a class, a mass or a bin as an input.

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
from app.pipeline.item_pipeline import ItemPipeline
from app.valuation import valuation as valuation_module
from app.vision.tracker import ItemState, TrackedItem
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


def annotate(frame, detections, items: list[TrackedItem]):
    """Draw boxes labelled with the item identity, not just the class.

    The item id is the thread the whole demonstration hangs on - the mass, the
    PMDI and the servo command all key off it - so it belongs on the video
    rather than only in a JSON payload nobody watching can see.
    """
    out = frame.copy()
    by_track = {item.track_id: item for item in items}
    for det in detections:
        x1, y1, x2, y2 = det.xyxy
        colour = CLASS_COLOR.get(det.class_name, (60, 200, 220))
        cv2.rectangle(out, (x1, y1), (x2, y2), colour, 2)

        item = by_track.get(det.track_id)
        label = f"{det.class_name} {det.confidence:.2f}"
        if item is not None:
            label = f"{item.item_id}  {label}"
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
    """One demonstration run: one camera, one board, one set of identities.

    Construct it with nothing and it runs headless and hardware-free, which is
    what the tests do. Give it a detector and a port and it is the SIH demo.
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

        self._source = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._jpeg: bytes | None = None
        self.camera_error: str | None = None
        self.camera_label: str | None = None
        self.frames = 0
        self.started_at: str | None = None
        #: Item ids handled by `measure_and_route`, so one physical object
        #: cannot be weighed and routed twice by an impatient second click.
        self._handled: set[str] = set()
        #: Finished records, newest last. The run's ledger, kept here because
        #: the tracker deliberately forgets an item once it has been drained.
        self._routed: dict[str, dict] = {}

    # -- hardware ----------------------------------------------------------
    def connect_board(self) -> dict:
        """Open the one serial link the HX711 and both servos share."""
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
                items = self.pipeline.process_detections(detections)
                self.frames += 1
                ok_enc, buf = cv2.imencode(
                    ".jpg", annotate(frame, detections, items), [cv2.IMWRITE_JPEG_QUALITY, 80]
                )
                if ok_enc:
                    self._jpeg = buf.tobytes()

    def latest_jpeg(self) -> bytes | None:
        with self._lock:
            return self._jpeg

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        if self._source is not None:
            self._source.release()
            self._source = None
        if self.link is not None:
            self.link.disconnect()

    # -- the operator step -------------------------------------------------
    def measure_and_route(self, item_id: str | None = None) -> dict:
        """Weigh the item on the pan, grade it, and move the paddle it earns.

        One call covers demonstration steps 3 to 10: read the cell, attach the
        mass to the identity the camera already minted, estimate from cited
        composition, take the A/B/C decision, and act on it. The operator
        chooses when, never what.
        """
        with self._lock:
            item = self._resolve(item_id)
            if isinstance(item, dict):
                return item

            if item.item_id in self._handled:
                return self._refusal(
                    item,
                    "ALREADY_PROCESSED",
                    f"{item.item_id} has already been weighed and routed. One physical "
                    "item gets one physical action.",
                )
            self._handled.add(item.item_id)

            component_class = item.class_name
            reading = self._read_mass(component_class)
            item.attach_weight(reading.grams, str(reading.status), reading.timestamp)
            item.weight_reading = reading.as_dict()
            # A mass is forwarded when it carries a real quantity - a settled
            # measurement, or a labelled stand-in. An UNAVAILABLE reading is
            # withheld entirely: its grams field is 0.0, and 0.0 with no
            # simulated flag would classify as MEASURED downstream.
            has_quantity = reading.status in (WeightStatus.MEASURED, WeightStatus.SIMULATED)
            valuation = valuation_module.value(
                {component_class: 1} if component_class else {},
                mass=reading.as_dict() if has_quantity else None,
                item_id=item.item_id,
            )
            decision = decision_engine.decide(
                component_class, item.confidence, valuation, cfg=self.cfg
            )
            item.valuation = valuation.as_dict()
            item.decision = decision.as_dict()
            item.actuation = self._actuate(item.item_id, decision)

            # Keep the finished record here rather than relying on the tracker.
            # `drain_finalized()` empties its list by design - that is what stops
            # one item reaching the ledger twice - so an item that leaves the
            # frame would otherwise vanish from the run's history a few seconds
            # after its paddle fired.
            record = item.as_dict()
            self._routed[item.item_id] = record
            return record

    def _resolve(self, item_id: str | None):
        """The item to act on, or a refusal explaining why there is none."""
        if item_id:
            item = self.pipeline.tracker.get(item_id)
            if item is None:
                return {
                    "error": "UNKNOWN_ITEM",
                    "reason": f"{item_id} is not in this run's item lifecycle.",
                }
            return item
        item = self.pipeline.current_item
        if item is None:
            return {
                "error": "NO_ITEM",
                "reason": (
                    "No confirmed item. Hold the component in front of the camera until "
                    "it is CONFIRMED - an object seen once is not yet something to weigh."
                ),
            }
        return item

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

    def _mock_mass(self, why: str, component_class: str | None = None) -> WeightReading:
        """A labelled stand-in mass, for a demonstration with no usable cell.

        `simulated` is true and the status is SIMULATED, which is what carries
        the fabrication forward: PMDI, the valuation and the decision all end
        up stamped SIMULATED, and the dashboard shows it as such. Nothing here
        can reach MEASURED.
        """
        grams = self.mock_mass_for(component_class)
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

    def _read_mass(self, component_class: str | None = None) -> WeightReading:
        mock = bool(self.cfg["demo.mock_mass.enabled"])
        sensor = self.weight_sensor
        if sensor is None:
            why = "No load cell is connected."
            return (
                self._mock_mass(why, component_class)
                if mock
                else _unavailable_reading(f"{why} Nothing is estimated in place of one.")
            )
        if not self.calibration.present:
            why = "The load cell is not calibrated."
            return (
                self._mock_mass(why, component_class)
                if mock
                else _unavailable_reading(
                    f"{why} Run `python -m app.calibrate` and verify against a second known mass."
                )
            )
        reading = sensor.read()
        # A cell that is connected and calibrated but could not settle still
        # falls back, so a flaky reading does not stall a demonstration.
        if mock and not reading.usable:
            return self._mock_mass(f"The cell returned {reading.status}.", component_class)
        return reading

    def _actuate(self, item_id: str, decision) -> dict:
        """Turn a decision into a paddle movement, or record why it is not one."""
        target = str(decision.decision)
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

    def _refusal(self, item: TrackedItem, code: str, reason: str) -> dict:
        return {"error": code, "reason": reason, "item": item.as_dict()}

    # -- reporting ---------------------------------------------------------
    def snapshot(self) -> dict:
        """Everything the dashboard renders, in one read."""
        with self._lock:
            current = self.pipeline.current_item
            # Everything routed this run, plus anything currently in view that
            # has not been routed yet. Routed records win: they carry the mass,
            # the decision and the actuation.
            live = [
                i.as_dict()
                for i in sorted(
                    self.pipeline.tracker.active, key=lambda i: i.first_frame, reverse=True
                )
                if i.item_id not in self._routed
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
                "current_item": current.as_dict() if current else None,
                "items": items[:25],
                "confirmed_count": sum(
                    1 for i in self.pipeline.tracker.active if i.state is ItemState.CONFIRMED
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
                        "No conveyor exists. The operator carries the component between "
                        "the camera, the load cell and the bins. Routing is immediate, "
                        "not scheduled."
                    ),
                },
            }
