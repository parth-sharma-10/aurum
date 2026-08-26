"""The run, as a file somebody can open.

Everything a run produces is available over the API as JSON, which is the right
shape for the dashboard and the wrong one for the person who has to hand a
result to a judge, a supervisor or a spreadsheet. This is the same records, flat.

**Every figure carries the status of what it was derived from.** A mass column
without its `mass_status` beside it is a number that looks measured and may be
assumed, and a value without its `price_status` looks like a market price and
may be a reference figure from last week. The columns are paired on purpose,
and no row is exported without them.

CSV only. A PDF would need a dependency this project does not have, and a
spreadsheet opens this.
"""

from __future__ import annotations

import csv
import io

#: Column order, and the contract. Paired: a figure, then what backs it.
COLUMNS = (
    "item_id",
    "class_name",
    "confidence",
    "mass_g",
    "mass_status",
    "precious_mass_g",
    "precious_fraction_ppm",
    "precious_value",
    "contained_value",
    "currency",
    "price_status",
    "decision",
    "physical_bin",
    "reason_code",
    "servo",
    "actuation_state",
    "commanded",
    "first_seen",
    "last_seen",
)


def _get(record: dict, *path, default=None):
    """Walk a nested record, returning `default` at the first thing missing.

    Records differ by how far the item got: an object refused for an unciteable
    class has no valuation, and one routed with no board has no command. Those
    are empty cells, not exceptions.
    """
    current = record
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def row(record: dict) -> dict:
    """One routed item, flattened to the exported columns."""
    decision = record.get("decision") or {}
    actuation = record.get("actuation") or {}
    return {
        "item_id": record.get("item_id"),
        "class_name": record.get("class_name"),
        "confidence": record.get("confidence"),
        "mass_g": record.get("weight_g"),
        "mass_status": record.get("weight_status"),
        "precious_mass_g": _get(record, "valuation", "pmdi", "precious_mass_g"),
        "precious_fraction_ppm": _get(record, "valuation", "pmdi", "precious_mass_fraction_ppm"),
        # Both, because they go missing independently. A PCB whose copper has
        # no price has no `contained_value` at all, and exporting only that
        # column would drop the precious figure it does have — on the class
        # with the largest one in the set.
        "precious_value": _get(record, "valuation", "precious_value"),
        "contained_value": _get(record, "valuation", "contained_value"),
        "currency": _get(record, "valuation", "currency"),
        "price_status": _get(record, "valuation", "price_status"),
        "decision": decision.get("decision"),
        "physical_bin": decision.get("physical_bin"),
        "reason_code": decision.get("reason_code"),
        "servo": actuation.get("servo"),
        # An item that never reached the board has no command state, and an
        # empty cell is the honest rendering of that. "OK" would not be.
        "actuation_state": actuation.get("state"),
        "commanded": actuation.get("commanded"),
        "first_seen": record.get("first_seen"),
        "last_seen": record.get("last_seen"),
    }


def rows(snapshot: dict) -> list[dict]:
    """Every item this run actually decided something about, oldest first.

    Items still in view that have not been weighed are excluded: a row with a
    class and nothing else reads as a failed sort rather than an unfinished one.
    """
    items = snapshot.get("items") or []
    decided = [i for i in items if i.get("decision")]
    return [row(i) for i in reversed(decided)]


def to_csv(snapshot: dict) -> str:
    """The run as CSV, header included. An empty run is a header and no rows."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows(snapshot))
    return buffer.getvalue()


def filename(snapshot: dict) -> str:
    """Named after the run, so two exports cannot overwrite each other."""
    session_id = _get(snapshot, "epr", "session_id", default="aurum-run")
    return f"{session_id}.csv"
