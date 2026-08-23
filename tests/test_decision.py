"""Tests for the A/B/C decision engine.

Three properties run through this file.

**C is reachable from every failure.** Bin C has no servo, so an unjustified
item reaches it by the machine doing nothing. Every path that cannot be
defended must end there, and no strong signal may skip a safety check.

**The engine never edits the evidence.** A PCB's cited precious fraction stays
higher than a CPU's. Policy decides the bin; it does not decide the chemistry.

**Every threshold is configuration.** No class name and no number appears in
the engine's logic, so these tests drive behaviour entirely through config.

Prices here are TEST fixture data. None of it is a market quote.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app import config
from app.decision import Bin, ReasonCode, class_support, decide
from app.valuation import valuation as valuation_module
from app.valuation.pmdi import MassStatus, OverallStatus
from app.valuation.prices import PriceService, StaticProvider, UnavailableProvider

NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)

# CPU-AU-001 gives 4.71 mg Au per piece = 0.00471 g. Choosing the mass sets the
# fraction exactly, which is how the boundary cases below stay unambiguous
# without inventing a single number.
CPU_AU_G = 0.00471


def mass_for_ppm(ppm: float, precious_g: float = CPU_AU_G) -> dict:
    """The measured mass that makes a CPU land on exactly `ppm`."""
    return {"grams": precious_g / ppm * 1_000_000, "simulated": False}


def measured(grams: float) -> dict:
    return {"grams": grams, "simulated": False}


def simulated(grams: float) -> dict:
    return {"grams": grams, "simulated": True}


def cfg(**env) -> config.Config:
    """Grading configuration built through the real config layer."""
    return config.load(environ={f"AURUM_{k.upper()}": str(v) for k, v in env.items()})


def unpriced() -> PriceService:
    return PriceService(UnavailableProvider(), max_age_seconds=900)


def priced() -> PriceService:
    """TEST fixture prices. Not market data."""
    table = {
        name: {
            "price_per_unit": value,
            "unit": "g",
            "currency": "USD",
            "timestamp": NOW.isoformat(),
        }
        for name, value in {
            "gold": 100.0,
            "silver": 1.0,
            "palladium": 40.0,
            "copper": 0.01,
            "nickel": 0.02,
            "tin": 0.03,
            "aluminium": 0.001,
        }.items()
    }
    return PriceService(StaticProvider(table, source="TEST fixture"), max_age_seconds=900)


def judge(component_class, confidence, mass=None, prices=None, configuration=None):
    """Value an item and decide its bin, the way the pipeline will."""
    prices = prices or unpriced()
    valuation = valuation_module.value({component_class: 1}, mass=mass, prices=prices, now=NOW)
    return decide(component_class, confidence, valuation, cfg=configuration or cfg())


class TestClassSupport:
    """The structural signal the ladder uses, derived from the database."""

    def test_cpu_has_composition_and_needs_no_mass(self):
        support = class_support("CPU")
        assert support == {"known": True, "has_composition": True, "requires_mass": False}

    def test_pcb_needs_a_mass_because_its_evidence_is_a_concentration(self):
        assert class_support("PCB")["requires_mass"] is True

    def test_ram_is_known_and_now_carries_composition(self):
        """Charles et al. 2017, per module - so no scale is required."""
        assert class_support("RAM") == {
            "known": True,
            "has_composition": True,
            "requires_mass": False,
        }

    def test_an_unlisted_class_is_unknown(self):
        assert class_support("GPU")["known"] is False


class TestBinA:
    def test_a_preferred_cpu_reaches_a(self):
        result = judge("CPU", 0.95, measured(42.7))
        assert result.decision is Bin.A
        assert result.reason_code is ReasonCode.A_PREFERRED_CLASS

    def test_a_preferred_connector_reaches_a(self):
        result = judge("Connector", 0.95, measured(5.0))
        assert result.decision is Bin.A
        assert result.reason_code is ReasonCode.A_PREFERRED_CLASS

    def test_the_reason_disclaims_any_scientific_ranking(self):
        reason = judge("CPU", 0.95, measured(42.7)).reason
        assert "engineering sorting policy" in reason
        assert "not a claim" in reason

    def test_changing_the_preferred_list_changes_the_outcome_with_no_code_change(self):
        configuration = cfg(bin_a_preferred_classes="PCB")
        assert judge("CPU", 0.95, measured(42.7), configuration=configuration).decision is Bin.B
        assert judge("PCB", 0.95, measured(1800.0), configuration=configuration).decision is Bin.A

    def test_a_class_outside_the_preferred_list_cannot_reach_a(self):
        configuration = cfg(bin_a_preferred_classes="RAM")
        result = judge("CPU", 0.95, measured(42.7), configuration=configuration)
        assert result.decision is Bin.B

    def test_with_class_awareness_off_a_high_fraction_reaches_a(self):
        configuration = cfg(grading_class_aware="false")
        result = judge("PCB", 0.95, measured(1800.0), configuration=configuration)
        assert result.decision is Bin.A
        assert result.reason_code is ReasonCode.A_PRECIOUS_FRACTION

    def test_with_class_awareness_off_a_low_fraction_does_not(self):
        configuration = cfg(grading_class_aware="false")
        result = judge("CPU", 0.95, measured(42.7), configuration=configuration)
        assert result.decision is Bin.B

    def test_a_configured_pmdi_value_can_promote_to_a(self):
        configuration = cfg(
            grading_class_aware="false",
            bin_a_min_precious_ppm=999999,
            bin_a_min_precious_value=0.1,
        )
        result = judge("CPU", 0.95, measured(42.7), prices=priced(), configuration=configuration)
        assert result.decision is Bin.A
        assert result.reason_code is ReasonCode.A_PMDI_VALUE

    def test_the_value_rule_is_inert_while_the_threshold_is_unmeasured(self):
        """minimum_precious_value ships UNMEASURED; it must not act as zero."""
        configuration = cfg(grading_class_aware="false", bin_a_min_precious_ppm=999999)
        result = judge("CPU", 0.95, measured(42.7), prices=priced(), configuration=configuration)
        assert result.decision is not Bin.A
        assert result.policy["bin_a_minimum_precious_value"] is None


class TestBinB:
    def test_a_pcb_with_a_measured_mass_reaches_b(self):
        result = judge("PCB", 0.95, measured(1800.0))
        assert result.decision is Bin.B
        assert result.reason_code is ReasonCode.B_PRECIOUS_FRACTION

    def test_a_cpu_below_the_premium_confidence_can_still_reach_b(self):
        result = judge("CPU", 0.65, measured(42.7))
        assert result.decision is Bin.B

    def test_base_metal_value_justifies_b_when_the_fraction_does_not(self):
        configuration = cfg(bin_b_min_precious_ppm=999999)
        result = judge("PCB", 0.95, measured(1800.0), prices=priced(), configuration=configuration)
        assert result.decision is Bin.B
        assert result.reason_code is ReasonCode.B_BASE_METAL_VALUE

    def test_per_piece_evidence_with_no_mass_is_recoverable_but_not_premium(self):
        configuration = cfg(grading_class_aware="false")
        result = judge("Connector", 0.95, mass=None, configuration=configuration)
        assert result.decision is Bin.B
        assert result.reason_code is ReasonCode.B_SUPPORTED_RECOVERABLE


class TestBinC:
    def test_a_class_with_no_cited_composition_is_unsupported(self):
        """C_UNSUPPORTED_MATERIAL still exists and still fires.

        RAM used to be the example. It is supported now, so the case is made
        with a class the database has never heard of - the same ladder rung,
        reached without pretending an evidence gap that has been closed.
        """
        result = judge("GPU", 0.95, measured(220.0))
        assert result.decision is Bin.C
        assert result.reason_code is ReasonCode.C_UNKNOWN_CLASS

    def test_an_unknown_class_is_refused(self):
        result = judge("GPU", 0.99, measured(100.0))
        assert result.decision is Bin.C
        assert result.reason_code is ReasonCode.C_UNKNOWN_CLASS

    def test_a_missing_class_is_refused(self):
        assert decide(None, 0.99, None, cfg=cfg()).reason_code is ReasonCode.C_UNKNOWN_CLASS

    def test_low_confidence_is_refused(self):
        result = judge("CPU", 0.10, measured(42.7))
        assert result.decision is Bin.C
        assert result.reason_code is ReasonCode.C_LOW_CONFIDENCE

    def test_a_pcb_without_a_measured_mass_is_refused(self):
        result = judge("PCB", 0.95, mass=None)
        assert result.decision is Bin.C
        assert result.reason_code is ReasonCode.C_UNMEASURED_WEIGHT

    def test_a_simulated_mass_does_not_satisfy_a_concentration(self):
        result = judge("PCB", 0.95, simulated(1800.0))
        assert result.decision is Bin.C
        assert result.reason_code is ReasonCode.C_UNMEASURED_WEIGHT

    @pytest.mark.parametrize("confidence", [None, -0.1, 1.5, "high"])
    def test_invalid_confidence_is_refused(self, confidence):
        valuation = valuation_module.value({"CPU": 1}, mass=measured(42.7), prices=unpriced())
        result = decide("CPU", confidence, valuation, cfg=cfg())
        assert result.decision is Bin.C
        assert result.reason_code is ReasonCode.C_INVALID_DATA

    def test_a_missing_valuation_is_refused(self):
        assert decide("CPU", 0.95, None, cfg=cfg()).reason_code is ReasonCode.C_MISSING_EVIDENCE

    def test_a_fraction_below_the_b_threshold_is_refused(self):
        configuration = cfg(grading_class_aware="false")
        result = judge("CPU", 0.95, mass_for_ppm(50.0), configuration=configuration)
        assert result.decision is Bin.C
        assert result.reason_code is ReasonCode.C_BELOW_THRESHOLD


class TestPriceUnavailablePolicy:
    def test_the_default_keeps_sorting_without_a_price(self):
        """A market outage must never stop the conveyor."""
        result = judge("CPU", 0.95, measured(42.7))
        assert result.signals["price_status"] == "UNAVAILABLE"
        assert result.signals["pmdi_value"] is None
        assert result.decision is Bin.A

    def test_the_strict_policy_refuses_without_a_price(self):
        configuration = cfg(price_unavailable_policy="route_to_c")
        result = judge("CPU", 0.95, measured(42.7), configuration=configuration)
        assert result.decision is Bin.C
        assert result.reason_code is ReasonCode.C_PRICE_UNAVAILABLE

    def test_the_strict_policy_allows_a_priced_item_through(self):
        configuration = cfg(price_unavailable_policy="route_to_c")
        result = judge("CPU", 0.95, measured(42.7), prices=priced(), configuration=configuration)
        assert result.decision is Bin.A

    def test_a_stale_price_is_not_current_enough_for_the_strict_policy(self):
        stale = PriceService(
            StaticProvider(
                {
                    "gold": {
                        "price_per_unit": 100.0,
                        "unit": "g",
                        "timestamp": "2020-01-01T00:00:00Z",
                    }
                }
            ),
            max_age_seconds=900,
        )
        configuration = cfg(price_unavailable_policy="route_to_c")
        result = judge("CPU", 0.95, measured(42.7), prices=stale, configuration=configuration)
        assert result.reason_code is ReasonCode.C_PRICE_UNAVAILABLE


class TestBoundaries:
    """threshold - epsilon, threshold, threshold + epsilon.

    The *threshold* is moved, not the item's mass. Solving for a mass that
    lands on a round ppm goes through a float division that lands a hair off,
    which would test the fixture's arithmetic instead of the engine's `>=`.
    Asking the pipeline for the fraction it actually computed removes that
    entirely.
    """

    MASS_G = 20.0  # fraction lands between the shipped B and A thresholds

    @staticmethod
    def actual_ppm(grams: float) -> float:
        valuation = valuation_module.value(
            {"CPU": 1}, mass=measured(grams), prices=unpriced(), now=NOW
        )
        return valuation.pmdi.precious_mass_fraction_ppm

    @pytest.mark.parametrize(
        ("confidence", "expected"),
        [(0.75 - 1e-9, Bin.B), (0.75, Bin.A), (0.75 + 1e-9, Bin.A)],
    )
    def test_the_bin_a_confidence_boundary(self, confidence, expected):
        assert judge("CPU", confidence, measured(42.7)).decision is expected

    @pytest.mark.parametrize(
        ("confidence", "expected"),
        [(0.60 - 1e-9, Bin.C), (0.60, Bin.B), (0.60 + 1e-9, Bin.B)],
    )
    def test_the_bin_b_confidence_boundary(self, confidence, expected):
        assert judge("CPU", confidence, measured(42.7)).decision is expected

    @pytest.mark.parametrize(("offset", "expected"), [(1e-6, Bin.B), (0.0, Bin.A), (-1e-6, Bin.A)])
    def test_the_bin_a_fraction_boundary(self, offset, expected):
        """Threshold just above the item's fraction, exactly on it, just below."""
        threshold = self.actual_ppm(self.MASS_G) + offset
        configuration = cfg(grading_class_aware="false", bin_a_min_precious_ppm=repr(threshold))
        result = judge("CPU", 0.95, measured(self.MASS_G), configuration=configuration)
        assert result.decision is expected

    @pytest.mark.parametrize(("offset", "expected"), [(1e-6, Bin.C), (0.0, Bin.B), (-1e-6, Bin.B)])
    def test_the_bin_b_fraction_boundary(self, offset, expected):
        threshold = self.actual_ppm(self.MASS_G) + offset
        configuration = cfg(
            grading_class_aware="false",
            bin_a_min_precious_ppm=999999,
            bin_b_min_precious_ppm=repr(threshold),
        )
        result = judge("CPU", 0.95, measured(self.MASS_G), configuration=configuration)
        assert result.decision is expected


