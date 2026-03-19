from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

from improve import ci, claude, git
from improve.config import Config
from improve.phases import build_commit_message, build_phase_prompt, extract_summary
from improve.process import format_duration
from improve.state import CIFixResult, PhaseResult

logger = logging.getLogger("improve")


def run_phase_in_worktree(
    phase: str,
    iteration: int,
    worktree_path: str,
    branch_diff: str,
    context: str,
    config: Config,
) -> PhaseResult:
    phase_start = time.monotonic()
    prompt = build_phase_prompt(phase, branch_diff, context)
    logger.info("%s] Running in worktree...", phase)
    output, total_claude = claude.run_claude(prompt, cwd=worktree_path, quiet=True, config=config)
    files = git.changed_files(worktree_path)
    elapsed = time.monotonic() - phase_start
    if not files:
        logger.info("%s] No changes", phase)
        return PhaseResult.no_changes(iteration, phase, elapsed, total_claude)
    summary = extract_summary(output)
    logger.info("%s] Changed %d file(s): %s", phase, len(files), ", ".join(files[:5]))
    logger.info("%s] Done in %s", phase, format_duration(elapsed))
    return PhaseResult(
        iteration=iteration,
        phase=phase,
        changes_made=True,
        files=files,
        summary=summary,
        ci_passed=True,
        ci_retries=0,
        duration_seconds=elapsed,
        claude_seconds=total_claude,
    )


def _collect_results(
    futures: list[Future[PhaseResult]], phases: list[str], iteration: int
) -> list[PhaseResult]:
    results: list[PhaseResult] = []
    for phase, future in zip(phases, futures, strict=True):
        try:
            results.append(future.result())
        except Exception:
            logger.exception("parallel] Phase %s crashed, skipping", phase)
            results.append(PhaseResult.crashed(iteration, phase))
    return results


def _check_ci_after_batch(
    branch: str,
    pre_batch_run_id: int | None,
    retry_ci_fixes: Callable[..., CIFixResult],
    config: Config,
) -> bool:
    ci_passed, ci_errors, _ci_time = ci.wait_for_ci(
        branch,
        config,
        known_previous_id=pre_batch_run_id,
    )
    ci_passed, *_ = retry_ci_fixes(ci_passed, ci_errors, "Fix CI")
    if not ci_passed:
        logger.warning("loop] Stopping: CI failed")
    return ci_passed


def run_parallel_batch(
    phases: list[str],
    iteration: int,
    branch: str,
    context: str,
    skip_ci: bool,
    add_result: Callable[[PhaseResult], None],
    retry_ci_fixes: Callable[..., CIFixResult],
    config: Config,
) -> bool:
    branch_diff = git.diff_vs_main()
    pre_batch_run_id = ci.get_latest_run_id(branch, config) if not skip_ci else None

    base_dir = tempfile.mkdtemp(prefix="improve-")

    worktrees: dict[str, str] = {}
    try:
        for phase in phases:
            path = os.path.join(base_dir, phase)
            if not git.create_worktree(path):
                return False
            worktrees[phase] = path

        with ThreadPoolExecutor(max_workers=len(phases)) as executor:
            futures = [
                executor.submit(
                    run_phase_in_worktree,
                    phase,
                    iteration,
                    worktrees[phase],
                    branch_diff,
                    context,
                    config,
                )
                for phase in phases
            ]
            results = _collect_results(futures, phases, iteration)

        seen_files: set[str] = set()
        for result in results:
            if not result.changes_made:
                continue
            overlap = seen_files & set(result.files)
            if overlap:
                logger.warning(
                    "parallel] %s overwrites file(s) also changed by earlier phase: %s",
                    result.phase,
                    ", ".join(sorted(overlap)),
                )
            try:
                applied = git.apply_worktree_changes(worktrees[result.phase])
            except OSError:
                logger.exception("parallel] Failed to apply changes from %s", result.phase)
                result.changes_made = False
                result.files = []
                continue
            result.files = applied
            seen_files.update(applied)

        for result in results:
            add_result(result)

        if not any(r.changes_made for r in results):
            logger.info("loop] Converged: no changes in any phase")
            return False

        changed = [r for r in results if r.changes_made]
        if len(changed) == 1:
            message = build_commit_message(changed[0].phase, changed[0].summary)
        else:
            message = "Improve code quality"

        if not git.commit_and_push(message, branch):
            logger.warning("loop] Stopping: push failed")
            return False

        return skip_ci or _check_ci_after_batch(branch, pre_batch_run_id, retry_ci_fixes, config)
    finally:
        for path in worktrees.values():
            git.remove_worktree(path)
        with contextlib.suppress(OSError):
            os.rmdir(base_dir)
