from __future__ import annotations

import logging

from improve import color


class TestInit:
    def test_disables_color_when_force_no_color_is_true(self):
        color.init(force_no_color=True)
        assert color.enabled is False

    def test_disables_color_when_no_color_env_is_set(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        color.init()
        assert color.enabled is False

    def test_disables_color_when_term_is_dumb(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "dumb")
        color.init()
        assert color.enabled is False

    def test_disables_color_when_stdout_is_not_a_tty(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("TERM", raising=False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        color.init()
        assert color.enabled is False

    def test_enables_color_when_stdout_is_a_tty(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("TERM", raising=False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        color.init()
        assert color.enabled is True


class TestWrap:
    def test_returns_plain_text_when_disabled(self):
        color.enabled = False
        assert color.wrap("hello", color.RED) == "hello"

    def test_wraps_text_with_ansi_codes_when_enabled(self):
        color.enabled = True
        result = color.wrap("hello", color.RED)
        assert result == f"{color.RED}hello{color.RESET}"


class TestPhaseColor:
    def test_returns_dark_green_for_simplify(self):
        assert color.phase_color("simplify") == color.DARK_GREEN

    def test_returns_dark_yellow_for_review(self):
        assert color.phase_color("review") == color.DARK_YELLOW

    def test_returns_dark_red_for_security(self):
        assert color.phase_color("security") == color.DARK_RED

    def test_returns_empty_string_for_unknown_phase(self):
        assert color.phase_color("unknown") == ""


class TestColorFormatter:
    def test_colorizes_known_tag_when_enabled(self):
        color.enabled = True
        formatter = color.ColorFormatter("%(message)s")
        record = logging.LogRecord("test", logging.INFO, "", 0, "loop] started", (), None)
        result = formatter.format(record)
        assert result.startswith(color.BOLD_WHITE)
        assert "loop]" in result

    def test_returns_plain_message_when_disabled(self):
        color.enabled = False
        formatter = color.ColorFormatter("%(message)s")
        record = logging.LogRecord("test", logging.INFO, "", 0, "loop] started", (), None)
        result = formatter.format(record)
        assert result == "loop] started"

    def test_returns_plain_message_when_no_bracket(self):
        color.enabled = True
        formatter = color.ColorFormatter("%(message)s")
        record = logging.LogRecord("test", logging.INFO, "", 0, "no bracket here", (), None)
        result = formatter.format(record)
        assert result == "no bracket here"

    def test_colorizes_phase_tag(self):
        color.enabled = True
        formatter = color.ColorFormatter("%(message)s")
        record = logging.LogRecord("test", logging.INFO, "", 0, "simplify] Running...", (), None)
        result = formatter.format(record)
        assert result.startswith(color.DARK_GREEN)