class TestEvidenceIsNotManipulated:
    """The engine sorts. It does not edit the chemistry."""

    def test_the_pcb_fraction_stays_above_the_cpu_fraction(self):
        cpu = judge("CPU", 0.95, measured(42.7))
        pcb = judge("PCB", 0.95, measured(1800.0))
        assert pcb.signals["precious_mass_fraction_ppm"] > cpu.signals["precious_mass_fraction_ppm"]

    def test_the_cpu_reaches_a_on_policy_not_on_precious_content(self):
        cpu = judge("CPU", 0.95, measured(42.7))
        pcb = judge("PCB", 0.95, measured(1800.0))
        assert cpu.decision is Bin.A
        assert pcb.decision is Bin.B
        # The bins invert the fractions, and the reason codes say why.
        assert cpu.reason_code is ReasonCode.A_PREFERRED_CLASS
        assert pcb.reason_code is ReasonCode.B_PRECIOUS_FRACTION

    def test_the_cited_amounts_are_unchanged_by_the_decision(self):
        cpu = judge("CPU", 0.95, measured(42.7))
        assert cpu.signals["precious_mass_g"] == CPU_AU_G
        assert "CPU-AU-001" in cpu.signals["evidence_sources"]

    def test_turning_off_the_policy_restores_the_evidence_ordering(self):
        configuration = cfg(grading_class_aware="false")
        cpu = judge("CPU", 0.95, measured(42.7), configuration=configuration)
        pcb = judge("PCB", 0.95, measured(1800.0), configuration=configuration)
        assert pcb.decision is Bin.A
        assert cpu.decision is Bin.B


