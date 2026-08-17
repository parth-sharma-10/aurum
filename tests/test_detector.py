"""Tests for detector configuration that does not need trained weights.

The inference size is the one setting where a wrong default is invisible: the
model still runs, still emits plausible boxes, and simply scores worse. This
model measures 0.806 mAP@50 at its trained 512 px and 0.742 at 640, so a
hardcoded 640 quietly cost 6.4 points until it was measured. These tests pin the
rule that the checkpoint decides.
"""

from __future__ import annotations

from types import SimpleNamespace

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
