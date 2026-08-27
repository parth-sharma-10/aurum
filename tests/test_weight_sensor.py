"""Tests for the HX711 measurement path.

The property under test throughout: **a number only becomes MEASURED when it
has earned it.** Settled readings, a calibration that was checked against a
second known mass, and real hardware behind it. Everything short of that says
so, and nothing invalid turns into a zero.

No Arduino, no serial port and no load cell are needed. The reader is an
interface, and a scripted stand-in exercises the same filter, the same
stability window and the same calibration the real cell does.
"""

from __future__ import annotations

import math

import pytest

from app import config, materials
from app.calibrate import DEFAULT_VERIFY_TOLERANCE_G, derive_counts_per_gram, verify
from app.weight import (
    PROTOCOL_VERSION,
    Calibration,
    RawSample,
    SimulatedRawReader,
    WeightSensor,
    WeightStatus,
    parse_weight_line,
)

# Derived from the bench session of 2026-08-22: empty about -261600 counts,
# and about -196470 with a 180 g reference. Used here as TEST fixture values
# for the arithmetic. They are NOT this repository's calibration -- see
# configs/calibration.yaml, which ships UNMEASURED.
BENCH_TARE = -261598.75
BENCH_LOADED_180G = -196451.33
BENCH_COUNTS_PER_GRAM = derive_counts_per_gram(BENCH_TARE, BENCH_LOADED_180G, 180.0)


def calibration(verified: bool = True) -> Calibration:
    return Calibration(
        counts_per_gram=BENCH_COUNTS_PER_GRAM,
        tare_counts=BENCH_TARE,
        reference_mass_g=180.0,
        verified=verified,
        verification_mass_g=100.0 if verified else None,
        verification_error_g=0.0 if verified else None,
    )


def counts_for(grams: float, cal: Calibration | None = None) -> float:
    cal = cal or calibration()
    return cal.tare_counts + grams * cal.counts_per_gram


class ScriptedReader:
    """Replays raw counts. Stands in for the board without pretending to be one."""

    name = "scripted"

    def __init__(self, counts, connected=True, drop_after=None):
        self._counts = list(counts)
        self.connected = connected
        self.last_error = None
        self._drop_after = drop_after
        self.reads = 0
        self.closed = False

    def read(self):
        self.reads += 1
        if self._drop_after is not None and self.reads > self._drop_after:
            self.connected = False
            self.last_error = "the load cell disconnected"
            return None
        if not self._counts:
            return None
        value = self._counts.pop(0)
        return None if value is None else RawSample(raw_counts=value)

    def close(self):
        self.closed = True


def clock_factory(step: float = 0.05):
    """A deterministic monotonic clock so stability windows are exact."""
    state = {"t": 0.0}

    def now():
        state["t"] += step
        return state["t"]

    return now


def sensor_for(reader, cal=None, simulated=False, **env):
    cfg = config.load(environ={f"AURUM_{k.upper()}": str(v) for k, v in env.items()})
    return WeightSensor(
        reader, calibration=cal if cal is not None else calibration(), cfg=cfg, simulated=simulated
    )


class TestProtocol:
    def test_a_well_formed_line_parses(self):
        sample = parse_weight_line("W,1,10432,-261605,OK")
        assert sample.raw_counts == -261605.0
        assert sample.board_millis == 10432

    @pytest.mark.parametrize(
        "line",
        [
            "W,1,10432,-261605,ERR",  # the board said it failed
            "W,2,1,5,OK",  # a protocol version we do not speak
            "W,1,x,5,OK",  # malformed
            "W,1,1,5",  # truncated
            "-261605",  # a bare number: counts or grams? refuse to guess
            "",  # empty
            "   ",  # whitespace
            "W,1,1,nan,OK",  # not a number
            "W,1,1,inf,OK",
            "GARBAGE",
            None,
            42,
        ],
    )
    def test_anything_else_is_dropped(self, line):
        assert parse_weight_line(line) is None

    @pytest.mark.parametrize(
        "counts",
        [
            8388607,  # +2**23-1, the positive rail — MEASURED on the bench
            8388608,
            -8388608,  # the negative rail
            -8388609,
            16777215,  # an all-ones word read as unsigned
        ],
    )
    def test_a_count_at_the_converter_s_rail_is_not_a_mass(self, counts):
        """An HX711 emits its rail when the input is at or past full scale, so a
        sample there reports "outside my range" rather than a quantity. An open
        bridge, a shorted one and an absent cell all produce it.

        Measured on the bench on 2026-08-27 with the pan EMPTY: **25 frames of
        25 carried exactly 8388607 with status OK**, which a verified 392.2167
        counts/g rendered as 22058.4 g on a pan rated for a few hundred grams,
        while the console told the operator to take an object off an empty
        platform. A real cell wanders by tens of counts; only a rail repeats
        bit-for-bit.

        The rail itself is refused, not merely values beyond it — the first
        version of this guard stopped at `2**23` and did nothing at all against
        the reading the board was actually sending.
        """
        assert parse_weight_line(f"W,1,10432,{counts},OK") is None

    @pytest.mark.parametrize("counts", [8388606, -8388607, 0, -261605])
    def test_a_count_inside_the_rails_still_parses(self, counts):
        """One count inside each rail, because this bound is off-by-one prone in
        both directions and the range is otherwise untouched."""
        assert parse_weight_line(f"W,1,10432,{counts},OK").raw_counts == float(counts)

    def test_the_protocol_carries_raw_counts_not_grams(self):
        """Calibration lives in Python, so the wire format must be raw."""
        assert "raw_counts" in __import__("app.weight", fromlist=["PROTOCOL"]).PROTOCOL
        assert PROTOCOL_VERSION == 1