class TestRamRoutesOnEvidenceOnly:
    """RAM is now supported, and must be routed by its evidence and nothing else.

    This class used to assert that no threshold change could route RAM,
    because RAM had no composition at all. That gap closed when Charles et al.
    (2017) was obtained. The property worth protecting is the same one from
    the other side: RAM reaches a bin because cited evidence puts it there,
    and if the evidence is removed no threshold may rescue it.
    """

    def test_a_module_reaches_a_routed_bin_on_its_cited_content(self):
        result = judge("RAM", 1.0, measured(17.6))
        assert result.decision is not Bin.C
        assert result.signals["evidence_status"] == "SUPPORTED"
        assert result.signals["precious_mass_g"] > 0

    def test_the_decision_names_the_evidence_it_used(self):
        result = judge("RAM", 1.0, measured(17.6))
        assert set(result.signals["evidence_sources"]) == {
            "RAM-AU-001",
            "RAM-AG-001",
            "RAM-PD-001",
            "RAM-CU-001",
        }

    def test_a_weak_detection_is_still_refused(self):
        """Evidence does not excuse the safety ladder."""
        result = judge("RAM", 0.2, measured(17.6))
        assert result.decision is Bin.C
        assert result.reason_code is ReasonCode.C_LOW_CONFIDENCE

    def test_no_mass_is_required_for_a_per_module_class(self):
        """Per-piece evidence needs no scale, so an unweighed module still routes."""
        result = judge("RAM", 1.0, mass=None)
        assert result.reason_code is not ReasonCode.C_UNMEASURED_WEIGHT


