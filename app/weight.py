"""Mass input for a batch — real HX711 load cell, or a labelled simulation.

The PPT's bench unit pairs the camera with an HX711 load cell. That hardware is
not attached to a laptop running the demo, so this module has two backends and
is loud about which one is active. A simulated reading is never presented as a
measurement: `simulated` is true in the batch record and the dashboard renders
"SIMULATED SENSOR" in place of the value's label.
"""

from __future__ import annotations

import contextlib
import math
import os
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from app import config as config_module

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class WeightReading:
    """A mass, and everything needed to decide whether to trust it.

    The four fields after `source` were added for the measurement path and all
    default, so the original three-argument construction still works. `status`
    is the one downstream should branch on: `simulated` alone cannot express
    "real hardware, settled, but the calibration was never verified".
    """

    grams: float
    simulated: bool
    source: str
    status: str | None = None
    usable: bool | None = None
    reason: str | None = None
    raw_counts: float | None = None
    timestamp: str | None = None
    #: A deliberate demonstration stand-in, not a reading of anything. Set only
    #: by the demo fallback, and the one thing that lets the estimator consume a
    #: SIMULATED mass. It travels with the data rather than being looked up from
    #: configuration, so the permission and the number it applies to cannot
    #: drift apart.
    mock: bool = False

    def as_dict(self) -> dict:
        out = {
            "grams": round(self.grams, 1),
            "kg": round(self.grams / 1000.0, 3),
            "simulated": self.simulated,
            "source": self.source,
            **(
                {"warning": "SIMULATED SENSOR — not a physical measurement"}
                if self.simulated
                else {}
            ),
        }
        # Only present for readings that came through the measurement path, so
        # an existing record's shape is unchanged.
        for key, value in (
            ("status", str(self.status) if self.status else None),
            ("usable", self.usable),
            ("reason", self.reason),
            ("raw_counts", self.raw_counts),
            ("timestamp", self.timestamp),
            ("mock", True if self.mock else None),
        ):
            if value is not None:
                out[key] = value
        return out


class WeightSource:
    """Base interface."""

    simulated = True
    label = "unknown"

    def read(self) -> WeightReading:  # pragma: no cover - interface
        raise NotImplementedError


class SimulatedLoadCell(WeightSource):
    """A clearly-labelled stand-in for the HX711.

    Produces a slowly drifting value with sensor-like jitter so the dashboard
    behaves as it would with hardware attached. It is seeded from a fixed base
    so a demo is repeatable, and it is *always* flagged as simulated.
    """

    simulated = True
    label = "simulated"

    def __init__(self, base_grams: float = 1840.0, jitter_g: float = 2.5) -> None:
        self.base = base_grams
        self.jitter = jitter_g
        self._t0 = time.time()

    def read(self) -> WeightReading:
        t = time.time() - self._t0
        drift = math.sin(t / 7.0) * self.jitter
        noise = math.sin(t * 13.0) * (self.jitter / 4.0)
        return WeightReading(self.base + drift + noise, True, "simulated load cell")


class HX711LoadCell(WeightSource):
    """Real HX711 over a serial link from an Arduino.

    Expects the Arduino sketch to print one calibrated reading in grams per
    line. Kept deliberately thin — if the port is not there, construction fails
    and the caller falls back to simulation with a visible mode change, rather
    than silently reporting invented numbers.
    """

    simulated = False
    label = "hx711"

    def __init__(self, port: str, baud: int = 9600, timeout: float = 1.0) -> None:
        try:
            import serial  # pyserial, optional dependency
        except ImportError as exc:
            raise RuntimeError(
                "pyserial is not installed; `pip install pyserial` to use a real HX711 load cell"
            ) from exc
        self._ser = serial.Serial(port, baud, timeout=timeout)
        self._last = 0.0
        time.sleep(2.0)  # Arduino resets on serial open

    def read(self) -> WeightReading:
        line = self._ser.readline().decode("ascii", errors="ignore").strip()
        # A partial or noisy line keeps the previous good reading rather than
        # emitting garbage into a batch record.
        with contextlib.suppress(ValueError):
            self._last = float(line)
        return WeightReading(self._last, False, f"HX711 @ {self._ser.port}")


