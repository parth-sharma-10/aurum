"""The EPR ledger: one durable row per thing that happened to one physical item.

`app.ledger` stores closed BATCHES — how many components a session saw, and
what they weighed in aggregate. That is the right shape for a recycler's
throughput and the wrong shape for Extended Producer Responsibility, which
asks about one object: what was it, what did it weigh, what was it worth, which
bin did it reach, and on what evidence.

    DETECTED -> CLASSIFIED -> WEIGHED -> COMPOSITION_LOOKUP -> PMDI_CALCULATED
      -> VALUE_CALCULATED -> BIN_ASSIGNED -> SERVO_SCHEDULED -> SERVO_TRIGGERED
      -> SORT_CONFIRMED | SORT_FAILURE

Three properties this module exists to hold.

**Append-only.** An event is a statement that something happened at a time.
Nothing here updates a row, because a trail that can be rewritten answers no
question worth asking. Re-recording the same event for the same item is
idempotent by `(item_id, event)` rather than an error, so a retried write does
not produce two DETECTED rows for one object.

**Provenance travels on every event, not on the run.** The model version, the
evidence database, the price snapshot, the grading policy, the calibration and
the hardware mode are stamped on each row. Storing them once per session would
be smaller and would quietly lie the moment anything is reconfigured mid-run.

**Simulation stays distinguishable for ever.** `simulated` is a column, not a
field inside the JSON, so "show me every item sorted on a real measurement" is
one WHERE clause rather than a full-table deserialise. A stand-in mass and a
weighed one must never add up together, in this file least of all.

**SORT_CONFIRMED is not written on hope.** It means the actuation contract was
satisfied: a frame went out and the board acknowledged it. A disconnected
board, a timeout or a bin with no servo produce SORT_FAILURE or nothing, never
a confirmation. An ACK is still not proof a servo physically moved, and the
event payload says so.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from app import config as config_module

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "aurum_epr.db"

#: The version of this software, for the provenance stamp. Bumped with the
#: project version in pyproject.toml.
SOFTWARE_VERSION = "0.1.0"

#: The PMDI formula's own version, separate from the software's. The formula is
#: from the concept document §4 and has not changed; if it ever does, an event
#: recorded under the old one must remain readable as such.
PMDI_VERSION = "1.0"

SCHEMA = """
CREATE TABLE IF NOT EXISTS epr_events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      TEXT NOT NULL,
    session_id   TEXT,
    event        TEXT NOT NULL,
    at           TEXT NOT NULL,
    simulated    INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    UNIQUE (item_id, event)
);
CREATE INDEX IF NOT EXISTS epr_events_item ON epr_events (item_id);
CREATE INDEX IF NOT EXISTS epr_events_at ON epr_events (at);
"""


class EprEvent(StrEnum):
    """What happened. The order here is the order it happens in."""

    DETECTED = "DETECTED"
    CLASSIFIED = "CLASSIFIED"
    WEIGHED = "WEIGHED"
    COMPOSITION_LOOKUP = "COMPOSITION_LOOKUP"
    PMDI_CALCULATED = "PMDI_CALCULATED"
    VALUE_CALCULATED = "VALUE_CALCULATED"
    BIN_ASSIGNED = "BIN_ASSIGNED"
    SERVO_SCHEDULED = "SERVO_SCHEDULED"
    SERVO_TRIGGERED = "SERVO_TRIGGERED"
    #: The actuation contract was satisfied: frame sent, board acknowledged.
    SORT_CONFIRMED = "SORT_CONFIRMED"
    #: It was not. Disconnected, timed out, refused, or faulted.
    SORT_FAILURE = "SORT_FAILURE"


#: The order above, as a sort key, so a trail reads in machine order rather
#: than in whatever order the rows came back.
ORDER = {event: i for i, event in enumerate(EprEvent)}


def _connect() -> sqlite3.Connection:
    """Open the ledger. `DB` is read at call time so a test can redirect it."""
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with closing(_connect()) as con:
        con.executescript(SCHEMA)
        con.commit()


def provenance(
    cfg: config_module.Config | None = None,
    model_version: str | None = None,
    calibration: dict | None = None,
) -> dict:
    """Everything needed to reproduce a figure, stamped on every event.

    `model_version` and `calibration` are passed in rather than read here: the
    detector is expensive to import and the calibration belongs to one physical
    machine, and this module has no business owning either.
    """
    cfg = config_module.load() if cfg is None else cfg
    from app import materials
    from app.routing.conveyor import hardware_mode

    database = materials.load()
    return {
        "software_version": SOFTWARE_VERSION,
        "pmdi_version": PMDI_VERSION,
        "vision_model_version": model_version,
        "composition_db_schema": database.get("schema_version"),
        "composition_db_evidence_count": len(database.get("evidence") or {}),
        "price_provider": cfg["pricing.provider"],
        "price_currency": cfg["pricing.currency"],
        "grading_policy": {
            "class_aware": cfg["grading.policy.class_aware"],
            "price_unavailable_policy": cfg["grading.policy.price_unavailable_policy"],
            "fallback": cfg["grading.fallback"],
            "bin_a_min_precious_fraction_ppm": cfg["grading.bin_a.minimum_precious_fraction_ppm"],
            "bin_b_min_precious_fraction_ppm": cfg["grading.bin_b.minimum_precious_fraction_ppm"],
        },
        "calibration": calibration
        or {"verified": False, "note": "No calibration was supplied to this event."},
        "hardware_mode": hardware_mode(cfg),
        "conveyor_mode": cfg["conveyor.mode"],
        "mock_mass_enabled": bool(cfg["demo.mock_mass.enabled"]),
    }


def record(
    item_id: str,
    event: EprEvent | str,
    payload: dict | None = None,
    session_id: str | None = None,
    prov: dict | None = None,
    simulated: bool = False,
    at: str | None = None,
) -> dict:
    """Append one event. Idempotent per (item_id, event).

    Idempotent rather than erroring because the pipeline may legitimately reach
    the same stage twice for one object - a retried manual measurement, a pan
    cycle re-entered - and a duplicate row would make a trail read as though
    the object had been weighed twice.
    """
    row = {
        "item_id": item_id,
        "session_id": session_id,
        "event": str(event),
        "at": at or datetime.now(UTC).isoformat(timespec="seconds"),
        "simulated": int(bool(simulated)),
        "payload": payload or {},
        "provenance": prov or {},
    }
    with closing(_connect()) as con:
        con.executescript(SCHEMA)
        con.execute(
            """INSERT OR IGNORE INTO epr_events
               (item_id, session_id, event, at, simulated, payload_json, provenance_json)
               VALUES (?,?,?,?,?,?,?)""",
            (
                row["item_id"],
                row["session_id"],
                row["event"],
                row["at"],
                row["simulated"],
                json.dumps(row["payload"], default=str),
                json.dumps(row["provenance"], default=str),
            ),
        )
        con.commit()
    return row


def _row(row: sqlite3.Row) -> dict:
    return {
        "event_id": row["event_id"],
        "item_id": row["item_id"],
        "session_id": row["session_id"],
        "event": row["event"],
        "at": row["at"],
        "simulated": bool(row["simulated"]),
        "payload": json.loads(row["payload_json"]),
        "provenance": json.loads(row["provenance_json"]),
    }


def history(item_id: str) -> list[dict]:
    """One item's whole trail, in machine order."""
    with closing(_connect()) as con:
        con.executescript(SCHEMA)
        rows = con.execute(
            "SELECT * FROM epr_events WHERE item_id = ? ORDER BY event_id", (item_id,)
        ).fetchall()
    return sorted((_row(r) for r in rows), key=lambda e: ORDER.get(e["event"], 99))


