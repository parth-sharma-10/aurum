# Vision QA: from a production miss back to the dataset

The model gets things wrong on the bench that it never got wrong on the test
split. This is the path from one of those frames to a labelled sample the next
training run can learn from.

**FiftyOne is a development tool and Aurum does not need it to run.** Nothing
in `app/` imports it. The live pipeline writes JPEGs and JSONL; everything
else happens afterwards, on a developer's machine.

```
live pipeline ──> data/vision_errors/*.jpg + failures.jsonl
                              │
                      (later, offline)
                              ▼
              python -m tools.fiftyone export
                              │
              python -m tools.fiftyone evaluate   ← needs labels
                              │
              python -m tools.fiftyone launch     ← the app
```

---

## Two kinds of failure category, and they are not interchangeable

This is the whole design.

**Decidable from one frame, with no label.** Captured at run time:

| Category | What justifies it |
|---|---|
| `NO_DETECTION` | the model returned nothing for this frame |
| `LOW_CONFIDENCE` | above the model's 0.35 operating floor, below the review threshold |
| `UNKNOWN_OBJECT` | a class the evidence database has no cited profile for |
| `INVALID_GEOMETRY` | a box with no positive area |
| `DUPLICATE_DETECTION` | two boxes of the *same* class overlapping at high IoU |
| `PARTIAL_VISIBILITY` | a box running into the frame edge |
| `MULTIPLE_OBJECTS` | more than one confirmed object where singulation assumes one |
| `TRACK_LOSS` | a confirmed item that vanished before it was processed |

**A comparison against a label.** Never claimed at run time:

`FALSE_POSITIVE` · `MISSED_DETECTION` · `CLASS_CONFUSION` · `TRACK_SWITCH` ·
`MOTION_BLUR` · `GLARE` · `OCCLUSION`

A running pipeline has no ground truth. A pipeline that asserted "this is a
false positive" would be inventing the very thing it is supposed to be checked
against. `FailureCapture.capture()` **raises** if one of these is claimed;
`evaluate_detections()` assigns them afterwards, or a person does in the app.

Two boxes of *different* classes overlapping is not a duplicate — a CPU on a
board overlaps the board, and that is the machine working.

---

## Capturing

Ships **off**. Turning it on costs a JPEG per event.

```bash
export AURUM_VISION_CAPTURE=true
export AURUM_VISION_CAPTURE_DIR=data/vision_errors   # default
export AURUM_VISION_LOW_CONFIDENCE=0.5               # review threshold
export AURUM_VISION_CAPTURE_LIMIT=50                 # per category
```

The per-category cap stops a demonstration filling a disk with a thousand
near-identical frames of the same problem. What was dropped is counted, not
hidden: `snapshot()["skipped_over_limit"]`.

Each sample carries the frame, the session and item ids, the timestamp, the
predictions, the decision, and the mass and price statuses — so a frame can be
read back alongside *why the machine did what it did with it*. Ground truth,
when it exists, is stored in a separate field and never merged into the
predictions.

---

## The workflow

```bash
# 1. What have we got? Needs nothing installed.
python -m tools.fiftyone summary

# 2. Build the dataset. Needs FiftyOne.
pip install fiftyone
python -m tools.fiftyone export

# 3. Label. In the app: draw ground_truth boxes on the frames worth labelling.
python -m tools.fiftyone launch

# 4. Score the labelled subset.
python -m tools.fiftyone evaluate
```

`summary` is the right first call. It says what was captured, by category, and
whether any of it is labelled — which decides whether `evaluate` has anything
to do.

### Evaluation

`evaluate()` runs FiftyOne's standard detection evaluation over the labelled
subset only:

```python
dataset.evaluate_detections(
    "predictions", gt_field="ground_truth", eval_key="eval", compute_mAP=True
)
```

**An unlabelled dataset is refused, not scored.** An evaluation against no
ground truth returns a precision of 0.0, which reads like a broken model
instead of an unlabelled dataset. `evaluate()` returns
`{"evaluated": false, "reason": ...}` and says what to do about it.

Afterwards, in the app or in Python:

```python
dataset.match(F("eval_fp") > 0)  # false positives
dataset.match(F("eval_fn") > 0)  # missed detections
dataset.sort_by("predictions.detections.confidence")  # weakest first
```

A dataset that silently dropped half its frames looks exactly like a model that
only failed on the other half, so `build_dataset()` returns a report naming
every sample it could not add and why.

---

## Coordinates

FiftyOne stores boxes as relative `[x, y, w, h]` in 0..1. Aurum carries
absolute `[x1, y1, x2, y2]` pixels. `to_relative()` is the one place that
converts, it is a pure function, and it is tested without FiftyOne installed.
A box running off the frame edge is clamped rather than rejected — a partially
visible object is a real detection.

---

## What this is not

It is **not** the runtime error-handling framework. `app/errors.py` keeps the
machine safe and running and records what failed with a code. This analyses
what the *vision model* got wrong, after the fact, so the next model gets it
right. Both exist; they answer different questions.
