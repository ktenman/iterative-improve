import itertools
import logging
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from improve.codex import CodexReply
from improve.council import COUNCIL, CouncilIteration, IterationContext, run_iteration
from improve.state import CRASHED_SUMMARY, CIFixResult, PhaseResult
from tests.conftest import _cp, _state, _test_config

FINDING = {
    "phase": "review",
    "file": "app.py",
    "symbol": "load",
    "line": 3,
    "severity": "high",
    "confidence": 90,
    "title": "Crash on empty input",
    "detail": "It indexes [0]",
    "suggested_fix": "Return early",
}
EMPTY = {"findings": {"findings": []}}


def _loop(tmp_path, monkeypatch, skip_ci=True) -> IterationContext:
    return SimpleNamespace(
        state=_state(tmp_path, monkeypatch),
        skip_ci=skip_ci,
        config=_test_config(),
        unsafe_to_squash=False,
        retry_ci_fixes=MagicMock(return_value=CIFixResult(True, 0, 0.0, 0.0)),
    )


def _replies(name, decision="fix", pick="mine"):
    position = {
        "id": "F1",
        "decision": decision,
        "approach": f"{name} approach",
        "reason": f"{name} reason",
    }
    return {
        "findings": {"findings": [FINDING]},
        "positions": {"positions": [position]},
        "picks": {"picks": [{"id": "F1", "pick": pick, "reason": "r"}]},
    }


