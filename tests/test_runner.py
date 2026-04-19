import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from improve.config import Config
from improve.mode import Mode
from improve.phases import AVAILABLE_PHASES
from improve.runner import MAX_CI_RETRIES, IterationLoop
from improve.state import LoopState, PhaseResult
from tests.conftest import _test_config


def _make_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skip_ci: bool = False,
    mode: Mode = Mode.SEQUENTIAL,
    phases: list[str] | None = None,
    squash: bool = False,
    continuous: bool = False,
    config: Config | None = None,
) -> IterationLoop:
    monkeypatch.setattr("improve.state.STATE_DIR", tmp_path)
    monkeypatch.setattr("improve.state.STATE_FILE", tmp_path / "state.json")
    state = LoopState(branch="feature", started_at="2025-01-01T00:00:00")
    if phases is None:
        phases = list(AVAILABLE_PHASES)
    if config is None:
        config = _test_config()
    return IterationLoop(
        state=state,
        skip_ci=skip_ci,
        mode=mode,
        phases=phases,
        config=config,
        squash=squash,
        continuous=continuous,
    )


class TestShutdown:
    def test_saves_state_and_exits_with_code_130(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        loop.loop_start = 1.0

        with (
            patch("improve.claude.terminate_active"),
            patch("builtins.print"),
            pytest.raises(SystemExit) as exc_info,
        ):
            loop.shutdown(2, None)

        assert exc_info.value.code == 130

    def test_exits_gracefully_when_state_save_raises_non_os_error(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        loop.loop_start = 1.0

        with (
            patch("improve.claude.terminate_active"),
            patch.object(loop.state, "save", side_effect=TypeError("not serializable")),
            patch("builtins.print"),
            pytest.raises(SystemExit) as exc_info,
        ):
            loop.shutdown(2, None)

        assert exc_info.value.code == 130

    def test_calls_terminate_active_before_exiting(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)

        with (
            patch("improve.claude.terminate_active") as mock_terminate,
            patch("builtins.print"),
            pytest.raises(SystemExit),
        ):
            loop.shutdown(2, None)

        mock_terminate.assert_called_once()


class TestRetryCiFixes:
    def test_returns_immediately_when_ci_already_passes(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)

        passed, retries, _, _ = loop.retry_ci_fixes(True, "", "Fix CI")

        assert passed is True
        assert retries == 0

    def test_succeeds_on_first_retry_attempt(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.claude.run_claude", return_value=("output", 1.5)),
            patch("improve.git.has_changes", return_value=True),
            patch("improve.ci.get_latest_run_id", return_value=100),
            patch("improve.git.commit_and_push", return_value=True),
            patch("improve.ci.wait_for_ci", return_value=(True, "", 3.0)),
        ):
            passed, retries, claude_time, ci_time = loop.retry_ci_fixes(False, "err", "Fix")

        assert passed is True
        assert retries == 1
        assert claude_time == 1.5
        assert ci_time == 3.0

    def test_stops_when_no_changes_produced(self, tmp_path, monkeypatch, caplog):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.claude.run_claude", return_value=("", 1.0)),
            patch("improve.git.has_changes", return_value=False),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            passed, retries, _, _ = loop.retry_ci_fixes(False, "err", "Fix")

        assert passed is False
        assert retries == 1
        assert "No fix produced" in caplog.text

    def test_stops_when_push_fails(self, tmp_path, monkeypatch, caplog):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.claude.run_claude", return_value=("", 1.0)),
            patch("improve.git.has_changes", return_value=True),
            patch("improve.git.commit_and_push", return_value=False),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            passed, retries, _, _ = loop.retry_ci_fixes(False, "err", "Fix")

        assert passed is False
        assert retries == 1
        assert "Push failed" in caplog.text

    def test_passes_pre_push_id_to_wait_for_ci(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.claude.run_claude", return_value=("out", 1.0)),
            patch("improve.git.has_changes", return_value=True),
            patch("improve.ci.get_latest_run_id", return_value=42),
            patch("improve.git.commit_and_push", return_value=True),
            patch("improve.ci.wait_for_ci", return_value=(True, "", 1.0)) as mock_wait,
        ):
            loop.retry_ci_fixes(False, "err", "Fix")

        mock_wait.assert_called_once_with("feature", loop.config, known_previous_id=42)

    def test_exhausts_all_retry_attempts(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.claude.run_claude", return_value=("out", 1.0)),
            patch("improve.git.has_changes", return_value=True),
            patch("improve.ci.get_latest_run_id", return_value=100),
            patch("improve.git.commit_and_push", return_value=True),
            patch("improve.ci.wait_for_ci", return_value=(False, "still failing", 2.0)),
        ):
            passed, retries, _, _ = loop.retry_ci_fixes(False, "err", "Fix")

        assert passed is False
        assert retries == MAX_CI_RETRIES

    def test_accumulates_claude_and_ci_time_across_retries(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.claude.run_claude", return_value=("out", 1.0)),
            patch("improve.git.has_changes", return_value=True),
            patch("improve.ci.get_latest_run_id", return_value=100),
            patch("improve.git.commit_and_push", return_value=True),
            patch(
                "improve.ci.wait_for_ci",
                side_effect=[(False, "error", 2.0), (True, "", 2.0)],
            ),
        ):
            passed, retries, claude_time, ci_time = loop.retry_ci_fixes(False, "err", "Fix")

        assert passed is True
        assert retries == 2
        assert claude_time == 2.0
        assert ci_time == 4.0

    def test_passes_commit_message_with_attempt_number(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.claude.run_claude", return_value=("out", 1.0)),
            patch("improve.git.has_changes", return_value=True),
            patch("improve.ci.get_latest_run_id", return_value=100),
            patch("improve.git.commit_and_push", return_value=True) as mock_push,
            patch("improve.ci.wait_for_ci", return_value=(True, "", 1.0)),
        ):
            loop.retry_ci_fixes(False, "err", "Fix CI after simplify")

        msg = mock_push.call_args[0][0]
        assert msg == "Fix CI after simplify (attempt 1)"

    def test_retries_exactly_max_times(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        call_count = 0

        def count_calls(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return ("out", 1.0)

        with (
            patch("improve.claude.run_claude", side_effect=count_calls),
            patch("improve.git.has_changes", return_value=True),
            patch("improve.ci.get_latest_run_id", return_value=100),
            patch("improve.git.commit_and_push", return_value=True),
            patch("improve.ci.wait_for_ci", return_value=(False, "err", 1.0)),
        ):
            _, retries, _, _ = loop.retry_ci_fixes(False, "err", "Fix")

        assert retries == MAX_CI_RETRIES
        assert call_count == MAX_CI_RETRIES

    def test_stops_retries_when_claude_raises_runtime_error(self, tmp_path, monkeypatch, caplog):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.claude.run_claude", side_effect=RuntimeError("Claude crashed")),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            passed, retries, claude_time, _ci_time = loop.retry_ci_fixes(False, "err", "Fix")

        assert passed is False
        assert retries == 1
        assert claude_time == 0.0
        assert "Claude failed" in caplog.text


class TestRunPhase:
    def test_returns_no_changes_when_claude_makes_none(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="file.py"),
            patch("improve.claude.run_claude", return_value=("NO_CHANGES_NEEDED", 1.0)),
            patch("improve.git.changed_files", return_value=[]),
        ):
            result = loop.run_phase("simplify", 1, skip_ci=False)

        assert result.changes_made is False
        assert result.summary == "No changes needed"

    def test_skips_ci_when_skip_ci_is_true(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="file.py"),
            patch("improve.claude.run_claude", return_value=("SUMMARY: Fixed stuff", 1.0)),
            patch("improve.git.changed_files", return_value=["file.py"]),
            patch("improve.git.commit_and_push", return_value=True),
            patch("improve.ci.wait_for_ci") as mock_wait,
        ):
            result = loop.run_phase("simplify", 1, skip_ci=True)

        assert result.changes_made is True
        mock_wait.assert_not_called()

    def test_waits_for_ci_when_not_skipped(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="file.py"),
            patch("improve.claude.run_claude", return_value=("SUMMARY: Fixed bug", 1.0)),
            patch("improve.git.changed_files", return_value=["file.py"]),
            patch("improve.ci.get_latest_run_id", return_value=100),
            patch("improve.git.commit_and_push", return_value=True),
            patch("improve.ci.wait_for_ci", return_value=(True, "", 2.0)),
            patch.object(loop, "retry_ci_fixes", return_value=(True, 0, 0.0, 0.0)),
        ):
            result = loop.run_phase("review", 1, skip_ci=False)

        assert result.changes_made is True
        assert result.ci_passed is True

    def test_does_not_wait_for_ci_when_commit_fails(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="file.py"),
            patch("improve.claude.run_claude", return_value=("SUMMARY: Stuff", 1.0)),
            patch("improve.git.changed_files", return_value=["file.py"]),
            patch("improve.git.commit_and_push", return_value=False),
            patch("improve.ci.wait_for_ci") as mock_wait,
        ):
            result = loop.run_phase("simplify", 1, skip_ci=False)

        assert result.ci_passed is False
        mock_wait.assert_not_called()

    def test_includes_ci_time_in_duration_log(self, tmp_path, monkeypatch, caplog):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="file.py"),
            patch("improve.claude.run_claude", return_value=("SUMMARY: Fixed bug", 1.0)),
            patch("improve.git.changed_files", return_value=["file.py"]),
            patch("improve.ci.get_latest_run_id", return_value=100),
            patch("improve.git.commit_and_push", return_value=True),
            patch("improve.ci.wait_for_ci", return_value=(True, "", 5.0)),
            patch.object(loop, "retry_ci_fixes", return_value=(True, 0, 0.0, 0.0)),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            result = loop.run_phase("simplify", 1, skip_ci=False)

        assert result.ci_seconds == 5.0

    def test_runs_security_phase(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="file.py"),
            patch("improve.claude.run_claude", return_value=("SUMMARY: Fixed XSS", 1.0)),
            patch("improve.git.changed_files", return_value=["file.py"]),
            patch("improve.git.commit_and_push", return_value=True),
        ):
            result = loop.run_phase("security", 1, skip_ci=True)

        assert result.phase == "security"

    def test_phase_result_has_correct_iteration_number(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="file.py"),
            patch("improve.claude.run_claude", return_value=("SUMMARY: Done", 2.0)),
            patch("improve.git.changed_files", return_value=["file.py"]),
            patch("improve.git.commit_and_push", return_value=True),
        ):
            result = loop.run_phase("simplify", 3, skip_ci=True)

        assert result.iteration == 3

    def test_no_changes_result_has_correct_phase(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="file.py"),
            patch("improve.claude.run_claude", return_value=("nothing", 1.0)),
            patch("improve.git.changed_files", return_value=[]),
        ):
            result = loop.run_phase("security", 1, skip_ci=True)

        assert result.phase == "security"
        assert result.ci_passed is True

    def test_fetches_pre_push_id_when_ci_not_skipped(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="file.py"),
            patch("improve.claude.run_claude", return_value=("SUMMARY: Done", 1.0)),
            patch("improve.git.changed_files", return_value=["file.py"]),
            patch("improve.ci.get_latest_run_id", return_value=42) as mock_get_id,
            patch("improve.git.commit_and_push", return_value=True),
            patch("improve.ci.wait_for_ci", return_value=(True, "", 1.0)),
            patch.object(loop, "retry_ci_fixes", return_value=(True, 0, 0.0, 0.0)),
        ):
            loop.run_phase("simplify", 1, skip_ci=False)

        mock_get_id.assert_called_once_with("feature", loop.config)

    def test_does_not_fetch_pre_push_id_when_ci_skipped(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="file.py"),
            patch("improve.claude.run_claude", return_value=("SUMMARY: Done", 1.0)),
            patch("improve.git.changed_files", return_value=["file.py"]),
            patch("improve.ci.get_latest_run_id") as mock_get_id,
            patch("improve.git.commit_and_push", return_value=True),
        ):
            loop.run_phase("simplify", 1, skip_ci=True)

        mock_get_id.assert_not_called()


