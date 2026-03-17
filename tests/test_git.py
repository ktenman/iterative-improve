import logging
from unittest.mock import patch

import pytest

from improve import git
from tests import _cp


class TestHeadSha:
    def test_returns_current_head_sha(self):
        with patch("improve.git.run", return_value=_cp(stdout="abc123def\n")) as mock_run:
            assert git.head_sha() == "abc123def"

        mock_run.assert_called_once_with(["git", "rev-parse", "HEAD"])

    def test_logs_warning_when_rev_parse_fails(self, caplog):
        import logging

        with (
            patch("improve.git.run", return_value=_cp(returncode=1, stdout="")),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            result = git.head_sha()

        assert result == ""
        assert "Failed to determine HEAD sha" in caplog.text


class TestRevertTo:
    def test_returns_true_on_successful_reset_and_push(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [_cp(), _cp()]
            assert git.revert_to("abc123", "feature") is True

        mock_run.assert_any_call(["git", "reset", "--hard", "abc123"])
        mock_run.assert_any_call(["git", "push", "--force-with-lease", "origin", "feature"])

    def test_returns_false_when_reset_fails(self):
        with patch("improve.git.run", return_value=_cp(returncode=1, stderr="error")):
            assert git.revert_to("abc123", "feature") is False

    def test_returns_false_when_force_push_fails(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [_cp(), _cp(returncode=1, stderr="rejected")]
            assert git.revert_to("abc123", "feature") is False

    def test_logs_truncated_sha_on_success(self, caplog):
        with (
            patch("improve.git.run") as mock_run,
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            mock_run.side_effect = [_cp(), _cp()]
            git.revert_to("abc12345xyz", "feature")

        assert "abc12345" in caplog.text

    def test_logs_reset_failed_warning(self, caplog):
        with (
            patch("improve.git.run", return_value=_cp(returncode=1, stderr="oops")),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            git.revert_to("abc123", "feature")

        assert "Reset failed" in caplog.text

    def test_logs_push_failed_warning(self, caplog):
        with (
            patch("improve.git.run") as mock_run,
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            mock_run.side_effect = [_cp(), _cp(returncode=1, stderr="rejected")]
            git.revert_to("abc123", "feature")

        assert "Force push failed" in caplog.text


class TestDiscardChanges:
    def test_runs_git_checkout(self):
        with patch("improve.git.run", return_value=_cp()) as mock_run:
            git.discard_changes()

        mock_run.assert_called_once_with(["git", "checkout", "--", "."])

    def test_logs_warning_when_checkout_fails(self, caplog):
        import logging

        with (
            patch("improve.git.run", return_value=_cp(returncode=1, stderr="error")),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            git.discard_changes()

        assert "Failed to discard changes" in caplog.text


class TestBranch:
    def test_returns_current_branch_name(self):
        with patch("improve.git.run", return_value=_cp(stdout="feature-x\n")) as mock_run:
            assert git.branch() == "feature-x"

        mock_run.assert_called_once_with(["git", "branch", "--show-current"])


class TestDetectPlatform:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://github.com/user/repo.git", "github"),
            ("git@github.com:user/repo.git", "github"),
            ("https://gitlab.com/user/repo.git", "gitlab"),
            ("git@gitlab.com:user/repo.git", "gitlab"),
            ("https://gitlab.mycompany.com/repo.git", "gitlab"),
            ("https://bitbucket.org/user/repo", "github"),
        ],
    )
    def test_detects_platform_from_remote_url(self, url, expected):
        with patch("improve.git.run", return_value=_cp(stdout=f"{url}\n")):
            assert git.detect_platform() == expected

    def test_defaults_to_github_when_remote_fails(self):
        with patch("improve.git.run", return_value=_cp(returncode=1)):
            assert git.detect_platform() == "github"


class TestHasChanges:
    def test_returns_true_when_porcelain_output_exists(self):
        with patch("improve.git.run", return_value=_cp(stdout=" M file.py\n")) as mock_run:
            assert git.has_changes() is True

        mock_run.assert_called_once_with(["git", "status", "--porcelain", "--no-renames"])

    def test_returns_false_when_porcelain_output_is_empty(self):
        with patch("improve.git.run", return_value=_cp(stdout="")):
            assert git.has_changes() is False


class TestChangedFiles:
    def test_extracts_filenames_from_porcelain_output(self):
        result = _cp(stdout=" M src/a.py\n?? src/b.py\n")
        with patch("improve.git.run", return_value=result) as mock_run:
            files = git.changed_files()

        assert files == ["src/a.py", "src/b.py"]
        mock_run.assert_called_once_with(["git", "status", "--porcelain", "--no-renames"])

    def test_returns_empty_list_when_no_changes(self):
        with patch("improve.git.run", return_value=_cp(stdout="")):
            assert git.changed_files() == []


class TestDiffVsMain:
    def test_returns_diff_output_stripped(self):
        with patch("improve.git.run", return_value=_cp(stdout="src/a.py\nsrc/b.py\n")) as mock_run:
            assert git.diff_vs_main() == "src/a.py\nsrc/b.py"

        mock_run.assert_called_once_with(["git", "diff", "--name-only", "main...HEAD"])


class TestHasConflicts:
    def test_returns_true_when_conflict_files_exist(self):
        with patch("improve.git.run", return_value=_cp(stdout="file.py\n")):
            assert git.has_conflicts() is True

    def test_returns_false_when_no_conflicts(self):
        with patch("improve.git.run", return_value=_cp(stdout="")):
            assert git.has_conflicts() is False


class TestConflictFiles:
    def test_returns_list_of_conflicted_files(self):
        with patch("improve.git.run", return_value=_cp(stdout="a.py\nb.py\n")) as mock_run:
            assert git.conflict_files() == ["a.py", "b.py"]

        mock_run.assert_called_once_with(["git", "diff", "--name-only", "--diff-filter=U"])


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

    def test_logs_warning_when_git_add_fails(self, caplog):
        import logging

        with (
            patch("improve.git.run") as mock_run,
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            mock_run.side_effect = [
                _cp(stdout=" M app.py\n"),
                _cp(returncode=1, stderr="add failed"),
            ]
            git.stage_tracked_changes()

        assert "Failed to stage files" in caplog.text


class TestCommitAndPush:
    def test_returns_true_on_successful_commit_and_push(self):
        with (
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.run") as mock_run,
        ):
            mock_run.side_effect = [_cp(), _cp()]
            assert git.commit_and_push("Fix bug", "feature") is True

        mock_run.assert_any_call(["git", "commit", "-m", "Fix bug"])
        mock_run.assert_any_call(["git", "push", "-u", "origin", "feature"])

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

    def test_logs_pushed_message_on_success(self, caplog):
        with (
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.run") as mock_run,
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            mock_run.side_effect = [_cp(), _cp()]
            git.commit_and_push("Fix bug", "feature")

        assert "Pushed:" in caplog.text

    def test_logs_commit_failed_warning(self, caplog):
        with (
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.run", return_value=_cp(returncode=1, stderr="err")),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            git.commit_and_push("Fix bug", "feature")

        assert "Commit failed" in caplog.text


class TestSyncWithMain:
    def test_returns_true_when_already_up_to_date(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [_cp(), _cp(stdout="0\n")]
            assert git.sync_with_main("feature") is True

        mock_run.assert_any_call(["git", "fetch", "origin", "main"])
        mock_run.assert_any_call(["git", "rev-list", "--count", "HEAD..origin/main"])

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

        mock_run.assert_any_call(["git", "merge", "origin/main", "--no-edit"])
        mock_run.assert_any_call(["git", "push", "-u", "origin", "feature"])

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

        mock_run.assert_any_call(["git", "merge", "--abort"])

    def test_returns_true_when_push_fails_after_clean_merge(self, caplog):
        import logging

        with (
            patch("improve.git.run") as mock_run,
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            mock_run.side_effect = [
                _cp(),  # fetch
                _cp(stdout="3\n"),  # rev-list
                _cp(),  # merge
                _cp(returncode=1, stderr="rejected"),  # push fails
            ]
            result = git.sync_with_main("feature")

        assert result is True
        assert "Push after merge failed" in caplog.text

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


class TestResolveExistingConflicts:
    def test_returns_true_when_no_conflicts_exist(self):
        with patch("improve.git.conflict_files", return_value=[]):
            assert git.resolve_existing_conflicts() is True

    def test_resolves_conflicts_with_claude_and_commits(self):
        with (
            patch("improve.git.conflict_files", return_value=["ci.py"]),
            patch("improve.git.has_conflicts", return_value=False),
            patch("improve.git.run_claude", return_value=("SUMMARY: Fixed", 1.0)),
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.extract_summary", return_value="Fixed"),
            patch("improve.git.run", return_value=_cp()),
        ):
            assert git.resolve_existing_conflicts() is True

    def test_aborts_merge_when_claude_fails_to_resolve(self):
        with (
            patch("improve.git.conflict_files", return_value=["ci.py"]),
            patch("improve.git.has_conflicts", side_effect=[True, False]),
            patch("improve.git.run_claude", return_value=("", 1.0)),
            patch("improve.git.run", return_value=_cp()),
        ):
            assert git.resolve_existing_conflicts() is True

    def test_returns_false_when_abort_also_fails(self):
        with (
            patch("improve.git.conflict_files", return_value=["ci.py"]),
            patch("improve.git.has_conflicts", side_effect=[True, True]),
            patch("improve.git.run_claude", return_value=("", 1.0)),
            patch("improve.git.run", return_value=_cp()),
        ):
            assert git.resolve_existing_conflicts() is False


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

    def test_logs_warning_on_failure(self, caplog):
        import logging

        with (
            patch("improve.git.run", return_value=_cp(returncode=1, stderr="busy")),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            git.remove_worktree("/tmp/wt")

        assert "Failed to remove worktree" in caplog.text


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


class TestApplyWorktreeChangesEmpty:
    def test_returns_empty_when_repo_root_cannot_be_determined(self):
        with (
            patch("improve.git.changed_files", return_value=["file.py"]),
            patch("improve.git.run", return_value=_cp(stdout="")),
        ):
            result = git.apply_worktree_changes("/tmp/wt")

        assert result == []


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

        assert files == []
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

        mock_run.assert_any_call(["git", "merge-base", "HEAD", "main"])
        mock_run.assert_any_call(["git", "rev-list", "--count", "abc123..HEAD"])
        mock_run.assert_any_call(["git", "reset", "--soft", "abc123"])
        mock_run.assert_any_call(["git", "commit", "-m", "Squashed"])
        mock_run.assert_any_call(["git", "push", "--force-with-lease", "origin", "feature"])

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

    def test_returns_true_when_zero_commits(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [
                _cp(stdout="abc123\n"),
                _cp(stdout="0\n"),
            ]
            assert git.squash_branch("feature", "Squashed") is True

    def test_logs_nothing_to_squash_for_one_commit(self, caplog):
        with (
            patch("improve.git.run") as mock_run,
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            mock_run.side_effect = [
                _cp(stdout="abc123\n"),
                _cp(stdout="1\n"),
            ]
            git.squash_branch("feature", "Squashed")

        assert "Nothing to squash" in caplog.text

    def test_logs_success_message_after_squash(self, caplog):
        with (
            patch("improve.git.run") as mock_run,
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            mock_run.side_effect = [
                _cp(stdout="abc123\n"),
                _cp(stdout="3\n"),
                _cp(),
                _cp(),
                _cp(),
            ]
            git.squash_branch("feature", "Squashed")

        assert "Squashed and force-pushed" in caplog.text
