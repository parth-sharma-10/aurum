"""Tests for the leakage-control machinery in ml.prepare.

These cover the pieces that decide whether a photograph can appear in both train
and test. They are unit tests over the grouping primitives; `ml.validate` is the
end-to-end check on the real dataset.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ml.prepare import SPLITS, DisjointSet, hamming, source_stem, split_clusters


class TestSourceStem:
    @pytest.mark.parametrize(
        "name,expected",
        [
            # Three Roboflow augmentations of one photograph collapse to one stem.
            ("IMG_1234_jpg.rf.547b0b092e9b4a148a2dfed48da9b131.jpg", "img_1234_jpg"),
            ("IMG_1234_jpg.rf.bffec6007116aec2b8fcf38902e99cad.jpg", "img_1234_jpg"),
            ("IMG_1234_jpg.rf.cb1a771d142534bc32566d132aaf077f.jpg", "img_1234_jpg"),
        ],
    )
    def test_augmented_copies_share_a_stem(self, name, expected):
        assert source_stem(Path(name)) == expected

    def test_distinct_photos_get_distinct_stems(self):
        a = source_stem(Path("PXL_20240610_233638946_jpg.rf.4d143fb1071.jpg"))
        b = source_stem(Path("PXL_20240610_233642488_jpg.rf.6406bd908ca.jpg"))
        assert a != b

    def test_plain_filename_passes_through(self):
        assert source_stem(Path("bench_photo_01.jpg")) == "bench_photo_01"

    def test_case_is_normalized(self):
        assert source_stem(Path("IMG_A.rf.deadbeef99.jpg")) == "img_a"


class TestDisjointSet:
    def test_transitive_merge(self):
        ds = DisjointSet()
        ds.union("a", "b")
        ds.union("b", "c")
        assert ds.find("a") == ds.find("c")

    def test_unrelated_stay_separate(self):
        ds = DisjointSet()
        ds.union("a", "b")
        assert ds.find("a") != ds.find("z")

    def test_find_is_idempotent(self):
        ds = DisjointSet()
        assert ds.find("solo") == ds.find("solo")


class TestHamming:
    def test_identical_hashes(self):
        assert hamming(0xDEADBEEF, 0xDEADBEEF) == 0

    def test_single_bit(self):
        assert hamming(0b1000, 0b1001) == 1

    def test_symmetric(self):
        assert hamming(0xFF00, 0x00FF) == hamming(0x00FF, 0xFF00) == 16


class TestSplitClusters:
    """A cluster must land in exactly one split, and rare classes must spread."""

    CLASSES = ["PCB", "RAM", "CPU", "Connector"]

    def _records(self, spec):
        """spec: {cluster_id: {class: n}} -> record list."""
        out = []
        for cid, counts in spec.items():
            boxes = []
            for c, n in counts.items():
                boxes.extend([(c, 0.5, 0.5, 0.2, 0.2)] * n)
            out.append({"cluster": cid, "boxes": boxes})
        return out

    def test_every_cluster_is_assigned_exactly_once(self):
        spec = {f"c{i}": {"RAM": 1} for i in range(50)}
        assignment = split_clusters(self._records(spec), self.CLASSES, seed=1)
        assert set(assignment) == set(spec)
        assert set(assignment.values()) <= set(SPLITS)

    def test_a_cluster_never_spans_splits(self):
        """Two images in one cluster share the cluster's single assignment."""
        recs = self._records({f"c{i}": {"PCB": 1} for i in range(30)})
        recs += [{"cluster": "c0", "boxes": [("PCB", 0.5, 0.5, 0.2, 0.2)]}]  # duplicate
        assignment = split_clusters(recs, self.CLASSES, seed=1)
        assert isinstance(assignment["c0"], str)

    def test_rare_class_reaches_every_split(self):
        """20 CPU clusters among 200 RAM clusters must not all land in train."""
        spec = {f"ram{i}": {"RAM": 3} for i in range(200)}
        spec.update({f"cpu{i}": {"CPU": 1} for i in range(20)})
        assignment = split_clusters(self._records(spec), self.CLASSES, seed=1)
        cpu_splits = {assignment[f"cpu{i}"] for i in range(20)}
        assert cpu_splits == set(SPLITS), f"CPU only in {cpu_splits}"

    def test_split_proportions_are_roughly_honoured(self):
        spec = {f"c{i}": {"RAM": 1} for i in range(1000)}
        assignment = split_clusters(self._records(spec), self.CLASSES, seed=1)
        n = len(spec)
        for split, frac in SPLITS.items():
            got = sum(1 for v in assignment.values() if v == split) / n
            assert abs(got - frac) < 0.05, f"{split}: {got:.3f} vs {frac}"

    def test_deterministic_for_a_given_seed(self):
        spec = {f"c{i}": {"RAM": 1, "CPU": i % 3} for i in range(60)}
        recs = self._records(spec)
        a = split_clusters(recs, self.CLASSES, seed=7)
        b = split_clusters(recs, self.CLASSES, seed=7)
        assert a == b

    def test_background_only_clusters_are_still_assigned(self):
        spec = {f"bg{i}": {} for i in range(30)}
        assignment = split_clusters(self._records(spec), self.CLASSES, seed=1)
        assert len(assignment) == 30
