"""One shape for every failure Aurum records, and one place that names them.

Before this module a failure was a sentence attached to whatever object had
failed: `camera_error` on the session, `last_error` on the link, `reason` on a
weight reading. Each is readable and none is queryable — nothing could ask
"how many hardware faults this run", and nothing downstream could branch on a
category without matching on English.

    AurumError(code=PRICE_ERROR, stage="pricing", item_id=..., message=...)

**This is a record, not an exception.** Aurum's failure discipline is to keep
running and route the item to a bin it can justify, which for an unreadable
item is C. Raising here would put a traceback on a projector. `AurumError`
subclasses nothing and is never raised; `ErrorLog.record()` returns the entry
so a caller can attach it to the item it belongs to.

**A code is a promise about what failed, not about what to do.** Nothing in
this module decides a bin, latches a fault or suppresses a command. Those
belong to `app.decision`, `app.hardware.fault` and `app.hardware.arduino`
respectively, and a code that quietly did any of them would be a policy hidden
in a logging module.

Secrets never reach here. `redact()` is applied to every message, because the
one string most likely to be interpolated into an error is the URL that failed
and the one thing most likely to be in that URL is an API key.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

#: How many entries to keep. A demonstration run produces a handful; a stuck
#: poll loop could produce thousands, and an unbounded list in a long-running
#: process is a memory leak with a friendly name.
LOG_LIMIT = 200

#: Query parameters and headers whose values must never be logged. Matched
#: case-insensitively against `key=value` and `"key": "value"` shapes.
_SECRET_KEYS = ("api_key", "apikey", "access_key", "token", "password", "secret")

_SECRET_PATTERN = re.compile(
    r"(?i)\b(" + "|".join(_SECRET_KEYS) + r")(\"?\s*[:=]\s*\"?)([^\s&\"',}]+)"
)


def redact(text: str | None) -> str | None:
    """Replace the value of anything that looks like a credential.

    Deliberately crude and deliberately applied to every message rather than
    at the one call site that seemed risky. A key leaks through the error path
    nobody thought about.
    """
    if not text:
        return text
    return _SECRET_PATTERN.sub(r"\1\2***REDACTED***", text)


class ErrorCode(StrEnum):
    """What kind of thing failed. One per stage of the machine, plus two
    cross-cutting ones that can happen at any stage."""

    VISION_ERROR = "VISION_ERROR"
    TRACKING_ERROR = "TRACKING_ERROR"
    WEIGHT_ERROR = "WEIGHT_ERROR"
    MATERIAL_ERROR = "MATERIAL_ERROR"
    PRICE_ERROR = "PRICE_ERROR"
    DECISION_ERROR = "DECISION_ERROR"
    ROUTING_ERROR = "ROUTING_ERROR"
    ARDUINO_ERROR = "ARDUINO_ERROR"
    SERVO_ERROR = "SERVO_ERROR"
    CONVEYOR_ERROR = "CONVEYOR_ERROR"
    #: An operation that did not finish inside its configured limit.
    TIMEOUT = "TIMEOUT"
    #: A setting that cannot be used. Usually fatal to one stage, not the run.
    CONFIG_ERROR = "CONFIG_ERROR"
    #: The latched state in app.hardware.fault. Recorded here as well so the
    #: error trail explains why nothing moved afterwards.
    HARDWARE_FAULT = "HARDWARE_FAULT"


@dataclass(frozen=True)
class AurumError:
    """One recorded failure, with enough context to find it again."""

    code: ErrorCode
    stage: str
    message: str
    session_id: str | None = None
    item_id: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "error_code": str(self.code),
            "stage": self.stage,
            "message": self.message,
            "session_id": self.session_id,
            "item_id": self.item_id,
            "timestamp": self.timestamp,
            "detail": dict(self.detail),
        }


class ErrorLog:
    """A bounded, ordered record of what went wrong, newest last."""

    def __init__(self, session_id: str | None = None, limit: int = LOG_LIMIT) -> None:
        self.session_id = session_id
        self._entries: deque[AurumError] = deque(maxlen=limit)

    def record(
        self,
        code: ErrorCode,
        stage: str,
        message: str,
        item_id: str | None = None,
        **detail,
    ) -> AurumError:
        """File one failure and hand it back, so a caller can also attach it."""
        entry = AurumError(
            code=code,
            stage=stage,
            message=redact(str(message)) or "",
            session_id=self.session_id,
            item_id=item_id,
            detail={k: redact(v) if isinstance(v, str) else v for k, v in detail.items()},
        )
        self._entries.append(entry)
        return entry

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> list[AurumError]:
        return list(self._entries)

    def for_item(self, item_id: str) -> list[AurumError]:
        return [e for e in self._entries if e.item_id == item_id]

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for entry in self._entries:
            tally[str(entry.code)] = tally.get(str(entry.code), 0) + 1
        return tally

    def snapshot(self, limit: int = 20) -> dict:
        """Recent failures, newest first, for the API and the dashboard."""
        recent = list(self._entries)[-limit:][::-1]
        return {
            "count": len(self._entries),
            "by_code": self.counts(),
            "recent": [e.as_dict() for e in recent],
            "note": (
                "A recorded failure is not a crash. Aurum keeps running and "
                "routes what it cannot read to Bin C."
            ),
        }
