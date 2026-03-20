import json
from unittest.mock import patch

import pytest

from improve.ci import CIConclusion
from improve.ci_glab import GitLabCI
from tests.conftest import _cp


@pytest.fixture()
def provider() -> GitLabCI:
    return GitLabCI()


class TestGitLabCIConstants:
    def test_status_map_has_expected_entries(self):
        from improve.ci_glab import _STATUS_MAP

        assert _STATUS_MAP["success"] == CIConclusion.SUCCESS
        assert _STATUS_MAP["canceled"] == CIConclusion.CANCELLED
        assert _STATUS_MAP["failed"] == CIConclusion.FAILURE
        assert _STATUS_MAP["skipped"] == CIConclusion.FAILURE
        assert len(_STATUS_MAP) == 4


class TestGetLatestRunId:
    def test_returns_pipeline_id_from_glab_output(self, provider):
        with patch("improve.ci_glab.run", return_value=_cp(stdout='[{"id": 777}]')):
            assert provider.get_latest_run_id("feature") == 777

    @pytest.mark.parametrize(
        "cp",
        [_cp(stdout="[]"), _cp(returncode=1), _cp(stdout="not json")],
    )
    def test_returns_none_on_invalid_response(self, provider, cp):
        with patch("improve.ci_glab.run", return_value=cp):
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
            ("success", CIConclusion.SUCCESS),
            ("canceled", CIConclusion.CANCELLED),
            ("failed", CIConclusion.FAILURE),
            ("skipped", CIConclusion.FAILURE),
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
            (CIConclusion.SUCCESS, True),
            (CIConclusion.FAILURE, False),
            (CIConclusion.CANCELLED, False),
        ],
    )
    def test_returns_expected_result_for_conclusion(self, provider, conclusion, expected):
        clock = iter([0.0, 0.0])
        with (
            patch("improve.ci_glab.time.monotonic", side_effect=lambda: next(clock)),
            patch.object(provider, "get_run_conclusion", return_value=conclusion),
        ):
            assert provider.watch_run(1, timeout=60) is expected

    def test_returns_false_on_timeout(self, provider):
        clock = iter([0.0, 0.0, 10.0, 61.0])
        with (
            patch("improve.ci_glab.time.monotonic", side_effect=lambda: next(clock)),
            patch("improve.ci_glab.time.sleep"),
            patch.object(provider, "get_run_conclusion", return_value=None),
        ):
            assert provider.watch_run(1, timeout=60) is False

    def test_polls_until_conclusion_reached(self, provider):
        clock = iter([0.0, 0.0, 10.0, 10.0, 20.0, 20.0, 30.0])
        with (
            patch("improve.ci_glab.time.monotonic", side_effect=lambda: next(clock)),
            patch("improve.ci_glab.time.sleep"),
            patch.object(
                provider, "get_run_conclusion", side_effect=[None, None, CIConclusion.SUCCESS]
            ) as mock_conclusion,
        ):
            result = provider.watch_run(1, timeout=60)

        assert result is True
        assert mock_conclusion.call_count == 3

    def test_logs_progress_during_polling(self, provider, caplog):
        clock = iter([0.0, 0.0, 10.0, 10.0, 20.0])
        with (
            caplog.at_level("INFO", logger="improve"),
            patch("improve.ci_glab.time.monotonic", side_effect=lambda: next(clock)),
            patch("improve.ci_glab.time.sleep"),
            patch.object(provider, "get_run_conclusion", side_effect=[None, CIConclusion.SUCCESS]),
        ):
            provider.watch_run(1, timeout=60)

        assert any("Polling pipeline #1" in r.message for r in caplog.records)


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

    @pytest.mark.parametrize(
        ("side_effect", "expected"),
        [
            ([_cp(returncode=1)], "No logs available"),
            ([_cp(stdout="")], "No logs available"),
        ],
    )
    def test_returns_no_logs_available_on_pipeline_error(self, provider, side_effect, expected):
        with patch("improve.ci_glab.run", side_effect=side_effect):
            assert provider.get_failed_logs(1) == expected

    @pytest.mark.parametrize(
        "stdout, expected",
        [
            (json.dumps({"jobs": [{"id": 10, "status": "success"}]}), "No failed jobs found"),
            ("[1, 2, 3]", "No failed jobs found"),
        ],
    )
    def test_returns_no_failed_jobs_fallback(self, provider, stdout, expected):
        with patch("improve.ci_glab.run", return_value=_cp(stdout=stdout)):
            assert provider.get_failed_logs(1) == expected

    @pytest.mark.parametrize(
        "trace_cp",
        [_cp(stdout=""), _cp(returncode=1, stdout="")],
    )
    def test_returns_no_logs_when_trace_fails(self, provider, trace_cp):
        pipeline = self._pipeline_json([{"id": 10, "status": "failed"}])
        with patch("improve.ci_glab.run", side_effect=[_cp(stdout=pipeline), trace_cp]):
            assert provider.get_failed_logs(1) == "No logs available"
