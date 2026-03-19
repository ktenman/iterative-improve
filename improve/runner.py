from __future__ import annotations

import logging
import signal
import sys
import time

from improve import ci, claude, color, git
from improve.config import Config
from improve.mode import Mode
from improve.parallel import run_parallel_batch
from improve.phases import (
    build_ci_fix_prompt,
    build_commit_message,
    build_phase_prompt,
    build_squash_prompt,
    extract_summary,
    strip_code_fences,
)
from improve.process import format_duration
from improve.state import CIFixResult, LoopState, PhaseResult, format_summary

logger = logging.getLogger("improve")

MAX_CI_RETRIES = 5


class IterationLoop:
    def __init__(
        self,
        state: LoopState,
        skip_ci: bool,
        mode: Mode,
        phases: list[str],
        config: Config,
        squash: bool = False,
        continuous: bool = False,
    ):
        self.state = state
        self.skip_ci = skip_ci
        self.mode = mode
        self.config = config
        self.squash = squash
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
            elapsed = time.monotonic() - self.loop_start if self.loop_start else 0
            print(format_summary(self.state.results, elapsed))
        except Exception:
            logger.warning("signal] Failed to print summary during shutdown", exc_info=True)
        sys.exit(130)

    def retry_ci_fixes(self, ci_passed: bool, ci_errors: str, commit_prefix: str) -> CIFixResult:
        total_claude = 0.0
        total_ci = 0.0
        retries = 0
        while not ci_passed and retries < MAX_CI_RETRIES:
            retries += 1
            logger.info("ci-fix] Attempt %d/%d...", retries, MAX_CI_RETRIES)
            try:
                _, claude_time = claude.run_claude(
                    build_ci_fix_prompt(ci_errors), config=self.config
                )
            except RuntimeError:
                logger.warning("ci-fix] Claude failed, stopping retries", exc_info=True)
                break
            total_claude += claude_time
            if not git.has_changes():
                logger.info("ci-fix] No fix produced")
                break
            pre_push_id = ci.get_latest_run_id(self.state.branch, self.config)
            if not git.commit_and_push(f"{commit_prefix} (attempt {retries})", self.state.branch):
                logger.warning("ci-fix] Push failed")
                break
            ci_passed, ci_errors, ci_time = ci.wait_for_ci(
                self.state.branch, self.config, known_previous_id=pre_push_id
            )
            total_ci += ci_time
        if not ci_passed and retries >= MAX_CI_RETRIES:
            logger.warning("ci-fix] All %d attempts exhausted, CI still failing", MAX_CI_RETRIES)
        return CIFixResult(ci_passed, retries, total_claude, total_ci)

    def run_phase(self, phase: str, iteration: int, skip_ci: bool) -> PhaseResult:
        phase_start = time.monotonic()
        prompt = build_phase_prompt(phase, git.diff_vs_main(), self.state.context())
        logger.info("%s] Running...", phase)
        output, total_claude = claude.run_claude(prompt, config=self.config)
        files = git.changed_files()
        if not files:
            logger.info("%s] No changes", phase)
            elapsed = time.monotonic() - phase_start
            return PhaseResult.no_changes(iteration, phase, elapsed, total_claude)
        summary = extract_summary(output)
        logger.info("%s] Changed %d file(s): %s", phase, len(files), ", ".join(files[:5]))

        pre_push_id = ci.get_latest_run_id(self.state.branch, self.config) if not skip_ci else None
        pushed = git.commit_and_push(build_commit_message(phase, summary), self.state.branch)
        ci_passed, total_ci, retries = pushed, 0.0, 0

        if pushed and not skip_ci:
            ci_passed, ci_errors, total_ci = ci.wait_for_ci(
                self.state.branch, self.config, known_previous_id=pre_push_id
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

    def run_batch_iteration(self, iteration: int) -> bool:
        pre_batch_run_id = (
            ci.get_latest_run_id(self.state.branch, self.config) if not self.skip_ci else None
        )
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

        if self.skip_ci:
            return True

        ci_passed, ci_errors, _ci_time = ci.wait_for_ci(
            self.state.branch,
            self.config,
            known_previous_id=pre_batch_run_id,
        )
        ci_passed, *_ = self.retry_ci_fixes(ci_passed, ci_errors, "Fix CI")
        if not ci_passed:
            logger.warning("loop] Stopping: CI failed")
        return ci_passed

    def run_parallel_batch_iteration(self, iteration: int) -> bool:
        phase_results: list[PhaseResult] = []

        def _track_result(result: PhaseResult) -> None:
            self.state.add(result)
            phase_results.append(result)

        keep_going = run_parallel_batch(
            phases=self._active_phases,
            iteration=iteration,
            branch=self.state.branch,
            context=self.state.context(),
            skip_ci=self.skip_ci,
            add_result=_track_result,
            retry_ci_fixes=self.retry_ci_fixes,
            config=self.config,
        )
        self._drop_converged_phases(phase_results)
        return keep_going

    def run_sequential_iteration(self, iteration: int) -> bool:
        results = []
        for phase in self._active_phases:
            result = self._run_phase_safe(phase, iteration, self.skip_ci)
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
        kept = self.state.kept_results()
        if not kept:
            logger.info("loop] No changes to squash")
            return
        prompt, fallback = build_squash_prompt(git.diff_vs_main(), kept)
        try:
            output, _ = claude.run_claude(prompt, quiet=True, config=self.config)
        except RuntimeError:
            logger.warning(
                "loop] Claude failed during squash, using fallback message", exc_info=True
            )
            output = ""
        message = strip_code_fences(output) or fallback
        if git.squash_branch(self.state.branch, message):
            logger.info("loop] Squashed all commits into one")
        else:
            logger.warning("loop] Squash failed, commits remain separate")

    def run(self, start_iteration: int, max_iterations: int) -> None:
        self.loop_start = time.monotonic()
        sep = color.separator()
        for i in range(start_iteration, max_iterations + 1):
            label = str(i) if self.continuous else f"{i}/{max_iterations}"
            print(f"\n{sep}\n  {color.section_title(f'Iteration {label}')}\n{sep}")
            logger.info("loop] === Iteration %s ===", label)
            self.state.iteration = i
            self.state.save()

            if not git.sync_with_main(self.state.branch):
                logger.error("loop] Merge conflict could not be resolved, stopping")
                break

            if self.mode == Mode.PARALLEL:
                keep_going = self.run_parallel_batch_iteration(i)
            elif self.mode == Mode.BATCH:
                keep_going = self.run_batch_iteration(i)
            else:
                keep_going = self.run_sequential_iteration(i)
            if not keep_going:
                break

        total = time.monotonic() - self.loop_start
        logger.info("loop] Finished in %s", format_duration(total))
        print(format_summary(self.state.results, total))
        if self.squash:
            self._squash_branch()
