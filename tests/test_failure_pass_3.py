"""The third failure pass: what the dashboard was told when things went wrong.

Each test here failed before its fix. The first two passes (docs/failure-modes.md)
were about the machine; these are about the screen - every case where the
backend answered with a 500, a frozen state or a wrong sentence, and the
dashboard passed it on as "the server is not running" or "Ready".
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_session import session

from app import api as api_mod
from app import config
from app import ledger as ledger_mod
from app.errors import ErrorCode
from app.weight import Calibration


def _frame():
    return np.zeros((48, 64, 3), np.uint8)


class _Tracker:
    """Detections for a frame; raises on the calls listed in `fail_on`."""

    def __init__(self, fail_on=()):
        self.calls = 0
        self.fail_on = set(fail_on)

    def track(self, _frame):
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError("transient driver hiccup")
        return []


def _source(reads):
    class Source:
        label = "webcam 1"

        def __init__(self, **_kwargs):
            pass

        def read(self):
            return reads()

        def release(self):
            pass

    return Source


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class TestConfig:
    @pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
    def test_a_non_finite_number_is_refused(self, value):
        """NaN compares False with every bound, so it slipped past all of them."""
        with pytest.raises(config.ConfigError, match="finite"):
            config.load(environ={"AURUM_WEIGHT_TIMEOUT_S": value})

    def test_a_bad_setting_stops_the_server_at_start(self, monkeypatch):
        """Not a server that answers /health and 500s everything else."""
        monkeypatch.setenv("AURUM_CAMERA_INDEX", "abc")
        with (
            pytest.raises(config.ConfigError, match="conveyor.camera.index"),
            TestClient(api_mod.app),
        ):
            pass


class TestCalibrationShape:
    @pytest.mark.parametrize("text", ["- 1\n- 2\n", "calibration: broken\n", "just text\n"])
    def test_valid_yaml_of_the_wrong_shape_is_uncalibrated(self, tmp_path, text):
        path = tmp_path / "calibration.yaml"
        path.write_text(text)
        cal = Calibration.load(path)
        assert not cal.has_factor
        assert "treating as uncalibrated" in cal.notes

    def test_a_file_with_no_calibration_key_stays_plain_uncalibrated(self, tmp_path):
        path = tmp_path / "calibration.yaml"
        path.write_text("other: 1\n")
        assert Calibration.load(path) == Calibration()


@pytest.fixture
def real_detector_client(tmp_path, monkeypatch):
    """The API with the real `detector()` - the path a missing model takes."""
    monkeypatch.setattr(ledger_mod, "DB", tmp_path / "batches.db")
    monkeypatch.setattr(api_mod, "_demo", None)
    monkeypatch.setattr(api_mod, "_detector", None)
    with TestClient(api_mod.app) as client:
        yield client


class TestModelUnavailable:
    def test_ready_reports_a_missing_model_instead_of_503(
        self, real_detector_client, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(api_mod, "DEFAULT_WEIGHTS", tmp_path / "absent.pt")
        r = real_detector_client.get("/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is False
        assert body["blocked_by"] == ["vision model"]
        assert "absent.pt" in body["checks"][0]["detail"]

    def test_weights_that_will_not_load_are_a_503_with_the_cause(
        self, real_detector_client, tmp_path, monkeypatch
    ):
        weights = tmp_path / "truncated.pt"
        weights.write_bytes(b"not a checkpoint")
        monkeypatch.setattr(api_mod, "DEFAULT_WEIGHTS", weights)

        def refuse(_path):
            raise RuntimeError("PytorchStreamReader failed reading zip archive")

        monkeypatch.setattr(api_mod, "AurumDetector", refuse)
        assert real_detector_client.get("/session").status_code == 503
        assert "failed to load" in real_detector_client.get("/session").json()["detail"]
        health = real_detector_client.get("/health").json()
        assert health["status"] == "model_error"
        ready = real_detector_client.get("/ready").json()
        assert ready["blocked_by"] == ["vision model"]


class TestCameraLoop:
    def _run(self, monkeypatch, reads, tracker):
        monkeypatch.setattr("app.demo.FrameSource", _source(reads))
        run = session()
        run.pipeline.detector_tracker = tracker
        assert run.start_camera()["running"]
        return run

    def test_a_transient_tracking_failure_clears_when_frames_resume(self, monkeypatch):
        """One exception used to mark the camera offline for the rest of the run."""
        tracker = _Tracker(fail_on={1})
        run = self._run(monkeypatch, lambda: (True, _frame()), tracker)
        try:
            assert _wait_for(lambda: tracker.calls > 5)
            assert _wait_for(lambda: run.camera_error is None)
            assert run.frames > 0
        finally:
            run.stop()

    def test_a_camera_that_stops_delivering_is_named(self, monkeypatch):
        """`running: true` and a frozen picture, with /ready green, before."""
        monkeypatch.setattr("app.pipeline.session.DemoSession.FRAME_STALL_S", 0.2)
        run = self._run(monkeypatch, lambda: (False, None), _Tracker())
        try:
            assert _wait_for(lambda: run.camera_error is not None)
            assert "No frame from webcam 1" in run.camera_error
            assert run.snapshot()["running"] is True
            assert run.errors.snapshot()["by_code"].get(str(ErrorCode.VISION_ERROR)) == 1
        finally:
            run.stop()

    def test_an_error_after_tracking_does_not_kill_the_thread(self, monkeypatch):
        """It died silently, and the screen said "Camera not started"."""
        run = self._run(monkeypatch, lambda: (True, _frame()), _Tracker())

        def boom(_detections, **_kwargs):
            raise ValueError("bad detection shape")

        run.pipeline.process_detections = boom
        try:
            assert _wait_for(lambda: run.camera_error is not None)
            assert "bad detection shape" in run.camera_error
            assert run.snapshot()["running"] is True
        finally:
            run.stop()


class TestMeasureFailure:
    def test_a_failed_weigh_releases_the_claim(self):
        """A 500, then ALREADY_PROCESSED for ever, for an object never graded."""
        run = session()
        from app.pipeline import scripted

        for frame in range(10):
            run.pipeline.process_detections(
                scripted.detections_for(0, scripted.SCRIPT[0]), frame_id=frame
            )
        assert run.current_assembly is not None

        def boom(_assembly=None):
            raise OSError("device reports readiness to read but returned no data")

        run._read_mass = boom
        first = run.measure_and_route()
        assert first["error"] == "MEASURE_FAILED"
        assert "returned no data" in first["reason"]

        del run._read_mass
        second = run.measure_and_route()
        assert "error" not in second
        assert second["decision"] is not None
