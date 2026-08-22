"""Valuation subsystem: material evidence -> PMDI -> valuation.

    prices.py      where a metal price came from, and whether it can be trusted
    pmdi.py        the Precious Metal Density Index, per the concept document
    valuation.py   PMDI plus the separate base-metal signal, packaged for audit

No grading logic lives here. `app.decision` consumes what this produces.
"""

from app.valuation.pmdi import (
    BASE_METALS,
    PRECIOUS_METALS,
    EvidenceStatus,
    MassStatus,
    OverallStatus,
    PmdiResult,
)
from app.valuation.pmdi import compute as compute_pmdi
from app.valuation.prices import MetalPrice, PriceService, PriceStatus
from app.valuation.valuation import Valuation
from app.valuation.valuation import value as value_item

__all__ = [
    "BASE_METALS",
    "PRECIOUS_METALS",
    "EvidenceStatus",
    "MassStatus",
    "MetalPrice",
    "OverallStatus",
    "PmdiResult",
    "PriceService",
    "PriceStatus",
    "Valuation",
    "compute_pmdi",
    "value_item",
]
