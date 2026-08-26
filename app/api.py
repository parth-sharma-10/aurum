"""FastAPI service exposing Aurum Vision to the rest of the Aurum stack.

This is the seam the deck describes: the vision layer publishes identifications
and batch records; orchestration, pricing and the EPR ledger consume them. It
deliberately stops at identification — there is no endpoint that returns a
metal content, because the model does not produce one.

    uvicorn app.api:app --reload --port 8000
    open http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import io
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

from app import config as config_module
from app import epr, ledger, pricing
from app.batch import BatchSession
from app.dashboard import draw_detections
from app.decision import engine as decision_engine
from app.detector import DEFAULT_WEIGHTS, AurumDetector
from app.pipeline import DemoSession, ItemPipeline
from app.routing import Conveyor, RoutingScheduler
from app.valuation import prices as prices_module
from app.valuation import valuation as valuation_module
from app.weight import get_weight_source

ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Create both ledgers before the first request is served."""
    ledger.init_db()
    epr.init_db()
    yield


app = FastAPI(
    title="Aurum Vision API",
    version="0.1",
    description=(
        "E-waste component identification. Returns component identities, counts "
        "and confidences. Does NOT measure precious-metal content."
    ),
    lifespan=lifespan,
)

# The browser dashboard in frontend/ is served by Vite on 5173 during
# development, which is a different origin from this service. Only that origin
# is allowed, only for reads, and without credentials: the API has no auth, so a
# wildcard would let any page a developer visits read their local ledger.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    # POST is allowed because the demonstration dashboard drives the session:
    # start the camera, connect the board, weigh the item on the pan. Still
    # only that one origin, and still no credentials.
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_detector: AurumDetector | None = None
_sessions: dict[str, BatchSession] = {}
_pipeline: ItemPipeline | None = None
_routing: RoutingScheduler | None = None
_belt: Conveyor | None = None
_demo: DemoSession | None = None


def demo_session() -> DemoSession:
    """The demonstration session for this process.

    One per process, because item identity is only meaningful within a run and
    there is only one camera and one board to go round.
    """
    global _demo
    if _demo is None:
        _demo = DemoSession(detector=detector())
    return _demo


def _conveyor() -> Conveyor:
    """The belt for this process, whatever `conveyor.mode` says it is."""
    global _belt
    if _belt is None:
        _belt = Conveyor.from_config()
    return _belt


def _scheduler() -> RoutingScheduler:
    """The routing queue for this process, sharing the tracker's identities."""
    global _routing
    if _routing is None:
        _routing = RoutingScheduler(lifecycle=pipeline().tracker, conveyor=_conveyor())
    return _routing


def pipeline() -> ItemPipeline:
    """The tracking session this process is running.

    One per process, because item identity is only meaningful within a run.
    """
    global _pipeline
    if _pipeline is None:
        _pipeline = ItemPipeline(detector=detector())
    return _pipeline


def detector() -> AurumDetector:
    global _detector
    if _detector is None:
        if not DEFAULT_WEIGHTS.exists():
            raise HTTPException(503, f"Model not trained yet: {DEFAULT_WEIGHTS} missing")
        _detector = AurumDetector(DEFAULT_WEIGHTS)
        _detector.warmup()
    return _detector


def _rel(p: Path) -> str:
    """Path for display. Weights may legitimately live outside the repo."""
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


def _decode(data: bytes) -> np.ndarray:
    # cv2.imdecode on a zero-length buffer raises rather than returning None,
    # which would surface as a 500 for what is really a bad request.
    if not data:
        raise HTTPException(400, "Empty upload: no image data received")
    try:
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    except cv2.error as exc:
        raise HTTPException(400, f"Could not decode image: {exc}") from exc
    if img is None:
        raise HTTPException(400, "Could not decode image")
    return img


@app.get("/health")
def health() -> dict:
    ok = DEFAULT_WEIGHTS.exists()
    return {
        "status": "ok" if ok else "model_missing",
        "model_version": detector().model_version if ok else None,
        "classes": detector().classes if ok else [],
        "weights": _rel(DEFAULT_WEIGHTS),
    }


@app.get("/model")
def model_info() -> dict:
    d = detector()
    metrics_path = ROOT / "reports" / "test_metrics.json"
    return {
        "model_version": d.model_version,
        "classes": d.classes,
        "metadata": d.meta,
        "test_metrics": (json.loads(metrics_path.read_text()) if metrics_path.exists() else None),
        "disclaimer": (
            "Aurum Vision identifies visible component categories from RGB "
            "imagery. It does not measure precious-metal composition."
        ),
    }


