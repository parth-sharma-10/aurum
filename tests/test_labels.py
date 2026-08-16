"""Tests for label normalization.

The property that matters is totality: every source label either maps or is
explicitly dropped. A label that silently disappears produces a dataset that
looks fine and trains a model that quietly cannot see a class.
"""

from __future__ import annotations

import textwrap

import pytest

from ml.labels import UnknownLabelError, load_label_map, normalize_key


class TestNormalizeKey:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("RAM stick", "ram_stick"),
            ("ram_stick", "ram_stick"),
            ("Ram-Stick", "ram_stick"),
            ("  RAM   STICK  ", "ram_stick"),
            ("Electrolytic Capacitor", "electrolytic_capacitor"),
            ("printed-circuit-board", "printed_circuit_board"),
            ("iC", "ic"),
        ],
    )
    def test_variants_collapse_to_one_key(self, raw, expected):
        assert normalize_key(raw) == expected

    def test_punctuation_only_label_does_not_crash(self):
        assert normalize_key("-") == ""


class TestRealLabelMap:
    @staticmethod
    @pytest.fixture(scope="class")
    def lm():
        return load_label_map()

    def test_expected_classes(self, lm):
        assert lm.classes == ["PCB", "RAM", "CPU", "Connector"]

    @pytest.mark.parametrize(
        "label,expected",
        [
            ("motherboard", "PCB"),
            ("PCB", "PCB"),
            ("printed_circuit_board", "PCB"),
            ("ram_stick", "RAM"),
            ("DDR4", "RAM"),
            ("memory", "RAM"),
            ("cpu", "CPU"),
            ("processor", "CPU"),
            ("connector", "Connector"),
            ("ram_slot", "Connector"),
            ("gpu_slot", "Connector"),
            ("rear_io", "Connector"),
        ],
    )
    def test_mappings(self, lm, label, expected):
        assert lm.resolve(label) == expected

    @pytest.mark.parametrize(
        "label",
        [
            "gpu",
            "graphics_card",
            "psu",
            "ssd",
            "hdd",
            "cpu_cooler",
            "resistor",
            "capacitor",
            "battery",
            "keyboard",
            "pads",
        ],
    )
    def test_dropped_labels_resolve_to_none(self, lm, label):
        assert lm.resolve(label) is None
        assert lm.drop_reason(label), f"{label} must carry a written reason"

    def test_ic_is_not_merged_into_cpu(self, lm):
        """An IC is any integrated circuit; calling it a CPU corrupts counts."""
        assert lm.resolve("IC") is None
        assert lm.resolve("integrated_circuit") is None

    def test_gpu_is_not_merged_into_pcb(self, lm):
        """A graphics card presents a cooler shroud, not an exposed board."""
        assert lm.resolve("gpu") is None
        assert "shroud" in lm.drop_reason("gpu").lower()

    def test_unknown_label_raises_rather_than_vanishing(self, lm):
        with pytest.raises(UnknownLabelError) as exc:
            lm.resolve("flux_capacitor")
        assert "flux_capacitor" in str(exc.value)

    def test_class_names_are_their_own_aliases(self, lm):
        for c in lm.classes:
            assert lm.resolve(c) == c

    def test_every_class_has_a_definition(self, lm):
        for c in lm.classes:
            assert lm.definitions.get(c, "").strip(), f"{c} has no definition"

    def test_class_to_index_is_stable_and_dense(self, lm):
        idx = lm.class_to_index
        assert sorted(idx.values()) == list(range(len(lm.classes)))


class TestMalformedConfigs:
    def _write(self, tmp_path, body: str):
        p = tmp_path / "labels.yaml"
        p.write_text(textwrap.dedent(body))
        return p

    def test_alias_in_two_classes_is_rejected(self, tmp_path):
        p = self._write(
            tmp_path,
            """
            version: "t"
            aurum_classes: [PCB, RAM]
            definitions: {}
            mappings:
              PCB: [board]
              RAM: [board]
            drop: {}
        """,
        )
        with pytest.raises(ValueError, match="maps to both"):
            load_label_map(p)

    def test_label_both_mapped_and_dropped_is_rejected(self, tmp_path):
        p = self._write(
            tmp_path,
            """
            version: "t"
            aurum_classes: [PCB]
            definitions: {}
            mappings:
              PCB: [board]
            drop:
              board: cannot be both
        """,
        )
        with pytest.raises(ValueError, match="both"):
            load_label_map(p)

    def test_mapping_to_undeclared_class_is_rejected(self, tmp_path):
        p = self._write(
            tmp_path,
            """
            version: "t"
            aurum_classes: [PCB]
            definitions: {}
            mappings:
              RAM: [ddr4]
            drop: {}
        """,
        )
        with pytest.raises(ValueError, match="not"):
            load_label_map(p)
