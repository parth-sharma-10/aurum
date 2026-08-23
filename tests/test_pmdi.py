"""Tests for PMDI and valuation.

Two properties run through the whole file.

**Nothing is invented.** Every figure traces to a cited evidence id, and where
evidence or price is missing the result is an explicit refusal rather than a
zero. A zero and an absence look identical in a total, which is how an
understated number gets quoted as a real one.

**Status propagates.** A simulated mass, a stale price or a missing citation
must still be visible in the output after passing through three layers.

Every price here is TEST fixture data. None of it is a market quote.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import materials
from app.valuation import pmdi as pmdi_module
from app.valuation import valuation as valuation_module
from app.valuation.pmdi import (
    BASE_METALS,
    PRECIOUS_METALS,
    EvidenceStatus,
    MassStatus,
    OverallStatus,
)
from app.valuation.prices import PriceService, PriceStatus, StaticProvider, UnavailableProvider

NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)

# Round TEST numbers, chosen so hand-computed expectations stay exact.
TEST_PRICE_PER_GRAM = {
    "gold": 100.0,
    "silver": 1.0,
    "palladium": 40.0,
    "copper": 0.01,
    "nickel": 0.02,
    "tin": 0.03,
    "aluminium": 0.001,
}

MEASURED_CPU = {"grams": 42.7, "simulated": False, "source": "HX711"}
MEASURED_PCB = {"grams": 1800.0, "simulated": False, "source": "HX711"}
SIMULATED_PCB = {"grams": 1800.0, "simulated": True, "source": "simulated load cell"}


def priced(ago: float = 60.0, currency: str = "USD", only=None) -> PriceService:
    """A TEST price service. Never live, never spot, never market data."""
    timestamp = (NOW - timedelta(seconds=ago)).isoformat()
    table = {
        material: {
            "price_per_unit": value,
            "unit": "g",
            "currency": currency,
            "timestamp": timestamp,
        }
        for material, value in TEST_PRICE_PER_GRAM.items()
        if only is None or material in only
    }
    return PriceService(StaticProvider(table, source="TEST fixture"), max_age_seconds=900)


def unpriced() -> PriceService:
    return PriceService(UnavailableProvider(), max_age_seconds=900)


class TestMetalGroups:
    def test_the_precious_set_is_the_four_named_metals(self):
        assert PRECIOUS_METALS == ("Au", "Ag", "Pd", "Pt")

    def test_the_base_set_is_the_three_named_metals(self):
        assert BASE_METALS == ("Cu", "Ni", "Sn")

    def test_aluminium_is_neither_and_is_reported_separately(self):
        """Al has cited evidence but was not asked for; it must not be hidden."""
        assert "Al" not in PRECIOUS_METALS + BASE_METALS
        result = pmdi_module.compute({"PCB": 1}, mass=MEASURED_PCB, prices=unpriced(), now=NOW)
        assert "Al" in result.other
        assert result.other["Al"].grams > 0


class TestCpu:
    def test_a_cpu_uses_the_cited_per_piece_gold_figure(self):
        """CPU-AU-001: 4.71 mg per piece. 1 piece -> 0.00471 g."""
        result = pmdi_module.compute({"CPU": 1}, mass=MEASURED_CPU, prices=unpriced(), now=NOW)
        assert result.available
        assert result.precious["Au"].grams == pytest.approx(0.00471)
        assert result.precious["Au"].evidence == ["CPU-AU-001"]

    def test_counts_multiply(self):
        result = pmdi_module.compute({"CPU": 3}, mass=MEASURED_CPU, prices=unpriced(), now=NOW)
        assert result.precious["Au"].grams == pytest.approx(3 * 0.00471)

    def test_the_cited_evidence_names_no_silver_or_palladium(self):
        """The gap is real. Adding Ag/Pd here would be fabrication."""
        result = pmdi_module.compute({"CPU": 1}, mass=MEASURED_CPU, prices=unpriced(), now=NOW)
        assert set(result.precious) == {"Au"}
        assert result.base == {}

    def test_the_fraction_is_price_independent(self):
        result = pmdi_module.compute({"CPU": 1}, mass=MEASURED_CPU, prices=unpriced(), now=NOW)
        assert result.precious_mass_fraction_ppm == pytest.approx(0.00471 / 42.7 * 1e6)
        assert result.pmdi_value is None

    def test_pmdi_value_with_a_test_price(self):
        result = pmdi_module.compute({"CPU": 1}, mass=MEASURED_CPU, prices=priced(), now=NOW)
        assert result.pmdi_value == pytest.approx(0.00471 * 100.0)
        assert result.price_status is PriceStatus.TEST


class TestConnector:
    def test_a_connector_uses_the_cited_per_piece_figure(self):
        result = pmdi_module.compute({"Connector": 1}, prices=unpriced(), now=NOW)
        assert result.available
        assert result.precious["Au"].grams == pytest.approx(0.000914)

    def test_a_connector_needs_no_mass_because_the_evidence_is_per_piece(self):
        """Per-piece evidence multiplies a count; only concentrations need mass."""
        result = pmdi_module.compute({"Connector": 2}, prices=unpriced(), now=NOW)
        assert result.available
        assert result.mass_status is MassStatus.UNMEASURED

    def test_without_a_mass_there_is_no_fraction(self):
        result = pmdi_module.compute({"Connector": 2}, prices=unpriced(), now=NOW)
        assert result.precious_mass_fraction_ppm is None


class TestPcb:
    def test_a_measured_mass_unlocks_the_concentration_evidence(self):
        """400 mg/kg Au x 1.8 kg = 0.72 g."""
        result = pmdi_module.compute({"PCB": 1}, mass=MEASURED_PCB, prices=unpriced(), now=NOW)
        assert result.available
        assert result.precious["Au"].grams == pytest.approx(0.72)
        assert result.precious["Ag"].grams == pytest.approx(2.34)
        assert result.precious["Pd"].grams == pytest.approx(0.9)

    def test_base_metals_are_present_for_a_pcb(self):
        result = pmdi_module.compute({"PCB": 1}, mass=MEASURED_PCB, prices=unpriced(), now=NOW)
        assert set(result.base) == {"Cu", "Ni", "Sn"}
        assert result.base["Cu"].grams == pytest.approx(385.92)

    def test_without_a_mass_a_concentration_cannot_be_used(self):
        result = pmdi_module.compute({"PCB": 1}, prices=unpriced(), now=NOW)
        assert not result.available
        assert result.evidence_status is EvidenceStatus.MISSING

    def test_a_simulated_mass_is_refused_for_concentration_evidence(self):
        result = pmdi_module.compute({"PCB": 1}, mass=SIMULATED_PCB, prices=unpriced(), now=NOW)
        assert not result.available
        assert result.mass_status is MassStatus.SIMULATED

    def test_a_refusal_reports_no_fraction_rather_than_zero_ppm(self):
        """'No cited evidence' must never render as '0 ppm precious metal'."""
        result = pmdi_module.compute({"PCB": 1}, mass=SIMULATED_PCB, prices=unpriced(), now=NOW)
        assert result.precious_mass_fraction_ppm is None

    def test_the_pcb_fraction_matches_the_cited_concentrations(self):
        """Au 400 + Ag 1300 + Pd 500 mg/kg = 2200 ppm precious."""
        result = pmdi_module.compute({"PCB": 1}, mass=MEASURED_PCB, prices=unpriced(), now=NOW)
        assert result.precious_mass_fraction_ppm == pytest.approx(2200.0)


class TestRam:
    def test_ram_has_no_cited_composition_and_is_unavailable(self):
        result = pmdi_module.compute({"RAM": 1}, mass={"grams": 7.8, "simulated": False}, now=NOW)
        assert not result.available
        assert result.evidence_status is EvidenceStatus.MISSING
        assert result.overall_status is OverallStatus.UNAVAILABLE

    def test_the_refusal_says_why(self):
        result = pmdi_module.compute({"RAM": 1}, prices=unpriced(), now=NOW)
        assert "RAM" in result.reason

    def test_ram_never_acquires_a_value_regression_guard(self):
        """If this fails, someone invented RAM composition data."""
        result = valuation_module.value({"RAM": 4}, mass=MEASURED_PCB, prices=priced(), now=NOW)
        assert result.pmdi.pmdi_value is None
        assert result.total_value is None
        assert result.as_dict()["precious_value"] is None

    def test_one_blocked_class_makes_the_whole_result_partial(self):
        """A total that quietly drops a component still reads as a total.

        So it is not quiet. The CPU's cited gold is reported, the RAM gap is
        reported alongside it, and the result declares itself PARTIAL so no
        consumer can read the figure as covering both.
        """
        result = pmdi_module.compute({"CPU": 1, "RAM": 1}, mass=MEASURED_CPU, now=NOW)

        assert result.available
        assert result.completeness == materials.PARTIAL_ESTIMATE
        assert result.evidence_status is EvidenceStatus.PARTIAL
        assert [v["component"] for v in result.valued] == ["CPU"]
        assert [n["component"] for n in result.not_valued] == ["RAM"]
        assert "PARTIAL ESTIMATE" in result.reason

    def test_a_partial_result_carries_no_ram_contribution(self):
        """Whatever else is true, no gram in the total came from the module."""
        alone = pmdi_module.compute({"CPU": 1}, mass=MEASURED_CPU, now=NOW)
        mixed = pmdi_module.compute({"CPU": 1, "RAM": 9}, mass=MEASURED_CPU, now=NOW)
        assert mixed.precious_mass_g == alone.precious_mass_g


class TestPricing:
    def test_no_provider_means_no_value_and_a_stated_reason(self):
        result = pmdi_module.compute({"CPU": 1}, mass=MEASURED_CPU, prices=unpriced(), now=NOW)
        assert result.pmdi_value is None
        assert result.price_status is PriceStatus.UNAVAILABLE
        assert "No price provider is configured" in result.reason

    def test_a_stale_price_still_computes_but_says_so(self):
        result = pmdi_module.compute(
            {"CPU": 1}, mass=MEASURED_CPU, prices=priced(ago=1800), now=NOW
        )
        assert result.pmdi_value == pytest.approx(0.471)
        assert result.price_status is PriceStatus.STALE
        assert result.overall_status is OverallStatus.STALE

    def test_a_partial_price_set_refuses_rather_than_understating(self):
        """Gold priced, silver and palladium not: a PCB total would be wrong."""
        result = pmdi_module.compute(
            {"PCB": 1}, mass=MEASURED_PCB, prices=priced(only={"gold"}), now=NOW
        )
        assert result.available
        assert result.pmdi_value is None
        assert "Ag" in result.reason and "Pd" in result.reason

    def test_multiple_metals_are_summed_at_their_own_prices(self):
        result = pmdi_module.compute({"PCB": 1}, mass=MEASURED_PCB, prices=priced(), now=NOW)
        expected = 0.72 * 100.0 + 2.34 * 1.0 + 0.9 * 40.0
        assert result.pmdi_value == pytest.approx(expected)


class TestValuation:
    def test_the_base_metal_signal_is_separate_from_pmdi(self):
        """PMDI is precious only. Base metals are a different signal."""
        result = valuation_module.value({"PCB": 1}, mass=MEASURED_PCB, prices=priced(), now=NOW)
        assert result.pmdi.pmdi_value == pytest.approx(0.72 * 100 + 2.34 * 1 + 0.9 * 40)
        assert result.base_value == pytest.approx(385.92 * 0.01 + 18.54 * 0.02 + 56.52 * 0.03)

    def test_the_total_is_the_two_signals_added(self):
        result = valuation_module.value({"PCB": 1}, mass=MEASURED_PCB, prices=priced(), now=NOW)
        assert result.total_value == pytest.approx(result.pmdi.pmdi_value + result.base_value)

    def test_a_component_with_no_cited_base_metal_totals_the_precious_figure(self):
        result = valuation_module.value({"CPU": 1}, mass=MEASURED_CPU, prices=priced(), now=NOW)
        assert result.base_value is None
        assert result.total_value == pytest.approx(0.471)

    def test_the_record_carries_every_field_needed_to_audit_it(self):
        record = valuation_module.value(
            {"CPU": 1}, mass=MEASURED_CPU, item_id="AUR-1", prices=priced(), now=NOW
        ).as_dict()
        for field in (
            "item_id",
            "component_class",
            "weight_g",
            "weight_status",
            "metals",
            "metal_amounts",
            "prices",
            "price_status",
            "price_timestamps",
            "pmdi",
            "currency",
            "total_value",
            "evidence_sources",
            "evidence_status",
            "overall_status",
        ):
            assert field in record, field

    def test_evidence_ids_survive_to_the_output(self):
        record = valuation_module.value(
            {"CPU": 1}, mass=MEASURED_CPU, prices=priced(), now=NOW
        ).as_dict()
        assert "CPU-AU-001" in record["evidence_sources"]

    def test_the_component_class_is_named_for_a_single_class_batch(self):
        result = valuation_module.value({"CPU": 2}, mass=MEASURED_CPU, prices=priced(), now=NOW)
        assert result.component_class == "CPU"

    def test_a_mixed_batch_has_no_single_component_class(self):
        result = valuation_module.value(
            {"CPU": 1, "Connector": 1}, mass=MEASURED_CPU, prices=priced(), now=NOW
        )
        assert result.component_class is None

    def test_mixed_currencies_are_refused(self):
        """Adding USD to EUR produces a number that looks like money."""
        table = {
            "gold": {"price_per_unit": 100.0, "unit": "g", "currency": "USD"},
            "silver": {"price_per_unit": 1.0, "unit": "g", "currency": "EUR"},
            "palladium": {"price_per_unit": 40.0, "unit": "g", "currency": "USD"},
        }
        service = PriceService(StaticProvider(table), max_age_seconds=900)
        result = pmdi_module.compute({"PCB": 1}, mass=MEASURED_PCB, prices=service, now=NOW)
        assert result.pmdi_value is None
        assert result.price_status is PriceStatus.ERROR


class TestStatusPropagation:
    def test_a_simulated_mass_keeps_the_result_simulated(self):
        """Connector evidence is per-piece, so a simulated mass does not block
        the estimate; it must still be visible in the output."""
        result = valuation_module.value(
            {"Connector": 1}, mass=SIMULATED_PCB, prices=priced(), now=NOW
        )
        assert result.weight_status is MassStatus.SIMULATED
        assert result.overall_status is OverallStatus.SIMULATED
        assert result.as_dict()["weight_status"] == "SIMULATED"

    def test_an_unmeasured_mass_is_reported_as_unmeasured(self):
        result = valuation_module.value({"Connector": 1}, mass=None, prices=priced(), now=NOW)
        assert result.weight_status is MassStatus.UNMEASURED

    def test_missing_evidence_makes_the_whole_result_unavailable(self):
        result = valuation_module.value({"RAM": 1}, mass=MEASURED_PCB, prices=priced(), now=NOW)
        assert result.overall_status is OverallStatus.UNAVAILABLE
        assert result.evidence_status is EvidenceStatus.MISSING

    def test_a_simulated_mass_outranks_a_stale_price(self):
        """The worst thing true of any input is what the record reports."""
        result = valuation_module.value(
            {"Connector": 1}, mass=SIMULATED_PCB, prices=priced(ago=1800), now=NOW
        )
        assert result.overall_status is OverallStatus.SIMULATED

    def test_a_stale_price_alone_reports_stale(self):
        result = valuation_module.value(
            {"CPU": 1}, mass=MEASURED_CPU, prices=priced(ago=1800), now=NOW
        )
        assert result.overall_status is OverallStatus.STALE


class TestNoDecisionLogicHere:
    def test_the_valuation_layer_never_produces_a_grade(self):
        """A/B/C is policy and belongs to app.decision, not to PMDI."""
        record = valuation_module.value(
            {"CPU": 1}, mass=MEASURED_CPU, prices=priced(), now=NOW
        ).as_dict()
        blob = str(record).lower()
        for word in ("grade", "bin_a", "bin_b", "servo", "target_bin"):
            assert word not in blob


class TestPrecisionAndLabelling:
    def test_intermediate_values_are_not_pre_rounded(self):
        """Rounding happens at as_dict(), not in the arithmetic."""
        result = pmdi_module.compute({"Connector": 1}, prices=unpriced(), now=NOW)
        assert result.precious["Au"].grams == 0.000914
        assert result.precious_mass_g == 0.000914

    def test_output_rounding_is_caller_controlled(self):
        result = pmdi_module.compute({"Connector": 1}, prices=unpriced(), now=NOW)
        assert result.as_dict(digits=4)["precious_mass_g"] == 0.0009
        assert result.as_dict(digits=9)["precious_mass_g"] == 0.000914

    def test_every_amount_is_labelled_contained_not_yield(self):
        """Contained composition is not recovery yield and must not be renamed."""
        result = pmdi_module.compute({"PCB": 1}, mass=MEASURED_PCB, prices=unpriced(), now=NOW)
        for amount in {**result.precious, **result.base}.values():
            assert amount.basis == "contained"
        assert "not a recovery yield" in result.as_dict()["disclaimer"]

    def test_the_formula_is_stated_in_the_output(self):
        record = pmdi_module.compute({"CPU": 1}, mass=MEASURED_CPU, now=NOW).as_dict()
        assert record["formula"] == "PMDI = (sum(C_type x Y_estimated)) x P_spot"


class TestFractionDenominatorRegression:
    """Pins down exactly what `precious_mass_fraction_ppm` divides by.

    A live ledger record showed 2.56 ppm for one CPU while an audit example had
    said 110 ppm. Both are right: the fraction divides by *the mass that was
    weighed*, and those two cases weighed different things. The audit's 42.7 g
    was an illustrative CPU mass; the ledger record carries 1840 g, the base
    value of the SIMULATED load cell standing in for whatever sat on the scale.

    These tests exist so that denominator can never drift silently.
    """

    CPU_AU_G = 0.00471  # CPU-AU-001: 4.71 mg per piece

    def test_the_denominator_is_the_weighed_mass(self):
        result = pmdi_module.compute({"CPU": 1}, mass={"grams": 42.7, "simulated": False})
        assert result.precious_mass_g == self.CPU_AU_G
        assert result.mass_g == 42.7
        assert result.precious_mass_fraction_ppm == pytest.approx(self.CPU_AU_G / 42.7 * 1e6)
        assert result.precious_mass_fraction_ppm == pytest.approx(110.3044, abs=1e-4)

    def test_the_same_component_on_a_heavier_scale_reading_gives_a_smaller_fraction(self):
        """1840 g is the simulated load cell's base, not a CPU's mass."""
        result = pmdi_module.compute({"CPU": 1}, mass={"grams": 1840.0, "simulated": False})
        assert result.precious_mass_g == self.CPU_AU_G
        assert result.precious_mass_fraction_ppm == pytest.approx(2.5598, abs=1e-4)

    def test_no_reference_mass_from_the_database_leaks_into_the_denominator(self):
        """PCB-MASS-001 is 1800 g and RAM-MASS-001 is 7.804 g. Neither is a
        denominator: only the weighed mass is."""
        for grams in (10.0, 250.0, 1800.0):
            result = pmdi_module.compute({"CPU": 1}, mass={"grams": grams, "simulated": False})
            assert result.mass_g == grams
            assert result.precious_mass_fraction_ppm == pytest.approx(self.CPU_AU_G / grams * 1e6)

    def test_ppm_is_parts_per_million_by_mass(self):
        """A gram of precious metal in a kilogram is 1000 ppm, by definition."""
        result = pmdi_module.compute(
            {"PCB": 1}, mass={"grams": 1000.0, "simulated": False}, prices=unpriced(), now=NOW
        )
        expected = result.precious_mass_g / 1000.0 * 1e6
        assert result.precious_mass_fraction_ppm == pytest.approx(expected)
        assert result.precious_mass_fraction_ppm == pytest.approx(2200.0)

    def test_milligram_evidence_is_converted_to_grams_before_dividing(self):
        """4.71 mg must not be divided as if it were 4.71 g."""
        result = pmdi_module.compute({"CPU": 1}, mass={"grams": 4710.0, "simulated": False})
        assert result.precious_mass_fraction_ppm == pytest.approx(1.0)
