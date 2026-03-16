from __future__ import annotations

import logging
import signal
import sys
import time
from dataclasses import replace

from improve import ci, claude, color, git
from improve.parallel import run_parallel_batch
from improve.process import format_duration
from improve.prompt import (
    build_ci_fix_prompt,
    build_commit_message,
    build_phase_prompt,
    extract_summary,
)
from improve.state import LoopState, PhaseResult, format_summary

logger = logging.getLogger("improve")

MAX_CI_RETRIES = 5


class IterationLoop:
    def __init__(
        self,
        state: LoopState,
        skip_ci: bool,
        batch: bool,
        phases: list[str],
        squash: bool = False,
        parallel: bool = False,
        revert_on_fail: bool = False,
        continuous: bool = False,
    ):
        self.state = state
        self.skip_ci = skip_ci
        self.batch = batch
        self.squash = squash
        self.parallel = parallel
        self.revert_on_fail = revert_on_fail
        self.continuous = continuous
        self.loop_start: float = 0.0
        self._active_phases: list[str] = list(phases)

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

    def shutdown(self, signum: int, _frame: object) -> None:
        logger.info("signal] Caught %s, shutting down...", signal.Signals(signum).name)
        try:
            claude.terminate_active()
        except Exception:
            logger.warning("signal] Failed to terminate Claude processes", exc_info=True)
        try:
            self.state.save()
        except Exception:
            logger.warning("signal] Failed to save state during shutdown", exc_info=True)
        try:
            self.print_summary(time.monotonic() - self.loop_start if self.loop_start else 0)
        except Exception:
            logger.warning("signal] Failed to print summary during shutdown", exc_info=True)
        sys.exit(130)

    def retry_ci_fixes(
        self, ci_passed: bool, ci_errors: str, commit_prefix: str
    ) -> tuple[bool, int, float, float]:
        total_claude = 0.0
        total_ci = 0.0
        retries = 0
        while not ci_passed and retries < MAX_CI_RETRIES:
            retries += 1
            logger.info("ci-fix] Attempt %d/%d...", retries, MAX_CI_RETRIES)
            _, claude_time = claude.run_claude(build_ci_fix_prompt(ci_errors))
            total_claude += claude_time
            if not git.has_changes():
                logger.info("ci-fix] No fix produced")
                break
            pre_push_id = ci.get_latest_run_id(self.state.branch)
            if not git.commit_and_push(f"{commit_prefix} (attempt {retries})", self.state.branch):
                logger.warning("ci-fix] Push failed")
                break
            ci_passed, ci_errors, ci_time = ci.wait_for_ci(
                self.state.branch, known_previous_id=pre_push_id
            )
            total_ci += ci_time
        if not ci_passed and retries >= MAX_CI_RETRIES:
            logger.warning("ci-fix] All %d attempts exhausted, CI still failing", MAX_CI_RETRIES)
        return ci_passed, retries, total_claude, total_ci

    def run_phase(self, phase: str, iteration: int, skip_ci: bool) -> PhaseResult:
        phase_start = time.monotonic()
        prompt = build_phase_prompt(phase, git.diff_vs_main(), self.state.context())
        logger.info("%s] Running...", phase)
        output, total_claude = claude.run_claude(prompt)
        files = git.changed_files()
        if not files:
            logger.info("%s] No changes", phase)
            elapsed = time.monotonic() - phase_start
            return PhaseResult.no_changes(iteration, phase, elapsed, total_claude)
        summary = extract_summary(output)
        logger.info("%s] Changed %d file(s): %s", phase, len(files), ", ".join(files[:5]))

        pre_push_id = ci.get_latest_run_id(self.state.branch) if not skip_ci else None
        pushed = git.commit_and_push(build_commit_message(phase, summary), self.state.branch)
        ci_passed, total_ci, retries = pushed, 0.0, 0

        if pushed and not skip_ci:
            ci_passed, ci_errors, total_ci = ci.wait_for_ci(
                self.state.branch, known_previous_id=pre_push_id
            )
            ci_passed, retries, fix_claude, fix_ci = self.retry_ci_fixes(
                ci_passed, ci_errors, f"Fix CI after {phase}"
            )
            total_claude += fix_claude
            total_ci += fix_ci

        elapsed = time.monotonic() - phase_start
        dur = format_duration(elapsed)
        if total_ci > 0:
            dur += f" (claude: {format_duration(total_claude)}, ci: {format_duration(total_ci)})"
        logger.info("%s] Phase done in %s", phase, dur)
        return PhaseResult(
            iteration=iteration,
            phase=phase,
            changes_made=True,
            files=files,
            summary=summary,
            ci_passed=ci_passed,
            ci_retries=retries,
            duration_seconds=elapsed,
            claude_seconds=total_claude,
            ci_seconds=total_ci,
        )

    def _run_phase_safe(self, phase: str, iteration: int, skip_ci: bool) -> PhaseResult:
        try:
            return self.run_phase(phase, iteration, skip_ci)
        except Exception:
            logger.exception("loop] Phase %s crashed, skipping", phase)
            try:
                git.discard_changes()
            except Exception:
                logger.warning("loop] Failed to discard changes after crash", exc_info=True)
            return PhaseResult.crashed(iteration, phase)

    def print_summary(self, total_elapsed: float) -> None:
        print(format_summary(self.state.results, total_elapsed))

    def _drop_converged_phases(self, results: list[PhaseResult]) -> None:
        converged = {
            r.phase for r in results if not r.changes_made and r.summary != "Phase crashed"
        }
        if not converged:
            return
        self._active_phases = [p for p in self._active_phases if p not in converged]
        logger.info(
            "loop] Dropping converged phase(s): %s — remaining: %s",
            ", ".join(sorted(converged)),
            ", ".join(self._active_phases) or "none",
        )

    def _mark_recent_reverted(self, count: int) -> None:
        for r in self.state.results[-count:]:
            if r["changes_made"]:
                r["reverted"] = True
        self.state.save()

    def _revert_batch(self, pre_sha: str, result_count: int) -> bool:
        logger.info("loop] Reverting batch changes (CI failed)")
        if not git.revert_to(pre_sha, self.state.branch):
            logger.warning("loop] Revert failed, changes may be in inconsistent state")
            return False
        self._mark_recent_reverted(result_count)
        return True

    def run_batch_iteration(self, iteration: int) -> bool:
        pre_sha = git.head_sha() if self.revert_on_fail else ""
        pre_batch_run_id = ci.get_latest_run_id(self.state.branch) if not self.skip_ci else None
        results = []
        for phase in self._active_phases:
            result = self._run_phase_safe(phase, iteration, skip_ci=True)
            self.state.add(result)
            results.append(result)

        self._drop_converged_phases(results)

        if not any(r.changes_made for r in results):
            logger.info("loop] Converged: no changes in any phase")
            return False
        if not all(r.ci_passed for r in results):
            logger.warning("loop] Stopping: push failed")
            return False

        if not self.skip_ci:
            ci_passed, ci_errors, _ci_time = ci.wait_for_ci(
                self.state.branch,
                known_previous_id=pre_batch_run_id,
            )
            ci_passed, _, _, _ = self.retry_ci_fixes(ci_passed, ci_errors, "Fix CI")
            if not ci_passed:
                if self.revert_on_fail and pre_sha:
                    return self._revert_batch(pre_sha, len(results))
                logger.warning("loop] Stopping: CI failed")
                return False
        return True

    def run_parallel_batch_iteration(self, iteration: int) -> bool:
        pre_sha = git.head_sha() if self.revert_on_fail else ""
        phase_results: list[PhaseResult] = []

        def _track_result(result: PhaseResult) -> None:
            self.state.add(result)
            phase_results.append(result)

        result = run_parallel_batch(
            phases=self._active_phases,
            iteration=iteration,
            branch=self.state.branch,
            context=self.state.context(),
            skip_ci=self.skip_ci,
            add_result=_track_result,
            retry_ci_fixes=self.retry_ci_fixes,
            revert_sha=pre_sha,
        )
        if phase_results:
            self._drop_converged_phases(phase_results)
        if pre_sha and result and git.head_sha() == pre_sha:
            self._mark_recent_reverted(len(phase_results))
        return result

    def run_sequential_iteration(self, iteration: int) -> bool:
        results = []
        for phase in self._active_phases:
            pre_sha = git.head_sha() if self.revert_on_fail else ""
            result = self._run_phase_safe(phase, iteration, self.skip_ci)

            if not result.ci_passed and self.revert_on_fail and pre_sha:
                logger.info("loop] Reverting %s changes (CI failed)", phase)
                if git.revert_to(pre_sha, self.state.branch):
                    result = replace(result, reverted=True)
                else:
                    logger.warning("loop] Revert failed for %s, stopping", phase)
                    self.state.add(result)
                    return False

            self.state.add(result)
            results.append(result)

            if not result.ci_passed and not self.revert_on_fail:
                logger.warning("loop] Stopping: CI failed after %s", phase)
                return False

        self._drop_converged_phases(results)
        has_changes = any(r.changes_made for r in results)
        if not has_changes:
            logger.info("loop] Converged: no changes in any phase")
        return has_changes

    def _squash_branch(self) -> None:
        kept = self.state.kept_results()
        if not kept:
            logger.info("loop] No changes to squash")
            return
        message = "Improve code quality\n\n" + "\n".join(f"- {r['summary']}" for r in kept)
        if git.squash_branch(self.state.branch, message):
            logger.info("loop] Squashed all commits into one")
        else:
            logger.warning("loop] Squash failed, commits remain separate")

    def run(self, start_iteration: int, max_iterations: int) -> None:
        self.loop_start = time.monotonic()
        for i in range(start_iteration, max_iterations + 1):
            label = str(i) if self.continuous else f"{i}/{max_iterations}"
            sep = color.separator()
            print(f"\n{sep}\n  {color.section_title(f'Iteration {label}')}\n{sep}")
            logger.info("loop] === Iteration %s ===", label)
            self.state.iteration = i
            self.state.save()

            if not git.sync_with_main(self.state.branch):
                logger.error("loop] Merge conflict could not be resolved, stopping")
                break

            if self.parallel:
                keep_going = self.run_parallel_batch_iteration(i)
            elif self.batch:
                keep_going = self.run_batch_iteration(i)
            else:
                keep_going = self.run_sequential_iteration(i)
            if not keep_going:
                break

        total = time.monotonic() - self.loop_start
        logger.info("loop] Finished in %s", format_duration(total))
        self.print_summary(total)
        if self.squash:
            self._squash_branch()
