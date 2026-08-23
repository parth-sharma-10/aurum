"""Integrity tests for the material reference database.

These are not tests of the *science* — no test here can tell you whether 4.71 mg
of gold per BGA package is true. They test the properties that make the claim
auditable: that every figure names a source, that the source resolves to a real
bibliography entry, that units are declared, that unit conversions are
arithmetically right, and above all that a composition figure never turns itself
into a recovery figure.

The last one is the point of the whole layer. "This connector contains gold" and
"this gold can be recovered" are different claims, and a system that silently
promotes the first into the second produces valuations that cannot be defended.
"""

from __future__ import annotations

import re

import yaml

from app import materials
from app.batch import recovery_estimate

DB = materials.load()
SOURCES = materials.load_sources()
EVIDENCE = DB.get("evidence", [])


class TestShippedDatabaseIsValid:
    def test_the_shipped_database_passes_every_integrity_rule(self):
        """One assertion covering the whole rule set, with readable output."""
        assert materials.validate(DB, SOURCES) == []

    def test_the_database_is_not_empty(self):
        assert EVIDENCE, "an empty database would disable estimation silently"

    def test_the_reference_file_is_well_formed_yaml(self):
        assert isinstance(yaml.safe_load(materials.REFERENCE.read_text()), dict)

    def test_the_source_manifest_is_well_formed_yaml(self):
        doc = yaml.safe_load(materials.SOURCES.read_text())
        assert isinstance(doc, dict) and doc["sources"]


class TestEveryValueIsTraceable:
    def test_every_evidence_id_is_unique(self):
        ids = [e["id"] for e in EVIDENCE]
        assert len(ids) == len(set(ids))

    def test_every_evidence_record_names_a_source_in_the_bibliography(self):
        for e in EVIDENCE:
            assert e.get("source") in SOURCES, f"{e['id']} cites an unknown source"

    def test_every_source_has_a_title_and_a_doi_or_url(self):
        for sid, src in SOURCES.items():
            assert src.get("title"), f"{sid} has no title"
            assert src.get("doi") or src.get("url"), f"{sid} has neither DOI nor URL"

    def test_every_database_record_points_at_an_evidence_id(self):
        """The components map may only reference; it may never carry a figure."""
        index = materials.evidence_index(DB)
        for cls, spec in DB["components"].items():
            for sub, body in spec["subtypes"].items():
                for metal, eid in (body.get("composition") or {}).items():
                    assert eid in index, f"{cls}/{sub}/{metal} -> unknown {eid}"
                    assert not isinstance(eid, int | float), "a raw number bypassed the evidence id"

    def test_every_bibliography_entry_lists_the_evidence_it_supports(self):
        by_source: dict[str, set[str]] = {}
        for e in EVIDENCE:
            by_source.setdefault(e["source"], set()).add(e["id"])
        for factor in DB["recovery"]["factors"]:
            by_source.setdefault(factor["source"], set()).add(factor["id"])
        for sid, src in SOURCES.items():
            assert set(src.get("evidence_ids") or []) == by_source.get(sid, set()), (
                f"{sid}: the manifest's evidence_ids disagree with the database"
            )

    def test_a_source_whose_full_text_was_not_read_supplies_no_value(self):
        """An unread paper may be cited as context, never as a measurement."""
        for sid, src in SOURCES.items():
            if not src.get("full_text_read"):
                assert not src.get("evidence_ids"), (
                    f"{sid} was never read in full but supplies numeric evidence"
                )
                assert not [e for e in EVIDENCE if e["source"] == sid]


class TestValuesAreWellFormed:
    def test_no_value_is_negative(self):
        for e in EVIDENCE:
            assert e["value"] >= 0, f"{e['id']} is negative"

    def test_every_value_declares_a_unit(self):
        for e in EVIDENCE:
            assert e.get("unit"), f"{e['id']} has no unit"

    def test_every_confidence_is_one_of_the_three_allowed_levels(self):
        for e in EVIDENCE:
            assert e["confidence"] in ("high", "medium", "low")

    def test_every_record_preserves_its_original_measurement(self):
        for e in EVIDENCE:
            assert e.get("original_unit"), f"{e['id']} lost the unit it was published in"
            assert e.get("original_value") is not None, f"{e['id']} lost its published value"

    def test_a_range_actually_contains_its_own_typical_value(self):
        for e in EVIDENCE:
            lo, hi, v = e.get("minimum"), e.get("maximum"), e["value"]
            if lo is not None:
                assert lo <= v, f"{e['id']}: value below its minimum"
            if hi is not None:
                assert v <= hi, f"{e['id']}: value above its maximum"

    def test_aggregated_evidence_is_never_labelled_high_confidence(self):
        for e in EVIDENCE:
            if e["evidence_type"] == "aggregated":
                assert e["confidence"] != "high", (
                    f"{e['id']}: an aggregation of other studies is not a direct measurement"
                )


