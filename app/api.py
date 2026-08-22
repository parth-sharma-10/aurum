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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from app import ledger, pricing
from app.batch import BatchSession
from app.dashboard import draw_detections
from app.decision import engine as decision_engine
from app.detector import DEFAULT_WEIGHTS, AurumDetector
from app.pipeline import ItemPipeline
from app.valuation import prices as prices_module
from app.valuation import valuation as valuation_module
from app.weight import get_weight_source

ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Create the batch ledger before the first request is served."""
    ledger.init_db()
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
    allow_methods=["GET"],
    allow_headers=["*"],
)

_detector: AurumDetector | None = None
_sessions: dict[str, BatchSession] = {}
_pipeline: ItemPipeline | None = None


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
    """Start a fresh run: new identities, tracker numbering restarted."""
    pipeline().reset()
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
            "Aurum does not implement physical bin routing or servo actuation. No "
            "stored record carries a bin assignment, so this is empty by fact, not "
            "by omission."
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
    """What each metal costs, and whether that figure can be trusted.

    Aurum ships no market data source. With `pricing.provider: unavailable`
    every entry comes back UNAVAILABLE with the setting that would change it,
    rather than a number nobody can attribute.
    """
    service = prices_module.PriceService.from_config()
    quotes = service.prices(prices_module.materials.METAL_NAMES)
    return {
        "provider": getattr(service.provider, "name", "unknown"),
        "max_age_seconds": service.max_age_seconds,
        "prices": {metal: quote.as_dict() for metal, quote in sorted(quotes.items())},
        "note": (
            "No live market data source is approved for this project. A price "
            "labelled TEST is fixture data and is not a market quote."
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
