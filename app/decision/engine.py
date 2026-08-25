"""The A/B/C decision engine — sorting policy, kept apart from the evidence.

    material evidence -> PMDI / valuation -> DECISION POLICY -> A / B / C -> routing

PMDI says what the cited evidence implies economically. This module says what
the machine does about it. They are different questions and nothing here
changes a measured or cited quantity: the engine reads the valuation and the
configuration, and writes a decision.

**Bin C is the fail-safe.** It has no servo; an item nobody routed reaches the
end of the belt and falls into it. So every path that cannot be justified ends
at C, and C is reached by doing nothing rather than by acting. C does not mean
worthless. It means Aurum cannot justify routing this item into A or B.

**Every threshold comes from configuration.** There is no class name and no
number in this file's logic. Changing `configs/grading.yaml` changes the
behaviour without touching Python, which is what makes the class-aware policy
replaceable when better evidence arrives.

**On `class_aware`, and why it selects the whole A gate.** On the current cited
data a PCB outranks a CPU on precious-metal fraction — roughly 2 200 ppm
against 110 ppm — because the CPU evidence is gold only. That ranking is left
exactly as the evidence states it. What `class_aware` changes is which gate Bin
A uses:

    class_aware: true    A requires membership of grading.bin_a.preferred_classes.
                         An ENGINEERING SORTING POLICY. A PCB then reaches B
                         despite its higher fraction, which is the intended
                         product behaviour.

    class_aware: false   A is decided by the fraction and value thresholds alone.
                         The purely evidence-driven ordering: a PCB reaches A
                         and a CPU reaches B.

Neither mode is a claim about which component contains more precious metal.

**On evidence completeness, and why it does not decide anything.** A mixed
assembly often yields a PARTIAL_ESTIMATE: the processor is valued from cited
per-piece data while the board's per-kilogram figure is refused, because the
one measured mass covers every component on it. That is reported here in
`signals.evidence_completeness`, alongside which components were and were not
valued - and it is deliberately NOT wired to a bin.

There is no existing requirement that an incomplete estimate may not reach the
premium stream, and inventing one would put a sorting policy inside the
evidence layer where nobody could configure it. The ladder below runs on the
numbers the evidence does support. If a completeness rule is ever wanted, it
belongs in configs/grading.yaml as an explicit policy, next to class_aware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app import config as config_module
from app import materials
from app.valuation.pmdi import EvidenceStatus, MassStatus, OverallStatus
from app.valuation.prices import PriceStatus
from app.valuation.valuation import Valuation

#: The physical machine: A and B have paddles, C is the end of the belt.
#: `app.routing` owns actuation; this is only the label a decision carries.
#: UNKNOWN is not a place, so it has no paddle - it reaches a bin through
#: `physical_bin` below.
SERVO_FOR_BIN = {"A": "A", "B": "B", "C": None, "UNKNOWN": None}


class Bin(StrEnum):
    """What Aurum decided. Four values, and the fourth is not a place.

    A, B and C are physical destinations. UNKNOWN is the honest answer when
    the evidence does not support any of them: an unreadable detection, a
    class with no cited composition, a mass that could not be measured or is
    not plausible for the class. It reaches a bin by `physical_bin`, which is
    C, because C is where an item goes when nobody does anything.

    Collapsing UNKNOWN into C would put "Aurum graded this and it did not
    qualify" and "Aurum could not read this at all" under one label, and only
    the second is a reason to look at the camera.
    """

    A = "A"
    B = "B"
    C = "C"
    UNKNOWN = "UNKNOWN"


class ReasonCode(StrEnum):
    """Machine-readable justification. Every decision carries exactly one.

    The `UNKNOWN_` codes mean Aurum could not judge the item. The `C_` codes
    mean it judged it and the item did not qualify. Both end up in bin C; only
    the first is a reason to check the camera, the cell or the evidence
    database.
    """

    A_PREFERRED_CLASS = "A_PREFERRED_CLASS"
    A_PRECIOUS_FRACTION = "A_PRECIOUS_FRACTION"
    A_PMDI_VALUE = "A_PMDI_VALUE"

    B_PRECIOUS_FRACTION = "B_PRECIOUS_FRACTION"
    B_BASE_METAL_VALUE = "B_BASE_METAL_VALUE"
    B_SUPPORTED_RECOVERABLE = "B_SUPPORTED_RECOVERABLE"

    #: Aurum could not judge the item. Decision UNKNOWN, physical bin C.
    UNKNOWN_CLASS = "UNKNOWN_CLASS"
    UNKNOWN_MATERIAL = "UNKNOWN_MATERIAL"
    UNKNOWN_EVIDENCE = "UNKNOWN_EVIDENCE"
    UNKNOWN_WEIGHT = "UNKNOWN_WEIGHT"
    UNKNOWN_CONFIDENCE = "UNKNOWN_CONFIDENCE"
    UNKNOWN_DATA = "UNKNOWN_DATA"
    #: The mass is not plausible for the class. Manual inspection.
    UNKNOWN_MASS_ANOMALY = "UNKNOWN_MASS_ANOMALY"

    #: Aurum judged the item and it did not qualify. Decision C.
    C_PRICE_UNAVAILABLE = "C_PRICE_UNAVAILABLE"
    C_BELOW_THRESHOLD = "C_BELOW_THRESHOLD"


THRESHOLD_NOTE = (
    "Grading thresholds are configurable engineering approximations for this "
    "prototype. They are not presented as universally validated scientific "
    "cutoffs. See configs/grading.yaml and docs/pmdi.md."
)


@dataclass
class Decision:
    """One routing decision, with everything needed to explain it.

    Two fields that are deliberately not the same thing:

        decision       what Aurum concluded: A, B, C or UNKNOWN
        physical_bin   where the item physically ends up: A, B or C

    They differ only for UNKNOWN, and that is the whole reason both exist. A
    dashboard shows `UNKNOWN -> C`, an operator knows to look at the item, and
    nothing in the record claims the machine graded something it could not
    read.
    """

    decision: Bin
    reason_code: ReasonCode
    reason: str
    signals: dict = field(default_factory=dict)
    policy: dict = field(default_factory=dict)
    status: OverallStatus = OverallStatus.UNAVAILABLE
    #: Where an UNKNOWN item is physically sent. From `grading.fallback`, so
    #: the fail-safe destination is configuration rather than a constant here.
    fallback_bin: str = "C"

    @property
    def physical_bin(self) -> str:
        """The bin the item actually reaches. UNKNOWN is not a place."""
        return self.fallback_bin if self.decision is Bin.UNKNOWN else str(self.decision)

    @property
    def unknown(self) -> bool:
        return self.decision is Bin.UNKNOWN

    @property
    def target_bin(self) -> str:
        """The routing target. Always a physical bin, never UNKNOWN.

        Named for what `app.routing` consumes: the scheduler needs somewhere to
        send the item, and "I could not tell" is not somewhere.
        """
        return self.physical_bin

    @property
    def servo(self) -> str | None:
        """The paddle this bin uses, or None for the fail-safe end of the belt."""
        return SERVO_FOR_BIN[self.physical_bin]

    def as_dict(self) -> dict:
        return {
            "decision": str(self.decision),
            "reason_code": str(self.reason_code),
            "reason": self.reason,
            "target_bin": self.target_bin,
            "physical_bin": self.physical_bin,
            "physical_fallback": self.fallback_bin if self.unknown else None,
            "unknown": self.unknown,
            "servo": self.servo,
            "signals": dict(self.signals),
            "policy": dict(self.policy),
            "status": str(self.status),
            "threshold_note": THRESHOLD_NOTE,
            "decision_note": (
                "UNKNOWN is a decision state, not a destination. Aurum could not "
                f"judge this item and it goes to bin {self.fallback_bin}, which is "
                "reached by nobody doing anything."
                if self.unknown
                else None
            ),
        }


def class_support(component_class: str, db: dict | None = None) -> dict:
    """What the evidence database can say about a class, structurally.

    Derived from the database rather than from a hardcoded list, so a class
    becomes supported the moment cited composition is added for it — and stops
    being supported if it is removed.
    """
    db = materials.load() if db is None else db
    spec = (db.get("components") or {}).get(component_class)
    if spec is None:
        return {"known": False, "has_composition": False, "requires_mass": False}

    subtypes = spec.get("subtypes") or {}
    default = subtypes.get(spec.get("default_subtype")) or {}
    composition = default.get("composition") or {}
    index = materials.evidence_index(db)
    requires_mass = any(
        index.get(eid, {}).get("quantity") == "concentration" for eid in composition.values()
    )
    return {
        "known": True,
        "has_composition": bool(composition),
        "requires_mass": requires_mass,
    }


def mass_range(component_class: str | None, cfg: config_module.Config) -> tuple | None:
    """The plausible mass window for a class, or None if none is configured.

    Derived from the settings table by name, so a class gets a window by
    having one configured rather than by being named in this file.
    """
    key = (component_class or "").lower()
    low, high = (
        f"grading.mass_plausibility.{key}_min_g",
        f"grading.mass_plausibility.{key}_max_g",
    )
    if low not in config_module.SPEC or high not in config_module.SPEC:
        return None
    return cfg[low], cfg[high]


def mass_anomaly(
    component_class: str | None, grams: float | None, cfg: config_module.Config
) -> str | None:
    """Why this mass is not possible for this class, or None if it is.

    Answers one question and refuses the obvious second one. An implausible
    mass says the identity, the mounting or the calibration is wrong; it says
    nothing whatsoever about what the object is made of, and no composition is
    ever inferred from it.
    """
    if not cfg["grading.mass_plausibility.enabled"] or grams is None:
        return None
    window = mass_range(component_class, cfg)
    if window is None:
        return None
    low, high = window
    if grams < low:
        return (
            f"{grams:.4g} g is below the {low:g} g minimum plausible for a "
            f"{component_class}. Nothing is inferred from it: check the detection, "
            "the pan and the calibration factor."
        )
    if grams > high:
        return (
            f"{grams:.4g} g is above the {high:g} g maximum plausible for a "
            f"{component_class}. Nothing is inferred from it: check the detection, "
            "the pan and the calibration factor."
        )
    return None


def _policy(cfg: config_module.Config) -> dict:
    return {
        "class_aware": cfg["grading.policy.class_aware"],
        "price_unavailable_policy": cfg["grading.policy.price_unavailable_policy"],
        "preferred_classes": list(cfg["grading.bin_a.preferred_classes"]),
        "bin_a_minimum_precious_fraction_ppm": cfg["grading.bin_a.minimum_precious_fraction_ppm"],
        "bin_a_minimum_confidence": cfg["grading.bin_a.minimum_confidence"],
        "bin_a_minimum_precious_value": _plain(cfg["grading.bin_a.minimum_precious_value"]),
        "bin_b_minimum_precious_fraction_ppm": cfg["grading.bin_b.minimum_precious_fraction_ppm"],
        "bin_b_minimum_confidence": cfg["grading.bin_b.minimum_confidence"],
        "fallback": cfg["grading.fallback"],
        "mass_plausibility": cfg["grading.mass_plausibility.enabled"],
    }


def _plain(value):
    """UNMEASURED serialises as its own name, never as a number."""
    return None if value is config_module.UNMEASURED else value


def _signals(component_class, confidence, valuation: Valuation | None, applied) -> dict:
    pmdi = valuation.pmdi if valuation else None
    return {
        "component_class": component_class,
        "confidence": confidence,
        "confidence_threshold_applied": applied,
        "precious_mass_fraction_ppm": pmdi.precious_mass_fraction_ppm if pmdi else None,
        "precious_mass_g": pmdi.precious_mass_g if pmdi and pmdi.available else None,
        "pmdi_value": pmdi.pmdi_value if pmdi else None,
        "base_value": valuation.base_value if valuation else None,
        "currency": valuation.currency if valuation else None,
        "price_status": str(pmdi.price_status) if pmdi else str(PriceStatus.UNAVAILABLE),
        "mass_status": str(pmdi.mass_status) if pmdi else str(MassStatus.UNMEASURED),
        "mass_g": pmdi.mass_g if pmdi else None,
        "evidence_status": str(pmdi.evidence_status) if pmdi else str(EvidenceStatus.MISSING),
        # Reported for the operator and for any future policy. Nothing in this
        # module branches on it - see the module docstring.
        "evidence_completeness": pmdi.completeness if pmdi else materials.INSUFFICIENT_EVIDENCE,
        "components_valued": [dict(v) for v in pmdi.valued] if pmdi else [],
        "components_not_valued": [dict(n) for n in pmdi.not_valued] if pmdi else [],
        "evidence_sources": list(pmdi.evidence_sources) if pmdi else [],
        "evidence_confidence": pmdi.confidence if pmdi else None,
    }


def _plausibility(component_class, valuation, cfg) -> dict:
    """What the mass window said, reported whether or not it fired."""
    grams = valuation.pmdi.mass_g if valuation else None
    window = mass_range(component_class, cfg)
    anomaly = mass_anomaly(component_class, grams, cfg)
    return {
        "checked": bool(cfg["grading.mass_plausibility.enabled"]) and window is not None,
        "mass_g": grams,
        "min_g": None if window is None else window[0],
        "max_g": None if window is None else window[1],
        "plausible": anomaly is None,
        "reason": anomaly,
        "basis": (
            "ENGINEERING APPROXIMATION - a deliberately wide window that catches a "
            "mis-detection or a calibration factor out by a decade, not a claim "
            "about what any component weighs."
        ),
    }


def decide(
    component_class: str | None,
    confidence: float | None,
    valuation: Valuation | None,
    cfg: config_module.Config | None = None,
    db: dict | None = None,
) -> Decision:
    """Evidence plus policy to a bin, following the fixed priority ladder.

    The ladder is walked in order and never short-circuited by a strong signal:
    a high PMDI does not excuse an unmeasured weight or a weak detection.
    """
    cfg = config_module.load() if cfg is None else cfg
    policy = _policy(cfg)
    status = valuation.overall_status if valuation else OverallStatus.UNAVAILABLE

    def out(bin_: Bin, code: ReasonCode, reason: str, applied=None) -> Decision:
        return Decision(
            decision=bin_,
            reason_code=code,
            reason=reason,
            signals={
                **_signals(component_class, confidence, valuation, applied),
                "mass_plausibility": _plausibility(component_class, valuation, cfg),
            },
            policy=policy,
            status=status,
            fallback_bin=policy["fallback"],
        )

    support = class_support(component_class, db) if component_class else {"known": False}

    # 1. Detection validity. A preferred-class policy must never rescue a
    #    detection the model could not stand behind.
    if not component_class or not support["known"]:
        return out(
            Bin.UNKNOWN,
            ReasonCode.UNKNOWN_CLASS,
            f"No cited material profile exists for class {component_class!r}.",
        )
    # `bool` is an int subclass, and True would otherwise pass as a confidence
    # of 1.0. A string or None must route to C rather than raise: bad input is
    # a routing decision, not a crashed conveyor.
    numeric = isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
    if not numeric or not 0.0 <= confidence <= 1.0:
        return out(
            Bin.UNKNOWN,
            ReasonCode.UNKNOWN_DATA,
            f"Detection confidence {confidence!r} is missing, non-numeric, or outside 0..1.",
        )

    # 2. Material evidence. An assembly may be rooted on a container whose own
    #    class carries no composition while its children do, so a valuation
    #    that actually produced an estimate settles the question the per-class
    #    check was asking.
    if not support["has_composition"] and not (valuation and valuation.pmdi.available):
        return out(
            Bin.UNKNOWN,
            ReasonCode.UNKNOWN_MATERIAL,
            f"The evidence database holds no cited composition for {component_class}. "
            "Routing it to a recovery stream would be a guess.",
        )
    if valuation is None:
        return out(Bin.UNKNOWN, ReasonCode.UNKNOWN_EVIDENCE, "No valuation was supplied.")

    # 3. Required measurement. Concentration evidence is meaningless without a
    #    real mass, and a simulated reading is not one.
    mass_status = valuation.weight_status
    # The demonstration fallback lets a SIMULATED mass through this gate, and
    # only this gate. Everything it produces still carries overall_status
    # SIMULATED, so a bin reached this way is visibly reached on a stand-in
    # number rather than a measurement.
    accepted = {MassStatus.MEASURED}
    if cfg["demo.mock_mass.enabled"]:
        accepted.add(MassStatus.SIMULATED)
    if support["requires_mass"] and mass_status not in accepted:
        return out(
            Bin.UNKNOWN,
            ReasonCode.UNKNOWN_WEIGHT,
            f"{component_class} composition is cited as a concentration, which needs a "
            f"measured mass; this item's mass is {mass_status}.",
        )

    # 3b. Mass plausibility. A mass that cannot belong to this class means the
    #     identity, the mounting or the calibration is wrong, and none of those
    #     is something to route on. It never implies a composition.
    anomaly = mass_anomaly(component_class, valuation.pmdi.mass_g, cfg)
    if anomaly is not None:
        return out(Bin.UNKNOWN, ReasonCode.UNKNOWN_MASS_ANOMALY, f"MASS_ANOMALY: {anomaly}")

    # 4. Data validity.
    if valuation.evidence_status is EvidenceStatus.MISSING or not valuation.pmdi.available:
        return out(
            Bin.UNKNOWN,
            ReasonCode.UNKNOWN_EVIDENCE,
            valuation.reason or "The material estimate is unavailable.",
        )
    price_current = valuation.pmdi.price_status in (
        PriceStatus.LIVE,
        PriceStatus.TEST,
        PriceStatus.SIMULATED,
    )
    if policy["price_unavailable_policy"] == "route_to_c" and not price_current:
        return out(
            Bin.C,
            ReasonCode.C_PRICE_UNAVAILABLE,
            "Policy price_unavailable_policy=route_to_c requires a current price; "
            f"the price status is {valuation.pmdi.price_status}.",
        )

    fraction = valuation.pmdi.precious_mass_fraction_ppm
    value = valuation.pmdi.pmdi_value
    base_value = valuation.base_value

    # 5. Bin A.
    a_conf = policy["bin_a_minimum_confidence"]
    if confidence >= a_conf:
        if policy["class_aware"]:
            if component_class in policy["preferred_classes"]:
                return out(
                    Bin.A,
                    ReasonCode.A_PREFERRED_CLASS,
                    f"{component_class} is a configured premium class and the detection "
                    f"({confidence:.2f}) clears the {a_conf:.2f} threshold. This is an "
                    "engineering sorting policy, not a claim that this class contains "
                    "more precious metal than any other.",
                    a_conf,
                )
        else:
            a_ppm = policy["bin_a_minimum_precious_fraction_ppm"]
            if fraction is not None and fraction >= a_ppm:
                return out(
                    Bin.A,
                    ReasonCode.A_PRECIOUS_FRACTION,
                    f"Precious-metal fraction {fraction:.1f} ppm meets the {a_ppm:.0f} ppm "
                    "premium threshold, from cited contained composition.",
                    a_conf,
                )
            a_value = policy["bin_a_minimum_precious_value"]
            if a_value is not None and value is not None and value >= a_value:
                return out(
                    Bin.A,
                    ReasonCode.A_PMDI_VALUE,
                    f"PMDI value {value:.4g} meets the {a_value:.4g} premium threshold.",
                    a_conf,
                )

    # 6. Bin B. A weaker detection can still justify the general stream.
    b_conf = policy["bin_b_minimum_confidence"]
    if confidence < b_conf:
        return out(
            Bin.UNKNOWN,
            ReasonCode.UNKNOWN_CONFIDENCE,
            f"Detection confidence {confidence:.2f} is below the {b_conf:.2f} minimum "
            "for any routed bin.",
            b_conf,
        )

    b_ppm = policy["bin_b_minimum_precious_fraction_ppm"]
    if fraction is not None and fraction >= b_ppm:
        return out(
            Bin.B,
            ReasonCode.B_PRECIOUS_FRACTION,
            f"Precious-metal fraction {fraction:.1f} ppm meets the {b_ppm:.0f} ppm "
            "recoverable threshold.",
            b_conf,
        )
    if base_value is not None and base_value > 0:
        return out(
            Bin.B,
            ReasonCode.B_BASE_METAL_VALUE,
            f"Cited base-metal content is worth {base_value:.4g} {valuation.currency or ''}".strip()
            + ", which justifies the smelting stream.",
            b_conf,
        )
    if fraction is None:
        # Per-piece evidence with no mass: a density cannot be formed, but the
        # cited content is real and the class is recoverable.
        return out(
            Bin.B,
            ReasonCode.B_SUPPORTED_RECOVERABLE,
            f"{component_class} has cited per-piece composition and no mass was "
            "required; the evidence supports recovery but not the premium stream.",
            b_conf,
        )

    # 7. Fail-safe.
    return out(
        Bin.C,
        ReasonCode.C_BELOW_THRESHOLD,
        f"Precious-metal fraction {fraction:.1f} ppm is below the {b_ppm:.0f} ppm "
        "recoverable threshold and no base-metal value is available.",
        b_conf,
    )