def get_weight_source(mode: str = "auto", port: str | None = None) -> WeightSource:
    """Resolve a weight source.

    mode: "auto" | "hx711" | "simulated" | "off"
    """
    if mode == "off":
        return None  # type: ignore[return-value]
    port = port or os.environ.get("AURUM_HX711_PORT")

    if mode in ("auto", "hx711") and port:
        try:
            src = HX711LoadCell(port)
            print(f"[weight] HX711 connected on {port}")
            return src
        except Exception as exc:
            if mode == "hx711":
                raise
            print(f"[weight] HX711 unavailable ({exc}); falling back to SIMULATED")
    elif mode == "hx711":
        raise RuntimeError("mode=hx711 requires --hx711-port or AURUM_HX711_PORT")

    print("[weight] using SIMULATED load cell — readings are not measurements")
    return SimulatedLoadCell()


# ---------------------------------------------------------------------------
# Phase 5: calibrated, filtered, stability-checked measurement.
#
# Everything above this line is the original mass input: a labelled simulation
# and a thin serial class that reads pre-calibrated grams. Everything below is
# the measurement path the conveyor uses, and it differs in one deliberate way:
# the Arduino sends RAW COUNTS and Python owns the calibration.
#
# That matters because a calibration factor is measured, auditable data. In
# Python it lives in a file under version control, next to the workflow that
# produced it and the second known mass that verified it. Burned into firmware
# it is a number nobody can check.
# ---------------------------------------------------------------------------

#: Serial line the Aurum weight sketch emits, one per HX711 sample:
#:
#:     W,<protocol_version>,<board_millis>,<raw_counts>,<status>
#:
#: Raw counts, never grams. `status` is OK or ERR. Nothing else is accepted:
#: a bare number could be counts or grams, and guessing which is exactly the
#: kind of assumption this project refuses to make.
PROTOCOL_VERSION = 1

#: The rails of a signed 24-bit converter. The HX711 is 24-bit two's
#: complement, and these two values are what it emits when its input is at or
#: beyond full scale - which is what an open bridge, a shorted one, or no cell
#: wired at all all produce.
#:
#: A reading AT a rail is refused, not just one beyond it. At the rail the
#: converter is reporting "outside my range", not a quantity: the true mass is
#: somewhere at-or-past full scale and is not knowable from the sample. That
#: makes a saturated cell and a legitimately maxed-out one indistinguishable -
#: correctly, because neither yields a mass. The cost is one count of headroom
#: at each end of a range this rig uses about 0.1% of.
#:
#: Measured on the bench on 2026-08-27 with the pan EMPTY: 25 frames of 25
#: carried exactly 8388607 with status OK, which a verified 392.2167 counts/g
#: rendered as 22058.4 g on a pan rated for a few hundred grams.
#:
#: A physical bound, not a plausibility heuristic. Class-level plausibility
#: stays in configs/grading.yaml, where it belongs.
HX711_MIN_COUNTS = -(2**23)
HX711_MAX_COUNTS = 2**23 - 1
PROTOCOL = "W,<version>,<board_millis>,<raw_counts>,<status>"

#: The sketch emits every 100 ms. A frame that arrives in appreciably less than
#: that came out of a buffer rather than off the wire, so half a period is the
#: line between replayed history and the present. It is a property of the
#: firmware's emit rate, not a tuning knob.
LIVE_FRAME_S = 0.05

CALIBRATION_FILE = ROOT / "configs" / "calibration.yaml"


