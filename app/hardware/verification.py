"""Whether anybody has established that a paddle physically moved.

The whole repository repeats one sentence: an ACK means the board received a
well-formed frame and believes it acted, and a stalled servo, a cut signal wire
and a dead supply rail all acknowledge identically. Until now that sentence
lived in prose — a `note` field on three snapshots, which nothing can check and
nothing can render as a state.

There are exactly two states, and no sensor can move between them:

    VERIFICATION_UNAVAILABLE      nobody has recorded watching this servo move
    PHYSICAL_MOVEMENT_VERIFIED    somebody watched it, and said so

**Software can only ever produce the first.** There is no encoder, no limit
switch and no camera on the paddle, so the second is reachable only by a human
answering `scripts/bench_check.py --move A`. That is not a gap to be closed
later with a better inference; it is the honest shape of the claim.

The record is written to `reports/` rather than `runs/` because it is evidence
and evidence is committed. It carries the angles that were in force, because a
paddle verified at one throw is not a paddle verified at another.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
RECORD = ROOT / "reports" / "movement_verification.json"


class MovementVerification(StrEnum):
    """Whether a paddle has been watched moving. Not whether it acknowledged."""

    #: The shipped state, and the state after any ACK. Says "nobody knows",
    #: never "it did not move".
    VERIFICATION_UNAVAILABLE = "VERIFICATION_UNAVAILABLE"
    #: A human watched this servo throw and recorded it.
    PHYSICAL_MOVEMENT_VERIFIED = "PHYSICAL_MOVEMENT_VERIFIED"


@dataclass(frozen=True)
class Observation:
    """One human's answer to "did the paddle move?", and what it was moving as."""

    servo: str
    moved: bool
    by: str
    at: str
    rest_deg: float | None = None
    push_deg: float | None = None
    hold_ms: float | None = None
    command_id: str | None = None

    @property
    def summary(self) -> str:
        seen = "watched moving" if self.moved else "commanded and NOT seen to move"
        throw = (
            f" at {self.rest_deg:.0f}->{self.push_deg:.0f} deg"
            if self.rest_deg is not None and self.push_deg is not None
            else ""
        )
        return f"SERVO_{self.servo} {seen}{throw} by {self.by} on {self.at[:10]}."


def observations(path: Path | None = None) -> list[Observation]:
    """Every recorded observation, oldest first. A missing file is an empty list.

    A malformed file is also an empty list rather than an exception: this is
    read to render a dashboard, and an unreadable record means the claim is
    unverified, which is the safe direction to fail in.
    """
    path = RECORD if path is None else path
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        try:
            out.append(Observation(**entry))
        except TypeError:
            continue
    return out


def record(
    servo: str,
    moved: bool,
    by: str,
    rest_deg: float | None = None,
    push_deg: float | None = None,
    hold_ms: float | None = None,
    command_id: str | None = None,
    path: Path | None = None,
) -> Observation:
    """Append one observation. Nothing is ever overwritten or removed.

    A servo that was seen moving in March and jammed in April must leave both
    facts on the record: the later one is what `state_for` reports, and the
    earlier one is how anybody works out when it changed.
    """
    path = RECORD if path is None else path
    entry = Observation(
        servo=servo,
        moved=moved,
        by=by,
        at=datetime.now(UTC).isoformat(timespec="seconds"),
        rest_deg=rest_deg,
        push_deg=push_deg,
        hold_ms=hold_ms,
        command_id=command_id,
    )
    existing = observations(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(o) for o in [*existing, entry]], indent=2) + "\n")
    return entry


def latest_for(servo: str, path: Path | None = None) -> Observation | None:
    """The most recent observation of this servo, or None."""
    for observation in reversed(observations(path)):
        if observation.servo == servo:
            return observation
    return None


def state_for(servo: str, path: Path | None = None) -> MovementVerification:
    """Whether this servo has been watched moving. Never inferred from an ACK."""
    latest = latest_for(servo, path)
    if latest is not None and latest.moved:
        return MovementVerification.PHYSICAL_MOVEMENT_VERIFIED
    return MovementVerification.VERIFICATION_UNAVAILABLE


def snapshot(servos: tuple[str, ...] = ("A", "B"), path: Path | None = None) -> dict:
    """The verification claim for each paddle, for the API and the dashboard."""
    states = {}
    for servo in servos:
        latest = latest_for(servo, path)
        states[servo] = {
            "state": str(state_for(servo, path)),
            "observed_at": latest.at if latest else None,
            "by": latest.by if latest else None,
            "detail": latest.summary
            if latest
            else f"No one has recorded watching SERVO_{servo} move.",
        }
    return {
        "servos": states,
        "verified": [s for s, v in states.items() if v["state"] == "PHYSICAL_MOVEMENT_VERIFIED"],
        "how": (
            "Run `python -m scripts.bench_check --port <port> --move A --move B` "
            "and answer it. There is no encoder and no camera on the paddle, so a "
            "human watching is the only thing that can produce this claim."
        ),
        "note": (
            "An ACK is not evidence of movement. A stalled servo, a cut signal "
            "wire and a dead supply rail all acknowledge identically."
        ),
    }
