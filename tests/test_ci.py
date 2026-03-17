import logging
from unittest.mock import patch

import pytest

from improve import ci
from improve.ci import CIProvider
from improve.ci_gh import GitHubCI
from tests import _cp


class TestSetTimeout:
    @pytest.mark.parametrize(
        ("minutes", "expected_seconds"),
        [(1, 60), (20, 1200)],
    )
    def test_converts_minutes_to_seconds(self, monkeypatch, minutes, expected_seconds):
        monkeypatch.setattr(ci, "CI_RUN_TIMEOUT", 0)

        ci.set_timeout(minutes)

        assert expected_seconds == ci.CI_RUN_TIMEOUT


class TestSetProvider:
    def test_replaces_the_active_provider(self, monkeypatch):
        original = ci._provider
        monkeypatch.setattr(ci, "_provider", original)
        new_provider = GitHubCI()

        ci.set_provider(new_provider)

        assert ci._provider is new_provider

    def test_accepts_any_ci_provider_implementation(self, monkeypatch):
        original = ci._provider
        monkeypatch.setattr(ci, "_provider", original)
        provider: CIProvider = GitHubCI()

        ci.set_provider(provider)

        assert ci._provider is provider


class TestGetLatestRunId:
    def test_returns_run_id_from_gh_output(self):
        with patch("improve.ci_gh.run", return_value=_cp(stdout='[{"databaseId": 42}]')):
            result = ci.get_latest_run_id("feature")

        assert result == 42

    def test_returns_none_when_no_runs(self):
        with patch("improve.ci_gh.run", return_value=_cp(stdout="[]")):
            result = ci.get_latest_run_id("feature")

        assert result is None

    def test_returns_none_on_command_failure(self):
        with patch("improve.ci_gh.run", return_value=_cp(returncode=1, stderr="error")):
            result = ci.get_latest_run_id("feature")

        assert result is None

    def test_returns_none_on_malformed_json_output(self):
        with patch("improve.ci_gh.run", return_value=_cp(stdout="not valid json")):
            result = ci.get_latest_run_id("feature")

        assert result is None

    def test_returns_none_when_json_has_unexpected_structure(self):
        with patch("improve.ci_gh.run", return_value=_cp(stdout='{"error": "rate limited"}')):
            result = ci.get_latest_run_id("feature")

        assert result is None