class TestCalibrationRecord:
    def test_an_uncalibrated_record_is_not_present(self):
        assert Calibration().present is False
        assert Calibration().grams(1000.0) is None

    def test_the_shipped_calibration_is_never_an_unverified_guess(self):
        """Guard: shipping a guessed factor would make every mass a guess.

        The rig was uncalibrated when this guard was written, so it asserted an
        absent factor. The invariant it was really protecting is narrower and
        survives the rig being calibrated: whatever ships must either have no
        factor at all, or have one checked against a second known mass. An
        unverified factor on disk is the dangerous case - `app.calibrate`
        records failed attempts deliberately, and such a record must never
        reach `present`.
        """
        shipped = Calibration.load()
        if shipped.has_factor:
            assert shipped.verified is True, "an unverified factor must not ship"
            assert shipped.verification_mass_g is not None
            assert shipped.present is True
        else:
            assert shipped.present is False

    def test_a_factor_converts_counts_to_grams(self):
        assert calibration().grams(counts_for(180.0)) == pytest.approx(180.0)

    def test_zero_grams_is_a_real_reading_not_an_absence(self):
        """An empty pan after tare weighs zero, and that is a measurement."""
        assert calibration().grams(BENCH_TARE) == pytest.approx(0.0)

    def test_a_record_round_trips_through_a_file(self, tmp_path):
        path = tmp_path / "calibration.yaml"
        calibration().save(path)
        assert Calibration.load(path).counts_per_gram == pytest.approx(BENCH_COUNTS_PER_GRAM)
        assert Calibration.load(path).verified is True

    @pytest.mark.parametrize("bad", ["UNMEASURED", "not a number", None, "nan", "inf"])
    def test_an_unusable_factor_reads_as_uncalibrated(self, tmp_path, bad):
        path = tmp_path / "calibration.yaml"
        path.write_text(f"calibration:\n  counts_per_gram: {bad}\n  tare_counts: 0\n")
        assert Calibration.load(path).present is False

    def test_malformed_yaml_is_uncalibrated_rather_than_fatal(self, tmp_path):
        path = tmp_path / "calibration.yaml"
        path.write_text("calibration: [unclosed\n")
        assert Calibration.load(path).present is False

    def test_a_missing_file_is_uncalibrated(self, tmp_path):
        assert Calibration.load(tmp_path / "absent.yaml").present is False

    def test_an_unverified_factor_is_not_present(self):
        """A failed calibration run must not open a gate a verified one opens.

        `app.calibrate` records a failed attempt rather than discarding it. One
        such run left 0.078 counts/g on disk, which reads an empty pan as
        -2033 g; `present` is what stops that record driving the machine.
        """
        unverified = Calibration(
            counts_per_gram=0.0784313725490196, tare_counts=-262685.55, verified=False
        )
        assert unverified.has_factor is True
        assert unverified.present is False

    def test_an_unverified_factor_still_converts_counts(self):
        """The arithmetic is `has_factor`, so the STABLE tier keeps its number."""
        unverified = Calibration(
            counts_per_gram=BENCH_COUNTS_PER_GRAM, tare_counts=BENCH_TARE, verified=False
        )
        assert unverified.present is False
        assert unverified.grams(counts_for(180.0)) == pytest.approx(180.0)


