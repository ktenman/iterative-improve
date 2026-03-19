from unittest.mock import patch

import pytest

from improve.ci import CIConclusion
from improve.ci_gh import GitHubCI
from tests.conftest import _cp


@pytest.fixture()
def provider() -> GitHubCI:
    return GitHubCI()


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
