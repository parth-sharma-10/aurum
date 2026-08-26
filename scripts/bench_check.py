"""Bench validation for the sorter board. Run it the moment the Arduino is plugged in.

Everything that can be checked without a human watching is checked automatically
and reported PASS/FAIL. The one thing that cannot be — whether a paddle actually
moved — is asked, because an ACK is not evidence of movement and this repository
does not pretend otherwise.

    python -m scripts.bench_check --port /dev/cu.usbmodem101
    AURUM_ARDUINO_ENABLED=true python -m scripts.bench_check --port ... --move A

Exits non-zero if any check failed, so it can gate a demonstration.
"""

from __future__ import annotations

import argparse
import sys
import time

from app import config as config_module
from app.hardware.arduino import ArduinoController
from app.hardware.link import BoardLink

#: Marks a step that reports what happened without deciding pass or fail.
UNVERIFIED = "UNVERIFIED"


def _report(results: list[tuple[str, str, str]]) -> int:
    width = max(len(name) for name, _, _ in results)
    print()
    for name, verdict, detail in results:
        print(f"  {verdict:<10} {name:<{width}}  {detail}")
    failed = [name for name, verdict, _ in results if verdict == "FAIL"]
    print()
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print("All checks passed.")
    return 0


def check_link(board: BoardLink) -> tuple[str, str]:
    """Open the port. The board resets on open, so this costs ~2 s by design."""
    state = board.connect()
    if not board.connected:
        return "FAIL", board.last_error or f"the port did not open ({state})"
    return "PASS", f"{board.port} at {board.baudrate} baud"


def check_ping(cfg: config_module.Config, board: BoardLink) -> tuple[str, str]:
    controller = ArduinoController(transport=board.transport, cfg=cfg)
    started = time.monotonic()
    if not controller.ping():
        return "FAIL", "no PONG; the sketch is not answering AURUM/1 on this port"
    return "PASS", f"PONG in {time.monotonic() - started:.3f} s"


def check_config(cfg: config_module.Config, board: BoardLink) -> tuple[str, str]:
    """Push the configured angles and require the board to acknowledge them.

    Unacknowledged, the paddles keep whatever the sketch booted with, and a
    throw measured on the bench is a measurement of the wrong geometry.
    """
    rest = cfg["conveyor.servo.rest_angle_deg"]
    push = cfg["conveyor.servo.push_angle_deg"]
    hold = cfg["conveyor.servo.actuation_ms"]
    budget_s = cfg["conveyor.arduino.ack_timeout_ms"] / 1000.0
    started = time.monotonic()
    if not board.configure_servos(rest, push, hold, budget_s=budget_s):
        return "FAIL", board.last_error or "the board did not acknowledge the CFG frame"
    applied = board.snapshot()["servo_config"]
    return "PASS", f"{applied} acknowledged in {time.monotonic() - started:.3f} s"


def check_weight(board: BoardLink, samples: int = 5) -> tuple[str, str]:
    """The HX711 half of the same link. Raw counts only: calibration is elsewhere."""
    counts = []
    for _ in range(samples):
        sample = board.next_weight()
        if sample is not None:
            counts.append(sample.raw_counts)
    if not counts:
        return "FAIL", "no W frames arrived; the cell is not being read"
    spread = max(counts) - min(counts)
    return (
        "PASS",
        f"{len(counts)}/{samples} frames, raw {min(counts):.0f}..{max(counts):.0f} (spread {spread:.0f})",
    )


def check_move(cfg: config_module.Config, board: BoardLink, target: str) -> tuple[str, str]:
    """Command one real stroke, then ask the operator what they saw.

    The question is the point. The board ACKs a stalled servo, a cut signal
    wire and a dead supply rail exactly as it ACKs a stroke, so the only thing
    that can turn this into a verification is a human looking at the paddle.
    """
    controller = ArduinoController(transport=board.transport, cfg=cfg)
    if not controller.enabled:
        return "FAIL", "actuation is disabled; re-run with AURUM_ARDUINO_ENABLED=true"
    print(f"\n  Commanding SERVO_{target}. Watch the paddle.")
    command = controller.move(target, item_id=f"BENCH-{target}")
    if not command.acknowledged:
        return "FAIL", f"{command.state}: {command.reason}"
    answer = input(f"  Did paddle {target} physically move? [y/N] ").strip().lower()
    if answer != "y":
        return "FAIL", "the board acknowledged but the paddle was not seen to move"
    return "PASS", f"acknowledged AND observed moving ({command.command_id})"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port; defaults to conveyor.arduino.port")
    parser.add_argument(
        "--move",
        choices=("A", "B"),
        action="append",
        default=[],
        help="also command this paddle and ask whether it moved. Repeatable.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config_module.load()
    port = args.port or cfg["conveyor.arduino.port"]
    if not port:
        print(
            "No port. Pass --port, or set conveyor.arduino.port / AURUM_ARDUINO_PORT.\n"
            "Nothing is invented in its place.",
            file=sys.stderr,
        )
        return 2

    board = BoardLink(
        port,
        baudrate=cfg["conveyor.arduino.baudrate"],
        timeout_s=cfg["conveyor.arduino.timeout_s"],
    )
    results: list[tuple[str, str, str]] = []
    print(f"Opening {port} (the board resets on open; this takes a moment)...")
    verdict, detail = check_link(board)
    results.append(("serial link", verdict, detail))
    if verdict == "FAIL":
        return _report(results)

    try:
        results.append(("board answers PING", *check_ping(cfg, board)))
        results.append(("servo angles applied (CFG)", *check_config(cfg, board)))
        results.append(("load cell streaming", *check_weight(board)))
        for target in args.move:
            results.append((f"paddle {target} observed moving", *check_move(cfg, board, target)))
        if not args.move:
            results.append(
                (
                    "physical movement",
                    UNVERIFIED,
                    "not commanded; re-run with --move A --move B and watch the paddles",
                )
            )
    finally:
        board.disconnect()
    return _report(results)


if __name__ == "__main__":
    raise SystemExit(main())
