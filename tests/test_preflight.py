from unittest.mock import patch

import pytest

from improve import preflight
from tests import _cp


class TestCheckGitRemote:
    def test_passes_when_remote_is_reachable(self):
        with patch("improve.preflight.run", return_value=_cp()) as mock_run:
            preflight._check_git_remote()
            mock_run.assert_called_once_with(["git", "ls-remote", "--heads", "origin"], timeout=15)

    def test_exits_when_remote_is_unreachable(self):
        with (
            patch("improve.preflight.run", return_value=_cp(returncode=1)),
            pytest.raises(SystemExit, match="1"),
        ):
            preflight._check_git_remote()


class TestCheckGitPush:
    def test_passes_when_push_is_allowed(self):
        with patch("improve.preflight.run", return_value=_cp()) as mock_run:
            preflight._check_git_push("feature-x")
            mock_run.assert_called_once_with(
                ["git", "push", "--dry-run", "origin", "feature-x"], timeout=15
            )

    def test_includes_branch_name_in_command(self):
        with patch("improve.preflight.run", return_value=_cp()) as mock_run:
            preflight._check_git_push("my-branch")
            assert mock_run.call_args[0][0][4] == "my-branch"

    def test_exits_when_push_is_denied(self):
        with (
            patch("improve.preflight.run", return_value=_cp(returncode=1)),
            pytest.raises(SystemExit, match="1"),
        ):
            preflight._check_git_push("feature-x")


class TestCheckCiAuth:
    def test_passes_when_gh_is_authenticated(self):
        with patch("improve.preflight.run", return_value=_cp()) as mock_run:
            preflight._check_ci_auth("gh")
            mock_run.assert_called_once_with(["gh", "auth", "status"], timeout=15)

    def test_passes_when_glab_is_authenticated(self):
        with patch("improve.preflight.run", return_value=_cp()) as mock_run:
            preflight._check_ci_auth("glab")
            mock_run.assert_called_once_with(["glab", "auth", "status"], timeout=15)

    def test_exits_when_not_authenticated(self):
        with (
            patch("improve.preflight.run", return_value=_cp(returncode=1)),
            pytest.raises(SystemExit, match="1"),
        ):
            preflight._check_ci_auth("gh")


class TestCheckCiRepoAccess:
    def test_uses_json_flag_for_github(self):
        with patch("improve.preflight.run", return_value=_cp()) as mock_run:
            preflight._check_ci_repo_access("gh")
            mock_run.assert_called_once_with(["gh", "repo", "view", "--json", "name"], timeout=15)

    def test_uses_plain_view_for_gitlab(self):
        with patch("improve.preflight.run", return_value=_cp()) as mock_run:
            preflight._check_ci_repo_access("glab")
            mock_run.assert_called_once_with(["glab", "repo", "view"], timeout=15)

    def test_exits_when_repo_is_not_accessible(self):
        with (
            patch("improve.preflight.run", return_value=_cp(returncode=1)),
            pytest.raises(SystemExit, match="1"),
        ):
            preflight._check_ci_repo_access("gh")


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