def recent(limit: int = 100) -> list[dict]:
    with closing(_connect()) as con:
        con.executescript(SCHEMA)
        rows = con.execute(
            "SELECT * FROM epr_events ORDER BY event_id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row(r) for r in rows]


def items(limit: int = 50) -> list[dict]:
    """One summary row per physical item, most recent first.

    `sorted_confirmed` counts only items whose actuation contract was actually
    satisfied, and `measured` excludes anything that touched a stand-in mass -
    both as columns, so neither can be quietly summed with its opposite.
    """
    with closing(_connect()) as con:
        con.executescript(SCHEMA)
        rows = con.execute(
            """SELECT item_id,
                      MIN(at) AS first_seen,
                      MAX(at) AS last_seen,
                      COUNT(*) AS events,
                      MAX(simulated) AS simulated,
                      MAX(event = 'SORT_CONFIRMED') AS confirmed,
                      MAX(event = 'SORT_FAILURE') AS failed
               FROM epr_events
               GROUP BY item_id
               ORDER BY MAX(event_id) DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [
        {
            "item_id": r["item_id"],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "events": r["events"],
            "simulated": bool(r["simulated"]),
            "sort_confirmed": bool(r["confirmed"]),
            "sort_failed": bool(r["failed"]),
        }
        for r in rows
    ]


def aggregates() -> dict:
    """Totals over the item trail, computed in SQL."""
    with closing(_connect()) as con:
        con.executescript(SCHEMA)
        totals = con.execute(
            """SELECT COUNT(DISTINCT item_id) AS items, COUNT(*) AS events
               FROM epr_events"""
        ).fetchone()
        outcomes = con.execute(
            """SELECT event, COUNT(DISTINCT item_id) AS n
               FROM epr_events
               WHERE event IN ('SORT_CONFIRMED', 'SORT_FAILURE')
               GROUP BY event"""
        ).fetchall()
        measured = con.execute(
            """SELECT COUNT(DISTINCT item_id) AS n FROM epr_events
               WHERE item_id NOT IN (SELECT item_id FROM epr_events WHERE simulated = 1)"""
        ).fetchone()
    by_outcome = {r["event"]: r["n"] for r in outcomes}
    return {
        "items": totals["items"],
        "events": totals["events"],
        "sort_confirmed": by_outcome.get("SORT_CONFIRMED", 0),
        "sort_failed": by_outcome.get("SORT_FAILURE", 0),
        "items_with_no_simulated_input": measured["n"],
        "note": (
            "SORT_CONFIRMED means a frame was written and the board acknowledged "
            "it. It is not evidence that a servo physically moved. An item that "
            "touched a stand-in mass is excluded from the measured count entirely."
        ),
    }
