# Aurum Vision — presentation runbook

A 2–3 minute live demonstration, plus what to do when the venue's laptop
misbehaves.

## Before you go on

```bash
cd aurum
source .venv/bin/activate
python run_demo.py --mode images --path data/aurum/test/images   # 10s smoke test
```

Checklist:

- [ ] `models/aurum_vision_v0_1_best.pt` exists
- [ ] Camera permission granted (macOS: System Settings → Privacy & Security →
      Camera → your terminal app). **Do this before the venue**; the prompt is
      modal and will eat your first 30 seconds on stage.
- [ ] Components on the table: 2 boards, 1–2 RAM modules, 1 CPU. Matte surface,
      not glass. Avoid a cluttered background — the model was trained largely on
      product photography and gets noisier against clutter.
- [ ] Lighting on the components, not behind them.
- [ ] Terminal font large; window maximised.

## Physical setup

Camera roughly 40–60 cm above the bench, looking down at a slight angle. Hold
components flat-on to the camera rather than edge-on: a RAM module seen edge-on
is a thin line and the model will miss it. That is a real limitation, so stage
around it rather than fighting it live.

## The sequence (2–3 minutes)

**0:00 — Frame the problem.** *"E-waste changes hands by weight. What's inside
the board is invisible at the point of collection. Aurum Vision's job is to make
what entered the workflow machine-readable."*

**0:15 — Start it.**

```bash
python run_demo.py
```

Dashboard comes up: live feed left, detection summary right, status strip
bottom showing model version, FPS and LIVE.

**0:30 — One component.** Hold up a single PCB. Point at the box, the class name
and the confidence. Let the count settle to `PCB 1`.

**0:50 — Add components.** Add a RAM module, then a CPU. Counts update:
`PCB 1 · RAM 1 · CPU 1`. Note Objects and Avg Conf. changing in the panel.

**1:15 — Move things around.** Rotate the components, slide them, briefly occlude
one with your hand. Two points to make out loud:

- detection continues across orientation changes
- the count does **not** flicker when your hand crosses the frame, because the
  batch count is a median over a trailing window rather than the last frame

**1:40 — Start a batch.** Press `B`. Batch ID appears. Arrange the final
composition — say 2 PCB, 1 RAM, 1 CPU — and let it settle.

**2:00 — Weight.** Point at the panel. If no load cell is attached it reads
**SIMULATED SENSOR** above the value. *Say that out loud* — "this is a simulated
reading; the HX711 integration is built but no cell is attached today." Do not
let anyone think it is a measurement.

**2:10 — Save the batch.** Press `S`. The batch record prints as JSON:

```json
{
  "batch_id": "AUR-…",
  "detections": { "PCB": 2, "RAM": 1, "CPU": 1, "Connector": 0 },
  "total_objects": 4,
  "average_confidence": 0.…,
  "model_version": "Aurum Vision v0.1"
}
```

*"That record is what feeds the valuation and EPR ledger steps."*

**2:30 — Close on the limitation, deliberately.** *"Note what this record does
not contain: a gold figure. The camera identifies components. Recovery value
comes from counts multiplied by published reference yields, and it is labelled
an estimate. We're not claiming an RGB camera can assay a board."*

Ending on the limitation reads as rigour, not weakness — and it pre-empts the
first question a judge who knows the field will ask.

## If the camera fails

`run_demo.py` falls back to image mode automatically. To force it:

```bash
python run_demo.py --mode images --path data/aurum/test/images
```

Arrow keys step through images; `SPACE` pauses. Everything else — counts, batch
record, save — behaves identically, and the mode is labelled in the status bar.

You can also pre-record a clip and run `--mode video --path clip.mp4`. **Record
this backup clip the night before.** It is the difference between a hiccup and a
dead demo.

## If detections are jumpy on stage

```bash
python run_demo.py --conf 0.45     # fewer, more confident boxes
python run_demo.py --window 60     # steadier counts, slower to respond
```

Raising `--conf` is the safer live adjustment: a missed detection is a smaller
problem in front of judges than a confident wrong one.

## Questions you should expect

**"Can it tell how much gold is in that board?"**
No, and we don't claim it. RGB imaging identifies components; it cannot measure
composition. Recovery figures are estimates from counts × published reference
yields, labelled as estimates in the JSON itself.

**"What's your accuracy?"**
Quote only the figures in `model-card.md`, measured on a held-out test split
whose images share no duplicate cluster with training. Do not round up, and do
not quote the validation number as if it were the test number.

**"Is this production ready?"**
No. It's a prototype trained on public data to show the concept works end to
end. Field deployment needs data collected on real benches.

**"How do we know the test set is really held out?"**
`python -m ml.validate` — it checks cluster containment, exact duplicates and
perceptual near-duplicates across splits, and it exits non-zero on failure. It
caught 18 leaks in an earlier version of the pipeline.

## What not to say

- ❌ "detects gold/precious-metal content"
- ❌ "95% accurate" (or any number not in `model-card.md`)
- ❌ "production-ready" / "industrial-grade sorting"
- ❌ presenting the simulated weight as a measurement
