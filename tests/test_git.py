from unittest.mock import patch

from improve import git
from tests import _cp


class TestBranch:
    def test_returns_current_branch_name(self):
        with patch("improve.git.run", return_value=_cp(stdout="feature-x\n")):
            assert git.branch() == "feature-x"


class TestHasChanges:
    def test_returns_true_when_porcelain_output_exists(self):
        with patch("improve.git.run", return_value=_cp(stdout=" M file.py\n")):
            assert git.has_changes() is True

    def test_returns_false_when_porcelain_output_is_empty(self):
        with patch("improve.git.run", return_value=_cp(stdout="")):
            assert git.has_changes() is False


class TestChangedFiles:
    def test_extracts_filenames_from_porcelain_output(self):
        with patch("improve.git.run", return_value=_cp(stdout=" M src/a.py\n?? src/b.py\n")):
            files = git.changed_files()
            assert "src/a.py" in files
            assert "src/b.py" in files

    def test_returns_empty_list_when_no_changes(self):
        with patch("improve.git.run", return_value=_cp(stdout="")):
            assert git.changed_files() == []


class TestDiffVsMain:
    def test_returns_diff_output_stripped(self):
        with patch("improve.git.run", return_value=_cp(stdout="src/a.py\nsrc/b.py\n")):
            assert git.diff_vs_main() == "src/a.py\nsrc/b.py"


class TestHasConflicts:
    def test_returns_true_when_conflict_files_exist(self):
        with patch("improve.git.run", return_value=_cp(stdout="file.py\n")):
            assert git.has_conflicts() is True

    def test_returns_false_when_no_conflicts(self):
        with patch("improve.git.run", return_value=_cp(stdout="")):
            assert git.has_conflicts() is False


class TestConflictFiles:
    def test_returns_list_of_conflicted_files(self):
        with patch("improve.git.run", return_value=_cp(stdout="a.py\nb.py\n")):
            assert git.conflict_files() == ["a.py", "b.py"]


