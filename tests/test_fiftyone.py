"""Tests for the vision-QA layer.

Split the way the layer is split. The capture side runs inside the live
pipeline and is tested unconditionally, with no FiftyOne, no disk and no
OpenCV. The dataset side needs FiftyOne and is skipped when it is absent —
which is the shipped state, because a 1 GB development dependency is not
something the test suite may require.

The property under test throughout: a failure category is a claim, and no
claim is made that the evidence at hand cannot support.
"""

from __future__ import annotations

import json

import pytest

from tools.fiftyone import dataset as dataset_module
from tools.fiftyone.failures import (
    REVIEW,
    RUNTIME,
    CaptureError,
    FailureCapture,
    VisionFailure,
    classify_frame,
    detection_dict,
    iou,
    is_degenerate,
    read_manifest,
    touches_edge,
)

W, H = 1280, 720


class Box:
    """The tracker's detection shape, without importing the tracker."""

    def __init__(self, track_id, class_name, confidence, xyxy):
        self.track_id = track_id
        self.class_name = class_name
        self.confidence = confidence
        self.xyxy = xyxy


def det(class_name="CPU", confidence=0.95, xyxy=(100, 100, 300, 300), track_id=1) -> Box:
    return Box(track_id, class_name, confidence, xyxy)


def sink(tmp_path, **kwargs) -> FailureCapture:
    """A capture that writes its manifest but never touches an image encoder."""
    kwargs.setdefault("enabled", True)
    kwargs.setdefault("session_id", "AUR-RUN-TEST")
    return FailureCapture(
        directory=tmp_path / "errors", write_frame=lambda path, frame: None, **kwargs
    )


class TestTheTwoKindsOfCategory:
    def test_every_category_is_in_exactly_one_set(self):
        assert set(VisionFailure) == RUNTIME | REVIEW
        assert set() == RUNTIME & REVIEW

    @pytest.mark.parametrize(
        "failure",
        [
            VisionFailure.FALSE_POSITIVE,
            VisionFailure.MISSED_DETECTION,
            VisionFailure.CLASS_CONFUSION,
        ],
    )
    def test_a_comparison_category_cannot_be_claimed_at_run_time(self, failure, tmp_path):
        """These are decided against a label, and the live pipeline has none."""
        with pytest.raises(CaptureError) as exc:
            sink(tmp_path).capture(failure)
        assert "ground truth" in str(exc.value)

    def test_a_runtime_category_is_accepted(self, tmp_path):
        assert sink(tmp_path).capture(VisionFailure.NO_DETECTION) is not None


class TestClassifyingOneFrame:
    def test_a_clean_frame_has_nothing_wrong_with_it(self):
        assert classify_frame([det()], W, H) == []

    def test_an_empty_frame_is_a_no_detection(self):
        [(failure, why)] = classify_frame([], W, H)
        assert failure is VisionFailure.NO_DETECTION
        assert "no detections" in why

    def test_a_weak_detection_is_flagged_with_its_confidence(self):
        found = dict(classify_frame([det(confidence=0.42)], W, H))
        assert VisionFailure.LOW_CONFIDENCE in found
        assert "0.42" in found[VisionFailure.LOW_CONFIDENCE]

    def test_the_review_threshold_is_configurable(self):
        assert classify_frame([det(confidence=0.42)], W, H, low_confidence=0.4) == []

    def test_a_class_with_no_cited_profile_is_an_unknown_object(self):
        found = dict(classify_frame([det(class_name="GPU")], W, H, known_classes={"CPU", "PCB"}))
        assert VisionFailure.UNKNOWN_OBJECT in found

    def test_a_known_class_is_not(self):
        assert classify_frame([det()], W, H, known_classes={"CPU"}) == []

    @pytest.mark.parametrize("box", [(300, 100, 100, 300), (100, 100, 100, 300), None, (1, 2, 3)])
    def test_a_degenerate_box_is_invalid_geometry(self, box):
        found = dict(classify_frame([det(xyxy=box)], W, H))
        assert VisionFailure.INVALID_GEOMETRY in found

    def test_a_box_on_the_frame_edge_is_partial_visibility(self):
        found = dict(classify_frame([det(xyxy=(0, 100, 300, 300))], W, H))
        assert VisionFailure.PARTIAL_VISIBILITY in found

    def test_two_boxes_of_one_class_on_top_of_each_other_are_a_duplicate(self):
        found = dict(
            classify_frame(
                [det(xyxy=(100, 100, 300, 300)), det(track_id=2, xyxy=(102, 102, 302, 302))], W, H
            )
        )
        assert VisionFailure.DUPLICATE_DETECTION in found

    def test_two_different_classes_in_one_place_are_not_a_duplicate(self):
        """A CPU on a board overlaps the board. That is the machine working."""
        found = dict(
            classify_frame(
                [det(class_name="PCB", xyxy=(100, 100, 300, 300)), det(xyxy=(102, 102, 302, 302))],
                W,
                H,
            )
        )
        assert VisionFailure.DUPLICATE_DETECTION not in found

    def test_every_category_returned_is_a_runtime_one(self):
        detections = [det(class_name="GPU", confidence=0.2, xyxy=(0, 0, 300, 300))]
        for failure, _ in classify_frame(detections, W, H, known_classes={"CPU"}):
            assert failure in RUNTIME

    def test_every_finding_names_the_observation_behind_it(self):
        detections = [det(class_name="GPU", confidence=0.2, xyxy=(0, 0, 300, 300))]
        for _, why in classify_frame(detections, W, H, known_classes={"CPU"}):
            assert why and len(why) > 20


