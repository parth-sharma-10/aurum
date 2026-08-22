"""The batch ledger — every read and write of the SQLite store lives here.

Before this module the only INSERT was inline in the API's close handler, so a
batch saved from the OpenCV demo went to a JSON file and never reached the
ledger the dashboard reads. Two persistence paths that disagree is worse than
one that is merely incomplete: an operator saw a batch on stage and could not
find it afterwards. Both callers now go through `save()`.

The record itself stays the source of truth. The columns exist to make a batch
listable and sortable without parsing JSON; `record_json` holds everything, and
anything read back comes from there, so adding a field to a record needs no
migration.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "aurum_batches.db"

SCHEMA = """
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
"""


def _connect() -> sqlite3.Connection:
    """Open the ledger, creating its directory if this is the first write.

    `DB` is read at call time rather than captured, so a test can point the
    whole module at a temporary file.
    """
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with closing(_connect()) as con:
        con.execute(SCHEMA)
        con.commit()


def save(record: dict) -> None:
    """Write one closed batch. The only INSERT in the codebase.

    Idempotent by batch id: re-saving the same batch replaces it rather than
    failing, because a demo operator pressing S twice is not an error worth
    ending the demo over.
    """
    w = record.get("weight") or {}
    with closing(_connect()) as con:
        con.execute(SCHEMA)
        con.execute(
            """INSERT OR REPLACE INTO batches
               (batch_id, created_at, model_version, total_objects,
                avg_confidence, weight_grams, weight_simulated, record_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                record["batch_id"],
                record["timestamp"],
                record["model_version"],
                record["total_objects"],
                record["average_confidence"],
                w.get("grams"),
                int(bool(w.get("simulated"))) if w else None,
                json.dumps(record),
            ),
        )
        con.commit()


def recent(limit: int = 50) -> list[dict]:
    with closing(_connect()) as con:
        con.execute(SCHEMA)
        rows = con.execute(
            "SELECT record_json FROM batches ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [json.loads(r["record_json"]) for r in rows]


def get(batch_id: str) -> dict | None:
    with closing(_connect()) as con:
        con.execute(SCHEMA)
        row = con.execute(
            "SELECT record_json FROM batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
    return json.loads(row["record_json"]) if row else None


def aggregates() -> dict:
    """Ledger totals, computed in SQL.

    Mass is returned as two separate figures. Aurum's weight input is either a
    real HX711 reading or a labelled simulation, and summing them would produce
    a number that reads as measured. Only a row that explicitly recorded
    `weight_simulated = 0` counts as measured, so a record with missing
    provenance falls on the cautious side.
    """
    with closing(_connect()) as con:
        con.execute(SCHEMA)
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

    return {
        "batch_count": totals["batches"],
        "total_count": totals["objects"],
        "total_weight": weight,
        "component_breakdown": {r["component"]: int(r["n"]) for r in components},
    }
