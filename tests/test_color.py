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
    def test_returns_green_for_simplify(self):
        assert color.phase_color("simplify") == color.GREEN

    def test_returns_yellow_for_review(self):
        assert color.phase_color("review") == color.YELLOW

    def test_returns_red_for_security(self):
        assert color.phase_color("security") == color.RED

    def test_returns_empty_string_for_unknown_phase(self):
        assert color.phase_color("unknown") == ""


class TestStatusMark:
    def test_shows_check_mark_for_passed_with_changes(self):
        color.enabled = False
        assert color.status_mark(passed=True, changed=True) == "\u2713"

    def test_shows_cross_mark_for_failed_with_changes(self):
        color.enabled = False
        assert color.status_mark(passed=False, changed=True) == "\u2717"

    def test_shows_dot_for_no_changes(self):
        color.enabled = False
        assert color.status_mark(passed=True, changed=False) == "\u00b7"

    def test_shows_revert_mark_when_reverted(self):
        color.enabled = False
        assert color.status_mark(passed=False, changed=True, reverted=True) == "\u21ba"

    def test_wraps_with_color_when_enabled(self):
        color.enabled = True
        result = color.status_mark(passed=True, changed=True)
        assert "\u2713" in result
        assert color.GREEN in result


class TestSeparatorAndTitle:
    def test_separator_returns_equals_when_disabled(self):
        color.enabled = False
        result = color.separator()
        assert result == "=" * color.BOX_WIDTH

    def test_separator_includes_ansi_when_enabled(self):
        color.enabled = True
        result = color.separator()
        assert "=" in result
        assert color.CYAN in result

    def test_section_title_returns_plain_when_disabled(self):
        color.enabled = False
        assert color.section_title("Results") == "Results"

    def test_section_title_includes_ansi_when_enabled(self):
        color.enabled = True
        result = color.section_title("Results")
        assert "Results" in result
        assert color.CYAN in result


class TestColorFormatter:
    def test_colorizes_known_tag_when_enabled(self):
        color.enabled = True
        formatter = color.ColorFormatter("%(message)s")
        record = logging.LogRecord("test", logging.INFO, "", 0, "loop] started", (), None)
        result = formatter.format(record)
        assert color.BOLD_WHITE in result
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
        assert color.GREEN in result

    def test_does_not_mutate_original_log_record(self):
        color.enabled = True
        formatter = color.ColorFormatter("%(message)s")
        record = logging.LogRecord("test", logging.INFO, "", 0, "loop] started", (), None)
        formatter.format(record)
        assert record.msg == "loop] started"
        assert record.args == ()

    def test_includes_opening_bracket_in_color_sequence(self):
        color.enabled = True
        formatter = color.ColorFormatter("[%(message)s")
        record = logging.LogRecord("test", logging.INFO, "", 0, "loop] started", (), None)
        result = formatter.format(record)
        assert f"{color.BOLD_WHITE}[loop]" in result

    def test_does_not_leak_ansi_codes_to_plain_formatter(self):
        color.enabled = True
        color_fmt = color.ColorFormatter("%(message)s")
        plain_fmt = logging.Formatter("%(message)s")
        record = logging.LogRecord("test", logging.INFO, "", 0, "loop] started", (), None)
        color_fmt.format(record)
        plain_result = plain_fmt.format(record)
        assert "\033[" not in plain_result
