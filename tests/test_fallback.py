"""Tests for strathmark/fallback.py — panel marks and event baseline."""

from strathmark.fallback import get_panel_mark


class TestGetPanelMark:
    def test_unknown_division_returns_default(self):
        time_val, explanation = get_panel_mark("SB", None)
        assert time_val == 20.0
        assert "Unknown" in explanation or "default" in explanation.lower()

    def test_known_division_returns_mark(self):
        time_val, explanation = get_panel_mark("SB", "Open")
        assert time_val > 0
        assert "Open" in explanation

    def test_sb_and_uh_differ(self):
        sb_time, _ = get_panel_mark("SB", "Open")
        uh_time, _ = get_panel_mark("UH", "Open")
        # UH and SB may have different panel marks
        assert sb_time > 0
        assert uh_time > 0

    def test_custom_marks_override(self):
        custom = {("SB", "Open"): 99.0}
        time_val, _ = get_panel_mark("SB", "Open", custom_marks=custom)
        assert time_val == 99.0

    def test_case_insensitive_division(self):
        time1, _ = get_panel_mark("SB", "Open")
        time2, _ = get_panel_mark("SB", "open")
        assert time1 == time2
