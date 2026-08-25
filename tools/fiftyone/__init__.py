"""Vision QA: production failures to a FiftyOne dataset, and back to the model.

Nothing in `app/` imports this package. The live pipeline writes plain JPEGs
and JSONL; everything here runs afterwards, on a developer's machine.

    python -m tools.fiftyone summary     what was captured, no FiftyOne needed
    python -m tools.fiftyone export      build the dataset
    python -m tools.fiftyone evaluate    score predictions against labels
    python -m tools.fiftyone launch      open the app
"""

from tools.fiftyone.failures import (
    RUNTIME,
    FailureCapture,
    VisionFailure,
    VisionSample,
    classify_frame,
    read_manifest,
)

__all__ = [
    "RUNTIME",
    "FailureCapture",
    "VisionFailure",
    "VisionSample",
    "classify_frame",
    "read_manifest",
]
