"""Valuation — the layer that turns an estimated material quantity into money.

Deliberately separate from everything upstream. The detector produces component
identities; `app.batch.recovery_estimate` turns counts into an *estimated*
material quantity using cited reference yields; only this module multiplies that
by a price. Nothing here may run inside the vision path, because a metal price
is time-varying external data and a detection is not.

    detected components
      → estimated material recovery   (app.batch.recovery_estimate)
      → price quote                   (a PriceProvider, here)
      → estimated value               (value_recovery, here)

**No price data ships with this repository.** `configs/price_reference.yaml`
is empty and disabled, so `get_provider()` returns None and valuation reports
itself unavailable rather than producing a figure. A spot price with no source
and no timestamp behind it is worse than no figure, because it gets quoted.
Tests supply their own provider; that is the only way a number appears.

Every value this module produces is an ESTIMATE derived from an estimate. It is
never a measurement of the material in a batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parent.parent
PRICE_CONFIG = ROOT / "configs" / "price_reference.yaml"

# Bumped when the arithmetic below changes, so a stored valuation can be told
# apart from one produced by a later version of this code.
CALCULATION_VERSION = "aurum-valuation-0.1"

DISCLAIMER = (
    "ESTIMATE ONLY — estimated material quantity multiplied by a configured "
    "price. Aurum Vision does not measure material content, and this is not an "
    "assay, an offer, or a market valuation."
)


@dataclass(frozen=True)
class PriceQuote:
    """One price for one material, with everything needed to audit it later."""

    material: str
    price_per_unit: float
    unit: str
    currency: str
    timestamp: str
    source: str

    def as_dict(self) -> dict:
        return {
            "material": self.material,
            "price_per_unit": self.price_per_unit,
            "unit": self.unit,
            "currency": self.currency,
            "timestamp": self.timestamp,
            "source": self.source,
        }


class PriceProvider(Protocol):
    """Anything that can price a material.

    Implementations are free to hit a live feed; the contract is only that a
    quote carries its own source and timestamp, so a stored valuation stays
    auditable after the feed has moved on.
    """

    name: str

    def quote(self, material: str, unit: str) -> PriceQuote | None: ...


class StaticPriceProvider:
    """Prices from a configuration file. No file, no prices.

    Used for tests and for an operator who wants to pin a price they can cite.
    It refuses to guess: a quote is returned only when the configured unit
    matches the unit the quantity is expressed in, because multiplying grams by
    a per-ounce price silently produces a number that looks plausible.
    """

    def __init__(self, prices: dict, source: str, timestamp: str | None = None) -> None:
        self._prices = prices
        self.name = source
        self._timestamp = timestamp or datetime.now(UTC).isoformat(timespec="seconds")

    @classmethod
    def from_config(cls, path: Path = PRICE_CONFIG) -> StaticPriceProvider | None:
        """Build from YAML, or None when pricing is not configured."""
        if not path.exists():
            return None
        import yaml

        cfg = yaml.safe_load(path.read_text()) or {}
        if not cfg.get("enabled"):
            return None
        prices = cfg.get("prices") or {}
        if not prices:
            return None
        return cls(prices, cfg.get("source", str(path)), cfg.get("timestamp"))

    def quote(self, material: str, unit: str) -> PriceQuote | None:
        entry = self._prices.get(material)
        if not entry or entry.get("unit") != unit:
            return None
        return PriceQuote(
            material=material,
            price_per_unit=float(entry["price_per_unit"]),
            unit=entry["unit"],
            currency=entry.get("currency", "USD"),
            timestamp=entry.get("timestamp", self._timestamp),
            source=entry.get("source", self.name),
        )


def get_provider() -> PriceProvider | None:
    """The configured provider, or None when pricing is disabled."""
    return StaticPriceProvider.from_config()


def _unavailable(reason: str) -> dict:
    return {
        "available": False,
        "reason": reason,
        "calculation_version": CALCULATION_VERSION,
        "disclaimer": DISCLAIMER,
    }


def value_recovery(recovery: dict, provider: PriceProvider | None = None) -> dict:
    """Estimated material quantity × price per unit = estimated value.

    Returns an explicit refusal rather than a partial number in every case where
    an input is missing: no recovery estimate, no provider, or a material the
    provider will not quote in the unit the quantity is expressed in. A
    valuation that silently drops the material it could not price would understate
    the total while still looking like a total.
    """
    if not recovery or not recovery.get("available"):
        return _unavailable(
            "No material recovery estimate available, so there is nothing to price. "
            + (recovery or {}).get("reason", "")
        )

    provider = provider or get_provider()
    if provider is None:
        return _unavailable(
            "No price source configured. Populate configs/price_reference.yaml with "
            "cited prices and set enabled: true, or supply a provider."
        )

    lines, total, unpriced = [], 0.0, []
    currency = None
    for component in recovery.get("components", []):
        material = component.get("material") or component.get("unit", "")
        quantity, unit = component.get("total"), component.get("unit")
        if quantity is None or unit is None:
            unpriced.append(component.get("component"))
            continue
        q = provider.quote(material, unit)
        if q is None:
            unpriced.append(component.get("component"))
            continue
        if currency and q.currency != currency:
            return _unavailable(
                f"Price source mixes currencies ({currency} and {q.currency}); "
                "refusing to add them into one total."
            )
        currency = q.currency
        value = round(quantity * q.price_per_unit, 6)
        total += value
        lines.append(
            {
                "component": component.get("component"),
                "count": component.get("count"),
                "estimated_quantity": quantity,
                "unit": unit,
                "price": q.as_dict(),
                "estimated_value": value,
            }
        )

    if not lines:
        return _unavailable(
            f"Price source quoted none of the estimated materials: {sorted(filter(None, unpriced))}"
        )

    return {
        "available": True,
        "kind": "ESTIMATE",
        "estimated_value": round(total, 6),
        "currency": currency,
        "components": lines,
        "unpriced_components": sorted(filter(None, unpriced)),
        "price_source": getattr(provider, "name", "unknown"),
        "calculation_version": CALCULATION_VERSION,
        "disclaimer": DISCLAIMER,
    }
