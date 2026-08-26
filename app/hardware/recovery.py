"""What was in flight when the process died.

`ACK_TIMEOUT` latches the machine because a command that went unacknowledged
may have left a paddle half out, and nothing may be commanded into a machine
whose physical state nobody knows. A process that is killed between writing the
frame and reading the reply is the same situation and a worse one: the timeout
at least leaves a record, and a `kill -9` leaves nothing at all. On the next
start the fault latch is clear, the command history is empty, and the machine
will happily actuate into whatever the last one left behind.

So the in-flight command is written to disk before the frame goes out and
removed once it settles, whatever it settles as. A marker still present at
startup means exactly one thing: a command was written and its outcome is
unknown.

    marker present at startup  ->  RECOVERY_REQUIRED  ->  a human looks at the rig

This is transient state, not evidence, so it lives in `runs/` and is not
committed — unlike `reports/movement_verification.json`, which is.

The window is about a second per routed item and this costs two small writes to
cover it. That trade is the same one the ACK timeout makes.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
MARKER = ROOT / "runs" / "in_flight.json"


def mark(command_id: str, target: str, item_id: str, path: Path | None = None) -> None:
    """Record that a frame is about to be written. Never raises.

    A machine that cannot write this marker must still be able to sort: losing
    the crash protection is bad, refusing to actuate because a disk is full is
    worse, and the operator is standing in front of the rig either way.
    """
    path = MARKER if path is None else path
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "command_id": command_id,
                    "target": target,
                    "item_id": item_id,
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
            )
            + "\n"
        )


def clear(path: Path | None = None) -> None:
    """The command settled. Its outcome is recorded elsewhere; this is done."""
    path = MARKER if path is None else path
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def pending(path: Path | None = None) -> dict | None:
    """The command that was in flight when the last process ended, or None.

    An unreadable marker still counts as a pending command. The file's presence
    is the signal; its contents are only there to say which command it was, and
    "something was in flight but I cannot tell you what" is not a reason to
    report that the machine is safe.
    """
    path = MARKER if path is None else path
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"command_id": None, "target": None, "item_id": None, "at": None}
    return loaded if isinstance(loaded, dict) else {"command_id": None}


def reason(marker: dict) -> str:
    """Why the machine will not move, in terms of what was actually interrupted."""
    command_id = marker.get("command_id") or "an unidentified command"
    target = marker.get("target")
    paddle = f"SERVO_{target}" if target in ("A", "B") else "a paddle"
    return (
        f"The previous run ended while {command_id} was in flight to {paddle}. "
        "Whether it moved, half moved or jammed is unknown, so nothing will be "
        "commanded until somebody looks at the rig and resets this."
    )
