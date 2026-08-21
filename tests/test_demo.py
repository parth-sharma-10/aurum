"""Tests for the runtime the live demo actually uses.

These cover the three files a presentation depends on and that nothing else
tested: frame sourcing, dashboard rendering, and mass input. The webcam cases
are the reason this file exists — a camera that reports itself open and then
never delivers a frame is the exact failure macOS produces when permission has
not been granted, and it used to end the demo as "frame source exhausted"
instead of falling back to images.

No camera, no window and no weights are needed: cv2's capture and rendering
entry points are stubbed.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

import run_demo
from app import demo as demo_mod
from app.dashboard import compose, draw_detections
from app.demo import FrameSource
from app.detector import Detection
from app.weight import HX711LoadCell, SimulatedLoadCell, get_weight_source


class FakeCapture:
    """Stands in for cv2.VideoCapture.

    `frames` is how many successful reads it will serve; `opened` is what
    isOpened() claims, which is deliberately independent of whether a frame ever
    arrives — that split is the bug under test.
    """

    def __init__(self, frames: int = 5, opened: bool = True):
        self.remaining = frames
        self.opened = opened
        self.released = False
        self.props: dict[int, float] = {}

    def isOpened(self):  # noqa: N802 - matches the cv2 API
        return self.opened

    def set(self, prop, value):
        self.props[prop] = value
        return True

    def read(self):
        if self.remaining <= 0:
            return False, None
        self.remaining -= 1
        return True, np.zeros((48, 64, 3), dtype=np.uint8)

    def release(self):
        self.released = True


@pytest.fixture
def capture(monkeypatch):
    """Install a FakeCapture and hand the test a handle on the instance."""
    made: list[FakeCapture] = []

    def factory(frames=5, opened=True):
        def _make(_source):
            cap = FakeCapture(frames, opened)
            made.append(cap)
            return cap

        monkeypatch.setattr(cv2, "VideoCapture", _make)
        return made

    return factory


class TestWebcamSource:
    def test_a_working_camera_opens(self, capture):
        made = capture(frames=5)
        src = FrameSource("webcam", None, 0, 1280, 720, 0.0)
        assert src.label == "webcam"
        assert src.scene_id == "live"
        assert made[0].props[cv2.CAP_PROP_FRAME_WIDTH] == 1280

    def test_camera_that_opens_but_never_delivers_a_frame_is_rejected(self, capture):
        """The macOS permission case: isOpened() is true and read() never works."""
        capture(frames=0)
        with pytest.raises(RuntimeError, match="Could not open camera"):
            FrameSource("webcam", None, 0, 1280, 720, 0.0, first_frame_timeout=0.05)

    def test_a_rejected_camera_is_released(self, capture):
        made = capture(frames=0)
        with pytest.raises(RuntimeError):
            FrameSource("webcam", None, 0, 1280, 720, 0.0, first_frame_timeout=0.05)
        assert made[0].released, "the capture device must not be left held open"

    def test_a_camera_that_will_not_open_is_rejected(self, capture):
        capture(frames=0, opened=False)
        with pytest.raises(RuntimeError, match="camera"):
            FrameSource("webcam", None, 0, 1280, 720, 0.0, first_frame_timeout=0.05)

    def test_the_error_names_the_image_mode_escape_hatch(self, capture):
        """The message is what the operator reads mid-demo, so it must say what to do."""
        capture(frames=0)
        with pytest.raises(RuntimeError, match="--mode images"):
            FrameSource("webcam", None, 0, 1280, 720, 0.0, first_frame_timeout=0.05)

    def test_waiting_is_bounded_by_the_configured_timeout(self, capture):
        import time

        capture(frames=0)
        t0 = time.perf_counter()
        with pytest.raises(RuntimeError):
            FrameSource("webcam", None, 0, 1280, 720, 0.0, first_frame_timeout=0.15)
        assert time.perf_counter() - t0 < 2.0, "a short timeout must not wait the default 3s"


class TestImageSource:
    @pytest.fixture
    def folder(self, tmp_path):
        for name in ("b.jpg", "a.jpg", "c.png"):
            cv2.imwrite(str(tmp_path / name), np.zeros((32, 32, 3), dtype=np.uint8))
        (tmp_path / "notes.txt").write_text("not an image")
        return tmp_path

    def test_reads_images_in_sorted_order_ignoring_non_images(self, folder):
        src = FrameSource("images", str(folder), 0, 0, 0, 0.0)
        assert src.label == "images 1/3"
        assert src.scene_id == "a.jpg"

    def test_scene_id_changes_with_the_file(self, folder):
        """The median window is cleared on scene change; that hinges on this id."""
        src = FrameSource("images", str(folder), 0, 0, 0, 0.0)
        first = src.scene_id
        src.advance(1)
        assert src.scene_id != first

    def test_advance_wraps_around(self, folder):
        src = FrameSource("images", str(folder), 0, 0, 0, 0.0)
        first = src.scene_id
        for _ in range(3):
            src.advance(1)
        assert src.scene_id == first

    def test_an_empty_folder_is_an_error_not_an_empty_demo(self, tmp_path):
        with pytest.raises(RuntimeError, match="No images found"):
            FrameSource("images", str(tmp_path), 0, 0, 0, 0.0)

    def test_a_missing_path_is_rejected(self):
        with pytest.raises(RuntimeError, match="requires --path"):
            FrameSource("images", None, 0, 0, 0, 0.0)

    def test_an_unknown_mode_is_rejected(self):
        with pytest.raises(RuntimeError, match="unknown mode"):
            FrameSource("telepathy", None, 0, 0, 0, 0.0)


class TestFallbackToImageMode:
    """`run_demo.py` must degrade to stills rather than exit when the camera fails."""

    def test_fallback_argv_switches_mode_and_keeps_other_flags(self):
        out = run_demo._fallback_argv(["--mode", "webcam", "--conf", "0.5"], "/imgs")
        assert out[:4] == ["--mode", "images", "--path", "/imgs"]
        assert "--conf" in out and "0.5" in out

    def test_fallback_argv_strips_the_equals_form_too(self):
        out = run_demo._fallback_argv(["--camera=2", "--path=/old", "--window=60"], "/imgs")
        assert "--camera=2" not in out
        assert "--path=/old" not in out
        assert "--window=60" in out

    def test_fallback_argv_drops_a_previous_path_value(self):
        out = run_demo._fallback_argv(["--path", "/old", "--frames", "10"], "/imgs")
        assert "/old" not in out
        assert out.count("--path") == 1

    def test_a_camera_failure_reruns_in_image_mode(self, monkeypatch, tmp_path):
        calls: list[list[str]] = []

        def fake_main(argv):
            calls.append(list(argv))
            if len(calls) == 1:
                raise RuntimeError("Could not open camera 0.")
            return 0

        monkeypatch.setattr(run_demo, "main", fake_main)
        monkeypatch.setattr(run_demo, "FALLBACK_IMAGES", tmp_path)
        monkeypatch.setattr("sys.argv", ["run_demo.py"])

        assert run_demo.run() == 0
        assert len(calls) == 2
        assert calls[1][:2] == ["--mode", "images"]

    def test_a_non_camera_failure_is_not_swallowed(self, monkeypatch):
        def fake_main(_argv):
            raise RuntimeError("No weights at models/x.pt")

        monkeypatch.setattr(run_demo, "main", fake_main)
        monkeypatch.setattr("sys.argv", ["run_demo.py"])
        with pytest.raises(RuntimeError, match="No weights"):
            run_demo.run()

    def test_missing_fallback_images_exit_nonzero_rather_than_looping(self, monkeypatch, tmp_path):
        def fake_main(_argv):
            raise RuntimeError("Could not open camera 0.")

        monkeypatch.setattr(run_demo, "main", fake_main)
        monkeypatch.setattr(run_demo, "FALLBACK_IMAGES", tmp_path / "absent")
        monkeypatch.setattr("sys.argv", ["run_demo.py"])
        assert run_demo.run() == 1


class TestDashboardRendering:
    """Smoke tests: the dashboard must draw without a window and without weights."""

    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    dets = [
        Detection(cls="PCB", conf=0.91, xyxy=(10, 10, 100, 100)),
        Detection(cls="RAM", conf=0.42, xyxy=(120, 20, 200, 60)),
    ]

    def _compose(self, **kw):
        base = {
            "frame": self.frame,
            "detections": self.dets,
            "counts": {"PCB": 1, "RAM": 1, "CPU": 0, "Connector": 0},
            "classes": ["PCB", "RAM", "CPU", "Connector"],
            "avg_conf": 0.66,
            "fps": 31.4,
            "model_version": "Aurum Vision v-test",
            "batch_id": "AUR-TEST0001",
            "frames": 45,
            "mode": "images 1/2",
        }
        return compose(**{**base, **kw})

    def test_canvas_is_the_frame_plus_panel_and_status_bar(self):
        canvas = self._compose()
        assert canvas.shape == (64 + 240 + 54, 320 + 340, 3)
        assert canvas.dtype == np.uint8

    def test_header_can_be_omitted(self):
        assert self._compose(header=False).shape[0] == 240 + 54

    def test_a_simulated_weight_renders(self):
        canvas = self._compose(weight={"kg": 1.84, "grams": 1840.0, "simulated": True})
        assert canvas.any(), "something must actually be drawn"

    def test_a_measured_weight_renders(self):
        assert self._compose(weight={"kg": 1.84, "grams": 1840.0, "simulated": False}).any()

    def test_an_unavailable_recovery_estimate_renders(self):
        assert self._compose(recovery={"available": False}).any()

    def test_drawing_detections_does_not_mutate_the_source_frame(self):
        before = self.frame.copy()
        draw_detections(self.frame, self.dets)
        assert np.array_equal(self.frame, before)

    def test_an_unknown_class_still_draws(self):
        """A class the palette does not know must not crash the render."""
        out = draw_detections(self.frame, [Detection(cls="GPU", conf=0.5, xyxy=(1, 1, 20, 20))])
        assert out.shape == self.frame.shape

    def test_a_box_at_the_top_edge_keeps_its_label_on_canvas(self):
        out = draw_detections(self.frame, [Detection(cls="CPU", conf=0.9, xyxy=(0, 0, 30, 30))])
        assert out.shape == self.frame.shape


class TestWeightSource:
    def test_off_means_no_source_at_all(self):
        assert get_weight_source("off") is None

    def test_simulated_is_always_flagged(self):
        src = get_weight_source("simulated")
        assert isinstance(src, SimulatedLoadCell)
        reading = src.read()
        assert reading.simulated is True
        assert "SIMULATED" in reading.as_dict()["warning"]

    def test_a_measured_reading_carries_no_warning_field(self):
        """The warning key is the UI's simulated flag; a real reading must not have it."""
        from app.weight import WeightReading

        assert "warning" not in WeightReading(1840.0, False, "HX711").as_dict()

    def test_auto_without_a_port_falls_back_to_simulation(self, monkeypatch):
        monkeypatch.delenv("AURUM_HX711_PORT", raising=False)
        assert isinstance(get_weight_source("auto"), SimulatedLoadCell)

    def test_hx711_without_a_port_is_an_error_not_a_silent_simulation(self, monkeypatch):
        monkeypatch.delenv("AURUM_HX711_PORT", raising=False)
        with pytest.raises(RuntimeError, match="requires --hx711-port"):
            get_weight_source("hx711")

    def test_auto_with_an_unusable_port_degrades_visibly(self, monkeypatch):
        """A missing load cell must simulate, not crash — and say so."""

        def boom(*_a, **_kw):
            raise OSError("no such device")

        monkeypatch.setattr(HX711LoadCell, "__init__", boom)
        assert isinstance(get_weight_source("auto", port="/dev/absent"), SimulatedLoadCell)

    def test_explicit_hx711_with_a_bad_port_raises_rather_than_simulating(self, monkeypatch):
        def boom(*_a, **_kw):
            raise OSError("no such device")

        monkeypatch.setattr(HX711LoadCell, "__init__", boom)
        with pytest.raises(OSError):
            get_weight_source("hx711", port="/dev/absent")

    def test_simulated_readings_stay_in_a_believable_band(self):
        """Drift and jitter, not noise that would look broken on stage."""
        src = SimulatedLoadCell(base_grams=1840.0, jitter_g=2.5)
        values = [src.read().grams for _ in range(20)]
        assert all(1830.0 < v < 1850.0 for v in values)


def test_demo_module_exposes_a_default_first_frame_timeout():
    assert demo_mod.FIRST_FRAME_TIMEOUT_S > 0
