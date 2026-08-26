"""Tests for the structured error model.

Two properties. A failure carries enough context to act on — a code, a stage,
an item and a time — and a credential never travels with it.
"""

from __future__ import annotations

import pytest

from app.errors import LOG_LIMIT, AurumError, ErrorCode, ErrorLog, redact
from app.pipeline.pan import PanState
from app.pipeline.session import _STAGE_ERROR


class TestRedaction:
    @pytest.mark.parametrize(
        "text",
        [
            "GET https://api.example.com/v1/latest?api_key=SEKRET123&base=USD failed",
            '{"token": "SEKRET123"}',
            "apikey=SEKRET123",
            "PASSWORD: SEKRET123",
            "access_key = SEKRET123",
            '"secret":"SEKRET123"',
        ],
    )
    def test_a_credential_never_survives(self, text):
        assert "SEKRET123" not in redact(text)
        assert "REDACTED" in redact(text)

    def test_the_surrounding_message_is_kept(self):
        out = redact("GET https://x/v1?api_key=SEKRET123&base=USD failed")
        assert "base=USD failed" in out

    def test_the_variable_name_is_not_a_credential(self):
        """Telling somebody which variable to export is the point of the message."""
        text = "Export AURUM_METALPRICE_API_KEY to enable live pricing."
        assert redact(text) == text

    @pytest.mark.parametrize("value", [None, ""])
    def test_an_absent_message_is_returned_unchanged(self, value):
        assert redact(value) == value

    def test_it_is_applied_to_every_recorded_message(self):
        log = ErrorLog("S1")
        entry = log.record(ErrorCode.PRICE_ERROR, "pricing", "failed with api_key=SEKRET123")
        assert "SEKRET123" not in entry.message

    def test_it_is_applied_to_string_detail_fields_too(self):
        log = ErrorLog("S1")
        entry = log.record(ErrorCode.PRICE_ERROR, "pricing", "boom", url="?api_key=SEKRET123")
        assert "SEKRET123" not in str(entry.as_dict())


class TestTheRecord:
    def test_an_entry_carries_the_context_needed_to_act_on_it(self):
        log = ErrorLog("AUR-RUN-1")
        entry = log.record(
            ErrorCode.SERVO_ERROR, "actuation", "no ACK", item_id="AUR-ITEM-9", error_code="TIMEOUT"
        )
        record = entry.as_dict()
        assert record["error_code"] == "SERVO_ERROR"
        assert record["stage"] == "actuation"
        assert record["session_id"] == "AUR-RUN-1"
        assert record["item_id"] == "AUR-ITEM-9"
        assert record["timestamp"]
        assert record["detail"]["error_code"] == "TIMEOUT"

    def test_it_is_a_record_and_not_an_exception(self):
        """Aurum keeps running and routes what it cannot read to C."""
        assert not issubclass(AurumError, BaseException)

    def test_entries_can_be_found_by_item(self):
        log = ErrorLog("S1")
        log.record(ErrorCode.WEIGHT_ERROR, "pan", "a", item_id="AUR-ITEM-1")
        log.record(ErrorCode.SERVO_ERROR, "actuation", "b", item_id="AUR-ITEM-2")
        assert [str(e.code) for e in log.for_item("AUR-ITEM-2")] == ["SERVO_ERROR"]

    def test_counts_are_by_code(self):
        log = ErrorLog("S1")
        log.record(ErrorCode.PRICE_ERROR, "pricing", "a")
        log.record(ErrorCode.PRICE_ERROR, "pricing", "b")
        log.record(ErrorCode.VISION_ERROR, "camera", "c")
        assert log.counts() == {"PRICE_ERROR": 2, "VISION_ERROR": 1}

    def test_the_snapshot_is_newest_first(self):
        log = ErrorLog("S1")
        log.record(ErrorCode.PRICE_ERROR, "pricing", "first")
        log.record(ErrorCode.VISION_ERROR, "camera", "second")
        assert log.snapshot()["recent"][0]["message"] == "second"

    def test_the_log_is_bounded(self):
        """A stuck poll loop must not be a memory leak with a friendly name."""
        log = ErrorLog("S1", limit=10)
        for i in range(50):
            log.record(ErrorCode.WEIGHT_ERROR, "pan", str(i))
        assert len(log) == 10
        assert log.entries()[-1].message == "49"

    def test_the_default_limit_is_finite(self):
        assert LOG_LIMIT > 0


class TestEveryStageHasACode:
    """§11's code list, checked against the enum rather than against prose."""

    @pytest.mark.parametrize(
        "name",
        [
            "VISION_ERROR",
            "TRACKING_ERROR",
            "WEIGHT_ERROR",
            "MATERIAL_ERROR",
            "PRICE_ERROR",
            "DECISION_ERROR",
            "ROUTING_ERROR",
            "ARDUINO_ERROR",
            "SERVO_ERROR",
            "TIMEOUT",
            "CONFIG_ERROR",
            "HARDWARE_FAULT",
        ],
    )
    def test_the_code_exists(self, name):
        assert ErrorCode(name)


class TestTheLoopFilesFailuresUnderTheRightStage:
    """One handler covers weighing, deciding and actuating. It must not call
    an actuation failure a weight problem: that sends whoever reads the log to
    the wrong end of the machine."""

    def test_every_pan_state_maps_to_a_code(self):
        assert set(_STAGE_ERROR) == set(PanState)

    def test_weighing_is_a_weight_error(self):
        assert _STAGE_ERROR[PanState.WEIGHING] is ErrorCode.WEIGHT_ERROR

    def test_processing_is_a_decision_error(self):
        assert _STAGE_ERROR[PanState.PROCESSING] is ErrorCode.DECISION_ERROR

    def test_routing_is_a_servo_error(self):
        assert _STAGE_ERROR[PanState.ROUTING] is ErrorCode.SERVO_ERROR
