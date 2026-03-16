import logging
from unittest.mock import patch

import pytest

from improve.loop import MAX_CI_RETRIES, IterationLoop
from improve.prompt import AVAILABLE_PHASES
from improve.state import LoopState, PhaseResult


def _make_loop(
    tmp_path,
    monkeypatch,
    skip_ci=False,
    batch=False,
    phases=None,
    squash=False,
    parallel=False,
    revert_on_fail=False,
    continuous=False,
):
    monkeypatch.setattr("improve.state.STATE_DIR", tmp_path)
    monkeypatch.setattr("improve.state.STATE_FILE", tmp_path / "state.json")
    state = LoopState(branch="feature", started_at="2025-01-01T00:00:00")
    if phases is None:
        phases = list(AVAILABLE_PHASES)
    return IterationLoop(
        state=state,
        skip_ci=skip_ci,
        batch=batch,
        phases=phases,
        squash=squash,
        parallel=parallel,
        revert_on_fail=revert_on_fail,
        continuous=continuous,
    )


class TestShutdown:
    def test_saves_state_and_exits_with_code_130(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        loop.loop_start = 1.0

        with (
            patch("improve.claude.terminate_active"),
            patch.object(loop, "print_summary"),
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
            patch.object(loop, "print_summary"),
            pytest.raises(SystemExit) as exc_info,
        ):
            loop.shutdown(2, None)

        assert exc_info.value.code == 130

    def test_calls_terminate_active_before_exiting(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)

        with (
            patch("improve.claude.terminate_active") as mock_terminate,
            patch.object(loop, "print_summary"),
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

    def test_stops_when_no_changes_produced(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.claude.run_claude", return_value=("", 1.0)),
            patch("improve.git.has_changes", return_value=False),
        ):
            passed, retries, _, _ = loop.retry_ci_fixes(False, "err", "Fix")

        assert passed is False
        assert retries == 1

    def test_stops_when_push_fails(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.claude.run_claude", return_value=("", 1.0)),
            patch("improve.git.has_changes", return_value=True),
            patch("improve.git.commit_and_push", return_value=False),
        ):
            passed, retries, _, _ = loop.retry_ci_fixes(False, "err", "Fix")

        assert passed is False
        assert retries == 1

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

    def test_logs_warning_when_all_retries_exhausted(self, tmp_path, monkeypatch, caplog):
        loop = _make_loop(tmp_path, monkeypatch)
        with (
            patch("improve.claude.run_claude", return_value=("out", 1.0)),
            patch("improve.git.has_changes", return_value=True),
            patch("improve.ci.get_latest_run_id", return_value=100),
            patch("improve.git.commit_and_push", return_value=True),
            patch("improve.ci.wait_for_ci", return_value=(False, "still failing", 2.0)),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            passed, retries, _, _ = loop.retry_ci_fixes(False, "err", "Fix")

        assert passed is False
        assert retries == MAX_CI_RETRIES
        assert "All 5 attempts exhausted" in caplog.text

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


class TestPrintSummary:
    def test_prints_formatted_results(self, tmp_path, monkeypatch, capsys):
        loop = _make_loop(tmp_path, monkeypatch)
        loop.state.add(
            PhaseResult(1, "simplify", True, ["a.py"], "Extracted helper", True, 0, 10.0, 8.0, 2.0)
        )

        loop.print_summary(15.0)

        output = capsys.readouterr().out
        assert "RESULTS" in output
        assert "Extracted helper" in output

    def test_prints_empty_summary_when_no_phases_ran(self, tmp_path, monkeypatch, capsys):
        loop = _make_loop(tmp_path, monkeypatch)

        loop.print_summary(0.0)

        output = capsys.readouterr().out
        assert "Phases run:     0" in output


class TestRunBatchIteration:
    def test_returns_false_when_no_changes_in_any_phase(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, batch=True)
        no_changes = PhaseResult(1, "simplify", False, [], "No changes", True, 0)

        with patch.object(loop, "run_phase", return_value=no_changes):
            result = loop.run_batch_iteration(1)

        assert result is False

    def test_returns_false_when_push_fails(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, batch=True)
        push_failed = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", False, 0)

        with patch.object(loop, "run_phase", return_value=push_failed):
            result = loop.run_batch_iteration(1)

        assert result is False

    def test_checks_ci_after_all_phases_complete(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, batch=True, skip_ci=False)
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
        loop = _make_loop(tmp_path, monkeypatch, batch=True, skip_ci=True)
        changed = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", True, 0)
        with (
            patch.object(loop, "run_phase", return_value=changed),
            patch("improve.ci.wait_for_ci") as mock_wait,
        ):
            result = loop.run_batch_iteration(1)

        assert result is True
        mock_wait.assert_not_called()


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

    def test_excludes_reverted_results_from_squash_message(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, squash=True)
        loop.state.add(PhaseResult(1, "simplify", True, ["a.py"], "Extracted helper", True, 0))
        loop.state.add(
            PhaseResult(1, "review", True, ["b.py"], "Reverted change", False, 1, reverted=True)
        )

        with (
            patch("improve.git.squash_branch", return_value=True) as mock_squash,
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(loop, "run_sequential_iteration", return_value=False),
        ):
            loop.run(1, 1)

        message = mock_squash.call_args[0][1]
        assert "Extracted helper" in message
        assert "Reverted change" not in message

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


class TestRun:
    def test_stops_on_merge_conflict(self, tmp_path, monkeypatch, capsys):
        loop = _make_loop(tmp_path, monkeypatch)

        with patch("improve.git.sync_with_main", return_value=False):
            loop.run(1, 3)

        output = capsys.readouterr().out
        assert "RESULTS" in output

    def test_dispatches_to_sequential_when_not_batch(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch)

        with (
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(loop, "run_sequential_iteration", return_value=False) as mock_seq,
        ):
            loop.run(1, 1)

        mock_seq.assert_called_once_with(1)

    def test_dispatches_to_batch_when_batch_mode(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, batch=True)

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
        loop = _make_loop(tmp_path, monkeypatch, parallel=True)

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


class TestRevertOnFail:
    def test_sequential_reverts_and_continues_on_ci_failure(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, revert_on_fail=True, phases=["simplify", "review"])
        results = [
            PhaseResult(1, "simplify", True, ["a.py"], "Stuff", False, 1),
            PhaseResult(1, "review", True, ["b.py"], "More", True, 0),
        ]

        with (
            patch("improve.git.head_sha", return_value="abc123"),
            patch("improve.git.revert_to", return_value=True) as mock_revert,
            patch.object(loop, "run_phase", side_effect=results),
        ):
            result = loop.run_sequential_iteration(1)

        mock_revert.assert_called_once_with("abc123", "feature")
        assert result is True
        reverted = [r for r in loop.state.results if r.get("reverted")]
        assert len(reverted) == 1
        assert reverted[0]["phase"] == "simplify"

    def test_sequential_stops_when_revert_fails(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, revert_on_fail=True, phases=["simplify", "review"])
        ci_failed = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", False, 1)

        with (
            patch("improve.git.head_sha", return_value="abc123"),
            patch("improve.git.revert_to", return_value=False),
            patch.object(loop, "run_phase", return_value=ci_failed),
        ):
            result = loop.run_sequential_iteration(1)

        assert result is False
        assert len(loop.state.results) == 1

    def test_sequential_stops_on_ci_failure_without_revert_flag(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, revert_on_fail=False)
        ci_failed = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", False, 0)

        with (
            patch("improve.git.head_sha", return_value="abc123"),
            patch.object(loop, "run_phase", return_value=ci_failed),
        ):
            result = loop.run_sequential_iteration(1)

        assert result is False

    def test_batch_reverts_all_on_ci_failure(self, tmp_path, monkeypatch):
        loop = _make_loop(
            tmp_path,
            monkeypatch,
            batch=True,
            skip_ci=False,
            revert_on_fail=True,
            phases=["simplify"],
        )
        changed = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", True, 0)

        with (
            patch("improve.git.head_sha", return_value="abc123"),
            patch("improve.ci.get_latest_run_id", return_value=100),
            patch.object(loop, "run_phase", return_value=changed),
            patch("improve.ci.wait_for_ci", return_value=(False, "error", 2.0)),
            patch.object(loop, "retry_ci_fixes", return_value=(False, 1, 1.0, 2.0)),
            patch("improve.git.revert_to", return_value=True) as mock_revert,
        ):
            result = loop.run_batch_iteration(1)

        assert result is True
        mock_revert.assert_called_once_with("abc123", "feature")

    def test_batch_stops_when_revert_fails(self, tmp_path, monkeypatch):
        loop = _make_loop(
            tmp_path,
            monkeypatch,
            batch=True,
            skip_ci=False,
            revert_on_fail=True,
            phases=["simplify"],
        )
        changed = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", True, 0)

        with (
            patch("improve.git.head_sha", return_value="abc123"),
            patch("improve.ci.get_latest_run_id", return_value=100),
            patch.object(loop, "run_phase", return_value=changed),
            patch("improve.ci.wait_for_ci", return_value=(False, "error", 2.0)),
            patch.object(loop, "retry_ci_fixes", return_value=(False, 1, 1.0, 2.0)),
            patch("improve.git.revert_to", return_value=False),
        ):
            result = loop.run_batch_iteration(1)

        assert result is False

    def test_parallel_marks_results_as_reverted_on_ci_failure(self, tmp_path, monkeypatch):
        loop = _make_loop(
            tmp_path,
            monkeypatch,
            parallel=True,
            skip_ci=False,
            revert_on_fail=True,
            phases=["simplify"],
        )
        changed = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", True, 0)

        with (
            patch("improve.git.head_sha", return_value="abc123"),
            patch(
                "improve.loop.run_parallel_batch",
                side_effect=lambda **kwargs: (kwargs["add_result"](changed), True)[1],
            ),
        ):
            result = loop.run_parallel_batch_iteration(1)

        assert result is True
        reverted = [r for r in loop.state.results if r.get("reverted")]
        assert len(reverted) == 1
        assert reverted[0]["phase"] == "simplify"


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


class TestCrashRecovery:
    def test_sequential_continues_after_phase_crash(self, tmp_path, monkeypatch):
        loop = _make_loop(tmp_path, monkeypatch, phases=["simplify", "review"], skip_ci=True)
        ok_result = PhaseResult(1, "review", True, ["b.py"], "Fixed", True, 0)

        with (
            patch("improve.git.head_sha", return_value="abc"),
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
            batch=True,
            skip_ci=True,
            phases=["simplify", "review"],
        )
        ok_result = PhaseResult(1, "review", True, ["b.py"], "Fixed", True, 0)

        with (
            patch("improve.git.head_sha", return_value="abc"),
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


class TestContinuousMode:
    def test_shows_iteration_without_max_in_continuous_mode(self, tmp_path, monkeypatch, capsys):
        loop = _make_loop(tmp_path, monkeypatch, continuous=True)

        with (
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(loop, "run_sequential_iteration", return_value=False),
        ):
            loop.run(1, 1000)

        output = capsys.readouterr().out
        assert "Iteration 1 ---" in output
        assert "1/1000" not in output

    def test_shows_iteration_with_max_when_not_continuous(self, tmp_path, monkeypatch, capsys):
        loop = _make_loop(tmp_path, monkeypatch, continuous=False)

        with (
            patch("improve.git.sync_with_main", return_value=True),
            patch.object(loop, "run_sequential_iteration", return_value=False),
        ):
            loop.run(1, 5)

        output = capsys.readouterr().out
        assert "Iteration 1/5 ---" in output


class TestPrintSummaryReverted:
    def test_shows_reverted_status_for_reverted_phases(self, tmp_path, monkeypatch, capsys):
        loop = _make_loop(tmp_path, monkeypatch)
        result = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", False, 1, reverted=True)
        loop.state.add(result)

        loop.print_summary(10.0)

        output = capsys.readouterr().out
        assert "CI:REVT" in output
        assert "Reverted:       1" in output