class TestCalibrationWorkflow:
    def test_a_factor_is_derived_from_a_known_mass(self):
        assert pytest.approx(361.93, abs=0.01) == BENCH_COUNTS_PER_GRAM

    def test_a_reference_mass_reads_itself_back(self):
        cal = Calibration(counts_per_gram=BENCH_COUNTS_PER_GRAM, tare_counts=BENCH_TARE)
        assert cal.grams(BENCH_LOADED_180G) == pytest.approx(180.0)

    @pytest.mark.parametrize("mass", [0.0, -5.0])
    def test_an_impossible_reference_mass_is_refused(self, mass):
        with pytest.raises(ValueError, match="must be positive"):
            derive_counts_per_gram(0.0, 100.0, mass)

    def test_a_cell_that_did_not_respond_is_refused(self):
        with pytest.raises(ValueError, match="did not respond"):
            derive_counts_per_gram(-261600.0, -261600.0, 180.0)

    def test_a_second_known_mass_verifies_the_factor(self):
        cal = calibration(verified=False)
        result = verify(cal, counts_for(100.0, cal), 100.0)
        assert result.verified is True
        assert result.verification_error_g == pytest.approx(0.0, abs=1e-6)

    def test_a_wrong_factor_fails_verification(self):
        """The reference mass always reads back right; a second mass catches it."""
        cal = calibration(verified=False)
        result = verify(cal, counts_for(105.0, cal), 100.0)
        assert result.verified is False
        assert result.verification_error_g == pytest.approx(5.0, abs=1e-6)

    def test_a_failed_verification_is_recorded_rather_than_discarded(self):
        result = verify(calibration(verified=False), counts_for(105.0), 100.0)
        assert result.counts_per_gram is not None
        assert result.recorded_at is not None

    @pytest.mark.parametrize(
        ("fraction", "expected"),
        [(0.5, True), (1.0, True), (1.5, False), (-0.5, True), (-1.5, False)],
    )
    def test_the_tolerance_boundary(self, fraction, expected):
        """Inclusive at the tolerance, on both sides of it.

        Expressed as a fraction of the tolerance rather than in grams, because
        the behaviour under test is the comparison - inclusive, and symmetric
        about zero - not whatever number the tolerance currently holds. Hard
        gram values made this fail when the tolerance was re-derived from
        measured repeatability, which is the tolerance changing, not the
        comparison breaking.

        The error is driven directly rather than through a counts round trip:
        solving for a mass that lands exactly on the tolerance goes through a
        float division that misses by an ulp, which would test the fixture's
        arithmetic instead of the comparison.
        """
        error_g = fraction * DEFAULT_VERIFY_TOLERANCE_G
        cal = Calibration(counts_per_gram=1.0, tare_counts=0.0, reference_mass_g=180.0)
        result = verify(cal, 100.0 + error_g, 100.0, tolerance_g=DEFAULT_VERIFY_TOLERANCE_G)
        assert result.verified is expected

    def test_an_uncalibrated_factor_cannot_be_verified(self):
        with pytest.raises(ValueError, match="uncalibrated"):
            verify(Calibration(), 100.0, 100.0)


class TestUncalibratedSensor:
    def test_real_hardware_without_a_calibration_refuses(self):
        reader = ScriptedReader([counts_for(180.0)] * 20)
        reading = sensor_for(reader, cal=Calibration()).read(now=clock_factory())
        assert reading.status is WeightStatus.UNAVAILABLE
        assert reading.usable is False

    def test_the_refusal_says_how_to_fix_it(self):
        reader = ScriptedReader([counts_for(180.0)] * 20)
        reading = sensor_for(reader, cal=Calibration()).read(now=clock_factory())
        assert "app.calibrate" in reading.reason

    def test_it_does_not_read_the_cell_at_all(self):
        """No point sampling a scale whose counts mean nothing."""
        reader = ScriptedReader([counts_for(180.0)] * 20)
        sensor_for(reader, cal=Calibration()).read(now=clock_factory())
        assert reader.reads == 0