class TestUnitConversions:
    """Conversions are re-derived here rather than trusted."""

    def test_percent_conversions_are_exact(self):
        for e in EVIDENCE:
            if e.get("original_unit") == "%":
                assert e["value"] == e["original_value"] * 10_000, (
                    f"{e['id']}: {e['original_value']}% should be "
                    f"{e['original_value'] * 10_000} mg/kg, not {e['value']}"
                )

    def test_ppm_and_mg_per_kg_are_the_same_number(self):
        for e in EVIDENCE:
            if e.get("original_unit") == "ppm":
                assert e["value"] == e["original_value"]
                assert e["unit"] == "mg/kg"

    def test_kilogram_conversions_are_exact(self):
        for e in EVIDENCE:
            if e.get("original_unit") == "kg":
                assert e["value"] == e["original_value"] * 1000

    def test_one_milligram_per_kilogram_is_one_gram_per_tonne(self):
        """The identity the database's `units` block asserts."""
        assert (1 / 1_000_000) * 1_000_000 == 1.0

    def test_a_per_piece_figure_in_milligrams_becomes_grams(self):
        est = recovery_estimate({"CPU": 1000})
        # 1000 pieces x 4.71 mg = 4710 mg = 4.71 g
        assert est["material_estimate"]["Au"]["typical_g"] == 4.71


class TestCompositionNeverBecomesRecovery:
    def test_the_shipped_database_offers_no_applicable_recovery_factor(self):
        status = materials.recovery_status(DB)
        assert status["available"] is False
        assert status["reason"]

    def test_recovery_factors_on_file_are_still_disclosed(self):
        """Refusing to apply a figure is not the same as hiding it."""
        assert materials.recovery_status(DB)["cited_factors_on_file"]

    def test_an_available_composition_estimate_still_refuses_recovery(self):
        est = recovery_estimate({"CPU": 2})
        assert est["available"] is True
        assert est["recovery"]["available"] is False

    def test_no_recovery_factor_is_derived_from_a_composition_record(self):
        evidence_ids = {e["id"] for e in EVIDENCE}
        for factor in DB["recovery"]["factors"]:
            assert factor["id"] not in evidence_ids
            assert "recovery_rate" in factor and "value" not in factor

    def test_every_recovery_factor_states_whether_it_applies_to_a_detection(self):
        for factor in DB["recovery"]["factors"]:
            assert isinstance(factor.get("applies_to_detection"), bool)
            if not factor["applies_to_detection"]:
                assert factor.get("applies_reason"), f"{factor['id']}: a refusal has to say why"

    def test_recovery_rates_are_percentages_within_range(self):
        for factor in DB["recovery"]["factors"]:
            assert factor["unit"] == "%"
            assert 0 <= factor["recovery_rate"] <= 100

    def test_a_composition_figure_is_never_copied_into_a_recovery_rate(self):
        rates = {f["recovery_rate"] for f in DB["recovery"]["factors"]}
        values = {e["value"] for e in EVIDENCE}
        assert not (rates & values), "a recovery rate equals a composition value verbatim"


