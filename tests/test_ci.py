from unittest.mock import MagicMock, patch

import pytest

from improve import ci
from improve.ci import CIConclusion, CIProvider, CIResult
from improve.ci_gh import GitHubCI
from tests.conftest import _cp, _test_config


class TestGetLatestRunId:
    @pytest.mark.parametrize(
        ("stdout", "returncode", "expected"),
        [
            ('[{"databaseId": 42}]', 0, 42),
            ("[]", 0, None),
            ("not valid json", 0, None),
            ('{"error": "rate limited"}', 0, None),
        ],
    )
    def test_parses_gh_output(self, stdout, returncode, expected):
        config = _test_config(GitHubCI())
        with patch("improve.ci_gh.run", return_value=_cp(stdout=stdout, returncode=returncode)):
            assert ci.get_latest_run_id("feature", config) == expected

    def test_returns_none_on_command_failure(self):
        config = _test_config(GitHubCI())
        with patch("improve.ci_gh.run", return_value=_cp(returncode=1, stderr="error")):
            assert ci.get_latest_run_id("feature", config) is None


class TestWaitForCi:
    def test_returns_ci_result_named_tuple(self):
        config = _test_config()
        with patch("improve.ci._wait_for_new_run", return_value=None):
            result = ci.wait_for_ci("feature", config, known_previous_id=100)

        assert isinstance(result, CIResult)
        assert result.passed is True
        assert result.errors == ""

    def test_fetches_previous_id_when_not_provided(self):
        config = _test_config()
        with (
            patch("improve.ci.get_latest_run_id", return_value=50) as mock_get,
            patch("improve.ci._wait_for_new_run", return_value=None),
        ):
            passed, _errors, _elapsed = ci.wait_for_ci("feature", config)

        assert passed is True
        mock_get.assert_called_once_with("feature", config)

    def test_returns_pass_when_no_new_run_detected(self):
        config = _test_config()
        with patch("improve.ci._wait_for_new_run", return_value=None):
            passed, errors, _elapsed = ci.wait_for_ci("feature", config, known_previous_id=100)

        assert passed is True
        assert errors == ""

    def test_returns_pass_when_run_succeeds(self):
        provider = MagicMock(spec=CIProvider)
        provider.watch_run.return_value = True
        config = _test_config(provider)
        with patch("improve.ci._wait_for_new_run", return_value=200):
            passed, errors, _elapsed = ci.wait_for_ci("feature", config, known_previous_id=100)

        assert passed is True
        assert errors == ""

    def test_returns_failure_with_logs_when_run_fails(self):
        provider = MagicMock(spec=CIProvider)
        provider.watch_run.return_value = False
        provider.get_run_conclusion.return_value = CIConclusion.FAILURE
        provider.get_failed_logs.return_value = "Error: test failed"
        config = _test_config(provider)
        with patch("improve.ci._wait_for_new_run", return_value=200):
            passed, errors, _elapsed = ci.wait_for_ci("feature", config, known_previous_id=100)

        assert passed is False
        assert "test failed" in errors

    def test_retries_when_run_is_cancelled(self):
        provider = MagicMock(spec=CIProvider)
        provider.watch_run.side_effect = [False, True]
        provider.get_run_conclusion.return_value = CIConclusion.CANCELLED
        config = _test_config(provider)
        with patch("improve.ci._wait_for_new_run", side_effect=[200, 300]):
            passed, _errors, _elapsed = ci.wait_for_ci("feature", config, known_previous_id=100)

        assert passed is True

    def test_stops_retrying_cancelled_after_max_attempts(self):
        provider = MagicMock(spec=CIProvider)
        provider.watch_run.return_value = False
        provider.get_run_conclusion.return_value = CIConclusion.CANCELLED
        provider.get_failed_logs.return_value = "cancelled"
        config = _test_config(provider)
        with patch("improve.ci._wait_for_new_run", side_effect=[200, 300, 400, 500]):
            passed, _errors, _elapsed = ci.wait_for_ci("feature", config, known_previous_id=100)

        assert passed is False

    def test_does_not_retry_cancelled_when_no_newer_run_found(self):
        provider = MagicMock(spec=CIProvider)
        provider.watch_run.return_value = False
        provider.get_run_conclusion.return_value = CIConclusion.CANCELLED
        provider.get_failed_logs.return_value = "No logs"
        config = _test_config(provider)
        with patch("improve.ci._wait_for_new_run", side_effect=[200, None]):
            passed, _errors, _elapsed = ci.wait_for_ci("feature", config, known_previous_id=100)

        assert passed is False

    def test_returns_positive_elapsed_time(self):
        provider = MagicMock(spec=CIProvider)
        provider.watch_run.return_value = True
        config = _test_config(provider)
        with patch("improve.ci._wait_for_new_run", return_value=200):
            _passed, _errors, elapsed = ci.wait_for_ci("feature", config, known_previous_id=100)

        assert elapsed >= 0

    def test_settle_breaks_when_get_latest_returns_none(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_SETTLE_DELAY", 0)
        monkeypatch.setattr(ci, "CI_SETTLE_CHECKS", 3)
        config = _test_config()
        with (
            patch("improve.ci.time.sleep"),
            patch("improve.ci.get_latest_run_id", side_effect=[200, None]),
        ):
            result = ci._wait_for_new_run("feature", 100, config)

        assert result == 200


class TestCancelledRetryBoundary:
    def test_exactly_max_cancelled_retries_breaks_loop(self):
        provider = MagicMock(spec=CIProvider)
        provider.watch_run.return_value = False
        provider.get_run_conclusion.return_value = CIConclusion.CANCELLED
        provider.get_failed_logs.return_value = "cancelled"
        config = _test_config(provider)
        with patch("improve.ci._wait_for_new_run", side_effect=[200, 300, 400, 500]):
            passed, _errors, _elapsed = ci.wait_for_ci("feature", config, known_previous_id=100)

        assert passed is False
        assert provider.watch_run.call_count == 4

    def test_non_cancelled_conclusion_breaks_immediately(self):
        provider = MagicMock(spec=CIProvider)
        provider.watch_run.return_value = False
        provider.get_run_conclusion.return_value = CIConclusion.FAILURE
        provider.get_failed_logs.return_value = "err"
        config = _test_config(provider)
        with patch("improve.ci._wait_for_new_run", return_value=200):
            passed, _errors, _elapsed = ci.wait_for_ci("feature", config, known_previous_id=100)

        assert passed is False
        assert provider.watch_run.call_count == 1

    def test_cancelled_retry_increments_run_id(self):
        provider = MagicMock(spec=CIProvider)
        provider.watch_run.side_effect = [False, False, True]
        provider.get_run_conclusion.return_value = CIConclusion.CANCELLED
        config = _test_config(provider)
        with patch("improve.ci._wait_for_new_run", side_effect=[200, 300, 400]):
            passed, _errors, _elapsed = ci.wait_for_ci("feature", config, known_previous_id=100)

        assert passed is True
        assert provider.watch_run.call_count == 3


class TestWaitForNewRunSettleLogic:
    def test_settle_updates_current_id_when_new_run_appears(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_SETTLE_DELAY", 0)
        monkeypatch.setattr(ci, "CI_SETTLE_CHECKS", 3)
        config = _test_config()
        with (
            patch("improve.ci.time.sleep"),
            patch("improve.ci.time.monotonic", side_effect=[0.0, 1.0, 2.0, 3.0, 4.0, 5.0]),
            patch("improve.ci.get_latest_run_id", side_effect=[200, 300, 300, 300]),
        ):
            result = ci._wait_for_new_run("feature", 100, config)

        assert result == 300

    def test_returns_none_when_current_id_equals_previous(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_APPEAR_TIMEOUT", 20)
        monkeypatch.setattr(ci, "CI_POLL_INTERVAL", 10)
        config = _test_config()
        with (
            patch("improve.ci.time.sleep"),
            patch("improve.ci.time.monotonic", side_effect=[0.0, 5.0, 15.0, 25.0]),
            patch("improve.ci.get_latest_run_id", return_value=100),
        ):
            result = ci._wait_for_new_run("feature", 100, config)

        assert result is None

    def test_returns_none_when_get_latest_returns_none(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_APPEAR_TIMEOUT", 20)
        monkeypatch.setattr(ci, "CI_POLL_INTERVAL", 10)
        config = _test_config()
        with (
            patch("improve.ci.time.sleep"),
            patch("improve.ci.time.monotonic", side_effect=[0.0, 5.0, 15.0, 25.0]),
            patch("improve.ci.get_latest_run_id", return_value=None),
        ):
            result = ci._wait_for_new_run("feature", None, config)

        assert result is None


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
        config = _test_config()
        with patch("improve.ci.get_latest_run_id", return_value=200):
            result = ci._wait_for_new_run("feature", 100, config)

        assert result == 200

    def test_returns_none_when_no_new_run_appears(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_APPEAR_TIMEOUT", 30)
        monkeypatch.setattr(ci, "CI_POLL_INTERVAL", 10)
        config = _test_config()
        with patch("improve.ci.get_latest_run_id", return_value=100):
            result = ci._wait_for_new_run("feature", 100, config)

        assert result is None

    def test_settles_on_latest_run_when_ids_keep_changing(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_SETTLE_DELAY", 5)
        monkeypatch.setattr(ci, "CI_SETTLE_CHECKS", 3)
        config = _test_config()
        with patch("improve.ci.get_latest_run_id", side_effect=[200, 300, 400, 400]):
            result = ci._wait_for_new_run("feature", 100, config)

        assert result == 400

    def test_skips_none_results_during_polling(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_POLL_INTERVAL", 10)
        monkeypatch.setattr(ci, "CI_SETTLE_DELAY", 5)
        monkeypatch.setattr(ci, "CI_SETTLE_CHECKS", 1)
        config = _test_config()
        with patch("improve.ci.get_latest_run_id", side_effect=[None, None, 200, 200]):
            result = ci._wait_for_new_run("feature", 100, config)

        assert result == 200
