"""Metal prices, and an honest account of where each one came from.

Aurum has **no approved live market data source**. `configs/pricing.yaml`
ships `provider: unavailable`, which is a decision rather than a placeholder:
every price comes back `UNAVAILABLE` and the valuation layer refuses to produce
a figure. A spot price with no source and no timestamp is worse than no price,
because it gets screenshotted and outlives the conversation that qualified it.

Four providers exist today:

    UnavailableProvider   prices nothing, says why.
    StaticProvider        pinned prices, labelled TEST. For tests.
    ReferenceProvider     a DATED SNAPSHOT of real published prices, labelled
                          REFERENCE. The shipped default.
    FallbackProvider      composes two providers: try one, fall back to the
                          other. The LIVE -> REFERENCE chain.

**REFERENCE is not a stale LIVE price, and it is not a fake one.** It is a real
published price with a real date, being used deliberately after that date. Age
does not degrade it the way a silent feed degrades a live quote, so
`PriceService` does not mark it STALE - but nothing may present it as current
either. The status travels with every quote so a dashboard can say "reference
price, 21 Aug 2026" instead of implying a live market feed.

    MetalpriceProvider   LIVE spot prices from MetalpriceAPI, in
                         app/valuation/metalprice.py. Needs an API key, so it
                         is not the shipped default; with `pricing.provider:
                         metalprice` it is composed over ReferenceProvider by
                         `FallbackProvider`, and an outage degrades to the
                         dated snapshot instead of stopping the pipeline.

A further provider is added by implementing `quote()` and registering it in
`PROVIDERS`. Nothing in `app.valuation.pmdi` changes when that happens - that
is the whole point of the abstraction.

Two conversion traps this module exists to close.

**Units.** Metal is quoted per gram, per kilogram and per troy ounce, and a
troy ounce is 31.1034768 g - NOT the avoirdupois 28.3495 g. Multiplying grams
by a per-ounce price produces a plausible number that is wrong by a factor of
31. Every quote is normalised to currency-per-gram through an explicit factor,
and a unit nobody has a factor for is an error rather than an assumption.

**Currency.** A price quoted in USD cannot be added to one quoted in INR. Each
quote is converted to the snapshot's single reporting currency through a cited,
dated FX rate, and a currency with no rate on file is an error rather than an
assumed parity of 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import yaml

from app import config as config_module
from app import materials
from app.pricing import PRICE_CONFIG

# A troy ounce is defined as exactly 31.1034768 g. These are unit definitions,
# not measurements, and are the only numeric constants in this module.
GRAMS_PER_UNIT: dict[str, float] = {
    "g": 1.0,
    "kg": 1000.0,
    "ozt": 31.1034768,
}


class PriceStatus(StrEnum):
    """Where a price came from and whether it can be trusted as current."""

    LIVE = "LIVE"
    #: A real published price, with a real date, used deliberately after it.
    #: Never presented as current; never degraded by age either.
    REFERENCE = "REFERENCE"
    TEST = "TEST"
    SIMULATED = "SIMULATED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


#: Statuses that carry an actual number. STALE does too, but it is deliberately
#: excluded here: a stale price is usable only if a caller decides it is, and
#: that decision belongs to the grading policy, not to this module.
PRICED = frozenset(
    {PriceStatus.LIVE, PriceStatus.REFERENCE, PriceStatus.TEST, PriceStatus.SIMULATED}
)


@dataclass(frozen=True)
class MetalPrice:
    """One metal's price, with everything needed to audit it later."""

    metal: str
    material: str
    status: PriceStatus
    price_per_gram: float | None = None
    currency: str | None = None
    quoted_price: float | None = None
    quoted_unit: str | None = None
    timestamp: str | None = None
    provider: str = "unavailable"
    age_seconds: float | None = None
    reason: str | None = None

    @property
    def has_number(self) -> bool:
        """True when a figure exists at all, stale or not."""
        return self.price_per_gram is not None

    @property
    def is_current(self) -> bool:
        """True only for a price that may be presented as current."""
        return self.status in PRICED

    def as_dict(self) -> dict:
        return {
            "metal": self.metal,
            "material": self.material,
            "status": str(self.status),
            "price_per_gram": self.price_per_gram,
            "currency": self.currency,
            "quoted_price": self.quoted_price,
            "quoted_unit": self.quoted_unit,
            "timestamp": self.timestamp,
            "provider": self.provider,
            "age_seconds": self.age_seconds,
            "reason": self.reason,
        }