class TestRunBatchIteration:
    def test_returns_false_when_no_changes_in_any_phase(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, mode=Mode.BATCH)
        no_changes = PhaseResult(1, "simplify", False, [], "No changes", True, 0)

        with patch.object(loop, "run_phase", return_value=no_changes):
            result = loop.run_batch_iteration(1)

        assert result is False

    def test_returns_false_when_push_fails(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, mode=Mode.BATCH)
        push_failed = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", False, 0)

        with patch.object(loop, "run_phase", return_value=push_failed):
            result = loop.run_batch_iteration(1)

        assert result is False

    def test_checks_ci_after_all_phases_complete(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, mode=Mode.BATCH, skip_ci=False)
        changed = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", True, 0)
        with (
            patch("improve.ci.get_latest_run_id", return_value=100),
            patch.object(loop, "run_phase", return_value=changed),
            patch("improve.ci.wait_for_ci", return_value=(True, "", 2.0)),
            patch.object(loop, "retry_ci_fixes", return_value=(True, 0, 0.0, 0.0)),
        ):
            result = loop.run_batch_iteration(1)

        assert result is True

    def test_skips_ci_check_when_skip_ci_is_true(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, mode=Mode.BATCH, skip_ci=True)
        changed = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", True, 0)
        with (
            patch.object(loop, "run_phase", return_value=changed),
            patch("improve.ci.wait_for_ci") as mock_wait,
        ):
            result = loop.run_batch_iteration(1)

        assert result is True
        mock_wait.assert_not_called()


