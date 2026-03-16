from concurrent.futures import Future
from unittest.mock import MagicMock, patch

from improve.parallel import _collect_results, run_parallel_batch, run_phase_in_worktree
from improve.state import PhaseResult


class TestRunPhaseInWorktree:
    def test_returns_no_changes_when_claude_makes_none(self):
        with (
            patch("improve.parallel.claude.run_claude", return_value=("NO_CHANGES_NEEDED", 1.0)),
            patch("improve.parallel.git.changed_files", return_value=[]),
        ):
            result = run_phase_in_worktree("simplify", 1, "/tmp/wt", "file.py", "None")

        assert result.changes_made is False
        assert result.summary == "No changes needed"

    def test_returns_changes_with_summary(self):
        with (
            patch("improve.parallel.claude.run_claude", return_value=("SUMMARY: Fixed stuff", 1.0)),
            patch("improve.parallel.git.changed_files", return_value=["file.py"]),
        ):
            result = run_phase_in_worktree("simplify", 1, "/tmp/wt", "file.py", "None")

        assert result.changes_made is True
        assert result.summary == "Fixed stuff"

    def test_passes_cwd_and_quiet_to_run_claude(self):
        with (
            patch("improve.parallel.claude.run_claude", return_value=("", 1.0)) as mock_claude,
            patch("improve.parallel.git.changed_files", return_value=[]),
        ):
            run_phase_in_worktree("review", 1, "/tmp/wt", "f.py", "None")

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
            )

        assert result is False

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
            )

        assert result is True

    def test_reverts_on_ci_failure_when_revert_sha_provided(self):
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
            patch("improve.parallel.git.revert_to", return_value=True) as mock_revert,
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
                revert_sha="abc123",
            )

        assert result is True
        mock_revert.assert_called_once_with("abc123", "feature")

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
            )

        assert result is False
