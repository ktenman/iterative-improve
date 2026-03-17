import json
from unittest.mock import patch

import pytest

from improve.ci_glab import GitLabCI
from tests import _cp


@pytest.fixture()
def provider():
    return GitLabCI()


class TestGetLatestRunId:
    def test_returns_pipeline_id_from_glab_output(self, provider):
        with patch("improve.ci_glab.run", return_value=_cp(stdout='[{"id": 777}]')):
            assert provider.get_latest_run_id("feature") == 777

    def test_returns_none_when_no_pipelines(self, provider):
        with patch("improve.ci_glab.run", return_value=_cp(stdout="[]")):
            assert provider.get_latest_run_id("feature") is None

    def test_returns_none_on_command_failure(self, provider):
        with patch("improve.ci_glab.run", return_value=_cp(returncode=1)):
            assert provider.get_latest_run_id("feature") is None

    def test_returns_none_on_malformed_json(self, provider):
        with patch("improve.ci_glab.run", return_value=_cp(stdout="not json")):
            assert provider.get_latest_run_id("feature") is None

    def test_passes_branch_and_per_page_flags(self, provider):
        with patch("improve.ci_glab.run", return_value=_cp(stdout="[]")) as mock_run:
            provider.get_latest_run_id("my-branch")

        mock_run.assert_called_once_with(
            ["glab", "ci", "list", "--ref", "my-branch", "--per-page", "1", "-F", "json"]
        )


class TestGetRunConclusion:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("success", "success"),
            ("canceled", "cancelled"),
            ("failed", "failure"),
            ("skipped", "failure"),
        ],
    )
    def test_maps_gitlab_status_to_expected_conclusion(self, provider, status, expected):
        with patch(
            "improve.ci_glab.run",
            return_value=_cp(stdout=json.dumps({"status": status})),
        ):
            assert provider.get_run_conclusion(1) == expected

    @pytest.mark.parametrize("status", ["running", "pending"])
    def test_returns_none_for_in_progress_status(self, provider, status):
        with patch(
            "improve.ci_glab.run",
            return_value=_cp(stdout=json.dumps({"status": status})),
        ):
            assert provider.get_run_conclusion(1) is None

    def test_returns_none_on_command_failure(self, provider):
        with patch("improve.ci_glab.run", return_value=_cp(returncode=1)):
            assert provider.get_run_conclusion(1) is None

    def test_returns_none_on_malformed_json(self, provider):
        with patch("improve.ci_glab.run", return_value=_cp(stdout="bad")):
            assert provider.get_run_conclusion(1) is None

    def test_returns_none_when_status_is_unhashable(self, provider):
        with patch(
            "improve.ci_glab.run",
            return_value=_cp(stdout=json.dumps({"status": ["not", "a", "string"]})),
        ):
            assert provider.get_run_conclusion(1) is None


class TestWatchRun:
    @pytest.mark.parametrize(
        ("conclusion", "expected"),
        [
            ("success", True),
            ("failure", False),
            ("cancelled", False),
        ],
    )
    def test_returns_expected_result_for_conclusion(self, provider, conclusion, expected):
        with patch.object(provider, "get_run_conclusion", return_value=conclusion):
            assert provider.watch_run(1, timeout=60) is expected

    def test_returns_false_on_timeout(self, provider, monkeypatch):
        monkeypatch.setattr("improve.ci_glab.POLL_INTERVAL", 0.001)
        with patch.object(provider, "get_run_conclusion", return_value=None):
            assert provider.watch_run(1, timeout=0) is False

    def test_polls_until_conclusion_reached(self, provider, monkeypatch):
        monkeypatch.setattr("improve.ci_glab.POLL_INTERVAL", 0.001)
        with patch.object(
            provider, "get_run_conclusion", side_effect=[None, None, "success"]
        ) as mock_conclusion:
            result = provider.watch_run(1, timeout=60)

        assert result is True
        assert mock_conclusion.call_count == 3


class TestGetFailedLogs:
    def _pipeline_json(self, jobs: list[dict]) -> str:
        return json.dumps({"jobs": jobs})

    def test_returns_log_output_for_failed_jobs(self, provider):
        pipeline = self._pipeline_json([{"id": 10, "status": "failed"}])
        with patch(
            "improve.ci_glab.run",
            side_effect=[
                _cp(stdout=pipeline),
                _cp(stdout="error details"),
            ],
        ):
            assert provider.get_failed_logs(1) == "error details"

    def test_concatenates_logs_from_multiple_failed_jobs(self, provider):
        pipeline = self._pipeline_json(
            [
                {"id": 10, "status": "failed"},
                {"id": 11, "status": "failed"},
            ]
        )
        with patch(
            "improve.ci_glab.run",
            side_effect=[
                _cp(stdout=pipeline),
                _cp(stdout="log A"),
                _cp(stdout="log B"),
            ],
        ):
            assert provider.get_failed_logs(1) == "log A\nlog B"

    def test_skips_successful_jobs(self, provider):
        pipeline = self._pipeline_json(
            [
                {"id": 10, "status": "success"},
                {"id": 11, "status": "failed"},
            ]
        )
        with patch(
            "improve.ci_glab.run",
            side_effect=[
                _cp(stdout=pipeline),
                _cp(stdout="only failure"),
            ],
        ):
            assert provider.get_failed_logs(1) == "only failure"

    def test_truncates_long_output(self, provider):
        pipeline = self._pipeline_json([{"id": 10, "status": "failed"}])
        with patch(
            "improve.ci_glab.run",
            side_effect=[
                _cp(stdout=pipeline),
                _cp(stdout="x" * 5000),
            ],
        ):
            result = provider.get_failed_logs(1)

        assert len(result) == 4000

    def test_returns_fallback_when_pipeline_command_fails(self, provider):
        with patch("improve.ci_glab.run", return_value=_cp(returncode=1)):
            assert provider.get_failed_logs(1) == "No logs available"

    def test_returns_fallback_when_no_failed_jobs(self, provider):
        pipeline = self._pipeline_json([{"id": 10, "status": "success"}])
        with patch("improve.ci_glab.run", return_value=_cp(stdout=pipeline)):
            assert provider.get_failed_logs(1) == "No failed jobs found"

    def test_returns_fallback_when_jobs_payload_is_non_dict(self, provider):
        with patch("improve.ci_glab.run", return_value=_cp(stdout="[1, 2, 3]")):
            assert provider.get_failed_logs(1) == "No failed jobs found"

    def test_returns_fallback_when_stdout_is_empty(self, provider):
        with patch("improve.ci_glab.run", return_value=_cp(stdout="")):
            assert provider.get_failed_logs(1) == "No logs available"

    def test_returns_fallback_when_trace_output_is_empty(self, provider):
        pipeline = self._pipeline_json([{"id": 10, "status": "failed"}])
        with patch(
            "improve.ci_glab.run",
            side_effect=[
                _cp(stdout=pipeline),
                _cp(stdout=""),
            ],
        ):
            assert provider.get_failed_logs(1) == "No logs available"

    def test_returns_fallback_when_trace_command_fails(self, provider):
        pipeline = self._pipeline_json([{"id": 10, "status": "failed"}])
        with patch(
            "improve.ci_glab.run",
            side_effect=[
                _cp(stdout=pipeline),
                _cp(returncode=1, stdout=""),
            ],
        ):
            assert provider.get_failed_logs(1) == "No logs available"
