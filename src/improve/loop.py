from __future__ import annotations

import logging
import signal
import sys
import time

from improve import ci, claude, git
from improve.parallel import run_parallel_batch
from improve.process import format_duration
from improve.prompt import (
    build_ci_fix_prompt,
    build_commit_message,
    build_phase_prompt,
    extract_summary,
)
from improve.state import LOG_FILE, STATE_FILE, LoopState, PhaseResult

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
    ):
        self.state = state
        self.skip_ci = skip_ci
        self.batch = batch
        self.squash = squash
        self.parallel = parallel
        self.loop_start: float = 0.0
        self._active_phases: list[str] = list(phases)

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

    def shutdown(self, signum: int, _frame: object) -> None:
        logger.info("signal] Caught %s, shutting down...", signal.Signals(signum).name)
        claude.terminate_active()
        try:
            self.state.save()
        except Exception:
            logger.warning("signal] Failed to save state during shutdown")
        elapsed = time.monotonic() - self.loop_start if self.loop_start else 0
        try:
            self.print_summary(elapsed)
        except Exception:
            logger.warning("signal] Failed to print summary during shutdown")
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
            return PhaseResult(
                iteration=iteration,
                phase=phase,
                changes_made=False,
                files=[],
                summary="No changes needed",
                ci_passed=True,
                ci_retries=0,
                duration_seconds=elapsed,
                claude_seconds=total_claude,
            )
        summary = extract_summary(output)
        logger.info("%s] Changed %d file(s): %s", phase, len(files), ", ".join(files[:5]))

        pre_push_id = ci.get_latest_run_id(self.state.branch) if not skip_ci else None
        pushed = git.commit_and_push(build_commit_message(phase, summary), self.state.branch)
        ci_passed = pushed
        total_ci = 0.0
        retries = 0

        if pushed and not skip_ci:
            ci_passed, ci_errors, ci_time = ci.wait_for_ci(
                self.state.branch, known_previous_id=pre_push_id
            )
            total_ci = ci_time
            ci_passed, retries, fix_claude, fix_ci = self.retry_ci_fixes(
                ci_passed, ci_errors, f"Fix CI after {phase}"
            )
            total_claude += fix_claude
            total_ci += fix_ci

        elapsed = time.monotonic() - phase_start
        duration = format_duration(elapsed)
        if total_ci > 0:
            claude_dur = format_duration(total_claude)
            ci_dur = format_duration(total_ci)
            duration += f" (claude: {claude_dur}, ci: {ci_dur})"
        logger.info("%s] Phase done in %s", phase, duration)
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

    def print_summary(self, total_elapsed: float) -> None:
        total_changed = sum(1 for r in self.state.results if r["changes_made"])
        total_ci_fixes = sum(r["ci_retries"] for r in self.state.results)
        total_claude_time = sum(r.get("claude_seconds", 0) for r in self.state.results)
        total_ci_time = sum(r.get("ci_seconds", 0) for r in self.state.results)

        overhead = format_duration(max(0, total_elapsed - total_claude_time - total_ci_time))
        lines = [
            f"\n{'=' * 60}",
            "RESULTS",
            f"{'=' * 60}",
            f"  Phases run:     {len(self.state.results)}",
            f"  With changes:   {total_changed}",
            f"  CI fixes:       {total_ci_fixes}",
            f"  Total time:     {format_duration(total_elapsed)}",
            f"  Claude time:    {format_duration(total_claude_time)}",
            f"  CI time:        {format_duration(total_ci_time)}",
            f"  Overhead:       {overhead}",
            "",
        ]
        for r in self.state.results:
            marker = "+" if r["changes_made"] else " "
            ci_status = "PASS" if r["ci_passed"] else "FAIL"
            duration = format_duration(r.get("duration_seconds", 0))
            lines.append(
                f"  [{marker}] {r['phase']:10s} | CI:{ci_status} | {duration:>9s} | {r['summary']}"
            )
        lines.append(f"\n  State: {STATE_FILE}")
        lines.append(f"  Log:   {LOG_FILE}")

        summary = "\n".join(lines)
        print(summary)
        logger.debug(summary)

    def _drop_converged_phases(self, results: list[PhaseResult]) -> None:
        converged = {r.phase for r in results if not r.changes_made}
        if not converged:
            return
        self._active_phases = [p for p in self._active_phases if p not in converged]
        logger.info(
            "loop] Dropping converged phase(s): %s — remaining: %s",
            ", ".join(sorted(converged)),
            ", ".join(self._active_phases) or "none",
        )

    def run_batch_iteration(self, iteration: int) -> bool:
        pre_batch_run_id = ci.get_latest_run_id(self.state.branch) if not self.skip_ci else None
        results = []
        for phase in self._active_phases:
            result = self.run_phase(phase, iteration, skip_ci=True)
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
                logger.warning("loop] Stopping: CI failed")
                return False
        return True

    def run_parallel_batch_iteration(self, iteration: int) -> bool:
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
        )
        if phase_results:
            self._drop_converged_phases(phase_results)
        return result

    def run_sequential_iteration(self, iteration: int) -> bool:
        results = []
        for phase in self._active_phases:
            result = self.run_phase(phase, iteration, self.skip_ci)
            self.state.add(result)
            results.append(result)

            if not result.ci_passed:
                logger.warning("loop] Stopping: CI failed after %s", phase)
                return False

        self._drop_converged_phases(results)

        if not any(r.changes_made for r in results):
            logger.info("loop] Converged: no changes in any phase")
            return False

        return True

    def _squash_branch(self) -> None:
        summaries = [r["summary"] for r in self.state.results if r["changes_made"]]
        if not summaries:
            logger.info("loop] No changes to squash")
            return
        message = "Improve code quality\n\n" + "\n".join(f"- {s}" for s in summaries)
        if git.squash_branch(self.state.branch, message):
            logger.info("loop] Squashed all commits into one")
        else:
            logger.warning("loop] Squash failed, commits remain separate")

    def run(self, start_iteration: int, max_iterations: int) -> None:
        self.loop_start = time.monotonic()

        for i in range(start_iteration, max_iterations + 1):
            print(f"\n--- Iteration {i}/{max_iterations} ---")
            logger.info("loop] === Iteration %d/%d ===", i, max_iterations)
            self.state.iteration = i
            self.state.save()

            if not git.sync_with_main(self.state.branch):
                logger.error("loop] Merge conflict could not be resolved, stopping")
                break

            if self.parallel:
                should_continue = self.run_parallel_batch_iteration(i)
            elif self.batch:
                should_continue = self.run_batch_iteration(i)
            else:
                should_continue = self.run_sequential_iteration(i)

            if not should_continue:
                break

        total = time.monotonic() - self.loop_start
        logger.info("loop] Finished in %s", format_duration(total))
        self.print_summary(total)

        if self.squash:
            self._squash_branch()