class TestStageTrackedChanges:
    def test_stages_all_changed_files(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [
                _cp(stdout=" M app.py\n?? new.py\n"),
                _cp(stdout=""),
            ]
            git.stage_tracked_changes()
            add_args = mock_run.call_args_list[1][0][0]
            assert add_args[:3] == ["git", "add", "--"]
            assert set(add_args[3:]) == {"app.py", "new.py"}

    def test_does_not_call_git_add_when_no_changes(self):
        with patch("improve.git.run", return_value=_cp(stdout="")) as mock_run:
            git.stage_tracked_changes()
            assert mock_run.call_count == 1


class TestCommitAndPush:
    def test_returns_true_on_successful_commit_and_push(self):
        with (
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.run") as mock_run,
        ):
            mock_run.side_effect = [_cp(), _cp()]
            assert git.commit_and_push("Fix bug", "feature") is True

    def test_returns_false_when_commit_fails(self):
        with (
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.run", return_value=_cp(returncode=1, stderr="err")),
        ):
            assert git.commit_and_push("Fix bug", "feature") is False

    def test_returns_false_when_push_fails(self):
        with (
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.run") as mock_run,
        ):
            mock_run.side_effect = [_cp(), _cp(returncode=1, stderr="rejected")]
            assert git.commit_and_push("Fix bug", "feature") is False


class TestSyncWithMain:
    def test_returns_true_when_already_up_to_date(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [_cp(), _cp(stdout="0\n")]
            assert git.sync_with_main("feature") is True

    def test_returns_true_when_fetch_fails(self):
        with patch("improve.git.run", return_value=_cp(returncode=1, stderr="network error")):
            assert git.sync_with_main("feature") is True

    def test_returns_true_after_clean_merge(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [
                _cp(),
                _cp(stdout="3\n"),
                _cp(),
                _cp(),
            ]
            assert git.sync_with_main("feature") is True

    def test_returns_false_when_merge_fails_without_conflicts(self):
        with (
            patch("improve.git.has_conflicts", return_value=False),
            patch("improve.git.run") as mock_run,
        ):
            mock_run.side_effect = [
                _cp(),  # fetch
                _cp(stdout="1\n"),  # rev-list
                _cp(returncode=1),  # merge
                _cp(),  # merge --abort
            ]
            assert git.sync_with_main("feature") is False

    def test_delegates_to_resolve_conflicts_when_merge_has_conflicts(self):
        with (
            patch("improve.git.has_conflicts", return_value=True),
            patch("improve.git._resolve_conflicts", return_value=True) as mock_resolve,
            patch("improve.git.run") as mock_run,
        ):
            mock_run.side_effect = [
                _cp(),  # fetch
                _cp(stdout="1\n"),  # rev-list
                _cp(returncode=1),  # merge fails
            ]

            result = git.sync_with_main("feature")

        assert result is True
        mock_resolve.assert_called_once_with("feature")


class TestResolveConflicts:
    def test_resolves_conflicts_and_pushes(self):
        with (
            patch("improve.git.conflict_files", return_value=["file.py"]),
            patch("improve.git.run_claude", return_value=("SUMMARY: Resolved", 1.0)),
            patch("improve.git.has_conflicts", return_value=False),
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.extract_summary", return_value="Resolved"),
            patch("improve.git.run") as mock_run,
        ):
            mock_run.side_effect = [_cp(), _cp()]  # commit, push

            result = git._resolve_conflicts("feature")

        assert result is True

    def test_aborts_when_conflicts_remain_after_claude(self):
        with (
            patch("improve.git.conflict_files", return_value=["file.py"]),
            patch("improve.git.run_claude", return_value=("", 1.0)),
            patch("improve.git.has_conflicts", return_value=True),
            patch("improve.git.run", return_value=_cp()),
        ):
            result = git._resolve_conflicts("feature")

        assert result is False

    def test_uses_fallback_commit_when_no_edit_fails(self):
        with (
            patch("improve.git.conflict_files", return_value=["file.py"]),
            patch("improve.git.run_claude", return_value=("SUMMARY: Fixed", 1.0)),
            patch("improve.git.has_conflicts", return_value=False),
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.extract_summary", return_value="Fixed"),
            patch("improve.git.run") as mock_run,
        ):
            mock_run.side_effect = [
                _cp(returncode=1),  # commit --no-edit fails
                _cp(),  # commit -m fallback
                _cp(),  # push
            ]

            result = git._resolve_conflicts("feature")

        assert result is True

    def test_aborts_when_both_commit_attempts_fail(self):
        with (
            patch("improve.git.conflict_files", return_value=["file.py"]),
            patch("improve.git.run_claude", return_value=("SUMMARY: Fixed", 1.0)),
            patch("improve.git.has_conflicts", return_value=False),
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.extract_summary", return_value="Fixed"),
            patch("improve.git.run") as mock_run,
        ):
            mock_run.side_effect = [
                _cp(returncode=1),  # commit --no-edit fails
                _cp(returncode=1),  # commit -m fails
                _cp(),  # merge --abort
            ]

            result = git._resolve_conflicts("feature")

        assert result is False

    def test_returns_false_when_push_fails_after_resolution(self):
        with (
            patch("improve.git.conflict_files", return_value=["file.py"]),
            patch("improve.git.run_claude", return_value=("SUMMARY: Fixed", 1.0)),
            patch("improve.git.has_conflicts", return_value=False),
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.extract_summary", return_value="Fixed"),
            patch("improve.git.run") as mock_run,
        ):
            mock_run.side_effect = [
                _cp(),  # commit
                _cp(returncode=1, stderr="rejected"),  # push fails
            ]

            result = git._resolve_conflicts("feature")

        assert result is False


class TestCreateWorktree:
    def test_returns_true_on_success(self):
        with patch("improve.git.run", return_value=_cp()):
            assert git.create_worktree("/tmp/test-wt") is True

    def test_returns_false_on_failure(self):
        with patch("improve.git.run", return_value=_cp(returncode=1, stderr="error")):
            assert git.create_worktree("/tmp/test-wt") is False

    def test_passes_detach_flag(self):
        with patch("improve.git.run", return_value=_cp()) as mock_run:
            git.create_worktree("/tmp/wt")

        mock_run.assert_called_once_with(["git", "worktree", "add", "--detach", "/tmp/wt"])


class TestRemoveWorktree:
    def test_calls_git_worktree_remove_with_force(self):
        with patch("improve.git.run") as mock_run:
            git.remove_worktree("/tmp/wt")

        mock_run.assert_called_once_with(["git", "worktree", "remove", "--force", "/tmp/wt"])


class TestChangedFilesWithCwd:
    def test_returns_files_from_porcelain_output(self):
        with patch("improve.git.run", return_value=_cp(stdout=" M src/a.py\n?? src/b.py\n")):
            files = git.changed_files("/tmp/wt")

        assert "src/a.py" in files
        assert "src/b.py" in files

    def test_passes_cwd_via_git_c_flag(self):
        with patch("improve.git.run", return_value=_cp(stdout="")) as mock_run:
            git.changed_files("/tmp/wt")

        mock_run.assert_called_once_with(
            ["git", "-C", "/tmp/wt", "status", "--porcelain", "--no-renames"]
        )

    def test_returns_empty_list_when_no_changes(self):
        with patch("improve.git.run", return_value=_cp(stdout="")):
            assert git.changed_files("/tmp/wt") == []


class TestApplyWorktreeChanges:
    def test_copies_changed_files_to_main_tree(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        main = tmp_path / "main"
        main.mkdir()
        (worktree / "file.py").write_text("new content")

        with (
            patch("improve.git.changed_files", return_value=["file.py"]),
            patch("improve.git.run", return_value=_cp(stdout=str(main))),
        ):
            files = git.apply_worktree_changes(str(worktree))

        assert files == ["file.py"]
        assert (main / "file.py").read_text() == "new content"

    def test_returns_empty_list_when_no_changes(self):
        with patch("improve.git.changed_files", return_value=[]):
            assert git.apply_worktree_changes("/tmp/wt") == []

    def test_deletes_files_removed_in_worktree(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        main = tmp_path / "main"
        main.mkdir()
        (main / "old.py").write_text("delete me")

        with (
            patch("improve.git.changed_files", return_value=["old.py"]),
            patch("improve.git.run", return_value=_cp(stdout=str(main))),
        ):
            git.apply_worktree_changes(str(worktree))

        assert not (main / "old.py").exists()

    def test_creates_parent_directories(self, tmp_path):
        worktree = tmp_path / "worktree"
        (worktree / "src" / "new").mkdir(parents=True)
        (worktree / "src" / "new" / "file.py").write_text("content")
        main = tmp_path / "main"
        main.mkdir()

        with (
            patch("improve.git.changed_files", return_value=["src/new/file.py"]),
            patch("improve.git.run", return_value=_cp(stdout=str(main))),
        ):
            git.apply_worktree_changes(str(worktree))

        assert (main / "src" / "new" / "file.py").read_text() == "content"

    def test_skips_path_traversal_attempts(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        main = tmp_path / "main"
        main.mkdir()

        with (
            patch("improve.git.changed_files", return_value=["../../etc/passwd"]),
            patch("improve.git.run", return_value=_cp(stdout=str(main))),
        ):
            files = git.apply_worktree_changes(str(worktree))

        assert files == ["../../etc/passwd"]
        assert not (main / "../../etc/passwd").exists()


class TestSquashBranch:
    def test_squashes_multiple_commits_into_one(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [
                _cp(stdout="abc123\n"),  # merge-base
                _cp(stdout="5\n"),  # rev-list --count
                _cp(),  # reset --soft
                _cp(),  # commit
                _cp(),  # push --force-with-lease
            ]
            assert git.squash_branch("feature", "Squashed") is True

    def test_returns_true_when_only_one_commit(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [
                _cp(stdout="abc123\n"),  # merge-base
                _cp(stdout="1\n"),  # rev-list --count
            ]
            assert git.squash_branch("feature", "Squashed") is True

    def test_returns_false_when_merge_base_not_found(self):
        with patch("improve.git.run", return_value=_cp(stdout="")):
            assert git.squash_branch("feature", "Squashed") is False

    def test_returns_false_when_force_push_fails(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [
                _cp(stdout="abc123\n"),  # merge-base
                _cp(stdout="3\n"),  # rev-list --count
                _cp(),  # reset --soft
                _cp(),  # commit
                _cp(returncode=1, stderr="rejected"),  # push fails
            ]
            assert git.squash_branch("feature", "Squashed") is False

    def test_returns_false_when_reset_fails(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [
                _cp(stdout="abc123\n"),  # merge-base
                _cp(stdout="3\n"),  # rev-list --count
                _cp(returncode=1, stderr="error"),  # reset --soft fails
            ]

            result = git.squash_branch("feature", "Squashed")

        assert result is False

    def test_returns_false_when_squash_commit_fails(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [
                _cp(stdout="abc123\n"),  # merge-base
                _cp(stdout="3\n"),  # rev-list --count
                _cp(),  # reset --soft
                _cp(returncode=1, stderr="error"),  # commit fails
            ]

            result = git.squash_branch("feature", "Squashed")

        assert result is False
