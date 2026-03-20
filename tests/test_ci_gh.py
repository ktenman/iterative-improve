from unittest.mock import patch

import pytest

from improve.ci import CIConclusion
from improve.ci_gh import GitHubCI
from tests.conftest import _cp


@pytest.fixture()
def provider() -> GitHubCI:
    return GitHubCI(workflow="CI")


class TestGitHubCI:
    def test_get_latest_run_id_parses_database_id(self, provider):
        with patch("improve.ci_gh.run", return_value=_cp(stdout='[{"databaseId": 99}]')):
            assert provider.get_latest_run_id("main") == 99

    def test_get_run_conclusion_returns_conclusion_field(self, provider):
        with patch("improve.ci_gh.run", return_value=_cp(stdout='{"conclusion": "failure"}')):
            assert provider.get_run_conclusion(42) == CIConclusion.FAILURE

    @pytest.mark.parametrize(
        "cp",
        [_cp(returncode=1), _cp(stdout="not json"), _cp(stdout="[1,2]")],
    )
    def test_get_run_conclusion_returns_none_on_invalid_response(self, provider, cp):
        with patch("improve.ci_gh.run", return_value=cp):
            assert provider.get_run_conclusion(42) is None

    @pytest.mark.parametrize(
        ("returncode", "expected"),
        [(0, True), (1, False)],
    )
    def test_watch_run_returns_expected_result(self, provider, returncode, expected):
        with patch("improve.ci_gh.run", return_value=_cp(returncode=returncode)):
            assert provider.watch_run(200, 900) is expected

    def test_watch_run_passes_timeout_to_subprocess(self, provider):
        with patch("improve.ci_gh.run", return_value=_cp()) as mock_run:
            provider.watch_run(42, 300)

        mock_run.assert_called_once_with(
            ["gh", "run", "watch", "42", "--exit-status"],
            timeout=300,
        )

    def test_get_failed_logs_truncates_to_4000_chars(self, provider):
        long_output = "x" * 5000
        with patch("improve.ci_gh.run", return_value=_cp(stdout=long_output)):
            result = provider.get_failed_logs(42)

        assert len(result) == 4000

    @pytest.mark.parametrize(
        "cp",
        [_cp(stdout=""), _cp(returncode=1)],
    )
    def test_get_failed_logs_returns_fallback(self, provider, cp):
        with patch("improve.ci_gh.run", return_value=cp):
            assert provider.get_failed_logs(42) == "No logs available"

    def test_get_latest_run_id_passes_workflow_and_branch(self, provider):
        with patch("improve.ci_gh.run", return_value=_cp(stdout="[]")) as mock_run:
            provider.get_latest_run_id("my-branch")

        args = mock_run.call_args[0][0]
        assert "--workflow" in args
        assert "my-branch" in args

    def test_get_run_conclusion_passes_run_id_in_command(self, provider):
        with patch("improve.ci_gh.run", return_value=_cp(stdout='{"conclusion": "success"}')) as m:
            result = provider.get_run_conclusion(99)

        assert result == CIConclusion.SUCCESS
        assert "99" in m.call_args[0][0]

    def test_get_failed_logs_passes_timeout(self, provider):
        with patch("improve.ci_gh.run", return_value=_cp(stdout="log output")) as mock_run:
            provider.get_failed_logs(42)

        assert mock_run.call_args[1].get("timeout") == 60

    def test_get_failed_logs_returns_exact_last_4000_chars(self, provider):
        output = "A" * 3000 + "B" * 2000
        with patch("improve.ci_gh.run", return_value=_cp(stdout=output)):
            result = provider.get_failed_logs(42)

        assert result.startswith("A")
        assert result.endswith("B" * 2000)
        assert len(result) == 4000

    def test_get_failed_logs_returns_full_output_under_4000(self, provider):
        output = "x" * 3999
        with patch("improve.ci_gh.run", return_value=_cp(stdout=output)):
            result = provider.get_failed_logs(42)

        assert result == output

    def test_get_latest_run_id_returns_first_element(self, provider):
        with patch(
            "improve.ci_gh.run",
            return_value=_cp(stdout='[{"databaseId": 42}, {"databaseId": 99}]'),
        ):
            assert provider.get_latest_run_id("main") == 42

    def test_get_run_conclusion_returns_none_for_null_conclusion(self, provider):
        with patch("improve.ci_gh.run", return_value=_cp(stdout='{"conclusion": null}')):
            assert provider.get_run_conclusion(42) is None


