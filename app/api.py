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
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from app.batch import BatchSession
from app.dashboard import draw_detections
from app.detector import DEFAULT_WEIGHTS, AurumDetector
from app.weight import get_weight_source

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "aurum_batches.db"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Create the batch ledger before the first request is served."""
    init_db()
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


def detector() -> AurumDetector:
    global _detector
    if _detector is None:
        if not DEFAULT_WEIGHTS.exists():
            raise HTTPException(503, f"Model not trained yet: {DEFAULT_WEIGHTS} missing")
        _detector = AurumDetector(DEFAULT_WEIGHTS)
        _detector.warmup()
    return _detector


def init_db() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB)) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS batches (
                batch_id       TEXT PRIMARY KEY,
                created_at     TEXT NOT NULL,
                model_version  TEXT NOT NULL,
                total_objects  INTEGER NOT NULL,
                avg_confidence REAL NOT NULL,
                weight_grams   REAL,
                weight_simulated INTEGER,
                record_json    TEXT NOT NULL
            )
        """)
        con.commit()


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

    w = rec.get("weight") or {}
    with closing(sqlite3.connect(DB)) as con:
        con.execute(
            "INSERT OR REPLACE INTO batches VALUES (?,?,?,?,?,?,?,?)",
            (
                rec["batch_id"],
                rec["timestamp"],
                rec["model_version"],
                rec["total_objects"],
                rec["average_confidence"],
                w.get("grams"),
                int(bool(w.get("simulated"))) if w else None,
                json.dumps(rec),
            ),
        )
        con.commit()
    return rec


@app.get("/stats")
def stats() -> dict:
    """Aggregates over the stored ledger, computed in SQL.

    Mass is reported as two separate totals rather than one. Aurum's weight
    input is either a real HX711 reading or a labelled simulation, and summing
    the two would produce a figure that reads as measured. Only a row that
    explicitly recorded `weight_simulated = 0` counts as measured, so a record
    with missing provenance falls on the cautious side instead of inflating the
    measured number.
    """
    with closing(sqlite3.connect(DB)) as con:
        con.row_factory = sqlite3.Row
        totals = con.execute(
            "SELECT COUNT(*) AS batches, COALESCE(SUM(total_objects), 0) AS objects FROM batches"
        ).fetchone()
        weights = con.execute("""
            SELECT weight_simulated = 0 AS measured,
                   COALESCE(SUM(weight_grams), 0) AS grams,
                   COUNT(*) AS n
            FROM batches
            WHERE weight_grams IS NOT NULL
            GROUP BY weight_simulated = 0
        """).fetchall()
        # Per-class counts live inside the stored record, not in a column.
        # json_each does the summation in SQLite rather than deserializing every
        # record in Python to add up four integers.
        components = con.execute("""
            SELECT d.key AS component, SUM(CAST(d.value AS INTEGER)) AS n
            FROM batches, json_each(batches.record_json, '$.detections') AS d
            GROUP BY d.key
            ORDER BY n DESC
        """).fetchall()

    weight = {"measured_grams": 0.0, "simulated_grams": 0.0, "batches_with_weight": 0}
    for row in weights:
        weight["measured_grams" if row["measured"] else "simulated_grams"] = round(
            float(row["grams"]), 1
        )
        weight["batches_with_weight"] += row["n"]
    weight["note"] = (
        "Simulated grams come from a labelled stand-in for the HX711 load cell "
        "and are not physical measurements."
    )

    return {
        "batch_count": totals["batches"],
        "total_count": totals["objects"],
        "total_weight": weight,
        "component_breakdown": {r["component"]: int(r["n"]) for r in components},
        "bin_breakdown": {},
        "bin_breakdown_note": (
            "Aurum does not implement physical bin routing or servo actuation. No "
            "stored record carries a bin assignment, so this is empty by fact, not "
            "by omission."
        ),
    }


@app.get("/batches")
def list_batches(limit: int = 50) -> dict:
    with closing(sqlite3.connect(DB)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM batches ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"count": len(rows), "batches": [json.loads(r["record_json"]) for r in rows]}


@app.get("/batches/{batch_id}")
def get_batch(batch_id: str) -> dict:
    with closing(sqlite3.connect(DB)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"No batch {batch_id}")
    return json.loads(row["record_json"])
