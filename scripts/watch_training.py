"""Live view of a training run.

Ultralytics prints a progress bar to stdout and appends one row per epoch to
results.csv. Neither is much use on its own while a three-hour run is going:
the bar is buried in a log full of carriage returns, and the CSV has no sense
of whether the process is still alive.

This reads both, plus the pidfile, and refreshes a single screen showing
whether the run is actually running, where it is, and whether the metric is
still improving. Standard library only — it must work while the environment is
busy training.

Usage:
    python scripts/watch_training.py
    python scripts/watch_training.py --interval 10
    python scripts/watch_training.py --once          # print once and exit
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUN = ROOT / "runs" / "aurum_vision_v0_1"
DEFAULT_PID = Path("/tmp/aurum_train.pid")
DEFAULT_LOG = Path("/tmp/aurum_train2.log")

# "  17/50   5.28G  1.098  0.958  1.384   92  512: 13% ... 20/153 1.1s/it 25.6s<2:25"
PROGRESS = re.compile(
    r"(\d+)/(\d+)\s+\S+G?\s+.*?(\d+)%\s+\S*\s*(\d+)/(\d+)\s+([\d.]+)s/it\s+(\S+)<(\S+)"
)

BOLD, DIM, GREEN, GOLD, RED, RESET = (
    "\033[1m",
    "\033[2m",
    "\033[32m",
    "\033[33m",
    "\033[31m",
    "\033[0m",
)


def alive(pid_file: Path) -> tuple[bool, int | None]:
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return False, None
    try:
        os.kill(pid, 0)
        return True, pid
    except (OSError, ProcessLookupError):
        return False, pid


def read_epochs(csv_path: Path) -> list[dict]:
    if not csv_path.exists():
        return []
    with csv_path.open() as fh:
        rows = [{k.strip(): v for k, v in r.items()} for r in csv.DictReader(fh)]
    return [r for r in rows if r.get("epoch", "").strip()]


def last_progress(log: Path) -> dict | None:
    """Pull the most recent progress-bar frame out of the log's tail."""
    if not log.exists():
        return None
    try:
        with log.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 8192))
            text = fh.read().decode("utf-8", "ignore")
    except OSError:
        return None
    matches = PROGRESS.findall(text.replace("\r", "\n"))
    if not matches:
        return None
    ep, total, pct, it, its, sit, elapsed, remaining = matches[-1]
    return {
        "epoch": int(ep),
        "epochs": int(total),
        "pct": int(pct),
        "it": int(it),
        "its": int(its),
        "s_per_it": float(sit),
        "elapsed": elapsed,
        "remaining": remaining,
    }


def bar(pct: int, width: int = 28) -> str:
    filled = round(width * pct / 100)
    return "━" * filled + "─" * (width - filled)


def render(run: Path, pid_file: Path, log: Path) -> str:
    rows = read_epochs(run / "results.csv")
    running, pid = alive(pid_file)
    prog = last_progress(log)
    out: list[str] = []

    state = f"{GREEN}RUNNING{RESET}" if running else f"{RED}NOT RUNNING{RESET}"
    pid_txt = f"pid {pid}" if pid else "no pidfile"
    out.append(f"{BOLD}AURUM VISION — training{RESET}   {state}  {DIM}({pid_txt}){RESET}")
    out.append("")

    if not rows:
        out.append(f"{DIM}No epochs recorded yet — the first one takes a few minutes.{RESET}")
        return "\n".join(out)

    done = len(rows)
    total = prog["epochs"] if prog else int(rows[-1].get("epoch", done))
    best = max(rows, key=lambda r: float(r.get("metrics/mAP50(B)", 0) or 0))
    best_map = float(best["metrics/mAP50(B)"])
    best_ep = int(float(best["epoch"]))

    if prog and running:
        out.append(
            f"  epoch {BOLD}{prog['epoch']}/{prog['epochs']}{RESET}  "
            f"{bar(prog['pct'])} {prog['pct']:3d}%   "
            f"{prog['it']}/{prog['its']} batches   "
            f"{DIM}{prog['elapsed']} elapsed, {prog['remaining']} left this epoch{RESET}"
        )
        left = (total - done) * (prog["s_per_it"] * prog["its"] + 12)
        out.append(f"  {DIM}rough ETA for the whole run: {timedelta(seconds=int(left))}{RESET}")
    else:
        out.append(f"  {done} epoch(s) recorded")
    out.append("")

    out.append(
        f"  {DIM}{'epoch':>6} {'mAP50':>8} {'mAP50-95':>9} "
        f"{'precision':>10} {'recall':>8} {'box_loss':>9}{RESET}"
    )
    for r in rows[-10:]:
        m50 = float(r.get("metrics/mAP50(B)", 0) or 0)
        mark = f"{GOLD}★{RESET}" if int(float(r["epoch"])) == best_ep else " "
        out.append(
            f"  {int(float(r['epoch'])):>6} {m50:>8.4f} "
            f"{float(r.get('metrics/mAP50-95(B)', 0) or 0):>9.4f} "
            f"{float(r.get('metrics/precision(B)', 0) or 0):>10.3f} "
            f"{float(r.get('metrics/recall(B)', 0) or 0):>8.3f} "
            f"{float(r.get('val/box_loss', 0) or 0):>9.4f} {mark}"
        )
    out.append("")
    out.append(f"  best so far: {BOLD}mAP50 {best_map:.4f}{RESET} at epoch {best_ep}")

    stale = done - best_ep
    if stale >= 10:
        out.append(
            f"  {DIM}{stale} epochs since the last improvement "
            f"(early stopping triggers at 15){RESET}"
        )

    out.append("")
    out.append(f"  {DIM}These are VALIDATION figures. They are not the held-out")
    out.append(f"  test result and must not be quoted as accuracy.{RESET}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--run", default=str(DEFAULT_RUN), help="training run directory")
    ap.add_argument("--pidfile", default=str(DEFAULT_PID))
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--once", action="store_true", help="print once and exit")
    args = ap.parse_args()

    run, pid_file, log = Path(args.run), Path(args.pidfile), Path(args.log)
    if args.once:
        print(render(run, pid_file, log))
        return 0

    try:
        while True:
            print("\033[2J\033[H", end="")  # clear, home
            print(render(run, pid_file, log))
            print(f"\n  {DIM}refreshing every {args.interval:g}s — Ctrl-C to stop{RESET}")
            if not alive(pid_file)[0] and read_epochs(run / "results.csv"):
                print(f"\n  {GOLD}Process is not running. Final state shown above.{RESET}")
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