class WeightStatus(StrEnum):
    """What a reading is, and whether anything may be built on it."""

    #: One unfiltered sample. No stability judgement yet.
    RAW = "RAW"
    #: Readings are still moving by more than the configured tolerance.
    UNSTABLE = "UNSTABLE"
    #: Settled, but the calibration behind it has not been verified against a
    #: second known mass. A number you may display, not one to compute with.
    STABLE = "STABLE"
    #: From the labelled simulation. Never presented as a measurement.
    SIMULATED = "SIMULATED"
    #: Settled, on a verified calibration. The only status a concentration
    #: calculation is allowed to consume.
    MEASURED = "MEASURED"
    #: No usable reading: uncalibrated, timed out, disconnected, or bad data.
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class Calibration:
    """The measured relationship between HX711 counts and grams.

    `verified` is true only once a *second* known mass has been placed and the
    predicted grams matched it within tolerance. One reference mass shows the
    cell responds; two show the scale is linear and the factor is right.
    """

    counts_per_gram: float | None = None
    tare_counts: float | None = None
    reference_mass_g: float | None = None
    verified: bool = False
    verification_mass_g: float | None = None
    verification_error_g: float | None = None
    recorded_at: str | None = None
    notes: str | None = None

    @property
    def has_factor(self) -> bool:
        """A factor and a tare exist, so counts can be converted at all.

        Says nothing about whether they are right. This is what the arithmetic
        needs; `present` is what the machine needs, and they are not the same
        question.
        """
        return bool(self.counts_per_gram) and self.tare_counts is not None

    @property
    def present(self) -> bool:
        """Calibrated AND verified against a second known mass.

        The gate every consumer that drives the machine checks. A factor is
        derived FROM the reference mass, so that mass always reads back
        correctly and an unverified factor can be arbitrarily wrong for every
        other load - a failed run once left 0.078 counts/g here, which reads an
        empty pan as -2033 g and would have opened this gate.

        `app.calibrate` deliberately records a failed attempt rather than
        discarding it, because knowing the factor was tried and missed is worth
        more than an empty file. This is what stops that record from being
        mistaken for a calibration.
        """
        return self.has_factor and self.verified

    def grams(self, raw_counts: float) -> float | None:
        """Convert raw counts to grams. None when there is no factor.

        Deliberately gated on `has_factor`, not `present`: an unverified factor
        still produces the number behind a STABLE reading, which is displayable
        and is explicitly not a measurement.
        """
        if not self.has_factor:
            return None
        return (raw_counts - self.tare_counts) / self.counts_per_gram

    def as_dict(self) -> dict:
        return {
            "counts_per_gram": self.counts_per_gram,
            "tare_counts": self.tare_counts,
            "reference_mass_g": self.reference_mass_g,
            "verified": self.verified,
            "verification_mass_g": self.verification_mass_g,
            "verification_error_g": self.verification_error_g,
            "recorded_at": self.recorded_at,
            "notes": self.notes,
        }

    @classmethod
    def load(cls, path: Path | None = None) -> Calibration:
        """Read the recorded calibration, or an empty one.

        An absent, unreadable or UNMEASURED file yields an uncalibrated
        instance rather than an error: an uncalibrated scale is a state the
        system must handle, not a reason to stop.
        """
        import yaml

        path = CALIBRATION_FILE if path is None else Path(path)
        if not path.exists():
            return cls()
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError:
            return cls(notes=f"{path.name} is not valid YAML; treating as uncalibrated")
        data = raw.get("calibration") or {}

        def number(key):
            value = data.get(key)
            if value is None or (isinstance(value, str) and value.strip().upper() == "UNMEASURED"):
                return None
            try:
                out = float(value)
            except (TypeError, ValueError):
                return None
            return None if math.isnan(out) or math.isinf(out) else out

        return cls(
            counts_per_gram=number("counts_per_gram"),
            tare_counts=number("tare_counts"),
            reference_mass_g=number("reference_mass_g"),
            verified=bool(data.get("verified", False)),
            verification_mass_g=number("verification_mass_g"),
            verification_error_g=number("verification_error_g"),
            recorded_at=data.get("recorded_at"),
            notes=data.get("notes"),
        )

    def save(self, path: Path | None = None) -> Path:
        import yaml

        path = CALIBRATION_FILE if path is None else Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Aurum - HX711 calibration record.\n"
            "# Written by the calibration workflow (python -m app.calibrate). This is\n"
            "# measured data about one physical machine: it is not a setting to\n"
            "# hand-edit, and it does not transfer to another rig.\n"
            "#\n"
            "# verified: true only after a SECOND known mass matched the prediction.\n\n"
            + yaml.safe_dump({"calibration": self.as_dict()}, sort_keys=False)
        )
        return path


