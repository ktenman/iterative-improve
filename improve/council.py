from __future__ import annotations

import logging
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from improve import ci, claude, codex, git, process
from improve.config import Config
from improve.council_prompts import build_fix_prompt, build_review_prompt
from improve.findings import AGENTS, Finding, findings_schema, merge_findings, reply_rows
from improve.phases import build_commit_message, extract_summary
from improve.rounds import ask_both, run_rounds
from improve.state import CIFixResult, LedgerEntry, LoopState, PhaseResult

logger = logging.getLogger("improve")

COUNCIL = "council"
NO_AGREEMENT = "Claude and Codex did not agree"
NO_CHANGES = "The agreed fix changed no files"


class IterationContext(Protocol):
    """The parts of the iteration loop that a council iteration uses."""

    state: LoopState
    skip_ci: bool
    config: Config
    unsafe_to_squash: bool

    def retry_ci_fixes(
        self, ci_passed: bool, ci_errors: str, commit_prefix: str
    ) -> CIFixResult: ...


def _git(root: str, args: list[str], what: str) -> str:
    result = process.run(["git", "-C", root, *args])
    if result.returncode != 0:
        raise RuntimeError(f"Failed to {what}: {result.stderr.strip() or f'git {args[0]} failed'}")
    return result.stdout


def _head() -> str:
    return process.run(["git", "rev-parse", "HEAD"]).stdout.strip()


def _tracked(root: str, paths: list[str]) -> list[str]:
    listed = _git(root, ["ls-files", "-z", "--", *paths], "list tracked files")
    return [path for path in listed.split("\0") if path]


