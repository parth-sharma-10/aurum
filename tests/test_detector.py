"""Tests for detector configuration that does not need trained weights.

The inference size is the one setting where a wrong default is invisible: the
model still runs, still emits plausible boxes, and simply scores worse. This
model measures 0.806 mAP@50 at its trained 512 px and 0.742 at 640, so a
hardcoded 640 quietly cost 6.4 points until it was measured. These tests pin the
rule that the checkpoint decides.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from app.detector import FALLBACK_IMGSZ, resolve_imgsz


def _model(args):
    """Stand-in for a YOLO wrapper, whose .model carries the training args."""
    return SimpleNamespace(model=SimpleNamespace(args=args))


def test_uses_the_size_the_checkpoint_was_trained_at():
    assert resolve_imgsz(_model({"imgsz": 512}), None) == 512


def test_explicit_request_overrides_the_checkpoint():
    assert resolve_imgsz(_model({"imgsz": 512}), 320) == 320


def test_falls_back_when_the_checkpoint_records_no_size():
    assert resolve_imgsz(_model({}), None) == FALLBACK_IMGSZ
    assert resolve_imgsz(_model(None), None) == FALLBACK_IMGSZ


def test_reads_args_exposed_as_an_object_rather_than_a_dict():
    assert resolve_imgsz(_model(SimpleNamespace(imgsz=512)), None) == 512


def test_non_numeric_size_falls_back_instead_of_crashing():
    assert resolve_imgsz(_model({"imgsz": "big"}), None) == FALLBACK_IMGSZ


def test_result_is_an_int_even_when_recorded_as_a_float():
    size = resolve_imgsz(_model({"imgsz": 512.0}), None)
    assert size == 512
    assert isinstance(size, int)


class TestReleaseTrainingDefaults:
    """`python -m ml.train` must rebuild the model the reports describe.

    These defaults drifted once already: the code said 100 epochs / 640 px /
    batch 16 while the evaluated checkpoint was 50 / 512 / 32, so anyone who
    followed the README got a model none of the published metrics applied to.
    The failure is silent — training succeeds and simply produces something
    else — so the canonical values are pinned here against both the metadata
    that ships with the weights and the environment-variable overrides.
    """

    def test_defaults_are_the_release_configuration(self):
        from ml.train import config

        cfg = config()
        assert cfg["epochs"] == 50
        assert cfg["imgsz"] == 512
        assert cfg["batch"] == 32
        assert cfg["patience"] == 15
        assert cfg["seed"] == 1337
        assert cfg["model"] == "yolo11n.pt"

    def test_defaults_match_the_metadata_shipped_with_the_weights(self):
        """The one check that catches drift on either side."""
        import json

        from ml.train import ROOT, config

        meta_path = ROOT / "models" / "aurum_vision_v0_1_meta.json"
        if not meta_path.exists():  # a clean checkout has no weights or metadata
            pytest.skip("model metadata not present")
        meta = json.loads(meta_path.read_text())
        cfg = config()
        assert cfg["imgsz"] == meta["image_size"]
        assert cfg["epochs"] == meta["epochs_requested"]
        assert cfg["batch"] == meta["batch"]
        assert cfg["seed"] == meta["seed"]

    @pytest.mark.parametrize(
        "env,key,expected",
        [
            ("AURUM_EPOCHS", "epochs", 120),
            ("AURUM_IMGSZ", "imgsz", 640),
            ("AURUM_BATCH", "batch", 8),
            ("AURUM_PATIENCE", "patience", 30),
            ("AURUM_SEED", "seed", 7),
        ],
    )
    def test_environment_overrides_still_win(self, monkeypatch, env, key, expected):
        from ml.train import config

        monkeypatch.setenv(env, str(expected))
        assert config()[key] == expected

    def test_model_override_still_wins(self, monkeypatch):
        from ml.train import config

        monkeypatch.setenv("AURUM_MODEL", "yolo11s.pt")
        assert config()["model"] == "yolo11s.pt"

    def test_device_override_still_wins(self, monkeypatch):
        from ml.train import config

        monkeypatch.setenv("AURUM_DEVICE", "cpu")
        assert config()["device"] == "cpu"

    def test_run_name_override_still_wins(self, monkeypatch):
        from ml.train import config

        monkeypatch.setenv("AURUM_RUN", "experiment_a")
        assert config()["name"] == "experiment_a"


class TestArtifactIdentity:
    """A filename does not prove which weights produced a metric; a digest does."""

    def test_artifact_info_reports_digest_and_size(self, tmp_path):
        from ml.train import artifact_info

        f = tmp_path / "weights.pt"
        f.write_bytes(b"not really a checkpoint")
        info = artifact_info(f)
        assert info["filename"] == "weights.pt"
        assert info["size_bytes"] == 23
        assert len(info["sha256"]) == 64
        assert info["sha256"] == hashlib.sha256(b"not really a checkpoint").hexdigest()

    def test_different_content_gives_a_different_digest(self, tmp_path):
        from ml.train import artifact_info

        a, b = tmp_path / "a.pt", tmp_path / "b.pt"
        a.write_bytes(b"aaaa")
        b.write_bytes(b"bbbb")
        assert artifact_info(a)["sha256"] != artifact_info(b)["sha256"]

    def test_shipped_metadata_matches_the_shipped_weights(self):
        """The published digest must be the digest of the file on disk."""
        import json

        from ml.train import ROOT, artifact_info

        meta_path = ROOT / "models" / "aurum_vision_v0_1_meta.json"
        weights = ROOT / "models" / "aurum_vision_v0_1_best.pt"
        if not (meta_path.exists() and weights.exists()):
            pytest.skip("weights or metadata not present in this checkout")
        recorded = json.loads(meta_path.read_text())["artifact"]
        assert recorded == artifact_info(weights)
