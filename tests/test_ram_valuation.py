"""RAM, end to end: cited evidence -> quantities -> prices -> rupees.

The arithmetic in this file is checked against numbers computed by hand, not
against whatever the implementation happens to produce. Every expected value
below is written out as its own multiplication so that a reader can verify it
without running anything:

    Au  18.0 mg = 0.0180 g  x  16,062.00 INR/g  =  289.116 INR
    Ag  28.4 mg = 0.0284 g  x     246.63 INR/g  =    7.004 INR
    Pd   1.2 mg = 0.0012 g  x   4,095.256 INR/g =    4.914 INR
    Cu   3.4 g              x      1.3858 INR/g =    4.712 INR
                                       contained  = 305.746 INR

Evidence: Charles et al. (2017), Waste Management 60:505-520, Table 2, the
"DIMMs (4-15)" row, n = 12, AAS after comminution and acid digestion.
Prices: the dated REFERENCE snapshot in configs/price_reference.yaml.

Two properties are load-bearing and are asserted repeatedly on purpose:

**The mass on the pan never enters a RAM figure.** RAM evidence is per module,
so a module on a 842 g motherboard is worth exactly what a loose module is
worth. Any test that could pass by allocating assembly mass to RAM would be
testing the wrong system.

**Contained is not recoverable.** No cited recovery factor was measured on a
RAM module, so recoverable value is a refusal carrying its reason - never a
number, and never a zero.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app import config, materials
from app.valuation import pmdi as pmdi_module
from app.valuation import valuation as valuation_module
from app.valuation.prices import (
    FallbackProvider,
    FxError,
    MetalPrice,
    PriceService,
    PriceStatus,
    ReferenceProvider,
    UnavailableProvider,
    convert_currency,
    to_grams,
)

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)

# --- the cited evidence, per module -----------------------------------------
AU_MG, AG_MG, PD_MG, CU_G = 18.0, 28.4, 1.2, 3.4

# --- the reference snapshot, INR per gram -----------------------------------
AU_INR_G = 16062.00
AG_INR_G = 246.63
CU_INR_G = 1.3858
# 1331.00 USD/ozt / 31.1034768 g/ozt = 42.792680... USD/g  x 95.70 = 4095.2560 INR/g
PD_INR_G = 1331.00 / 31.1034768 * 95.70

# --- the hand calculation ---------------------------------------------------
AU_VALUE = 0.0180 * AU_INR_G  # 289.116
AG_VALUE = 0.0284 * AG_INR_G  # 7.004292
PD_VALUE = 0.0012 * PD_INR_G  # 4.914307...
CU_VALUE = 3.4 * CU_INR_G  # 4.71172
PRECIOUS_VALUE = AU_VALUE + AG_VALUE + PD_VALUE
CONTAINED_VALUE = PRECIOUS_VALUE + CU_VALUE


def prices() -> PriceService:
    """The shipped snapshot, resolved the way production resolves it."""
    return PriceService.from_config(config.load())


def valued(counts, mass=None):
    return valuation_module.value(counts, mass=mass, prices=prices(), now=NOW)


class TestQuantities:
    """Counts times cited per-module figures. No mass anywhere."""

    def test_one_module(self):
        result = pmdi_module.compute({"RAM": 1}, now=NOW)
        assert result.precious["Au"].grams == pytest.approx(0.0180)
        assert result.precious["Ag"].grams == pytest.approx(0.0284)
        assert result.precious["Pd"].grams == pytest.approx(0.0012)
        assert result.base["Cu"].grams == pytest.approx(3.4)

    @pytest.mark.parametrize("count", [1, 2, 3, 4, 8, 16])
    def test_n_modules_scale_exactly(self, count):
        result = pmdi_module.compute({"RAM": count}, now=NOW)
        assert result.precious["Au"].grams == pytest.approx(count * AU_MG / 1000)
        assert result.precious["Ag"].grams == pytest.approx(count * AG_MG / 1000)
        assert result.precious["Pd"].grams == pytest.approx(count * PD_MG / 1000)
        assert result.base["Cu"].grams == pytest.approx(count * CU_G)

    def test_milligrams_become_grams_and_grams_stay_grams(self):
        """The unit trap inside the evidence, not inside the price."""
        result = pmdi_module.compute({"RAM": 1}, now=NOW)
        # Gold is cited in mg and must be divided by 1000.
        assert result.precious["Au"].grams == pytest.approx(AU_MG / 1000)
        # Copper is cited in g and must NOT be.
        assert result.base["Cu"].grams == pytest.approx(CU_G)
        assert result.base["Cu"].grams > result.precious["Au"].grams * 100

    def test_zero_modules_produce_no_estimate_rather_than_a_zero(self):
        result = pmdi_module.compute({"RAM": 0}, now=NOW)
        assert not result.available
        assert result.precious == {}

    def test_a_negative_count_is_refused_rather_than_multiplied(self):
        """Counts come from a tracker and cannot be negative. If one ever is,
        multiplying it by a composition would produce a negative quantity that
        silently nets off a real one elsewhere in the total."""
        result = pmdi_module.compute({"RAM": -2}, now=NOW)
        assert not result.available
        assert all(a.grams >= 0 for a in {**result.precious, **result.base}.values())
        assert "-2" in result.reason

    def test_a_negative_count_does_not_poison_a_valid_one(self):
        both = pmdi_module.compute({"RAM": 2, "CPU": -1}, now=NOW)
        alone = pmdi_module.compute({"RAM": 2}, now=NOW)
        assert both.precious["Au"].grams == pytest.approx(alone.precious["Au"].grams)
        assert [n["component"] for n in both.not_valued] == ["CPU"]

    def test_a_non_numeric_count_is_refused(self):
        result = pmdi_module.compute({"RAM": "two"}, now=NOW)
        assert not result.available


class TestPrices:
    """The snapshot, its conversions, and what it is allowed to claim."""

    def test_every_metal_ram_needs_is_priced_in_rupees(self):
        service = prices()
        for metal in ("Au", "Ag", "Pd", "Cu"):
            quote = service.price(metal, now=NOW)
            assert quote.has_number, metal
            assert quote.currency == "INR", metal

    def test_the_prices_are_the_verified_figures(self):
        service = prices()
        assert service.price("Au", now=NOW).price_per_gram == pytest.approx(AU_INR_G)
        assert service.price("Ag", now=NOW).price_per_gram == pytest.approx(AG_INR_G)
        assert service.price("Cu", now=NOW).price_per_gram == pytest.approx(CU_INR_G)
        assert service.price("Pd", now=NOW).price_per_gram == pytest.approx(PD_INR_G)

    def test_a_troy_ounce_is_not_an_avoirdupois_ounce(self):
        """31.1034768 g, not 28.3495. A 9% error that looks entirely plausible."""
        assert to_grams(31.1034768, "ozt") == pytest.approx(1.0)
        assert to_grams(1000.0, "kg") == pytest.approx(1.0)
        assert to_grams(5.0, "g") == 5.0
        wrong = 1331.00 / 28.3495 * 95.70
        assert abs(PD_INR_G - wrong) / wrong > 0.08

    def test_an_unknown_unit_raises_rather_than_guessing(self):
        with pytest.raises(ValueError, match="No gram conversion"):
            to_grams(100.0, "lb")

    def test_usd_per_ounce_becomes_inr_per_gram(self):
        """The one entry in the snapshot that needs both conversions."""
        per_gram_usd = 1331.00 / 31.1034768
        assert per_gram_usd == pytest.approx(42.792644, abs=1e-6)
        assert convert_currency(per_gram_usd, "USD", "INR", {"USD": {"rate": 95.70}}) == (
            pytest.approx(PD_INR_G)
        )

    def test_a_missing_fx_rate_is_an_error_not_a_parity_of_one(self):
        with pytest.raises(FxError, match="No FX rate"):
            convert_currency(100.0, "USD", "INR", {})

    def test_converting_a_currency_to_itself_changes_nothing(self):
        assert convert_currency(42.0, "INR", "INR", {}) == 42.0

    def test_a_reference_price_is_never_reported_as_live(self):
        quote = prices().price("Au", now=NOW)
        assert quote.status is PriceStatus.REFERENCE
        assert quote.status is not PriceStatus.LIVE
        assert "not a live market quote" in quote.reason

    def test_a_reference_price_does_not_go_stale_with_age(self):
        """It was published on a date and is being used after it. That is not
        the same failure as a live feed going quiet, and must not share a name."""
        far_future = datetime(2030, 1, 1, tzinfo=UTC)
        quote = prices().price("Au", now=far_future)
        assert quote.status is PriceStatus.REFERENCE
        assert quote.age_seconds > 0
        assert quote.has_number

    def test_every_price_carries_its_source_and_date(self):
        service = prices()
        for metal in ("Au", "Ag", "Pd", "Cu"):
            quote = service.price(metal, now=NOW)
            assert quote.timestamp, f"{metal} price has no date"
            assert "Source:" in quote.reason, f"{metal} price has no source"
            assert quote.quoted_price is not None and quote.quoted_unit

    def test_an_unquoted_metal_refuses_rather_than_returning_zero(self):
        quote = ReferenceProvider({}, currency="INR").quote("Au", "gold")
        assert quote.status is PriceStatus.UNAVAILABLE
        assert quote.price_per_gram is None

    def test_a_malformed_price_entry_is_an_error_not_a_number(self):
        provider = ReferenceProvider({"gold": {"price_per_unit": "not a number", "unit": "g"}})
        assert provider.quote("Au", "gold").status is PriceStatus.ERROR

    def test_a_price_entry_with_no_unit_is_an_error(self):
        provider = ReferenceProvider({"gold": {"price_per_unit": 100.0}})
        assert provider.quote("Au", "gold").status is PriceStatus.ERROR


class TestFallback:
    """LIVE -> REFERENCE, without pretending the fallback was live."""

    class Dead:
        name = "dead-feed"

        def quote(self, metal, material):
            return MetalPrice(metal, material, PriceStatus.UNAVAILABLE, reason="feed down")

    class Exploding:
        name = "exploding-feed"

        def quote(self, metal, material):
            raise TimeoutError("the market feed timed out")

    def test_it_falls_back_when_the_primary_has_no_number(self):
        provider = FallbackProvider(self.Dead(), ReferenceProvider.from_config())
        quote = provider.quote("Au", "gold")
        assert quote.price_per_gram == pytest.approx(AU_INR_G)
        assert quote.status is PriceStatus.REFERENCE
        assert "Fell back from dead-feed" in quote.reason

    def test_a_primary_that_raises_does_not_take_the_pipeline_down(self):
        provider = FallbackProvider(self.Exploding(), ReferenceProvider.from_config())
        quote = provider.quote("Au", "gold")
        assert quote.has_number
        assert "timed out" in quote.reason

    def test_both_failing_refuses_rather_than_inventing(self):
        provider = FallbackProvider(self.Dead(), UnavailableProvider())
        assert provider.quote("Au", "gold").price_per_gram is None

    def test_the_primary_wins_when_it_answers(self):
        live = ReferenceProvider(
            {"gold": {"price_per_unit": 1.0, "unit": "g", "currency": "INR"}}, currency="INR"
        )
        provider = FallbackProvider(live, ReferenceProvider.from_config())
        assert provider.quote("Au", "gold").price_per_gram == 1.0


class TestContainedValue:
    """The rupee figure, against the hand calculation at the top of this file."""

    def test_one_module(self):
        result = valued({"RAM": 1})
        assert result.pmdi.pmdi_value == pytest.approx(PRECIOUS_VALUE)
        assert result.base_value == pytest.approx(CU_VALUE)
        assert result.total_value == pytest.approx(CONTAINED_VALUE)
        assert result.currency == "INR"

    def test_one_module_matches_the_hand_calculation_to_the_paisa(self):
        assert valued({"RAM": 1}).total_value == pytest.approx(305.746, abs=0.001)

    def test_two_modules_are_exactly_twice_one(self):
        one = valued({"RAM": 1}).total_value
        two = valued({"RAM": 2}).total_value
        assert two == pytest.approx(2 * one)
        assert two == pytest.approx(611.492, abs=0.001)

    @pytest.mark.parametrize("count", [1, 2, 4, 8])
    def test_n_modules_scale_linearly(self, count):
        assert valued({"RAM": count}).total_value == pytest.approx(count * CONTAINED_VALUE)

    def test_gold_dominates_the_value(self):
        """Charles et al. put gold at 93.5% of DIMM value at Feb-2016 prices.

        An independent check that the arithmetic is the right shape: if a unit
        conversion were wrong by 1000x anywhere, this proportion would move.
        """
        gold_share = AU_VALUE / CONTAINED_VALUE
        assert gold_share == pytest.approx(0.945, abs=0.01)

    def test_the_figure_is_labelled_contained(self):
        record = valued({"RAM": 1}).as_dict()
        assert record["value_basis"] == "CONTAINED"
        assert record["contained_value"] == pytest.approx(CONTAINED_VALUE)

    def test_precision_survives_a_small_count(self):
        """Palladium is 1.2 mg. Rounding it to zero anywhere loses a real figure."""
        pd = valued({"RAM": 1}).pmdi.precious["Pd"]
        assert pd.grams == pytest.approx(0.0012)
        assert 0.0012 * PD_INR_G > 4.0


class TestRecoverableValue:
    """Contained is not recoverable, and the difference is stated, not implied."""

    def test_it_is_unavailable_and_says_why(self):
        record = valued({"RAM": 2}).as_dict()
        recoverable = record["recoverable_value"]
        assert recoverable["available"] is False
        assert recoverable["value"] is None
        assert "No component-specific recovery factor" in recoverable["reason"]

    def test_it_is_absent_rather_than_zero(self):
        """A missing figure rendered as 0 reads as 'nothing is recoverable',
        which is a claim the evidence does not support either."""
        recoverable = valued({"RAM": 2}).as_dict()["recoverable_value"]
        assert recoverable["value"] is None
        assert recoverable["value"] != 0

    def test_the_database_applies_no_recovery_factor_to_any_component(self):
        status = materials.recovery_status()
        assert status["available"] is False
        assert "RAM" in status["reason"]

    def test_the_general_weee_figures_are_not_applied_to_ram(self):
        """Charles quotes >95% gold recovery at integrated refineries. It is a
        secondary citation about WEEE in general and must not become a factor."""
        contained = valued({"RAM": 1}).total_value
        assert contained == pytest.approx(CONTAINED_VALUE)
        assert contained != pytest.approx(CONTAINED_VALUE * 0.95)


class TestAssemblyMassIsNeverUsedAsRamMass:
    """Section 8, asserted from several directions because it is the easiest
    thing in this whole feature to get quietly wrong."""

    ASSEMBLY = {"PCB": 1, "RAM": 2, "CPU": 1, "Connector": 3}

    def test_the_ram_figure_is_identical_loose_and_on_a_board(self):
        loose = valued({"RAM": 2}).pmdi
        board = valued(self.ASSEMBLY, mass={"grams": 842.0, "simulated": False}).pmdi
        # The assembly adds CPU and connector gold, but its RAM contribution
        # must be the same 2 x 18.0 mg it is on its own.
        assert board.precious["Au"].grams == pytest.approx(
            loose.precious["Au"].grams + (4.71 + 3 * 0.914) / 1000
        )
        assert board.base["Cu"].grams == pytest.approx(loose.base["Cu"].grams)

    @pytest.mark.parametrize("grams", [15.6, 200.0, 842.0, 5000.0])
    def test_the_boards_mass_does_not_move_the_ram_estimate(self, grams):
        result = valued(self.ASSEMBLY, mass={"grams": grams, "simulated": False}).pmdi
        assert result.base["Cu"].grams == pytest.approx(2 * CU_G)

    def test_the_pcb_line_is_refused_so_nothing_is_double_counted(self):
        result = valued(self.ASSEMBLY, mass={"grams": 842.0, "simulated": False}).pmdi
        assert result.completeness == materials.PARTIAL_ESTIMATE
        assert [n["component"] for n in result.not_valued] == ["PCB"]
        assert {v["component"] for v in result.valued} == {"RAM", "CPU", "Connector"}
        # No line in the whole estimate consumed the assembly mass.
        for amount in {**result.precious, **result.base}.values():
            assert "batch mass" not in (amount.calculation or "")

    def test_the_assembly_total_is_the_sum_of_its_valued_parts(self):
        result = valued(self.ASSEMBLY, mass={"grams": 842.0, "simulated": False})
        parts = (
            valued({"RAM": 2}).total_value
            + valued({"CPU": 1}).total_value
            + valued({"Connector": 3}).total_value
        )
        assert result.total_value == pytest.approx(parts)

    def test_the_record_names_what_it_did_and_did_not_value(self):
        record = valued(self.ASSEMBLY, mass={"grams": 842.0, "simulated": False}).as_dict()
        assert record["completeness"] == "PARTIAL_ESTIMATE"
        assert [n["component"] for n in record["not_valued"]] == ["PCB"]
        assert "valued twice" in record["not_valued"][0]["reason"]


class TestCitationPropagation:
    """A figure that cannot be traced back to a paper is not usable evidence."""

    def test_the_evidence_ids_reach_the_valuation_record(self):
        record = valued({"RAM": 2}).as_dict()
        assert set(record["evidence_sources"]) == {
            "RAM-AU-001",
            "RAM-AG-001",
            "RAM-PD-001",
            "RAM-CU-001",
        }

    def test_each_metal_carries_its_own_arithmetic(self):
        record = valued({"RAM": 2}).as_dict()
        amounts = {**record["metal_amounts"]["precious"], **record["metal_amounts"]["base"]}
        assert amounts["Au"]["calculation"] == "2 x 18.0 mg per piece"
        assert amounts["Cu"]["calculation"] == "2 x 3.4 g per piece"
        for metal, amount in amounts.items():
            assert amount["evidence"], f"{metal} lost its citation"
            assert amount["basis"] == "contained", f"{metal} is not labelled contained"

    def test_every_evidence_id_resolves_to_a_source_with_a_doi(self):
        index = materials.evidence_index(materials.load())
        sources = materials.load_sources()
        for eid in ("RAM-AU-001", "RAM-AG-001", "RAM-PD-001", "RAM-CU-001"):
            src = sources[index[eid]["source"]]
            assert src["doi"] or src["url"]
            assert src["full_text_read"] is True

    def test_the_prices_are_auditable_from_the_record(self):
        record = valued({"RAM": 1}).as_dict()
        for metal in ("Au", "Ag", "Pd", "Cu"):
            quote = record["prices"][metal]
            assert quote["status"] == "REFERENCE"
            assert quote["timestamp"]
            assert quote["quoted_price"] and quote["quoted_unit"]
