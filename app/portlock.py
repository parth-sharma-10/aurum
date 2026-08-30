"""One advisory lock per serial device node, so two readers cannot split a board.

macOS `cu.*` device nodes do not lock. A second process opens the same port
successfully and the two then SPLIT the board's replies between them - each
`readline()` takes whatever the other has not taken yet. The symptom is not a
refused open: it is a port that looks perfectly healthy while every command
times out and every stream goes quiet.

That has cost two bench sessions. On 2026-08-26 it was an hour spent suspecting
the firmware, and `BoardLink` grew this lock in response. On 2026-08-27 it was
`python -m app.calibrate` blocking in `readline()` with the pan empty, because
the calibration reader was the one serial consumer that never took the lock -
the backend held the port and ate every weight frame.

So it lives here rather than on `BoardLink`: at the top level, importable by
both `app.weight` and `app.hardware.link` without a cycle, since
`app/hardware/__init__.py` imports `link`, which imports `app.weight`.

`fcntl` is POSIX-only and is confined to this module, which is where a Windows
port would put its own implementation.
"""

from __future__ import annotations

import contextlib
import fcntl
import os

#: Where the per-port lock files live. One file per device node, so two boards
#: on two ports do not exclude each other.
LOCK_DIR = "/tmp"


def lock_path(port: str) -> str:
    return os.path.join(LOCK_DIR, f"aurum-{os.path.basename(port)}.lock")


class PortLock:
    """The right to be the only reader of one serial port, in this process tree.

    `flock` is per open-file-description, so this also catches two holders
    inside one process, which is the same bug with a shorter cable.
    """

    def __init__(self, port: str) -> None:
        self.port = port
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> str | None:
        """Take the lock. None on success, else a description of who holds it.

        A lock file that cannot be OPENED is not treated as contention: that
        says nothing about who holds the port, and refusing on it would brick a
        working machine over a /tmp permission. Only a lock somebody else holds
        refuses.
        """
        # Already ours. Re-acquiring through the same object - which is what
        # reconnecting does every time the dashboard is opened - must not be
        # refused by its own lock, because `flock` is per open-file-description
        # and a second `open()` here would fail against the descriptor we still
        # hold.
        if self._handle is not None:
            return None

        try:
            handle = open(lock_path(self.port), "a+")  # noqa: SIM115 - held for the lock's life
        except OSError:
            return None
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            owner = ""
            with contextlib.suppress(Exception):
                handle.seek(0)
                owner = handle.read().strip()
            handle.close()
            return owner or "another process"
        handle.seek(0)
        handle.truncate()
        handle.write(f"PID {os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return None

    def release(self) -> None:
        """Give the port back. Safe on a lock that was never held."""
        handle, self._handle = self._handle, None
        if handle is None:
            return
        with contextlib.suppress(Exception):
            fcntl.flock(handle, fcntl.LOCK_UN)
        with contextlib.suppress(Exception):
            handle.close()


def contention_message(port: str, owner: str) -> str:
    """Why a busy port is not a broken one, in the operator's words."""
    return (
        f"{port} is already owned by {owner}. Two readers on one macOS cu.* port "
        "do not fail to open - they split the board's replies, so the port looks "
        "healthy while every command times out and the stream goes quiet. Stop "
        "the other process and try again."
    )