class CouncilIteration:
    def __init__(self, loop: IterationContext, iteration: int, phases: list[str]):
        self.loop = loop
        self.state = loop.state
        self.config = loop.config
        self.iteration = iteration
        self.phases = phases
        self.started = time.monotonic()
        self.baseline = git.changed_files()
        self.head = _head()
        self.claude_session = str(uuid.uuid4())
        self.claude_started = False
        self.codex_thread = ""
        self.claude_seconds = 0.0
        self.codex_seconds = 0.0
        self.pushed = False

    def run(self) -> PhaseResult:
        findings = self._review()
        self._discard_stray_edits()
        if not findings:
            return self._converged("No findings left")
        agreement = run_rounds(findings, self.ask)
        self._discard_stray_edits()
        self._record("skipped", agreement.skipped)
        self._record("disputed", [(finding, NO_AGREEMENT) for finding in agreement.disputed])
        if agreement.unanswered and not agreement.fixes:
            raise RuntimeError(
                f"{len(agreement.unanswered)} finding(s) went unanswered and "
                "nothing was agreed to fix"
            )
        if not agreement.fixes:
            return self._converged("No fixes agreed")
        return self._fix(agreement.fixes)

    def ask(self, agent: str, prompt: str, schema: dict) -> dict:
        if agent == "claude":
            data, seconds = claude.ask_claude(
                prompt, schema, self.claude_session, self.claude_started, self.config
            )
            self.claude_started = True
            self.claude_seconds += seconds
            return data
        reply = codex.run_codex(prompt, schema, self.config, thread=self.codex_thread)
        self.codex_thread = reply.thread
        self.codex_seconds += reply.elapsed
        return reply.data

    def _review(self) -> list[Finding]:
        changed = git.diff_vs_main().splitlines()
        if not changed:
            logger.info("council] No files changed vs main, nothing to review")
            return []
        prompt = build_review_prompt(self.phases, changed, self.state.ledger)
        logger.info("council] Claude and Codex are reviewing %d file(s)...", len(changed))
        prompts = {agent: prompt for agent in AGENTS}
        replies = ask_both(self.ask, prompts, findings_schema(self.phases))
        reports = {agent: reply_rows(reply, "findings") for agent, reply in replies.items()}
        findings = merge_findings(reports, changed, self.state.ledger, git.repo_root())
        logger.info(
            "council] Claude reported %d, Codex %d; %d left after merging",
            len(reports["claude"]),
            len(reports["codex"]),
            len(findings),
        )
        return findings

    def _reject_stray_commits(self) -> None:
        if self.pushed:
            return
        head = _head()
        if head and head == self.head:
            return
        raise RuntimeError(
            f"HEAD moved from {self.head or 'unknown'} to {head or 'unknown'} during the "
            "iteration: refusing to push commits nobody reviewed"
        )

    def _discard_stray_edits(self) -> None:
        self._reject_stray_commits()
        stray = git.changed_files_since(self.baseline)
        if not stray:
            return
        logger.warning(
            "council] Discarding %d file(s) changed during the iteration: %s",
            len(stray),
            ", ".join(stray[:5]),
        )
        root = git.repo_root()
        if not root:
            raise RuntimeError("Failed to locate the repo root: refusing to touch stray files")
        _git(root, ["reset", "-q", "--", *stray], "unstage stray edits")
        tracked = _tracked(root, stray)
        if tracked:
            _git(root, ["checkout", "-q", "--", *tracked], "restore stray edits")
        for path in set(stray) - set(tracked):
            Path(root, path).unlink(missing_ok=True)

    def discard_after_crash(self) -> bool:
        try:
            self._discard_stray_edits()
        except Exception:
            logger.error(
                "council] Failed to discard changes after crash, agent edits are still in the "
                "working tree — stopping",
                exc_info=True,
            )
            return False
        return True

    def _record(self, outcome: str, items: list[tuple[Finding, str]]) -> None:
        if not items:
            return
        entries = [
            LedgerEntry(
                iteration=self.iteration,
                outcome=outcome,
                phase=finding.phase,
                severity=finding.severity,
                file=finding.file,
                symbol=finding.symbol,
                line=finding.line,
                title=finding.title,
                reason=reason,
            )
            for finding, reason in items
        ]
        self.state.settle(entries)
        ids = ", ".join(finding.id for finding, _ in items)
        logger.info("council] %s: %s", outcome.capitalize(), ids)

    def _fix(self, fixes: list[tuple[Finding, str]]) -> PhaseResult:
        logger.info("council] Claude is fixing %d agreed finding(s)...", len(fixes))
        output, seconds = claude.run_claude(build_fix_prompt(fixes), config=self.config)
        self.claude_seconds += seconds
        self._reject_stray_commits()
        files = git.changed_files_since(self.baseline)
        if not files:
            self._record("skipped", [(finding, NO_CHANGES) for finding, _ in fixes])
            return self._converged("Agreed fixes changed no files")
        summary = extract_summary(output)
        shipped = self._ship(files, summary, fixes)
        self.claude_seconds += shipped.claude_time
        return replace(
            self._result(summary),
            changes_made=True,
            files=files,
            ci_passed=shipped.passed,
            ci_retries=shipped.retries,
            ci_seconds=shipped.ci_time,
        )

    def _ship(
        self, files: list[str], summary: str, fixes: list[tuple[Finding, str]]
    ) -> CIFixResult:
        branch = self.state.branch
        skip_ci = self.loop.skip_ci
        pre_push_id = None if skip_ci else ci.get_latest_run_id(branch, self.config)
        if not git.commit_and_push(build_commit_message(COUNCIL, summary), branch, files):
            logger.warning("council] Push failed")
            return CIFixResult(False, 0, 0.0, 0.0)
        self.pushed = True
        self._record("fixed", fixes)
        if skip_ci:
            return CIFixResult(True, 0, 0.0, 0.0)
        passed, errors, ci_seconds = ci.wait_for_ci(
            branch, self.config, known_previous_id=pre_push_id
        )
        fixed = self.loop.retry_ci_fixes(passed, errors, "Fix CI after council")
        return fixed._replace(ci_time=fixed.ci_time + ci_seconds)

    def _result(self, summary: str) -> PhaseResult:
        elapsed = time.monotonic() - self.started
        blank = PhaseResult.no_changes(self.iteration, COUNCIL, elapsed, self.claude_seconds)
        return replace(blank, summary=summary, codex_seconds=self.codex_seconds)

    def _converged(self, reason: str) -> PhaseResult:
        logger.info("loop] Converged: %s", reason.lower())
        return self._result(reason)


def _retry_unless_repeated(crashed_before: bool) -> bool:
    if crashed_before:
        logger.error("council] Crashed twice in a row, stopping")
        return False
    logger.info("loop] Retrying council next iteration")
    return True


def run_iteration(loop: IterationContext, iteration: int, phases: list[str]) -> bool:
    crashed_before = loop.state.crashed_last(COUNCIL)
    council = CouncilIteration(loop, iteration, phases)
    try:
        result = council.run()
    except Exception:
        logger.exception("council] Iteration crashed")
        discarded = council.discard_after_crash()
        loop.state.add(PhaseResult.crashed(iteration, COUNCIL))
        if council.pushed:
            logger.error("council] Crashed after pushing, CI result unknown, stopping")
            return False
        if not discarded:
            loop.unsafe_to_squash = True
            return False
        return _retry_unless_repeated(crashed_before)
    loop.state.add(result)
    if result.changes_made and not result.ci_passed:
        logger.warning("loop] Stopping: push or CI failed after the council's fixes")
        return False
    return result.changes_made
