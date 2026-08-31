"""The configuration reference is generated, so its parser is the thing to test.

Both bugs these cover were real. A rule of dashes closes a themed comment block
in `app/config.py` and sits directly above the entry it describes, so treating
it as a terminator silently dropped the description. And `configs/conveyor.yaml`
documents the camera backend as `auto | CAP_DSHOW | ...`, whose pipes ended the
table column they landed in and turned one row into nine.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "gen_configuration", ROOT / "scripts" / "gen_configuration.py"
)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


class TestReadingTheCommentAboveASetting:
    def test_a_closing_rule_of_dashes_does_not_hide_the_description(self):
        lines = [
            "# ------------------------------",
            "# It exists so a broken load cell does not cost the demonstration.",
            "# ------------------------------",
            '"demo.mock_mass.enabled": (_bool, False, "AURUM_DEMO_MOCK_MASS"),',
        ]
        assert "broken load cell" in gen.comments_above(lines, 4)

    def test_a_blank_comment_line_becomes_a_paragraph_break(self):
        lines = ["# first", "#", "# second", '"k": (_bool, False, "E"),']
        assert gen.comments_above(lines, 4) == "first<br><br>second"

    def test_it_stops_at_the_previous_entry(self):
        lines = [
            "# belongs to the entry above",
            '"other.key": (_bool, False, "OTHER"),',
            '"k": (_bool, False, "E"),',
        ]
        assert gen.comments_above(lines, 3) == ""

    def test_a_setting_with_no_comment_reports_nothing(self):
        assert gen.comments_above(['"k": (_int, 0, "E"),'], 1) == ""


class TestKeepingTheTableIntact:
    def test_a_pipe_is_escaped_rather_than_ending_the_column(self):
        assert gen._cell("auto | CAP_DSHOW | CAP_ANY") == "auto \\| CAP_DSHOW \\| CAP_ANY"

    def test_every_generated_row_has_exactly_the_five_columns(self):
        """The rendered document, not a fixture — this is the failure it stops."""
        text = (ROOT / "docs" / "configuration.md").read_text()
        for line in text.splitlines():
            if not line.startswith("| "):
                continue
            unescaped = sum(
                1 for i, c in enumerate(line) if c == "|" and (i == 0 or line[i - 1] != "\\")
            )
            assert unescaped == 6, f"{unescaped} columns in: {line[:80]}"


class TestTheRealConfiguration:
    def test_every_setting_in_the_spec_reaches_the_document(self):
        from app import config

        text = (ROOT / "docs" / "configuration.md").read_text()
        missing = [key for key in config.SPEC if f"`{key}`" not in text]
        assert not missing, f"regenerate docs/configuration.md — missing {missing}"

    def test_the_camera_index_trap_is_documented(self):
        """A wrong index streams a plausible ceiling instead of failing, which is
        the one thing an operator cannot discover from the screen."""
        text = (ROOT / "docs" / "configuration.md").read_text()
        row = next(line for line in text.splitlines() if "`conveyor.camera.index`" in line)
        assert "built-in camera" in row