class TestRunBatchCIFailure:
    def test_batch_stops_when_ci_fails(self, tmp_path, monkeypatch):
        loop = _make_loop(
            tmp_path,
            monkeypatch,
            mode=Mode.BATCH,
            skip_ci=False,
            phases=["simplify"],
        )
        changed = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", True, 0)

        with (
            patch("improve.ci.get_latest_run_id", return_value=100),
            patch.object(loop, "run_phase", return_value=changed),
            patch("improve.ci.wait_for_ci", return_value=(False, "error", 2.0)),
            patch.object(loop, "retry_ci_fixes", return_value=(False, 1, 1.0, 2.0)),
        ):
            result = loop.run_batch_iteration(1)

        assert result is False


class TestRunSequentialIteration:
    def test_stops_when_ci_fails_after_first_phase(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        ci_failed = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", False, 0)

        with patch.object(loop, "run_phase", return_value=ci_failed):
            result = loop.run_sequential_iteration(1)

        assert result is False

    def test_stops_when_ci_fails_after_second_phase(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        results = [
            PhaseResult(1, "simplify", True, ["a.py"], "Stuff", True, 0),
            PhaseResult(1, "review", True, ["b.py"], "More stuff", False, 0),
        ]

        with patch.object(loop, "run_phase", side_effect=results):
            result = loop.run_sequential_iteration(1)

        assert result is False

    def test_returns_false_when_converged_with_no_changes(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        no_changes = PhaseResult(1, "simplify", False, [], "No changes", True, 0)

        with patch.object(loop, "run_phase", return_value=no_changes):
            result = loop.run_sequential_iteration(1)

        assert result is False

    def test_returns_true_when_changes_made_and_ci_passes(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        changed = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", True, 0)

        with patch.object(loop, "run_phase", return_value=changed):
            result = loop.run_sequential_iteration(1)

        assert result is True

    def test_runs_only_specified_phases(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, phases=["security"])
        changed = PhaseResult(1, "security", True, ["a.py"], "Fixed XSS", True, 0)

        with patch.object(loop, "run_phase", return_value=changed) as mock_phase:
            loop.run_sequential_iteration(1)

        mock_phase.assert_called_once()


class TestSquashBranch:
    def test_squashes_when_changes_exist(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, squash=True)
        loop.state.add(PhaseResult(1, "simplify", True, ["a.py"], "Extracted helper", True, 0))

        with (
            patch("improve.git.squash_branch", return_value=True) as mock_squash,
            patch("improve.git.sync_with_main", return_value=True),
            patch("improve.git.diff_vs_main", return_value="diff"),
            patch("improve.claude.run_claude", return_value=("", 0.0)),
            patch.object(loop, "run_sequential_iteration", return_value=False),
        ):
            loop.run(1, 1)

        mock_squash.assert_called_once()

    def test_skips_squash_when_no_changes(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, squash=True)

        with (
            patch("improve.git.squash_branch") as mock_squash,
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(loop, "run_sequential_iteration", return_value=False),
        ):
            loop.run(1, 1)

        mock_squash.assert_not_called()

    def test_does_not_squash_when_flag_is_false(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, squash=False)
        loop.state.add(PhaseResult(1, "simplify", True, ["a.py"], "Stuff", True, 0))

        with (
            patch("improve.git.squash_branch") as mock_squash,
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(loop, "run_sequential_iteration", return_value=False),
        ):
            loop.run(1, 1)

        mock_squash.assert_not_called()

    def test_uses_strip_code_fences_on_claude_output(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, squash=True)
        loop.state.add(PhaseResult(1, "simplify", True, ["a.py"], "Stuff", True, 0))

        with (
            patch("improve.git.diff_vs_main", return_value="diff"),
            patch("improve.claude.run_claude", return_value=("```\nClean message\n```", 0.0)),
            patch("improve.git.squash_branch", return_value=True) as mock_squash,
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(loop, "run_sequential_iteration", return_value=False),
        ):
            loop.run(1, 1)

        message = mock_squash.call_args[0][1]
        assert message == "Clean message"

    def test_uses_fallback_when_claude_returns_empty(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, squash=True)
        loop.state.add(PhaseResult(1, "simplify", True, ["a.py"], "Extracted helper", True, 0))

        with (
            patch("improve.git.diff_vs_main", return_value="diff"),
            patch("improve.claude.run_claude", return_value=("", 0.0)),
            patch("improve.git.squash_branch", return_value=True) as mock_squash,
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(loop, "run_sequential_iteration", return_value=False),
        ):
            loop.run(1, 1)

        message = mock_squash.call_args[0][1]
        assert "Extracted helper" in message

    def test_uses_fallback_when_claude_raises_runtime_error(self, tmp_path, monkeypatch, caplog):
        loop = _make_loop(tmp_path, monkeypatch, squash=True)
        loop.state.add(PhaseResult(1, "simplify", True, ["a.py"], "Extracted helper", True, 0))

        with (
            patch("improve.git.diff_vs_main", return_value="diff"),
            patch("improve.claude.run_claude", side_effect=RuntimeError("Claude crashed")),
            patch("improve.git.squash_branch", return_value=True) as mock_squash,
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(loop, "run_sequential_iteration", return_value=False),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            loop.run(1, 1)

        message = mock_squash.call_args[0][1]
        assert "Extracted helper" in message
        assert "Claude failed during squash" in caplog.text


class TestRun:
    def test_stops_on_merge_conflict(self, tmp_path, monkeypatch, capsys):
        loop = _make_loop(tmp_path, monkeypatch)

        with patch("improve.git.sync_with_main", return_value=False):
            loop.run(1, 3)

        output = capsys.readouterr().out
        assert "Results" in output

    def test_dispatches_to_sequential_when_not_batch(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)

        with (
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(loop, "run_sequential_iteration", return_value=False) as mock_seq,
        ):
            loop.run(1, 1)

        mock_seq.assert_called_once_with(1)

    def test_dispatches_to_batch_when_batch_mode(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, mode=Mode.BATCH)

        with (
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(loop, "run_batch_iteration", return_value=False) as mock_batch,
        ):
            loop.run(1, 1)

        mock_batch.assert_called_once_with(1)

    def test_runs_multiple_iterations_until_convergence(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)

        with (
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(loop, "run_sequential_iteration", side_effect=[True, False]),
        ):
            loop.run(1, 5)

        assert loop.state.iteration == 2

    def test_saves_state_each_iteration(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(loop, "run_sequential_iteration", return_value=False),
        ):
            loop.run(1, 1)

        assert loop.state.iteration == 1
        assert (tmp_path / "state.json").exists()


class TestIntegration:
    def test_full_loop_with_changes_then_convergence(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, skip_ci=True, phases=["simplify", "review"])
        phase_results = [
            PhaseResult(1, "simplify", True, ["a.py"], "Extracted helper", True, 0),
            PhaseResult(1, "review", True, ["b.py"], "Fixed null check", True, 0),
            PhaseResult(2, "simplify", False, [], "No changes needed", True, 0),
            PhaseResult(2, "review", False, [], "No changes needed", True, 0),
        ]

        with (
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(loop, "run_phase", side_effect=phase_results),
        ):
            loop.run(1, 5)

        assert loop.state.iteration == 2
        assert len(loop.state.results) == 4

    def test_full_loop_stops_on_ci_failure(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, skip_ci=True, phases=["simplify"])
        ci_failed = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", False, 0)

        with (
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(loop, "run_phase", return_value=ci_failed),
        ):
            loop.run(1, 5)

        assert loop.state.iteration == 1
        assert len(loop.state.results) == 1


class TestRunParallelDispatch:
    def test_dispatches_to_parallel_when_parallel_mode(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, mode=Mode.PARALLEL)

        with (
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(
                loop,
                "run_parallel_batch_iteration",
                return_value=False,
            ) as mock_parallel,
        ):
            loop.run(1, 1)

        mock_parallel.assert_called_once_with(1)


class TestDropConvergedPhases:
    def test_crashed_phase_is_not_dropped_from_active_phases(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, phases=["simplify", "review"], skip_ci=True)
        results = [
            PhaseResult.crashed(1, "simplify"),
            PhaseResult(1, "review", True, ["b.py"], "Fixed", True, 0),
        ]

        loop._drop_converged_phases(results)

        assert "simplify" in loop._active_phases

    def test_converged_phase_is_dropped_from_active_phases(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, phases=["simplify", "review"], skip_ci=True)
        results = [
            PhaseResult(1, "simplify", False, [], "No changes needed", True, 0),
            PhaseResult(1, "review", True, ["b.py"], "Fixed", True, 0),
        ]

        loop._drop_converged_phases(results)

        assert "simplify" not in loop._active_phases
        assert "review" in loop._active_phases

    def test_no_phases_dropped_when_all_have_changes(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, phases=["simplify", "review"])
        results = [
            PhaseResult(1, "simplify", True, ["a.py"], "Changed", True, 0),
            PhaseResult(1, "review", True, ["b.py"], "Changed", True, 0),
        ]

        loop._drop_converged_phases(results)

        assert loop._active_phases == ["simplify", "review"]

    def test_all_phases_dropped_when_none_have_changes(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, phases=["simplify", "review"])
        results = [
            PhaseResult(1, "simplify", False, [], "No changes needed", True, 0),
            PhaseResult(1, "review", False, [], "No changes needed", True, 0),
        ]

        loop._drop_converged_phases(results)

        assert loop._active_phases == []


class TestRunPhaseSafe:
    def test_catches_exception_and_discards_changes(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, skip_ci=True, phases=["simplify"])

        with (
            patch.object(loop, "run_phase", side_effect=RuntimeError("boom")),
            patch("improve.git.discard_changes") as mock_discard,
        ):
            result = loop._run_phase_safe("simplify", 1, True)

        assert result.summary == "Phase crashed"
        assert result.changes_made is False
        mock_discard.assert_called_once()

    def test_handles_discard_changes_also_raising(self, tmp_path, monkeypatch, caplog):
        loop = _make_loop(tmp_path, monkeypatch, skip_ci=True, phases=["simplify"])

        with (
            patch.object(loop, "run_phase", side_effect=RuntimeError("boom")),
            patch("improve.git.discard_changes", side_effect=RuntimeError("double boom")),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            result = loop._run_phase_safe("simplify", 1, True)

        assert result.summary == "Phase crashed"
        assert "Failed to discard changes" in caplog.text


class TestCrashRecovery:
    def test_sequential_continues_after_phase_crash(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, phases=["simplify", "review"], skip_ci=True)
        ok_result = PhaseResult(1, "review", True, ["b.py"], "Fixed", True, 0)

        with (
            patch.object(
                loop,
                "run_phase",
                side_effect=[RuntimeError("boom"), ok_result],
            ),
            patch("improve.git.discard_changes"),
        ):
            result = loop.run_sequential_iteration(1)

        assert result is True
        assert len(loop.state.results) == 2
        assert loop.state.results[0]["summary"] == "Phase crashed"
        assert loop.state.results[1]["changes_made"] is True

    def test_batch_continues_after_phase_crash(self, tmp_path, monkeypatch):
        loop = _make_loop(
            tmp_path,
            monkeypatch,
            mode=Mode.BATCH,
            skip_ci=True,
            phases=["simplify", "review"],
        )
        ok_result = PhaseResult(1, "review", True, ["b.py"], "Fixed", True, 0)

        with (
            patch.object(
                loop,
                "run_phase",
                side_effect=[RuntimeError("crash"), ok_result],
            ),
            patch("improve.git.discard_changes"),
        ):
            result = loop.run_batch_iteration(1)

        assert result is True
        assert loop.state.results[0]["summary"] == "Phase crashed"


class TestBatchSkipsCiGet:
    def test_batch_does_not_fetch_pre_batch_run_id_when_skip_ci(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, mode=Mode.BATCH, skip_ci=True)
        changed = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", True, 0)
        with (
            patch("improve.ci.get_latest_run_id") as mock_get_id,
            patch.object(loop, "run_phase", return_value=changed),
        ):
            loop.run_batch_iteration(1)

        mock_get_id.assert_not_called()

    def test_batch_fetches_pre_batch_run_id_when_ci_enabled(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, mode=Mode.BATCH, skip_ci=False)
        changed = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", True, 0)
        with (
            patch("improve.ci.get_latest_run_id", return_value=100) as mock_get_id,
            patch.object(loop, "run_phase", return_value=changed),
            patch("improve.ci.wait_for_ci", return_value=(True, "", 1.0)),
            patch.object(loop, "retry_ci_fixes", return_value=(True, 0, 0.0, 0.0)),
        ):
            loop.run_batch_iteration(1)

        mock_get_id.assert_called_once()


class TestShutdownElapsedHandling:
    def test_shutdown_prints_summary_with_zero_elapsed_when_loop_not_started(
        self, tmp_path, monkeypatch
    ):
        loop = _make_loop(tmp_path, monkeypatch)
        loop.loop_start = 0.0

        with (
            patch("improve.claude.terminate_active"),
            patch("builtins.print") as mock_print,
            pytest.raises(SystemExit),
        ):
            loop.shutdown(2, None)

        mock_print.assert_called_once()


class TestContinuousMode:
    def test_shows_iteration_without_max_in_continuous_mode(self, tmp_path, monkeypatch, capsys):
        loop = _make_loop(tmp_path, monkeypatch, continuous=True)

        with (
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(loop, "run_sequential_iteration", return_value=False),
        ):
            loop.run(1, 1000)

        output = capsys.readouterr().out
        assert "Iteration 1" in output
        assert "1/1000" not in output

    def test_shows_iteration_with_max_when_not_continuous(self, tmp_path, monkeypatch, capsys):
        loop = _make_loop(tmp_path, monkeypatch, continuous=False)

        with (
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(loop, "run_sequential_iteration", return_value=False),
        ):
            loop.run(1, 5)

        output = capsys.readouterr().out
        assert "Iteration 1/5" in output


class TestMaxCiRetries:
    def test_retry_loop_runs_exactly_5_times_not_4_or_6(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        claude_calls = []

        def track_claude(*args, **kwargs):
            claude_calls.append(1)
            return ("out", 1.0)

        with (
            patch("improve.claude.run_claude", side_effect=track_claude),
            patch("improve.git.has_changes", return_value=True),
            patch("improve.ci.get_latest_run_id", return_value=100),
            patch("improve.git.commit_and_push", return_value=True),
            patch("improve.ci.wait_for_ci", return_value=(False, "err", 1.0)),
        ):
            _, retries, _, _ = loop.retry_ci_fixes(False, "err", "Fix")

        assert retries == 5
        assert len(claude_calls) == 5


class TestRunPhaseCiTimeBoundary:
    def test_no_ci_breakdown_when_total_ci_is_zero(self, tmp_path, monkeypatch, caplog):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="f.py"),
            patch("improve.claude.run_claude", return_value=("SUMMARY: Fix", 2.0)),
            patch("improve.git.changed_files", return_value=["f.py"]),
            patch("improve.git.commit_and_push", return_value=True),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            result = loop.run_phase("simplify", 1, skip_ci=True)

        assert result.ci_seconds == 0.0
        assert "(claude:" not in caplog.text

    def test_ci_breakdown_shown_when_total_ci_is_positive(self, tmp_path, monkeypatch, caplog):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="f.py"),
            patch("improve.claude.run_claude", return_value=("SUMMARY: Fix", 2.0)),
            patch("improve.git.changed_files", return_value=["f.py"]),
            patch("improve.ci.get_latest_run_id", return_value=100),
            patch("improve.git.commit_and_push", return_value=True),
            patch("improve.ci.wait_for_ci", return_value=(True, "", 0.1)),
            patch.object(loop, "retry_ci_fixes", return_value=(True, 0, 0.0, 0.0)),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            result = loop.run_phase("simplify", 1, skip_ci=False)

        assert result.ci_seconds == 0.1
        assert "claude:" in caplog.text


class TestRunPhaseNoChangesResult:
    def test_no_changes_returns_ci_passed_true(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="file.py"),
            patch("improve.claude.run_claude", return_value=("nothing", 1.0)),
            patch("improve.git.changed_files", return_value=[]),
        ):
            result = loop.run_phase("simplify", 1, skip_ci=False)

        assert result.ci_passed is True
        assert result.changes_made is False
        assert result.ci_retries == 0

    def test_no_changes_has_duration(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="file.py"),
            patch("improve.claude.run_claude", return_value=("nothing", 1.5)),
            patch("improve.git.changed_files", return_value=[]),
        ):
            result = loop.run_phase("simplify", 1, skip_ci=False)

        assert result.duration_seconds > 0
        assert result.claude_seconds == 1.5


class TestRunPhaseDetails:
    def test_returns_files_in_result(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="file.py"),
            patch("improve.claude.run_claude", return_value=("SUMMARY: Fixed", 1.0)),
            patch("improve.git.changed_files", return_value=["x.py"]),
            patch("improve.git.commit_and_push", return_value=True),
        ):
            result = loop.run_phase("review", 1, skip_ci=True)

        assert result.files == ["x.py"]
        assert result.iteration == 1
        assert result.phase == "review"

    def test_includes_ci_time_breakdown_in_log_when_ci_runs(self, tmp_path, monkeypatch, caplog):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="f.py"),
            patch("improve.claude.run_claude", return_value=("SUMMARY: Fix", 2.0)),
            patch("improve.git.changed_files", return_value=["f.py"]),
            patch("improve.ci.get_latest_run_id", return_value=100),
            patch("improve.git.commit_and_push", return_value=True),
            patch("improve.ci.wait_for_ci", return_value=(True, "", 10.0)),
            patch.object(loop, "retry_ci_fixes", return_value=(True, 0, 0.0, 0.0)),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            loop.run_phase("simplify", 1, skip_ci=False)

        assert "claude:" in caplog.text
        assert "ci:" in caplog.text

    def test_does_not_show_ci_breakdown_when_no_ci_time(self, tmp_path, monkeypatch, caplog):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="f.py"),
            patch("improve.claude.run_claude", return_value=("SUMMARY: Fix", 2.0)),
            patch("improve.git.changed_files", return_value=["f.py"]),
            patch("improve.git.commit_and_push", return_value=True),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            loop.run_phase("simplify", 1, skip_ci=True)

        assert "ci:" not in caplog.text

    def test_accumulates_fix_claude_time(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="f.py"),
            patch("improve.claude.run_claude", return_value=("SUMMARY: Fix", 3.0)),
            patch("improve.git.changed_files", return_value=["f.py"]),
            patch("improve.ci.get_latest_run_id", return_value=100),
            patch("improve.git.commit_and_push", return_value=True),
            patch("improve.ci.wait_for_ci", return_value=(False, "err", 5.0)),
            patch.object(loop, "retry_ci_fixes", return_value=(True, 1, 2.0, 4.0)),
        ):
            result = loop.run_phase("simplify", 1, skip_ci=False)

        assert result.claude_seconds == 5.0
        assert result.ci_seconds == 9.0