class TestGeometryHelpers:
    def test_iou_of_a_box_with_itself_is_one(self):
        assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == pytest.approx(1.0)

    def test_iou_of_disjoint_boxes_is_zero(self):
        assert iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0

    def test_iou_of_a_degenerate_box_is_zero_not_an_error(self):
        assert iou((10, 10, 10, 10), (0, 0, 10, 10)) == 0.0

    def test_a_box_in_the_middle_touches_no_edge(self):
        assert touches_edge((100, 100, 300, 300), W, H) is False

    def test_a_box_at_the_far_edge_touches_it(self):
        assert touches_edge((100, 100, W, 300), W, H) is True

    def test_a_missing_box_is_degenerate(self):
        assert is_degenerate(None) is True


class TestCapture:
    def test_it_is_off_by_default(self, tmp_path):
        capture = FailureCapture(directory=tmp_path)
        assert capture.enabled is False
        assert capture.capture(VisionFailure.NO_DETECTION) is None

    def test_disabled_capture_writes_nothing(self, tmp_path):
        capture = FailureCapture(directory=tmp_path / "errors")
        capture.capture(VisionFailure.NO_DETECTION)
        assert not (tmp_path / "errors").exists()

    def test_a_sample_carries_the_context_the_dataset_will_want(self, tmp_path):
        capture = sink(tmp_path)
        sample = capture.capture(
            VisionFailure.LOW_CONFIDENCE,
            note="0.42 is below the review threshold",
            detections=[det(confidence=0.42)],
            item_id="AUR-ITEM-1",
            decision="UNKNOWN",
            mass_status="SIMULATED",
            price_status="REFERENCE",
        )
        assert sample.session_id == "AUR-RUN-TEST"
        assert sample.item_id == "AUR-ITEM-1"
        assert sample.decision == "UNKNOWN"
        assert sample.predictions[0]["confidence"] == 0.42
        assert sample.timestamp

    def test_ground_truth_is_stored_apart_from_predictions(self, tmp_path):
        sample = sink(tmp_path).capture(
            VisionFailure.LOW_CONFIDENCE,
            detections=[det(class_name="CPU")],
            ground_truth=[det(class_name="PCB")],
        )
        assert sample.predictions[0]["class_name"] == "CPU"
        assert sample.ground_truth[0]["class_name"] == "PCB"

    def test_a_sample_with_no_label_has_an_empty_ground_truth(self, tmp_path):
        assert sink(tmp_path).capture(VisionFailure.NO_DETECTION).ground_truth == []

    def test_the_manifest_is_one_json_object_per_line(self, tmp_path):
        capture = sink(tmp_path)
        capture.capture(VisionFailure.NO_DETECTION)
        capture.capture(VisionFailure.LOW_CONFIDENCE, detections=[det(confidence=0.4)])
        lines = capture.manifest_path.read_text().strip().splitlines()
        assert len(lines) == 2
        assert [json.loads(line)["failure"] for line in lines] == [
            "NO_DETECTION",
            "LOW_CONFIDENCE",
        ]

    def test_the_manifest_reads_back(self, tmp_path):
        capture = sink(tmp_path)
        capture.capture(VisionFailure.NO_DETECTION)
        assert read_manifest(capture.directory)[0]["failure"] == "NO_DETECTION"

    def test_a_truncated_final_line_does_not_lose_the_samples_before_it(self, tmp_path):
        capture = sink(tmp_path)
        capture.capture(VisionFailure.NO_DETECTION)
        with capture.manifest_path.open("a") as handle:
            handle.write('{"failure": "NO_DET')
        assert len(read_manifest(capture.directory)) == 1

    def test_an_absent_manifest_reads_as_empty(self, tmp_path):
        assert read_manifest(tmp_path / "nothing") == []

    def test_the_per_category_limit_stops_a_disk_filling(self, tmp_path):
        capture = sink(tmp_path, per_category_limit=3)
        for _ in range(10):
            capture.capture(VisionFailure.NO_DETECTION)
        assert len(capture.samples) == 3
        assert capture.skipped == 7

    def test_the_limit_is_per_category_not_overall(self, tmp_path):
        capture = sink(tmp_path, per_category_limit=1)
        capture.capture(VisionFailure.NO_DETECTION)
        capture.capture(VisionFailure.INVALID_GEOMETRY)
        assert len(capture.samples) == 2

    def test_capturing_a_frame_records_everything_wrong_with_it(self, tmp_path):
        capture = sink(tmp_path)
        detections = [det(class_name="GPU", confidence=0.2, xyxy=(0, 0, 300, 300))]
        samples = capture.capture_frame(
            frame=object(), detections=detections, width=W, height=H, known_classes={"CPU"}
        )
        assert {str(s.failure) for s in samples} == {
            "LOW_CONFIDENCE",
            "UNKNOWN_OBJECT",
            "PARTIAL_VISIBILITY",
        }

    def test_the_snapshot_counts_by_category(self, tmp_path):
        capture = sink(tmp_path)
        capture.capture(VisionFailure.NO_DETECTION)
        capture.capture(VisionFailure.NO_DETECTION)
        snap = capture.snapshot()
        assert snap["captured"] == 2
        assert snap["by_category"] == {"NO_DETECTION": 2}

    def test_a_detection_object_and_a_dict_convert_the_same_way(self):
        as_object = detection_dict(det())
        as_dict = detection_dict(
            {"track_id": 1, "class_name": "CPU", "confidence": 0.95, "xyxy": (100, 100, 300, 300)}
        )
        assert as_object == as_dict


