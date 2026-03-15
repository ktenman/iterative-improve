from unittest.mock import patch

import pytest

from improve.process import format_duration, require_tools, run


class TestRun:
    def test_returns_completed_process_on_success(self):
        result = run(["echo", "hello"])

        assert result.returncode == 0
        assert result.stdout.strip() == "hello"

    def test_returns_nonzero_exit_code_without_raising(self):
        result = run(["false"])

        assert result.returncode == 1

    def test_returns_exit_code_1_on_timeout(self):
        result = run(["sleep", "10"], timeout=1)

        assert result.returncode == 1
        assert result.stdout == ""
        assert result.stderr == "Timed out"


class TestRequireTools:
    def test_raises_system_exit_1_when_tool_missing(self):
        with (
            patch("improve.process.shutil.which", return_value=None),
            pytest.raises(SystemExit) as exc_info,
        ):
            require_tools()

        assert exc_info.value.code == 1

    def test_checks_for_git_claude_and_gh(self):
        calls = []

        def tracking_which(tool):
            calls.append(tool)
            return f"/usr/bin/{tool}"

        with patch("improve.process.shutil.which", side_effect=tracking_which):
            require_tools()

        assert calls == ["git", "claude", "gh"]

    def test_passes_when_all_tools_found(self):
        with patch("improve.process.shutil.which", return_value="/usr/bin/git"):
            require_tools()


class TestFormatDuration:
    @pytest.mark.parametrize(
        "seconds, expected",
        [
            (0, "0.0s"),
            (30.5, "30.5s"),
            (59.9, "59.9s"),
            (60, "1m 0s"),
            (125, "2m 5s"),
            (3599, "59m 59s"),
            (3600, "1h 0m 0s"),
            (3725, "1h 2m 5s"),
            (7261, "2h 1m 1s"),
        ],
    )
    def test_formats_duration(self, seconds, expected):
        result = format_duration(seconds)

        assert result == expected