class TestRamFailsClosedWithoutEvidence:
    """Strip the composition and no threshold may route RAM. The old guard, kept."""

    @pytest.fixture
    def stripped(self, tmp_path, monkeypatch):
        import yaml

        from app import materials

        db = yaml.safe_load(materials.REFERENCE.read_text())
        db["components"]["RAM"]["subtypes"]["dimm_module"]["composition"] = {}
        path = tmp_path / "no_ram_composition.yaml"
        path.write_text(yaml.safe_dump(db))
        monkeypatch.setattr(materials, "REFERENCE", path)
        return path

    @pytest.mark.parametrize(
        "env",
        [
            {"bin_a_min_precious_ppm": 0, "bin_b_min_precious_ppm": 0},
            {"bin_a_min_confidence": 0, "bin_b_min_confidence": 0},
            {"bin_a_preferred_classes": "RAM"},
            {"grading_class_aware": "false", "bin_a_min_precious_ppm": 0},
        ],
    )
    def test_no_threshold_change_can_route_ram(self, stripped, env):
        result = judge("RAM", 1.0, measured(7.8), configuration=cfg(**env))
        assert result.decision is Bin.C
        assert result.reason_code is ReasonCode.C_UNSUPPORTED_MATERIAL

    def test_even_with_prices_available(self, stripped):
        assert judge("RAM", 1.0, measured(7.8), prices=priced()).decision is Bin.C

    def test_the_reason_names_the_database_gap(self, stripped):
        assert "no cited composition" in judge("RAM", 1.0, measured(7.8)).reason