@dataclass(frozen=True)
class RawSample:
    """One HX711 reading as it left the board."""

    raw_counts: float
    board_millis: int | None = None


def parse_weight_line(line: object) -> RawSample | None:
    """Parse one protocol line, or None if it is not one.

    Deliberately strict. Malformed, partial or ERR lines are dropped rather
    than coerced: a half-received line that happens to parse into a plausible
    number is worse than no reading at all.
    """
    if not isinstance(line, str):
        return None
    parts = line.strip().split(",")
    if len(parts) != 5 or parts[0].strip() != "W":
        return None
    try:
        version = int(parts[1])
        millis = int(parts[2])
        counts = float(parts[3])
    except (TypeError, ValueError):
        return None
    if version != PROTOCOL_VERSION or parts[4].strip().upper() != "OK":
        return None
    if math.isnan(counts) or math.isinf(counts):
        return None
    # A railed converter is not a heavy object. On 2026-08-27 an EMPTY pan
    # reported raw 8388608 - exactly 2**23 - which a verified 392.2167 counts/g
    # turned into 22058.4 g, repeated bit-for-bit for as long as it was watched,
    # while the console told the operator to remove an object that was not
    # there. A real cell wanders by tens of counts; only a rail repeats exactly.
    # Dropped here rather than downstream so that no consumer - pan machine,
    # calibration, estimator - can be handed one as a mass.
    if counts >= HX711_MAX_COUNTS or counts <= HX711_MIN_COUNTS:
        return None
    return RawSample(raw_counts=counts, board_millis=millis)


class RawReader:
    """Anything that yields HX711 counts. Implementations must not block forever."""

    name = "unknown"
    connected = False

    def read(self) -> RawSample | None:  # pragma: no cover - interface
        raise NotImplementedError

    def drain(self) -> None:
        """Discard anything buffered, so the next read is of the pan NOW.

        A no-op for readers that hold no buffer. It matters for a serial cell:
        the board streams unprompted at 10 Hz, so a caller that pauses - to let
        an operator place a mass - returns to a queue of frames describing the
        pan before they touched it.
        """
        return None

    def close(self) -> None:
        return None


class HX711SerialReader:
    """Raw counts from the Aurum weight sketch over USB serial.

    Owns no calibration and no filtering: it turns bytes into samples and
    reports honestly when it cannot. A disconnect mid-run is an expected
    condition on a machine with a USB cable, not an exception to propagate.
    """

    name = "hx711-serial"

    def __init__(self, port: str, baudrate: int = 9600, timeout_s: float = 1.0) -> None:
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError(
                "pyserial is not installed; `pip install pyserial` to read a real HX711"
            ) from exc
        self.port = port
        self._ser = serial.Serial(port, baudrate, timeout=timeout_s)
        self.connected = True
        self.last_error: str | None = None
        time.sleep(2.0)  # the board resets when the port opens

    def read(self) -> RawSample | None:
        if not self.connected:
            return None
        try:
            line = self._ser.readline().decode("ascii", errors="ignore")
        except Exception as exc:  # pyserial raises several unrelated types
            self.connected = False
            self.last_error = f"serial read failed on {self.port}: {exc}"
            return None
        return parse_weight_line(line)

    def drain(self, budget_s: float = 30.0) -> int:
        """Read forward until the stream is live, and return frames discarded.

        The sketch emits every 100 ms whether or not anyone listens, and the OS
        keeps the whole backlog. A caller that paused - to let an operator place
        a mass - is handed that history from the beginning, instantly, in order.
        Twenty such frames average to the pan as it was BEFORE the mass landed,
        which is how a calibration derived 0.08 counts/g from a cell that
        actually responds at ~394.

        Flushing the buffer is not enough, because the kernel refills it from
        the backlog immediately. What separates history from now is arrival
        time: a queued frame returns instantly, a live one makes the reader wait
        for the board to send it. So read forward until a frame has to be waited
        for. That needs no clock shared with the board.
        """
        if not self.connected:
            return 0
        with contextlib.suppress(Exception):
            self._ser.reset_input_buffer()
        discarded = 0
        deadline = time.monotonic() + budget_s
        while time.monotonic() < deadline:
            started = time.monotonic()
            line = self.read()
            if line is None:
                continue
            if time.monotonic() - started >= LIVE_FRAME_S:
                return discarded  # we waited for it, so it is of the present
            discarded += 1
        return discarded

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._ser.close()
        self.connected = False


