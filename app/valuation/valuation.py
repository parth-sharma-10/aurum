"""Valuation — PMDI plus the base-metal signal, packaged so it can be audited.

The layer above `app.valuation.pmdi` and below the decision engine:

    material evidence -> PMDI / precious metrics -> VALUATION -> decision policy

Two value signals are kept apart on purpose, because collapsing them is how a
number stops meaning anything:

    precious_value   the concept document's PMDI figure. Au, Ag, Pd, Pt.
    base_value       Cu, Ni, Sn. A separate recycling signal, never called PMDI.

PMDI measures precious-metal economics and nothing else. It does not know about
base metals, recyclability, processing cost or environmental value, and this
module does not teach it to. Bin B's base-metal reasoning reads `base_value`;
`pmdi_value` stays exactly what the concept document defined.

Nothing here decides a grade. `app.decision` consumes this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.valuation import pmdi as pmdi_module
from app.valuation.pmdi import (
    DISCLAIMER,
    EvidenceStatus,
    MassStatus,
    OverallStatus,
    PmdiResult,
    _worst,
)
from app.valuation.prices import MetalPrice, PriceService, PriceStatus


@dataclass
class Valuation:
    """One item's, or one batch's, complete valuation with its provenance."""

    item_id: str | None
    counts: dict[str, int]
    pmdi: PmdiResult
    base_value: float | None = None
    base_price_status: PriceStatus = PriceStatus.UNAVAILABLE
    base_prices: dict[str, MetalPrice] = field(default_factory=dict)
    total_value: float | None = None
    currency: str | None = None
    overall_status: OverallStatus = OverallStatus.UNAVAILABLE
    reason: str | None = None

    @property
    def component_class(self) -> str | None:
        """The single detected class, or None for a mixed batch."""
        present = [c for c, n in self.counts.items() if n]
        return present[0] if len(present) == 1 else None

    @property
    def evidence_status(self) -> EvidenceStatus:
        return self.pmdi.evidence_status

    @property
    def weight_status(self) -> MassStatus:
        return self.pmdi.mass_status

    def as_dict(self, digits: int = 9) -> dict:
        """Full precision is kept internally; rounding happens only here."""
        p = self.pmdi
        price_timestamps = {
            metal: quote.timestamp
            for metal, quote in sorted({**p.prices, **self.base_prices}.items())
            if quote.timestamp
        }
        return {
            "item_id": self.item_id,
            "component_class": self.component_class,
            "counts": dict(self.counts),
            "weight_g": None if p.mass_g is None else round(p.mass_g, 4),
            "weight_status": str(self.weight_status),
            "metals": sorted({**p.precious, **p.base, **p.other}),
            "metal_amounts": {
                "precious": {m: a.as_dict(digits) for m, a in sorted(p.precious.items())},
                "base": {m: a.as_dict(digits) for m, a in sorted(p.base.items())},
                "other": {m: a.as_dict(digits) for m, a in sorted(p.other.items())},
            },
            "prices": {
                metal: quote.as_dict()
                for metal, quote in sorted({**p.prices, **self.base_prices}.items())
            },
            "price_status": str(p.price_status),
            "base_price_status": str(self.base_price_status),
            "price_timestamps": price_timestamps,
            "pmdi": p.as_dict(digits),
            "precious_value": None if p.pmdi_value is None else round(p.pmdi_value, 6),
            "base_value": None if self.base_value is None else round(self.base_value, 6),
            "total_value": None if self.total_value is None else round(self.total_value, 6),
            "currency": self.currency,
            "evidence_sources": list(p.evidence_sources),
            "evidence_status": str(self.evidence_status),
            "confidence": p.confidence,
            "overall_status": str(self.overall_status),
            "reason": self.reason,
            "kind": "ESTIMATE",
            "disclaimer": DISCLAIMER,
        }


def _value_of(amounts, quotes) -> tuple[float | None, PriceStatus, str | None]:
    """Sum grams x price-per-gram, or refuse and say why.

    Refuses on a partial set for the same reason `pmdi` does: a total missing
    one of its metals still presents itself as a total.
    """
    if not amounts:
        return None, PriceStatus.UNAVAILABLE, "no metal in this group has cited evidence."
    priced = {m: q for m, q in quotes.items() if q.has_number}
    if len(priced) != len(amounts):
        missing = sorted(set(amounts) - set(priced))
        return None, PriceStatus.UNAVAILABLE, f"no price for {', '.join(missing)}."
    currencies = {q.currency for q in priced.values()}
    if len(currencies) > 1:
        return (
            None,
            PriceStatus.ERROR,
            "the price source mixes currencies; refusing to add them into one total.",
        )
    total = sum(amounts[m].grams * priced[m].price_per_gram for m in amounts)
    status = (
        PriceStatus.STALE
        if any(q.status is PriceStatus.STALE for q in priced.values())
        else next(iter(priced.values())).status
    )
    return total, status, None


def value(
    counts: dict[str, int],
    mass: dict | None = None,
    item_id: str | None = None,
    prices: PriceService | None = None,
    now: datetime | None = None,
) -> Valuation:
    """Counts and an optional mass to a complete, auditable valuation."""
    now = datetime.now(UTC) if now is None else now
    prices = PriceService.from_config() if prices is None else prices
    result = pmdi_module.compute(counts, mass=mass, prices=prices, now=now)

    if not result.available:
        return Valuation(
            item_id=item_id,
            counts=dict(counts),
            pmdi=result,
            overall_status=OverallStatus.UNAVAILABLE,
            reason=result.reason,
        )

    base_quotes = prices.prices(sorted(result.base), now=now) if result.base else {}
    base_total, base_status, base_gap = _value_of(result.base, base_quotes)

    total: float | None = None
    currency = result.currency
    if result.pmdi_value is not None and base_total is not None:
        if result.currency != next(iter(base_quotes.values())).currency:
            total, currency = None, None
        else:
            total = result.pmdi_value + base_total
    elif result.pmdi_value is not None and not result.base:
        # Nothing base-metal is cited for these components, so the precious
        # figure is the whole of what the evidence supports.
        total = result.pmdi_value

    statuses = [result.overall_status]
    if base_status is PriceStatus.STALE:
        statuses.append(OverallStatus.STALE)

    reasons = [r for r in (result.reason, base_gap and f"No base-metal value: {base_gap}") if r]
    return Valuation(
        item_id=item_id,
        counts=dict(counts),
        pmdi=result,
        base_value=base_total,
        base_price_status=base_status,
        base_prices=base_quotes,
        total_value=total,
        currency=currency if total is not None else result.currency,
        overall_status=_worst(statuses),
        reason=" ".join(reasons) or None,
    )