@app.post("/detect")
async def detect(file: UploadFile = File(...)) -> dict:
    """Single-image detection: boxes, classes, confidences, counts."""
    d = detector()
    res = d.predict(_decode(await file.read()))
    return {
        "model_version": d.model_version,
        "detections": [
            {"class": x.cls, "confidence": round(x.conf, 4), "box_xyxy": list(x.xyxy)}
            for x in res.detections
        ],
        "counts": {c: res.counts.get(c, 0) for c in d.classes},
        "total_objects": len(res.detections),
        "average_confidence": round(res.mean_confidence, 4),
        "inference_ms": round(res.inference_ms, 2),
    }


@app.post("/detect/annotated")
async def detect_annotated(file: UploadFile = File(...)) -> Response:
    """Same as /detect but returns the annotated JPEG, for quick visual checks."""
    d = detector()
    img = _decode(await file.read())
    res = d.predict(img)
    ok, buf = cv2.imencode(".jpg", draw_detections(img, res.detections))
    if not ok:
        raise HTTPException(500, "Could not encode annotated image")
    return Response(io.BytesIO(buf.tobytes()).getvalue(), media_type="image/jpeg")


@app.get("/routing")
def routing_status() -> dict:
    """The routing queue: what is scheduled, what is due, and what was refused.

    A route is a time, not a movement. A DUE route is one the actuation layer
    will act on; nothing here moves a servo.
    """
    return {**_scheduler().snapshot(now=time.monotonic()), "conveyor": _conveyor().snapshot()}


@app.get("/conveyor")
def conveyor_status() -> dict:
    """The belt: mode, speed, where that speed came from, and the ETAs it implies.

    `mode: NONE` is the shipped answer and is a fact about this machine, not a
    missing configuration: there is no conveyor. `SPEED ... (SIMULATED)` is a
    demonstration value and says so on every reading.
    """
    return _conveyor().snapshot()


# ---------------------------------------------------------------------------
# The session.
#
#   camera -> assembly identity -> [operator puts it on the pan] -> load cell
#   -> PMDI -> A/B/C -> Servo A / Servo B / nothing -> [object removed]
#
# THE LOAD CELL DRIVES THIS. There is no endpoint in the normal path: the pan
# state machine detects the object, waits for the mass to settle, grades it and
# routes it on its own. `POST /session/measure` is a developer fallback and is
# documented as one.
#
# There is no conveyor, so there is no scheduling here either: the decision is
# taken and the paddle moves. The operator never says which bin.
# ---------------------------------------------------------------------------


@app.post("/session/start")
def session_start(mode: str = "webcam", path: str | None = None) -> dict:
    """Open the camera and begin detecting and tracking in the background."""
    return demo_session().start_camera(mode=mode, path=path)


@app.post("/session/board/connect")
def session_board_connect() -> dict:
    """Open the single serial link the HX711 and both servos share."""
    return demo_session().connect_board()


@app.get("/session")
def session_state() -> dict:
    """Everything the dashboard renders: items, evidence, decisions, hardware."""
    return demo_session().snapshot()


@app.post("/session/measure")
def session_measure(item_id: str | None = None) -> dict:
    """DEVELOPER FALLBACK. Weigh the object on the pan now, grade it, route it.

    Not the normal path. The load cell triggers a measurement by itself and no
    call is required to sort an object; this exists for a bench with no working
    cell, a mass that will not settle, and driving the chain from a terminal.

    Defaults to the assembly most recently confirmed. The classes come from the
    model, the mass from the cell and the bin from the decision engine: no
    route can be requested through this endpoint.
    """
    return demo_session().measure_and_route(item_id)


@app.get("/session/pan")
def session_pan() -> dict:
    """The automatic weighing cycle: where it is, and why it is there."""
    return demo_session().pan.snapshot()


@app.post("/session/stop")
def session_stop() -> dict:
    """Release the camera and the serial port."""
    demo_session().stop()
    return {"running": False}


@app.get("/session/frame")
def session_frame() -> Response:
    """The most recent annotated frame, as a JPEG."""
    frame = demo_session().latest_jpeg()
    if frame is None:
        raise HTTPException(503, "No frame yet. Start the session with POST /session/start.")
    return Response(frame, media_type="image/jpeg")


