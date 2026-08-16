#!/usr/bin/env python3
"""Aurum Vision — one command to start the demo.

    python run_demo.py                      # webcam
    python run_demo.py --mode images --path data/aurum/test/images
    python run_demo.py --mode video --path clip.mp4

If the webcam cannot be opened — on macOS the usual cause is that the terminal
has not been granted camera permission — this falls back to IMAGE DEMO MODE over
the held-out test images rather than exiting. A presentation should degrade, not
die.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.demo import build_parser, main  # noqa: E402

ROOT = Path(__file__).resolve().parent
FALLBACK_IMAGES = ROOT / "data" / "aurum" / "test" / "images"


def _fallback_argv(argv: list[str], path: Path) -> list[str]:
    """Rewrite the command line for image mode, keeping the user's other flags."""
    out: list[str] = []
    skip = False
    for i, tok in enumerate(argv):
        if skip:
            skip = False
            continue
        if tok in ("--mode", "--path", "--camera"):
            skip = True
            continue
        if tok.startswith(("--mode=", "--path=", "--camera=")):
            continue
        out.append(tok)
    return ["--mode", "images", "--path", str(path), *out]


def run() -> int:
    argv = sys.argv[1:]
    args = build_parser().parse_args(argv)
    try:
        return main(argv)
    except RuntimeError as exc:
        if args.mode != "webcam" or "camera" not in str(exc).lower():
            raise
        print(f"\n{exc}\n", file=sys.stderr)
        if not FALLBACK_IMAGES.is_dir():
            print("No fallback images available at "
                  f"{FALLBACK_IMAGES}. Run `python -m ml.prepare`, or pass "
                  "--mode images --path <folder>.", file=sys.stderr)
            return 1
        print(f"Falling back to IMAGE DEMO MODE using {FALLBACK_IMAGES}\n")
        return main(_fallback_argv(argv, FALLBACK_IMAGES))


if __name__ == "__main__":
    raise SystemExit(run())