class TestDiscoverWorkflow:
    def test_skips_discovery_when_workflow_is_explicit(self):
        provider = GitHubCI(workflow="Build")
        with patch("improve.ci_gh.run", return_value=_cp(stdout="[]")) as mock_run:
            provider.get_latest_run_id("main")

        assert mock_run.call_count == 1
        assert mock_run.call_args[0][0][1] == "run"

    def test_discovers_ci_workflow_from_active_workflows(self):
        provider = GitHubCI()
        workflows = '[{"name": "CI", "state": "active"}, {"name": "CodeQL", "state": "active"}]'
        with patch(
            "improve.ci_gh.run",
            side_effect=[_cp(stdout=workflows), _cp(stdout='[{"databaseId": 42}]')],
        ):
            assert provider.get_latest_run_id("main") == 42

    def test_prefers_ci_over_build_in_priority_order(self):
        provider = GitHubCI()
        workflows = '[{"name": "Build", "state": "active"}, {"name": "CI", "state": "active"}]'
        with patch(
            "improve.ci_gh.run",
            side_effect=[_cp(stdout=workflows), _cp(stdout="[]")],
        ) as mock_run:
            provider.get_latest_run_id("main")

        run_list_args = mock_run.call_args_list[1][0][0]
        assert "--workflow" in run_list_args
        idx = run_list_args.index("--workflow")
        assert run_list_args[idx + 1] == "CI"

    def test_omits_workflow_flag_when_no_match_found(self):
        provider = GitHubCI()
        workflows = '[{"name": "CodeQL", "state": "active"}]'
        with patch(
            "improve.ci_gh.run",
            side_effect=[_cp(stdout=workflows), _cp(stdout="[]")],
        ) as mock_run:
            provider.get_latest_run_id("main")

        run_list_args = mock_run.call_args_list[1][0][0]
        assert "--workflow" not in run_list_args

    def test_caches_discovered_workflow_across_calls(self):
        provider = GitHubCI()
        workflows = '[{"name": "CI", "state": "active"}]'
        with patch(
            "improve.ci_gh.run",
            side_effect=[_cp(stdout=workflows), _cp(stdout="[]"), _cp(stdout="[]")],
        ) as mock_run:
            provider.get_latest_run_id("main")
            provider.get_latest_run_id("main")

        workflow_list_calls = [c for c in mock_run.call_args_list if c[0][0][1] == "workflow"]
        assert len(workflow_list_calls) == 1

    def test_falls_back_when_gh_workflow_list_fails(self):
        provider = GitHubCI()
        with patch(
            "improve.ci_gh.run",
            side_effect=[_cp(returncode=1), _cp(stdout='[{"databaseId": 42}]')],
        ):
            assert provider.get_latest_run_id("main") == 42

    def test_ignores_inactive_workflows(self):
        provider = GitHubCI()
        workflows = '[{"name": "CI", "state": "disabled_manually"}]'
        with patch(
            "improve.ci_gh.run",
            side_effect=[_cp(stdout=workflows), _cp(stdout="[]")],
        ) as mock_run:
            provider.get_latest_run_id("main")

        run_list_args = mock_run.call_args_list[1][0][0]
        assert "--workflow" not in run_list_args

    def test_matches_workflow_name_case_insensitively(self):
        provider = GitHubCI()
        workflows = '[{"name": "ci", "state": "active"}]'
        with patch(
            "improve.ci_gh.run",
            side_effect=[_cp(stdout=workflows), _cp(stdout="[]")],
        ) as mock_run:
            provider.get_latest_run_id("main")

        run_list_args = mock_run.call_args_list[1][0][0]
        assert "--workflow" in run_list_args

    def test_handles_malformed_json_in_workflow_list(self):
        provider = GitHubCI()
        with patch(
            "improve.ci_gh.run",
            side_effect=[_cp(stdout="not json"), _cp(stdout='[{"databaseId": 7}]')],
        ):
            assert provider.get_latest_run_id("main") == 7