class TestWaitForCi:
    def test_fetches_previous_id_when_not_provided(self):
        with (
            patch("improve.ci.get_latest_run_id", return_value=50) as mock_get,
            patch("improve.ci._wait_for_new_run", return_value=None),
        ):
            passed, _errors, _elapsed = ci.wait_for_ci("feature")

        assert passed is True
        mock_get.assert_called_once_with("feature")

    def test_returns_pass_when_no_new_run_detected(self):
        with patch("improve.ci._wait_for_new_run", return_value=None):
            passed, errors, _elapsed = ci.wait_for_ci("feature", known_previous_id=100)

        assert passed is True
        assert errors == ""

    def test_returns_pass_when_run_succeeds(self):
        with (
            patch("improve.ci._wait_for_new_run", return_value=200),
            patch.object(ci._provider, "watch_run", return_value=True),
        ):
            passed, _errors, _elapsed = ci.wait_for_ci("feature", known_previous_id=100)

        assert passed is True

    def test_returns_failure_with_logs_when_run_fails(self):
        with (
            patch("improve.ci._wait_for_new_run", return_value=200),
            patch.object(ci._provider, "watch_run", return_value=False),
            patch.object(ci._provider, "get_run_conclusion", return_value="failure"),
            patch.object(ci._provider, "get_failed_logs", return_value="Error: test failed"),
        ):
            passed, errors, _elapsed = ci.wait_for_ci("feature", known_previous_id=100)

        assert passed is False
        assert "test failed" in errors

    def test_retries_when_run_is_cancelled(self):
        with (
            patch("improve.ci._wait_for_new_run", side_effect=[200, 300]),
            patch.object(ci._provider, "watch_run", side_effect=[False, True]),
            patch.object(ci._provider, "get_run_conclusion", return_value="cancelled"),
        ):
            passed, _errors, _elapsed = ci.wait_for_ci("feature", known_previous_id=100)

        assert passed is True

    def test_stops_retrying_cancelled_after_max_attempts(self):
        with (
            patch("improve.ci._wait_for_new_run", side_effect=[200, 300, 400, 500]),
            patch.object(ci._provider, "watch_run", return_value=False),
            patch.object(ci._provider, "get_run_conclusion", return_value="cancelled"),
            patch.object(ci._provider, "get_failed_logs", return_value="cancelled"),
        ):
            passed, _errors, _elapsed = ci.wait_for_ci("feature", known_previous_id=100)

        assert passed is False

    def test_does_not_retry_cancelled_when_no_newer_run_found(self):
        with (
            patch("improve.ci._wait_for_new_run", side_effect=[200, None]),
            patch.object(ci._provider, "watch_run", return_value=False),
            patch.object(ci._provider, "get_run_conclusion", return_value="cancelled"),
            patch.object(ci._provider, "get_failed_logs", return_value="No logs"),
        ):
            passed, _errors, _elapsed = ci.wait_for_ci("feature", known_previous_id=100)

        assert passed is False

    def test_logs_waiting_message(self, caplog):
        with (
            patch("improve.ci._wait_for_new_run", return_value=None),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            ci.wait_for_ci("feature", known_previous_id=100)

        assert "Waiting for CI run" in caplog.text

    def test_logs_skipping_when_no_run_detected(self, caplog):
        with (
            patch("improve.ci._wait_for_new_run", return_value=None),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            ci.wait_for_ci("feature", known_previous_id=100)

        assert "No CI run detected" in caplog.text

    def test_logs_passed_message_on_success(self, caplog):
        with (
            patch("improve.ci._wait_for_new_run", return_value=200),
            patch.object(ci._provider, "watch_run", return_value=True),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            ci.wait_for_ci("feature", known_previous_id=100)

        assert "Passed in" in caplog.text

    def test_logs_failed_message_on_failure(self, caplog):
        with (
            patch("improve.ci._wait_for_new_run", return_value=200),
            patch.object(ci._provider, "watch_run", return_value=False),
            patch.object(ci._provider, "get_run_conclusion", return_value="failure"),
            patch.object(ci._provider, "get_failed_logs", return_value="err"),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            ci.wait_for_ci("feature", known_previous_id=100)

        assert "Failed after" in caplog.text

    def test_logs_cancelled_retry_message(self, caplog):
        with (
            patch("improve.ci._wait_for_new_run", side_effect=[200, 300]),
            patch.object(ci._provider, "watch_run", side_effect=[False, True]),
            patch.object(ci._provider, "get_run_conclusion", return_value="cancelled"),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            ci.wait_for_ci("feature", known_previous_id=100)

        assert "was cancelled" in caplog.text

    def test_returns_positive_elapsed_time(self):
        with (
            patch("improve.ci._wait_for_new_run", return_value=200),
            patch.object(ci._provider, "watch_run", return_value=True),
        ):
            _passed, _errors, elapsed = ci.wait_for_ci("feature", known_previous_id=100)

        assert elapsed >= 0

    def test_settle_breaks_when_get_latest_returns_none(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_SETTLE_DELAY", 0)
        monkeypatch.setattr(ci, "CI_SETTLE_CHECKS", 3)
        with (
            patch("improve.ci.time.sleep"),
            patch("improve.ci.get_latest_run_id", side_effect=[200, None]),
        ):
            result = ci._wait_for_new_run("feature", 100)

        assert result == 200


class TestWaitForNewRun:
    @pytest.fixture(autouse=True)
    def _mock_time(self):
        self._clock = 0.0

        def monotonic():
            return self._clock

        def sleep(seconds):
            self._clock += seconds

        with (
            patch("improve.ci.time.monotonic", side_effect=monotonic),
            patch("improve.ci.time.sleep", side_effect=sleep),
        ):
            yield

    def test_returns_new_run_after_settle(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_SETTLE_DELAY", 5)
        monkeypatch.setattr(ci, "CI_SETTLE_CHECKS", 1)
        with patch("improve.ci.get_latest_run_id", return_value=200):
            result = ci._wait_for_new_run("feature", 100)

        assert result == 200

    def test_returns_none_when_no_new_run_appears(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_APPEAR_TIMEOUT", 30)
        monkeypatch.setattr(ci, "CI_POLL_INTERVAL", 10)
        with patch("improve.ci.get_latest_run_id", return_value=100):
            result = ci._wait_for_new_run("feature", 100)

        assert result is None

    def test_settles_on_latest_run_when_ids_keep_changing(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_SETTLE_DELAY", 5)
        monkeypatch.setattr(ci, "CI_SETTLE_CHECKS", 3)
        with patch("improve.ci.get_latest_run_id", side_effect=[200, 300, 400, 400]):
            result = ci._wait_for_new_run("feature", 100)

        assert result == 400

    def test_skips_none_results_during_polling(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_POLL_INTERVAL", 10)
        monkeypatch.setattr(ci, "CI_SETTLE_DELAY", 5)
        monkeypatch.setattr(ci, "CI_SETTLE_CHECKS", 1)
        with patch("improve.ci.get_latest_run_id", side_effect=[None, None, 200, 200]):
            result = ci._wait_for_new_run("feature", 100)

        assert result == 200
