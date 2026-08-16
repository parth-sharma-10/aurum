#!/usr/bin/env python3
"""Aurum Vision — one command to start the demo.

    python run_demo.py                      # webcam
    python run_demo.py --mode images --path reports/test_predictions
    python run_demo.py --mode video --path clip.mp4

Falls back from webcam to the bundled sample images if no camera can be opened,
so the presentation still has something to show if the venue's laptop refuses
camera access.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.demo import build_parser, main  # noqa: E402

FALLBACK_IMAGES = Path(__file__).resolve().parent / "data" / "aurum" / "test" / "images"


def run() -> int:
    args = build_parser().parse_args()
    try:
        return main(sys.argv[1:])
    except RuntimeError as exc:
        if args.mode == "webcam" and "camera" in str(exc).lower():
            print(f"\n{exc}\n")
            if FALLBACK_IMAGES.is_dir():
                print(f"Falling back to IMAGE DEMO MODE using {FALLBACK_IMAGES}\n")
                return main(["--mode", "images", "--path", str(FALLBACK_IMAGES),
                             "--weights", args.weights, "--conf", str(args.conf)])
        raise


if __name__ == "__main__":
    raise SystemExit(run())