def to_grams(price: float, unit: str) -> float:
    """Convert a price per `unit` into a price per gram.

    Raises on an unknown unit. Guessing here is how a per-ounce quote becomes a
    per-gram number that is wrong by 31x and still looks reasonable.
    """
    try:
        factor = GRAMS_PER_UNIT[unit]
    except KeyError:
        raise ValueError(
            f"No gram conversion for price unit {unit!r}. Known units: "
            f"{', '.join(sorted(GRAMS_PER_UNIT))}."
        ) from None
    return price / factor


class FxError(ValueError):
    """No cited rate exists to convert a quote into the reporting currency."""


def convert_currency(amount: float, source: str, target: str, rates: dict) -> float:
    """Convert `amount` from `source` currency into `target`.

    `rates` maps a currency to its rate against the TARGET, e.g. under a target
    of INR, `{"USD": {"rate": 95.70}}` means one USD is 95.70 INR.

    Raises rather than assuming. A missing rate is not parity: silently
    treating 1331 USD as 1331 INR would understate a palladium price by a
    factor of 95 while still looking like a number.
    """
    if source == target:
        return amount
    entry = (rates or {}).get(source)
    rate = (entry or {}).get("rate")
    if rate is None:
        raise FxError(
            f"No FX rate on file to convert {source} into {target}. Add an "
            f"fx entry for {source} with a rate, a source and a timestamp."
        )
    rate = float(rate)
    if rate <= 0:
        raise FxError(f"The {source}/{target} rate must be positive, got {rate}.")
    return amount * rate


def _quote_from_entry(
    metal: str,
    material: str,
    entry: dict,
    status: PriceStatus,
    provider: str,
    reason: str,
    fx: dict | None = None,
    currency: str | None = None,
) -> MetalPrice:
    """One configured price entry to a normalised per-gram quote.

    Shared by every file-backed provider so the unit and currency arithmetic
    exists once. Both conversions are recorded on the quote: `quoted_price` and
    `quoted_unit` keep the figure as published, so a stored valuation can be
    audited back to its source without re-deriving anything.
    """
    unit = entry.get("unit")
    source_currency = entry.get("currency", "USD")
    target = currency or source_currency
    try:
        per_gram = to_grams(float(entry["price_per_unit"]), unit)
        per_gram = convert_currency(per_gram, source_currency, target, fx or {})
    except (ValueError, KeyError, TypeError) as exc:
        return MetalPrice(
            metal=metal,
            material=material,
            status=PriceStatus.ERROR,
            provider=provider,
            reason=str(exc),
        )
    detail = entry.get("source")
    if source_currency != target:
        rate = (fx or {}).get(source_currency, {})
        reason = (
            f"{reason} Converted {entry['price_per_unit']} {source_currency}/{unit} "
            f"-> {source_currency}/g (1 ozt = {GRAMS_PER_UNIT['ozt']} g) "
            f"-> {target}/g at {source_currency}/{target} {rate.get('rate')} "
            f"({rate.get('source', 'unattributed rate')}, {rate.get('timestamp', 'undated')})."
        )
    return MetalPrice(
        metal=metal,
        material=material,
        status=status,
        price_per_gram=per_gram,
        currency=target,
        quoted_price=float(entry["price_per_unit"]),
        quoted_unit=unit,
        timestamp=entry.get("timestamp"),
        provider=provider,
        reason=f"{reason} Source: {detail}." if detail else reason,
    )


class UnavailableProvider:
    """The default. Prices nothing, and says which setting would change that."""

    name = "unavailable"
    status = PriceStatus.UNAVAILABLE

    def quote(self, metal: str, material: str) -> MetalPrice:
        return MetalPrice(
            metal=metal,
            material=material,
            status=PriceStatus.UNAVAILABLE,
            provider=self.name,
            reason=(
                "No price provider is configured. Aurum ships no market data "
                "source. Set pricing.provider in configs/pricing.yaml, or "
                "export AURUM_PRICE_PROVIDER."
            ),
        )