class TestStability:
    def test_a_settled_series_on_a_verified_calibration_is_measured(self):
        reader = ScriptedReader([counts_for(180.0)] * 40)
        reading = sensor_for(reader).read(now=clock_factory())
        assert reading.status is WeightStatus.MEASURED
        assert reading.usable is True
        assert reading.grams == pytest.approx(180.0)

    def test_the_first_reading_is_never_accepted(self):
        """A cell settles; whichever number arrives first is the worst one."""
        reader = ScriptedReader([counts_for(180.0)] * 40)
        sensor_for(reader).read(now=clock_factory())
        assert reader.reads > 1

    @pytest.mark.parametrize("window_ms", [450, 500, 550, 730])
    def test_a_still_mass_settles_whatever_the_window_is(self, window_ms):
        """Regression: settling must not depend on the sample spacing.

        The window used to be judged by comparing `now` against the oldest
        sample still inside it, which quietly required a sample to land exactly
        on the boundary. A 450 ms window fed by a 10 Hz cell then never settled,
        however still the mass was, and the shipped 500 ms default only worked
        because 100 ms divides into it.
        """
        reader = ScriptedReader([counts_for(180.0)] * 400)
        reading = sensor_for(
            reader, weight_stability_window_ms=window_ms, weight_timeout_s=30
        ).read(now=clock_factory(step=0.1))
        assert reading.status is WeightStatus.MEASURED
        assert reading.grams == pytest.approx(180.0)

    def test_a_moving_series_is_unstable(self):
        drifting = [counts_for(180.0 + i * 5.0) for i in range(200)]
        reading = sensor_for(ScriptedReader(drifting)).read(now=clock_factory())
        assert reading.status is WeightStatus.UNSTABLE
        assert reading.usable is False

    def test_an_unstable_reading_still_reports_its_last_value(self):
        drifting = [counts_for(180.0 + i * 5.0) for i in range(200)]
        reading = sensor_for(ScriptedReader(drifting)).read(now=clock_factory())
        assert reading.grams > 0
        assert "did not settle" in reading.reason.lower()

    def test_inside_the_tolerance_still_settles(self):
        """Half the tolerance of wobble is a settled reading, not a moving one."""
        wobble = [counts_for(180.0 + (0.2 if i % 2 else -0.2)) for i in range(60)]
        reading = sensor_for(ScriptedReader(wobble), stability_tolerance_g=0.5).read(
            now=clock_factory()
        )
        assert reading.status is WeightStatus.MEASURED

    def test_outside_the_tolerance_does_not(self):
        wobble = [counts_for(180.0 + (2.0 if i % 2 else -2.0)) for i in range(200)]
        reading = sensor_for(ScriptedReader(wobble), stability_tolerance_g=0.5).read(
            now=clock_factory()
        )
        assert reading.status is WeightStatus.UNSTABLE

    def test_a_median_filter_absorbs_a_single_spike(self):
        series = [counts_for(180.0)] * 40
        series[10] = counts_for(9000.0)
        reading = sensor_for(ScriptedReader(series)).read(now=clock_factory())
        assert reading.grams == pytest.approx(180.0)

    def test_the_stability_window_is_configurable(self):
        reader = ScriptedReader([counts_for(180.0)] * 400)
        sensor = sensor_for(reader, weight_stability_window_ms=1000)
        assert sensor.window_ms == 1000.0
        assert sensor.read(now=clock_factory(step=0.05)).status is WeightStatus.MEASURED


class TestUnverifiedCalibration:
    def test_a_settled_reading_on_an_unverified_factor_is_stable_not_measured(self):
        reader = ScriptedReader([counts_for(180.0)] * 40)
        reading = sensor_for(reader, cal=calibration(verified=False)).read(now=clock_factory())
        assert reading.status is WeightStatus.STABLE
        assert reading.usable is False

    def test_the_reason_names_the_missing_second_mass(self):
        reader = ScriptedReader([counts_for(180.0)] * 40)
        reading = sensor_for(reader, cal=calibration(verified=False)).read(now=clock_factory())
        assert "second known mass" in reading.reason


class TestFailureModes:
    def test_a_timeout_with_no_data_is_unavailable(self):
        reading = sensor_for(ScriptedReader([])).read(now=clock_factory())
        assert reading.status is WeightStatus.UNAVAILABLE
        assert "within" in reading.reason

    def test_a_disconnect_mid_read_is_unavailable(self):
        reader = ScriptedReader([counts_for(180.0)] * 40, drop_after=3)
        reading = sensor_for(reader).read(now=clock_factory())
        assert reading.status is WeightStatus.UNAVAILABLE
        assert "disconnect" in reading.reason.lower()

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_a_non_finite_reading_is_unavailable_not_zero(self, bad):
        reading = sensor_for(ScriptedReader([bad] * 10)).read(now=clock_factory())
        assert reading.status is WeightStatus.UNAVAILABLE
        assert "finite" in reading.reason

    def test_an_invalid_reading_never_silently_becomes_a_mass(self):
        reading = sensor_for(ScriptedReader([math.nan] * 10)).read(now=clock_factory())
        assert reading.usable is False

    def test_dropped_lines_do_not_end_the_read(self):
        """A few unparseable lines are normal on a serial link."""
        series = [None, None, *([counts_for(180.0)] * 40)]
        reading = sensor_for(ScriptedReader(series)).read(now=clock_factory())
        assert reading.status is WeightStatus.MEASURED