class TestEstimationFailsClosed:
    def test_a_class_with_no_cited_composition_blocks_the_estimate(self):
        """RAM has no composition data, and that must stop the whole estimate."""
        est = recovery_estimate({"RAM": 4})
        assert est["available"] is False
        assert "RAM" in est["reason"]
        assert "components" not in est
        assert "material_estimate" not in est

    def test_one_unsupported_class_yields_a_partial_estimate_that_says_so(self):
        """The CPU is valued, the RAM is not, and the gap is on the record.

        The property this protects is unchanged from when the whole estimate
        was refused: a total that silently omits RAM must never read as a
        total for CPU + RAM. It is now met by naming the omission rather than
        by withholding the CPU figure that IS defensible.
        """
        est = recovery_estimate({"CPU": 2, "RAM": 1})

        assert est["completeness"] == materials.PARTIAL_ESTIMATE
        assert [v["component"] for v in est["valued"]] == ["CPU"]
        assert [n["component"] for n in est["not_valued"]] == ["RAM"]
        assert "RAM" in est["not_valued"][0]["reason"]
        # Nothing in the totals came from RAM.
        assert {line["component"] for line in est["components"]} == {"CPU"}
        # And the record refuses to call itself a total for the whole object.
        assert "not a total for the whole object" in est["reason"]

    def test_zero_detections_produce_no_estimate(self):
        assert recovery_estimate({"PCB": 0, "RAM": 0})["available"] is False

    def test_a_concentration_is_refused_without_a_mass(self):
        est = recovery_estimate({"PCB": 1})
        assert est["available"] is False
        assert "mass" in est["reason"]

    def test_a_concentration_is_refused_against_a_simulated_mass(self):
        """An invented mass times a real concentration is an invented quantity."""
        est = recovery_estimate({"PCB": 1}, {"grams": 1500.0, "simulated": True})
        assert est["available"] is False
        assert "SIMULATED" in est["reason"]

    def test_a_concentration_is_accepted_against_a_measured_mass(self):
        est = recovery_estimate({"PCB": 1}, {"grams": 1000.0, "simulated": False})
        assert est["available"] is True
        # 1 kg x 400 mg/kg = 400 mg = 0.4 g
        assert est["material_estimate"]["Au"]["typical_g"] == 0.4

    def test_a_mixed_object_never_attributes_its_whole_mass_to_one_class(self):
        """The double count of section 10, refused at its source.

        One kilogram carrying a board and a processor is not one kilogram of
        board material. The concentration line is dropped - not scaled, not
        guessed at - and the per-piece CPU line, which needs no mass at all,
        still stands.
        """
        est = recovery_estimate({"PCB": 1, "CPU": 1}, {"grams": 1000.0, "simulated": False})

        assert est["completeness"] == materials.PARTIAL_ESTIMATE
        assert [n["component"] for n in est["not_valued"]] == ["PCB"]
        assert "valued twice" in est["not_valued"][0]["reason"]
        # The CPU figure is per-piece and owes nothing to the mass.
        assert {line["component"] for line in est["components"]} == {"CPU"}
        assert all(line["calculation"].startswith("1 x") for line in est["components"])

    def test_no_two_lines_ever_claim_the_same_physical_mass(self):
        """The invariant behind section 11, asserted directly.

        At most one component class may be valued from a concentration against
        any one measured mass. Anything else is the same grams counted twice.
        """
        for counts in ({"PCB": 1, "CPU": 1}, {"PCB": 1, "CPU": 1, "RAM": 2}, {"PCB": 2, "CPU": 1}):
            est = recovery_estimate(counts, {"grams": 842.0, "simulated": False})
            by_mass = {
                line["component"]
                for line in est.get("components", [])
                if "batch mass" in line["calculation"]
            }
            assert len(by_mass) <= 1, f"{counts} valued {by_mass} against one mass"

    def test_a_disabled_database_produces_nothing(self, tmp_path, monkeypatch):
        path = tmp_path / "off.yaml"
        path.write_text(yaml.safe_dump({"enabled": False, "evidence": [], "components": {}}))
        monkeypatch.setattr(materials, "REFERENCE", path)
        assert recovery_estimate({"CPU": 1})["available"] is False

    def test_a_malformed_database_is_reported_not_silently_accepted(self):
        assert materials.validate({}, SOURCES) == ["database contains no evidence records"]
        broken = {
            "evidence": [
                {
                    "id": "X-AU-001",
                    "component": "CPU",
                    "metal": "Au",
                    "quantity": "per_piece",
                    "value": -1,
                    "confidence": "excellent",
                    "evidence_type": "vibes",
                    "source": "NOT-A-SOURCE",
                }
            ],
            "components": {},
        }
        errors = materials.validate(broken, SOURCES)
        assert any("negative value" in e for e in errors)
        assert any("no unit" in e for e in errors)
        assert any("confidence" in e for e in errors)
        assert any("evidence_type" in e for e in errors)
        assert any("bibliography" in e for e in errors)


