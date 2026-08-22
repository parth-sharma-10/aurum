"""The material reference layer: detected components -> cited material figures.

Aurum Vision identifies component classes. It cannot see composition, so any
material figure has to come from published data rather than from the image.
This module owns that data's contract:

    configs/material_reference.yaml     the figures, each naming an evidence id
    docs/sources/material_sources.yaml  the bibliography those ids resolve to

Three quantities are kept apart on purpose, because conflating them is how a
recycling estimate turns into a false claim:

    composition   how much metal is present, from a cited measurement
    recovery      how much of it a cited process actually got out
    measurement   what Aurum measured about this batch: nothing but counts

`estimate()` fails closed. A detected class with no cited figure blocks the
whole estimate rather than contributing a silent zero, because a total that
quietly omits a component still reads as a total.

Two evidence shapes exist and they are not interchangeable:

    per_piece      mg of metal in one component -> multiply by the count
    concentration  mg of metal per kg of material -> needs a mass first

Only a *measured* batch mass may drive a concentration, and only when one class
is present, since a mixed batch's mass cannot be attributed to one of them.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = ROOT / "configs" / "material_reference.yaml"
SOURCES = ROOT / "docs" / "sources" / "material_sources.yaml"

VALID_CONFIDENCE = ("high", "medium", "low")
VALID_QUANTITY = ("per_piece", "concentration", "mass")
VALID_EVIDENCE_TYPE = ("measured", "aggregated")

# Price configs and assayers talk about "gold", not "Au". The estimate carries
# both so a record stays readable and `app.pricing` still has a material name it
# can quote against.
METAL_NAMES = {
    "Au": "gold",
    "Ag": "silver",
    "Pd": "palladium",
    "Pt": "platinum",
    "Cu": "copper",
    "Ni": "nickel",
    "Sn": "tin",
    "Al": "aluminium",
}


def load(path: Path | None = None) -> dict:
    """The reference database, or an empty dict when it is absent."""
    path = REFERENCE if path is None else path
    if not path.exists():
        return {}
    import yaml

    return yaml.safe_load(path.read_text()) or {}


def load_sources(path: Path | None = None) -> dict[str, dict]:
    """The bibliography, keyed by source id."""
    path = SOURCES if path is None else path
    if not path.exists():
        return {}
    import yaml

    doc = yaml.safe_load(path.read_text()) or {}
    return {s["id"]: s for s in doc.get("sources", [])}


def evidence_index(db: dict) -> dict[str, dict]:
    return {e["id"]: e for e in db.get("evidence", [])}


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def validate(db: dict, sources: dict[str, dict] | None = None) -> list[str]:
    """Every integrity rule the database must satisfy, as a list of failures.

    Returned rather than raised so a caller can report all problems at once;
    tests assert the list is empty. An empty database is invalid — a file that
    silently loads as nothing would disable estimation without saying so.
    """
    sources = load_sources() if sources is None else sources
    errors: list[str] = []
    records = db.get("evidence") or []
    if not records:
        return ["database contains no evidence records"]

    seen: set[str] = set()
    for e in records:
        eid = e.get("id")
        if not eid:
            errors.append(f"evidence record without an id: {e!r}")
            continue
        if eid in seen:
            errors.append(f"{eid}: duplicate evidence id")
        seen.add(eid)

        value = e.get("value")
        if value is None:
            errors.append(f"{eid}: no value")
        elif not isinstance(value, int | float):
            errors.append(f"{eid}: value is not numeric ({value!r})")
        elif value < 0:
            errors.append(f"{eid}: negative value ({value})")

        if not e.get("unit"):
            errors.append(f"{eid}: no unit")
        if e.get("quantity") not in VALID_QUANTITY:
            errors.append(f"{eid}: quantity must be one of {VALID_QUANTITY}")
        if e.get("confidence") not in VALID_CONFIDENCE:
            errors.append(f"{eid}: confidence must be one of {VALID_CONFIDENCE}")
        if e.get("evidence_type") not in VALID_EVIDENCE_TYPE:
            errors.append(f"{eid}: evidence_type must be one of {VALID_EVIDENCE_TYPE}")

        src = sources.get(e.get("source"))
        if src is None:
            errors.append(f"{eid}: source {e.get('source')!r} is not in the bibliography")
        else:
            if not src.get("title"):
                errors.append(f"{eid}: source {src.get('id')} has no title")
            if not (src.get("doi") or src.get("url")):
                errors.append(f"{eid}: source {src.get('id')} has neither a DOI nor a URL")

        lo, hi = e.get("minimum"), e.get("maximum")
        for name, bound in (("minimum", lo), ("maximum", hi)):
            if bound is not None and bound < 0:
                errors.append(f"{eid}: negative {name} ({bound})")
        if lo is not None and hi is not None and lo > hi:
            errors.append(f"{eid}: minimum {lo} exceeds maximum {hi}")
        if isinstance(value, int | float):
            if lo is not None and value < lo:
                errors.append(f"{eid}: value {value} is below its own minimum {lo}")
            if hi is not None and value > hi:
                errors.append(f"{eid}: value {value} is above its own maximum {hi}")

    index = {e["id"]: e for e in records if e.get("id")}
    for cls, spec in (db.get("components") or {}).items():
        subtypes = spec.get("subtypes") or {}
        default = spec.get("default_subtype")
        if default not in subtypes:
            errors.append(f"{cls}: default_subtype {default!r} is not a defined subtype")
        for sub, body in subtypes.items():
            refs = dict(body.get("composition") or {})
            if body.get("mass"):
                refs["__mass__"] = body["mass"]
            for metal, eid in refs.items():
                ref = index.get(eid)
                if ref is None:
                    errors.append(f"{cls}/{sub}/{metal}: unknown evidence id {eid!r}")
                    continue
                if metal != "__mass__" and ref.get("metal") != metal:
                    errors.append(
                        f"{cls}/{sub}/{metal}: {eid} is evidence for {ref.get('metal')!r}"
                    )
                if ref.get("component") != cls:
                    errors.append(f"{cls}/{sub}/{metal}: {eid} belongs to {ref.get('component')!r}")
            upper = body.get("upper_bound_subtype")
            if upper is not None and upper not in subtypes:
                errors.append(f"{cls}/{sub}: upper_bound_subtype {upper!r} is not defined")

    for factor in (db.get("recovery") or {}).get("factors") or []:
        fid = factor.get("id", "<no id>")
        if fid in seen:
            errors.append(f"{fid}: recovery id collides with an evidence id")
        seen.add(fid)
        rate = factor.get("recovery_rate")
        if not isinstance(rate, int | float):
            errors.append(f"{fid}: recovery_rate is not numeric")
        elif not 0 <= rate <= 100:
            errors.append(f"{fid}: recovery_rate {rate} is outside 0-100%")
        if factor.get("confidence") not in VALID_CONFIDENCE:
            errors.append(f"{fid}: confidence must be one of {VALID_CONFIDENCE}")
        if factor.get("source") not in sources:
            errors.append(f"{fid}: source {factor.get('source')!r} is not in the bibliography")
        if "applies_to_detection" not in factor:
            errors.append(f"{fid}: must state applies_to_detection")

    return errors


# ---------------------------------------------------------------------------
# estimation
# ---------------------------------------------------------------------------
_PER_PIECE_TO_GRAMS = {"mg": 0.001, "g": 1.0}


def _grams_per_piece(ref: dict) -> float | None:
    """Metal in one component, as grams. None when not a per-piece figure."""
    if ref.get("quantity") != "per_piece":
        return None
    scale = _PER_PIECE_TO_GRAMS.get(ref.get("unit"))
    return None if scale is None else float(ref["value"]) * scale


def _usable_mass_g(mass: dict | None, counts: dict[str, int]) -> tuple[float | None, str]:
    """The batch mass a concentration may be applied to, or None and a reason.

    A simulated reading is refused outright: `app.weight.SimulatedLoadCell`
    exists so the dashboard has something to render, and multiplying an
    invented mass by a real concentration produces an invented quantity that
    looks measured. A mixed batch is refused too, because its mass cannot be
    attributed to one component class.
    """
    classes_present = [c for c, n in counts.items() if n]
    if len(classes_present) > 1:
        return None, (
            "a concentration needs a mass, and this batch mixes "
            f"{sorted(classes_present)}; its total mass cannot be attributed to one class"
        )
    if not mass:
        return None, "a concentration needs a mass, and this batch carries no weight reading"

    # The demonstration fallback. A reading marks itself `mock` only when the
    # session deliberately fabricated it under demo.mock_mass.enabled, and what
    # it produces stays labelled SIMULATED everywhere it surfaces. Any other
    # simulated mass is refused outright, because multiplying an invented mass
    # by a real concentration yields an invented quantity that looks measured.
    demo_mass = bool(mass.get("mock"))
    if mass.get("simulated") and not demo_mass:
        return None, (
            "a concentration needs a mass, and this batch's weight is SIMULATED; "
            "refusing to multiply an invented mass by a real concentration"
        )
    # `simulated: false` is not by itself enough. A reading from real hardware
    # can still be unsettled, or rest on a calibration nobody verified against
    # a second known mass. Readings that predate the measurement path carry no
    # status and keep their previous behaviour.
    status = mass.get("status")
    allowed = {"MEASURED", "SIMULATED"} if demo_mass else {"MEASURED"}
    if status is not None and status not in allowed:
        return None, (
            "a concentration needs a measured mass, and this batch's weight is "
            f"{status}; refusing to multiply an unverified mass by a real concentration"
        )
    grams = mass.get("grams")
    if grams is None and mass.get("kg") is not None:
        grams = float(mass["kg"]) * 1000.0
    if not grams or grams <= 0:
        return None, "a concentration needs a mass, and the weight reading is zero or absent"
    return float(grams), ""


def _component_lines(
    cls: str,
    count: int,
    db: dict,
    index: dict[str, dict],
    mass: dict | None,
    counts: dict,
    sources: dict[str, dict],
) -> tuple[list[dict], str]:
    """Per-metal estimate lines for one class, or an empty list and a reason."""
    spec = (db.get("components") or {}).get(cls)
    if spec is None:
        return [], f"{cls} has no entry in the material reference database"

    subtypes = spec.get("subtypes") or {}
    sub_name = spec.get("default_subtype")
    body = subtypes.get(sub_name) or {}
    composition = body.get("composition") or {}
    if not composition:
        gap = (db.get("gaps") or {}).get(cls) or {}
        return [], (
            f"{cls} has no cited composition data"
            + (f": {' '.join(str(gap['reason']).split())}" if gap.get("reason") else "")
        )

    upper_name = body.get("upper_bound_subtype")
    upper_comp = (subtypes.get(upper_name) or {}).get("composition") or {}

    lines: list[dict] = []
    for metal, eid in sorted(composition.items()):
        ref = index.get(eid)
        if ref is None:
            return [], f"{cls}/{metal} names unknown evidence id {eid!r}"
        if not ref.get("source"):
            return [], f"{cls}/{metal} ({eid}) has no source, so it cannot be cited or audited"
        if not ref.get("unit"):
            return [], f"{cls}/{metal} ({eid}) has no unit"
        per_piece = _grams_per_piece(ref)
        if per_piece is None:
            grams, reason = _usable_mass_g(mass, counts)
            if grams is None:
                return [], f"{cls}/{metal} is given as a concentration and {reason}"
            typical = grams * float(ref["value"]) / 1_000_000.0
            lo = ref.get("minimum")
            hi = ref.get("maximum")
            low = grams * float(lo) / 1_000_000.0 if lo is not None else None
            high = grams * float(hi) / 1_000_000.0 if hi is not None else None
            basis = f"measured batch mass {grams:g} g x {ref['value']} {ref['unit']}"
            evidence_ids = [eid]
        else:
            typical = count * per_piece
            low = None
            high = None
            evidence_ids = [eid]
            upper_eid = upper_comp.get(metal)
            if upper_eid:
                upper_ref = index[upper_eid]
                upper_per_piece = _grams_per_piece(upper_ref)
                if upper_per_piece is not None:
                    high = count * upper_per_piece
                    evidence_ids.append(upper_eid)
            basis = f"{count} x {ref['value']} {ref['unit']} per piece"

        lines.append(
            {
                "component": cls,
                "subtype": sub_name,
                "count": count,
                "metal": metal,
                "material": METAL_NAMES.get(metal, metal.lower()),
                "per_unit": ref.get("value"),
                "per_unit_unit": ref.get("unit"),
                "unit": "g",
                "total": round(typical, 9),
                "total_min": None if low is None else round(low, 9),
                "total_max": None if high is None else round(high, 9),
                "calculation": basis,
                "evidence": evidence_ids,
                "confidence": ref.get("confidence"),
                "source": _citation(ref, eid, sources),
            }
        )
    return lines, ""


def _citation(ref: dict, eid: str, sources: dict[str, dict]) -> str:
    """A one-line citation, so a record stays auditable away from this repo."""
    src = sources.get(ref.get("source"), {})
    authors = src.get("authors") or []
    who = authors[0].split()[-1] if authors else str(ref.get("source"))
    more = " et al." if len(authors) > 1 else ""
    ident = src.get("doi") or src.get("url") or ""
    title = " ".join(str(src.get("title", "")).split())
    return f"[{eid}] {who}{more} ({src.get('year')}). {title}. {src.get('journal')}. {ident}"


def _worst(confidences: list[str]) -> str:
    for level in ("low", "medium", "high"):
        if level in confidences:
            return level
    return "unknown"


def estimate(counts: dict[str, int], mass: dict | None = None, db: dict | None = None) -> dict:
    """Component counts -> estimated material present, or an explicit refusal.

    `mass` is a batch weight record as written by `app.weight.WeightReading`.
    It is only consulted for concentration-based evidence, and only when it is
    a real measurement.
    """
    db = load() if db is None else db
    detected = {c: n for c, n in counts.items() if n}

    if not db or not db.get("enabled"):
        return _unavailable(counts, "The material reference database is absent or disabled.")
    if not detected:
        return _unavailable(counts, "No components were detected, so there is nothing to estimate.")

    index = evidence_index(db)
    sources = load_sources()
    lines: list[dict] = []
    blocked: list[str] = []
    for cls, count in sorted(detected.items()):
        got, reason = _component_lines(cls, count, db, index, mass, detected, sources)
        if not got:
            blocked.append(reason)
        lines.extend(got)

    if blocked:
        return _unavailable(
            counts,
            "Estimation is blocked because not every detected component has usable cited "
            "data. " + " | ".join(blocked),
        )

    # A bound only means something when every contributing line has one. Summing
    # the bounds that happen to exist would produce a "maximum" below the
    # typical value, which is how a range stops being a range and starts being
    # a wrong number.
    totals: dict[str, dict] = {}
    for line in lines:
        agg = totals.setdefault(
            line["metal"], {"typical_g": 0.0, "min_g": 0.0, "max_g": 0.0, "evidence": []}
        )
        agg["typical_g"] += line["total"]
        for key, src in (("min_g", "total_min"), ("max_g", "total_max")):
            if agg[key] is None or line[src] is None:
                agg[key] = None
            else:
                agg[key] += line[src]
        agg["evidence"].extend(line["evidence"])
    for agg in totals.values():
        agg["typical_g"] = round(agg["typical_g"], 9)
        for key in ("min_g", "max_g"):
            agg[key] = None if agg[key] is None else round(agg[key], 9)
        agg["evidence"] = sorted(set(agg["evidence"]))
        agg["bounds_note"] = (
            None
            if agg["min_g"] is not None and agg["max_g"] is not None
            else "A bound is null where at least one contributing component has no cited bound."
        )

    return {
        "available": True,
        "kind": "ESTIMATE",
        "basis": db.get("basis"),
        "detected_components": dict(counts),
        "components": lines,
        "material_estimate": totals,
        "evidence": sorted({e for line in lines for e in line["evidence"]}),
        "confidence": _worst([line["confidence"] for line in lines]),
        "recovery": recovery_status(db),
        "measured_material": NOT_MEASURED,
        "disclaimer": DISCLAIMER,
    }


def recovery_status(db: dict | None = None) -> dict:
    """Whether a cited recovery factor may be applied to a detection.

    Composition never becomes recovery here. A factor is only offered when its
    source measured recovery from a feed matching what Aurum detected, which
    no factor in the shipped database does.
    """
    db = load() if db is None else db
    section = (db.get("recovery") or {}) if db else {}
    factors = section.get("factors") or []
    applicable = [f for f in factors if f.get("applies_to_detection")]
    if not applicable:
        return {
            "available": False,
            "reason": " ".join(
                str(
                    section.get("reason")
                    or "No cited recovery factor available for this component."
                ).split()
            ),
            "cited_factors_on_file": [f.get("id") for f in factors],
            "note": (
                "These factors are recorded for audit. None is applied, because each was "
                "measured on a processed feed rather than on a component as detected."
            ),
        }
    return {
        "available": True,
        "factors": applicable,
        "note": "Recovery is an ESTIMATE of an ESTIMATE and is never a measurement.",
    }


DISCLAIMER = (
    "ESTIMATE ONLY — derived from component counts and published reference "
    "yields. Aurum Vision does not measure precious-metal content; RGB imagery "
    "cannot determine composition."
)

# Aurum never produces this. It is stated in every record so that "we did not
# measure it" is a field a consumer can read, not an absence it has to infer.
NOT_MEASURED = {
    "available": False,
    "reason": (
        "Aurum does not measure material content. Establishing a measured "
        "quantity requires destructive assay or XRF, neither of which is part "
        "of this system."
    ),
}


def _unavailable(counts: dict[str, int], reason: str) -> dict:
    """A refusal carrying no numeric field a UI could mistake for a figure."""
    return {
        "available": False,
        "kind": "ESTIMATE",
        "basis": None,
        "detected_components": dict(counts),
        "measured_material": NOT_MEASURED,
        "reason": reason,
        "disclaimer": DISCLAIMER,
    }
