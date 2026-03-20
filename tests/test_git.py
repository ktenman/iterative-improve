import logging
from unittest.mock import patch

import pytest

from improve import git
from improve.platform import Platform
from tests.conftest import _cp


class TestBranch:
    def test_returns_current_branch_name(self):
        with patch("improve.git.run", return_value=_cp(stdout="feature-x\n")) as mock_run:
            assert git.branch() == "feature-x"

        mock_run.assert_called_once_with(["git", "branch", "--show-current"])


class TestDetectPlatform:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://github.com/user/repo.git", Platform.GITHUB),
            ("git@github.com:user/repo.git", Platform.GITHUB),
            ("https://gitlab.com/user/repo.git", Platform.GITLAB),
            ("git@gitlab.com:user/repo.git", Platform.GITLAB),
            ("https://gitlab.mycompany.com/repo.git", Platform.GITLAB),
            ("https://bitbucket.org/user/repo", Platform.GITHUB),
        ],
    )
    def test_detects_platform_from_remote_url(self, url, expected):
        with patch("improve.git.run", return_value=_cp(stdout=f"{url}\n")):
            assert git.detect_platform() == expected

    def test_defaults_to_github_when_remote_fails(self):
        with patch("improve.git.run", return_value=_cp(returncode=1)):
            assert git.detect_platform() == Platform.GITHUB


class TestHasChanges:
    def test_returns_true_when_porcelain_output_exists(self):
        with patch("improve.git.run", return_value=_cp(stdout=" M file.py\n")) as mock_run:
            assert git.has_changes() is True

        mock_run.assert_called_once_with(["git", "status", "--porcelain", "--no-renames"])

    def test_returns_false_when_porcelain_output_is_empty(self):
        with patch("improve.git.run", return_value=_cp(stdout="")):
            assert git.has_changes() is False

    def test_returns_false_when_only_improve_loop_files_changed(self):
        with patch("improve.git.run", return_value=_cp(stdout=" M .improve-loop/state.json\n")):
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

    def test_excludes_improve_loop_directory_files(self):
        result = _cp(stdout=" M src/a.py\n M .improve-loop/state.json\n?? .improve-loop/run.log\n")
        with patch("improve.git.run", return_value=result):
            assert git.changed_files() == ["src/a.py"]

    def test_returns_empty_when_only_improve_loop_files_changed(self):
        result = _cp(stdout=" M .improve-loop/state.json\n")
        with patch("improve.git.run", return_value=result):
            assert git.changed_files() == []

    @pytest.mark.parametrize(
        "porcelain,expected",
        [
            (" M file.py\n", "file.py"),
            ("A  new.py\n", "new.py"),
            ("?? untracked.py\n", "untracked.py"),
        ],
    )
    def test_strips_status_prefix_from_porcelain_output(self, porcelain, expected):
        with patch("improve.git.run", return_value=_cp(stdout=porcelain)):
            files = git.changed_files()

        assert files == [expected]


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
    @pytest.mark.parametrize(
        "stdout,expected",
        [
            ("a.py\nb.py\n", ["a.py", "b.py"]),
            ("\n", []),
            ("only.py\n", ["only.py"]),
        ],
    )
    def test_returns_conflict_files_from_git_output(self, stdout, expected):
        with patch("improve.git.run", return_value=_cp(stdout=stdout)):
            assert git.conflict_files() == expected


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

    def test_does_not_push_when_commit_fails(self):
        with (
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.run") as mock_run,
        ):
            mock_run.return_value = _cp(returncode=1, stderr="err")
            git.commit_and_push("msg", "feature")

        assert mock_run.call_count == 1


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


