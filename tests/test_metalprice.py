"""Tests for the live MetalpriceAPI provider.

Every call is mocked. Nothing here reaches the network, needs a key, or
asserts a market price — the property under test is that a live feed either
produces a figure whose arithmetic and provenance are checkable, or refuses
in a way `FallbackProvider` and `PriceService` can act on.

The one number that is not a fixture is 31.1034768: a troy ounce is a
definition, and the whole point of this module is that it is applied once.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app import config
from app.valuation import metalprice
from app.valuation.metalprice import ENV_API_KEY, FetchError, MetalpriceProvider
from app.valuation.prices import (
    FallbackProvider,
    PriceService,
    PriceStatus,
    ReferenceProvider,
    build_provider,
)

#: A fixed market instant, so a test can decide what "now" means.
MARKET_TS = 1755792000
MARKET_ISO = datetime.fromtimestamp(MARKET_TS, tz=UTC).isoformat(timespec="seconds")

GRAMS_PER_OZT = 31.1034768

#: TEST values, not market quotes. Chosen so every conversion is checkable by
#: hand: 3110.34768 USD/ozt is exactly 100 USD/g.
RATES = {
    "XAU": 1.0 / 3110.34768,
    "USDXAU": 3110.34768,
    "XAG": 1.0 / 31.1034768,
    "USDXAG": 31.1034768,
    "XPD": 1.0 / 622.069536,
    "USDXPD": 622.069536,
    "INR": 80.0,
}


def payload(**overrides) -> dict:
    out = {"success": True, "base": "USD", "timestamp": MARKET_TS, "rates": dict(RATES)}
    out.update(overrides)
    return out


class Feed:
    """A stand-in for the network. Records every call it is asked to make."""

    def __init__(self, *responses):
        self.responses = list(responses) or [payload()]
        self.calls: list[str] = []

    def __call__(self, url: str, timeout_s: float):
        self.calls.append(url)
        item = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def provider(feed=None, clock=None, **kwargs) -> MetalpriceProvider:
    metalprice.reset_cache()
    kwargs.setdefault("api_key", "TEST-KEY-NOT-A-REAL-CREDENTIAL")
    return MetalpriceProvider(
        fetch=feed or Feed(),
        clock=clock or (lambda: 0.0),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _clean_cache():
    metalprice.reset_cache()
    yield
    metalprice.reset_cache()


class TestASuccessfulQuote:
    def test_gold_is_priced_per_gram_in_the_reporting_currency(self):
        quote = provider().quote("Au", "gold")
        assert quote.status is PriceStatus.LIVE
        assert quote.currency == "INR"
        # 3110.34768 USD/ozt -> 100 USD/g -> 8000 INR/g at 80.
        assert quote.price_per_gram == pytest.approx(8000.0)

    def test_silver_and_palladium_take_the_same_path(self):
        p = provider()
        assert p.quote("Ag", "silver").price_per_gram == pytest.approx(80.0)
        assert p.quote("Pd", "palladium").price_per_gram == pytest.approx(1600.0)

    def test_the_quote_keeps_the_figure_as_published(self):
        quote = provider().quote("Au", "gold")
        assert quote.quoted_unit == "ozt"
        assert quote.quoted_price == pytest.approx(3110.34768)

    def test_the_market_timestamp_is_carried_not_the_call_time(self):
        assert provider().quote("Au", "gold").timestamp == MARKET_ISO

    def test_the_troy_ounce_not_the_avoirdupois_one_is_applied(self):
        """28.3495 g would put this 9.7% out. The gram figure says which was used."""
        quote = provider(currency="USD").quote("Au", "gold")
        assert quote.price_per_gram == pytest.approx(3110.34768 / GRAMS_PER_OZT)

    def test_a_forward_rate_alone_is_still_usable(self):
        """A response carrying only XAU and not USDXAU inverts it rather than failing."""
        rates = {k: v for k, v in RATES.items() if not k.startswith("USD")}
        quote = provider(Feed(payload(rates=rates))).quote("Au", "gold")
        assert quote.status is PriceStatus.LIVE
        assert quote.price_per_gram == pytest.approx(8000.0)


class TestCurrencyConversion:
    def test_no_conversion_happens_when_the_base_is_the_reporting_currency(self):
        quote = provider(currency="USD").quote("Au", "gold")
        assert quote.currency == "USD"
        assert quote.price_per_gram == pytest.approx(100.0)

    def test_the_fx_leg_rides_the_same_request(self):
        feed = Feed()
        provider(feed).quote("Au", "gold")
        assert "INR" in feed.calls[0]

    def test_a_missing_fx_rate_is_an_error_not_an_assumed_parity(self):
        rates = {k: v for k, v in RATES.items() if k != "INR"}
        quote = provider(Feed(payload(rates=rates))).quote("Au", "gold")
        assert quote.status is PriceStatus.ERROR
        assert quote.price_per_gram is None
        assert "No parity is assumed" in quote.reason

    def test_the_conversion_is_recorded_on_the_quote(self):
        assert "USD/INR 80.0" in provider().quote("Au", "gold").reason


class TestTheCache:
    def test_three_metals_cost_one_request(self):
        feed = Feed()
        p = provider(feed)
        for metal in ("Au", "Ag", "Pd"):
            p.quote(metal, metal.lower())
        assert len(feed.calls) == 1

    def test_a_second_provider_reuses_the_first_one_s_snapshot(self):
        """PriceService builds a provider per item; the quota is per account."""
        feed = Feed()
        provider(feed).quote("Au", "gold")
        second = MetalpriceProvider(api_key="TEST-KEY", fetch=feed, clock=lambda: 1.0)
        second.quote("Au", "gold")
        assert len(feed.calls) == 1

    def test_the_cache_expires(self):
        feed = Feed()
        now = [0.0]
        p = provider(feed, clock=lambda: now[0], cache_seconds=900.0)
        p.quote("Au", "gold")
        now[0] = 901.0
        p.quote("Au", "gold")
        assert len(feed.calls) == 2

    def test_it_does_not_expire_early(self):
        feed = Feed()
        now = [0.0]
        p = provider(feed, clock=lambda: now[0], cache_seconds=900.0)
        p.quote("Au", "gold")
        now[0] = 899.0
        p.quote("Au", "gold")
        assert len(feed.calls) == 1


class TestFailure:
    """No failure path may produce a number, and none may produce a zero."""

    def test_a_missing_key_is_named_not_guessed_around(self):
        quote = MetalpriceProvider(api_key=None, fetch=Feed()).quote("Au", "gold")
        assert quote.status is PriceStatus.UNAVAILABLE
        assert quote.price_per_gram is None
        assert ENV_API_KEY in quote.reason

    def test_an_empty_key_is_treated_as_no_key(self):
        assert config.secret(ENV_API_KEY, {ENV_API_KEY: "   "}) is None

    def test_a_missing_key_makes_no_request(self):
        feed = Feed()
        MetalpriceProvider(api_key=None, fetch=feed).quote("Au", "gold")
        assert feed.calls == []

    def test_a_timeout_produces_no_price(self):
        quote = provider(Feed(TimeoutError("timed out"))).quote("Au", "gold")
        assert quote.status is PriceStatus.ERROR
        assert quote.price_per_gram is None

    def test_a_network_failure_produces_no_price(self):
        quote = provider(Feed(FetchError("could not reach the price API"))).quote("Au", "gold")
        assert quote.status is PriceStatus.ERROR
        assert "could not reach" in quote.reason

    @pytest.mark.parametrize(
        ("code", "phrase"),
        [(101, "API key"), (102, "API key"), (104, "allowance"), (202, "currencies")],
    )
    def test_a_documented_api_error_is_reported_in_words(self, code, phrase):
        body = {"success": False, "error": {"code": code, "info": ""}}
        quote = provider(Feed(body)).quote("Au", "gold")
        assert quote.status is PriceStatus.ERROR
        assert phrase in quote.reason

    def test_the_rate_limit_message_is_passed_through(self):
        body = {"success": False, "error": {"code": 104, "info": "Monthly limit reached."}}
        assert "Monthly limit reached." in provider(Feed(body)).quote("Au", "gold").reason

    @pytest.mark.parametrize(
        "body",
        [
            {"success": True, "base": "USD", "timestamp": MARKET_TS},
            {"success": True, "base": "USD", "timestamp": MARKET_TS, "rates": {}},
            {"success": True, "base": "USD", "rates": RATES},
            {"success": True, "base": "USD", "timestamp": "yesterday", "rates": RATES},
        ],
        ids=["no rates", "empty rates", "no timestamp", "unparseable timestamp"],
    )
    def test_a_malformed_response_produces_no_price(self, body):
        quote = provider(Feed(body)).quote("Au", "gold")
        assert quote.price_per_gram is None
        assert quote.status is PriceStatus.ERROR

    def test_a_metal_the_feed_does_not_quote_is_refused(self):
        rates = {k: v for k, v in RATES.items() if "XPD" not in k}
        quote = provider(Feed(payload(rates=rates))).quote("Pd", "palladium")
        assert quote.status is PriceStatus.UNAVAILABLE
        assert quote.price_per_gram is None

    @pytest.mark.parametrize("bad", [0, -1.0, "1856.9", None, True])
    def test_a_rate_that_is_not_a_positive_number_is_refused(self, bad):
        rates = {**RATES, "XAU": bad, "USDXAU": bad}
        quote = provider(Feed(payload(rates=rates))).quote("Au", "gold")
        assert quote.price_per_gram is None

    def test_a_metal_on_a_different_unit_basis_is_never_converted(self):
        """Copper is quoted per tonne. Dividing it by 31.1 would be wrong by 32 150x."""
        quote = provider().quote("Cu", "copper")
        assert quote.status is PriceStatus.UNAVAILABLE
        assert "troy ounce" in quote.reason

    def test_an_unexpected_exception_is_caught_not_propagated(self):
        quote = provider(Feed(RuntimeError("something nobody predicted"))).quote("Au", "gold")
        assert quote.status is PriceStatus.ERROR
        assert quote.price_per_gram is None


class TestRecovery:
    def test_a_failed_refresh_serves_the_last_good_snapshot(self):
        feed = Feed(payload(), FetchError("the feed went away"))
        now = [0.0]
        p = provider(feed, clock=lambda: now[0], cache_seconds=10.0)
        assert p.quote("Au", "gold").price_per_gram == pytest.approx(8000.0)
        now[0] = 20.0
        recovered = p.quote("Au", "gold")
        assert recovered.price_per_gram == pytest.approx(8000.0)
        assert "Serving the last successful quote" in recovered.reason

    def test_the_feed_coming_back_is_picked_up(self):
        newer = payload(rates={**RATES, "USDXAU": 6220.69536})
        feed = Feed(payload(), FetchError("blip"), newer)
        now = [0.0]
        p = provider(feed, clock=lambda: now[0], cache_seconds=10.0)
        p.quote("Au", "gold")
        now[0] = 20.0
        p.quote("Au", "gold")
        now[0] = 40.0
        assert p.quote("Au", "gold").price_per_gram == pytest.approx(16000.0)


class TestStaleness:
    """A served-from-cache quote is aged against its MARKET timestamp."""

    def test_a_quote_inside_the_window_is_live(self):
        service = PriceService(provider=provider(), max_age_seconds=900.0)
        now = datetime.fromtimestamp(MARKET_TS + 60, tz=UTC)
        assert service.price("Au", now=now).status is PriceStatus.LIVE

    def test_a_quote_past_the_window_is_stale_and_keeps_its_number(self):
        service = PriceService(provider=provider(), max_age_seconds=900.0)
        now = datetime.fromtimestamp(MARKET_TS + 901, tz=UTC)
        quote = service.price("Au", now=now)
        assert quote.status is PriceStatus.STALE
        assert quote.price_per_gram == pytest.approx(8000.0)

    def test_a_stale_quote_is_not_current(self):
        service = PriceService(provider=provider(), max_age_seconds=900.0)
        now = datetime.fromtimestamp(MARKET_TS + 901, tz=UTC)
        assert not service.price("Au", now=now).is_current


class TestTheFallbackChain:
    def test_a_live_outage_degrades_to_the_dated_snapshot(self):
        chain = FallbackProvider(
            provider(Feed(FetchError("the feed is down"))),
            ReferenceProvider.from_config(),
        )
        quote = chain.quote("Au", "gold")
        assert quote.status is PriceStatus.REFERENCE
        assert quote.price_per_gram is not None
        assert "Fell back from metalprice" in quote.reason

    def test_a_live_quote_wins_when_there_is_one(self):
        chain = FallbackProvider(provider(), ReferenceProvider.from_config())
        assert chain.quote("Au", "gold").status is PriceStatus.LIVE

    def test_copper_falls_through_to_the_snapshot(self):
        """The live feed declines it on units; the snapshot has a cited figure."""
        chain = FallbackProvider(provider(), ReferenceProvider.from_config())
        assert chain.quote("Cu", "copper").status is PriceStatus.REFERENCE


class TestConfiguration:
    def test_the_provider_is_reachable_by_name(self, monkeypatch):
        monkeypatch.setenv(ENV_API_KEY, "TEST-KEY")
        built = build_provider("metalprice", config.load())
        assert isinstance(built, FallbackProvider)
        assert built.primary.name == "metalprice"

    def test_the_fallback_can_be_switched_off(self, monkeypatch):
        monkeypatch.setenv(ENV_API_KEY, "TEST-KEY")
        monkeypatch.setenv("AURUM_PRICE_FALLBACK_TO_REFERENCE", "false")
        assert isinstance(build_provider("metalprice", config.load()), MetalpriceProvider)

    def test_an_unknown_provider_name_lists_the_known_ones(self):
        with pytest.raises(config.ConfigError) as exc:
            build_provider("bloomberg", config.load())
        assert "metalprice" in str(exc.value)


class TestTheKeyNeverEscapes:
    """The one string in this system that must never be rendered anywhere."""

    KEY = "SUPER-SECRET-KEY-0123456789"

    def _quotes(self, feed):
        p = MetalpriceProvider(api_key=self.KEY, fetch=feed, clock=lambda: 0.0)
        return [p.quote(m, m.lower()) for m in ("Au", "Cu")]

    def test_it_is_not_in_a_successful_quote(self):
        for quote in self._quotes(Feed()):
            assert self.KEY not in str(quote.as_dict())

    @pytest.mark.parametrize(
        "response",
        [
            FetchError("HTTP 401 from the price API, api_key=" + KEY),
            {"success": False, "error": {"code": 102, "info": "api_key=" + KEY + " is invalid"}},
            RuntimeError("boom while calling with api_key=" + KEY),
        ],
        ids=["transport error", "api error", "unexpected exception"],
    )
    def test_it_is_redacted_out_of_every_failure_message(self, response):
        metalprice.reset_cache()
        for quote in self._quotes(Feed(response)):
            assert self.KEY not in str(quote.as_dict())

    def test_it_is_not_in_the_reported_endpoint(self):
        p = MetalpriceProvider(api_key=self.KEY)
        assert self.KEY not in p.endpoint
        assert self.KEY in p._url()

    def test_it_never_enters_the_config_object(self, monkeypatch):
        monkeypatch.setenv(ENV_API_KEY, self.KEY)
        assert self.KEY not in str(config.load().as_dict())