class TestCoordinateConversion:
    """Pure arithmetic, tested without FiftyOne because it needs none."""

    def test_a_box_becomes_relative_xywh(self):
        assert dataset_module.to_relative((640, 360, 1280, 720), 1280, 720) == [0.5, 0.5, 0.5, 0.5]

    def test_a_full_frame_box_is_the_whole_unit_square(self):
        assert dataset_module.to_relative((0, 0, 1280, 720), 1280, 720) == [0.0, 0.0, 1.0, 1.0]

    def test_a_box_running_off_the_edge_is_clamped_not_rejected(self):
        """A partially visible object is a real detection; FiftyOne needs 0..1."""
        assert dataset_module.to_relative((-50, -50, 1400, 800), 1280, 720) == [
            0.0,
            0.0,
            1.0,
            1.0,
        ]

    @pytest.mark.parametrize(
        ("box", "width", "height"),
        [(None, 1280, 720), ((1, 2, 3), 1280, 720), ((0, 0, 10, 10), 0, 720)],
    )
    def test_a_box_that_cannot_be_converted_raises_rather_than_guessing(self, box, width, height):
        with pytest.raises(ValueError):
            dataset_module.to_relative(box, width, height)


class TestTheSummaryNeedsNothingInstalled:
    def test_an_empty_directory_summarises_as_empty(self, tmp_path):
        report = dataset_module.summary(tmp_path)
        assert report["samples"] == 0
        assert report["labelled"] == 0

    def test_it_counts_by_category(self, tmp_path):
        capture = sink(tmp_path)
        capture.capture(VisionFailure.NO_DETECTION)
        capture.capture(VisionFailure.NO_DETECTION)
        capture.capture(VisionFailure.INVALID_GEOMETRY)
        report = dataset_module.summary(capture.directory)
        assert report["by_category"] == {"NO_DETECTION": 2, "INVALID_GEOMETRY": 1}

    def test_it_says_which_categories_never_appeared(self, tmp_path):
        capture = sink(tmp_path)
        capture.capture(VisionFailure.NO_DETECTION)
        report = dataset_module.summary(capture.directory)
        assert "FALSE_POSITIVE" in report["categories_never_captured_at_runtime"]
        assert "NO_DETECTION" not in report["categories_never_captured_at_runtime"]

    def test_it_reports_whether_fiftyone_is_installed(self, tmp_path):
        assert dataset_module.summary(tmp_path)["fiftyone_installed"] in (True, False)


