"""Tests for the calibration workflow itself.

The property under test: **the workflow refuses rather than writing a factor it
cannot defend.** A tare is the zero every later reading subtracts, so a
calibration derived from a dead cell does not fail loudly later - it produces a
confident, wrong mass for as long as nobody re-checks it.

`derive_counts_per_gram` and `verify` are exercised in `test_weight_sensor.py`.
What is covered here is the collection loop and the CLI around them, which had
no test file at all.
"""

from __future__ import annotations

import pytest

from app.calibrate import READ_BUDGET_S, _average
from app.weight import Calibration, RawSample, StuckWatch, WeightSensor

BENCH_TARE = -263078.25
BENCH_COUNTS_PER_GRAM = 392.2166666666667


def calibration() -> Calibration:
    return Calibration(
        counts_per_gram=BENCH_COUNTS_PER_GRAM,
        tare_counts=BENCH_TARE,
        reference_mass_g=204.0,
        verified=True,
    )


class StuckReader:
    """A converter that has stopped converting: connected, and saying nothing.

    The shape that matters - `connected` stays True, because a frozen converter
    is not a disconnect - is what made the collection loop unbounded.
    """

    name = "hx711-serial"

    def __init__(self, counts: float = 0.0) -> None:
        self.connected = True
        self._watch = StuckWatch()
        self._counts = counts
        self.reads = 0

    @property
    def stuck(self) -> bool:
        return self._watch.error is not None

    @property
    def last_error(self) -> str | None:
        return self._watch.error

    def read(self):
        self.reads += 1
        return self._watch.accept(RawSample(raw_counts=self._counts))


class SilentReader:
    """Connected, never stuck, and never delivering a frame."""

    name = "hx711-serial"
    connected = True
    stuck = False
    last_error = None

    def __init__(self) -> None:
        self.reads = 0

    def read(self):
        self.reads += 1
        return None


def sensor_for(reader) -> WeightSensor:
    return WeightSensor(reader, calibration=calibration(), simulated=False)


class TestARefusalRatherThanAFactor:
    def test_a_frozen_converter_refuses_instead_of_looping_for_ever(self):
        """Refused for the RIGHT reason, not by outlasting the budget.

        Asserted with a budget it would blow through, so that removing the
        stuck check fails here instead of quietly falling through to the
        timeout - which is a backstop, not the diagnosis.
        """
        reader = StuckReader()
        with pytest.raises(RuntimeError) as excinfo:
            _average(sensor_for(reader), samples=20, label="empty pan", budget_s=60.0)
        assert "empty pan" in str(excinfo.value)
        assert "of 20 frames arrived" not in str(excinfo.value)

    def test_the_refusal_carries_the_readers_own_diagnosis(self):
        """The operator needs the wiring, not 'calibration failed'."""
        reader = StuckReader()
        with pytest.raises(RuntimeError) as excinfo:
            _average(sensor_for(reader), samples=20, label="empty pan")
        assert "DOUT/SCK" in str(excinfo.value)

    def test_an_open_cell_resting_at_zero_refuses_too(self):
        """The other shape of the same fault - a floating line, not a frozen one."""

        class Floating(StuckReader):
            def read(self):
                self.reads += 1
                # 0 / -1 alternating: never bit-for-bit equal, still not a mass.
                self._counts = 0.0 if self.reads % 2 else -1.0
                return self._watch.accept(RawSample(raw_counts=self._counts))

        reader = Floating()
        with pytest.raises(RuntimeError) as excinfo:
            _average(sensor_for(reader), samples=20, label="empty pan")
        assert "digital zero" in str(excinfo.value)

    def test_it_gives_up_rather_than_waiting_for_a_board_that_is_silent(self):
        reader = SilentReader()
        with pytest.raises(RuntimeError) as excinfo:
            _average(sensor_for(reader), samples=20, label="reference mass", budget_s=0.2)
        assert "0 of 20" in str(excinfo.value)

    def test_the_budget_is_generous_against_a_healthy_board(self):
        """20 samples at 10 Hz is ~2 s. The bound must not fire on a real rig."""
        assert READ_BUDGET_S > 20 * 0.1 * 5