class TestCheckConvergence:
    def test_returns_true_when_no_changes_in_any_result(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, phases=["simplify", "review"])
        results = [
            PhaseResult(1, "simplify", False, [], "No changes needed", True, 0),
            PhaseResult(1, "review", False, [], "No changes needed", True, 0),
        ]

        assert loop._check_convergence(results) is True

    def test_returns_false_when_changes_exist(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, phases=["simplify"])
        results = [PhaseResult(1, "simplify", True, ["a.py"], "Fixed", True, 0)]

        assert loop._check_convergence(results) is False

    def test_drops_converged_phases(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, phases=["simplify", "review"])
        results = [
            PhaseResult(1, "simplify", False, [], "No changes needed", True, 0),
            PhaseResult(1, "review", True, ["b.py"], "Fixed", True, 0),
        ]

        loop._check_convergence(results)

        assert "simplify" not in loop._active_phases
        assert "review" in loop._active_phases

    def test_returns_false_when_a_phase_crashed_even_if_no_changes(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, phases=["simplify", "review"])
        results = [
            PhaseResult.crashed(1, "simplify"),
            PhaseResult(1, "review", False, [], "No changes needed", True, 0),
        ]

        assert loop._check_convergence(results) is False

    def test_keeps_crashed_phase_active_for_retry(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, phases=["simplify", "review"])
        results = [
            PhaseResult.crashed(1, "simplify"),
            PhaseResult(1, "review", False, [], "No changes needed", True, 0),
        ]

        loop._check_convergence(results)

        assert loop._active_phases == ["simplify"]

    def test_logs_retry_message_when_phase_crashed(self, tmp_path, monkeypatch, caplog):
        loop = _make_loop(tmp_path, monkeypatch, phases=["simplify"])
        results = [PhaseResult.crashed(1, "simplify")]

        with caplog.at_level(logging.INFO, logger="improve"):
            loop._check_convergence(results)

        assert "Retrying crashed phase(s) next iteration" in caplog.text
        assert "simplify" in caplog.text