class StaticProvider:
    """Pinned prices from configuration, or supplied directly by a test.

    Every price this provider returns is labelled TEST. It exists to exercise
    the calculation pipeline deterministically. It is never LIVE, never SPOT,
    and never a current market price, regardless of what is in the file.
    """

    name = "static"
    status = PriceStatus.TEST

    def __init__(self, prices: dict | None = None, source: str | None = None) -> None:
        """`prices` maps a material name to the shape used by app.pricing."""
        self._prices = prices or {}
        self.source = source or "configs/price_reference.yaml"

    @classmethod
    def from_config(cls, path: Path = PRICE_CONFIG) -> StaticProvider:
        """Build from the pinned-price file. A disabled or empty file prices nothing."""
        if not path.exists():
            return cls({}, source=str(path))
        cfg = yaml.safe_load(path.read_text()) or {}
        if not cfg.get("enabled"):
            return cls({}, source=str(path))
        return cls(cfg.get("prices") or {}, source=cfg.get("source", str(path)))

    def quote(self, metal: str, material: str) -> MetalPrice:
        entry = self._prices.get(material) or self._prices.get(metal)
        if not entry:
            return MetalPrice(
                metal=metal,
                material=material,
                status=PriceStatus.UNAVAILABLE,
                provider=self.name,
                reason=f"The pinned price source quotes no price for {material}.",
            )
        return _quote_from_entry(
            metal,
            material,
            entry,
            status=PriceStatus.TEST,
            provider=self.name,
            reason="Pinned reference price. TEST data — not a live market quote.",
        )


class ReferenceProvider:
    """A dated snapshot of real published prices. The shipped default.

    Every figure here was published by a named source on a named date and is
    being used deliberately after that date. That is a different thing from a
    live feed and a different thing from an invented number, and the REFERENCE
    status is what keeps all three apart downstream.

    The snapshot names one reporting currency and carries the FX rates needed
    to reach it, so a palladium price published in USD per troy ounce and a
    gold price published in INR per gram can be added into one total without
    anybody performing that conversion in their head.
    """

    name = "reference"
    status = PriceStatus.REFERENCE

    def __init__(
        self,
        prices: dict | None = None,
        currency: str = "INR",
        fx: dict | None = None,
        source: str | None = None,
        as_of: str | None = None,
    ) -> None:
        self._prices = prices or {}
        self.currency = currency
        self.fx = fx or {}
        self.source = source or str(PRICE_CONFIG)
        self.as_of = as_of

    @classmethod
    def from_config(cls, path: Path = PRICE_CONFIG) -> ReferenceProvider:
        """Build from the snapshot file. A disabled or absent file prices nothing."""
        if not path.exists():
            return cls({}, source=str(path))
        cfg = yaml.safe_load(path.read_text()) or {}
        if not cfg.get("enabled"):
            return cls({}, source=str(path))
        return cls(
            prices=cfg.get("prices") or {},
            currency=cfg.get("currency", "INR"),
            fx=cfg.get("fx") or {},
            source=cfg.get("source", str(path)),
            as_of=cfg.get("as_of"),
        )

    def quote(self, metal: str, material: str) -> MetalPrice:
        entry = self._prices.get(material) or self._prices.get(metal)
        if not entry:
            return MetalPrice(
                metal=metal,
                material=material,
                status=PriceStatus.UNAVAILABLE,
                provider=self.name,
                reason=(
                    f"The reference snapshot quotes no price for {material}. No figure "
                    "is substituted for one."
                ),
            )
        dated = entry.get("timestamp") or self.as_of or "an unstated date"
        return _quote_from_entry(
            metal,
            material,
            entry,
            status=PriceStatus.REFERENCE,
            provider=self.name,
            reason=(
                f"REFERENCE PRICE as of {dated} — a real published price being used "
                "after its date, not a live market quote."
            ),
            fx=self.fx,
            currency=self.currency,
        )


