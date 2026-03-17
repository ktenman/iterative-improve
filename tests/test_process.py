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

    def test_returns_exit_code_1_when_command_not_found(self):
        result = run(["nonexistent_binary_xyz_12345"])

        assert result.returncode == 1
        assert result.stdout == ""
        assert result.stderr != ""


class TestRequireTools:
    def test_raises_system_exit_1_when_tool_missing(self):
        with (
            patch("improve.process.shutil.which", return_value=None),
            pytest.raises(SystemExit) as exc_info,
        ):
            require_tools()

        assert exc_info.value.code == 1

    @pytest.mark.parametrize(
        ("ci_tool", "expected"),
        [
            ("gh", ["git", "claude", "gh"]),
            ("glab", ["git", "claude", "glab"]),
        ],
    )
    def test_checks_for_expected_tools(self, ci_tool, expected):
        calls = []

        def tracking_which(tool):
            calls.append(tool)
            return f"/usr/bin/{tool}"

        with patch("improve.process.shutil.which", side_effect=tracking_which):
            require_tools(ci_tool=ci_tool)

        assert calls == expected

    def test_passes_when_all_tools_found(self):
        with patch("improve.process.shutil.which", return_value="/usr/bin/git"):
            require_tools()

    def test_raises_system_exit_when_glab_missing(self):
        def selective_which(tool):
            if tool == "glab":
                return None
            return f"/usr/bin/{tool}"

        with (
            patch("improve.process.shutil.which", side_effect=selective_which),
            pytest.raises(SystemExit) as exc_info,
        ):
            require_tools(ci_tool="glab")

        assert exc_info.value.code == 1


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