@app.get("/session/stream")
def session_stream() -> StreamingResponse:
    """The annotated camera feed as multipart MJPEG, for an <img> tag."""

    def frames():
        while True:
            frame = demo_session().latest_jpeg()
            if frame is not None:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            time.sleep(0.05)

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/arduino")
def arduino_status() -> dict:
    """The link to the board, and every command this run has issued.

    `ACKED` means the board received a well-formed frame and reports it acted.
    It is not evidence that a servo physically moved.
    """
    session = demo_session()
    return {
        "board": session.link.snapshot() if session.link else {"connected": False},
        "actuation": (
            session.controller.snapshot()
            if session.controller
            else {
                "connected": False,
                "actuation_enabled": False,
                "reason": "No board is connected. POST /session/board/connect.",
            }
        ),
        "wiring": {
            "servo_a_pin": "D9",
            "servo_b_pin": "D10",
            "hx711_dout": "D2",
            "hx711_sck": "D3",
            "bin_c": "no servo - reached by this system doing nothing",
        },
    }


@app.get("/hardware")
def hardware_status() -> dict:
    """The machine's physical state: mode, link, fault, servo geometry."""
    return demo_session().snapshot()["hardware"]


@app.post("/hardware/fault/reset")
def hardware_fault_reset() -> dict:
    """Clear a latched hardware fault. Nothing does this automatically.

    A fault latches because a command that went unacknowledged may have left a
    paddle in a position nobody knows. Clearing it is a statement that somebody
    has looked at the rig, so it is a deliberate call and it is recorded.
    """
    session = demo_session()
    cleared = session.fault.reset(by="dashboard")
    return {
        "cleared": cleared.as_dict() if cleared else None,
        "fault": session.fault.snapshot(),
    }


@app.get("/errors")
def error_log() -> dict:
    """Failures this run recorded, newest first, by code.

    A recorded failure is not a crash. Aurum keeps running and routes what it
    cannot read to Bin C; this is where the reason for that ends up.
    """
    return demo_session().errors.snapshot(limit=50)


@app.get("/epr")
def epr_items(limit: int = 50) -> dict:
    """One summary row per physical item the EPR ledger has heard of."""
    return {"items": epr.items(limit), "aggregates": epr.aggregates()}


@app.get("/epr/{item_id}")
def epr_item(item_id: str) -> dict:
    """One item's whole trail: every event, with the provenance of each.

    This is the Extended Producer Responsibility record. It answers, for one
    physical object: what was it, what did it weigh, was that weighed or
    assumed, what was it worth, which bin did it reach, and on what model,
    evidence database, price snapshot and grading policy.
    """
    trail = epr.history(item_id)
    if not trail:
        raise HTTPException(404, f"No EPR record for {item_id}")
    return {
        "item_id": item_id,
        "events": trail,
        "sort_confirmed": any(e["event"] == "SORT_CONFIRMED" for e in trail),
        "simulated_inputs": any(e["simulated"] for e in trail),
        "provenance": trail[-1]["provenance"],
    }


@app.post("/track")
async def track(file: UploadFile = File(...)) -> dict:
    """One frame of a tracking run: detections folded into item lifecycles.

    Unlike `/detect`, this is stateful. Repeated calls with the same object
    continue one item rather than reporting a new one each time, which is what
    keeps a single physical component from becoming several ledger rows.
    """
    p = pipeline()
    items = p.process_detections(
        p.detector_tracker.track(_decode(await file.read())),
    )
    return {
        "frames_processed": p.frames_processed,
        "active_items": [item.as_dict() for item in items],
        "current_item": p.current_item.as_dict() if p.current_item else None,
    }


@app.get("/items")
def list_items() -> dict:
    """Items in the current tracking run."""
    return pipeline().snapshot()


@app.get("/items/current")
def current_item() -> dict:
    """The confirmed item most recently seen, or an explicit absence."""
    item = pipeline().current_item
    if item is None:
        return {
            "current_item": None,
            "reason": (
                "No confirmed item. An object seen once is not yet something to weigh or route."
            ),
        }
    return {"current_item": item.as_dict()}


@app.post("/track/reset")
def reset_tracking() -> dict:
    """Start a fresh run: new identities, tracker numbering restarted.

    Resets BOTH tracking runs this process holds. `/track` drives a standalone
    pipeline and the dashboard drives its own inside `DemoSession`, and an
    operator asking for a new item means the one in front of them - not
    whichever of the two this endpoint happened to own first.
    """
    pipeline().reset()
    demo_session().reset()
    return {"status": "reset", "frames_processed": 0}


@app.post("/batch/start")
def batch_start() -> dict:
    d = detector()
    s = BatchSession(classes=d.classes)
    _sessions[s.batch_id] = s
    return {"batch_id": s.batch_id, "started_at": s.started_at}


@app.post("/batch/{batch_id}/frame")
async def batch_frame(batch_id: str, file: UploadFile = File(...)) -> dict:
    s = _sessions.get(batch_id)
    if s is None:
        raise HTTPException(404, f"Unknown batch {batch_id}")
    res = detector().predict(_decode(await file.read()))
    s.add_frame(res.counts, res.mean_confidence)
    return {
        "batch_id": batch_id,
        "frames_observed": s.frames_seen,
        "frame_counts": res.counts,
        "stable_counts": s.stable_counts(),
    }