class FallbackProvider:
    """Try one provider, fall back to another. The LIVE -> REFERENCE chain.

    The fallback fires when the primary returns no usable number OR raises:
    a market feed that times out must degrade to the dated snapshot rather
    than taking the pipeline down with it. Which provider actually answered is
    visible in the returned quote's `provider` and `status`, so a fallback is
    never mistaken for a live price.
    """

    def __init__(self, primary, fallback) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = f"{getattr(primary, 'name', '?')}->{getattr(fallback, 'name', '?')}"

    def quote(self, metal: str, material: str) -> MetalPrice:
        try:
            quote = self.primary.quote(metal, material)
        except Exception as exc:  # a market feed is external and may do anything
            quote = MetalPrice(
                metal=metal,
                material=material,
                status=PriceStatus.ERROR,
                provider=getattr(self.primary, "name", "primary"),
                reason=f"The primary price source failed: {exc}",
            )
        if quote.has_number:
            return quote
        second = self.fallback.quote(metal, material)
        if not second.has_number:
            return second
        return replace(
            second,
            reason=f"{second.reason} Fell back from {getattr(self.primary, 'name', '?')}: "
            f"{quote.reason}",
        )


def _metalprice(cfg: config_module.Config):
    """The live feed, composed over the dated snapshot.

    Imported here rather than at module scope: `app.valuation.metalprice`
    reuses this module's quote construction, so a top-level import would be a
    cycle. It is also the only provider that needs a network, a key and a
    cache, and nothing that runs without one should pay to import it.
    """
    from app.valuation.metalprice import MetalpriceProvider

    live = MetalpriceProvider.from_config(cfg)
    if not cfg["pricing.fallback_to_reference"]:
        return live
    return FallbackProvider(live, ReferenceProvider.from_config())


#: Every provider that `pricing.provider` may name, keyed to a factory taking
#: the resolved config. One registry, so adding a feed is one entry here and
#: one value in the `_one_of` in app/config.py.
PROVIDERS = {
    "unavailable": lambda cfg: UnavailableProvider(),
    "static": lambda cfg: StaticProvider.from_config(),
    "reference": lambda cfg: ReferenceProvider.from_config(),
    "metalprice": _metalprice,
}


def build_provider(name: str, cfg: config_module.Config | None = None):
    """The provider `pricing.provider` names, or an error listing the choices."""
    cfg = config_module.load() if cfg is None else cfg
    try:
        factory = PROVIDERS[name]
    except KeyError:
        raise config_module.ConfigError(
            f"pricing.provider: unknown provider {name!r}. Known providers: "
            f"{', '.join(sorted(PROVIDERS))}."
        ) from None
    return factory(cfg)


def _age_seconds(timestamp: str | None, now: datetime) -> float | None:
    if not timestamp:
        return None
    text = timestamp.strip().replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (now - when).total_seconds()


@dataclass
class PriceService:
    """Resolves prices for metals, applying the configured staleness limit."""

    provider: object = field(default_factory=UnavailableProvider)
    max_age_seconds: float | None = None

    @classmethod
    def from_config(cls, cfg: config_module.Config | None = None) -> PriceService:
        cfg = config_module.load() if cfg is None else cfg
        return cls(
            provider=build_provider(cfg["pricing.provider"], cfg),
            max_age_seconds=cfg["pricing.max_age_seconds"],
        )

    def price(self, metal: str, now: datetime | None = None) -> MetalPrice:
        """The current price for one metal symbol, e.g. ``Au``."""
        material = materials.METAL_NAMES.get(metal, metal.lower())
        quote = self.provider.quote(metal, material)
        if not quote.has_number:
            return quote

        now = datetime.now(UTC) if now is None else now
        age = _age_seconds(quote.timestamp, now)
        if age is None:
            return quote

        # A reference price is not a live quote that went quiet. It was
        # published on a stated date and is being used deliberately after it,
        # so age is reported but never changes its status. STALE keeps its one
        # meaning: a feed that should have been current and was not.
        if quote.status is PriceStatus.REFERENCE:
            return replace(quote, age_seconds=age)

        stale = self.max_age_seconds is not None and age > self.max_age_seconds
        return replace(
            quote,
            age_seconds=age,
            status=PriceStatus.STALE if stale else quote.status,
            reason=(
                f"Quoted {age:.0f}s ago, older than the configured "
                f"{self.max_age_seconds:.0f}s limit."
                if stale
                else quote.reason
            ),
        )

    def prices(self, metals, now: datetime | None = None) -> dict[str, MetalPrice]:
        now = datetime.now(UTC) if now is None else now
        return {metal: self.price(metal, now) for metal in metals}