class TestTheEstimateLabelsItself:
    def test_an_estimate_is_never_labelled_a_measurement(self):
        est = recovery_estimate({"CPU": 1})
        assert est["kind"] == "ESTIMATE"
        assert est["measured_material"]["available"] is False
        assert "ESTIMATE ONLY" in est["disclaimer"]

    def test_a_refusal_carries_the_disclaimer_too(self):
        est = recovery_estimate({"RAM": 1})
        assert "ESTIMATE ONLY" in est["disclaimer"]
        assert est["measured_material"]["available"] is False

    def test_every_estimate_line_carries_its_evidence_and_citation(self):
        for line in recovery_estimate({"CPU": 2, "Connector": 1})["components"]:
            assert line["evidence"]
            assert "10." in line["source"] or "http" in line["source"], "no resolvable identifier"
            assert line["confidence"] in ("high", "medium", "low")

    def test_the_estimate_confidence_is_the_weakest_of_its_inputs(self):
        est = recovery_estimate({"CPU": 1})
        assert est["confidence"] == "medium"  # CPU-AU-001 is medium, not high

    def test_a_bound_is_null_rather_than_partial(self):
        """CPU has no upper-bound subtype, so the combined maximum is unknown."""
        est = recovery_estimate({"CPU": 2, "Connector": 3})
        au = est["material_estimate"]["Au"]
        assert au["max_g"] is None
        assert au["bounds_note"]

    def test_a_bound_that_exists_is_never_below_the_typical_value(self):
        est = recovery_estimate({"Connector": 3})
        au = est["material_estimate"]["Au"]
        assert au["max_g"] >= au["typical_g"]


class TestDocumentationMatchesTheDatabase:
    """The docs are hand-written, so drift is caught here rather than in review."""

    DOC = (materials.ROOT / "docs" / "material-reference.md").read_text()

    def test_every_evidence_id_appears_in_the_documentation(self):
        for e in EVIDENCE:
            assert e["id"] in self.DOC, f"{e['id']} is in the database but not documented"

    def test_every_recovery_factor_id_appears_in_the_documentation(self):
        for factor in DB["recovery"]["factors"]:
            assert factor["id"] in self.DOC, f"{factor['id']} is undocumented"

    def test_the_documentation_invents_no_evidence_ids(self):
        known = {e["id"] for e in EVIDENCE} | {f["id"] for f in DB["recovery"]["factors"]}
        cited = set(re.findall(r"`((?:PCB|RAM|CPU|CONN)-[A-Z]+-(?:REC-)?\d{3})`", self.DOC))
        assert cited - known == set(), "the documentation cites evidence that does not exist"

    def test_every_documented_value_matches_the_database(self):
        """The evidence table must carry the database's numbers, not stale ones."""
        for e in EVIDENCE:
            row = next(
                (ln for ln in self.DOC.splitlines() if ln.startswith(f"| `{e['id']}`")), None
            )
            assert row, f"{e['id']} has no evidence-table row"
            assert f"| {e['value']:g} |" in row, (
                f"{e['id']}: the documented value differs from the database"
            )

    def test_every_source_doi_or_url_is_documented(self):
        for src in SOURCES.values():
            ident = src.get("doi") or src["url"]
            assert ident in self.DOC, f"{src['id']} is not cited in the documentation"


class TestRamGenerationSlots:
    """The DDR subtypes are prepared, empty, and unreachable from an image.

    They exist so that finding one citable figure is a one-line change rather
    than a schema change. They must not become a way for a generation to be
    assumed: Aurum cannot read DDR2 from DDR4 in an RGB frame, and a subtype
    that could be selected by appearance would be exactly the invented
    composition this database exists to prevent.
    """

    @staticmethod
    def ram():
        return materials.load()["components"]["RAM"]

    def test_every_generation_has_a_slot(self):
        subtypes = self.ram()["subtypes"]
        assert {"ddr2_module", "ddr3_module", "ddr4_module", "ddr5_module"} <= set(subtypes)
        for name in ("ddr2_module", "ddr3_module", "ddr4_module", "ddr5_module"):
            assert subtypes[name]["generation"]

    def test_no_slot_carries_a_composition_figure(self):
        for name, body in self.ram()["subtypes"].items():
            assert not body.get("composition"), f"{name} acquired a figure without a citation"

    def test_a_detection_resolves_to_the_unspecified_module(self):
        """The generation is not guessed, so the general entry is what applies."""
        assert self.ram()["default_subtype"] == "dimm_module"

    def test_ram_still_fails_closed_with_the_slots_in_place(self):
        est = recovery_estimate({"RAM": 4}, {"grams": 120.0, "simulated": False})
        assert est["available"] is False
        assert est["completeness"] == materials.INSUFFICIENT_EVIDENCE
        assert "material_estimate" not in est

    def test_the_shipped_database_still_validates(self):
        assert materials.validate(materials.load()) == []