@app.post("/batch/{batch_id}/close")
def batch_close(batch_id: str, weight_mode: str = "off", hx711_port: str | None = None) -> dict:
    """Finalize a batch, persist it to SQLite, and return the record."""
    s = _sessions.pop(batch_id, None)
    if s is None:
        raise HTTPException(404, f"Unknown batch {batch_id}")
    d = detector()
    wsrc = get_weight_source(weight_mode, hx711_port) if weight_mode != "off" else None
    rec = s.record(d.model_version, wsrc.read().as_dict() if wsrc else None, source="api")
    s.save(rec)
    ledger.save(rec)
    return rec


@app.get("/stats")
def stats() -> dict:
    """Aggregates over the stored ledger. See app.ledger.aggregates for the SQL.

    `bin_breakdown` is empty by fact rather than omission, and stays that way
    until an actuator exists to fill it.
    """
    agg = ledger.aggregates()
    agg["total_weight"]["note"] = (
        "Simulated grams come from a labelled stand-in for the HX711 load cell "
        "and are not physical measurements."
    )
    return {
        **agg,
        "bin_breakdown": {},
        "bin_breakdown_note": (
            "Empty by fact, not by omission: these aggregates are over the stored "
            "batch ledger, and a batch carries no bin assignment. Per-item bins and "
            "servo actuation live in the demonstration session (GET /session), which "
            "is held in memory for the length of a run and is not written here."
        ),
    }


@app.get("/batches")
def list_batches(limit: int = 50) -> dict:
    records = ledger.recent(limit)
    return {"count": len(records), "batches": records}


@app.get("/batches/{batch_id}")
def get_batch(batch_id: str) -> dict:
    record = ledger.get(batch_id)
    if record is None:
        raise HTTPException(404, f"No batch {batch_id}")
    return record


@app.get("/prices")
def metal_prices() -> dict:
    """What each metal costs, whether that figure is current, and where it came from.

    Every quote carries its own status. LIVE is a market feed answering now;
    REFERENCE is a real published price being used deliberately after its date;
    STALE is a feed that should have been current and was not; UNAVAILABLE is
    an explicit absence. No path here produces a number without a source, and
    none produces a zero for a price that could not be fetched.

    The API key is never in this payload. See app/valuation/metalprice.py.
    """
    cfg = config_module.load()
    service = prices_module.PriceService.from_config(cfg)
    quotes = service.prices(prices_module.materials.METAL_NAMES)
    return {
        "provider": getattr(service.provider, "name", "unknown"),
        "configured_provider": cfg["pricing.provider"],
        "currency": cfg["pricing.currency"],
        "max_age_seconds": service.max_age_seconds,
        "prices": {metal: quote.as_dict() for metal, quote in sorted(quotes.items())},
        "note": (
            "A price labelled TEST is fixture data. A price labelled REFERENCE is "
            "a real published figure being used after its date, never a live quote. "
            "Only LIVE is a current market price."
        ),
    }


@app.get("/batches/{batch_id}/valuation")
def batch_valuation(batch_id: str) -> dict:
    """Estimated value of a stored batch, priced at request time.

    Not stored on the record and not part of `/batches`: a price is
    time-varying external data, so baking one into a batch would make the record
    silently wrong the next day. The quote's own source and timestamp travel
    with the result instead.

    Returns `available: false` with a reason whenever recovery estimation or the
    price source is unconfigured, which is the current shipped state.
    """
    record = ledger.get(batch_id)
    if record is None:
        raise HTTPException(404, f"No batch {batch_id}")
    result = valuation_module.value(
        record.get("detections", {}),
        mass=record.get("weight"),
        item_id=batch_id,
    )
    return {
        "batch_id": batch_id,
        "detected_components": record.get("detections", {}),
        "recovery_estimate": record.get("recovery_estimate", {}),
        # The recovery-based path, kept working for existing consumers.
        # Phase 10 consolidates it into the valuation subsystem below.
        "valuation": pricing.value_recovery(record.get("recovery_estimate", {})),
        # The PMDI subsystem: the precious-metal signal, and the valuation that
        # adds the separate base-metal signal to it.
        "pmdi": result.pmdi.as_dict(),
        "item_valuation": result.as_dict(),
        # The sorting policy's verdict on that evidence. A batch holding more
        # than one class has no single component class, so it cannot be routed
        # and the engine says so rather than picking one.
        "decision": decision_engine.decide(
            result.component_class,
            record.get("average_confidence"),
            result,
        ).as_dict(),
    }
