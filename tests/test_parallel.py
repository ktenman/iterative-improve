from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest

from improve.parallel import (
    _collect_results,
    _create_worktrees,
    _merge_worktree_results,
    run_parallel_batch,
    run_phase_in_worktree,
)
from improve.state import PhaseResult
from tests.conftest import _test_config


class TestRunPhaseInWorktree:
    def test_returns_no_changes_when_claude_makes_none(self):
        with (
            patch("improve.parallel.claude.run_claude", return_value=("NO_CHANGES_NEEDED", 1.0)),
            patch("improve.parallel.git.changed_files", return_value=[]),
        ):
            result = run_phase_in_worktree(
                "simplify", 1, "/tmp/wt", "file.py", "None", _test_config()
            )

        assert result.changes_made is False
        assert result.summary == "No changes needed"

    def test_returns_changes_with_summary(self):
        with (
            patch("improve.parallel.claude.run_claude", return_value=("SUMMARY: Fixed stuff", 1.0)),
            patch("improve.parallel.git.changed_files", return_value=["file.py"]),
        ):
            result = run_phase_in_worktree(
                "simplify", 1, "/tmp/wt", "file.py", "None", _test_config()
            )

        assert result.changes_made is True
        assert result.summary == "Fixed stuff"

    def test_passes_cwd_and_quiet_to_run_claude(self):
        with (
            patch("improve.parallel.claude.run_claude", return_value=("", 1.0)) as mock_claude,
            patch("improve.parallel.git.changed_files", return_value=[]),
        ):
            run_phase_in_worktree("review", 1, "/tmp/wt", "f.py", "None", _test_config())

        _, kwargs = mock_claude.call_args
        assert kwargs["cwd"] == "/tmp/wt"
        assert kwargs["quiet"] is True


class TestCollectResults:
    def test_returns_crashed_result_when_future_raises(self):
        ok_future = MagicMock(spec=Future)
        ok_future.result.return_value = PhaseResult(
            1,
            "simplify",
            True,
            ["a.py"],
            "Fixed",
            True,
            0,
        )
        bad_future = MagicMock(spec=Future)
        bad_future.result.side_effect = RuntimeError("boom")

        results = _collect_results([ok_future, bad_future], ["simplify", "review"], 1)

        assert len(results) == 2
        assert results[0].changes_made is True
        assert results[1].changes_made is False
        assert results[1].summary == "Phase crashed"
        assert results[1].phase == "review"


