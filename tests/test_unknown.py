"""Tests for the UNKNOWN decision state and the mass plausibility check.

Two propositions, and they are the same proposition twice:

**UNKNOWN is not C.** "Aurum judged this and it did not qualify" and "Aurum
could not read this at all" both send an item to bin C by nobody doing
anything. Only the second is a reason to look at the camera, and the record now
says which happened.

**A mass never implies a composition.** An implausible mass says the identity,
the mounting or the calibration factor is wrong. It says nothing about metal,
and nothing here derives a bin from a weight.

Every mass in this file is a TEST value.
"""

from __future__ import annotations

import pytest

from app import config
from app.decision.engine import Bin, Decision, ReasonCode, decide, mass_anomaly, mass_range
from app.valuation import valuation as valuation_module
from app.valuation.prices import PriceService, UnavailableProvider


def cfg(**environ):
    return config.load(environ=environ)


def unpriced() -> PriceService:
    return PriceService(provider=UnavailableProvider())


def measured(grams: float) -> dict:
    return {"grams": grams, "simulated": False}


def judge(component_class, confidence, mass, counts=None, configuration=None):
    settings = configuration or cfg()
    valuation = valuation_module.value(counts or {component_class: 1}, mass=mass, prices=unpriced())
    return decide(component_class, confidence, valuation, cfg=settings)


class TestUnknownIsADecisionNotAPlace:
    def test_it_reaches_bin_c_physically(self):
        result = judge("GPU", 0.99, measured(100.0))
        assert result.decision is Bin.UNKNOWN
        assert result.physical_bin == "C"

    def test_it_has_no_servo(self):
        """There is no paddle for "I could not tell"."""
        assert judge("GPU", 0.99, measured(100.0)).servo is None

    def test_the_routing_target_is_the_physical_bin(self):
        """`app.routing` needs somewhere to send the item, and UNKNOWN is not."""
        assert judge("GPU", 0.99, measured(100.0)).target_bin == "C"

    def test_the_record_carries_both(self):
        record = judge("GPU", 0.99, measured(100.0)).as_dict()
        assert record["decision"] == "UNKNOWN"
        assert record["physical_bin"] == "C"
        assert record["physical_fallback"] == "C"
        assert record["unknown"] is True
        assert "not a destination" in record["decision_note"]

    def test_a_real_c_is_not_marked_unknown(self):
        """A judged item that did not qualify keeps its own answer.

        A CPU at 400 g is inside the plausible window and carries cited
        per-piece gold, so the ladder reaches the threshold rung and finds the
        fraction too low - a judgement, not a failure to read.
        """
        result = judge(
            "CPU",
            0.95,
            {"grams": 400.0, "simulated": False},
            configuration=cfg(AURUM_GRADING_CLASS_AWARE="false"),
        )
        assert result.decision is Bin.C
        assert result.unknown is False
        assert result.as_dict()["physical_fallback"] is None
        assert result.as_dict()["decision_note"] is None

    def test_the_fallback_bin_is_configuration(self):
        """Where an unreadable item goes is a policy, not a constant in Python."""
        result = judge("GPU", 0.99, measured(100.0), configuration=cfg(AURUM_GRADING_FALLBACK="B"))
        assert result.decision is Bin.UNKNOWN
        assert result.physical_bin == "B"

    def test_a_decision_defaults_to_c_as_its_fallback(self):
        assert Decision(Bin.UNKNOWN, ReasonCode.UNKNOWN_CLASS, "x").physical_bin == "C"


