from unittest.mock import patch

import pytest

from improve import preflight
from tests import _cp


class TestCheck:
    def test_succeeds_when_command_returns_zero(self):
        with patch("improve.preflight.run", return_value=_cp()) as mock_run:
            preflight._check(["git", "status"], "error message")
            mock_run.assert_called_once_with(["git", "status"], timeout=15)

    def test_exits_when_command_returns_nonzero(self):
        with (
            patch("improve.preflight.run", return_value=_cp(returncode=1)),
            pytest.raises(SystemExit, match="1"),
        ):
            preflight._check(["git", "status"], "error message")


class TestRunPreflight:
    def test_runs_all_checks_when_ci_enabled(self):
        with patch("improve.preflight.run", return_value=_cp()) as mock_run:
            preflight.run_preflight("feature-x", "gh", skip_ci=False)
            assert mock_run.call_count == 4
            mock_run.assert_any_call(["git", "ls-remote", "--heads", "origin"], timeout=15)
            mock_run.assert_any_call(
                ["git", "push", "--dry-run", "origin", "feature-x"], timeout=15
            )
            mock_run.assert_any_call(["gh", "auth", "status"], timeout=15)
            mock_run.assert_any_call(["gh", "repo", "view", "--json", "name"], timeout=15)

    def test_skips_ci_checks_when_skip_ci_is_true(self):
        with patch("improve.preflight.run", return_value=_cp()) as mock_run:
            preflight.run_preflight("feature-x", "gh", skip_ci=True)
            assert mock_run.call_count == 2
            mock_run.assert_any_call(["git", "ls-remote", "--heads", "origin"], timeout=15)
            mock_run.assert_any_call(
                ["git", "push", "--dry-run", "origin", "feature-x"], timeout=15
            )

    def test_uses_glab_commands_for_gitlab(self):
        with patch("improve.preflight.run", return_value=_cp()) as mock_run:
            preflight.run_preflight("feature-x", "glab", skip_ci=False)
            mock_run.assert_any_call(["glab", "auth", "status"], timeout=15)
            mock_run.assert_any_call(["glab", "repo", "view"], timeout=15)

    def test_stops_at_first_failing_check(self):
        with (
            patch("improve.preflight.run", return_value=_cp(returncode=1)),
            pytest.raises(SystemExit, match="1"),
        ):
            preflight.run_preflight("feature-x", "gh", skip_ci=False)