def _repo_with_subdirectory(root):
    root.mkdir()
    (root / "sub").mkdir()
    (root / "src.py").write_text("committed\n")
    for args in (
        ["init", "-q", "."],
        ["add", "-A"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root


def _key(schema):
    return next(iter(schema["properties"]))


@contextmanager
def _agents(
    claude=None,
    codex=None,
    fixed_files=("app.py",),
    stray=(),
    tracked=(),
    root="/repo",
    diff="app.py\nlib.py",
    heads=None,
):
    claude = claude or _replies("Claude")
    codex = codex or _replies("Codex", pick="theirs")
    fixed = []

    def ask_claude(prompt, schema, session, resume, config):
        return claude[_key(schema)], 2.0

    def run_codex(prompt, schema, config, thread=""):
        return CodexReply(codex[_key(schema)], "thread-1", 3.0)

    def run_claude(prompt, config=None):
        fixed.append(prompt)
        return "Done.\nSUMMARY: Guard empty input", 5.0

    def changed_since(baseline):
        return list(fixed_files) if fixed else list(stray)

    def run_git(cmd, timeout=120):
        return _cp(stdout="\0".join(tracked)) if cmd[3] == "ls-files" else _cp()

    with (
        patch("improve.git.diff_vs_main", return_value=diff),
        patch("improve.git.repo_root", return_value=root),
        patch("improve.git.changed_files", return_value=[]),
        patch("improve.git.changed_files_since", side_effect=changed_since),
        patch("improve.git.commit_and_push", return_value=True) as push,
        patch("improve.claude.ask_claude", side_effect=ask_claude) as ask,
        patch("improve.codex.run_codex", side_effect=run_codex) as codex_call,
        patch("improve.claude.run_claude", side_effect=run_claude) as fix,
        patch("improve.ci.get_latest_run_id", return_value=41) as latest,
        patch("improve.ci.wait_for_ci", return_value=(True, "", 7.0)) as wait,
        patch("improve.process.run", side_effect=run_git) as git_call,
        patch("improve.council._head", side_effect=heads or itertools.repeat("sha-1")),
    ):
        yield SimpleNamespace(
            git=git_call,
            push=push,
            ask=ask,
            codex=codex_call,
            fix=fix,
            latest=latest,
            wait=wait,
        )


class TestConvergence:
    def test_reviews_again_without_debating_when_no_findings_are_left(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch)

        with _agents(claude=EMPTY, codex=EMPTY) as agents:
            keep_going = run_iteration(loop, 1, ["review"])

        assert keep_going is True
        result = loop.state.results[-1]
        assert (result["phase"], result["summary"], result["changes_made"]) == (
            "council",
            "No findings left",
            False,
        )
        assert (result["ci_passed"], result["ci_retries"], result["files"]) == (True, 0, [])
        assert (agents.ask.call_count, agents.codex.call_count) == (1, 1)
        agents.fix.assert_not_called()

    def test_logs_the_review_and_that_the_next_pass_reviews_again(
        self, tmp_path, monkeypatch, caplog
    ):
        loop = _loop(tmp_path, monkeypatch)

        with _agents(claude=EMPTY, codex=EMPTY), caplog.at_level(logging.INFO, logger="improve"):
            run_iteration(loop, 1, ["review"])

        assert caplog.messages == [
            "council] Claude and Codex are reviewing 2 file(s)...",
            "council] Claude reported 0, Codex 0; 0 left after merging",
            "loop] No findings left, reviewing again next iteration",
        ]

    def test_reviews_again_without_asking_anyone_when_nothing_changed_vs_main(
        self, tmp_path, monkeypatch
    ):
        loop = _loop(tmp_path, monkeypatch)

        with _agents(diff="") as agents:
            keep_going = run_iteration(loop, 1, ["review"])

        assert keep_going is True
        result = loop.state.results[-1]
        assert (result["phase"], result["summary"], result["changes_made"]) == (
            "council",
            "No findings left",
            False,
        )
        assert (agents.ask.call_count, agents.codex.call_count) == (0, 0)
        agents.fix.assert_not_called()

    def test_logs_nothing_to_review_and_that_the_next_pass_reviews_again_when_nothing_changed(
        self, tmp_path, monkeypatch, caplog
    ):
        loop = _loop(tmp_path, monkeypatch)

        with _agents(diff=""), caplog.at_level(logging.INFO, logger="improve"):
            run_iteration(loop, 1, ["review"])

        assert caplog.messages == [
            "council] No files changed vs main, nothing to review",
            "loop] No findings left, reviewing again next iteration",
        ]

    def test_reviews_again_when_both_reviewers_skip_every_finding(
        self, tmp_path, monkeypatch, caplog
    ):
        loop = _loop(tmp_path, monkeypatch)
        claude = _replies("Claude", decision="skip")
        codex = _replies("Codex", decision="skip")

        with (
            _agents(claude=claude, codex=codex) as agents,
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            keep_going = run_iteration(loop, 1, ["review"])

        assert keep_going is True
        assert loop.state.results[-1]["summary"] == "No fixes agreed"
        assert [
            (e["outcome"], e["phase"], e["severity"], e["reason"]) for e in loop.state.ledger
        ] == [("skipped", "review", "high", "Claude reason")]
        assert "council] Skipped: F1" in caplog.messages
        assert "loop] No fixes agreed, reviewing again next iteration" in caplog.messages
        agents.fix.assert_not_called()

    def test_retries_instead_of_converging_when_no_finding_gets_an_answer(
        self, tmp_path, monkeypatch, caplog
    ):
        loop = _loop(tmp_path, monkeypatch)
        claude = _replies("Claude")
        claude["positions"] = {"positions": []}
        codex = _replies("Codex", pick="theirs")
        codex["positions"] = {"positions": []}

        with (
            _agents(claude=claude, codex=codex) as agents,
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            keep_going = run_iteration(loop, 1, ["review"])

        assert keep_going is True
        assert loop.state.results[-1]["summary"] == CRASHED_SUMMARY
        assert "loop] Converged: no fixes agreed" not in caplog.messages
        assert "Neither reviewer answered any of the 1 finding(s)" in caplog.text
        agents.fix.assert_not_called()

    def test_records_disputed_findings_without_fixing_them(self, tmp_path, monkeypatch, caplog):
        loop = _loop(tmp_path, monkeypatch)

        with (
            _agents(codex=_replies("Codex", decision="skip", pick="mine")) as agents,
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            keep_going = run_iteration(loop, 2, ["review"])

        assert keep_going is True
        assert loop.state.ledger == [
            {
                "iteration": 2,
                "outcome": "disputed",
                "phase": "review",
                "severity": "high",
                "file": "app.py",
                "symbol": "load",
                "line": 3,
                "title": "Crash on empty input",
                "reason": "Claude and Codex did not agree",
            }
        ]
        assert "council] Disputed: F1" in caplog.messages
        agents.fix.assert_not_called()

    def test_drops_findings_the_ledger_already_settled(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch)
        loop.state.ledger.append(
            {
                "iteration": 1,
                "outcome": "skipped",
                "phase": "review",
                "severity": "high",
                "file": "app.py",
                "symbol": "load",
                "line": 40,
                "title": "Old",
                "reason": "r",
            }
        )

        with _agents() as agents:
            run_iteration(loop, 2, ["review"])

        assert loop.state.results[-1]["summary"] == "No findings left"
        assert "- app.py:load · skipped · Old (r)" in agents.ask.call_args_list[0].args[0]


class TestFix:
    def test_claude_fixes_the_agreed_findings_and_the_loop_continues(
        self, tmp_path, monkeypatch, caplog
    ):
        loop = _loop(tmp_path, monkeypatch)

        with _agents() as agents, caplog.at_level(logging.INFO, logger="improve"):
            keep_going = run_iteration(loop, 1, ["review"])

        assert keep_going is True
        assert "  Agreed approach: Claude approach" in agents.fix.call_args.args[0]
        assert "[Claude] Crash on empty input" in agents.fix.call_args.args[0]
        assert agents.fix.call_args.kwargs == {"config": loop.config}
        agents.push.assert_called_once_with("Fix guard empty input", "feature", ["app.py"])
        result = loop.state.results[-1]
        assert (result["changes_made"], result["files"], result["summary"]) == (
            True,
            ["app.py"],
            "Guard empty input",
        )
        assert (result["ci_passed"], result["ci_retries"], result["ci_seconds"]) == (True, 0, 0.0)
        assert [(e["outcome"], e["reason"]) for e in loop.state.ledger] == [
            ("fixed", "Claude approach")
        ]
        assert "council] Claude is fixing 1 agreed finding(s)..." in caplog.messages
        assert "council] Fixed: F1" in caplog.messages

    def test_uses_codex_approach_when_claude_picks_theirs(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch)
        claude = _replies("Claude", pick="theirs")
        codex = _replies("Codex", pick="mine")

        with _agents(claude=claude, codex=codex) as agents:
            run_iteration(loop, 1, ["review"])

        assert "Agreed approach: Codex approach" in agents.fix.call_args.args[0]

    def test_records_time_spent_by_each_agent(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch)

        with (
            _agents(),
            patch("improve.council.time.monotonic", side_effect=itertools.count(100)),
        ):
            run_iteration(loop, 1, ["review"])

        result = loop.state.results[-1]
        assert (result["claude_seconds"], result["codex_seconds"]) == (11.0, 9.0)
        assert result["duration_seconds"] == 1.0

    def test_skips_the_findings_and_reviews_again_when_the_fix_changes_nothing(
        self, tmp_path, monkeypatch, caplog
    ):
        loop = _loop(tmp_path, monkeypatch)

        with (
            _agents(fixed_files=()) as agents,
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            keep_going = run_iteration(loop, 1, ["review"])

        assert keep_going is True
        assert loop.state.results[-1]["summary"] == "Agreed fixes changed no files"
        assert [(e["outcome"], e["reason"]) for e in loop.state.ledger] == [
            ("skipped", "The agreed fix changed no files")
        ]
        assert (
            "loop] Agreed fixes changed no files, reviewing again next iteration" in caplog.messages
        )
        agents.push.assert_not_called()


class TestPassesWithoutAFix:
    def test_two_passes_in_a_row_without_a_fix_stop_the_loop(self, tmp_path, monkeypatch, caplog):
        loop = _loop(tmp_path, monkeypatch)

        with (
            _agents(claude=EMPTY, codex=EMPTY),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            first = run_iteration(loop, 1, ["review"])
            second = run_iteration(loop, 2, ["review"])

        assert (first, second) == (True, False)
        assert [m for m in caplog.messages if m.startswith("loop]")] == [
            "loop] No findings left, reviewing again next iteration",
            "loop] Converged: no findings left",
        ]

    def test_a_pass_that_ships_a_fix_continues_after_a_pass_without_one(
        self, tmp_path, monkeypatch
    ):
        loop = _loop(tmp_path, monkeypatch)
        loop.state.add(PhaseResult.no_changes(1, COUNCIL))

        with _agents():
            keep_going = run_iteration(loop, 2, ["review"])

        assert keep_going is True

    def test_a_pass_that_ships_a_fix_resets_the_count(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch)
        loop.state.add(PhaseResult.no_changes(1, COUNCIL))
        loop.state.add(PhaseResult(2, COUNCIL, True, ["app.py"], "Guard empty input", True, 0))

        with _agents(claude=EMPTY, codex=EMPTY):
            keep_going = run_iteration(loop, 3, ["review"])

        assert keep_going is True

    def test_a_crash_does_not_count_as_a_pass_without_a_fix(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch)
        loop.state.add(PhaseResult.crashed(1, COUNCIL))

        with _agents(claude=EMPTY, codex=EMPTY):
            keep_going = run_iteration(loop, 2, ["review"])

        assert keep_going is True

    def test_a_crash_between_two_passes_without_a_fix_does_not_reset_the_count(
        self, tmp_path, monkeypatch, caplog
    ):
        loop = _loop(tmp_path, monkeypatch)
        loop.state.add(PhaseResult.no_changes(1, COUNCIL))
        loop.state.add(PhaseResult.crashed(2, COUNCIL))

        with (
            _agents(claude=EMPTY, codex=EMPTY),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            keep_going = run_iteration(loop, 3, ["review"])

        assert keep_going is False
        assert caplog.messages[-1] == "loop] Converged: no findings left"


class TestSessions:
    def test_reviews_start_new_sessions_and_rounds_resume_them(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch)

        with _agents() as agents:
            run_iteration(loop, 1, ["review"])

        claude_calls = [call.args for call in agents.ask.call_args_list]
        assert [args[3] for args in claude_calls] == [False, True, True]
        assert len({args[2] for args in claude_calls}) == 1
        assert all(args[4] is loop.config for args in claude_calls)
        codex_calls = agents.codex.call_args_list
        assert [call.kwargs.get("thread", "") for call in codex_calls] == [
            "",
            "thread-1",
            "thread-1",
        ]
        assert all(call.args[2] is loop.config for call in codex_calls)

    def test_both_reviewers_get_the_same_prompt_and_schema(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch)

        with _agents() as agents:
            run_iteration(loop, 1, ["review", "security"])

        claude_review = agents.ask.call_args_list[0].args
        codex_review = agents.codex.call_args_list[0].args
        assert claude_review[:2] == codex_review[:2]
        assert claude_review[0].startswith("You are one of two independent reviewers")
        assert "Files changed on this branch (vs main):\napp.py\nlib.py\n\n" in claude_review[0]
        items = claude_review[1]["properties"]["findings"]["items"]
        assert items["properties"]["phase"]["enum"] == ["review", "security"]

    def test_a_new_iteration_has_a_fresh_claude_session_and_no_codex_thread(
        self, tmp_path, monkeypatch
    ):
        loop = _loop(tmp_path, monkeypatch)

        with patch("improve.git.changed_files", return_value=["notes.md"]):
            iteration = CouncilIteration(loop, 1, ["review"])

        assert uuid.UUID(iteration.claude_session).version == 4
        assert iteration.codex_thread == ""
        assert iteration.baseline == ["notes.md"]


class TestStrayEdits:
    def test_discards_files_a_reviewer_changed(self, tmp_path, monkeypatch, caplog):
        loop = _loop(tmp_path, monkeypatch)
        (tmp_path / "stray.py").write_text("x")

        with (
            _agents(claude=EMPTY, codex=EMPTY, stray=("stray.py",), root=str(tmp_path)),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            run_iteration(loop, 1, ["review"])

        assert not (tmp_path / "stray.py").exists()
        assert caplog.messages == [
            "council] Discarding 1 file(s) changed during the iteration: stray.py"
        ]

    def test_stops_the_loop_when_a_reviewer_commits_during_the_iteration(
        self, tmp_path, monkeypatch, caplog
    ):
        loop = _loop(tmp_path, monkeypatch)

        with (
            _agents(claude=EMPTY, codex=EMPTY, heads=["sha-1", "sha-2", "sha-2"]) as agents,
            caplog.at_level(logging.ERROR, logger="improve"),
        ):
            keep_going = run_iteration(loop, 1, ["review"])

        assert keep_going is False
        agents.push.assert_not_called()
        assert loop.state.results[-1]["summary"] == CRASHED_SUMMARY
        assert caplog.messages == [
            "council] Iteration crashed",
            "council] Failed to discard changes after crash, agent edits are still in "
            "the working tree — stopping",
        ]

    def test_a_reviewer_commit_marks_the_branch_unsafe_to_squash(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch)

        with _agents(claude=EMPTY, codex=EMPTY, heads=["sha-1", "sha-2", "sha-2"]):
            run_iteration(loop, 1, ["review"])

        assert loop.unsafe_to_squash is True

    def test_a_clean_iteration_leaves_the_branch_safe_to_squash(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch)

        with _agents(claude=EMPTY, codex=EMPTY):
            run_iteration(loop, 1, ["review"])

        assert loop.unsafe_to_squash is False

    def test_stops_the_loop_when_git_cannot_read_head(self, tmp_path, monkeypatch, caplog):
        loop = _loop(tmp_path, monkeypatch)

        with (
            _agents(claude=EMPTY, codex=EMPTY, heads=["", "", ""]) as agents,
            caplog.at_level(logging.ERROR, logger="improve"),
        ):
            keep_going = run_iteration(loop, 1, ["review"])

        assert keep_going is False
        agents.push.assert_not_called()
        assert loop.state.results[-1]["summary"] == CRASHED_SUMMARY

    def test_the_commit_the_council_pushed_is_not_treated_as_a_stray_commit(
        self, tmp_path, monkeypatch, caplog
    ):
        loop = _loop(tmp_path, monkeypatch, skip_ci=False)

        with (
            _agents(heads=["sha-1", "sha-1", "sha-1", "sha-1", "sha-2"]) as agents,
            caplog.at_level(logging.ERROR, logger="improve"),
        ):
            agents.wait.side_effect = RuntimeError("gh exploded")
            run_iteration(loop, 1, ["review"])

        agents.push.assert_called_once()
        assert caplog.messages == [
            "council] Iteration crashed",
            "council] Crashed after pushing, CI result unknown, stopping",
        ]

    def test_resets_staged_changes_before_restoring_them(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch)

        with _agents(
            claude=EMPTY,
            codex=EMPTY,
            stray=("staged.py",),
            tracked=("staged.py",),
            root=str(tmp_path),
        ) as agents:
            run_iteration(loop, 1, ["review"])

        root = str(tmp_path)
        assert agents.git.call_args_list == [
            call(["git", "-C", root, "reset", "-q", "--", "staged.py"]),
            call(["git", "-C", root, "ls-files", "-z", "--", "staged.py"]),
            call(["git", "-C", root, "checkout", "-q", "--", "staged.py"]),
        ]

    def test_a_tracked_stray_file_is_restored_and_not_deleted(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch)
        (tmp_path / "src.py").write_text("edited by a reviewer")

        with _agents(
            claude=EMPTY, codex=EMPTY, stray=("src.py",), tracked=("src.py",), root=str(tmp_path)
        ) as agents:
            run_iteration(loop, 1, ["review"])

        assert (tmp_path / "src.py").exists()
        checkout = call(["git", "-C", str(tmp_path), "checkout", "-q", "--", "src.py"])
        assert checkout in agents.git.call_args_list

    def test_a_tracked_stray_file_is_restored_when_run_from_a_subdirectory(
        self, tmp_path, monkeypatch
    ):
        root = _repo_with_subdirectory(tmp_path / "repo")
        monkeypatch.chdir(root / "sub")
        (root / "src.py").write_text("edited by a reviewer")
        loop = _loop(tmp_path, monkeypatch)

        with (
            patch("improve.git.repo_root", return_value=str(root)),
            patch("improve.git.changed_files", return_value=[]),
            patch("improve.git.changed_files_since", return_value=["src.py"]),
        ):
            CouncilIteration(loop, 1, ["review"])._discard_stray_edits()

        assert (root / "src.py").read_text() == "committed\n"

    def test_nothing_is_checked_out_when_every_stray_file_is_untracked(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch)

        with _agents(claude=EMPTY, codex=EMPTY, stray=("new.py",), root=str(tmp_path)) as agents:
            run_iteration(loop, 1, ["review"])

        assert [c.args[0][3] for c in agents.git.call_args_list] == ["reset", "ls-files"]

    def test_names_at_most_five_stray_files(self, tmp_path, monkeypatch, caplog):
        loop = _loop(tmp_path, monkeypatch)
        stray = tuple(f"f{i}.py" for i in range(6))

        with (
            _agents(claude=EMPTY, codex=EMPTY, stray=stray, root=str(tmp_path)),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            run_iteration(loop, 1, ["review"])

        assert caplog.messages == [
            "council] Discarding 6 file(s) changed during the iteration:"
            " f0.py, f1.py, f2.py, f3.py, f4.py"
        ]

    def test_checks_for_stray_edits_again_after_the_rounds(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch)

        with _agents(
            codex=_replies("Codex", pick="mine"), stray=("s.py",), root=str(tmp_path)
        ) as agents:
            run_iteration(loop, 1, ["review"])

        assert [c.args[0][3] for c in agents.git.call_args_list] == [
            "reset",
            "ls-files",
            "reset",
            "ls-files",
        ]

    def test_leaves_the_tree_alone_when_reviewers_changed_nothing(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch)

        with _agents() as agents:
            run_iteration(loop, 1, ["review"])

        agents.git.assert_not_called()

    def test_a_stray_file_is_kept_when_git_cannot_list_which_files_are_tracked(
        self, tmp_path, monkeypatch, caplog
    ):
        loop = _loop(tmp_path, monkeypatch)
        (tmp_path / "src.py").write_text("edited by a reviewer")

        def failed_ls_files(cmd, timeout=120):
            if cmd[3] == "ls-files":
                return _cp(returncode=128, stderr="fatal: unable to read index")
            return _cp()

        with (
            _agents(claude=EMPTY, codex=EMPTY, stray=("src.py",), root=str(tmp_path)) as agents,
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            agents.git.side_effect = failed_ls_files
            keep_going = run_iteration(loop, 1, ["review"])

        assert (tmp_path / "src.py").read_text() == "edited by a reviewer"
        assert keep_going is False
        assert (
            "council] Failed to discard changes after crash, agent edits are still in "
            "the working tree — stopping"
        ) in caplog.messages

    def test_a_stray_file_is_kept_when_git_cannot_restore_it(self, tmp_path, monkeypatch, caplog):
        loop = _loop(tmp_path, monkeypatch)
        (tmp_path / "src.py").write_text("edited by a reviewer")

        def failed_checkout(cmd, timeout=120):
            if cmd[3] == "checkout":
                return _cp(returncode=1, stderr="error: unable to unlink src.py")
            return _cp(stdout="src.py") if cmd[3] == "ls-files" else _cp()

        with (
            _agents(
                claude=EMPTY,
                codex=EMPTY,
                stray=("src.py",),
                tracked=("src.py",),
                root=str(tmp_path),
            ) as agents,
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            agents.git.side_effect = failed_checkout
            keep_going = run_iteration(loop, 1, ["review"])

        assert (tmp_path / "src.py").read_text() == "edited by a reviewer"
        assert keep_going is False
        assert (
            "council] Failed to discard changes after crash, agent edits are still in "
            "the working tree — stopping"
        ) in caplog.messages

    def test_an_untracked_stray_file_is_kept_when_git_cannot_locate_the_repo_root(
        self, tmp_path, monkeypatch, caplog
    ):
        loop = _loop(tmp_path, monkeypatch)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "new.py").write_text("written by a reviewer")

        with (
            _agents(claude=EMPTY, codex=EMPTY, stray=("new.py",), root=""),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            keep_going = run_iteration(loop, 1, ["review"])

        assert (tmp_path / "new.py").read_text() == "written by a reviewer"
        assert keep_going is False
        assert (
            "council] Failed to discard changes after crash, agent edits are still in "
            "the working tree — stopping"
        ) in caplog.messages

    def test_stops_the_loop_when_the_fixer_commits_its_work_instead_of_leaving_it_in_the_tree(
        self, tmp_path, monkeypatch, caplog
    ):
        loop = _loop(tmp_path, monkeypatch)
        heads = ["sha-1", "sha-1", "sha-1", "sha-2", "sha-2"]

        with (
            _agents(fixed_files=(), heads=heads) as agents,
            caplog.at_level(logging.ERROR, logger="improve"),
        ):
            keep_going = run_iteration(loop, 1, ["review"])

        assert keep_going is False
        agents.push.assert_not_called()
        assert loop.state.ledger == []
        assert loop.state.results[-1]["summary"] == CRASHED_SUMMARY


class TestShipping:
    def test_waits_for_ci_and_lets_claude_fix_failures(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch, skip_ci=False)
        loop.retry_ci_fixes.return_value = CIFixResult(True, 1, 4.0, 6.0)

        with _agents() as agents:
            keep_going = run_iteration(loop, 1, ["review"])

        assert keep_going is True
        agents.latest.assert_called_once_with("feature", loop.config)
        agents.wait.assert_called_once_with("feature", loop.config, known_previous_id=41)
        loop.retry_ci_fixes.assert_called_once_with(True, "", "Fix CI after council")
        result = loop.state.results[-1]
        assert (result["ci_retries"], result["ci_seconds"], result["claude_seconds"]) == (
            1,
            13.0,
            15.0,
        )

    def test_skips_ci_when_asked(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch, skip_ci=True)

        with _agents() as agents:
            run_iteration(loop, 1, ["review"])

        agents.latest.assert_not_called()
        agents.wait.assert_not_called()
        loop.retry_ci_fixes.assert_not_called()

    def test_stops_when_the_push_fails(self, tmp_path, monkeypatch, caplog):
        loop = _loop(tmp_path, monkeypatch, skip_ci=False)

        with _agents() as agents, caplog.at_level(logging.WARNING, logger="improve"):
            agents.push.return_value = False
            keep_going = run_iteration(loop, 1, ["review"])

        assert keep_going is False
        result = loop.state.results[-1]
        assert (result["changes_made"], result["ci_passed"]) == (True, False)
        assert loop.state.ledger == []
        agents.wait.assert_not_called()
        assert caplog.messages == [
            "council] Push failed",
            "loop] Stopping: push or CI failed after the council's fixes",
        ]

    def test_stops_when_ci_still_fails_after_claudes_fixes(self, tmp_path, monkeypatch, caplog):
        loop = _loop(tmp_path, monkeypatch, skip_ci=False)
        loop.retry_ci_fixes.return_value = CIFixResult(False, 5, 1.0, 2.0)

        with _agents(), caplog.at_level(logging.WARNING, logger="improve"):
            keep_going = run_iteration(loop, 1, ["review"])

        assert keep_going is False
        assert loop.state.results[-1]["ci_retries"] == 5
        assert caplog.messages == ["loop] Stopping: push or CI failed after the council's fixes"]


class TestCrashes:
    def test_a_crash_is_recorded_and_retried_next_iteration(self, tmp_path, monkeypatch, caplog):
        loop = _loop(tmp_path, monkeypatch)

        with _agents() as agents, caplog.at_level(logging.INFO, logger="improve"):
            agents.ask.side_effect = RuntimeError("usage limit")
            keep_going = run_iteration(loop, 1, ["review"])

        assert keep_going is True
        assert loop.state.results == [asdict(PhaseResult.crashed(1, COUNCIL))]
        agents.git.assert_not_called()
        assert "council] Iteration crashed" in caplog.messages
        assert caplog.messages[-1] == "loop] Retrying council next iteration"

    def test_a_crash_discards_files_changed_since_the_baseline(self, tmp_path, monkeypatch, caplog):
        loop = _loop(tmp_path, monkeypatch)
        (tmp_path / "leftover.py").write_text("x")

        with (
            _agents(stray=("leftover.py",), root=str(tmp_path)) as agents,
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            agents.ask.side_effect = RuntimeError("usage limit")
            keep_going = run_iteration(loop, 1, ["review"])

        assert keep_going is True
        root = str(tmp_path)
        assert agents.git.call_args_list == [
            call(["git", "-C", root, "reset", "-q", "--", "leftover.py"]),
            call(["git", "-C", root, "ls-files", "-z", "--", "leftover.py"]),
        ]
        assert not (tmp_path / "leftover.py").exists()
        assert caplog.messages == [
            "One parallel agent failed, terminating the others",
            "council] Iteration crashed",
            "council] Discarding 1 file(s) changed during the iteration: leftover.py",
        ]

    def test_a_crash_after_pushing_stops_the_loop_because_ci_is_unknown(
        self, tmp_path, monkeypatch, caplog
    ):
        loop = _loop(tmp_path, monkeypatch, skip_ci=False)

        with _agents() as agents, caplog.at_level(logging.ERROR, logger="improve"):
            agents.wait.side_effect = RuntimeError("gh exploded")
            keep_going = run_iteration(loop, 1, ["review"])

        assert keep_going is False
        agents.push.assert_called_once()
        assert caplog.messages[-1] == "council] Crashed after pushing, CI result unknown, stopping"

    def test_stops_after_two_crashes_in_a_row(self, tmp_path, monkeypatch, caplog):
        loop = _loop(tmp_path, monkeypatch)
        loop.state.add(PhaseResult.crashed(1, COUNCIL))

        with _agents() as agents, caplog.at_level(logging.INFO, logger="improve"):
            agents.codex.side_effect = RuntimeError("Codex failed")
            keep_going = run_iteration(loop, 2, ["review"])

        assert keep_going is False
        assert len(loop.state.results) == 2
        assert caplog.messages[-1] == "council] Crashed twice in a row, stopping"

    def test_a_crash_after_a_successful_iteration_is_retried(self, tmp_path, monkeypatch):
        loop = _loop(tmp_path, monkeypatch)
        loop.state.add(PhaseResult.no_changes(1, COUNCIL))

        with _agents() as agents:
            agents.fix.side_effect = RuntimeError("Claude failed")
            keep_going = run_iteration(loop, 2, ["review"])

        assert keep_going is True

    def test_stops_the_loop_when_discarding_changes_after_a_crash_fails(
        self, tmp_path, monkeypatch, caplog
    ):
        loop = _loop(tmp_path, monkeypatch)

        with (
            _agents(stray=("x.py",)) as agents,
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            agents.ask.side_effect = RuntimeError("boom")
            agents.git.side_effect = OSError("disk")
            keep_going = run_iteration(loop, 1, ["review"])

        assert keep_going is False
        assert caplog.messages == [
            "One parallel agent failed, terminating the others",
            "council] Iteration crashed",
            "council] Discarding 1 file(s) changed during the iteration: x.py",
            "council] Failed to discard changes after crash, agent edits are still in "
            "the working tree — stopping",
        ]


class TestMalformedReplies:
    def test_ignores_findings_that_are_not_objects(self, tmp_path, monkeypatch, caplog):
        loop = _loop(tmp_path, monkeypatch)
        claude = _replies("Claude")
        claude["findings"] = {"findings": "not a list"}
        codex = _replies("Codex", pick="theirs")
        codex["findings"] = {"findings": ["junk", FINDING]}

        with (
            _agents(claude=claude, codex=codex) as agents,
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            keep_going = run_iteration(loop, 1, ["review"])

        assert keep_going is True
        assert "reported by Codex" in agents.ask.call_args_list[1].args[0]
        assert "council] Claude reported 0, Codex 1; 1 left after merging" in caplog.messages