class SimulatedRawReader:
    """A labelled stand-in producing counts, so the whole path can run dry.

    Feeds the same filter, the same stability window and the same calibration
    as the real cell, so a simulated run exercises the production code rather
    than an imitation of it. Its output can never become MEASURED.
    """

    name = "simulated"

    def __init__(self, grams: float = 1840.0, calibration: Calibration | None = None) -> None:
        self.connected = True
        self.grams = grams
        self._calibration = calibration
        self._t0 = time.time()

    def read(self) -> RawSample | None:
        drift = math.sin((time.time() - self._t0) / 7.0) * 2.5
        grams = self.grams + drift
        cal = self._calibration
        counts = (
            grams * cal.counts_per_gram + cal.tare_counts
            if cal and cal.has_factor
            else grams * 1000.0
        )
        return RawSample(raw_counts=counts)


class WeightSensor:
    """Raw counts to a mass you can defend, or an explicit refusal.

        samples -> median filter -> stability window -> calibration -> status

    The first reading is never accepted. A load cell settles, a belt vibrates,
    and a hand leaving the pan takes a moment; whichever number arrives first
    is the least trustworthy one in the series.
    """

    def __init__(
        self,
        reader,
        calibration: Calibration | None = None,
        cfg: config_module.Config | None = None,
        simulated: bool | None = None,
    ) -> None:
        cfg = config_module.load() if cfg is None else cfg
        self.reader = reader
        self.calibration = Calibration.load() if calibration is None else calibration
        self.window_ms = cfg["conveyor.weight.stability_window_ms"]
        self.tolerance_g = cfg["conveyor.weight.stability_tolerance_g"]
        self.timeout_s = cfg["conveyor.weight.timeout_s"]
        self.filter_samples = cfg["conveyor.weight.filter_samples"]
        self.simulated = (
            getattr(reader, "name", "") == SimulatedRawReader.name
            if simulated is None
            else simulated
        )

    # -- helpers ----------------------------------------------------------
    def _unavailable(self, reason: str, raw: float | None = None) -> WeightReading:
        return WeightReading(
            grams=0.0,
            simulated=self.simulated,
            source=getattr(self.reader, "name", "unknown"),
            status=WeightStatus.UNAVAILABLE,
            usable=False,
            reason=reason,
            raw_counts=raw,
            timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    def _reading(self, grams: float, status: WeightStatus, raw, reason=None) -> WeightReading:
        return WeightReading(
            grams=grams,
            simulated=self.simulated,
            source=getattr(self.reader, "name", "unknown"),
            status=status,
            usable=status is WeightStatus.MEASURED,
            reason=reason,
            raw_counts=raw,
            timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    # -- reading ----------------------------------------------------------
    def read(self, now=None) -> WeightReading:
        """Collect samples until the reading settles, or say why it did not."""
        clock = time.monotonic if now is None else now
        # `has_factor`, not `present`: an unverified factor may still produce a
        # settled reading, which `_settled` labels STABLE and never MEASURED.
        # Gating on `present` here would delete that tier.
        if not self.simulated and not self.calibration.has_factor:
            return self._unavailable(
                "The load cell is not calibrated. Run `python -m app.calibrate` to "
                "record a factor, then verify it with a second known mass."
            )

        deadline = clock() + self.timeout_s
        window_s = self.window_ms / 1000.0
        raw_buffer: list[float] = []
        last_grams: float | None = None
        last_raw: float | None = None
        # The current run of readings that have stayed within tolerance, as the
        # moment it started and the extremes seen since. Tracked as a running
        # min/max rather than a list of timestamped samples, because comparing
        # `now` against the oldest sample still inside the window silently
        # requires a sample to land exactly on the boundary: a 450 ms window fed
        # by a 10 Hz cell never settles at all, however still the mass is. The
        # shipped 500 ms window only worked because 100 ms divides into it.
        stable_since: float | None = None
        run_min = run_max = 0.0

        while clock() < deadline:
            sample = self.reader.read()
            if sample is None:
                if not getattr(self.reader, "connected", True):
                    return self._unavailable(
                        getattr(self.reader, "last_error", None)
                        or "The load-cell reader disconnected."
                    )
                continue

            raw_buffer.append(sample.raw_counts)
            raw_buffer = raw_buffer[-max(1, int(self.filter_samples)) :]
            # Median, not mean: one electrically noisy sample should not move
            # the answer, and a spike is exactly what a mean would smear in.
            filtered = statistics.median(raw_buffer)
            grams = self._to_grams(filtered)
            if grams is None or math.isnan(grams) or math.isinf(grams):
                return self._unavailable("The reading is not a finite number.", filtered)

            last_grams, last_raw = grams, filtered
            stamp = clock()

            if stable_since is None:
                # The first reading is never accepted on its own: a cell
                # settles, a bench vibrates, and a hand leaving the pan takes a
                # moment. It only opens the window.
                stable_since, run_min, run_max = stamp, grams, grams
                continue

            run_min, run_max = min(run_min, grams), max(run_max, grams)
            if run_max - run_min > self.tolerance_g:
                # It moved. The window restarts from here rather than from the
                # start of the run, which is what "stayed within tolerance for
                # the whole window" has to mean.
                stable_since, run_min, run_max = stamp, grams, grams
            elif stamp - stable_since >= window_s:
                return self._settled(grams, last_raw, run_max - run_min)

        if last_grams is None:
            return self._unavailable(f"No usable reading arrived within {self.timeout_s:.1f}s.")
        return self._reading(
            last_grams,
            WeightStatus.UNSTABLE,
            last_raw,
            f"Did not settle within {self.tolerance_g:.2f} g over "
            f"{self.window_ms:.0f} ms before the {self.timeout_s:.1f}s timeout.",
        )

    def _settled(self, grams: float, raw: float | None, spread: float) -> WeightReading:
        if self.simulated:
            return self._reading(
                grams,
                WeightStatus.SIMULATED,
                raw,
                "SIMULATED SENSOR - not a physical measurement.",
            )
        if not self.calibration.verified:
            return self._reading(
                grams,
                WeightStatus.STABLE,
                raw,
                "The reading settled, but this calibration has not been verified "
                "against a second known mass, so it is not a measurement.",
            )
        return self._reading(
            grams,
            WeightStatus.MEASURED,
            raw,
            f"Settled within {spread:.3f} g on a verified calibration.",
        )

    def _to_grams(self, raw: float) -> float | None:
        if self.simulated and not self.calibration.has_factor:
            # The simulation encodes grams at a nominal 1000 counts/g so the
            # whole path runs before any cell is calibrated.
            return raw / 1000.0
        return self.calibration.grams(raw)

    def tare(self, samples: int = 20) -> float | None:
        """Average the empty-pan counts. The zero every later reading subtracts."""
        collected = []
        deadline = time.monotonic() + self.timeout_s
        while len(collected) < samples and time.monotonic() < deadline:
            sample = self.reader.read()
            if sample is not None:
                collected.append(sample.raw_counts)
        return statistics.fmean(collected) if collected else None

    def close(self) -> None:
        self.reader.close()