class TestRunParallelBatch:
    def test_returns_false_when_no_changes_in_any_phase(self):
        no_changes = PhaseResult(1, "simplify", False, [], "No changes", True, 0)
        add_result = MagicMock()

        with (
            patch("improve.parallel.git.diff_vs_main", return_value="file.py"),
            patch("improve.parallel.git.create_worktree", return_value=True),
            patch("improve.parallel.git.remove_worktree"),
            patch("improve.parallel.run_phase_in_worktree", return_value=no_changes),
            patch("tempfile.mkdtemp", return_value="/tmp/improve-test"),
        ):
            result = run_parallel_batch(
                ["simplify", "review"],
                1,
                "feature",
                "None",
                True,
                add_result,
                MagicMock(),
                _test_config(),
            )

        assert result is False

    def test_returns_true_when_a_phase_crashed_so_loop_retries_next_iteration(self):
        results = [
            PhaseResult.crashed(1, "simplify"),
            PhaseResult(1, "review", False, [], "No changes needed", True, 0),
        ]

        with (
            patch("improve.parallel.git.diff_vs_main", return_value="file.py"),
            patch("improve.parallel.git.create_worktree", return_value=True),
            patch("improve.parallel.git.remove_worktree"),
            patch("improve.parallel.run_phase_in_worktree", side_effect=results),
            patch("tempfile.mkdtemp", return_value="/tmp/improve-test"),
        ):
            result = run_parallel_batch(
                ["simplify", "review"],
                1,
                "feature",
                "None",
                True,
                MagicMock(),
                MagicMock(),
                _test_config(),
            )

        assert result is True

    def test_applies_changes_and_commits(self):
        changed = PhaseResult(1, "simplify", True, ["a.py"], "Fixed stuff", True, 0)
        add_result = MagicMock()

        with (
            patch("improve.parallel.git.diff_vs_main", return_value="a.py"),
            patch("improve.parallel.git.create_worktree", return_value=True),
            patch("improve.parallel.git.remove_worktree"),
            patch("improve.parallel.git.apply_worktree_changes", return_value=["a.py"]),
            patch("improve.parallel.git.commit_and_push", return_value=True),
            patch("improve.parallel.run_phase_in_worktree", return_value=changed),
            patch("tempfile.mkdtemp", return_value="/tmp/improve-test"),
        ):
            result = run_parallel_batch(
                ["simplify"],
                1,
                "feature",
                "None",
                True,
                add_result,
                MagicMock(),
                _test_config(),
            )

        assert result is True

    def test_returns_false_when_worktree_creation_fails(self):
        with (
            patch("improve.parallel.git.diff_vs_main", return_value="file.py"),
            patch("improve.parallel.git.create_worktree", return_value=False),
            patch("improve.parallel.git.remove_worktree"),
            patch("tempfile.mkdtemp", return_value="/tmp/improve-test"),
        ):
            result = run_parallel_batch(
                ["simplify"],
                1,
                "feature",
                "None",
                True,
                MagicMock(),
                MagicMock(),
                _test_config(),
            )

        assert result is False

    def test_cleans_up_worktrees_on_completion(self):
        no_changes = PhaseResult(1, "simplify", False, [], "No changes", True, 0)

        with (
            patch("improve.parallel.git.diff_vs_main", return_value="file.py"),
            patch("improve.parallel.git.create_worktree", return_value=True),
            patch("improve.parallel.git.remove_worktree") as mock_remove,
            patch("improve.parallel.run_phase_in_worktree", return_value=no_changes),
            patch("tempfile.mkdtemp", return_value="/tmp/improve-test"),
        ):
            run_parallel_batch(
                ["simplify"],
                1,
                "feature",
                "None",
                True,
                MagicMock(),
                MagicMock(),
                _test_config(),
            )

        mock_remove.assert_called_once()

    def test_checks_ci_when_not_skipped(self):
        changed = PhaseResult(1, "simplify", True, ["a.py"], "Fixed", True, 0)
        retry = MagicMock(return_value=(True, 0, 0.0, 0.0))

        with (
            patch("improve.parallel.git.diff_vs_main", return_value="a.py"),
            patch("improve.parallel.ci.get_latest_run_id", return_value=100),
            patch("improve.parallel.git.create_worktree", return_value=True),
            patch("improve.parallel.git.remove_worktree"),
            patch("improve.parallel.git.apply_worktree_changes", return_value=["a.py"]),
            patch("improve.parallel.git.commit_and_push", return_value=True),
            patch("improve.parallel.ci.wait_for_ci", return_value=(True, "", 2.0)),
            patch("improve.parallel.run_phase_in_worktree", return_value=changed),
            patch("tempfile.mkdtemp", return_value="/tmp/improve-test"),
        ):
            result = run_parallel_batch(
                ["simplify"],
                1,
                "feature",
                "None",
                False,
                MagicMock(),
                retry,
                _test_config(),
            )

        assert result is True

    def test_handles_oserror_when_applying_worktree_changes(self):
        changed = PhaseResult(1, "simplify", True, ["a.py"], "Fixed", True, 0)
        add_result = MagicMock()

        with (
            patch("improve.parallel.git.diff_vs_main", return_value="a.py"),
            patch("improve.parallel.git.create_worktree", return_value=True),
            patch("improve.parallel.git.remove_worktree"),
            patch(
                "improve.parallel.git.apply_worktree_changes",
                side_effect=OSError("Permission denied"),
            ),
            patch("improve.parallel.run_phase_in_worktree", return_value=changed),
            patch("tempfile.mkdtemp", return_value="/tmp/improve-test"),
        ):
            result = run_parallel_batch(
                ["simplify"],
                1,
                "feature",
                "None",
                True,
                add_result,
                MagicMock(),
                _test_config(),
            )

        assert result is False
        added = add_result.call_args[0][0]
        assert added.changes_made is False

    def test_returns_false_when_push_fails(self):
        changed = PhaseResult(1, "simplify", True, ["a.py"], "Fixed", True, 0)

        with (
            patch("improve.parallel.git.diff_vs_main", return_value="a.py"),
            patch("improve.parallel.git.create_worktree", return_value=True),
            patch("improve.parallel.git.remove_worktree"),
            patch("improve.parallel.git.apply_worktree_changes", return_value=["a.py"]),
            patch("improve.parallel.git.commit_and_push", return_value=False),
            patch("improve.parallel.run_phase_in_worktree", return_value=changed),
            patch("tempfile.mkdtemp", return_value="/tmp/improve-test"),
        ):
            result = run_parallel_batch(
                ["simplify"],
                1,
                "feature",
                "None",
                True,
                MagicMock(),
                MagicMock(),
                _test_config(),
            )

        assert result is False

    def test_uses_generic_message_when_multiple_phases_changed(self):
        results = [
            PhaseResult(1, "simplify", True, ["a.py"], "Simplified", True, 0),
            PhaseResult(1, "review", True, ["b.py"], "Fixed", True, 0),
        ]

        with (
            patch("improve.parallel.git.diff_vs_main", return_value="a.py\nb.py"),
            patch("improve.parallel.git.create_worktree", return_value=True),
            patch("improve.parallel.git.remove_worktree"),
            patch("improve.parallel.git.apply_worktree_changes", side_effect=[["a.py"], ["b.py"]]),
            patch("improve.parallel.git.commit_and_push", return_value=True) as mock_push,
            patch("improve.parallel.run_phase_in_worktree", side_effect=results),
            patch("tempfile.mkdtemp", return_value="/tmp/improve-test"),
        ):
            run_parallel_batch(
                ["simplify", "review"],
                1,
                "feature",
                "None",
                True,
                MagicMock(),
                MagicMock(),
                _test_config(),
            )

        commit_message = mock_push.call_args[0][0]
        assert commit_message == "Improve code quality"

    def test_single_changed_phase_uses_phase_commit_message(self):
        changed = PhaseResult(1, "simplify", True, ["a.py"], "extract helper", True, 0)

        with (
            patch("improve.parallel.git.diff_vs_main", return_value="a.py"),
            patch("improve.parallel.git.create_worktree", return_value=True),
            patch("improve.parallel.git.remove_worktree"),
            patch("improve.parallel.git.apply_worktree_changes", return_value=["a.py"]),
            patch("improve.parallel.git.commit_and_push", return_value=True) as mock_push,
            patch("improve.parallel.run_phase_in_worktree", return_value=changed),
            patch("tempfile.mkdtemp", return_value="/tmp/improve-test"),
        ):
            run_parallel_batch(
                ["simplify"],
                1,
                "feature",
                "None",
                True,
                MagicMock(),
                MagicMock(),
                _test_config(),
            )

        commit_message = mock_push.call_args[0][0]
        assert commit_message.startswith("Extract")

    def test_overlap_detection_logs_warning(self, caplog):
        import logging

        results = [
            PhaseResult(1, "simplify", True, ["shared.py"], "Simplified", True, 0),
            PhaseResult(1, "review", True, ["shared.py"], "Fixed", True, 0),
        ]

        with (
            patch("improve.parallel.git.diff_vs_main", return_value="shared.py"),
            patch("improve.parallel.git.create_worktree", return_value=True),
            patch("improve.parallel.git.remove_worktree"),
            patch(
                "improve.parallel.git.apply_worktree_changes",
                side_effect=[["shared.py"], ["shared.py"]],
            ),
            patch("improve.parallel.git.commit_and_push", return_value=True),
            patch("improve.parallel.run_phase_in_worktree", side_effect=results),
            patch("tempfile.mkdtemp", return_value="/tmp/improve-test"),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            run_parallel_batch(
                ["simplify", "review"],
                1,
                "feature",
                "None",
                True,
                MagicMock(),
                MagicMock(),
                _test_config(),
            )

        assert "overwrites" in caplog.text
        assert "shared.py" in caplog.text

    def test_skip_ci_true_bypasses_ci_check(self):
        changed = PhaseResult(1, "simplify", True, ["a.py"], "Fixed", True, 0)
        retry = MagicMock()

        with (
            patch("improve.parallel.git.diff_vs_main", return_value="a.py"),
            patch("improve.parallel.git.create_worktree", return_value=True),
            patch("improve.parallel.git.remove_worktree"),
            patch("improve.parallel.git.apply_worktree_changes", return_value=["a.py"]),
            patch("improve.parallel.git.commit_and_push", return_value=True),
            patch("improve.parallel.run_phase_in_worktree", return_value=changed),
            patch("tempfile.mkdtemp", return_value="/tmp/improve-test"),
        ):
            result = run_parallel_batch(
                ["simplify"],
                1,
                "feature",
                "None",
                True,
                MagicMock(),
                retry,
                _test_config(),
            )

        assert result is True
        retry.assert_not_called()

    def test_oserror_marks_result_as_no_changes_with_empty_files(self):
        changed = PhaseResult(1, "simplify", True, ["a.py", "b.py"], "Fixed", True, 0)
        add_result = MagicMock()

        with (
            patch("improve.parallel.git.diff_vs_main", return_value="a.py"),
            patch("improve.parallel.git.create_worktree", return_value=True),
            patch("improve.parallel.git.remove_worktree"),
            patch(
                "improve.parallel.git.apply_worktree_changes",
                side_effect=OSError("Permission denied"),
            ),
            patch("improve.parallel.run_phase_in_worktree", return_value=changed),
            patch("tempfile.mkdtemp", return_value="/tmp/improve-test"),
        ):
            run_parallel_batch(
                ["simplify"],
                1,
                "feature",
                "None",
                True,
                add_result,
                MagicMock(),
                _test_config(),
            )

        added = add_result.call_args[0][0]
        assert added.changes_made is False
        assert added.files == []

    def test_returns_false_when_ci_fails(self):
        changed = PhaseResult(1, "simplify", True, ["a.py"], "Fixed", True, 0)
        retry = MagicMock(return_value=(False, 1, 1.0, 2.0))

        with (
            patch("improve.parallel.git.diff_vs_main", return_value="a.py"),
            patch("improve.parallel.ci.get_latest_run_id", return_value=100),
            patch("improve.parallel.git.create_worktree", return_value=True),
            patch("improve.parallel.git.remove_worktree"),
            patch("improve.parallel.git.apply_worktree_changes", return_value=["a.py"]),
            patch("improve.parallel.git.commit_and_push", return_value=True),
            patch("improve.parallel.ci.wait_for_ci", return_value=(False, "error", 2.0)),
            patch("improve.parallel.run_phase_in_worktree", return_value=changed),
            patch("tempfile.mkdtemp", return_value="/tmp/improve-test"),
        ):
            result = run_parallel_batch(
                ["simplify"],
                1,
                "feature",
                "None",
                False,
                MagicMock(),
                retry,
                _test_config(),
            )

        assert result is False

    def test_cleans_up_base_dir_when_create_raises(self, tmp_path):
        base_dir = tmp_path / "improve-tmp"
        base_dir.mkdir()

        with (
            patch("improve.parallel.git.diff_vs_main", return_value="f.py"),
            patch(
                "improve.parallel.git.create_worktree", side_effect=RuntimeError("worktree boom")
            ),
            patch("improve.parallel.git.remove_worktree"),
            patch("tempfile.mkdtemp", return_value=str(base_dir)),
            pytest.raises(RuntimeError),
        ):
            run_parallel_batch(
                ["simplify"],
                1,
                "feature",
                "None",
                True,
                MagicMock(),
                MagicMock(),
                _test_config(),
            )

        assert not base_dir.exists()


class TestCreateWorktrees:
    def test_returns_worktree_paths_on_success(self):
        with patch("improve.parallel.git.create_worktree", return_value=True):
            result = _create_worktrees(["simplify", "review"], "/tmp/base")

        assert result == {
            "simplify": "/tmp/base/simplify",
            "review": "/tmp/base/review",
        }

    def test_returns_none_when_creation_fails(self):
        with patch("improve.parallel.git.create_worktree", return_value=False):
            result = _create_worktrees(["simplify"], "/tmp/base")

        assert result is None

    def test_returns_empty_dict_for_empty_phases(self):
        result = _create_worktrees([], "/tmp/base")

        assert result == {}

    def test_cleans_up_partial_worktrees_on_failure(self):
        outcomes = iter([True, False])
        with (
            patch("improve.parallel.git.create_worktree", side_effect=lambda _: next(outcomes)),
            patch("improve.parallel.git.remove_worktree") as mock_remove,
        ):
            result = _create_worktrees(["simplify", "review"], "/tmp/base")

        assert result is None
        mock_remove.assert_called_once_with("/tmp/base/simplify")

    def test_cleans_up_partial_worktrees_when_create_raises(self):
        outcomes = iter([True, RuntimeError("boom")])

        def fake_create(_path):
            value = next(outcomes)
            if isinstance(value, Exception):
                raise value
            return value

        with (
            patch("improve.parallel.git.create_worktree", side_effect=fake_create),
            patch("improve.parallel.git.remove_worktree") as mock_remove,
            pytest.raises(RuntimeError),
        ):
            _create_worktrees(["simplify", "review"], "/tmp/base")

        mock_remove.assert_called_once_with("/tmp/base/simplify")


class TestMergeWorktreeResults:
    def test_applies_changes_from_worktrees(self):
        results = [PhaseResult(1, "simplify", True, ["a.py"], "Fixed", True, 0)]
        worktrees = {"simplify": "/tmp/wt/simplify"}

        with patch("improve.parallel.git.apply_worktree_changes", return_value=["a.py"]):
            _merge_worktree_results(results, worktrees)

        assert results[0].files == ["a.py"]

    def test_marks_result_as_no_changes_on_oserror(self):
        results = [PhaseResult(1, "simplify", True, ["a.py"], "Fixed", True, 0)]
        worktrees = {"simplify": "/tmp/wt/simplify"}

        with patch(
            "improve.parallel.git.apply_worktree_changes",
            side_effect=OSError("denied"),
        ):
            _merge_worktree_results(results, worktrees)

        assert results[0].changes_made is False
        assert results[0].files == []

    def test_skips_results_with_no_changes(self):
        results = [PhaseResult(1, "simplify", False, [], "No changes", True, 0)]
        worktrees = {"simplify": "/tmp/wt/simplify"}

        with (
            patch("improve.parallel.git.apply_worktree_changes") as mock_apply,
            patch("improve.parallel.git.repo_root") as mock_root,
        ):
            _merge_worktree_results(results, worktrees)

        mock_apply.assert_not_called()
        mock_root.assert_not_called()

    def test_resolves_repo_root_only_once_for_multiple_results(self):
        results = [
            PhaseResult(1, "simplify", True, ["a.py"], "Fixed", True, 0),
            PhaseResult(1, "review", True, ["b.py"], "Fixed", True, 0),
            PhaseResult(1, "security", True, ["c.py"], "Fixed", True, 0),
        ]
        worktrees = {"simplify": "/tmp/s", "review": "/tmp/r", "security": "/tmp/sec"}

        with (
            patch("improve.parallel.git.repo_root", return_value="/repo") as mock_root,
            patch(
                "improve.parallel.git.apply_worktree_changes",
                side_effect=[["a.py"], ["b.py"], ["c.py"]],
            ) as mock_apply,
        ):
            _merge_worktree_results(results, worktrees)

        assert mock_root.call_count == 1
        for call in mock_apply.call_args_list:
            assert call.args[1] == "/repo"