class TestSimulation:
    def test_a_simulated_reading_is_labelled_and_never_measured(self):
        sensor = WeightSensor(
            SimulatedRawReader(grams=1840.0), calibration=Calibration(), simulated=True
        )
        reading = sensor.read(now=clock_factory())
        assert reading.status is WeightStatus.SIMULATED
        assert reading.usable is False
        assert reading.simulated is True

    def test_simulation_works_with_no_calibration_at_all(self):
        sensor = WeightSensor(SimulatedRawReader(), calibration=Calibration(), simulated=True)
        assert sensor.read(now=clock_factory()).grams > 0

    def test_the_warning_survives_serialisation(self):
        sensor = WeightSensor(SimulatedRawReader(), calibration=Calibration(), simulated=True)
        record = sensor.read(now=clock_factory()).as_dict()
        assert record["simulated"] is True
        assert "SIMULATED SENSOR" in record["warning"]
        assert record["status"] == "SIMULATED"


class TestDownstreamGate:
    """Only a MEASURED mass may drive a concentration-based estimate."""

    def test_a_measured_mass_unlocks_a_pcb_estimate(self):
        mass = {"grams": 1800.0, "simulated": False, "status": "MEASURED"}
        assert materials.estimate({"PCB": 1}, mass)["available"] is True

    @pytest.mark.parametrize("status", ["STABLE", "UNSTABLE", "UNAVAILABLE", "RAW"])
    def test_anything_less_than_measured_is_refused(self, status):
        mass = {"grams": 1800.0, "simulated": False, "status": status}
        result = materials.estimate({"PCB": 1}, mass)
        assert result["available"] is False
        assert status in result["reason"]

    def test_a_record_predating_the_measurement_path_is_unaffected(self):
        """Old ledger rows carry no status and must keep working."""
        mass = {"grams": 1800.0, "simulated": False}
        assert materials.estimate({"PCB": 1}, mass)["available"] is True


class TestItemIdentity:
    """The mass lands on the identity the tracker already minted."""

    def test_weighing_attaches_to_the_existing_item_id(self):
        from app.pipeline import ItemPipeline
        from app.vision import TrackedDetection

        pipeline = ItemPipeline()
        item = pipeline.process_detections(
            [TrackedDetection(1, "CPU", 0.9, (0, 0, 10, 10))], frame_id=0
        )[0]
        original = item.item_id

        sensor = sensor_for(ScriptedReader([counts_for(42.7)] * 40))
        weighed = pipeline.weigh_item(original, sensor, now=clock_factory())
        assert weighed.item_id == original
        assert weighed.weight_g == pytest.approx(42.7)
        assert weighed.weight_status == "MEASURED"
        assert weighed.weight_timestamp is not None

    def test_a_refusal_attaches_too_rather_than_leaving_a_hole(self):
        from app.pipeline import ItemPipeline
        from app.vision import TrackedDetection

        pipeline = ItemPipeline()
        item = pipeline.process_detections(
            [TrackedDetection(1, "PCB", 0.9, (0, 0, 10, 10))], frame_id=0
        )[0]
        sensor = sensor_for(ScriptedReader([]), cal=Calibration())
        weighed = pipeline.weigh_item(item.item_id, sensor, now=clock_factory())
        assert weighed.weight_status == "UNAVAILABLE"
        assert weighed.weight_reading["usable"] is False

    def test_weighing_an_unknown_id_returns_none(self):
        from app.pipeline import ItemPipeline

        sensor = sensor_for(ScriptedReader([counts_for(42.7)] * 40))
        assert ItemPipeline().weigh_item("AUR-ITEM-NOPE", sensor) is None

    def test_weighing_does_not_create_a_second_identity(self):
        from app.pipeline import ItemPipeline
        from app.vision import TrackedDetection

        pipeline = ItemPipeline()
        pipeline.process_detections([TrackedDetection(1, "CPU", 0.9, (0, 0, 10, 10))], frame_id=0)
        before = {i.item_id for i in pipeline.active_items}
        pipeline.weigh_item(
            next(iter(before)),
            sensor_for(ScriptedReader([counts_for(42.7)] * 40)),
            now=clock_factory(),
        )
        assert {i.item_id for i in pipeline.active_items} == before