class TestSafetyLadderIsNotSkipped:
    def test_a_preferred_class_does_not_rescue_an_invalid_detection(self):
        valuation = valuation_module.value({"CPU": 1}, mass=measured(42.7), prices=unpriced())
        assert decide("CPU", None, valuation, cfg=cfg()).decision is Bin.C

    def test_a_preferred_class_does_not_rescue_a_missing_measurement(self):
        configuration = cfg(bin_a_preferred_classes="PCB")
        result = judge("PCB", 0.99, mass=None, configuration=configuration)
        assert result.decision is Bin.C
        assert result.reason_code is ReasonCode.C_UNMEASURED_WEIGHT

    def test_a_high_fraction_does_not_rescue_low_confidence(self):
        configuration = cfg(grading_class_aware="false")
        result = judge("PCB", 0.10, measured(1800.0), configuration=configuration)
        assert result.decision is Bin.C
        assert result.reason_code is ReasonCode.C_LOW_CONFIDENCE


class TestExplainability:
    def test_a_decision_carries_its_signals_policy_and_status(self):
        record = judge("CPU", 0.91, measured(42.7)).as_dict()
        for field in (
            "decision",
            "reason_code",
            "reason",
            "target_bin",
            "servo",
            "signals",
            "policy",
            "status",
            "threshold_note",
        ):
            assert field in record, field

    def test_the_confidence_threshold_actually_used_is_recorded(self):
        assert judge("CPU", 0.91, measured(42.7)).signals["confidence_threshold_applied"] == 0.75
        assert judge("CPU", 0.65, measured(42.7)).signals["confidence_threshold_applied"] == 0.60

    def test_thresholds_are_labelled_as_approximations(self):
        note = judge("CPU", 0.91, measured(42.7)).as_dict()["threshold_note"]
        assert "engineering approximations" in note
        assert "not presented as universally validated" in note

    def test_bin_c_has_no_servo(self):
        """The fail-safe is reached by the machine doing nothing."""
        assert judge("GPU", 0.95, measured(220.0)).servo is None

    def test_a_and_b_name_their_paddle(self):
        assert judge("CPU", 0.95, measured(42.7)).servo == "A"
        assert judge("PCB", 0.95, measured(1800.0)).servo == "B"

    def test_the_reason_codes_are_machine_readable(self):
        code = judge("GPU", 0.95, measured(220.0)).reason_code
        assert isinstance(code, ReasonCode)
        assert str(code) == "C_UNKNOWN_CLASS"


