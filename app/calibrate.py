"""The HX711 calibration workflow.

    empty pan -> tare -> known mass -> factor -> SECOND known mass -> verified

Two masses, not one. A single reference mass proves the cell responds and lets
you compute a factor; it cannot tell you whether the factor is right, because
the mass you derived it from will always read back correctly. A second,
different mass is the only thing that catches a wrong factor, a non-linear
cell, or a tare taken while something was still on the pan.

Until that second check passes, `verified` stays false and the sensor reports
`STABLE` rather than `MEASURED` — a number you may display, never one a
concentration calculation may consume.

    python -m app.calibrate --port COM3 --reference-mass 180 --verify-mass 100

The arithmetic below is pure and separately tested; the CLI only handles the
prompting and the serial port.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from datetime import UTC, datetime

from app import config as config_module
from app.weight import (
    LIVE_FRAME_S,
    Calibration,
    HX711SerialReader,
    WeightSensor,
)

#: How far a verification reading may sit from the known mass and still pass.
#: MEASURED ON THIS RIG, 2026-08-26, not assumed. The previous 0.1 g was an
#: assumption that a bench cell resolves a tenth of a gram; a repeatability
#: experiment falsified it. Placing and re-placing the same mass moves the
#: reading far more than the electronics do:
#:
#:     204 g  x2  sample sd 0.431 g   (max-min 0.610)
#:     170 g  x3  sample sd 0.245 g   (max-min 0.488)
#:     374 g      max-min 0.119 g     (a heavier mass seats more repeatably)
#:     zero, warmed: 3.3 counts = 0.008 g
#:
#: The check derives a factor from ONE reference burst and tests it against ONE
#: verification burst, so both placements contribute:
#:
#:     factor uncertainty 0.431 g at 204 g = 0.211%, at 170 g -> 0.359 g
#:     verification placement scatter                        -> 0.245 g
#:     combined in quadrature                                -> 0.435 g  (1 sigma)
#:
#: 3 sigma is 1.30 g, rounded up to 1.5 g so a sound calibration does not fail
#: on placement luck. It stays far tighter than anything it exists to catch: a
#: tare taken under a 5 g object, or a factor wrong by more than 0.9%.
#:
#: Nothing downstream needs better. At 170 g this is 0.88% relative; a CPU would
#: need an 88% mass error to cross the 100 ppm Bin B threshold, and the tightest
#: plausibility window (Connector, 0.5 g minimum) is governed by zero stability
#: at 0.008 g, not by the slope. Override with --tolerance.
DEFAULT_VERIFY_TOLERANCE_G = 1.5

#: How long one burst of samples may take before the workflow gives up, in
#: seconds.
#:
#: A board streaming at 10 Hz delivers the default 20 samples in about 2 s, so
#: this is fifteen times the expected cost and will not fire on a healthy rig.
#: It exists because every exit from the collection loop below used to depend on
#: frames actually arriving: a reader that returns nothing while staying
#: connected - which is exactly what a frozen or open converter does - left
#: `python -m app.calibrate` running for ever with no output.
READ_BUDGET_S = 30.0


def derive_counts_per_gram(
    tare_counts: float, loaded_counts: float, reference_mass_g: float
) -> float:
    """Counts per gram from an empty reading and a known mass.

    Raises rather than returning a sentinel: a zero or negative reference mass,
    or a load that moved the cell not at all, means the experiment did not
    happen and there is nothing to record.
    """
    if reference_mass_g <= 0:
        raise ValueError(f"reference mass must be positive, got {reference_mass_g}")
    delta = loaded_counts - tare_counts
    if delta == 0:
        raise ValueError(
            "the loaded reading equals the tare reading; the cell did not respond "
            "to the reference mass"
        )
    return delta / reference_mass_g


def verify(
    calibration: Calibration,
    verification_counts: float,
    verification_mass_g: float,
    tolerance_g: float = DEFAULT_VERIFY_TOLERANCE_G,
) -> Calibration:
    """Check a factor against a second known mass and record the outcome.

    Returns a new Calibration carrying the error and whether it passed. A
    failed check is recorded, not discarded: knowing the factor was tried and
    missed by 4 g is worth more than an empty file.
    """
    predicted = calibration.grams(verification_counts)
    if predicted is None:
        raise ValueError("cannot verify an uncalibrated factor")
    error = predicted - verification_mass_g
    return Calibration(
        counts_per_gram=calibration.counts_per_gram,
        tare_counts=calibration.tare_counts,
        reference_mass_g=calibration.reference_mass_g,
        verified=abs(error) <= tolerance_g,
        verification_mass_g=verification_mass_g,
        verification_error_g=error,
        recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
        notes=(
            f"Verified against a second known mass: predicted {predicted:.3f} g "
            f"for a {verification_mass_g:g} g reference, error {error:+.3f} g, "
            f"tolerance {tolerance_g:g} g."
        ),
    )


def _average(
    sensor: WeightSensor, samples: int, label: str, budget_s: float = READ_BUDGET_S
) -> float:
    """Average a burst of readings taken NOW, not whatever was already queued.

    The drain is the whole point. Each step of this workflow waits on a human
    placing a mass, and the board streams at 10 Hz throughout; without
    discarding that backlog every step reads the pan as it was before the
    operator touched it, and all three averages come back nearly equal.
    """
    drain = getattr(sensor.reader, "drain", None)
    if callable(drain):
        discarded = drain()
        if discarded:
            print(f"  {label}: discarded {discarded} buffered frames from before this step")

    started = time.monotonic()
    collected: list[float] = []
    deadline = started + budget_s
    while len(collected) < samples:
        sample = sensor.reader.read()
        if sample is not None:
            collected.append(sample.raw_counts)
            continue
        if not getattr(sensor.reader, "connected", True):
            raise RuntimeError(f"the load cell disconnected while reading {label}")
        # A converter that has stopped converting stays CONNECTED and returns
        # nothing, so neither the check above nor `len(collected) < samples`
        # can ever end this loop. Refuse with the reader's own diagnosis.
        #
        # This is the one workflow that MUST refuse rather than cope: a tare
        # averaged off a dead cell writes a fabricated zero over a verified
        # factor, and the result looks entirely healthy afterwards.
        if getattr(sensor.reader, "stuck", False):
            raise RuntimeError(
                f"{label}: {getattr(sensor.reader, 'last_error', None) or 'the load cell is not converting.'}"
            )
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"{label}: only {len(collected)} of {samples} frames arrived in "
                f"{budget_s:.0f} s. The board should send ten a second - check it is "
                "running a sketch that streams weight frames, and that nothing else owns "
                "the port."
            )
    elapsed = time.monotonic() - started
    spread = max(collected) - min(collected)
    print(
        f"  {label}: {statistics.fmean(collected):.1f} counts (spread {spread:.1f}) "
        f"in {elapsed:.1f} s"
    )

    # A board emitting at 10 Hz cannot deliver this burst appreciably faster
    # than real time. If it did, the drain failed and these counts describe an
    # earlier state of the pan - the exact fault that produced a 0.08 counts/g
    # factor from a cell that responds at ~394. Refuse rather than average it:
    # a calibration derived from the wrong moment is worse than none.
    expected_s = samples * LIVE_FRAME_S
    if elapsed < expected_s:
        raise RuntimeError(
            f"{label}: {samples} frames arrived in {elapsed:.2f} s, faster than the "
            f"board can send them ({expected_s:.1f} s minimum). These are buffered "
            "readings of an earlier pan, not a measurement of what is on it now."
        )
    return statistics.fmean(collected)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="serial port, e.g. COM3 or /dev/ttyACM0")
    parser.add_argument("--baudrate", type=int, default=None)
    parser.add_argument(
        "--reference-mass", type=float, required=True, help="first known mass, in grams"
    )
    parser.add_argument(
        "--verify-mass",
        type=float,
        required=True,
        help="a SECOND, DIFFERENT known mass used to verify the factor",
    )
    parser.add_argument("--tolerance", type=float, default=DEFAULT_VERIFY_TOLERANCE_G)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true", help="show the plan and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config_module.load()

    if args.verify_mass == args.reference_mass:
        print(
            "The verification mass must be DIFFERENT from the reference mass. "
            "The mass a factor was derived from always reads back correctly, so "
            "using it again verifies nothing."
        )
        return 2

    if args.dry_run:
        print(__doc__)
        return 0

    baudrate = args.baudrate or cfg["conveyor.arduino.baudrate"]
    reader = HX711SerialReader(args.port, baudrate=baudrate, timeout_s=2.0)
    sensor = WeightSensor(reader, calibration=Calibration(), cfg=cfg, simulated=False)

    try:
        input("1/3  Empty the pan completely, then press Enter... ")
        tare_counts = _average(sensor, args.samples, "tare")

        input(f"2/3  Place the {args.reference_mass:g} g reference mass, then press Enter... ")
        loaded_counts = _average(sensor, args.samples, "loaded")

        counts_per_gram = derive_counts_per_gram(tare_counts, loaded_counts, args.reference_mass)
        candidate = Calibration(
            counts_per_gram=counts_per_gram,
            tare_counts=tare_counts,
            reference_mass_g=args.reference_mass,
        )
        print(f"  derived: {counts_per_gram:.4f} counts/g")

        input(
            f"3/3  Remove it and place the {args.verify_mass:g} g verification mass, "
            "then press Enter... "
        )
        verify_counts = _average(sensor, args.samples, "verification")
        result = verify(candidate, verify_counts, args.verify_mass, args.tolerance)
    finally:
        reader.close()

    path = result.save()
    print(f"\nWritten to {path}")
    if result.verified:
        print(
            f"CALIBRATED and VERIFIED. Error {result.verification_error_g:+.3f} g "
            f"within {args.tolerance:g} g. Readings can now reach MEASURED."
        )
        return 0

    print(
        f"NOT VERIFIED. Predicted mass was off by {result.verification_error_g:+.3f} g, "
        f"outside the {args.tolerance:g} g tolerance. The factor was recorded but "
        "readings will stay STABLE rather than MEASURED. Check the tare, the mounting, "
        "and that both masses are what they claim to be."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