class TestTheLivePipelineNeverImportsFiftyone:
    def test_the_capture_module_does_not(self):
        from tools.fiftyone import failures

        source = __import__("pathlib").Path(failures.__file__).read_text()
        assert "import fiftyone" not in source

    def test_a_session_can_be_built_without_it(self):
        """The shipped state: FiftyOne absent, capture off, machine unaffected."""
        from app import config
        from app.pipeline.session import DemoSession

        session = DemoSession(cfg=config.load(environ={}))
        assert session.capture.enabled is False

    def test_the_session_snapshot_reports_the_capture_state(self):
        from app import config
        from app.pipeline.session import DemoSession

        snapshot = DemoSession(cfg=config.load(environ={})).snapshot()["vision_capture"]
        assert snapshot["enabled"] is False
        assert "comparisons" in snapshot["note"]


@pytest.mark.skipif(not dataset_module.available(), reason="FiftyOne is not installed")
class TestTheDataset:
    """Only runs where FiftyOne is present. It is a development dependency."""

    def labelled_capture(self, tmp_path):
        from PIL import Image

        capture = FailureCapture(directory=tmp_path / "errors", enabled=True, session_id="RUN")
        capture.directory.mkdir(parents=True, exist_ok=True)
        path = capture.directory / "frame.jpg"
        Image.new("RGB", (W, H)).save(path)
        capture._encode = lambda frame, name: str(path)
        capture.capture(
            VisionFailure.LOW_CONFIDENCE,
            frame=object(),
            detections=[det(confidence=0.4)],
            ground_truth=[det(confidence=None)],
        )
        return capture

    def test_a_dataset_is_built_from_the_manifest(self, tmp_path):
        capture = self.labelled_capture(tmp_path)
        _, report = dataset_module.build_dataset(capture.directory, name="aurum-test-errors")
        assert report["samples_added"] == 1
        assert report["labelled"] == 1

    def test_predictions_and_ground_truth_land_in_separate_fields(self, tmp_path):
        capture = self.labelled_capture(tmp_path)
        dataset, _ = dataset_module.build_dataset(capture.directory, name="aurum-test-errors")
        sample = dataset.first()
        assert sample["predictions"].detections
        assert sample["ground_truth"].detections

    def test_a_missing_frame_is_reported_not_silently_dropped(self, tmp_path):
        capture = FailureCapture(
            directory=tmp_path / "errors",
            enabled=True,
            write_frame=lambda path, frame: None,
        )
        capture.capture(VisionFailure.NO_DETECTION)
        _, report = dataset_module.build_dataset(capture.directory, name="aurum-test-errors")
        assert report["samples_added"] == 0
        assert report["skipped"]

    def test_evaluating_an_unlabelled_dataset_refuses_rather_than_scoring_zero(self, tmp_path):
        from PIL import Image

        capture = FailureCapture(directory=tmp_path / "errors", enabled=True)
        capture.directory.mkdir(parents=True, exist_ok=True)
        path = capture.directory / "frame.jpg"
        Image.new("RGB", (W, H)).save(path)
        capture._encode = lambda frame, name: str(path)
        capture.capture(VisionFailure.NO_DETECTION, frame=object())
        dataset, _ = dataset_module.build_dataset(capture.directory, name="aurum-test-errors")
        result = dataset_module.evaluate(dataset)
        assert result["evaluated"] is False
        assert "not a score at all" in result["reason"]

    def test_a_labelled_dataset_evaluates(self, tmp_path):
        capture = self.labelled_capture(tmp_path)
        dataset, _ = dataset_module.build_dataset(capture.directory, name="aurum-test-errors")
        result = dataset_module.evaluate(dataset)
        assert result["evaluated"] is True
        assert "report" in result
