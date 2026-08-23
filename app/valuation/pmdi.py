"""PMDI — the Precious Metal Density Index.

The project's authoritative definition, from the Aurum concept document, §4:

    PMDI = (Sigma (C_type x Y_estimated)) x P_spot

        C_type       count of a component type
        Y_estimated  estimated precious-metal yield per component
        P_spot       spot price of the relevant metal

Three things about that formula shape this module.

**It produces a currency amount, not a density.** `count x grams x
currency/gram` has units of currency; nothing is divided by mass. So this
module reports two separate quantities and never conflates them:
`pmdi_value` is the concept document's figure, and `precious_mass_fraction_ppm`
is the true density — precious-metal mass over component mass — which needs no
price at all. The second is what keeps the conveyor sorting when no price is
available.

**It needs a price, and Aurum has no approved market data source.** With
`pricing.provider: unavailable`, `pmdi_value` is `UNAVAILABLE` and only the
price-independent fraction is produced. No number is invented to fill the gap.

**`Y_estimated` is called a yield, and Aurum does not have yields.** The
repository holds *contained composition* from cited assays: how much metal is
in a component, not how much of it a recovery process would get out. Those are
different quantities and this module never renames one into the other. Every
figure here is labelled `contained`. The recovery figures that do exist
(LIN2023) were measured on a decopperized, stamp-sheared feed that no detected
component resembles, and `app.materials` refuses to apply them.

This module computes a signal. It does not decide anything: no A/B/C logic
lives here, because a grade is a policy about a number and this is the number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from app import materials
from app.valuation.prices import MetalPrice, PriceService, PriceStatus

#: Platinum is listed although the database holds no figure for it. Its absence
#: is a documented fact (no source read reports Pt in these components), not an
#: oversight, and naming it here keeps that visible.
PRECIOUS_METALS = ("Au", "Ag", "Pd", "Pt")

#: The base metals the project asked for. Aluminium is present in the evidence
#: but is not in this set; it is reported under `other_metals` rather than
#: silently folded into a base-metal total nobody asked for.
BASE_METALS = ("Cu", "Ni", "Sn")

DISCLAIMER = (
    "ESTIMATE — component counts multiplied by cited CONTAINED composition, "
    "not a recovery yield and not an assay. Aurum does not measure metal "
    "content."
)


class MassStatus(StrEnum):
    MEASURED = "MEASURED"
    SIMULATED = "SIMULATED"
    UNMEASURED = "UNMEASURED"


class EvidenceStatus(StrEnum):
    """How much of what was detected the cited evidence actually covers.

    A fact about the database, not a verdict about the object. PARTIAL means
    some detected components were valued and others were not, and which is
    which is in `valued` / `not_valued`. Nothing here decides a bin.
    """

    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"


class OverallStatus(StrEnum):
    """The worst thing true of any input, so nothing hides downstream."""

    ESTIMATED = "ESTIMATED"
    STALE = "STALE"
    SIMULATED = "SIMULATED"
    UNAVAILABLE = "UNAVAILABLE"


#: Worst first. `_worst()` returns the first status present in a result.
_SEVERITY = (
    OverallStatus.UNAVAILABLE,
    OverallStatus.SIMULATED,
    OverallStatus.STALE,
    OverallStatus.ESTIMATED,
)


def _worst(statuses) -> OverallStatus:
    present = set(statuses)
    for status in _SEVERITY:
        if status in present:
            return status
    return OverallStatus.ESTIMATED


def mass_status(mass: dict | None) -> MassStatus:
    """Classify a weight record from `app.weight.WeightReading.as_dict()`."""
    if not mass or mass.get("grams") is None:
        return MassStatus.UNMEASURED
    return MassStatus.SIMULATED if mass.get("simulated") else MassStatus.MEASURED


@dataclass
class MetalAmount:
    """How much of one metal the cited evidence says is contained."""

    metal: str
    material: str
    grams: float
    evidence: list[str] = field(default_factory=list)
    confidence: str | None = None
    #: Human-readable arithmetic, e.g. "1 x 4.71 mg per piece".
    calculation: str | None = None
    #: Always "contained". Present so that no consumer can mistake it for yield.
    basis: str = "contained"

    def as_dict(self, digits: int = 9) -> dict:
        return {
            "metal": self.metal,
            "material": self.material,
            "grams": round(self.grams, digits),
            "basis": self.basis,
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "calculation": self.calculation,
        }


@dataclass
class PmdiResult:
    """The precious-metal signal for one item or one batch of counts."""

    available: bool
    counts: dict[str, int]
    evidence_status: EvidenceStatus
    overall_status: OverallStatus
    mass_g: float | None = None
    mass_status: MassStatus = MassStatus.UNMEASURED
    precious: dict[str, MetalAmount] = field(default_factory=dict)
    base: dict[str, MetalAmount] = field(default_factory=dict)
    other: dict[str, MetalAmount] = field(default_factory=dict)
    prices: dict[str, MetalPrice] = field(default_factory=dict)
    pmdi_value: float | None = None
    currency: str | None = None
    price_status: PriceStatus = PriceStatus.UNAVAILABLE
    evidence_sources: list[str] = field(default_factory=list)
    confidence: str | None = None
    reason: str | None = None
    #: COMPLETE | PARTIAL_ESTIMATE | INSUFFICIENT_EVIDENCE, from app.materials.
    completeness: str = materials.INSUFFICIENT_EVIDENCE
    #: Detected components the estimate covers, and the ones it does not with
    #: the reason. A partial total is only meaningful alongside these.
    valued: list[dict] = field(default_factory=list)
    not_valued: list[dict] = field(default_factory=list)

    @property
    def precious_mass_g(self) -> float:
        return sum(a.grams for a in self.precious.values())

    @property
    def base_mass_g(self) -> float:
        return sum(a.grams for a in self.base.values())

    @property
    def precious_mass_fraction_ppm(self) -> float | None:
        """Precious-metal mass per million parts of component mass.

        The price-independent quantity, and the one that keeps the conveyor
        sorting when no price is available.

        `None` in two cases, both of which would otherwise read as a measured
        zero: without a mass, because a fraction of an unknown total is not a
        number; and without a usable estimate, because "no cited evidence"
        must not render as "0 ppm precious metal".
        """
        if not self.available or not self.mass_g:
            return None
        return self.precious_mass_g / self.mass_g * 1_000_000

    def as_dict(self, digits: int = 9) -> dict:
        """Round only here. Intermediates stay at full precision."""
        fraction = self.precious_mass_fraction_ppm
        return {
            "available": self.available,
            "formula": "PMDI = (sum(C_type x Y_estimated)) x P_spot",
            "counts": dict(self.counts),
            "mass_g": None if self.mass_g is None else round(self.mass_g, 4),
            "mass_status": str(self.mass_status),
            "precious_metals": {m: a.as_dict(digits) for m, a in sorted(self.precious.items())},
            "base_metals": {m: a.as_dict(digits) for m, a in sorted(self.base.items())},
            "other_metals": {m: a.as_dict(digits) for m, a in sorted(self.other.items())},
            "precious_mass_g": round(self.precious_mass_g, digits),
            "base_mass_g": round(self.base_mass_g, digits),
            "precious_mass_fraction_ppm": None if fraction is None else round(fraction, 4),
            "prices": {m: p.as_dict() for m, p in sorted(self.prices.items())},
            "pmdi_value": None if self.pmdi_value is None else round(self.pmdi_value, 6),
            "currency": self.currency,
            "price_status": str(self.price_status),
            "evidence_status": str(self.evidence_status),
            "completeness": self.completeness,
            "valued": [dict(v) for v in self.valued],
            "not_valued": [dict(n) for n in self.not_valued],
            "evidence_sources": list(self.evidence_sources),
            "confidence": self.confidence,
            "overall_status": str(self.overall_status),
            "reason": self.reason,
            "basis": "contained composition, not recovery yield",
            "disclaimer": DISCLAIMER,
        }


def _unavailable(
    counts: dict[str, int], mass: dict | None, reason: str, not_valued: list[dict] | None = None
) -> PmdiResult:
    return PmdiResult(
        available=False,
        counts=dict(counts),
        evidence_status=EvidenceStatus.MISSING,
        overall_status=OverallStatus.UNAVAILABLE,
        mass_g=(mass or {}).get("grams"),
        mass_status=mass_status(mass),
        reason=reason,
        completeness=materials.INSUFFICIENT_EVIDENCE,
        not_valued=list(not_valued or []),
    )


def _amounts_by_metal(estimate: dict) -> dict[str, MetalAmount]:
    """Fold the material layer's per-component lines into one entry per metal.

    `app.materials` has already done the unit work — mg per piece times a count,
    or mg/kg times a measured mass — and reports grams. Redoing that arithmetic
    here would be a second place for it to be wrong.
    """
    out: dict[str, MetalAmount] = {}
    for line in estimate.get("components", []):
        metal = line["metal"]
        entry = out.get(metal)
        if entry is None:
            out[metal] = MetalAmount(
                metal=metal,
                material=line.get("material", materials.METAL_NAMES.get(metal, metal)),
                grams=line["total"],
                evidence=list(line.get("evidence", [])),
                confidence=line.get("confidence"),
                calculation=line.get("calculation"),
            )
            continue
        entry.grams += line["total"]
        entry.evidence = sorted(set(entry.evidence) | set(line.get("evidence", [])))
        entry.calculation = f"{entry.calculation}; {line.get('calculation')}"
    return out


def compute(
    counts: dict[str, int],
    mass: dict | None = None,
    prices: PriceService | None = None,
    now: datetime | None = None,
) -> PmdiResult:
    """Component counts plus an optional measured mass to a PMDI signal.

    Fails closed the whole way down. A detected class with no cited composition
    contributes nothing rather than a silent zero, and no price means no
    `pmdi_value` rather than a placeholder figure.

    When only some detected classes could be valued the result is still
    produced, with `completeness` PARTIAL_ESTIMATE and `not_valued` naming what
    is missing and why. The totals then cover the valued components only. That
    is a fact about the evidence and nothing more: no bin follows from it here,
    and `app.decision` holds any policy that might.
    """
    now = datetime.now(UTC) if now is None else now
    estimate = materials.estimate(counts, mass)
    if not estimate.get("available"):
        return _unavailable(
            counts,
            mass,
            estimate.get("reason", "No material estimate."),
            not_valued=estimate.get("not_valued"),
        )
    completeness = estimate.get("completeness", materials.COMPLETE)

    amounts = _amounts_by_metal(estimate)
    precious = {m: a for m, a in amounts.items() if m in PRECIOUS_METALS}
    base = {m: a for m, a in amounts.items() if m in BASE_METALS}
    other = {m: a for m, a in amounts.items() if m not in PRECIOUS_METALS + BASE_METALS}

    prices = PriceService.from_config() if prices is None else prices
    quotes = prices.prices(sorted(precious), now=now) if precious else {}

    # The value is produced only when every contributing metal has a figure. A
    # total that quietly drops the metal it could not price still reads as a
    # total, which is how an understated valuation gets quoted as a real one.
    priced = {m: q for m, q in quotes.items() if q.has_number}
    value: float | None = None
    currency: str | None = None
    price_status = PriceStatus.UNAVAILABLE
    if precious and len(priced) == len(precious):
        currencies = {q.currency for q in priced.values()}
        if len(currencies) > 1:
            price_status = PriceStatus.ERROR
        else:
            currency = currencies.pop()
            value = sum(precious[m].grams * priced[m].price_per_gram for m in precious)
            price_status = (
                PriceStatus.STALE
                if any(q.status is PriceStatus.STALE for q in priced.values())
                else next(iter(priced.values())).status
            )

    statuses = [OverallStatus.ESTIMATED]
    if mass_status(mass) is MassStatus.SIMULATED:
        statuses.append(OverallStatus.SIMULATED)
    if price_status is PriceStatus.STALE:
        statuses.append(OverallStatus.STALE)

    return PmdiResult(
        available=True,
        counts=dict(counts),
        evidence_status=(
            EvidenceStatus.SUPPORTED
            if completeness == materials.COMPLETE
            else EvidenceStatus.PARTIAL
        ),
        completeness=completeness,
        valued=list(estimate.get("valued") or []),
        not_valued=list(estimate.get("not_valued") or []),
        overall_status=_worst(statuses),
        mass_g=(mass or {}).get("grams"),
        mass_status=mass_status(mass),
        precious=precious,
        base=base,
        other=other,
        prices=quotes,
        pmdi_value=value,
        currency=currency,
        price_status=price_status,
        evidence_sources=list(estimate.get("evidence", [])),
        confidence=estimate.get("confidence"),
        reason=" ".join(
            r
            for r in (
                estimate.get("reason"),
                None
                if value is not None
                else "No PMDI value: " + _price_gap_reason(precious, quotes, price_status),
            )
            if r
        )
        or None,
    )


def _price_gap_reason(precious, quotes, price_status: PriceStatus) -> str:
    if not precious:
        return "the cited evidence names no precious metal for these components."
    if price_status is PriceStatus.ERROR:
        return "the price source mixes currencies; refusing to add them into one total."
    missing = sorted(m for m, q in quotes.items() if not q.has_number)
    detail = quotes[missing[0]].reason if missing else "no price available"
    return f"no price for {', '.join(missing)}. {detail}"
