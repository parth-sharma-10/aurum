"""Metal prices, and an honest account of where each one came from.

Aurum has **no approved live market data source**. `configs/pricing.yaml`
ships `provider: unavailable`, which is a decision rather than a placeholder:
every price comes back `UNAVAILABLE` and the valuation layer refuses to produce
a figure. A spot price with no source and no timestamp is worse than no price,
because it gets screenshotted and outlives the conversation that qualified it.

Two providers exist today:

    UnavailableProvider   the default. Prices nothing, says why.
    StaticProvider        pinned prices from configs/price_reference.yaml,
                          labelled TEST, never LIVE.

A live provider is added by implementing `PriceProvider` and registering it in
`PROVIDERS`. Nothing in `app.valuation.pmdi` changes when that happens — that
is the whole point of the abstraction.

The unit trap this module exists to close: metal is quoted per gram, per
kilogram and per troy ounce, and a troy ounce is 31.1 grams. Multiplying grams
by a per-ounce price produces a plausible number that is wrong by a factor of
31. Every quote is normalised to currency-per-gram through an explicit factor,
and a unit nobody has a factor for is an error rather than an assumption.
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
    TEST = "TEST"
    SIMULATED = "SIMULATED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


#: Statuses that carry an actual number. STALE does too, but it is deliberately
#: excluded here: a stale price is usable only if a caller decides it is, and
#: that decision belongs to the grading policy, not to this module.
PRICED = frozenset({PriceStatus.LIVE, PriceStatus.TEST, PriceStatus.SIMULATED})


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
        unit = entry.get("unit")
        try:
            per_gram = to_grams(float(entry["price_per_unit"]), unit)
        except (ValueError, KeyError, TypeError) as exc:
            return MetalPrice(
                metal=metal,
                material=material,
                status=PriceStatus.ERROR,
                provider=self.name,
                reason=str(exc),
            )
        return MetalPrice(
            metal=metal,
            material=material,
            status=PriceStatus.TEST,
            price_per_gram=per_gram,
            currency=entry.get("currency", "USD"),
            quoted_price=float(entry["price_per_unit"]),
            quoted_unit=unit,
            timestamp=entry.get("timestamp"),
            provider=self.name,
            reason="Pinned reference price. TEST data — not a live market quote.",
        )


PROVIDERS = {
    "unavailable": UnavailableProvider,
    "static": StaticProvider,
}


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
        name = cfg["pricing.provider"]
        factory = PROVIDERS[name]
        provider = factory.from_config() if name == "static" else factory()
        return cls(provider=provider, max_age_seconds=cfg["pricing.max_age_seconds"])

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