class TestStatusPropagation:
    def test_a_simulated_mass_is_never_reported_as_measured(self):
        result = judge("Connector", 0.95, simulated(5.0))
        assert result.signals["mass_status"] == str(MassStatus.SIMULATED)
        assert result.status is OverallStatus.SIMULATED

    def test_an_unmeasured_mass_is_reported_as_unmeasured(self):
        assert judge("CPU", 0.95, None).signals["mass_status"] == str(MassStatus.UNMEASURED)

    def test_a_stale_price_reaches_the_decision_status(self):
        stale = PriceService(
            StaticProvider(
                {
                    "gold": {
                        "price_per_unit": 100.0,
                        "unit": "g",
                        "timestamp": "2020-01-01T00:00:00Z",
                    }
                }
            ),
            max_age_seconds=900,
        )
        result = judge("CPU", 0.95, measured(42.7), prices=stale)
        assert result.status is OverallStatus.STALE
        assert result.signals["price_status"] == "STALE"


class TestEvidenceCompletenessIsReportedNotEnforced:
    """Completeness is a fact about the database, not a routing rule.

    A mixed object often yields a PARTIAL_ESTIMATE: the processor is valued
    from cited per-piece data while the board's per-kilogram figure is refused
    because one mass covers every component. That is reported in the signals so
    an operator can see it and a future policy can read it.

    It is deliberately NOT wired to a bin. No existing grading requirement says
    an incomplete estimate may not reach the premium stream, and inventing one
    here would bury a sorting policy inside the evidence layer where nobody
    could configure it. These tests pin that decision down so it cannot be
    added by accident.
    """

    @staticmethod
    def mixed(mass=None, configuration=None):
        # PCB is cited per kilogram, so a mass covering both classes cannot be
        # attributed to it and its line is refused. The CPU's per-piece gold
        # still stands, which is what makes the result partial rather than
        # absent.
        valuation = valuation_module.value(
            {"CPU": 1, "PCB": 1}, mass=mass, prices=unpriced(), now=NOW
        )
        return valuation, decide("CPU", 0.94, valuation, cfg=configuration or cfg())

    def test_the_signals_carry_the_completeness_and_what_was_missed(self):
        valuation, decision = self.mixed()
        assert valuation.completeness == "PARTIAL_ESTIMATE"
        signals = decision.as_dict()["signals"]
        assert signals["evidence_completeness"] == "PARTIAL_ESTIMATE"
        assert [v["component"] for v in signals["components_valued"]] == ["CPU"]
        assert [n["component"] for n in signals["components_not_valued"]] == ["PCB"]
        assert signals["components_not_valued"][0]["reason"]

    def test_a_partial_estimate_is_not_by_itself_a_reason_to_refuse_a_bin(self):
        """The same class, the same confidence, one extra unvalued component."""
        complete = judge("CPU", 0.94)
        _, partial = self.mixed()
        assert complete.decision is partial.decision
        assert complete.reason_code is partial.reason_code

    def test_no_reason_code_mentions_completeness(self):
        """If a completeness rule ever lands, it must announce itself."""
        _, decision = self.mixed()
        assert "PARTIAL" not in str(decision.reason_code)

    def test_completeness_never_overrides_the_safety_ladder(self):
        """Being partial does not excuse a weak detection either."""
        valuation = valuation_module.value(
            {"CPU": 1, "PCB": 1}, mass=None, prices=unpriced(), now=NOW
        )
        decision = decide("CPU", 0.2, valuation, cfg=cfg())
        assert decision.decision is Bin.C
        assert decision.reason_code is ReasonCode.C_LOW_CONFIDENCE

    def test_an_object_whose_every_class_lacks_evidence_still_fails_closed(self):
        """INSUFFICIENT_EVIDENCE is unchanged: nothing valued, nothing routed."""
        valuation = valuation_module.value({"PCB": 1}, mass=None, prices=unpriced())
        decision = decide("PCB", 0.94, valuation, cfg=cfg())
        assert valuation.completeness == "INSUFFICIENT_EVIDENCE"
        assert decision.decision is Bin.C
        assert decision.reason_code is ReasonCode.C_UNMEASURED_WEIGHT
