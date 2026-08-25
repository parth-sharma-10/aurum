"""The developer workflow: export, evaluate, launch.

    python -m tools.fiftyone summary
    python -m tools.fiftyone export --dir data/vision_errors
    python -m tools.fiftyone evaluate
    python -m tools.fiftyone launch

`summary` needs nothing installed and is the right first call: it says what was
captured and whether any of it is labelled, which decides whether `evaluate`
has anything to do.
"""

from __future__ import annotations

import argparse
import json
import sys

from tools.fiftyone import dataset as dataset_module
from tools.fiftyone.failures import DEFAULT_DIR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.fiftyone", description=__doc__)
    parser.add_argument(
        "command", choices=("summary", "export", "evaluate", "launch"), help="what to do"
    )
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="capture directory")
    parser.add_argument("--name", default=dataset_module.DATASET_NAME, help="dataset name")
    parser.add_argument("--eval-key", default="eval", help="evaluation key to store under")
    args = parser.parse_args(argv)

    if args.command == "summary":
        print(json.dumps(dataset_module.summary(args.dir), indent=2))
        return 0

    if not dataset_module.available():
        print(
            "FiftyOne is not installed. It is a development tool and Aurum does "
            "not need it to run:\n    pip install fiftyone\n"
            "`summary` works without it.",
            file=sys.stderr,
        )
        return 2

    if args.command == "export":
        _, report = dataset_module.build_dataset(args.dir, args.name)
        print(json.dumps(report, indent=2))
        return 0
    if args.command == "evaluate":
        print(
            json.dumps(
                dataset_module.evaluate(name=args.name, eval_key=args.eval_key, directory=args.dir),
                indent=2,
                default=str,
            )
        )
        return 0
    dataset_module.launch(name=args.name, directory=args.dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