class TestChangedFilesWithCwd:
    def test_returns_files_from_porcelain_output(self):
        with patch("improve.git.run", return_value=_cp(stdout=" M src/a.py\n?? src/b.py\n")):
            files = git.changed_files("/tmp/wt")

        assert "src/a.py" in files
        assert "src/b.py" in files

    def test_returns_empty_list_when_no_changes(self):
        with patch("improve.git.run", return_value=_cp(stdout="")):
            assert git.changed_files("/tmp/wt") == []

    def test_omits_cwd_flag_when_cwd_is_none(self):
        with patch("improve.git.run", return_value=_cp(stdout="")) as mock_run:
            git.changed_files()

        cmd = mock_run.call_args[0][0]
        assert "-C" not in cmd

    def test_includes_cwd_flag_when_cwd_is_provided(self):
        with patch("improve.git.run", return_value=_cp(stdout="")) as mock_run:
            git.changed_files("/some/path")

        cmd = mock_run.call_args[0][0]
        assert "-C" in cmd
        assert "/some/path" in cmd


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

        mock_run.assert_any_call(["git", "merge-base", "HEAD", "origin/main"])
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

    @pytest.mark.parametrize(
        "side_effects",
        [
            pytest.param(
                [_cp(stdout="abc123\n"), _cp(stdout="3\n"), _cp(returncode=1, stderr="error")],
                id="reset_fails",
            ),
            pytest.param(
                [
                    _cp(stdout="abc123\n"),
                    _cp(stdout="3\n"),
                    _cp(),
                    _cp(returncode=1, stderr="error"),
                ],
                id="commit_fails",
            ),
            pytest.param(
                [
                    _cp(stdout="abc123\n"),
                    _cp(stdout="3\n"),
                    _cp(),
                    _cp(),
                    _cp(returncode=1, stderr="rejected"),
                ],
                id="push_fails",
            ),
        ],
    )
    def test_returns_false_when_any_step_fails(self, side_effects):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = side_effects
            assert git.squash_branch("feature", "Squashed") is False

    def test_returns_true_when_zero_commits(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [
                _cp(stdout="abc123\n"),
                _cp(stdout="0\n"),
            ]
            assert git.squash_branch("feature", "Squashed") is True


class TestDiscardChangesSuccess:
    def test_does_not_log_on_successful_checkout(self, caplog):
        with (
            patch("improve.git.run", return_value=_cp(returncode=0)),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            git.discard_changes()

        assert "Failed to discard" not in caplog.text


class TestDetectPlatformCaseSensitivity:
    def test_url_check_is_case_insensitive(self):
        with patch("improve.git.run", return_value=_cp(stdout="https://GITLAB.com/repo\n")):
            assert git.detect_platform() == Platform.GITLAB


class TestSyncWithMainNonZeroBehind:
    def test_count_one_triggers_merge(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [
                _cp(),
                _cp(stdout="1\n"),
                _cp(),
                _cp(),
            ]
            assert git.sync_with_main("feature") is True

        mock_run.assert_any_call(["git", "merge", "origin/main", "--no-edit"])

    def test_count_zero_does_not_merge(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [_cp(), _cp(stdout="0\n")]
            assert git.sync_with_main("feature") is True

        assert mock_run.call_count == 2


class TestApplyWorktreeSkipsDeleteWhenBothMissing:
    def test_skips_file_when_neither_src_nor_dst_exists(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        main = tmp_path / "main"
        main.mkdir()

        with (
            patch("improve.git.changed_files", return_value=["missing.py"]),
            patch("improve.git.run", return_value=_cp(stdout=str(main))),
        ):
            files = git.apply_worktree_changes(str(worktree))

        assert files == ["missing.py"]
        assert not (main / "missing.py").exists()


class TestChangedFilesSlicePrecision:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (" M x.py\n", ["x.py"]),
            ("?? y.py\n", ["y.py"]),
            ("A  z.py\n", ["z.py"]),
            ("MM a.py\n", ["a.py"]),
        ],
    )
    def test_removes_exactly_three_char_prefix(self, raw, expected):
        with patch("improve.git.run", return_value=_cp(stdout=raw)):
            assert git.changed_files() == expected

    def test_single_char_filename_preserved(self):
        with patch("improve.git.run", return_value=_cp(stdout=" M x\n")):
            assert git.changed_files() == ["x"]


class TestSquashBranchBoundary:
    def test_returns_true_for_zero_commits_without_squashing(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [_cp(stdout="abc\n"), _cp(stdout="0\n")]
            result = git.squash_branch("feat", "msg")

        assert result is True
        assert mock_run.call_count == 2

    def test_returns_true_for_one_commit_without_squashing(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [_cp(stdout="abc\n"), _cp(stdout="1\n")]
            result = git.squash_branch("feat", "msg")

        assert result is True
        assert mock_run.call_count == 2

    def test_squashes_two_commits(self):
        with patch("improve.git.run") as mock_run:
            mock_run.side_effect = [
                _cp(stdout="abc\n"),
                _cp(stdout="2\n"),
                _cp(),
                _cp(),
                _cp(),
            ]
            result = git.squash_branch("feat", "msg")

        assert result is True
        assert mock_run.call_count == 5


class TestCommitResolutionTruncation:
    def test_truncates_summary_to_exactly_40_chars(self):
        summary_40 = "A" * 40
        summary_60 = "A" * 60
        with (
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.extract_summary", return_value=summary_60),
            patch("improve.git.run") as mock_run,
        ):
            mock_run.side_effect = [_cp(returncode=1), _cp()]
            git._commit_resolution("output")

        fallback_msg = mock_run.call_args_list[1][0][0][3]
        summary_part = fallback_msg.replace("Resolve merge conflicts: ", "")
        assert summary_part == summary_40

    def test_summary_shorter_than_40_not_truncated(self):
        short = "Fix merge"
        with (
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.extract_summary", return_value=short),
            patch("improve.git.run") as mock_run,
        ):
            mock_run.side_effect = [_cp(returncode=1), _cp()]
            git._commit_resolution("output")

        fallback_msg = mock_run.call_args_list[1][0][0][3]
        assert short in fallback_msg


class TestCommitResolution:
    def test_returns_true_when_no_edit_commit_succeeds(self):
        with (
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.run", return_value=_cp()),
        ):
            assert git._commit_resolution("output") is True

    def test_uses_fallback_commit_when_no_edit_fails(self):
        with (
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.extract_summary", return_value="Fixed conflicts"),
            patch("improve.git.run") as mock_run,
        ):
            mock_run.side_effect = [
                _cp(returncode=1),
                _cp(),
            ]
            result = git._commit_resolution("output")

        assert result is True
        fallback_cmd = mock_run.call_args_list[1][0][0]
        assert fallback_cmd[0:3] == ["git", "commit", "-m"]
        assert "Fixed conflicts" in fallback_cmd[3]

    def test_truncates_summary_to_40_chars_in_fallback(self):
        long_summary = "A" * 60
        with (
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.extract_summary", return_value=long_summary),
            patch("improve.git.run") as mock_run,
        ):
            mock_run.side_effect = [_cp(returncode=1), _cp()]
            git._commit_resolution("output")

        fallback_msg = mock_run.call_args_list[1][0][0][3]
        summary_part = fallback_msg.replace("Resolve merge conflicts: ", "")
        assert len(summary_part) <= 40

    def test_truncates_at_word_boundary_when_space_found_after_position_15(self):
        long_summary = "Resolved authentication conflicts in middleware layer code"
        with (
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.extract_summary", return_value=long_summary),
            patch("improve.git.run") as mock_run,
        ):
            mock_run.side_effect = [_cp(returncode=1), _cp()]
            git._commit_resolution("output")

        fallback_msg = mock_run.call_args_list[1][0][0][3]
        summary_part = fallback_msg.replace("Resolve merge conflicts: ", "")
        assert len(summary_part) <= 40
        assert not summary_part.endswith(" ")

    def test_returns_false_when_both_commits_fail(self):
        with (
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.extract_summary", return_value="x"),
            patch("improve.git.run") as mock_run,
        ):
            mock_run.side_effect = [_cp(returncode=1), _cp(returncode=1)]
            assert git._commit_resolution("output") is False


class TestResolveConflictsEdgeCases:
    def test_limits_conflict_file_display_to_5(self, caplog):
        files = [f"f{i}.py" for i in range(10)]
        with (
            patch("improve.git.conflict_files", return_value=files),
            patch("improve.git.run_claude", return_value=("", 1.0)),
            patch("improve.git.has_conflicts", return_value=True),
            patch("improve.git.run", return_value=_cp()),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            git._resolve_conflicts("feature")

        assert "f4.py" in caplog.text
        assert "f5.py" not in caplog.text

    def test_aborts_merge_when_conflicts_remain(self):
        with (
            patch("improve.git.conflict_files", return_value=["a.py"]),
            patch("improve.git.run_claude", return_value=("", 1.0)),
            patch("improve.git.has_conflicts", return_value=True),
            patch("improve.git.run", return_value=_cp()) as mock_run,
        ):
            git._resolve_conflicts("feature")

        mock_run.assert_any_call(["git", "merge", "--abort"])

    def test_aborts_merge_when_commit_fails(self):
        with (
            patch("improve.git.conflict_files", return_value=["a.py"]),
            patch("improve.git.run_claude", return_value=("", 1.0)),
            patch("improve.git.has_conflicts", return_value=False),
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.extract_summary", return_value="x"),
            patch("improve.git.run") as mock_run,
        ):
            mock_run.side_effect = [_cp(returncode=1), _cp(returncode=1), _cp()]
            git._resolve_conflicts("feature")

        mock_run.assert_any_call(["git", "merge", "--abort"])

    def test_returns_false_and_aborts_merge_when_claude_raises_runtime_error(self):
        with (
            patch("improve.git.conflict_files", return_value=["a.py"]),
            patch("improve.git.run_claude", side_effect=RuntimeError("Claude crashed")),
            patch("improve.git.run", return_value=_cp()) as mock_run,
        ):
            result = git._resolve_conflicts("feature")

        assert result is False
        mock_run.assert_any_call(["git", "merge", "--abort"])


class TestResolveExistingConflictsEdgeCases:
    def test_returns_false_when_commit_resolution_fails(self):
        with (
            patch("improve.git.conflict_files", return_value=["a.py"]),
            patch("improve.git.has_conflicts", return_value=False),
            patch("improve.git.run_claude", return_value=("", 1.0)),
            patch("improve.git._commit_resolution", return_value=False),
            patch("improve.git.run", return_value=_cp()),
        ):
            assert git.resolve_existing_conflicts() is False

    def test_returns_false_and_aborts_merge_when_claude_raises_runtime_error(self):
        with (
            patch("improve.git.conflict_files", return_value=["a.py"]),
            patch("improve.git.run_claude", side_effect=RuntimeError("Claude crashed")),
            patch("improve.git.run", return_value=_cp()) as mock_run,
        ):
            result = git.resolve_existing_conflicts()

        assert result is False
        mock_run.assert_any_call(["git", "merge", "--abort"])

    def test_calls_merge_abort_when_commit_fails(self):
        with (
            patch("improve.git.conflict_files", return_value=["a.py"]),
            patch("improve.git.has_conflicts", return_value=False),
            patch("improve.git.run_claude", return_value=("", 1.0)),
            patch("improve.git._commit_resolution", return_value=False),
            patch("improve.git.run", return_value=_cp()) as mock_run,
        ):
            git.resolve_existing_conflicts()

        mock_run.assert_any_call(["git", "merge", "--abort"])


class TestResolveAndCommit:
    def test_returns_true_on_successful_resolution_and_commit(self):
        with (
            patch("improve.git.run_claude", return_value=("SUMMARY: Fixed", 1.0)),
            patch("improve.git.has_conflicts", return_value=False),
            patch("improve.git.stage_tracked_changes"),
            patch("improve.git.run", return_value=_cp()),
        ):
            assert git._resolve_and_commit(["file.py"], "test") is True

    def test_returns_false_when_claude_fails(self):
        with (
            patch("improve.git.run_claude", side_effect=RuntimeError("boom")),
            patch("improve.git.run", return_value=_cp()),
        ):
            assert git._resolve_and_commit(["file.py"], "test") is False

    def test_aborts_merge_when_conflicts_remain(self):
        with (
            patch("improve.git.run_claude", return_value=("", 1.0)),
            patch("improve.git.has_conflicts", return_value=True),
            patch("improve.git.run", return_value=_cp()) as mock_run,
        ):
            assert git._resolve_and_commit(["file.py"], "test") is False

        mock_run.assert_any_call(["git", "merge", "--abort"])

    def test_aborts_merge_when_commit_fails(self):
        with (
            patch("improve.git.run_claude", return_value=("", 1.0)),
            patch("improve.git.has_conflicts", return_value=False),
            patch("improve.git._commit_resolution", return_value=False),
            patch("improve.git.run", return_value=_cp()) as mock_run,
        ):
            assert git._resolve_and_commit(["file.py"], "test") is False

        mock_run.assert_any_call(["git", "merge", "--abort"])


class TestAbortMergeGracefully:
    def test_returns_true_when_abort_succeeds(self):
        with (
            patch("improve.git.has_conflicts", return_value=False),
            patch("improve.git.run", return_value=_cp()),
        ):
            assert git._abort_merge_gracefully() is True

    def test_returns_false_when_conflicts_persist_after_abort(self):
        with (
            patch("improve.git.has_conflicts", return_value=True),
            patch("improve.git.run", return_value=_cp()),
        ):
            assert git._abort_merge_gracefully() is False
