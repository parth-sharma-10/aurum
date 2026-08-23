"""Tests for the price layer.

The property under test throughout: a price is either a real figure with a
provenance, or an explicit refusal. There is no third state, and no path
through this module produces a number nobody can source.

Every price in this file is TEST data. None of it is a market quote.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import config
from app.valuation.prices import (
    GRAMS_PER_UNIT,
    MetalPrice,
    PriceService,
    PriceStatus,
    StaticProvider,
    UnavailableProvider,
    to_grams,
)

NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


def stamp(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def fixture_prices(unit: str = "g", ago: float = 60.0) -> dict:
    """Deterministic TEST fixture values. Not market data."""
    return {
        "gold": {
            "price_per_unit": 100.0,
            "unit": unit,
            "currency": "USD",
            "timestamp": stamp(ago),
        },
        "silver": {
            "price_per_unit": 1.0,
            "unit": unit,
            "currency": "USD",
            "timestamp": stamp(ago),
        },
        "palladium": {
            "price_per_unit": 40.0,
            "unit": unit,
            "currency": "USD",
            "timestamp": stamp(ago),
        },
    }


class TestUnitConversion:
    def test_per_gram_is_unchanged(self):
        assert to_grams(100.0, "g") == 100.0

    def test_per_kilogram_converts_down(self):
        assert to_grams(1000.0, "kg") == 1.0

    def test_troy_ounce_uses_the_exact_definition(self):
        """A troy ounce is exactly 31.1034768 g; a 31x error looks plausible."""
        assert to_grams(31.1034768, "ozt") == pytest.approx(1.0)
        assert GRAMS_PER_UNIT["ozt"] == 31.1034768

    def test_an_unknown_unit_raises_rather_than_guessing(self):
        with pytest.raises(ValueError, match="No gram conversion"):
            to_grams(100.0, "lb")

    def test_the_error_lists_the_units_it_does_know(self):
        with pytest.raises(ValueError, match="g, kg, ozt"):
            to_grams(100.0, "tola")


class TestUnavailableProvider:
    def test_it_prices_nothing(self):
        quote = UnavailableProvider().quote("Au", "gold")
        assert quote.status is PriceStatus.UNAVAILABLE
        assert quote.price_per_gram is None
        assert not quote.has_number
        assert not quote.is_current

    def test_the_reason_names_the_setting_that_would_change_it(self):
        assert "AURUM_PRICE_PROVIDER" in UnavailableProvider().quote("Au", "gold").reason

    def test_it_refuses_when_it_is_the_configured_provider(self):
        """Selecting it must refuse, not invent."""
        service = PriceService.from_config(
            config.load(environ={"AURUM_PRICE_PROVIDER": "unavailable"})
        )
        quote = service.price("Au", now=NOW)
        assert quote.status is PriceStatus.UNAVAILABLE
        assert quote.price_per_gram is None


class TestStaticProvider:
    def test_a_quote_is_labelled_test_never_live(self):
        quote = StaticProvider(fixture_prices()).quote("Au", "gold")
        assert quote.status is PriceStatus.TEST
        assert quote.status is not PriceStatus.LIVE
        assert "not a live market quote" in quote.reason

    def test_it_converts_the_quoted_unit_to_grams(self):
        quote = StaticProvider(fixture_prices(unit="kg")).quote("Au", "gold")
        assert quote.price_per_gram == pytest.approx(0.1)
        assert quote.quoted_price == 100.0
        assert quote.quoted_unit == "kg"

    def test_it_keeps_the_quote_as_given_alongside_the_conversion(self):
        quote = StaticProvider(fixture_prices(unit="ozt")).quote("Au", "gold")
        assert quote.quoted_unit == "ozt"
        assert quote.quoted_price == 100.0
        assert quote.price_per_gram == pytest.approx(100.0 / 31.1034768)

    def test_an_unpriced_metal_is_unavailable_not_zero(self):
        quote = StaticProvider(fixture_prices()).quote("Cu", "copper")
        assert quote.status is PriceStatus.UNAVAILABLE
        assert quote.price_per_gram is None

    def test_an_unconvertible_unit_is_an_error(self):
        prices = {"gold": {"price_per_unit": 1.0, "unit": "barrel", "currency": "USD"}}
        assert StaticProvider(prices).quote("Au", "gold").status is PriceStatus.ERROR

    def test_it_never_labels_a_price_as_anything_but_test(self):
        """Whatever the file holds, this provider is for tests only.

        It reads the same file the reference provider does, so the guard that
        matters is the label: a StaticProvider quote must never claim to be a
        REFERENCE or a LIVE price.
        """
        quote = StaticProvider.from_config().quote("Au", "gold")
        assert quote.status in (PriceStatus.TEST, PriceStatus.UNAVAILABLE)

    def test_a_missing_file_prices_nothing(self, tmp_path):
        provider = StaticProvider.from_config(tmp_path / "absent.yaml")
        assert provider.quote("Au", "gold").status is PriceStatus.UNAVAILABLE


class TestStaleness:
    def test_a_fresh_quote_keeps_its_status(self):
        service = PriceService(StaticProvider(fixture_prices(ago=60)), max_age_seconds=900)
        assert service.price("Au", now=NOW).status is PriceStatus.TEST

    def test_an_old_quote_becomes_stale(self):
        service = PriceService(StaticProvider(fixture_prices(ago=1800)), max_age_seconds=900)
        assert service.price("Au", now=NOW).status is PriceStatus.STALE

    def test_stale_keeps_its_number_so_a_caller_can_decide(self):
        """Staleness is reported, not enforced. The grading policy decides."""
        service = PriceService(StaticProvider(fixture_prices(ago=1800)), max_age_seconds=900)
        quote = service.price("Au", now=NOW)
        assert quote.price_per_gram == 100.0
        assert quote.has_number
        assert not quote.is_current

    def test_the_boundary_is_not_stale(self):
        service = PriceService(StaticProvider(fixture_prices(ago=900)), max_age_seconds=900)
        assert service.price("Au", now=NOW).status is PriceStatus.TEST

    def test_one_second_past_the_boundary_is_stale(self):
        service = PriceService(StaticProvider(fixture_prices(ago=901)), max_age_seconds=900)
        assert service.price("Au", now=NOW).status is PriceStatus.STALE

    def test_the_reason_reports_the_age_and_the_limit(self):
        service = PriceService(StaticProvider(fixture_prices(ago=1800)), max_age_seconds=900)
        assert "900s limit" in service.price("Au", now=NOW).reason

    def test_age_is_recorded(self):
        service = PriceService(StaticProvider(fixture_prices(ago=300)), max_age_seconds=900)
        assert service.price("Au", now=NOW).age_seconds == pytest.approx(300)

    def test_a_quote_with_no_timestamp_cannot_be_aged(self):
        prices = {"gold": {"price_per_unit": 1.0, "unit": "g", "currency": "USD"}}
        service = PriceService(StaticProvider(prices), max_age_seconds=900)
        quote = service.price("Au", now=NOW)
        assert quote.age_seconds is None
        assert quote.status is PriceStatus.TEST

    def test_an_unparseable_timestamp_does_not_crash_the_pipeline(self):
        prices = {"gold": {"price_per_unit": 1.0, "unit": "g", "timestamp": "last tuesday"}}
        assert PriceService(StaticProvider(prices)).price("Au", now=NOW).age_seconds is None


class TestPriceService:
    def test_it_resolves_the_material_name_from_the_metal_symbol(self):
        """The evidence layer says Au; a price file says gold."""
        assert PriceService(StaticProvider(fixture_prices())).price("Au").material == "gold"

    def test_several_metals_at_once(self):
        service = PriceService(StaticProvider(fixture_prices()))
        quotes = service.prices(["Au", "Ag", "Pd"], now=NOW)
        assert set(quotes) == {"Au", "Ag", "Pd"}
        assert all(q.has_number for q in quotes.values())

    def test_it_is_built_from_configuration(self, tmp_path):
        (tmp_path / "pricing.yaml").write_text("pricing:\n  provider: static\n")
        cfg = config.load(tmp_path, environ={})
        assert isinstance(PriceService.from_config(cfg).provider, StaticProvider)

    def test_the_configured_staleness_limit_is_used(self, tmp_path):
        (tmp_path / "pricing.yaml").write_text("pricing:\n  max_age_seconds: 60\n")
        cfg = config.load(tmp_path, environ={})
        assert PriceService.from_config(cfg).max_age_seconds == 60.0


class TestSerialization:
    def test_a_quote_carries_everything_needed_to_audit_it(self):
        quote = PriceService(StaticProvider(fixture_prices())).price("Au", now=NOW).as_dict()
        for field in (
            "metal",
            "material",
            "status",
            "price_per_gram",
            "currency",
            "quoted_price",
            "quoted_unit",
            "timestamp",
            "provider",
            "age_seconds",
            "reason",
        ):
            assert field in quote

    def test_status_serializes_as_a_plain_string(self):
        assert MetalPrice("Au", "gold", PriceStatus.TEST).as_dict()["status"] == "TEST"
