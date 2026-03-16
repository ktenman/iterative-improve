import json
from unittest.mock import patch

from improve import ci
from tests import _cp


class TestSetTimeout:
    def test_converts_minutes_to_seconds(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_RUN_TIMEOUT", ci.CI_RUN_TIMEOUT)

        ci.set_timeout(20)

        assert ci.CI_RUN_TIMEOUT == 1200

    def test_rejects_zero_timeout(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_RUN_TIMEOUT", ci.CI_RUN_TIMEOUT)

        ci.set_timeout(1)

        assert ci.CI_RUN_TIMEOUT == 60


class TestGetLatestRunId:
    def test_returns_run_id_from_gh_output(self):
        with patch("improve.ci.run") as mock_run:
            mock_run.return_value = _cp(stdout=json.dumps([{"databaseId": 42}]))

            result = ci.get_latest_run_id("feature")

        assert result == 42

    def test_returns_none_when_no_runs(self):
        with patch("improve.ci.run", return_value=_cp(stdout="[]")):
            result = ci.get_latest_run_id("feature")

        assert result is None

    def test_returns_none_on_command_failure(self):
        with patch("improve.ci.run", return_value=_cp(returncode=1, stderr="error")):
            result = ci.get_latest_run_id("feature")

        assert result is None

    def test_returns_none_and_warns_on_malformed_json_output(self, caplog):
        with patch("improve.ci.run", return_value=_cp(stdout="not valid json")):
            result = ci.get_latest_run_id("feature")

        assert result is None
        assert "Failed to parse CI run list output" in caplog.text

    def test_returns_none_and_warns_when_json_has_unexpected_structure(self, caplog):
        with patch("improve.ci.run", return_value=_cp(stdout='{"error": "rate limited"}')):
            result = ci.get_latest_run_id("feature")

        assert result is None
        assert "Failed to parse CI run list output" in caplog.text


class TestWaitForCi:
    def test_returns_pass_when_no_new_run_detected(self):
        with patch("improve.ci._wait_for_new_run", return_value=None):
            passed, errors, _elapsed = ci.wait_for_ci("feature", known_previous_id=100)

        assert passed is True
        assert errors == ""

    def test_returns_pass_when_run_succeeds(self):
        with (
            patch("improve.ci._wait_for_new_run", return_value=200),
            patch("improve.ci._watch_run", return_value=True),
        ):
            passed, _errors, _elapsed = ci.wait_for_ci("feature", known_previous_id=100)

        assert passed is True

    def test_returns_failure_with_logs_when_run_fails(self):
        with (
            patch("improve.ci._wait_for_new_run", return_value=200),
            patch("improve.ci._watch_run", return_value=False),
            patch("improve.ci._get_run_conclusion", return_value="failure"),
            patch("improve.ci.run", return_value=_cp(stdout="Error: test failed")),
        ):
            passed, errors, _elapsed = ci.wait_for_ci("feature", known_previous_id=100)

        assert passed is False
        assert "test failed" in errors

    def test_retries_when_run_is_cancelled(self):
        with (
            patch("improve.ci._wait_for_new_run", side_effect=[200, 300]),
            patch("improve.ci._watch_run", side_effect=[False, True]),
            patch("improve.ci._get_run_conclusion", return_value="cancelled"),
        ):
            passed, _errors, _elapsed = ci.wait_for_ci("feature", known_previous_id=100)

        assert passed is True

    def test_stops_retrying_cancelled_after_max_attempts(self):
        with (
            patch("improve.ci._wait_for_new_run", side_effect=[200, 300, 400, 500]),
            patch("improve.ci._watch_run", return_value=False),
            patch("improve.ci._get_run_conclusion", return_value="cancelled"),
            patch("improve.ci.run", return_value=_cp(stdout="cancelled")),
        ):
            passed, _errors, _elapsed = ci.wait_for_ci("feature", known_previous_id=100)

        assert passed is False

    def test_does_not_retry_cancelled_when_no_newer_run_found(self):
        with (
            patch("improve.ci._wait_for_new_run", side_effect=[200, None]),
            patch("improve.ci._watch_run", return_value=False),
            patch("improve.ci._get_run_conclusion", return_value="cancelled"),
            patch("improve.ci.run", return_value=_cp(stdout="No logs")),
        ):
            passed, _errors, _elapsed = ci.wait_for_ci("feature", known_previous_id=100)

        assert passed is False


class TestGetRunConclusion:
    def test_returns_conclusion_from_gh_output(self):
        with patch("improve.ci.run", return_value=_cp(stdout='{"conclusion": "success"}')):
            assert ci._get_run_conclusion(42) == "success"

    def test_returns_cancelled_conclusion(self):
        with patch("improve.ci.run", return_value=_cp(stdout='{"conclusion": "cancelled"}')):
            assert ci._get_run_conclusion(42) == "cancelled"

    def test_returns_none_on_command_failure(self):
        with patch("improve.ci.run", return_value=_cp(returncode=1)):
            assert ci._get_run_conclusion(42) is None

    def test_returns_none_on_malformed_json(self):
        with patch("improve.ci.run", return_value=_cp(stdout="not json")):
            assert ci._get_run_conclusion(42) is None

    def test_warns_on_malformed_json(self, caplog):
        with patch("improve.ci.run", return_value=_cp(stdout="not json")):
            ci._get_run_conclusion(42)

        assert "Failed to parse CI run conclusion for run #42" in caplog.text


class TestWatchRun:
    def test_returns_true_on_success(self):
        with patch("improve.ci.run", return_value=_cp()):
            assert ci._watch_run(200) is True

    def test_returns_false_on_failure(self):
        with patch("improve.ci.run", return_value=_cp(returncode=1)):
            assert ci._watch_run(200) is False


class TestWaitForNewRun:
    def test_returns_new_run_after_settle(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_SETTLE_DELAY", 0)
        monkeypatch.setattr(ci, "CI_SETTLE_CHECKS", 1)
        with patch("improve.ci.get_latest_run_id", return_value=200):
            result = ci._wait_for_new_run("feature", 100)

        assert result == 200

    def test_returns_none_when_no_new_run_appears(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_APPEAR_TIMEOUT", 0.01)
        monkeypatch.setattr(ci, "CI_POLL_INTERVAL", 0.001)
        with patch("improve.ci.get_latest_run_id", return_value=100):
            result = ci._wait_for_new_run("feature", 100)

        assert result is None

    def test_settles_on_latest_run_when_ids_keep_changing(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_SETTLE_DELAY", 0)
        monkeypatch.setattr(ci, "CI_SETTLE_CHECKS", 3)
        with patch("improve.ci.get_latest_run_id", side_effect=[200, 300, 400, 400]):
            result = ci._wait_for_new_run("feature", 100)

        assert result == 400

    def test_skips_none_results_during_polling(self, monkeypatch):
        monkeypatch.setattr(ci, "CI_POLL_INTERVAL", 0)
        monkeypatch.setattr(ci, "CI_SETTLE_DELAY", 0)
        monkeypatch.setattr(ci, "CI_SETTLE_CHECKS", 1)
        with patch("improve.ci.get_latest_run_id", side_effect=[None, None, 200, 200]):
            result = ci._wait_for_new_run("feature", 100)

        assert result == 200
