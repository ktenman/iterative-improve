from unittest.mock import patch

import pytest

from improve.ci_gh import GitHubCI
from tests import _cp


@pytest.fixture()
def provider():
    return GitHubCI()


class TestGitHubCI:
    def test_get_latest_run_id_parses_database_id(self, provider):
        with patch("improve.ci_gh.run", return_value=_cp(stdout='[{"databaseId": 99}]')):
            assert provider.get_latest_run_id("main") == 99

    def test_get_run_conclusion_returns_conclusion_field(self, provider):
        with patch("improve.ci_gh.run", return_value=_cp(stdout='{"conclusion": "failure"}')):
            assert provider.get_run_conclusion(42) == "failure"

    def test_get_run_conclusion_returns_none_on_command_failure(self, provider):
        with patch("improve.ci_gh.run", return_value=_cp(returncode=1)):
            assert provider.get_run_conclusion(42) is None

    def test_get_run_conclusion_returns_none_on_malformed_json(self, provider):
        with patch("improve.ci_gh.run", return_value=_cp(stdout="not json")):
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

    def test_get_failed_logs_returns_fallback_when_empty(self, provider):
        with patch("improve.ci_gh.run", return_value=_cp(stdout="")):
            assert provider.get_failed_logs(42) == "No logs available"