class TestWhichRungsAreUnknown:
    """Cannot judge -> UNKNOWN. Judged and did not qualify -> C."""

    @pytest.mark.parametrize(
        ("component_class", "confidence", "mass", "code"),
        [
            ("GPU", 0.99, measured(100.0), ReasonCode.UNKNOWN_CLASS),
            ("CPU", "high", measured(42.7), ReasonCode.UNKNOWN_DATA),
            ("CPU", 0.10, measured(42.7), ReasonCode.UNKNOWN_CONFIDENCE),
            ("PCB", 0.95, None, ReasonCode.UNKNOWN_WEIGHT),
            ("PCB", 0.95, {"grams": 1800.0, "simulated": True}, ReasonCode.UNKNOWN_WEIGHT),
        ],
        ids=["unknown class", "bad confidence", "weak detection", "no mass", "simulated mass"],
    )
    def test_a_rung_aurum_cannot_judge_is_unknown(self, component_class, confidence, mass, code):
        result = judge(component_class, confidence, mass)
        assert result.decision is Bin.UNKNOWN
        assert result.reason_code is code
        assert result.physical_bin == "C"

    def test_no_class_at_all_is_unknown(self):
        result = decide(None, 0.99, None, cfg=cfg())
        assert result.decision is Bin.UNKNOWN
        assert result.reason_code is ReasonCode.UNKNOWN_CLASS

    def test_below_the_threshold_is_a_judgement_and_stays_c(self):
        result = judge(
            "CPU",
            0.95,
            {"grams": 400.0, "simulated": False},
            configuration=cfg(AURUM_GRADING_CLASS_AWARE="false"),
        )
        assert result.reason_code is ReasonCode.C_BELOW_THRESHOLD
        assert result.decision is Bin.C
        assert result.unknown is False

    def test_a_price_policy_refusal_is_a_judgement_and_stays_c(self):
        result = judge(
            "CPU",
            0.95,
            measured(42.7),
            configuration=cfg(AURUM_PRICE_UNAVAILABLE_POLICY="route_to_c"),
        )
        assert result.reason_code is ReasonCode.C_PRICE_UNAVAILABLE
        assert result.decision is Bin.C

    def test_a_and_b_are_untouched(self):
        assert judge("CPU", 0.95, measured(42.7)).decision is Bin.A
        assert judge("PCB", 0.95, measured(1800.0)).decision is Bin.B


class TestMassPlausibility:
    def test_a_plausible_mass_passes(self):
        assert mass_anomaly("CPU", 42.7, cfg()) is None

    @pytest.mark.parametrize(
        ("component_class", "grams"),
        [
            ("CPU", 0.4),
            ("CPU", 5000.0),
            ("RAM", 0.1),
            ("RAM", 900.0),
            ("PCB", 1.0),
            ("PCB", 9000.0),
        ],
    )
    def test_an_impossible_mass_is_named(self, component_class, grams):
        assert mass_anomaly(component_class, grams, cfg()) is not None

    def test_the_window_is_generous_enough_for_a_whole_assembly(self):
        """One mass covers every component on a board. 842 g is a real fixture."""
        assert mass_anomaly("PCB", 842.0, cfg()) is None

    def test_no_mass_is_not_an_anomaly(self):
        """Absent and implausible are different facts."""
        assert mass_anomaly("PCB", None, cfg()) is None

    def test_a_class_with_no_configured_window_is_not_checked(self):
        assert mass_range("GPU", cfg()) is None
        assert mass_anomaly("GPU", 1e9, cfg()) is None

    def test_it_can_be_switched_off(self):
        assert mass_anomaly("RAM", 9000.0, cfg(AURUM_MASS_PLAUSIBILITY="false")) is None

    def test_the_window_is_configurable(self):
        assert mass_anomaly("RAM", 900.0, cfg(AURUM_MASS_MAX_RAM_G="1000")) is None


class TestMassAnomalyRoutes:
    def test_an_impossible_mass_makes_the_decision_unknown(self):
        result = judge("PCB", 0.95, measured(9000.0))
        assert result.decision is Bin.UNKNOWN
        assert result.reason_code is ReasonCode.UNKNOWN_MASS_ANOMALY
        assert result.physical_bin == "C"

    def test_the_reason_says_what_to_check(self):
        reason = judge("PCB", 0.95, measured(9000.0)).reason
        assert "MASS_ANOMALY" in reason
        assert "calibration factor" in reason

    def test_nothing_is_inferred_from_the_weight(self):
        """The composition must not be read off a mass that is already suspect."""
        reason = judge("PCB", 0.95, measured(9000.0)).reason
        assert "Nothing is inferred from it" in reason

    def test_a_high_confidence_detection_does_not_rescue_it(self):
        assert judge("CPU", 1.0, measured(4000.0)).decision is Bin.UNKNOWN

    def test_the_check_is_reported_even_when_it_passes(self):
        signals = judge("CPU", 0.95, measured(42.7)).signals["mass_plausibility"]
        assert signals["checked"] is True
        assert signals["plausible"] is True
        assert signals["min_g"] == 5.0
        assert signals["max_g"] == 500.0
        assert "ENGINEERING APPROXIMATION" in signals["basis"]

    def test_the_ladder_order_puts_an_unmeasured_mass_first(self):
        """No mass is a different problem from an impossible one, and comes first."""
        assert judge("PCB", 0.95, None).reason_code is ReasonCode.UNKNOWN_WEIGHT
