# Phase 11 — closing every software loose end

> **Historical record. Phase 11 completed on 2026-08-26 and this plan is kept as
> written, not updated.** The figures below are the ones it was planned
> against; for the current state see
> [COMPLETION_PLAN.md](COMPLETION_PLAN.md#next-session-checkpoint), and for what
> the phase actually delivered see *Phase 11 — what closed* in the same file.
>
> One line here is worth flagging as overtaken: the contract says the only
> outstanding item is mounting and calibrating the load cell. That was true when
> written. The cell was calibrated on 2026-08-26 and its input went **open** on
> 2026-08-27 — see
> [docs/hardware.md](hardware.md#the-cell-itself-open-and-not-reading--2026-08-27).

The contract: after this phase the only outstanding item is **mounting and
calibrating the load cell**. Everything solvable in software is solved.

Baseline before any change: **969 tests pass**, ruff clean, format clean,
frontend builds.

| # | Work | Why it was outstanding |
|---|---|---|
| 1 | `app/errors.py` — one structured error model | Failures were recorded as free text on the item, with no code a dashboard or a log aggregator could branch on |
| 2 | `app/valuation/metalprice.py` — a LIVE provider | `PROVIDERS` had no live entry, so `pmdi_value` was permanently REFERENCE at best |
| 3 | `app/routing/conveyor.py` — speed sources | Belt speed was a config constant; nothing could measure or mock it, so `UNMEASURED` blocked the whole routing layer |
| 4 | `app/hardware/fault.py` — latched fault | A failed ACK left the machine willing to try the next item |
| 5 | `Bin.UNKNOWN` + physical fallback | "Cannot judge" and "judged, does not qualify" both rendered as C |
| 6 | Mass plausibility | Nothing checked a mass against the class it was attached to |
| 7 | `app/epr.py` — per-item event ledger | The SQLite ledger stored batches; per-item history lived in memory and died with the run |
| 8 | `tools/fiftyone/` — vision QA | No path from a production miss to a dataset |
| 9 | Session integration | The scheduler and `ServoActuator` existed and nothing called them |
| 10 | Dashboard, docs, tests | Follows the above |

## Decisions taken here, and why

**The mock conveyor ships OFF (`conveyor.mode: NONE`).** The rig has no belt.
A belt turned on by default would make the machine wait eight seconds and fire
a paddle at nothing. It is one environment variable away and fully wired —
same posture as `arduino.enabled` and `demo.mock_mass.enabled`.

**No new runtime dependency for MetalpriceAPI.** One authenticated GET is
`urllib.request`. The official SDK is a package for the same call, and this
repository already declined `python-dotenv` on the same reasoning.

**FiftyOne is a dev-time tool, never imported by the running pipeline.**
Production writes plain JSONL and JPEGs; `tools/fiftyone/` converts them later.
Tests skip when FiftyOne is absent rather than requiring a 1 GB install.

**UNKNOWN is a decision state; C is a place.** An item Aurum cannot judge is
`decision: UNKNOWN, physical_bin: C`. The physical outcome is unchanged — it
still reaches C by nobody doing anything — but the dashboard no longer says
Aurum graded something it could not read.
